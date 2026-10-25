"""Small Bilibili reader with one narrowly scoped like action; aiohttp only.

Reference (search fields, first-page cid and Chinese subtitle selection):
https://github.com/c1overzz/dsh-bili-scroll/blob/main/vendor/bilibili-mcp/mcp_server.py
Independently implemented HTTP transport; no MCP or bilibili-api dependency.
Reference vendor code is MIT, Copyright (c) 2026 AGI_Ananas; repository changes
MIT, Copyright (c) 2026 c1overzz. Permission notice retained below:

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Credentials: optional explicit JSON file, flat sessdata/bili_jct/buvid3/
dedeuserid keys (case-insensitive, as in upstream). No implicit file/env scan.
No login, arbitrary write API, audio transcription or access-control bypass.
Only explicit like=1 is supported (never unlike/comment/coin/favorite). A subtitle
is one part's text and is NOT evidence that video frames were seen. Media is
video-only 480p from official playurl, fetched without cookies, and a signed CDN
URL is never persisted or shown to a model.
Instances must be used on one asyncio loop, and closed after use.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree

import aiohttp

__all__ = ["BiliClient", "BiliClientError"]
_API = "https://api.bilibili.com"
_SUBTITLE_HOSTS = frozenset({"aisubtitle.hdslb.com", "subtitle.bilibili.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"})
# Media CDN suffix allowlist. Suffix match, never arbitrary hosts.
_MEDIA_SUFFIXES = (".bilivideo.com", ".bilivideo.cn", ".hdslb.com")
# No audio is ever downloaded: playurl is restricted to the video-only 480p tier.
_MEDIA_QUALITY = {"qn": 32, "fnval": 16, "fourk": 0}
# Danmaku live here as one static XML file per part. No cookie, no protobuf, no paging.
_DANMAKU_HOST = "comment.bilibili.com"
_DANMAKU_MAX_BYTES = 4 * 1024 * 1024
_DANMAKU_NODE_LIMIT = 20000
_DANMAKU_TEXT = 60
_DANMAKU_BUCKET_MS = 30000
_MIXIN = (46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
          27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
          37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
          22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52)


class BiliClientError(RuntimeError):
    """Safe public error: never contains raw server messages, URLs or cookies."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def _error(reason: str) -> BiliClientError:
    messages = {
        "credentials": "B站凭证文件无效或不可读（未输出凭证）。",
        "risk_control": "B站风控/请求受限，暂时无法读取；请稍后再试。",
        "authentication": "B站要求有效登录凭证或访问权限。",
        "not_found": "B站视频不存在、已删除或不可访问。",
        "api_error": "B站 API 返回读取失败。",
        "network": "B站网络连接失败（详细异常已隐藏）。",
        "timeout": "B站读取超时。",
        "http_error": "B站 HTTP 读取失败。",
        "redirect": "拒绝 B站 HTTP 重定向。",
        "response_too_large": "B站响应超过大小限制。",
        "invalid_response": "B站返回了无效或不完整的数据。",
        "unsafe_url": "拒绝不在 HTTPS 白名单中的 B站地址。",
        "closed": "B站客户端已经关闭。",
        "private_address": "拒绝解析到私网/环回地址的媒体地址。",
        "media_too_large": "媒体文件超过大小上限，已停止下载。",
        "no_stream": "该视频没有可用的受限视频流（可能需要登录或版权限制）。",
        "missing_cid": "缺少分P标识（cid），无法定位弹幕。",
    }
    return BiliClientError(reason, messages[reason])


def _text(value: object, limit: int = 10000) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))[:limit]


def _bvid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        raise ValueError("bvid 必须是 BV 开头的 12 位视频编号")
    return value


def _comment(value: object, limit: int = 300) -> str:
    """Sanitize one comment into inert text: no tags, no links, no control chars."""
    raw = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    raw = re.sub(r"https?://\S+", " ", raw)
    raw = re.sub(r"www\.\S+", " ", raw)
    raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw[:limit]


def _mid_number(*values: object) -> int:
    """First value that parses as a positive int, else 0.

    The reply API mixes types: row["mid"] is an int while member["mid"] is a string,
    and floor is usually null. Comparing without normalizing silently loses the
    "comment is from the uploader" signal, so every id goes through here.
    """
    for value in values:
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return 0


NESTED_UP_LIMIT = 3
# 楼中楼跟进：最多看 2 个线程，每线程 20 条，只为找作者本人写的话。
NESTED_THREADS = 2
NESTED_PS = 20


