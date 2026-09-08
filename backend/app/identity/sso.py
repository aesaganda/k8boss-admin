"""
What the four single sign-on providers share, and the registry the routes use.

This console offers four ways to sign in without a password — OpenID Connect, a
generic OAuth 2.0 authorization server, the built-in OpenShift OAuth server, and
SAML 2.0 — and they differ in almost everything that matters: how the browser is
sent away, what comes back, and what makes the answer trustworthy. What they do
*not* differ in is what happens afterwards, and that is what lives here: the
identity shape they all reduce to, the tri-state group rule, the allowlist that
fails closed, and the TLS trust configuration that decides whether any of the
assertions were delivered over a channel worth believing.

## Why a registry of modules rather than a class hierarchy

Each provider is a module exposing the small interface documented in
:data:`PROVIDERS`, and :mod:`app.api.auth` drives all four through it. The
alternative — one base class with four subclasses — was rejected because the
interesting content of each provider is its *argument*: OIDC's is a list of
checks on a signed token, OpenShift's is why an unsigned access token is
nonetheless the cluster speaking, SAML's is which subtree of an XML document may
be read. Those arguments belong in module docstrings next to the code they
justify, and a hierarchy would push three of them into overrides of a method
whose base implementation is wrong for all of them.

The registry is also what keeps the *route* generic. There is one
``/api/auth/{provider}/start`` and one callback per binding, so a fifth provider
cannot arrive with a fifth pair of hand-written handlers that quietly skips the
audit call the other four make.

## The one rule that is not negotiable across all four

**A group membership that was not reported is not an empty one.** Every provider
returns ``groups=None`` when it could not see membership at all and ``()`` when
it looked and there was none, and the two go to opposite answers: role mapping
leaves a stored role alone on ``None`` (writing the default demotes an
administrator whenever the provider omits the attribute), and the sign-in
allowlist refuses on ``None`` (an allowlist that admits everyone whenever the
claim goes missing stops working exactly when the provider is misconfigured).
:mod:`app.identity.roles` holds the first half; :func:`check_group_allowlist`
holds the second.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from app.config import settings
from app.errors import IdentityProviderUnavailable, Invalid, PermissionDenied

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FederatedIdentity:
    """One verified assertion, reduced to what this console stores.

    The same shape whether it came from a signed ID token, a userinfo document,
    the cluster's own ``User`` object or a SAML assertion — because everything
    downstream of verification (account binding, role mapping, the audit row) is
    identical, and four near-identical dataclasses would be four places for that
    downstream code to drift.
    """

    #: The provider's own stable identifier for the person. This is what the
    #: account is bound to, and it is deliberately not the username: a username
    #: is a label a provider may re-issue to somebody else.
    subject: str
    username: str
    display_name: str | None
    email: str | None
    #: Groups as reported, or ``None`` when the provider did not report them at
    #: all. See the module docstring — the distinction is load-bearing in two
    #: opposite directions.
    groups: tuple[str, ...] | None


@dataclass(frozen=True)
class Begin:
    """A started handshake: where to send the browser, and what to seal."""

    url: str
    handshake: Any


def parse_groups(raw: Any) -> tuple[str, ...] | None:
    """Normalise a provider's group value into the tri-state.

    ``None`` in, ``None`` out: absent stays absent all the way through.

    A **bare string** is split on commas and whitespace rather than wrapped in a
    one-element tuple. Several issuers emit a single group as a plain string and
    several emit a delimited list in one, and treating ``"admins,staff"`` as one
    group produces a group nobody is ever in — which fails as a silent
    non-promotion rather than as an error.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return tuple(part for part in raw.replace(",", " ").split() if part)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return tuple(str(group) for group in raw)
    # A scalar that is not a string — an integer group id, say. One group.
    return (str(raw),)


