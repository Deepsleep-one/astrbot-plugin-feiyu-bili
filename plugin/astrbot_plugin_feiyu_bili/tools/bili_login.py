"""Manual, cloud-side Bilibili official QR login helper (Python stdlib only).

Run --generate once, show its official URL as a local QR image, then --poll KEY
only when instructed by the user. This module never automatically polls/sleeps.
Success writes only allowlisted credentials to DESTINATION with atomic replace
and mode 0600. stdout contains only the QR URL/temporary key or a safe status.
No third-party QR service, no redirects, no credential logging. Do not import
this helper into the read-only client: login is an explicit separate workflow.
"""
from __future__ import annotations

import argparse
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

DESTINATION = Path("/AstrBot/data/plugin_data/astrbot_plugin_feiyu_bili/bili_credential.json")
_BASE = "https://passport.bilibili.com/x/passport-login/web/qrcode/"
_MAX_BYTES = 65536
_TIMEOUT = 15


class LoginError(RuntimeError):
    """Fixed status only: no server messages, headers or raw exception details."""


_ALLOWED_COOKIES = frozenset({"sessdata", "bili_jct", "buvid3", "dedeuserid"})
_REQUIRED = ("sessdata", "bili_jct", "dedeuserid")


class _PollData(dict):
    """Transport-only metadata: JSON fields cannot forge this attribute."""
    def __init__(self, data, credentials):
        super().__init__(data)
        self.response_credentials = credentials


def _cookie_value(name, value):
    if name == "sessdata":
        value = value.replace(",", "%2C")
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 or c in ';,"\\\\' for c in value):
        raise ValueError()
    return value


def _response_cookies(headers):
    """Read only actual response headers, never body-supplied cookie lookalikes."""
    result = {}
    try:
        lines = headers.get_all("Set-Cookie", [])
        if len(lines) > 30 or sum(len(line) for line in lines) > _MAX_BYTES:
            raise ValueError()
        for line in lines:
            jar = SimpleCookie()
            jar.load(line)
            for name, morsel in jar.items():
                canonical = name.lower()
                if canonical not in _ALLOWED_COOKIES:
                    continue
                value = _cookie_value(canonical, morsel.value)
                if canonical in result and result[canonical] != value:
                    raise ValueError()
                result[canonical] = value
        return result
    except (CookieError, ValueError, TypeError, AttributeError):
        # Older API variants may still supply a valid callback URL fallback.
        return {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(operation: str, key: str | None = None) -> dict:
    if operation not in ("generate", "poll"):
        raise LoginError("invalid_operation")
    url = _BASE + operation
    if operation == "poll":
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
            raise LoginError("invalid_key")
        url += "?" + urlencode({"qrcode_key": key})
    # No environment proxy and no cookie jar; host/path are compile-time fixed.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}, method="GET")
    try:
        with opener.open(request, timeout=_TIMEOUT) as response:
            if response.status != 200:
                raise LoginError("http_error")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > _MAX_BYTES):
                raise LoginError("response_too_large")
            raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise LoginError("response_too_large")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                raise LoginError("invalid_response")
            if payload["code"] != 0:
                raise LoginError("api_error")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise LoginError("invalid_response")
            if operation == "poll" and type(data.get("code")) is int and data["code"] == 0:
                return _PollData(data, _response_cookies(response.headers))
            return data
    except LoginError:
        raise
    except HTTPError as exc:
        status = "redirect_blocked" if 300 <= exc.code < 400 else "http_error"
        raise LoginError(status) from None
    except (TimeoutError, URLError, OSError):
        raise LoginError("network_error") from None
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise LoginError("invalid_response") from None


def generate() -> dict:
    data = _request("generate")
    key, url = data.get("qrcode_key"), data.get("url")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", key):
        raise LoginError("invalid_response")
    try:
        parts = urlsplit(url)
        valid = (isinstance(url, str) and parts.scheme == "https"
                 and (parts.hostname, parts.path) in {
                     ("passport.bilibili.com", "/h5-app/passport/login/scan"),
                     ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
                 }
                 and parts.port in (None, 443)
                 and not parts.username and not parts.password and not parts.fragment
                 and parse_qs(parts.query).get("qrcode_key") == [key]
                 and not any(ord(c) < 33 or ord(c) > 126 or c == "\\" for c in url))
        if not valid:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise LoginError("invalid_response") from None
    return {"url": url, "qrcode_key": key}