def _reply_item(row: object, up_mid: int, *, pinned: bool = False,
                nested: bool = False) -> dict | None:
    """One comment turned into inert, identity-free facts.

    Only floor/likes/text/is_up survive, plus two placement flags: pinned means it
    is the uploader's pinned comment, nested means it came from the reply thread
    under another comment. No uid, nickname, avatar or per-person history is kept.
    """
    if not isinstance(row, dict):
        return None
    content = row.get("content")
    text = _comment(content.get("message") if isinstance(content, dict) else "")
    if not text:
        return None
    member = row.get("member") if isinstance(row.get("member"), dict) else {}
    who = _mid_number(row.get("mid"), member.get("mid"), row.get("mid_str"), member.get("mid_str"))
    likes = row.get("like") if type(row.get("like")) is int else 0
    return {"floor": _mid_number(row.get("floor")) or 0,
            "is_up": bool(up_mid) and who == up_mid,
            "likes": max(0, likes), "text": text,
            "pinned": bool(pinned), "nested": bool(nested)}


def _reply_fingerprint(row: dict) -> str:
    """Stable in-memory identity for dedupe: rpid when present, else the text digest."""
    rpid = _mid_number(row.get("rpid"))
    if rpid:
        return f"r{rpid}"
    content = row.get("content")
    text = _comment(content.get("message") if isinstance(content, dict) else "", 300)
    return f"t{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}" if text else ""


def _parse_danmaku(raw: bytes) -> list[tuple[int, str]]:
    """Parse the legacy danmaku XML into (at_ms, text) pairs; drop anything odd.

    The file is a flat `<i><d p="t,mode,size,...">text</d></i>` list. Only the first
    field (seconds) is used; everything else is presentation. DOCTYPE/ENTITY bodies
    are refused outright so a hostile file cannot expand entities on us.
    """
    if not raw or len(raw) > _DANMAKU_MAX_BYTES:
        return []
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        return []
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError):
        return []
    rows = []
    for index, node in enumerate(root.iter("d")):
        if index >= _DANMAKU_NODE_LIMIT:
            break
        fields = (node.get("p") or "").split(",")
        try:
            seconds = float(fields[0])
        except (ValueError, IndexError):
            continue
        if not 0 <= seconds <= 86400 * 8:
            continue
        text = _comment(node.text or "", _DANMAKU_TEXT)
        if len(text) < 2:
            continue
        rows.append((int(seconds * 1000), text))
    rows.sort()
    return rows


