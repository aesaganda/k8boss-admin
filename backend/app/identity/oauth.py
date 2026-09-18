"""
Generic OAuth 2.0 single sign-on: Authorization Code flow with PKCE, no ID token.

For an authorization server that is **not** an OpenID Connect provider — GitHub,
GitLab, Gitea, Bitbucket, a Keycloak client with the OIDC protocol turned off,
an internal server that speaks RFC 6749 and nothing more. Those servers issue an
access token and no ``id_token``, publish no JWKS, and mostly publish no
discovery document either, so :mod:`app.identity.oidc` cannot be pointed at
them: its discovery fetch 404s, and if it somehow got past that it would give up
at "the identity provider returned no ID token".

## The trust argument, which is genuinely different

OIDC's identity is a **signed assertion**: this console verifies a signature,
an issuer, an audience, an expiry and a nonce, and only then reads a claim.
There is no signature here, so that whole apparatus has nothing to work on.

What replaces it is the round trip itself. The console exchanges a code it
obtained under PKCE for an access token at the configured token endpoint, over a
TLS connection it verified, and then spends that token on a single read of the
configured userinfo endpoint — again over a verified TLS connection to a host
the operator named. A 200 there is the *provider* resolving that token to a
person. An attacker who feeds this console a token they minted gets 401 from
userinfo; one who intercepts the authorization code without the PKCE verifier
cannot exchange it.

That is a real argument and it is weaker than OIDC's in one specific way, worth
stating rather than glossing: **the identity is only as good as the TLS
verification of the userinfo call.** With a signed ID token, a compromised
channel still cannot forge an assertion. Here it can. So
``OAUTH_VERIFY_TLS=false`` is not an inconvenience switch on this provider — it
removes the only thing standing between the console and an attacker-chosen
identity — and it is logged at WARNING on every call that uses it. Prefer
``OAUTH_CA_CERTIFICATE_FILE`` for a private CA.

## No nonce, and why that is not a gap

``nonce`` binds an *ID token* to one browser's sign-in attempt. There is no ID
token to bind, and a nonce sent to a server that will not echo it back is
decoration. What actually protects this flow against a response replayed from
somewhere else is the same pair OIDC also relies on: ``state``, checked against
the sealed handshake cookie, and PKCE, whose verifier never leaves this server.

## No discovery, and why that is not the OIDC decision reversed

:mod:`app.identity.oidc` reaches every endpoint through the issuer's discovery
document specifically so an operator cannot point the authorization endpoint at
one deployment and the token endpoint at another. That protection is unavailable
here — a bare OAuth 2.0 server is not required to publish metadata anywhere, and
RFC 8414 is served by a minority of them — so the endpoints are configured
individually and the mismatch OIDC prevents is one an administrator has to avoid
themselves. It is named in the README rather than left to be discovered.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from app.errors import IdentityProviderUnavailable, PermissionDenied
from app.identity import handshake as handshake_service
from app.identity import sso
from app.identity.sso import Begin, FederatedIdentity
from app.identity import provider_config

logger = logging.getLogger(__name__)

#: Registry identity. See :mod:`app.identity.sso`.
NAME = "oauth"
BINDING = "query"
CALLBACK_SUFFIX = "callback"

def _cfg() -> provider_config.ProviderConfig:
    """This deployment's oauth configuration: the stored row, else the environment.

    Resolved per call rather than held, because a cached provider configuration
    outlives the edit that changed it — see :mod:`app.identity.provider_store`
    on why nothing there is cached.
    """
    return provider_config.resolve(NAME)



def enabled() -> bool:
    """True when this deployment has a usable OAuth 2.0 configuration.

    All four of authorization URL, token URL, userinfo URL and client id are
    required. Any one of them missing produces a button that cannot complete,
    and a button that leads to an error reads as a broken console rather than an
    unconfigured one.

    The userinfo URL is in that list and it is the one worth defending: without
    it the flow ends holding an opaque string, and a console that issued a
    session at that point would be signing people in on the strength of a
    successful HTTP call rather than on any statement about who they are.
    """
    cfg = _cfg()
    return bool(
        cfg.enabled
        and cfg.authorization_url.strip()
        and cfg.token_url.strip()
        and cfg.userinfo_url.strip()
        and cfg.client_id.strip()
    )


def require_enabled() -> None:
    if not enabled():
        raise sso.not_configured(
            NAME,
            provider_config.configure_hint(
                _cfg(),
                "OAUTH_ENABLED, OAUTH_AUTHORIZATION_URL, OAUTH_TOKEN_URL, "
                "OAUTH_USERINFO_URL and OAUTH_CLIENT_ID",
            ),
        )


def label() -> str:
    cfg = _cfg()
    return cfg.button_label


def admin_group() -> str:
    cfg = _cfg()
    return cfg.admin_group


def configured_callback_url() -> str:
    cfg = _cfg()
    return cfg.redirect_url.strip()


def _verify_tls() -> bool | str:
    cfg = _cfg()
    return sso.tls_verify(
        verify=cfg.verify_tls,
        ca_file=cfg.ca_certificate_file,
        provider=NAME,
    )


def generate_pkce() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` for PKCE S256. See
    :func:`app.identity.sso.generate_pkce`."""
    return sso.generate_pkce()


