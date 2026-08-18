"""Opt-in cookie session authentication for HTTP and WebSocket APIs."""

from __future__ import annotations

import hmac
from http.cookies import CookieError, SimpleCookie

from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import AuthenticationRequired, PermissionDenied
from app.identity.service import load_session

_PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/config", "/api/auth/login"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _cookie(scope) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    try:
        parsed = SimpleCookie()
        parsed.load(raw)
        morsel = parsed.get(settings.auth_cookie_name)
        return morsel.value if morsel else None
    except CookieError:
        return None


async def _reject_http(scope, receive, send, error) -> None:
    response = JSONResponse(
        status_code=error.http_status,
        content=error.to_envelope(),
        headers={"WWW-Authenticate": "Session"} if error.http_status == 401 else None,
    )
    await response(scope, receive, send)


class AuthenticationMiddleware:
    """Attach a verified principal and reject protected unauthenticated requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_principal"] = None
        state["auth_session"] = None

        if not settings.auth_enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path in _PUBLIC_PATHS or scope.get("method", "GET").upper() == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        session = load_session(_cookie(scope))
        if session is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4401, "reason": "Sign in required"})
                return
            await _reject_http(scope, receive, send, AuthenticationRequired())
            return

        state["auth_principal"] = session.principal
        state["auth_session"] = session

        if scope["type"] == "http" and scope.get("method", "GET").upper() not in _SAFE_METHODS:
            supplied = _header(scope, b"x-csrf-token") or ""
            if not hmac.compare_digest(supplied, session.csrf_token):
                await _reject_http(
                    scope,
                    receive,
                    send,
                    PermissionDenied(
                        "The request did not carry the CSRF token bound to this session."
                    ),
                )
                return

        await self.app(scope, receive, send)