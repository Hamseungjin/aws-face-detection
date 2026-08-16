"""code-server /proxy Cookie re-encoding compatibility.

code-server reconstructs the Cookie header with encodeURIComponent-style
percent-encoding (e.g. base64 padding '=' -> '%3D'). Starlette's
SessionMiddleware then fails to verify the signed session cookie.

This module restores ONLY the configured application session cookie value
exactly once before SessionMiddleware runs. Unrelated cookies are left
unchanged. Cookie values are never logged.
"""
from __future__ import annotations

import logging
from urllib.parse import unquote

logger = logging.getLogger("webui")


def normalize_session_cookie_header(
    cookie_header: str,
    session_cookie_name: str,
) -> tuple[str, bool]:
    """Percent-decode the session cookie value at most once.

    Returns (header, changed) where *changed* is True only when the session
    cookie value was rewritten. Other cookies are preserved segment-for-segment.
    """
    if not cookie_header or not session_cookie_name:
        return cookie_header or "", False

    segments = cookie_header.split(";")
    out: list[str] = []
    changed = False

    for segment in segments:
        stripped = segment.strip()
        if not stripped or "=" not in stripped:
            out.append(segment)
            continue

        name, sep, value = stripped.partition("=")
        name = name.strip()
        if name != session_cookie_name:
            out.append(segment)
            continue

        # Reverse a single encodeURIComponent-style pass only.
        decoded = unquote(value)
        if decoded == value:
            out.append(segment)
            continue

        changed = True
        leading = segment[: len(segment) - len(segment.lstrip())]
        out.append("%s%s=%s" % (leading, name, decoded))

    return ";".join(out), changed


class CodeServerSessionCookieCompatMiddleware:
    """ASGI middleware: normalize app session cookie before SessionMiddleware.

    Must be registered *after* SessionMiddleware via FastAPI/Starlette
    add_middleware() so it wraps SessionMiddleware and runs first on the
    inbound request path.
    """

    def __init__(self, app, session_cookie_name: str):
        self.app = app
        self.session_cookie_name = session_cookie_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers")
        if not headers:
            await self.app(scope, receive, send)
            return

        new_headers = []
        changed = False
        for key, value in headers:
            if key.lower() != b"cookie":
                new_headers.append((key, value))
                continue
            try:
                text = value.decode("latin-1")
            except Exception:
                new_headers.append((key, value))
                continue
            new_text, did_change = normalize_session_cookie_header(
                text, self.session_cookie_name
            )
            if did_change:
                changed = True
                new_headers.append((key, new_text.encode("latin-1")))
            else:
                new_headers.append((key, value))

        if changed:
            scope = dict(scope)
            scope["headers"] = new_headers
            # Safe diagnostic only — never log Cookie header or values.
            logger.debug("proxy_cookie_normalized=true")

        await self.app(scope, receive, send)
