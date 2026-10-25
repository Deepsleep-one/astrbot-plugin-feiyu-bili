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
is only the first video's part and is NOT evidence that video frames were seen.
Instances must be used on one asyncio loop, and closed after use.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp

__all__ = ["BiliClient", "BiliClientError"]
_API = "https://api.bilibili.com"
_SUBTITLE_HOSTS = frozenset({"aisubtitle.hdslb.com", "subtitle.bilibili.com", "i0.hdslb.com", "i1.hdslb.com", "i2.hdslb.com"})
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
    }
    return BiliClientError(reason, messages[reason])


def _text(value: object, limit: int = 10000) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))[:limit]


def _bvid(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        raise ValueError("bvid 必须是 BV 开头的 12 位视频编号")
    return value


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
        """Return normalized public metadata, NOT proof of watching the video."""
        bvid = _bvid(bvid)
        data = await self._api("/x/web-interface/view", {"bvid": bvid})
        owner = data.get("owner") or {}
        pages = data.get("pages") or []
        if not isinstance(owner, dict) or not isinstance(pages, list):
            raise _error("invalid_response")
        cid = data.get("cid")
        if not cid and pages and isinstance(pages[0], dict):
            cid = pages[0].get("cid")
        return {"bvid": bvid, "title": _text(data.get("title")),
                "author": _text(owner.get("name"), 1000),
                "description": _text(data.get("desc")),
                "url": f"https://www.bilibili.com/video/{bvid}",
                "cid": cid if type(cid) is int and cid > 0 else None,
                "duration": data.get("duration"), "pubdate": data.get("pubdate"),
                "pages": len(pages)}

    async def subtitle(self, bvid: str) -> dict:
        """Read first-part subtitle only. Empty text NEVER implies video was watched.

        source is a safe provenance label, never a possibly token-bearing CDN URL.
        Operational failures are reported in reason rather than mistaken for no subtitle.
        """
        bvid = _bvid(bvid)
        try:
            info = await self.detail(bvid)
            if not info["cid"]:
                return {"text": "", "source": "none", "reason": "missing_cid"}
            data = await self._api("/x/player/wbi/v2", await self._signed({"bvid": bvid, "cid": info["cid"]}))
            subtitle = data.get("subtitle") or {}
            if not isinstance(subtitle, dict):
                raise _error("invalid_response")
            tracks = subtitle.get("subtitles") or []
            if not isinstance(tracks, list) or any(not isinstance(t, dict) for t in tracks):
                raise _error("invalid_response")
            if not tracks:
                reason = "login_required" if data.get("need_login_subtitle") or subtitle.get("need_login_subtitle") else "no_subtitle_or_login_required"
                return {"text": "", "source": "none", "reason": reason}
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
            fragments, count, truncated = [], 0, False
            for row in body:
                if not isinstance(row, dict) or not isinstance(row.get("content"), str):
                    raise _error("invalid_response")
                content = row["content"].strip()
                if not content:
                    continue
                remaining = self.max_subtitle_chars - count
                part = ("\n" if fragments else "") + content
                fragments.append(part[:remaining])
                count += min(len(part), remaining)
                if len(part) > remaining:
                    truncated = True
                    break
            text = "".join(fragments)
            return {"text": text, "source": "bilibili_subtitle:first_part" if text else "none",
                    "reason": "truncated" if truncated else "ok" if text else "empty_subtitle"}
        except BiliClientError as exc:
            return {"text": "", "source": "none", "reason": exc.reason}