def parse_allowed_groups(configured: str) -> set[str]:
    """The comma-separated allowlist setting, normalised for comparison."""
    from app.identity import roles

    return {
        roles.normalize_group(group)
        for group in (configured or "").split(",")
        if group.strip()
    }


def check_group_allowlist(
    identity: FederatedIdentity,
    *,
    allowed: Iterable[str] | str,
    provider: str,
    source_hint: str,
) -> None:
    """Refuse a verified identity that is not in a permitted group.

    An empty allowlist means every account the provider authenticates may use
    the console, which is the right default for a deployment whose provider
    already only knows the right people.

    When it is set and the groups were **not reported**, this refuses. That is
    the opposite of the role-mapping rule in :mod:`app.identity.roles`, and
    deliberately: role mapping asks "should this person be promoted", where the
    safe answer under uncertainty is to change nothing, while this asks "may
    this person in at all", where the safe answer under uncertainty is no. An
    allowlist that admitted everyone whenever the membership went missing would
    be an allowlist that stops working exactly when the provider is
    misconfigured.

    ``source_hint`` is the sentence that tells the administrator *where* to add
    the membership — a claim, a userinfo field, a SAML attribute — because
    "groups were not reported" is not actionable and "add the groups claim to
    the client's token mapping" is.
    """
    from app.identity import roles

    permitted = (
        parse_allowed_groups(allowed)
        if isinstance(allowed, str)
        else {roles.normalize_group(group) for group in allowed}
    )
    if not permitted:
        return

    if identity.groups is None:
        logger.warning(
            "Refusing %s sign-in for %r: an allowlist is configured but the "
            "provider reported no group membership, so it could not be checked.",
            provider, identity.username,
        )
        raise PermissionDenied(
            "Your single sign-on account could not be checked against this "
            "console's permitted groups.",
            hint=source_hint,
            context={"provider": provider},
        )

    presented = {roles.normalize_group(group) for group in identity.groups}
    if not (presented & permitted):
        logger.info(
            "Refusing %s sign-in for %r: not in a permitted group.",
            provider, identity.username,
        )
        raise PermissionDenied(
            "Your account is not a member of a group permitted to use this "
            "console.",
            context={"provider": provider},
        )


def read_field(document: Mapping[str, Any], path: str) -> Any:
    """Read ``path`` out of a JSON document, descending on dots.

    A flat key is the common case and is tried first, so a provider that really
    does emit a field called ``user.name`` is not mis-read as a nested one. The
    dotted form exists because a userinfo document is not standardised outside
    OpenID Connect and several real providers nest the interesting values one or
    two levels down; without it those deployments would need a proxy in front of
    the provider to flatten a document.

    Returns ``None`` for an absent path, which every caller then has to keep
    distinct from an empty value — see :func:`parse_groups`.
    """
    if not path:
        return None
    if path in document:
        return document[path]
    current: Any = document
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def tls_verify(*, verify: bool, ca_file: str, provider: str) -> bool | str:
    """What to hand ``httpx`` as ``verify``.

    A CA bundle path when one is configured, otherwise the boolean. Disabling
    verification entirely is possible and is logged at WARNING every time,
    because an unverified TLS connection to an identity provider means the
    assertions this console carefully validates were delivered by whoever was on
    the path — and for the two providers with no signature to check at all
    (OAuth 2.0 and OpenShift), the channel *is* the whole trust argument.
    """
    if ca_file:
        return ca_file
    if not verify:
        logger.warning(
            "TLS verification is disabled for the %s identity provider. Its "
            "certificate is not being checked, so the identity this console is "
            "about to trust was delivered over a channel anyone on the path can "
            "control.", provider,
        )
        return False
    return True


