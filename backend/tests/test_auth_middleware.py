"""The ASGI-level half of session authentication, driven without a TestClient.

The middleware is exercised directly here because the thing worth asserting is
the *order* of the two messages it sends to reject a WebSocket, and a test
client hides it: Starlette's `TestClient` hands a pre-accept close code straight
to the caller, so a rejection that never accepted still looks like 4401 from
inside a test while a real browser sees only a failed handshake. Asserting the
code would therefore have passed against the bug this file exists to catch.
"""

from __future__ import annotations

import json

import pytest

from app.config import settings
from app.identity.service import SessionIdentity
from app.middleware import auth as auth_module
from app.middleware.auth import AuthenticationMiddleware


async def _never_called(scope, receive, send):
    raise AssertionError(
        "The application was reached without a session; the middleware should "
        "have rejected the connection itself."
    )


async def _receive():  # pragma: no cover - the rejection never reads a frame
    raise AssertionError("The rejection should not wait for a client frame.")


def _ws_scope(path: str = "/api/ws/pods/default/api/logs") -> dict:
    return {"type": "websocket", "path": path, "headers": []}


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    return settings


async def test_the_websocket_rejection_accepts_before_it_closes(auth_enabled):
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await AuthenticationMiddleware(_never_called)(_ws_scope(), _receive, send)

    # The order is the whole point. A close before the accept is a handshake
    # failure, the browser reports a network error, and the operator whose
    # session expired goes looking for a proxy problem that does not exist.
    assert [message["type"] for message in sent] == [
        "websocket.accept",
        "websocket.close",
    ]


async def test_the_websocket_rejection_carries_the_session_expiry_code(auth_enabled):
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await AuthenticationMiddleware(_never_called)(_ws_scope(), _receive, send)

    close = sent[-1]
    # 4401 is what LogViewer and PodTerminal branch on to say "sign in again"
    # rather than blaming the connection. It only reaches them because of the
    # accept asserted above.
    assert close["code"] == 4401


async def test_a_websocket_with_authentication_disabled_is_not_intercepted(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    reached = False

    async def app(scope, receive, send):
        nonlocal reached
        reached = True

    async def send(message):  # pragma: no cover - nothing is sent on this path
        raise AssertionError("A disabled auth middleware must send nothing itself.")

    await AuthenticationMiddleware(app)(_ws_scope(), _receive, send)

    assert reached is True


def _http_scope(csrf: bytes) -> dict:
    """A state-changing request carrying `csrf` as the raw header bytes."""
    return {
        "type": "http",
        "path": "/api/clusters",
        "method": "POST",
        "headers": [(b"x-csrf-token", csrf)],
    }


async def test_a_non_ascii_csrf_token_is_refused_rather_than_crashing(
    auth_enabled, monkeypatch
):
    """The header is the attacker's to write, and the refusal has to survive it.

    `hmac.compare_digest` raises TypeError on a `str` carrying any non-ASCII
    character. Compared as strings, one raw byte above 127 in X-CSRF-Token turned
    this audited 403 into an unhandled 500 rendered outside CORSMiddleware — an
    unrecorded server error bought with a single byte.
    """
    session = SessionIdentity(
        principal=object(), csrf_token="expected-token", expires_at=None,
    )
    monkeypatch.setattr(auth_module, "load_session", lambda _cookie: session)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    await AuthenticationMiddleware(_never_called)(
        _http_scope("é".encode("latin-1")), _receive, send,
    )

    start = sent[0]
    assert start["status"] == 403
    assert json.loads(sent[1]["body"])["error"] == "permission_denied"
