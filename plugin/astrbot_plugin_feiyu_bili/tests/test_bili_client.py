"""Offline contract/security tests. Run: python -m unittest discover -s tests -v.

Requires aiohttp (same dependency as the client); no live Bilibili requests.
Direct module loading deliberately avoids importing AstrBot/plugin initialization.
"""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

_SPEC = importlib.util.spec_from_file_location("feiyu_bili_client", Path(__file__).resolve().parents[1] / "bili_client.py")
module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(module)
BiliClient, BiliClientError = module.BiliClient, module.BiliClientError
BV = "BV1xx411c7mD"


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, payload=None, *, status=200, chunks=None, length=None):
        self.status = status
        self.content_length = length
        raw = json.dumps(payload).encode() if chunks is None else None
        self.content = Content(chunks if chunks is not None else [raw])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.get(url, **kwargs)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self):
        self.closed = True


def nav():
    return {"code": -101, "data": {"wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}}


def api(data):
    return Response({"code": 0, "data": data})


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, *responses, **kwargs):
        client = BiliClient(**kwargs)
        client._session = Session(responses)
        self.addAsyncCleanup(client.close)
        return client

    async def test_anonymous_search_signed_and_normalized(self):
        client = self.client(Response(nav()), api({"result": [
            {"bvid": BV, "title": '<em class="keyword">猫</em>&amp;狗',
             "author": "UP", "description": "<b>说明</b>", "arcurl": "https://evil.invalid"},
            {"bvid": "invalid", "title": "bad"}]}))
        rows = await client.search("猫", limit=5)
        self.assertEqual(rows, [{"bvid": BV, "title": "猫&狗", "author": "UP", "description": "说明",
                                 "url": "https://www.bilibili.com/video/" + BV}])
        calls = client._session.calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], "https://api.bilibili.com/x/web-interface/wbi/search/type")
        self.assertIn("w_rid", calls[1][1]["params"])
        self.assertEqual(calls[1][1]["cookies"], {})
        self.assertTrue(all(call[1]["allow_redirects"] is False for call in calls))

    async def test_search_pagination_defaults_and_validation(self):
        client = self.client(Response(nav()), api({}), api({}))
        await client.search("猫", 5)
        self.assertEqual(client._session.calls[-1][1]["params"]["order"], "pubdate")
        await client.search("猫", page=2, order="totalrank")
        params = client._session.calls[-1][1]["params"]
        self.assertEqual((params["page"], params["order"]), ("2", "totalrank"))
        for kwargs in [{"page": 0}, {"page": True}, {"page": 1001}, {"order": "bad"}, {"order": []}]:
            with self.assertRaises(ValueError):
                await client.search("猫", **kwargs)
        self.assertEqual(len(client._session.calls), 3)

    async def test_feed_and_related_normalize_list_data(self):
        item = {"bvid": BV, "title": "<b>猫</b>&amp;狗", "owner": {"name": "UP"}, "desc": "说明"}
        client = self.client(Response(nav()), api({"item": [None, item, item]}), api([item, item]))
        expected = [{"bvid": BV, "title": "猫&狗", "author": "UP", "description": "说明",
                     "url": "https://www.bilibili.com/video/" + BV}]
        self.assertEqual(await client.recommendations(), expected)
        feed = client._session.calls[-1]
        self.assertEqual(feed[0], module._API + "/x/web-interface/wbi/index/top/feed/rcmd")
        self.assertIn("w_rid", feed[1]["params"])
        self.assertEqual(await client.related(BV), expected)
        self.assertEqual(client._session.calls[-1][1]["params"], {"bvid": BV})
        for method in [client.recommendations, lambda limit: client.related(BV, limit)]:
            for limit in [0, 51, True]:
                with self.assertRaises(ValueError):
                    await method(limit)

    async def test_related_rejects_nonlist_or_bad_code(self):
        for payload, reason in [({"code": 0, "data": {}}, "invalid_response"),
                                ({"code": -101, "data": []}, "authentication")]:
            client = self.client(Response(payload))
            with self.assertRaises(BiliClientError) as caught:
                await client.related(BV)
            self.assertEqual(caught.exception.reason, reason)

    async def test_like_missing_credentials_fails_closed(self):
        for cookies in [{}, {"SESSDATA": "secret"}, {"bili_jct": "csrf-secret"}]:
            client = self.client()
            client._cookies = cookies
            with self.assertRaises(BiliClientError) as caught:
                await client.like(BV)
            self.assertEqual(caught.exception.reason, "authentication")
            self.assertEqual(client._session.calls, [])

    async def test_like_deduplicates_and_has_liked_scalar_strict(self):
        client = self.client(api(1))
        client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
        self.assertEqual(await client.like(BV), {"status": "already_liked"})
        self.assertEqual(client._session.posts, [])
        self.assertEqual(client._session.calls[0][0], module._API + "/x/web-interface/archive/has/like")
        for data in [True, None, {}, "1", 2]:
            client = self.client(api(data))
            with self.assertRaises(BiliClientError) as caught:
                await client.has_liked(BV)
            self.assertEqual(caught.exception.reason, "invalid_response")

    async def test_like_unique_post_body_cookies_and_no_redirect(self):
        client = self.client(api(0), api(None), api(1))
        client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
        self.assertEqual(await client.like(BV), {"status": "liked"})
        self.assertEqual(await client.like(BV), {"status": "already_liked"})
        self.assertEqual(len(client._session.posts), 1)
        url, kwargs = client._session.posts[0]
        self.assertEqual(url, "https://api.bilibili.com/x/web-interface/archive/like")
        self.assertEqual(kwargs, {"data": {"bvid": BV, "like": 1, "csrf": "csrf-secret"},
                                  "allow_redirects": False, "cookies": client._cookies})
        self.assertNotIn("csrf-secret", url)
        self.assertNotIn("params", kwargs)

    async def test_post_session_safe_headers_and_cancellation(self):
        client = BiliClient(timeout=3)
        client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
        self.addAsyncCleanup(client.close)
        fake = Session([api(0), api(None)])
        with patch.object(module.aiohttp, "ClientSession", return_value=fake) as factory:
            await client.like(BV)
        config = factory.call_args.kwargs
        self.assertEqual(config["headers"], {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"})
        self.assertFalse(config["trust_env"])
        self.assertIsInstance(config["cookie_jar"], module.aiohttp.DummyCookieJar)
        self.assertEqual(config["timeout"].total, 3)
        client = self.client(api(0))
        client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
        with patch.object(client._session, "post", side_effect=asyncio.CancelledError()) as post:
            with self.assertRaises(asyncio.CancelledError):
                await client.like(BV)
        self.assertEqual(post.call_count, 1)

    async def test_post_restricted_even_at_transport_boundary(self):
        client = self.client()
        client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
        for url, kwargs in [(module._API + "/x/web-interface/archive/coin/add", {}),
                            (module._API + "/x/web-interface/archive/like?csrf=secret", {}),
                            (module._API + "/x/web-interface/archive/like", {"params": {"like": 2}}),
                            ("https://aisubtitle.hdslb.com/x", {"subtitle": True})]:
            with self.assertRaises(BiliClientError):
                await client._json(url, _like_bvid=BV, **kwargs)
        self.assertEqual(client._session.calls, [])

    async def test_post_errors_safe_no_retry(self):
        cases = [(Response(status=302), "redirect"),
                 (Response({"code": -111, "message": "csrf-secret"}), "authentication"),
                 (Response({"code": -999, "message": "csrf-secret"}), "api_error"),
                 (Response({"code": False}), "invalid_response"),
                 (Response({}, length=3000000), "response_too_large"),
                 (Response(chunks=[b"not json"]), "invalid_response"),
                 (module.aiohttp.ClientError("csrf-secret"), "network"),
                 (asyncio.TimeoutError("csrf-secret"), "timeout")]
        for response, reason in cases:
            client = self.client(api(0), response)
            client._cookies = {"SESSDATA": "secret", "bili_jct": "csrf-secret"}
            with self.assertRaises(BiliClientError) as caught:
                await client.like(BV)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(len(client._session.posts), 1)
            self.assertEqual(len(client._session.calls), 2)
            self.assertFalse(client._session.posts[0][1]["allow_redirects"])

    async def test_wbi_known_vector_sanitization_and_cache(self):
        client = self.client(Response(nav()))
        with patch.object(module.time, "time", return_value=1702204169):
            signed = await client._signed({"foo": "114", "bar": "514", "baz": "1919810"})
            self.assertEqual(client._wbi_key, "ea1db124af3c7062474693fa704f4ff8")
            self.assertEqual(signed["w_rid"], "6149fdadf571698ca7e6a567265cd0ee")
            cleaned = await client._signed({"keyword": "a!'()*猫"})
        self.assertEqual(cleaned["keyword"], "a猫")
        self.assertEqual(len(client._session.calls), 1)
        expected = dict(cleaned)
        digest = expected.pop("w_rid")
        self.assertEqual(digest, hashlib.md5((urlencode(expected) + client._wbi_key).encode()).hexdigest())

    async def test_detail_first_cid_fallback(self):
        client = self.client(api({"title": "title", "desc": "description", "owner": {"name": "UP"},
                                  "pages": [{"cid": 123}, {"cid": 456}]}))
        info = await client.detail(BV)
        self.assertEqual(info["cid"], 123)
        self.assertEqual(info["pages"], 2)
        self.assertEqual(info["author"], "UP")
        self.assertEqual(client._session.calls[0][1]["params"], {"bvid": BV})

    async def test_subtitle_chinese_preference_first_part_no_cookie(self):
        client = self.client(api({"cid": 123}), Response(nav()), api({"subtitle": {"subtitles": [
            {"lan": "en", "subtitle_url": "https://evil.invalid/sub.json"},
            {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/sub.json?token=private"} ]}}),
            Response({"body": [{"content": "你好"}, {"content": "世界"}]}))
        client._cookies = {"SESSDATA": "private-secret"}
        result = await client.subtitle(BV)
        self.assertEqual(result, {"text": "你好\n世界", "source": "bilibili_subtitle:first_part", "reason": "ok"})
        self.assertIsNone(client._session.calls[-1][1]["cookies"])
        self.assertNotIn("private", str(result))
        self.assertTrue(all(c[0].startswith("https://") for c in client._session.calls))

    async def test_subtitle_no_cid(self):
        client = self.client(api({}))
        self.assertEqual((await client.subtitle(BV))["reason"], "missing_cid")
        self.assertEqual(len(client._session.calls), 1)

    async def test_subtitle_absence_is_not_claimed_watched(self):
        for player, reason in [({}, "no_subtitle_or_login_required"),
                               ({"need_login_subtitle": True}, "login_required")]:
            with self.subTest(reason=reason):
                client = self.client(api({"cid": 1}), Response(nav()), api(player))
                self.assertEqual(await client.subtitle(BV), {"text": "", "source": "none", "reason": reason})

    async def test_subtitle_empty_and_truncation(self):
        for body, expected in [([{"content": " "}], ("", "empty_subtitle")),
                                ([{"content": "123"}, {"content": "456"}], ("123\n4", "truncated"))]:
            client = self.client(api({"cid": 1}), Response(nav()),
                                 api({"subtitle": {"subtitles": [{"subtitle_url": "https://aisubtitle.hdslb.com/a"}]}}),
                                 Response({"body": body}), max_subtitle_chars=5)
            result = await client.subtitle(BV)
            self.assertEqual((result["text"], result["reason"]), expected)
            if not result["text"]:
                self.assertEqual(result["source"], "none")

    async def test_subtitle_bad_urls_fail_before_request(self):
        urls = ["http://aisubtitle.hdslb.com/a", "https://aisubtitle.hdslb.com.evil.com/a",
                "https://evil.com/a", "https://127.0.0.1/a", "https://api.bilibili.com/a",
                "https://u:p@aisubtitle.hdslb.com/a", "https://aisubtitle.hdslb.com:8443/a",
                "file:///etc/passwd", "//evil.com/a", "https://aisubtitle.hdslb.com/a#fragment",
                "https://aisubtitle.hdslb.com/\nfoo", "https://aisubtitle.hdslb.com\\@evil.com/a"]
        for url in urls:
            with self.subTest(url=url):
                client = self.client(api({"cid": 1}), Response(nav()),
                                     api({"subtitle": {"subtitles": [{"subtitle_url": url}]}}))
                result = await client.subtitle(BV)
                self.assertEqual(result, {"text": "", "source": "none", "reason": "unsafe_url"})
                self.assertEqual(len(client._session.calls), 3)

    async def test_transport_redirect_never_followed(self):
        client = self.client(Response(status=302))
        with self.assertRaises(BiliClientError) as caught:
            await client._json("https://aisubtitle.hdslb.com/a", subtitle=True)
        self.assertEqual(caught.exception.reason, "redirect")
        self.assertEqual(len(client._session.calls), 1)
        self.assertFalse(client._session.calls[0][1]["allow_redirects"])

    async def test_api_and_http_risk_control(self):
        for response in [Response(status=n) for n in (403, 412, 429)] + [
                Response({"code": n, "message": "secret-cookie"}) for n in (-412, -352, -509)]:
            client = self.client(response)
            with self.assertRaises(BiliClientError) as caught:
                await client.detail(BV)
            self.assertEqual(caught.exception.reason, "risk_control")
            self.assertNotIn("secret", str(caught.exception))

    async def test_auth_notfound_other_api(self):
        for code, reason in [(-101, "authentication"), (-404, "not_found"), (-999, "api_error")]:
            client = self.client(Response({"code": code, "message": "secret"}))
            with self.assertRaises(BiliClientError) as caught:
                await client.detail(BV)
            self.assertEqual(caught.exception.reason, reason)

    async def test_subtitle_risk_is_not_no_subtitle(self):
        client = self.client(Response(status=412))
        self.assertEqual(await client.subtitle(BV), {"text": "", "source": "none", "reason": "risk_control"})

    async def test_streaming_and_declared_size_limits(self):
        for response in [Response({}, length=2048), Response(chunks=[b"x" * 800, b"x" * 800])]:
            client = self.client(response, max_response_bytes=1024)
            with self.assertRaises(BiliClientError) as caught:
                await client.detail(BV)
            self.assertEqual(caught.exception.reason, "response_too_large")

    async def test_malformed_response(self):
        for response in [Response(chunks=[b"not json"]), Response([]), Response({}),
                         Response({"code": 0, "data": []}), Response({"code": False, "data": {}}),
                         api({"owner": [] , "pages": "bad"})]:
            client = self.client(response)
            with self.assertRaises(BiliClientError) as caught:
                await client.detail(BV)
            self.assertEqual(caught.exception.reason, "invalid_response")

    async def test_invalid_subtitle_data(self):
        for data in [{"body": {}}, {"body": [{"content": 12}]}]:
            client = self.client(api({"cid": 1}), Response(nav()),
                                 api({"subtitle": {"subtitles": [{"subtitle_url": "https://aisubtitle.hdslb.com/a"}]}}),
                                 Response(data))
            self.assertEqual((await client.subtitle(BV))["reason"], "invalid_response")

    async def test_transport_errors_are_sanitized(self):
        for exc, reason in [(module.aiohttp.ClientError("SESSDATA=secret"), "network"),
                            (asyncio.TimeoutError("SESSDATA=secret"), "timeout")]:
            client = self.client(exc)
            with self.assertRaises(BiliClientError) as caught:
                await client.detail(BV)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("secret", str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_cancel_is_not_swallowed(self):
        client = self.client()
        client._api = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await client.subtitle(BV)

    async def test_close_and_context(self):
        client = self.client()
        session = client._session
        async with client as same:
            self.assertIs(client, same)
        self.assertTrue(session.closed)
        await client.close()
        with self.assertRaises(BiliClientError):
            await client.detail(BV)

    async def test_session_configuration(self):
        client = BiliClient(timeout=3)
        self.addAsyncCleanup(client.close)
        fake = Session([api({})])
        with patch.object(module.aiohttp, "ClientSession", return_value=fake) as factory:
            await client.detail(BV)
        config = factory.call_args.kwargs
        self.assertFalse(config["trust_env"])
        self.assertIsInstance(config["cookie_jar"], module.aiohttp.DummyCookieJar)
        self.assertEqual(config["timeout"].total, 3)

    async def test_input_validation_no_network(self):
        client = self.client()
        for value in ["", "https://b23.tv/example", "BVbad", "BV1xx411c7mD\n"]:
            with self.assertRaises(ValueError):
                await client.detail(value)
        for keyword, limit in [("", 5), ("x" * 201, 5), ("ok", 0), ("ok", 51), ("ok", True)]:
            with self.assertRaises(ValueError):
                await client.search(keyword, limit)
        self.assertEqual(client._session.calls, [])

    async def test_api_url_whitelist_applies_without_cdn_mode(self):
        client = self.client()
        for url in ["https://evil.com/x", "https://aisubtitle.hdslb.com/a", "http://api.bilibili.com/x"]:
            with self.assertRaises(BiliClientError) as caught:
                await client._json(url)
            self.assertEqual(caught.exception.reason, "unsafe_url")
        self.assertEqual(client._session.calls, [])


class CredentialsTests(unittest.TestCase):
    def load(self, data):
        # Keep synthetic credentials in memory: no filesystem writes at all.
        with patch.object(module.Path, "open", return_value=BytesIO(data.encode("utf-8"))) as opened:
            result = BiliClient._load_credentials("explicit-credential.json")
        opened.assert_called_once_with("rb")
        return result

    def test_absence_does_not_scan_environment_or_working_directory(self):
        with patch.object(module.Path, "open", side_effect=AssertionError("must not open")):
            self.assertEqual(BiliClient()._cookies, {})

    def test_upstream_flat_format_and_case_aliases(self):
        cookies = self.load(json.dumps({"sessdata": "secret%2Cvalue", "bili_jct": "csrf", "buvid3": "device",
                                        "dedeuserid": "123", "refresh_token": "not-used", "extra": {"ignored": True}}))
        self.assertEqual(cookies, {"SESSDATA": "secret%2Cvalue", "bili_jct": "csrf", "buvid3": "device", "DedeUserID": "123"})
        self.assertEqual(self.load('{"SESSDATA":"s","DedeUserID":123}'), {"SESSDATA": "s", "DedeUserID": "123"})

    def test_bad_credentials_never_echo_content_or_path(self):
        for raw in ['{"sessdata":"secret"', '[]', '{"sessdata":"secret;injected=1"}',
                    '{"sessdata":"secret\\r\\nCookie:x"}', '{"sessdata":{"secret":1}}',
                    'x' * 17000]:
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(BiliClientError) as caught:
                    self.load(raw)
                self.assertEqual(caught.exception.reason, "credentials")
                self.assertNotIn("secret", str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
        with self.assertRaises(BiliClientError):
            BiliClient("missing-file-secret.json")

    def test_constructor_bounds(self):
        for kwargs in [{"timeout": 0}, {"timeout": 61}, {"max_response_bytes": 10},
                       {"max_subtitle_chars": 0}, {"max_subtitle_chars": 200001}]:
            with self.assertRaises(ValueError):
                BiliClient(**kwargs)


if __name__ == "__main__":
    unittest.main()