def _sample_danmaku(rows, limit: int, *, bucket_ms: int = _DANMAKU_BUCKET_MS,
                    per_bucket: int = 2) -> tuple[list[dict], int]:
    """Spread the sample over the whole part and keep the heat signal.

    Taking the first N lines would only ever show the opening rush, so entries are
    grouped into fixed time buckets and at most `per_bucket` of them are kept per
    bucket. Identical shouts in the same bucket collapse into one entry carrying
    `count`, which is the honest way to say "this line was spammed a lot" without
    dumping 200 copies into the model context.
    """
    first, counts = {}, {}
    for at_ms, text in rows:
        key = (at_ms // bucket_ms, text.casefold())
        if key in first:
            counts[key] += 1
            continue
        if sum(1 for k in first if k[0] == key[0]) >= per_bucket:
            continue
        first[key] = (at_ms, text)
        counts[key] = 1
    items = []
    for key in sorted(first, key=lambda k: first[k][0]):
        at_ms, text = first[key]
        items.append({"at_ms": at_ms, "text": text, "count": counts[key]})
        if len(items) >= limit:
            break
    return items, len(rows)


def _danmaku_closed(raw: bytes) -> bool:
    """True when B站 itself says this video's danmaku is switched off.

    A real empty file and a switched-off one look almost the same (both `<i>` with no
    `<d>`), but the switched-off one carries a `<text_side>` sentence. Distinguishing
    them keeps the answer honest: "nobody has written danmaku here" is a different
    claim from "the uploader turned danmaku off".
    """
    if not raw:
        return False
    try:
        root = ElementTree.fromstring(raw)
    except (ElementTree.ParseError, ValueError):
        return False
    for child in root:
        if child.tag == "text_side" and (child.text or "").strip():
            return True
    return False


class BiliClient:
    def __init__(self, credential_file: str | Path | None = None, *,
                 timeout: float = 15, max_response_bytes: int = 2 * 1024 * 1024,
                 max_subtitle_chars: int = 50000):
        if not 0 < timeout <= 60:
            raise ValueError("timeout 必须在 (0, 60] 秒之间")
        if not 1024 <= max_response_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_response_bytes 必须在 1024 到 8MiB 之间")
        if not 1 <= max_subtitle_chars <= 200000:
            raise ValueError("max_subtitle_chars 必须在 1 到 200000 之间")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_subtitle_chars = max_subtitle_chars
        self._cookies = self._load_credentials(credential_file)
        self._session = None
        self._closed = False
        self._wbi_key = None
        self._wbi_time = 0.0
        self._like_lock = asyncio.Lock()

    @staticmethod
    def _load_credentials(filename):
        if filename is None:
            return {}
        try:
            with Path(filename).open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError()
            allowed = {"sessdata": "SESSDATA", "bili_jct": "bili_jct",
                       "buvid3": "buvid3", "dedeuserid": "DedeUserID"}
            cookies = {}
            for key, value in data.items():
                name = allowed.get(str(key).lower())
                if name is None or value in (None, ""):
                    continue
                if not isinstance(value, (str, int)):
                    raise ValueError()
                value = str(value)
                if len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 or c in ';,"\\' for c in value):
                    raise ValueError()
                cookies[name] = value
            return cookies
        except (OSError, ValueError, TypeError, RecursionError):
            raise _error("credentials") from None

    def reload_credentials(self, credential_file):
        """Explicit operator-triggered refresh; enables QR login without service restart."""
        self._cookies = self._load_credentials(credential_file)
        self._wbi_key = None
        self._wbi_time = 0.0

    async def __aenter__(self):
        if self._closed:
            raise _error("closed")
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self):
        self._closed = True
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._cookies.clear()

    @staticmethod
    def _validate_url(url: str, subtitle: bool):
        try:
            parts = urlsplit(url)
            hosts = _SUBTITLE_HOSTS if subtitle else {"api.bilibili.com"}
            valid = (parts.scheme == "https" and parts.hostname in hosts
                     and parts.port in (None, 443) and not parts.username
                     and not parts.password and not parts.fragment
                     and not any(ord(c) < 33 or ord(c) > 126 or c == "\\" for c in url))
            if not valid:
                raise ValueError()
        except (ValueError, TypeError):
            raise _error("unsafe_url") from None

    async def _json(self, url: str, params=None, *, subtitle: bool = False,
                    _like_bvid: str | None = None):
        self._validate_url(url, subtitle)
        body = None
        if _like_bvid is not None:
            # This is NOT a general POST transport: callers cannot choose a path,
            # operation, extra fields, query string or CSRF location.
            if url != _API + "/x/web-interface/archive/like" or params is not None or subtitle:
                raise _error("unsafe_url")
            if not self._cookies.get("SESSDATA") or not self._cookies.get("bili_jct"):
                raise _error("authentication")
            body = {"bvid": _bvid(_like_bvid), "like": 1, "csrf": self._cookies["bili_jct"]}
        if self._closed:
            raise _error("closed")
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
            # No redirect following; cookies only on fixed API host, NEVER on CDN.
            if body is None:
                request = self._session.get(url, params=params, allow_redirects=False,
                                            cookies=None if subtitle else self._cookies)
            else:
                # Exactly one attempt. Never retry an ambiguous write failure.
                request = self._session.post(_API + "/x/web-interface/archive/like",
                                             data=body, allow_redirects=False,
                                             cookies=self._cookies)
            async with request as response:
                if 300 <= response.status < 400:
                    raise _error("redirect")
                if response.status in (403, 412, 429):
                    raise _error("risk_control")
                if response.status in (401,):
                    raise _error("authentication")
                if response.status != 200:
                    raise _error("http_error")
                if response.content_length and response.content_length > self.max_response_bytes:
                    raise _error("response_too_large")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > self.max_response_bytes:
                        raise _error("response_too_large")
                data = json.loads(raw.decode("utf-8-sig"))
                if not isinstance(data, dict):
                    raise _error("invalid_response")
                return data
        except BiliClientError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise _error("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise _error("network") from None
        except (UnicodeError, ValueError, TypeError, RecursionError):
            raise _error("invalid_response") from None

    async def _api(self, path: str, params=None):
        payload = await self._json(_API + path, params)
        data = self._api_data(payload)
        if not isinstance(data, dict):
            raise _error("invalid_response")
        return data

    @staticmethod
    def _api_data(payload):
        """Validate the envelope without assuming dict/list/scalar data shape."""
        code = payload.get("code")
        if type(code) is not int:
            raise _error("invalid_response")
        if code != 0:
            reason = ("risk_control" if code in (-412, -352, -509, -799) else
                      "authentication" if code in (-101, -111, -403, 61000) else
                      "not_found" if code in (-404, 62002) else "api_error")
            raise _error(reason)
        return payload.get("data")

    async def _signed(self, params):
        if self._wbi_key is None or time.monotonic() - self._wbi_time > 1800:
            # Anonymous nav returns code=-101 but still supplies public WBI keys.
            nav = await self._json(_API + "/x/web-interface/nav")
            if nav.get("code") not in (0, -101):
                raise _error("risk_control" if nav.get("code") in (-412, -352) else "api_error")
            try:
                images = nav["data"]["wbi_img"]
                key = "".join(urlsplit(images[name]).path.rsplit("/", 1)[-1].split(".")[0]
                              for name in ("img_url", "sub_url"))
                if not re.fullmatch(r"[a-fA-F0-9]{64}", key):
                    raise ValueError()
                self._wbi_key = "".join(key[i] for i in _MIXIN)[:32]
                self._wbi_time = time.monotonic()
            except (KeyError, IndexError, TypeError, ValueError, AttributeError):
                raise _error("invalid_response") from None
        values = dict(params, wts=int(time.time()))
        values = {k: re.sub(r"[!'()*]", "", str(v)) for k, v in sorted(values.items())}
        values["w_rid"] = hashlib.md5((urlencode(values) + self._wbi_key).encode()).hexdigest()
        return values

    async def search(self, keyword: str, limit: int = 5, *,
                     page: int = 1, order: str = "pubdate") -> list[dict]:
        """Search public videos without requiring credentials; failures raise safe errors."""
        if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 200:
            raise ValueError("keyword 必须是 1 到 200 字符的非空字符串")
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        if type(page) is not int or not 1 <= page <= 1000:
            raise ValueError("page 必须在 1 到 1000 之间")
        if not isinstance(order, str) or order not in {"pubdate", "totalrank", "click", "dm", "stow", "scores"}:
            raise ValueError("order 必须是支持的 B站排序方式")
        # Search API returns at most 50 hits on a single page.
        params = await self._signed({"keyword": keyword.strip(), "search_type": "video",
                                     "page": page, "page_size": 50, "order": order})
        data = await self._api("/x/web-interface/wbi/search/type", params)
        items = data.get("result", [])
        return self._normalize_videos(items, limit)

    @staticmethod
    def _normalize_videos(items, limit):
        if not isinstance(items, list):
            raise _error("invalid_response")
        result, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", str(item.get("bvid", ""))):
                continue
            bvid = item["bvid"]
            if bvid in seen:
                continue
            seen.add(bvid)
            owner = item.get("owner")
            author = item.get("author") or (owner.get("name") if isinstance(owner, dict) else "")
            result.append({"bvid": bvid, "title": _text(item.get("title")),
                           "author": _text(author, 1000),
                           "description": _text(item.get("description") or item.get("desc")),
                           "url": f"https://www.bilibili.com/video/{bvid}"})
            if len(result) >= limit:
                break
        return result

    async def recommendations(self, limit: int = 10) -> list[dict]:
        """Official web homepage feed, using nav-derived WBI signatures."""
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        params = await self._signed({"fresh_type": 3, "ps": limit, "fresh_idx": 1,
                                     "fresh_idx_1h": 1, "feed_version": "V8"})
        data = await self._api("/x/web-interface/wbi/index/top/feed/rcmd", params)
        return self._normalize_videos(data.get("item", []), limit)

    async def related(self, bvid: str, limit: int = 10) -> list[dict]:
        """Related archive endpoint has list data (unlike most API endpoints)."""
        bvid = _bvid(bvid)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("limit 必须在 1 到 50 之间")
        payload = await self._json(_API + "/x/web-interface/archive/related", {"bvid": bvid})
        return self._normalize_videos(self._api_data(payload), limit)

    async def has_liked(self, bvid: str) -> bool:
        """Read official current-account like state; reject unknown values."""
        payload = await self._json(_API + "/x/web-interface/archive/has/like", {"bvid": _bvid(bvid)})
        data = self._api_data(payload)
        if type(data) is not int or data not in (0, 1):
            raise _error("invalid_response")
        return data == 1

    async def like(self, bvid: str) -> dict:
        """Like only, never unlike. Network/timeout errors mean uncertain outcome.

        No write retry or optimistic success. Callers must persist an uncertain
        outcome on ambiguous errors/cancellation rather than automatically retry.
        """
        bvid = _bvid(bvid)
        async with self._like_lock:
            if not self._cookies.get("SESSDATA") or not self._cookies.get("bili_jct"):
                raise _error("authentication")
            if await self.has_liked(bvid):
                return {"status": "already_liked"}
            payload = await self._json(_API + "/x/web-interface/archive/like", _like_bvid=bvid)
            self._api_data(payload)  # code=0 only; write responses may have null data.
            return {"status": "liked"}

    async def detail(self, bvid: str) -> dict:
        """Return normalized public metadata, NOT proof of watching the video.

        `parts` is the ordered per-page list (cid/page/part_title/duration) so
        callers can resolve ?p=N without guessing or trusting a caller-supplied cid.
        """
        bvid = _bvid(bvid)
        data = await self._api("/x/web-interface/view", {"bvid": bvid})
        owner = data.get("owner") or {}
        pages = data.get("pages") or []
        if not isinstance(owner, dict) or not isinstance(pages, list):
            raise _error("invalid_response")
        cid = data.get("cid")
        if not cid and pages and isinstance(pages[0], dict):
            cid = pages[0].get("cid")
        parts = []
        for index, row in enumerate(pages, start=1):
            if not isinstance(row, dict):
                continue
            page_cid = row.get("cid")
            if type(page_cid) is not int or page_cid <= 0:
                continue
            parts.append({
                "page": row.get("page") if type(row.get("page")) is int else index,
                "cid": page_cid,
                "part_title": _text(row.get("part"), 200),
                "duration": row.get("duration") if type(row.get("duration")) is int else None,
            })
        return {"bvid": bvid, "title": _text(data.get("title")),
                "author": _text(owner.get("name"), 1000),
                "description": _text(data.get("desc")),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "aid": data.get("aid") if type(data.get("aid")) is int and data.get("aid") > 0 else None,
                "owner_mid": owner.get("mid") if type(owner.get("mid")) is int else None,
                "cid": cid if type(cid) is int and cid > 0 else None,
                "duration": data.get("duration"), "pubdate": data.get("pubdate"),
                "pages": len(pages), "parts": parts}

    async def replies(self, bvid: str, limit: int = 8) -> dict:
        """Read one bounded snapshot of comments: first page + pinned + a few nested UP replies.

        Read-only, no cookie. Comments are AUDIENCE speech, never the uploader's
        argument. Author identity is reduced to `is_up` on purpose: no uid, no
        per-person history, no profile. The pinned comment is read first, and nested
        replies are read exactly one level deep — at most NESTED_UP_LIMIT of the
        uploader's own answers, and only those the first page already carries as a
        preview, so this never turns into crawling. Text is sanitized (tags/links/
        controls removed, length capped) so a comment cannot smuggle instructions or
        URLs into the model context.
        """
        bvid = _bvid(bvid)
        limit = max(1, min(int(limit or 8), 20))
        meta = await self.detail(bvid)
        aid = meta.get("aid")
        if type(aid) is not int or aid <= 0:
            return {"bvid": bvid, "source": "bilibili_replies", "reason": "no_aid", "items": []}
        try:
            data = await self._api("/x/v2/reply", {"type": 1, "oid": aid, "sort": 2, "pn": 1, "ps": limit})
        except BiliClientError as exc:
            return {"bvid": bvid, "source": "bilibili_replies", "reason": exc.reason, "items": []}
        if not isinstance(data, dict):
            return {"bvid": bvid, "source": "bilibili_replies", "reason": "no_replies", "items": []}
        upper = data.get("upper") if isinstance(data.get("upper"), dict) else {}
        up_mid = _mid_number(upper.get("mid"))
        rows = data.get("replies") if isinstance(data.get("replies"), list) else []
        items, seen = [], set()

        def take(row, *, pinned=False, nested=False) -> bool:
            item = _reply_item(row, up_mid, pinned=pinned, nested=nested)
            if item is None:
                return False
            fingerprint = _reply_fingerprint(row)
            if fingerprint and fingerprint in seen:
                return False
            if fingerprint:
                seen.add(fingerprint)
            items.append(item)
            return True

        # 置顶评论：UP 主自己顶上去的那条，常常是补充说明或勘误，优先读。
        take(upper.get("top"), pinned=True)
        nested_up = 0

        def take_child(children, *, budget) -> int:
            """Collect the uploader's own answers out of one thread, capped."""
            found = 0
            for child in children:
                if found >= budget or len(items) >= limit:
                    break
                if not isinstance(child, dict):
                    continue
                member = child.get("member") if isinstance(child.get("member"), dict) else {}
                child_mid = _mid_number(child.get("mid"), member.get("mid"),
                                        child.get("mid_str"), member.get("mid_str"))
                if not up_mid or child_mid != up_mid:
                    continue
                if take(child, nested=True):
                    found += 1
            return found

        for row in rows:
            if len(items) >= limit:
                break
            take(row)
            if nested_up >= NESTED_UP_LIMIT or not isinstance(row, dict):
                continue
            preview = row.get("replies")
            if isinstance(preview, list):
                nested_up += take_child(preview, budget=NESTED_UP_LIMIT - nested_up)
        # 第一页带来的预览通常只有末尾几条，UP 主的回答常在更深的楼层里。
        # 所以最多再跟进 NESTED_THREADS 个楼中楼（每个 ps=NESTED_PS），只为找作者本人的话；
        # 有界、只 GET、失败就跳过，不会退化成爬取。
        if nested_up < NESTED_UP_LIMIT:
            followed = 0
            for row in rows:
                if nested_up >= NESTED_UP_LIMIT or followed >= NESTED_THREADS or len(items) >= limit:
                    break
                if not isinstance(row, dict):
                    continue
                root = _mid_number(row.get("rpid"))
                rcount = row.get("rcount")
                if not root or type(rcount) is not int or rcount <= 0:
                    continue
                followed += 1
                try:
                    thread = await self._api("/x/v2/reply/reply",
                                             {"oid": aid, "root": root, "type": 1, "pn": 1, "ps": NESTED_PS})
                except BiliClientError:
                    continue
                children = thread.get("replies") if isinstance(thread, dict) else None
                if isinstance(children, list):
                    nested_up += take_child(children, budget=NESTED_UP_LIMIT - nested_up)
        return {"bvid": bvid, "source": "bilibili_replies",
                "reason": "" if items else "no_replies", "items": items,
                "nested_up": nested_up}

    async def danmaku(self, bvid: str, cid: int | None = None, *, limit: int = 40,
                      max_bytes: int = _DANMAKU_MAX_BYTES) -> dict:
        """Read ONE bounded sample of live comments (danmaku) for one part.

        Read-only: a single static XML file, no cookie, no protobuf, no paging, nothing
        posted. Danmaku carry a timestamp but no author flag, so they are audience
        reactions at a moment, never the uploader's view and never usable as evidence
        that the uploader said something. Sampling is bucketed by time so the model sees
        reactions across the whole part instead of only the opening rush.
        """
        bvid = _bvid(bvid)
        limit = max(1, min(int(limit or 40), 120))
        if cid is None:
            try:
                info = await self.detail(bvid)
            except BiliClientError as exc:
                return self._empty_danmaku(bvid, None, exc.reason)
            cid = info.get("cid")
        if type(cid) is not int or cid <= 0:
            return self._empty_danmaku(bvid, None, "missing_cid")
        try:
            raw = await self._danmaku_xml(cid, max_bytes=max_bytes)
        except BiliClientError as exc:
            return self._empty_danmaku(bvid, cid, exc.reason)
        rows = _parse_danmaku(raw)
        if not rows:
            return self._empty_danmaku(bvid, cid, "closed" if _danmaku_closed(raw) else "empty")
        items, total = _sample_danmaku(rows, limit)
        return {"bvid": bvid, "cid": cid, "source": "bilibili_danmaku",
                "reason": "ok", "items": items, "total": total,
                "bucket_ms": _DANMAKU_BUCKET_MS, "sampled": len(items)}

    @staticmethod
    def _empty_danmaku(bvid, cid, reason: str) -> dict:
        return {"bvid": bvid, "cid": cid, "source": "bilibili_danmaku",
                "reason": reason, "items": [], "total": 0,
                "bucket_ms": _DANMAKU_BUCKET_MS, "sampled": 0}

    async def _danmaku_xml(self, cid: int, *, max_bytes: int) -> bytes:
        """Fetch the raw danmaku XML: fixed host, https, no cookie, bounded size."""
        url = f"https://{_DANMAKU_HOST}/{int(cid)}.xml"
        try:
            parts = urlsplit(url)
            if not (parts.scheme == "https" and parts.hostname == _DANMAKU_HOST
                    and parts.port in (None, 443) and not parts.username
                    and not parts.password and not parts.fragment):
                raise ValueError()
        except (ValueError, TypeError):
            raise _error("unsafe_url") from None
        if self._closed:
            raise _error("closed")
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    headers={"User-Agent": "Mozilla/5.0",
                             "Referer": "https://www.bilibili.com/"})
            async with self._session.get(url, allow_redirects=False, cookies=None) as response:
                if 300 <= response.status < 400:
                    raise _error("redirect")
                if response.status == 404:
                    raise _error("not_found")
                if response.status in (403, 412, 429):
                    raise _error("risk_control")
                if response.status != 200:
                    raise _error("http_error")
                if response.content_length and response.content_length > max_bytes:
                    raise _error("response_too_large")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    raw.extend(chunk)
                    if len(raw) > max_bytes:
                        raise _error("response_too_large")
                return bytes(raw)
        except BiliClientError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise _error("timeout") from None
        except (aiohttp.ClientError, OSError):
            raise _error("network") from None


    async def subtitle(self, bvid: str, cid: int | None = None) -> dict:
        """Read one part's subtitle. Empty text NEVER implies video was watched.

        source is a safe provenance label, never a possibly token-bearing CDN URL.
        Operational failures are reported in reason rather than mistaken for no subtitle.
        `segments` keeps real from/to milliseconds; when they are unavailable nothing
        is invented, and summaries/outlines are never treated as a transcript.
        """
        bvid = _bvid(bvid)
        try:
            if cid is not None and (type(cid) is not int or cid <= 0):
                raise _error("invalid_response")
            if cid is None:
                info = await self.detail(bvid)
                cid = info["cid"]
            if not cid:
                return self._empty_subtitle("missing_cid")
            data = await self._api("/x/player/wbi/v2", await self._signed({"bvid": bvid, "cid": cid}))
            subtitle = data.get("subtitle") or {}
            if not isinstance(subtitle, dict):
                raise _error("invalid_response")
            tracks = subtitle.get("subtitles") or []
            if not isinstance(tracks, list) or any(not isinstance(t, dict) for t in tracks):
                raise _error("invalid_response")
            if not tracks:
                reason = "login_required" if data.get("need_login_subtitle") or subtitle.get("need_login_subtitle") else "no_subtitle_or_login_required"
                return self._empty_subtitle(reason)
            target = next((t for t in tracks if t.get("lan") in ("ai-zh", "zh-CN", "zh")), tracks[0])
            url = target.get("subtitle_url")
            if not isinstance(url, str):
                raise _error("unsafe_url")
            if url.startswith("//"):
                url = "https:" + url
            payload = await self._json(url, subtitle=True)
            body = payload.get("body")
            if not isinstance(body, list):
                raise _error("invalid_response")
            fragments, segments, count, truncated = [], [], 0, False
            for row in body:
                if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                    raise _error("invalid_response")
                content = row["content"].strip()
                if not content:
                    continue
                remaining = self.max_subtitle_chars - count
                part = ("\n" if fragments else "") + content
                kept = part[:remaining]
                fragments.append(kept)
                count += len(kept)
                start, end = row.get("from"), row.get("to")
                if type(start) in (int, float) and type(end) in (int, float) and 0 <= start <= end:
                    segments.append({"from_ms": int(round(start * 1000)),
                                     "to_ms": int(round(end * 1000)),
                                     "text": content[:remaining] if remaining > 0 else ""})
                if len(part) > remaining:
                    truncated = True
                    break
            text = "".join(fragments)
            return {"text": text, "source": "bilibili_subtitle:part" if text else "none",
                    "reason": "truncated" if truncated else "ok" if text else "empty_subtitle",
                    "lang": str(target.get("lan") or "")[:32], "segments": segments,
                    "chars": len(text)}
        except BiliClientError as exc:
            return self._empty_subtitle(exc.reason)

    @staticmethod
    def _empty_subtitle(reason: str) -> dict:
        return {"text": "", "source": "none", "reason": reason,
                "lang": "", "segments": [], "chars": 0}

    async def playurl(self, bvid: str, cid: int) -> dict:
        """Official playurl for one part, restricted to the video-only 480p tier.

        Returns metadata plus short-lived signed CDN URLs. Callers must keep those
        URLs out of logs and model prompts; they are never persisted.
        """
        bvid = _bvid(bvid)
        if type(cid) is not int or cid <= 0:
            raise ValueError("cid 必须是正整数")
        params = await self._signed({"bvid": bvid, "cid": cid,
                                     **_MEDIA_QUALITY, "platform": "pc"})
        data = await self._api("/x/player/wbi/playurl", params)
        streams = []
        dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
        videos = dash.get("video") if isinstance(dash.get("video"), list) else []
        for row in videos:
            if not isinstance(row, dict):
                continue
            urls = self._stream_urls(row)
            width, height = row.get("width"), row.get("height")
            if not urls or type(width) is not int or type(height) is not int:
                continue
            if height > 480 or width > 854 or height <= 0 or width <= 0:
                continue
            streams.append({"height": height, "width": width,
                            "codec": str(row.get("codecs") or "")[:32],
                            "bandwidth": row.get("bandwidth") if type(row.get("bandwidth")) is int else 0,
                            "urls": urls, "kind": "dash"})
        durl = data.get("durl") if isinstance(data.get("durl"), list) else []
        for row in durl:
            if not isinstance(row, dict):
                continue
            urls = self._stream_urls(row)
            if urls:
                streams.append({"height": 0, "width": 0, "codec": "durl",
                                "bandwidth": 0, "urls": urls, "kind": "durl"})
        if not streams:
            raise _error("no_stream")
        # Best tier within the cap: the largest height that still fits 480p, then the
        # widest bandwidth, because a bigger frame is better evidence at the same cost.
        streams.sort(key=lambda item: (item["height"] or 0, item["bandwidth"]), reverse=True)
        tiers = [{k: item[k] for k in ("height", "width", "codec", "bandwidth", "urls", "kind")}
                 for item in streams[:4]]
        best = tiers[0]
        return {"bvid": bvid, "cid": cid, "kind": best["kind"], "height": best["height"],
                "width": best["width"], "codec": best["codec"], "urls": best["urls"],
                "bandwidth": best["bandwidth"], "tiers": tiers,
                "quality": data.get("quality") if type(data.get("quality")) is int else None,
                "accept_quality": [q for q in data.get("accept_quality") or [] if type(q) is int]}

    async def audio(self, bvid: str, cid: int) -> dict:
        """Official audio-only stream for one part, from the smallest bitrate tier.

        Only `dash.audio` rows are considered: the ASR model downsamples to 16 kHz
        mono anyway, so the cheapest tier is also the honest one. Signed URLs are
        returned in memory only and are never logged or persisted.
        """
        bvid = _bvid(bvid)
        if type(cid) is not int or cid <= 0:
            raise ValueError("cid 必须是正整数")
        params = await self._signed({"bvid": bvid, "cid": cid,
                                     **_MEDIA_QUALITY, "platform": "pc"})
        data = await self._api("/x/player/wbi/playurl", params)
        dash = data.get("dash") if isinstance(data.get("dash"), dict) else {}
        rows = dash.get("audio") if isinstance(dash.get("audio"), list) else []
        streams = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            urls = self._stream_urls(row)
            if not urls:
                continue
            streams.append({
                "urls": urls,
                "bandwidth": row.get("bandwidth") if type(row.get("bandwidth")) is int else 0,
                "codec": str(row.get("codecs") or "")[:32],
                "mime": str(row.get("mimeType") or row.get("mime_type") or "")[:64],
            })
        if not streams:
            raise _error("no_stream")
        streams.sort(key=lambda item: (item["bandwidth"] or 10 ** 9))
        best = streams[0]
        return {"bvid": bvid, "cid": cid, "kind": "dash_audio", "urls": best["urls"],
                "bandwidth": best["bandwidth"], "codec": best["codec"], "mime": best["mime"],
                "tiers": [{k: item[k] for k in ("bandwidth", "codec", "urls")}
                          for item in streams[:4]]}

    @staticmethod
    def _stream_urls(row: dict) -> list[str]:
        candidates = []
        for key in ("baseUrl", "base_url", "url"):
            value = row.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
        for key in ("backupUrl", "backup_url"):
            value = row.get(key)
            if isinstance(value, list):
                candidates.extend(u for u in value if isinstance(u, str) and u)
        safe = []
        for url in candidates:
            try:
                parts = urlsplit(url)
            except (TypeError, ValueError):
                continue
            if (parts.scheme == "https" and parts.hostname
                    and parts.hostname.endswith(_MEDIA_SUFFIXES)
                    and parts.port in (None, 443) and not parts.username
                    and not parts.password and not parts.fragment
                    and not any(ord(c) < 33 or ord(c) > 126 or c == "\\" for c in url)):
                safe.append(url)
        return safe

    async def videoshot(self, bvid: str, cid: int) -> dict:
        """Official storyboard snapshot (视频快照): real frames with real timestamps.

        Only used when no local decoder exists; resolution is thumbnail-sized and
        callers must label that provenance instead of implying full-video decoding.
        """
        bvid = _bvid(bvid)
        if type(cid) is not int or cid <= 0:
            raise ValueError("cid 必须是正整数")
        payload = None
        for path, params in (("/x/player/wbi/videoshot", await self._signed({"bvid": bvid, "cid": cid, "index": 1})),
                             ("/x/player/videoshot", {"bvid": bvid, "cid": cid, "index": 1})):
            try:
                payload = await self._api(path, params)
                break
            except BiliClientError as exc:
                if exc.reason not in ("api_error", "not_found", "authentication"):
                    raise
        if not isinstance(payload, dict):
            raise _error("api_error")
        images = payload.get("image")
        index = payload.get("index")
        if not isinstance(images, list) or not isinstance(index, list) or not images:
            raise _error("invalid_response")
        grid = {key: payload.get(key) for key in ("img_x_len", "img_x_size", "img_y_len", "img_y_size")}
        if not all(type(v) is int and v > 0 for v in grid.values()):
            raise _error("invalid_response")
        urls = [u if u.startswith("https://") else "https:" + u for u in images if isinstance(u, str)]
        if urls and urls[0].startswith("http://"):
            urls[0] = "https://" + urls[0][len("http://"):]
        times = [int(round(float(t) * 1000)) for t in index if isinstance(t, (int, float))]
        if len(urls) != len(times) or not urls:
            raise _error("invalid_response")
        return {"urls": urls, "times_ms": times, "grid": grid}

    async def download_media(self, url: str, dest, *, max_bytes: int, timeout: float | None = None) -> dict:
        """Stream one signed CDN URL into a caller-owned file.

        Never sends cookies, never follows redirects, rejects non-allowlisted hosts
        and private/loopback resolutions, and removes the partial file on any error.
        """
        dest = Path(dest)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes 必须是正整数")
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
        except (TypeError, ValueError):
            raise _error("unsafe_url") from None
        if (parts.scheme != "https" or not host.endswith(_MEDIA_SUFFIXES)
                or parts.port not in (None, 443) or parts.username or parts.password
                or parts.fragment
                or any(ord(c) < 33 or ord(c) > 126 or c == "\\" for c in url)):
            raise _error("unsafe_url")
        if self._closed:
            raise _error("closed")
        await self._assert_public(host)
        try:
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    cookie_jar=aiohttp.DummyCookieJar(), trust_env=False,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/",
                             "Origin": "https://www.bilibili.com"})
            digest, size = hashlib.sha256(), 0
            dest.parent.mkdir(parents=True, exist_ok=True)
            timeout = self.timeout if timeout is None else max(1.0, float(timeout))
            async with self._session.get(url, allow_redirects=False, cookies=None,
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if 300 <= response.status < 400:
                    raise _error("redirect")
                if response.status in (403, 412, 429):
                    raise _error("risk_control")
                if response.status != 200:
                    raise _error("http_error")
                declared = response.content_length
                if declared and declared > max_bytes:
                    raise _error("media_too_large")
                with dest.open("wb") as stream:
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise _error("media_too_large")
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
            if size <= 0:
                raise _error("invalid_response")
            return {"bytes": size, "sha256": digest.hexdigest()}
        except BaseException:
            # Partial media is never left behind, whatever ended the transfer.
            dest.unlink(missing_ok=True)
            raise

    async def _assert_public(self, host: str) -> None:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise _error("network") from None
        if not infos:
            raise _error("network")
        for info in infos:
            try:
                address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
            except (IndexError, ValueError):
                raise _error("network") from None
            if (address.is_private or address.is_loopback or address.is_link_local
                    or address.is_reserved or address.is_multicast or address.is_unspecified):
                raise _error("private_address")
