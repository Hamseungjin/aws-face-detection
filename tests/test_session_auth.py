"""Session cookie authentication tests (login → me → protected routes).

These tests exercise the real SessionMiddleware + FastAPI routes via TestClient.
They must not print secrets, cookie values, or passwords.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "web-ui" / "backend"
sys.path.insert(0, str(BACKEND))

# Fixed test environment before importing the app (boto3 clients are constructed
# at import time; disable IMDS and use dummy keys).
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_DEFAULT_REGION"] = "ap-northeast-2"
_IMPORT_DB_DIR = tempfile.TemporaryDirectory(prefix="session-auth-import-")
os.environ["KIOSK_DB_PATH"] = str(Path(_IMPORT_DB_DIR.name) / "kiosk.sqlite3")
os.environ["WEBUI_SESSION_SECRET"] = "test-session-secret-for-session-auth"
os.environ["WEBUI_SESSION_HTTPS_ONLY"] = "false"
os.environ["WEBUI_SESSION_SAME_SITE"] = "lax"
os.environ["WEBUI_AUTH_USERNAME"] = "session-tester"

import auth as auth_mod  # noqa: E402

os.environ["WEBUI_AUTH_PASSWORD_HASH"] = auth_mod.hash_password("session-test-password")

import importlib  # noqa: E402

import config as config_mod  # noqa: E402

importlib.reload(config_mod)

import app as app_mod  # noqa: E402

importlib.reload(app_mod)

from starlette.testclient import TestClient  # noqa: E402


def _redact_set_cookie(header_value):
    if not header_value:
        return ""
    return re.sub(r"(=)[^;]+", r"\1<redacted>", header_value, count=1)


def _login(client, username="session-tester", password="session-test-password"):
    return client.post("/api/login", json={"username": username, "password": password})


def test_login_sets_session_cookie_without_secure_when_https_only_false():
    assert config_mod.SESSION_HTTPS_ONLY is False
    client = TestClient(app_mod.app)
    response = _login(client)
    assert response.status_code == 200
    assert response.json().get("ok") is True
    set_cookie = response.headers.get("set-cookie") or ""
    assert config_mod.SESSION_COOKIE in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "secure" not in set_cookie.lower()
    # Value must never be required by assertions beyond presence.
    assert _redact_set_cookie(set_cookie).endswith("samesite=lax") or "samesite=lax" in set_cookie.lower()


def test_me_authenticated_after_login():
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    me = client.get("/api/me")
    assert me.status_code == 200
    body = me.json()
    assert body["authenticated"] is True
    assert body["user"] == "session-tester"
    # No secret material in the body.
    dumped = str(body).lower()
    assert "session" not in dumped or body.get("user") == "session-tester"
    assert "password" not in dumped
    assert "secret" not in dumped


def test_protected_endpoints_require_session():
    client = TestClient(app_mod.app)
    for path, method in (
        ("/api/kiosk/lockers", "get"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "authentication required"

    response = client.post(
        "/api/kiosk/retrieve/lookup",
        json={"retrievalCode": "12345678"},
    )
    assert response.status_code == 401


def test_protected_endpoints_with_session_do_not_return_401(monkeypatch):
    """Auth must pass; AWS may still fail with 502/503 — that is not a session 401."""
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200

    # /api/me is the session oracle
    assert client.get("/api/me").json()["authenticated"] is True

    lockers = client.get("/api/kiosk/lockers")
    assert lockers.status_code == 200
    assert "lockers" in lockers.json()
    assert client.get("/api/capture-frame").status_code == 404
    assert client.get("/api/config").status_code == 404
    assert client.get("/api/detect-labels").status_code == 404


def test_https_only_true_sets_secure_flag(monkeypatch):
    """Production HTTPS mode must retain Secure cookies (do not weaken globally)."""
    monkeypatch.setenv("WEBUI_SESSION_HTTPS_ONLY", "true")
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "prod-like-session-secret-value")
    monkeypatch.setenv("WEBUI_AUTH_USERNAME", "session-tester")
    monkeypatch.setenv(
        "WEBUI_AUTH_PASSWORD_HASH",
        auth_mod.hash_password("session-test-password"),
    )
    importlib.reload(config_mod)
    importlib.reload(app_mod)
    assert config_mod.SESSION_HTTPS_ONLY is True

    client = TestClient(app_mod.app)
    response = _login(client)
    assert response.status_code == 200
    set_cookie = (response.headers.get("set-cookie") or "").lower()
    assert "secure" in set_cookie
    assert "httponly" in set_cookie

    # Restore development defaults for any subsequent tests in this module.
    monkeypatch.setenv("WEBUI_SESSION_HTTPS_ONLY", "false")
    importlib.reload(config_mod)
    importlib.reload(app_mod)


def test_invalid_login_does_not_authenticate():
    client = TestClient(app_mod.app)
    response = client.post(
        "/api/login",
        json={"username": "session-tester", "password": "wrong-password"},
    )
    assert response.status_code == 401
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is False


def test_logout_clears_session():
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    assert client.get("/api/me").json()["authenticated"] is True
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/me").json()["authenticated"] is False
    assert client.get("/api/kiosk/lockers").status_code == 401


def test_config_module_defaults_https_only_false_for_local_http():
    """Missing WEBUI_SESSION_HTTPS_ONLY must not enable Secure cookies by default."""
    # config_mod was loaded with false; document the default contract.
    assert config_mod.SESSION_HTTPS_ONLY is False
    assert config_mod.SESSION_SAME_SITE == "lax"
    assert config_mod.SESSION_COOKIE == "webui_session"


# --- code-server /proxy session-cookie re-encoding compatibility -----------

from urllib.parse import quote  # noqa: E402

from session_cookie_compat import (  # noqa: E402
    CodeServerSessionCookieCompatMiddleware,
    normalize_session_cookie_header,
)


def _codeserver_encode_cookie_value(value: str) -> str:
    """Mirror code-server stringifyCookie encodeURIComponent-style encoding."""
    return quote(value, safe="")


def _session_cookie_value_from_jar(client) -> str:
    """Return session cookie value only; callers must not print it."""
    name = config_mod.SESSION_COOKIE
    assert name in client.cookies
    return client.cookies.get(name)


def test_compat_normal_direct_cookie_authenticates():
    """A. Direct (unencoded) session cookie still yields authenticated=true."""
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["user"] == "session-tester"


def test_compat_codeserver_encoded_cookie_authenticates():
    """B. encodeURIComponent-style session value is restored → authenticated=true."""
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    raw_value = _session_cookie_value_from_jar(client)
    # Must contain base64 padding that code-server would encode (or encode whole value).
    encoded_value = _codeserver_encode_cookie_value(raw_value)
    assert encoded_value != raw_value or "%" in encoded_value or "=" in raw_value

    # Fresh client: send only the percent-encoded Cookie header (no jar rewrite).
    probe = TestClient(app_mod.app)
    name = config_mod.SESSION_COOKIE
    me = probe.get("/api/me", headers={"Cookie": "%s=%s" % (name, encoded_value)})
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["user"] == "session-tester"


def test_compat_encoded_cookie_fails_without_normalization():
    """C. Same transformed cookie fails SessionMiddleware without compat layer."""
    from starlette.applications import Starlette
    from starlette.middleware.sessions import SessionMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    raw_value = _session_cookie_value_from_jar(client)
    encoded_value = _codeserver_encode_cookie_value(raw_value)
    name = config_mod.SESSION_COOKIE
    secret = config_mod.SESSION_SECRET

    async def me_route(request):
        return JSONResponse(
            {
                "authenticated": bool(request.session.get("user")),
                "user": request.session.get("user"),
            }
        )

    mini = Starlette(routes=[Route("/api/me", me_route)])
    mini.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=name,
        max_age=config_mod.SESSION_MAX_AGE,
        same_site=config_mod.SESSION_SAME_SITE,
        https_only=False,
    )
    # No CodeServerSessionCookieCompatMiddleware on *mini*.
    bare = TestClient(mini)
    me = bare.get("/api/me", headers={"Cookie": "%s=%s" % (name, encoded_value)})
    assert me.status_code == 200
    assert me.json()["authenticated"] is False
    assert me.json()["user"] is None

    # Control: original (unencoded) value still works on bare SessionMiddleware.
    me_ok = bare.get("/api/me", headers={"Cookie": "%s=%s" % (name, raw_value)})
    assert me_ok.status_code == 200
    assert me_ok.json()["authenticated"] is True


def test_compat_unrelated_cookies_preserved():
    """D. Only the configured session cookie value is rewritten."""
    name = config_mod.SESSION_COOKIE
    session_val = "payload%3D%3D.sig%2Bextra"
    header = "foo=bar; %s=%s; another=value%%keep" % (name, session_val)
    # Note: another has literal %keep (not a valid escape that unquote expands fully)
    new_header, changed = normalize_session_cookie_header(header, name)
    assert changed is True
    # Session value decoded once
    assert "%s=payload==.sig+extra" % name in new_header.replace(" ", "")
    # Unrelated cookies logically unchanged
    assert "foo=bar" in new_header
    assert "another=value%keep" in new_header
    # code-server-session style cookie must not be touched if present
    header2 = "code-server-session=abc%3Ddef; " + name + "=x%3D"
    new2, ch2 = normalize_session_cookie_header(header2, name)
    assert ch2 is True
    assert "code-server-session=abc%3Ddef" in new2
    assert (name + "=x=") in new2.replace(" ", "")


def test_compat_no_session_cookie_unauthenticated():
    """E. Missing session cookie keeps unauthenticated behavior."""
    client = TestClient(app_mod.app)
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is False
    assert client.get("/api/kiosk/lockers").status_code == 401


def test_compat_does_not_validate_tampered_session():
    """F. Middleware must not turn a tampered signature into a valid session."""
    client = TestClient(app_mod.app)
    assert _login(client).status_code == 200
    raw_value = _session_cookie_value_from_jar(client)
    # Tamper the signed payload (flip last character class safely).
    if raw_value[-1].isalnum():
        tampered = raw_value[:-1] + ("A" if raw_value[-1] != "A" else "B")
    else:
        tampered = raw_value + "x"
    assert tampered != raw_value

    name = config_mod.SESSION_COOKIE
    probe = TestClient(app_mod.app)
    # Direct tampered
    me1 = probe.get("/api/me", headers={"Cookie": "%s=%s" % (name, tampered)})
    assert me1.status_code == 200
    assert me1.json()["authenticated"] is False
    # code-server-encoded tampered — still invalid after one unquote
    encoded_tampered = _codeserver_encode_cookie_value(tampered)
    me2 = probe.get(
        "/api/me", headers={"Cookie": "%s=%s" % (name, encoded_tampered)}
    )
    assert me2.status_code == 200
    assert me2.json()["authenticated"] is False


def test_compat_double_encoded_normalized_at_most_once():
    """G. Only one unquote pass — double-encoded input is not fully expanded."""
    name = config_mod.SESSION_COOKIE
    # Simulate double encodeURIComponent: '=' -> '%3D' -> '%253D'
    single = "abc%3D"
    double = "abc%253D"
    h1, c1 = normalize_session_cookie_header("%s=%s" % (name, single), name)
    h2, c2 = normalize_session_cookie_header("%s=%s" % (name, double), name)
    assert c1 is True
    assert c2 is True
    # One pass: %3D -> = ; %253D -> %3D (NOT =)
    assert h1 == "%s=abc=" % name
    assert h2 == "%s=abc%%3D" % name or h2 == "%s=abc%3D" % name
    assert "=" not in h2.split("=", 1)[1] or h2.endswith("%3D")
    # No-op on already-normal value (no percent escapes)
    h3, c3 = normalize_session_cookie_header("%s=abc=" % name, name)
    assert c3 is False
    assert h3 == "%s=abc=" % name


def test_compat_middleware_only_http_scope_passthrough():
    """Non-HTTP scopes are forwarded without attempting Cookie mutation."""
    seen = {}

    async def inner(scope, receive, send):
        seen["type"] = scope["type"]
        seen["headers"] = scope.get("headers")

    mw = CodeServerSessionCookieCompatMiddleware(
        inner, session_cookie_name=config_mod.SESSION_COOKIE
    )

    async def run():
        await mw({"type": "websocket", "headers": []}, None, None)

    import asyncio

    asyncio.get_event_loop().run_until_complete(run())
    assert seen["type"] == "websocket"

