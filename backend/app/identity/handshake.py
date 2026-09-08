"""
The short-lived sealed cookie that carries one single sign-on across the redirect.

Between ``/api/auth/{provider}/start`` and the provider's callback the browser
visits the identity provider, so several values have to survive a round trip
this server does not control: the ``state``, the ``nonce``, the PKCE
``code_verifier`` and the exact ``redirect_uri`` that was sent. All of them are
secrets or integrity-critical, and none may be tampered with.

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

## One cookie per provider, not one cookie

The name and the path both carry the provider (``k8boss_admin_oidc`` on
``/api/auth/oidc``, ``k8boss_admin_saml`` on ``/api/auth/saml``). A console
offering several providers is a console where an operator can click the wrong
button, go back, and click another: a single shared cookie would let the second
handshake overwrite the first, and — worse — would let a callback for one
provider consume a handshake started for a different one. The provider is
therefore also written *inside* the sealed payload and checked on unseal, so the
binding survives a browser that ignores ``Path`` scoping.

## The cookie attributes, which are not interchangeable

``SameSite`` cannot be copied from the session cookie, which is ``Strict``, and
it is not even the same for every provider:

* **Redirect-callback providers (OIDC, OAuth, OpenShift) use ``Lax``.** The
  callback is a top-level ``GET`` navigation *from the identity provider's
  origin*, and a browser does not send a ``Strict`` cookie on a cross-site
  navigation — so a ``Strict`` handshake cookie is simply absent when the
  callback runs, and every sign-in fails with "the sign-in did not match".
  ``Lax`` is sent on top-level navigations, which is exactly and only what this
  needs.
* **SAML uses ``None``, and therefore requires ``Secure``.** Its assertion
  arrives on a cross-site *form POST*, and ``Lax`` is not sent on those either —
  this is the single most common reason a working SAML integration stops working
  in a browser that tightened its defaults. ``SameSite=None`` without ``Secure``
  is discarded outright by every current browser, which is why
  :func:`app.identity.saml.enabled` refuses to offer SAML on a deployment with
  ``AUTH_COOKIE_SECURE`` off rather than showing a button that cannot complete.

``HttpOnly`` because the page never reads it. ``Secure`` follows the session
cookie's setting for the redirect providers, so a plain-HTTP development
deployment still works and an HTTPS one is not downgraded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from cryptography.fernet import InvalidToken

from app import crypto
from app.config import settings

logger = logging.getLogger(__name__)

#: Prefix shared by every provider's handshake cookie.
COOKIE_PREFIX = "k8boss_admin"

#: How long a started sign-in may take to come back. Long enough for a password,
#: a second factor and a consent screen; short enough that an abandoned handshake
#: cookie is not a usable artefact later.
TTL_SECONDS = 600

#: Providers whose callback is a cross-site form POST rather than a navigation.
#: See the module docstring: these need ``SameSite=None; Secure`` and the others
#: must not have it, because ``None`` on a plain-HTTP deployment is discarded.
_CROSS_SITE_POST_PROVIDERS = frozenset({"saml"})


def cookie_name(provider: str) -> str:
    """The handshake cookie for one provider."""
    return f"{COOKIE_PREFIX}_{provider}"


def cookie_path(provider: str) -> str:
    """Cookie path. Narrower than the session cookie's ``/`` because nothing
    outside one provider's own routes has any use for it."""
    return f"/api/auth/{provider}"


@dataclass(frozen=True)
class Handshake:
    """What one sign-in attempt has to carry across the redirect.

    ``state``, ``nonce`` and ``code_verifier`` are the OAuth-family values.
    **SAML reuses two of them rather than growing its own fields**, and it is
    worth saying which: ``state`` holds the ``AuthnRequest`` ID, which is the
    value the assertion's ``InResponseTo`` has to equal, and it is checked for
    exactly the same reason — it is what binds the response to a sign-in this
    console started. ``nonce`` and ``code_verifier`` are empty there, because
    SAML has no ID token to bind and no code to exchange.
    """

    #: Which provider started this handshake. Checked on unseal, so a callback
    #: cannot consume a handshake minted for a different provider even if the
    #: browser ignores the cookie's ``Path``.
    provider: str
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
                "provider": handshake.provider,
                "state": handshake.state,
                "nonce": handshake.nonce,
                "code_verifier": handshake.code_verifier,
                "redirect_uri": handshake.redirect_uri,
                "next_path": handshake.next_path,
            },
            separators=(",", ":"),
        )
    )


def unseal(cookie_value: str | None, *, provider: str) -> Handshake | None:
    """Decrypt a handshake cookie, or ``None`` if it is missing, stale, forged
    or was minted for a different provider.

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
        sealed_provider = str(payload["provider"])
        if sealed_provider != provider:
            # Not a cookie muddle to be tolerated: the sealed payload names the
            # provider whose verification rules were meant to apply to the
            # response now being presented, and honouring it under another
            # provider's rules is how a flow with weaker checks gets to complete
            # a handshake started for one with stronger ones.
            logger.info(
                "A handshake sealed for %r was presented on the %r callback; "
                "refusing it.", sealed_provider, provider,
            )
            return None
        return Handshake(
            provider=sealed_provider,
            state=str(payload["state"]),
            nonce=str(payload["nonce"]),
            code_verifier=str(payload["code_verifier"]),
            redirect_uri=str(payload["redirect_uri"]),
            next_path=str(payload.get("next_path") or "/"),
        )
    except (InvalidToken, ValueError, KeyError, TypeError):
        logger.info(
            "A %s handshake cookie was missing, expired or unreadable; the "
            "sign-in will have to be restarted.", provider,
        )
        return None


def cookie_attributes(provider: str) -> dict[str, object]:
    """Keyword arguments for ``Response.set_cookie``. See the module docstring."""
    cross_site_post = provider in _CROSS_SITE_POST_PROVIDERS
    return {
        "max_age": TTL_SECONDS,
        "path": cookie_path(provider),
        "httponly": True,
        # SameSite=None is only honoured on a Secure cookie, so it is forced
        # here rather than followed from the setting. The providers that need it
        # are refused outright on a deployment that is not serving over HTTPS —
        # see app.identity.saml.enabled — so this cannot quietly mark a cookie
        # Secure on a plain-HTTP console and make every sign-in fail instead.
        "secure": True if cross_site_post else settings.auth_cookie_secure,
        # Lax, NOT Strict, for the redirect providers: a Strict cookie is not
        # sent on the cross-site top-level navigation the identity provider
        # performs, so the callback would never see it. None, not Lax, for a
        # cross-site form POST, which does not carry a Lax cookie either.
        "samesite": "none" if cross_site_post else "lax",
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
    "COOKIE_PREFIX",
    "TTL_SECONDS",
    "Handshake",
    "cookie_attributes",
    "cookie_name",
    "cookie_path",
    "safe_next_path",
    "seal",
    "unseal",
]
