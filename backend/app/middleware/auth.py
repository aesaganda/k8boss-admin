"""Opt-in cookie session authentication for HTTP and WebSocket APIs."""

from __future__ import annotations

import hmac
from http.cookies import CookieError, SimpleCookie

from fastapi.responses import JSONResponse

from app.config import settings
from app.errors import AuthenticationRequired, PermissionDenied
from app.identity.service import load_session

#: Reachable without a session before any provider's routes are added.
#:
#: Single sign-on *is how you get a session*, so every route on the way to one
#: has to be here: challenging them for a session makes the flow impossible to
#: start, and the symptom is a login button that answers 401.
#:
#: Being public is not the same as being unprotected. ``/start`` mints a sealed
#: handshake and redirects; a callback refuses anything that does not match a
#: handshake this console started, and issues a session only after a verified
#: assertion.
_BASE_PUBLIC_PATHS = frozenset({
    "/api/health",
    "/api/auth/config",
    "/api/auth/login",
})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_public_paths_cache: frozenset[str] | None = None


def _public_paths() -> frozenset[str]:
    """Every path reachable without a session. Still exact strings.

    Built from the single sign-on registry rather than written out, so a fifth
    provider cannot be reachable in the router and unreachable here — which
    fails as "sign in to continue" on the callback of a provider that just
    authenticated somebody, and reads as a broken identity provider.

    Enumerated rather than prefix-matched, deliberately. A rule like "anything
    under /api/auth/" is one route away from exempting something that should
    never have been: this list is short, and every entry on it is a route that
    exists because a session cannot yet be presented.

    **One of them is a POST**, which the CSRF check below would otherwise cover:
    SAML's assertion consumer service. It is exempt because the request comes
    from the identity provider's origin and a console demanding a CSRF token
    there would be demanding one from a party that has never seen a page of it.
    What stands in its place is not weaker: the assertion carries a signature
    checked against a configured certificate, and an ``InResponseTo`` *inside
    that signed subtree* which must equal the request id sealed in this
    browser's own handshake cookie. Any future POST route added here needs its
    own answer to that question, not this one by inheritance.
    """
    global _public_paths_cache
    if _public_paths_cache is None:
        from app.identity import sso

        paths = set(_BASE_PUBLIC_PATHS)
        for provider in sso.providers().values():
            paths.add(f"/api/auth/{provider.NAME}/start")
            paths.add(f"/api/auth/{provider.NAME}/{provider.CALLBACK_SUFFIX}")
        # Service-provider metadata: an entityID and an ACS URL, published so an
        # administrator does not transcribe them. Public because the identity
        # provider's setup form fetches it, and there is nobody signed in on the
        # far side of that fetch.
        paths.add("/api/auth/saml/metadata")
        _public_paths_cache = frozenset(paths)
    return _public_paths_cache


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
            path in _public_paths() or scope.get("method", "GET").upper() == "OPTIONS"
        ):
            await self.app(scope, receive, send)
            return

        session = load_session(_cookie(scope))
        if session is None:
            if scope["type"] == "websocket":
                # Accepted first, then closed. A close sent before the accept is
                # a handshake failure at the transport level, and the browser
                # never surfaces the code: both viewers then reported an expired
                # session as "connection lost" and sent the operator to debug a
                # network that was fine. Accepting costs one frame and makes
                # 4401 reach `onclose`, which is the only place the frontend can
                # tell "sign in again" from "something dropped us".
                await send({"type": "websocket.accept"})
                await send({"type": "websocket.close", "code": 4401, "reason": "Sign in required"})
                return
            await _reject_http(scope, receive, send, AuthenticationRequired())
            return

        state["auth_principal"] = session.principal
        state["auth_session"] = session

        if scope["type"] == "http" and scope.get("method", "GET").upper() not in _SAFE_METHODS:
            supplied = _header(scope, b"x-csrf-token") or ""
            # Compared as bytes. `compare_digest` refuses a `str` carrying any
            # non-ASCII character, and the TypeError escapes this middleware as a
            # 500 rather than the audited refusal a bad CSRF token is supposed to
            # produce: the header is the attacker's to write, so one non-ASCII
            # character would trade a recorded security event for an unrecorded
            # server error.
            if not hmac.compare_digest(supplied.encode(), session.csrf_token.encode()):
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