def tls_context(*, verify: bool, ca_file: str) -> ssl.SSLContext | None:
    """The same trust configuration as an ``SSLContext``, for non-httpx callers.

    ``httpx`` takes a CA bundle path or a bool directly; ``PyJWKClient`` fetches
    the JWKS with ``urllib``, which takes neither. Without this the JWKS fetch
    silently ignores the CA file and the verification flag and falls back to the
    system trust store — so an issuer behind a private CA completes discovery
    and then fails at key retrieval, reported as "the provider's signing keys
    could not be read", on a deployment whose CA is configured correctly.

    ``None`` means "the library's default", which is right only when neither
    setting is in play.
    """
    if ca_file:
        return ssl.create_default_context(cafile=ca_file)
    if not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return None


def decode_token_response(response: Any, *, provider: str) -> dict[str, Any]:
    """The token endpoint's body, whether it answered JSON or form-encoded.

    RFC 6749 requires JSON, and GitHub — the single most common deployment of
    the generic OAuth provider — answers ``application/x-www-form-urlencoded``
    unless the request carried ``Accept: application/json``. This console sends
    that header, so the form case should not arise; it is parsed anyway because
    the failure when it does is a ``JSONDecodeError`` escaping as a 500, which
    tells the operator nothing about a server that is behaving exactly the way
    its documentation says it does.
    """
    from urllib.parse import parse_qsl

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
    if content_type == "application/x-www-form-urlencoded":
        return dict(parse_qsl(response.text))
    try:
        payload = response.json()
    except ValueError as exc:
        raise IdentityProviderUnavailable(
            "The identity provider's token endpoint returned a body this console "
            "could not read.",
            detail=f"content-type {content_type!r}",
            context={"provider": provider},
        ) from exc
    if not isinstance(payload, dict):
        raise IdentityProviderUnavailable(
            "The identity provider's token endpoint did not return an object.",
            context={"provider": provider},
        )
    return payload


def exchange_authorization_code(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    verify: bool | str,
    timeout: float,
    provider: str,
) -> dict[str, Any]:
    """Trade an authorization code for tokens. Shared by all three OAuth-family
    providers, because the differences between them are in what the *response*
    is worth, not in how the code is spent.

    A confidential client sends its secret; a public client relies on PKCE
    alone, which is the current recommendation for browser-driven flows and is
    why the secret is optional rather than required.

    Three response classes, kept apart on purpose:

    * **5xx** is the provider being unwell — ``identity_provider_unavailable``.
      Recording it as a refusal would put an outage in the audit trail as
      ``denied``, the outcome reserved for a credential rejection and the one
      that looks like an attack, and would send the operator to check a client
      registration that is fine.
    * **4xx** is a refusal. The body is *not* propagated: some servers echo the
      client secret back in an error description.
    * **200 carrying an ``error``** is a refusal too. Some servers answer
      failures with the wrong status, and dropping the field would report this
      two lines later as "no access token", as though the provider had said
      nothing at all.
    """
    import httpx

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        with httpx.Client(timeout=timeout, verify=verify) as client:
            response = client.post(
                token_url, data=data, headers={"Accept": "application/json"}
            )
    except Exception as exc:  # noqa: BLE001 - every transport failure means the same thing
        raise IdentityProviderUnavailable(
            "The identity provider could not be reached to complete the sign-in.",
            detail=type(exc).__name__,
            context={"provider": provider},
        ) from exc

    if response.status_code != 200:
        logger.error(
            "%s token exchange returned HTTP %s.", provider, response.status_code
        )
        if response.status_code >= 500:
            raise IdentityProviderUnavailable(
                "The identity provider could not complete the sign-in.",
                detail=f"token endpoint returned HTTP {response.status_code}",
                hint="The provider returned a server error. Nothing about the "
                     "account or this console's configuration is implied.",
                context={"provider": provider},
            )
        raise PermissionDenied(
            "The identity provider refused to complete the sign-in.",
            detail=f"token endpoint returned HTTP {response.status_code}",
            hint="Usually a mismatched redirect URI or client secret. Check the "
                 "client registration at the provider.",
            context={"provider": provider},
        )

    payload = decode_token_response(response, provider=provider)
    if payload.get("error"):
        raise PermissionDenied(
            "The identity provider refused to complete the sign-in.",
            detail=str(payload.get("error"))[:200],
            context={"provider": provider},
        )
    return payload