def _credentials(data: dict) -> dict:
    """Prefer transport-verified Set-Cookie; parse callback URL only as fallback."""
    if isinstance(data, _PollData) and all(data.response_credentials.get(k) for k in _REQUIRED):
        return dict(data.response_credentials)
    try:
        url = data["url"]
        if not isinstance(url, str) or len(url) > _MAX_BYTES:
            raise ValueError()
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in ("passport.biligame.com", "passport.bilibili.com", "account.bilibili.com")
                or parts.port not in (None, 443) or parts.username or parts.password or parts.fragment
                or any(ord(c) < 33 or c == "\\" for c in url)):
            raise ValueError()
        query = parse_qs(parts.query, max_num_fields=30)
        allowed = {"sessdata": "sessdata", "bili_jct": "bili_jct",
                   "buvid3": "buvid3", "dedeuserid": "dedeuserid"}
        result = {}
        for name, values in query.items():
            canonical = allowed.get(name.lower())
            if canonical is None:
                continue
            if canonical in result or len(values) != 1:
                raise ValueError()
            result[canonical] = _cookie_value(canonical, values[0])
        if not all(result.get(k) for k in ("sessdata", "bili_jct", "dedeuserid")):
            raise ValueError()
        return result
    except (KeyError, ValueError, TypeError, AttributeError):
        raise LoginError("invalid_credentials_response") from None


def diagnose(data: dict) -> dict:
    """Opt-in safe structural diagnosis; never returns URL/query/cookie values.

    Call diagnose(_request('poll', key)) locally if necessary; do not print the
    intermediate response. No automatic request or persistence occurs here.
    """
    result = {"scheme": "", "host": "", "path": "", "query_keys": [],
              "query_required_present": {key: False for key in _REQUIRED},
              "cookie_required_present": {key: False for key in _REQUIRED}}
    if isinstance(data, _PollData):
        result["cookie_required_present"] = {key: bool(data.response_credentials.get(key)) for key in _REQUIRED}
    try:
        url = data.get("url", "")
        if not isinstance(url, str) or len(url) > _MAX_BYTES:
            return result
        parts = urlsplit(url)
        query = parse_qs(parts.query, keep_blank_values=True, max_num_fields=30)
        # Never echo userinfo, query values, fragment, raw URL or error details.
        result.update(scheme=parts.scheme, host=parts.hostname or "", path=parts.path)
        result["query_keys"] = sorted(k for k in query if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k))
        lowered = {key.lower(): values for key, values in query.items()}
        result["query_required_present"] = {key: bool(lowered.get(key) and any(lowered[key])) for key in _REQUIRED}
    except (ValueError, TypeError, AttributeError):
        pass
    return result


def _save(credentials: dict):
    """Create temporary file beside destination, fsync, atomic replace, mode 0600."""
    temporary = None
    fd = None
    try:
        DESTINATION.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".bili_credential-", suffix=".tmp", dir=DESTINATION.parent)
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            fd = None  # fdopen now owns it, even if writing fails.
            json.dump(credentials, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, DESTINATION)
        temporary = None
    except (OSError, ValueError, TypeError):
        raise LoginError("credential_save_failed") from None
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def poll(key: str) -> dict:
    data = _request("poll", key)
    code = data.get("code")
    if type(code) is not int:
        raise LoginError("invalid_response")
    statuses = {86101: "waiting_scan", 86090: "waiting_confirmation", 86038: "expired"}
    if code in statuses:
        return {"status": statuses[code]}
    if code != 0:
        raise LoginError("unknown_login_status")
    _save(_credentials(data))
    return {"status": "success"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Official Bilibili QR login; credentials stay on this cloud host.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--generate", action="store_true")
    action.add_argument("--poll", metavar="KEY")
    args = parser.parse_args(argv)
    try:
        result = generate() if args.generate else poll(args.poll)
        code = 0
    except LoginError as exc:
        result = {"status": str(exc)}
        code = 1
    except Exception:
        # No traceback: an unexpected library error may contain credential data.
        result = {"status": "internal_error"}
        code = 1
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
