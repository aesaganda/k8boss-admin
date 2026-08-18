"""
The short-lived sealed cookie that carries one OIDC sign-in across the redirect.

Between ``/api/auth/oidc/start`` and ``/api/auth/oidc/callback`` the browser
visits the identity provider, so four values have to survive a round trip this
server does not control: the ``state``, the ``nonce``, the PKCE ``code_verifier``
and the exact ``redirect_uri`` that was sent. All four are secrets or integrity-
critical, and none may be tampered with.

**Sealed with the console's existing Fernet key** (:mod:`app.crypto`), not with a
new signing secret. Two reasons, and the second is the load-bearing one:

* That module already solved key persistence, including the failure it was
  written against — a key regenerated per boot silently invalidates everything
  encrypted with the previous one. A second secret would need the same care and
  would not get it.
* Fernet tokens carry an authenticated timestamp, so ``decrypt`` with a TTL gives
  expiry for free. Hand-rolled expiry inside the payload is checkable only after
  decryption and is the kind of thing that gets skipped.

**A database table would also work and is deliberately not used.** A pending
handshake is per-browser, lives for a few minutes, and is worthless to anyone
else. Storing it would add a table, a cleanup job for abandoned sign-ins, and a
cross-replica read on the hottest path of the login flow, to hold state that the
browser is already carrying.

## The cookie attributes, which are not interchangeable

``SameSite=Lax``, and this is the one setting that cannot be copied from the
session cookie. The session cookie is ``Strict``. The callback request is a
top-level navigation *from the identity provider's origin*, and a browser does
not send a ``Strict`` cookie on a cross-site navigation — so a ``Strict``
handshake cookie is simply absent when the callback runs, and every sign-in fails
with "the sign-in did not match". ``Lax`` is sent on top-level navigations, which
is exactly and only what this needs.

``HttpOnly`` because the page never reads it. ``Secure`` follows the session
cookie's setting so a plain-HTTP development deployment still works and an HTTPS
one is not downgraded. ``Path=/api/auth/oidc`` so it is not attached to every
request in the console for the few minutes it exists.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from cryptography.fernet import InvalidToken

from app import crypto
from app.config import settings

logger = logging.getLogger(__name__)

#: Cookie name for the sealed handshake.
COOKIE_NAME = "k8boss_admin_oidc"

#: Cookie path. Narrower than the session cookie's ``/`` because nothing outside
#: the OIDC routes has any use for it.
COOKIE_PATH = "/api/auth/oidc"

#: How long a started sign-in may take to come back. Long enough for a password,
#: a second factor and a consent screen; short enough that an abandoned handshake
#: cookie is not a usable artefact later.
TTL_SECONDS = 600


@dataclass(frozen=True)
class Handshake:
    """The four values one sign-in attempt has to carry across the redirect."""

    state: str
    nonce: str
    code_verifier: str
    redirect_uri: str
    #: Where to send the browser once the session exists. Carried through the
    #: handshake rather than taken from the callback's query string, which the
    #: identity provider controls — an attacker-chosen post-login redirect is an
    #: open redirect wearing this console's domain.
    next_path: str = "/"


def seal(handshake: Handshake) -> str:
    """Encrypt and authenticate a handshake into a cookie value."""
    return crypto.encrypt(
        json.dumps(
            {
                "state": handshake.state,
                "nonce": handshake.nonce,
                "code_verifier": handshake.code_verifier,
                "redirect_uri": handshake.redirect_uri,
                "next_path": handshake.next_path,
            },
            separators=(",", ":"),
        )
    )


def unseal(cookie_value: str | None) -> Handshake | None:
    """Decrypt a handshake cookie, or ``None`` if it is missing, stale or forged.

    One ``None`` for every failure, on purpose. The caller turns it into a single
    "start the sign-in again" outcome, and distinguishing "expired" from
    "tampered with" for the browser would tell whoever is submitting cookies
    which of their guesses was closer.

    The TTL is enforced by Fernet against its own authenticated timestamp, so an
    expired token is rejected before its contents are parsed — the payload never
    gets to assert its own freshness.
    """
    if not cookie_value:
        return None
    try:
        raw = crypto.decrypt_with_ttl(cookie_value, ttl_seconds=TTL_SECONDS)
        payload = json.loads(raw)
        return Handshake(
            state=str(payload["state"]),
            nonce=str(payload["nonce"]),
            code_verifier=str(payload["code_verifier"]),
            redirect_uri=str(payload["redirect_uri"]),
            next_path=str(payload.get("next_path") or "/"),
        )
    except (InvalidToken, ValueError, KeyError, TypeError):
        logger.info(
            "An OIDC handshake cookie was missing, expired or unreadable; the "
            "sign-in will have to be restarted."
        )
        return None


def cookie_attributes() -> dict[str, object]:
    """Keyword arguments for ``Response.set_cookie``. See the module docstring."""
    return {
        "max_age": TTL_SECONDS,
        "path": COOKIE_PATH,
        "httponly": True,
        "secure": settings.auth_cookie_secure,
        # Lax, NOT Strict. A Strict cookie is not sent on the cross-site
        # top-level navigation the identity provider performs, so the callback
        # would never see it and every sign-in would fail.
        "samesite": "lax",
    }


def safe_next_path(candidate: str | None) -> str:
    """A same-origin path to return to after sign-in, or ``/``.

    Only a path is ever accepted, and only one that starts with a single ``/``.
    Anything else — an absolute URL, a scheme-relative ``//evil.example``, a
    backslash some browsers normalise into a slash — becomes ``/``.

    The failure this prevents: a login link carrying
    ``?next=https://evil.example`` produces a page on the console's own domain
    that authenticates the user and then hands them to somebody else's site,
    which is the classic phishing amplifier for exactly this endpoint.
    """
    value = (candidate or "").strip()
    if not value.startswith("/"):
        return "/"
    if value.startswith("//") or value.startswith("/\\"):
        return "/"
    if "\\" in value or "\n" in value or "\r" in value:
        return "/"
    return value


__all__ = [
    "COOKIE_NAME",
    "COOKIE_PATH",
    "TTL_SECONDS",
    "Handshake",
    "cookie_attributes",
    "safe_next_path",
    "seal",
    "unseal",
]
