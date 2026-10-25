"""Offline tests only: never initiate real login or write real credentials."""
from contextlib import redirect_stdout
from email.message import Message
from io import StringIO
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

_SPEC = importlib.util.spec_from_file_location("bili_login_helper", Path(__file__).resolve().parents[1] / "tools" / "bili_login.py")
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)
KEY = "a" * 32
URL = "https://passport.bilibili.com/h5-app/passport/login/scan?navhide=1&qrcode_key=" + KEY
CALLBACK = "https://passport.biligame.com/crossDomain?DedeUserID=123&SESSDATA=secret%2Cvalue&bili_jct=csrf&refresh_token=NEVER_STORE"


class LoginTests(unittest.TestCase):
    def response(self, data=None, *, raw=None, status=200, headers=None):
        response = MagicMock()
        response.status = status
        response.headers = headers or {}
        response.read.return_value = raw if raw is not None else json.dumps({"code": 0, "data": data}).encode()
        response.__enter__.return_value = response
        return response

    def cookie_headers(self):
        headers = Message()
        for cookie in ["SESSDATA=header-secret%2Cvalue; Domain=.bilibili.com; Path=/; Secure; HttpOnly",
                       "bili_jct=header-csrf; Path=/; Secure", "DedeUserID=123; Path=/",
                       "buvid3=device; Path=/", "refresh_token=NEVER_STORE; Path=/"]:
            headers.add_header("Set-Cookie", cookie)
        return headers

    def test_success_uses_real_response_cookie_headers_without_url(self):
        response = self.response({"code": 0, "url": "", "cookies": {"sessdata": "BODY_FAKE"}}, headers=self.cookie_headers())
        with patch.object(m, "build_opener") as factory, patch.object(m, "_save") as save:
            factory.return_value.open.return_value = response
            self.assertEqual(m.poll(KEY), {"status": "success"})
        save.assert_called_once_with({"sessdata": "header-secret%2Cvalue", "bili_jct": "header-csrf",
                                      "dedeuserid": "123", "buvid3": "device"})

    def test_response_cookie_priority_over_callback(self):
        response = self.response({"code": 0, "url": CALLBACK}, headers=self.cookie_headers())
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.return_value = response
            data = m._request("poll", KEY)
        self.assertEqual(m._credentials(data)["sessdata"], "header-secret%2Cvalue")
        self.assertNotIn("header-secret", repr(data))

    def test_body_cannot_forge_transport_cookies(self):
        fake = {"code": 0, "url": "", "cookies": {"sessdata": "BODY_FAKE"},
                "response_credentials": {"sessdata": "s", "bili_jct": "j", "dedeuserid": "1"}}
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.return_value = self.response(fake)
            data = m._request("poll", KEY)
        with self.assertRaisesRegex(m.LoginError, "^invalid_credentials_response$"):
            m._credentials(data)

    def test_waiting_response_does_not_read_cookie_headers(self):
        response = self.response({"code": 86101}, headers=self.cookie_headers())
        with patch.object(m, "build_opener") as factory, patch.object(m, "_response_cookies") as cookies:
            factory.return_value.open.return_value = response
            self.assertEqual(m.poll(KEY), {"status": "waiting_scan"})
        cookies.assert_not_called()

    def test_invalid_and_conflicting_cookie_values(self):
        for lines in [["SESSDATA=s; Path=/", "SESSDATA=other; Path=/"],
                      ['SESSDATA="bad;value"; Path=/']]:
            headers = Message()
            for line in lines:
                headers.add_header("Set-Cookie", line)
            self.assertEqual(m._response_cookies(headers), {})

    def test_new_official_callback_path_is_parsed_only(self):
        callback = CALLBACK.replace("passport.biligame.com/crossDomain", "account.bilibili.com/h5/account-h5/auth/callback")
        with patch.object(m, "build_opener") as opener:
            self.assertEqual(m._credentials({"url": callback})["dedeuserid"], "123")
        opener.assert_not_called()

    def test_diagnosis_has_no_query_cookie_values(self):
        data = m._PollData({"url": CALLBACK}, m._response_cookies(self.cookie_headers()))
        result = m.diagnose(data)
        self.assertEqual(result["scheme"], "https")
        self.assertEqual(result["host"], "passport.biligame.com")
        self.assertEqual(result["path"], "/crossDomain")
        self.assertIn("SESSDATA", result["query_keys"])
        self.assertTrue(all(result["cookie_required_present"].values()))
        self.assertTrue(all(result["query_required_present"].values()))
        text = json.dumps(result)
        for forbidden in ("secret", "csrf", "123", "NEVER_STORE", "device", CALLBACK):
            self.assertNotIn(forbidden, text)
        self.assertFalse(any(m.diagnose({"url": ""})["cookie_required_present"].values()))

    def test_generate_only_official_url_and_key(self):
        with patch.object(m, "_request", return_value={"url": URL, "qrcode_key": KEY}) as request:
            self.assertEqual(m.generate(), {"url": URL, "qrcode_key": KEY})
        request.assert_called_once_with("generate")

    def test_generate_new_account_scan_url(self):
        url = "https://account.bilibili.com/h5/account-h5/auth/scan-web?navhide=1&callback=&qrcode_key=" + KEY
        with patch.object(m, "_request", return_value={"url": url, "qrcode_key": KEY}):
            self.assertEqual(m.generate(), {"url": url, "qrcode_key": KEY})

    def test_generate_host_and_path_pairs_cannot_be_crossed(self):
        urls = [
            "https://account.bilibili.com/h5-app/passport/login/scan?qrcode_key=" + KEY,
            "https://passport.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY,
            "http://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY,
            "https://account.bilibili.com.evil.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY,
            "https://u:p@account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY,
            "https://account.bilibili.com:8443/h5/account-h5/auth/scan-web?qrcode_key=" + KEY,
            "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY + "#bad",
            "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + KEY + "&qrcode_key=duplicate",
        ]
        for url in urls:
            with self.subTest(url=url), patch.object(m, "_request", return_value={"url": url, "qrcode_key": KEY}):
                with self.assertRaises(m.LoginError):
                    m.generate()

    def test_generate_blocks_nonofficial_urls(self):
        for url in ["https://evil.com/scan", URL.replace("https:", "http:"),
                    URL.replace("passport.bilibili.com", "passport.bilibili.com.evil.com"),
                    URL.replace("passport.bilibili.com", "u:p@passport.bilibili.com"),
                    URL + "#bad", URL + "&qrcode_key=duplicate", URL.replace(KEY, "b" * 32)]:
            with self.subTest(url=url), patch.object(m, "_request", return_value={"url": url, "qrcode_key": KEY}):
                with self.assertRaises(m.LoginError):
                    m.generate()

    def test_transport_fixed_host_get_no_proxy_timeout_size(self):
        response = self.response({"url": URL, "qrcode_key": KEY})
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.return_value = response
            self.assertEqual(m._request("generate")["url"], URL)
        handlers = factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], m._NoRedirect)
        request = factory.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://passport.bilibili.com/x/passport-login/web/qrcode/generate")
        self.assertEqual(request.method, "GET")
        self.assertEqual(factory.return_value.open.call_args.kwargs["timeout"], 15)
        response.read.assert_called_once_with(65537)

    def test_poll_key_validation_before_network(self):
        with patch.object(m, "build_opener") as opener:
            for key in ["", "short", KEY + "\n", "https://evil.com/" + KEY, ";" * 32]:
                with self.assertRaises(m.LoginError):
                    m._request("poll", key)
        opener.assert_not_called()

    def test_poll_url(self):
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.return_value = self.response({"code": 86101})
            m._request("poll", KEY)
        self.assertEqual(factory.return_value.open.call_args.args[0].full_url,
                         "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key=" + KEY)

    def test_redirect_handler_does_not_follow(self):
        self.assertIsNone(m._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.com"))
        error = HTTPError("https://secret", 302, "secret", {}, None)
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaisesRegex(m.LoginError, "^redirect_blocked$"):
                m._request("generate")

    def test_size_status_json_and_api_failures_are_safe(self):
        cases = [(self.response({}, headers={"Content-Length": "999999"}), "response_too_large"),
                 (self.response({}, raw=b"x" * 65537), "response_too_large"),
                 (self.response({}, status=403), "http_error"),
                 (self.response({}, raw=b"secret"), "invalid_response"),
                 (self.response({}, raw=b'{"code":-1,"message":"secret"}'), "api_error"),
                 (self.response({}, raw=b'{"code":0,"data":[]}'), "invalid_response")]
        for response, reason in cases:
            with self.subTest(reason=reason), patch.object(m, "build_opener") as factory:
                factory.return_value.open.return_value = response
                with self.assertRaises(m.LoginError) as caught:
                    m._request("generate")
                self.assertEqual(str(caught.exception), reason)
                self.assertNotIn("secret", str(caught.exception))

    def test_transport_exception_does_not_expose_url_or_cookie(self):
        with patch.object(m, "build_opener") as factory:
            factory.return_value.open.side_effect = URLError("SESSDATA=secret")
            with self.assertRaisesRegex(m.LoginError, "^network_error$") as caught:
                m._request("generate")
        self.assertTrue(caught.exception.__suppress_context__)

    def test_waiting_states_do_not_save_or_sleep(self):
        for code, status in [(86101, "waiting_scan"), (86090, "waiting_confirmation"), (86038, "expired")]:
            with patch.object(m, "_request", return_value={"code": code}), patch.object(m, "_save") as save:
                self.assertEqual(m.poll(KEY), {"status": status})
            save.assert_not_called()

    def test_success_allowlist_compatible_with_client(self):
        expected = {"sessdata": "secret%2Cvalue", "bili_jct": "csrf", "dedeuserid": "123"}
        with patch.object(m, "_request", return_value={"code": 0, "url": CALLBACK,
                                                     "refresh_token": "NEVER_STORE"}), patch.object(m, "_save") as save:
            self.assertEqual(m.poll(KEY), {"status": "success"})
        save.assert_called_once_with(expected)
        self.assertNotIn("refresh_token", save.call_args.args[0])

    def test_credentials_url_is_parsed_not_fetched(self):
        with patch.object(m, "build_opener") as opener:
            m._credentials({"url": CALLBACK})
        opener.assert_not_called()

    def test_bad_callback_or_missing_credentials(self):
        for callback in [CALLBACK.replace("passport.biligame.com", "evil.com"),
                         CALLBACK.replace("https:", "http:"), CALLBACK + "&SESSDATA=duplicate",
                         CALLBACK.replace("bili_jct=csrf", "bili_jct="),
                         CALLBACK.replace("secret%2Cvalue", "secret%0ACookie:bad")]:
            with self.subTest(callback=callback), self.assertRaisesRegex(m.LoginError, "^invalid_credentials_response$"):
                m._credentials({"url": callback})

    def test_unknown_status_and_invalid_code(self):
        for code in [True, "0", -123]:
            with patch.object(m, "_request", return_value={"code": code}), patch.object(m, "_save") as save:
                with self.assertRaises(m.LoginError):
                    m.poll(KEY)
            save.assert_not_called()

    def test_atomic_private_write_order_no_real_files(self):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.fileno.return_value = 99
        with patch.object(m.Path, "mkdir") as mkdir, patch.object(m.tempfile, "mkstemp", return_value=(99, "/cloud/.private.tmp")) as mktemp, \
             patch.object(m.os, "chmod") as chmod, patch.object(m.os, "fdopen", return_value=stream), \
             patch.object(m.os, "fsync") as fsync, patch.object(m.os, "replace") as replace, \
             patch.object(m.os, "unlink") as unlink, patch.object(m.json, "dump") as dump:
            m._save({"sessdata": "synthetic"})
        mkdir.assert_called_once_with(parents=True, exist_ok=True, mode=0o700)
        self.assertEqual(mktemp.call_args.kwargs["dir"], m.DESTINATION.parent)
        chmod.assert_called_once_with("/cloud/.private.tmp", 0o600)
        dump.assert_called_once()
        stream.flush.assert_called_once()
        fsync.assert_called_once_with(99)
        replace.assert_called_once_with("/cloud/.private.tmp", m.DESTINATION)
        unlink.assert_not_called()

    def test_save_failure_cleans_temp_and_does_not_print_credentials(self):
        with patch.object(m.Path, "mkdir"), patch.object(m.tempfile, "mkstemp", return_value=(99, "/cloud/.private.tmp")), \
             patch.object(m.os, "chmod", side_effect=OSError("secret")), patch.object(m.os, "close") as close, \
             patch.object(m.os, "unlink") as unlink, patch.object(m.os, "replace") as replace:
            with self.assertRaisesRegex(m.LoginError, "^credential_save_failed$"):
                m._save({"sessdata": "secret"})
        close.assert_called_once_with(99)
        unlink.assert_called_once_with("/cloud/.private.tmp")
        replace.assert_not_called()

    def test_cli_success_only_status(self):
        out = StringIO()
        with patch.object(m, "_request", return_value={"code": 0, "url": CALLBACK}), \
             patch.object(m, "_save"), redirect_stdout(out):
            code = m.main(["--poll", KEY])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {"status": "success"})
        self.assertNotIn("secret", out.getvalue())
        self.assertNotIn(KEY, out.getvalue())

    def test_cli_unexpected_exception_no_traceback_or_secret(self):
        out = StringIO()
        with patch.object(m, "generate", side_effect=RuntimeError("SESSDATA=secret")), redirect_stdout(out):
            code = m.main(["--generate"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue()), {"status": "internal_error"})


if __name__ == "__main__":
    unittest.main()