def authorization_url(*, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Where to send the browser to begin the handshake.

    ``scope`` is omitted entirely when none is configured rather than sent
    empty: several servers reject ``scope=`` as a malformed parameter, and the
    error page they produce names the parameter without saying it was empty.
    """
    cfg = _cfg()
    params = {
        "response_type": "code",
        "client_id": cfg.client_id.strip(),
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    scopes = cfg.scopes.strip()
    if scopes:
        params["scope"] = scopes
    return f"{cfg.authorization_url.strip()}?{urlencode(params)}"


def exchange_code(*, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Trade the authorization code for an access token.

    Most servers this provider exists for require a client secret, which is why
    it is prominent in the README even though it is optional here.
    """
    cfg = _cfg()
    return sso.exchange_authorization_code(
        token_url=cfg.token_url.strip(),
        client_id=cfg.client_id.strip(),
        client_secret=cfg.client_secret,
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        verify=_verify_tls(),
        timeout=cfg.timeout_seconds,
        provider=NAME,
    )


def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Read the signed-in person's profile with the freshly minted token.

    This single call is the entire identity assertion of this provider — see the
    module docstring. It is made with the token as a bearer credential and its
    result is never cached: a cached profile would outlive the account being
    disabled at the provider.
    """
    cfg = _cfg()
    try:
        with httpx.Client(
            timeout=cfg.timeout_seconds, verify=_verify_tls()
        ) as client:
            response = client.get(
                cfg.userinfo_url.strip(),
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise IdentityProviderUnavailable(
            "The OAuth provider's userinfo endpoint could not be reached.",
            detail=type(exc).__name__,
            context={"provider": NAME},
        ) from exc

    if response.status_code in (401, 403):
        # The token was issued and cannot read the profile it was issued for,
        # which is almost always a missing scope. Naming that beats a generic
        # "sign-in failed" an administrator has no way to act on.
        raise PermissionDenied(
            "The OAuth provider would not return a profile for the account that "
            "just signed in.",
            detail=f"userinfo returned HTTP {response.status_code}",
            hint="The token is missing the scope the userinfo endpoint requires. "
                 f"OAUTH_SCOPES is currently {cfg.scopes.strip()!r}.",
            context={"provider": NAME},
        )
    if response.status_code != 200:
        raise IdentityProviderUnavailable(
            "The OAuth provider's userinfo endpoint did not answer.",
            detail=f"HTTP {response.status_code}",
            context={"provider": NAME},
        )

    try:
        document = response.json()
    except ValueError as exc:
        raise IdentityProviderUnavailable(
            "The OAuth provider's userinfo endpoint returned a body this console "
            "could not read.",
            context={"provider": NAME},
        ) from exc
    if not isinstance(document, dict):
        raise IdentityProviderUnavailable(
            "The OAuth provider's userinfo endpoint did not return an object.",
            context={"provider": NAME},
        )
    return document


def identity_from_userinfo(document: dict[str, Any]) -> FederatedIdentity:
    """Reduce a userinfo document to the identity this console stores.

    Every field is named by configuration, because there is no standard for this
    document outside OpenID Connect: GitHub calls the subject ``id`` and the
    username ``login``, GitLab calls them ``id`` and ``username``, Keycloak
    without OIDC still emits ``sub`` and ``preferred_username``.

    **The subject is required and is not allowed to fall back to the username.**
    It is what :func:`app.identity.service.provision_federated_user` binds the
    account to, and a provider that recycles usernames — most of them, once an
    account is deleted — would otherwise hand the second holder of a name
    whatever role the first one had. If the configured field is absent the
    sign-in is refused, which is a configuration error an administrator can fix
    in one place, rather than a binding that silently is not one.
    """
    cfg = _cfg()
    subject = sso.read_field(document, cfg.subject_field)
    subject = str(subject).strip() if subject is not None else ""
    if not subject:
        raise PermissionDenied(
            "The OAuth provider's profile carried no stable identifier for this "
            "account.",
            hint=f"OAUTH_SUBJECT_FIELD is {cfg.subject_field!r} and the "
                 "userinfo document does not contain it. Set it to the field this "
                 "provider uses — GitHub and GitLab both call it 'id'.",
            context={"provider": NAME},
        )

    username = sso.read_field(document, cfg.username_field)
    if username is None:
        # Falling back to the email and then to the subject, in that order: an
        # email is a name a person recognises in an audit row, and a numeric
        # subject is not — but a numeric subject is still better than refusing a
        # sign-in over a cosmetic field.
        username = sso.read_field(document, cfg.email_field) or subject

    email = sso.read_field(document, cfg.email_field)
    display_name = sso.read_field(document, cfg.display_name_field)

    return FederatedIdentity(
        subject=subject,
        username=str(username),
        display_name=str(display_name) if display_name else None,
        email=str(email) if email else None,
        groups=sso.parse_groups(sso.read_field(document, cfg.groups_field)),
    )


def check_group_allowlist(identity: FederatedIdentity) -> None:
    cfg = _cfg()
    sso.check_group_allowlist(
        identity,
        allowed=cfg.allowed_groups,
        provider=NAME,
        source_hint=(
            f"The provider's userinfo document has no "
            f"{cfg.groups_field!r} field. Point OAUTH_GROUPS_FIELD at "
            "the field it does use, request the scope that carries it, or clear "
            "OAUTH_ALLOWED_GROUPS."
        ),
    )


# --------------------------------------------------------------------------- #
# The registry interface (see app.identity.sso)
# --------------------------------------------------------------------------- #


def begin(*, callback_url: str, next_path: str) -> Begin:
    """Start the Authorization Code + PKCE handshake.

    ``nonce`` is sealed empty rather than minted. There is no ID token to bind
    it to, and a value carried through the handshake that nothing ever checks is
    the kind of thing a later reader mistakes for a protection that is in force.
    """
    state = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()
    return Begin(
        url=authorization_url(
            redirect_uri=callback_url, state=state, code_challenge=challenge
        ),
        handshake=handshake_service.Handshake(
            provider=NAME,
            state=state,
            nonce="",
            code_verifier=verifier,
            redirect_uri=callback_url,
            next_path=next_path,
        ),
    )


def complete(*, handshake, params) -> FederatedIdentity:
    """Exchange the code, then read the profile the token resolves to."""
    tokens = exchange_code(
        code=params.get("code") or "",
        code_verifier=handshake.code_verifier,
        redirect_uri=handshake.redirect_uri,
    )
    access_token = tokens.get("access_token")
    if not access_token:
        raise PermissionDenied(
            "The OAuth provider returned no access token, so nothing could be "
            "read about who signed in.",
            context={"provider": NAME},
        )
    identity = identity_from_userinfo(fetch_userinfo(str(access_token)))
    check_group_allowlist(identity)
    return identity


__all__ = [
    "BINDING",
    "CALLBACK_SUFFIX",
    "NAME",
    "admin_group",
    "authorization_url",
    "begin",
    "check_group_allowlist",
    "complete",
    "configured_callback_url",
    "enabled",
    "exchange_code",
    "fetch_userinfo",
    "generate_pkce",
    "identity_from_userinfo",
    "label",
    "require_enabled",
]