def generate_pkce() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` for PKCE S256.

    S256 rather than ``plain``: with ``plain`` the challenge *is* the verifier,
    so anyone who saw the authorization request can complete the exchange, which
    is the whole thing PKCE exists to stop. It matters most on the two providers
    with no signed assertion, where PKCE and ``state`` are the only two things
    binding the response to this sign-in.
    """
    import base64
    import hashlib
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #
#
# Each value is a module exposing:
#
#   NAME               str, matching the key and the URL segment
#   BINDING            "query" when the provider calls back with a GET carrying
#                      query parameters, "form" when it POSTs a form. It decides
#                      which route serves the callback and — through
#                      app.identity.handshake — whether the handshake cookie can
#                      be SameSite=Lax or has to be None+Secure.
#   CALLBACK_SUFFIX    the path segment after /api/auth/{name}/ that the provider
#                      calls back on. "callback" for the OAuth family, "acs" for
#                      SAML, because that is the name every SAML administrator
#                      registers and a console that called it something else
#                      would be asking them to mistype it.
#   enabled()          -> bool. Every value the flow needs, present. A provider
#                      that is half-configured reports False rather than
#                      offering a button that leads to an error: a broken button
#                      reads as a broken console, an absent one reads as "not set
#                      up here", and only the second is actionable.
#   require_enabled()  -> None, raising Invalid when it is not.
#   label()            -> str, the login page's button text.
#   admin_group()      -> str, the group whose members get the console's admin role.
#   configured_callback_url() -> str, the explicitly configured absolute URL, or
#                      "" to derive one from the request.
#   begin(callback_url, next_path) -> Begin
#   complete(handshake, params) -> FederatedIdentity
#
# The route layer knows nothing else about any of them. That is the point: the
# audit calls, the throttle, the account provisioning and the failure redirect
# are written once, so a fifth provider cannot arrive with its own copy of them
# that forgets one.
_REGISTRY: dict[str, Any] | None = None


def providers() -> dict[str, Any]:
    """The provider registry, built on first use.

    Late import because each provider module imports :mod:`app.config` and, in
    SAML's case, a signature library — and this module is imported by
    :mod:`app.identity.roles`' callers at a point where a circular import would
    otherwise be possible.
    """
    global _REGISTRY
    if _REGISTRY is None:
        from app.identity import oauth, oidc, openshift, saml

        _REGISTRY = {
            module.NAME: module for module in (oidc, oauth, openshift, saml)
        }
    return _REGISTRY


def get(name: str) -> Any | None:
    """The provider module for ``name``, or ``None`` if there is no such provider."""
    return providers().get(name)


def enabled_providers() -> list[Any]:
    """Every provider this deployment can actually complete a sign-in with.

    Empty unless ``AUTH_ENABLED``: single sign-on issues a console session, and
    a console that is not authenticating requests has no session to issue.
    Offering the button anyway would send an operator through a full handshake
    to arrive back at a console that never asked who they were.
    """
    if not settings.auth_enabled:
        return []
    return [module for module in providers().values() if module.enabled()]


def not_configured(provider: str, hint: str) -> Invalid:
    """The §1.3 error a disabled provider produces."""
    return Invalid(
        "That single sign-on provider is not configured on this deployment.",
        hint=hint,
        context={"provider": provider},
    )


__all__ = [
    "Begin",
    "FederatedIdentity",
    "check_group_allowlist",
    "decode_token_response",
    "enabled_providers",
    "exchange_authorization_code",
    "generate_pkce",
    "get",
    "not_configured",
    "parse_allowed_groups",
    "parse_groups",
    "providers",
    "read_field",
    "tls_context",
    "tls_verify",
]
