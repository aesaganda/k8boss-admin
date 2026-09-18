"""
OpenID Connect single sign-on: Authorization Code flow with PKCE.

Works against any standards-compliant issuer — Keycloak, Entra ID, Okta,
Authentik, Google, Auth0 — by reading the issuer's
``.well-known/openid-configuration`` rather than by carrying per-vendor
knowledge. The ID token that comes back is verified against the issuer's JWKS
before a single claim in it is believed.

## What this module refuses to shortcut

An ID token is a bearer assertion about who somebody is. Every check below has a
specific attack it prevents, and skipping any of them produces a login flow that
works perfectly in a demo:

**Signature, against the issuer's published keys.** Without it the token is
`base64` and anybody can mint one. ``PyJWKClient`` fetches and caches the JWKS;
the key is selected by the token's own ``kid``.

**Algorithm allowlist.** ``jwt.decode`` is told which algorithms are acceptable.
Passing the token's own ``alg`` back to the verifier is the classic JWT
vulnerability: the attacker sets ``alg: none``, or sets ``HS256`` and signs with
the public RSA key the verifier is about to use as an HMAC secret.

**``iss`` and ``aud``.** A token minted for a *different* application by the same
issuer is a valid, correctly-signed token. Without an audience check it is
accepted here, so anyone who can obtain a token for any other client of the same
IdP can sign in to this console.

**``exp`` and ``iat``, required rather than optional.** A token with no expiry
that verifies is a permanent credential.

**``nonce``, against the value stashed at the start of the handshake.** This is
what binds the token to *this* browser's sign-in attempt rather than to a token
replayed from somewhere else.

**PKCE (S256).** The authorization code is useless to anyone who intercepts it
without the verifier, which never leaves this server's sealed cookie.

**``state``.** Checked against the same sealed cookie, which is what stops a
third party starting a login and getting the victim's browser to complete it.

## One issuer, from the environment

K8Boss stores identity providers as database rows with an admin CRUD surface.
This console configures LDAP from the environment, and a provider configured a
different way would mean two places to look when a login fails and two things to
get right in a Helm chart. So OIDC is environment-configured too, one issuer per
deployment — as are the three siblings this module sits beside
(:mod:`app.identity.oauth`, :mod:`app.identity.openshift`,
:mod:`app.identity.saml`), which are four *kinds* of provider and still one of
each.

Several concurrent issuers **of the same kind** remains a real feature and a real
design change — a table, a CRUD surface, encrypted per-row secrets, and a
subject-collision story across issuers — not a config key, and the honest thing
is to say the console does not do it rather than to half-build it. Two different
kinds do not collide in the same way: each carries its own ``auth_source`` on the
account row, so a username asserted by two of them is refused rather than merged
(see :func:`app.identity.service.provision_federated_user`).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings
from app.errors import IdentityProviderUnavailable, PermissionDenied
from app.identity import handshake as handshake_service
from app.identity import sso
from app.identity.sso import Begin, FederatedIdentity

logger = logging.getLogger(__name__)

#: Signature algorithms this console will accept on an ID token. Asymmetric only.
#:
#: HMAC families are deliberately absent. With an ``HS*`` algorithm the
#: verification key is a shared secret, and the standard confusion attack is to
#: present a token signed ``HS256`` using the issuer's *public* key as the secret
#: — which a verifier that takes the algorithm from the token itself will happily
#: accept. Naming the acceptable algorithms here rather than trusting the header
#: is what makes that impossible.
ALLOWED_ALGORITHMS: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")

#: How long a discovery document is trusted before it is fetched again. Issuers
#: rotate endpoints rarely; JWKS keys rotate more often and are cached separately
#: by PyJWKClient, which re-fetches on an unknown ``kid``.
_DISCOVERY_TTL_SECONDS = 900

_discovery_cache: dict[str, Any] | None = None
_discovery_fetched_at: float = 0.0

#: One JWKS client, reused across verifications.
#:
#: ``PyJWKClient`` caches keys **per instance**, so constructing one per call
#: means an HTTP round trip to the issuer on every single sign-in — and two when
#: a token carries an unknown ``kid``. Beyond the latency, it makes the console's
#: sign-in path fail whenever the issuer is briefly unreachable, for a document
#: that changes when keys rotate.
#:
#: Keyed by ``jwks_uri`` so that reconfiguring the issuer does not keep serving
#: the previous one's keys, which would let a decommissioned provider keep
#: minting valid logins.
_jwk_clients: dict[str, Any] = {}


#: What a verified assertion reduces to. The shared shape, aliased rather than
#: redefined: everything downstream of verification — account binding, role
#: mapping, the audit row — is identical for all four providers, and a private
#: copy here would be the thing that drifts when one of them grows a field.
#:
#: The name is retained because it reads correctly at the call sites in this
#: module, and because ``groups=None`` means the same thing here as everywhere
#: else: the issuer omitted the claim, which must not demote anybody.
OidcIdentity = FederatedIdentity

#: Registry identity. See :mod:`app.identity.sso` for the interface these four
#: module-level names and the four functions at the bottom of this file make up.
NAME = "oidc"
BINDING = "query"
CALLBACK_SUFFIX = "callback"


def enabled() -> bool:
    """True when this deployment has a usable OIDC configuration.

    Both the issuer and the client id are required, because either one alone
    produces a login button that cannot work. A button that leads to an error is
    worse than an absent button: the operator concludes SSO is broken rather than
    unconfigured.
    """
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer.strip()
        and settings.oidc_client_id.strip()
    )


def require_enabled() -> None:
    """Raise the §1.3 error a disabled provider should produce."""
    if not enabled():
        raise sso.not_configured(
            NAME,
            "Set OIDC_ENABLED, OIDC_ISSUER and OIDC_CLIENT_ID, then restart "
            "the console.",
        )


def label() -> str:
    return settings.oidc_button_label


def admin_group() -> str:
    return settings.oidc_admin_group


def configured_callback_url() -> str:
    return settings.oidc_redirect_url.strip()


def reset_discovery_cache() -> None:
    """Forget the cached discovery document and signing keys.

    Both, together: a stale JWKS client outliving a reconfigured issuer would
    keep accepting tokens signed by the provider that was just replaced.
    """
    global _discovery_cache, _discovery_fetched_at
    _discovery_cache = None
    _discovery_fetched_at = 0.0
    _jwk_clients.clear()


def _tls_context():
    """The TLS trust configuration as an ``SSLContext``, for ``PyJWKClient``.

    It fetches the JWKS with ``urllib``, which takes neither a CA path nor a
    bool, so without this the one fetch that decides *which key is trusted*
    silently ignores ``OIDC_CA_CERTIFICATE_FILE`` and ``OIDC_VERIFY_TLS``. The
    failure that produces: an issuer behind a private CA completes discovery
    (httpx honoured the bundle) and then fails at key retrieval, reported as
    "the provider's signing keys could not be read" — every sign-in failing on a
    deployment whose CA is configured correctly.
    """
    return sso.tls_context(
        verify=settings.oidc_verify_tls,
        ca_file=settings.oidc_ca_certificate_file,
    )


def _verify_tls() -> bool | str:
    """What to hand httpx as ``verify``. See :func:`app.identity.sso.tls_verify`."""
    return sso.tls_verify(
        verify=settings.oidc_verify_tls,
        ca_file=settings.oidc_ca_certificate_file,
        provider=NAME,
    )


def discovery() -> dict[str, Any]:
    """The issuer's OpenID configuration, cached for :data:`_DISCOVERY_TTL_SECONDS`.

    Reached through the issuer rather than through individually configured
    endpoint URLs, so an operator cannot half-configure the flow — pointing the
    authorization endpoint at one deployment and the token endpoint at another is
    a configuration that produces confusing failures deep in the handshake.

    A fetch failure is ``identity_provider_unavailable`` (502), never a login
    rejection. "The directory could not answer" and "your credentials were wrong"
    send an operator to two completely different places, and the second one is a
    dead end when the first is true.
    """
    global _discovery_cache, _discovery_fetched_at
    if (
        _discovery_cache is not None
        and (time.monotonic() - _discovery_fetched_at) < _DISCOVERY_TTL_SECONDS
    ):
        return _discovery_cache

    issuer = settings.oidc_issuer.strip().rstrip("/")
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with httpx.Client(
            timeout=settings.oidc_timeout_seconds, verify=_verify_tls()
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            document = response.json()
    except Exception as exc:  # noqa: BLE001 - every transport failure means the same thing here
        logger.error(
            "OIDC discovery failed against %s: %s", url, type(exc).__name__,
        )
        raise IdentityProviderUnavailable(
            "The single sign-on provider's configuration could not be read.",
            detail=f"{type(exc).__name__} fetching {url}",
            hint="Check OIDC_ISSUER, the console's network path to it, and "
                 "OIDC_CA_CERTIFICATE_FILE if the issuer uses a private CA.",
            context={"provider": "oidc", "issuer": issuer},
        ) from exc

    missing = [
        key
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "issuer")
        if not document.get(key)
    ]
    if missing:
        # Reported as unavailable rather than cached and used. A document missing
        # jwks_uri would otherwise fail later, inside token verification, where
        # the error reads as "your token is bad" instead of "your issuer is not
        # serving a complete configuration".
        raise IdentityProviderUnavailable(
            "The single sign-on provider returned an incomplete configuration.",
            detail=f"missing: {', '.join(missing)}",
            context={"provider": "oidc", "issuer": issuer},
        )

    # OpenID Connect Discovery requires the document's `issuer` to equal the
    # issuer it was fetched from. Checked rather than assumed, because this
    # document supplies `jwks_uri` and `token_endpoint` AND the `iss` value that
    # tokens are then validated against — so a document that names a different
    # issuer would have the console validating tokens as consistent with itself
    # rather than with the provider an operator configured.
    declared = str(document.get("issuer") or "").rstrip("/")
    if declared != issuer:
        raise IdentityProviderUnavailable(
            "The single sign-on provider's configuration names a different issuer.",
            detail=f"OIDC_ISSUER is {issuer!r}; the document declares {declared!r}",
            hint="Point OIDC_ISSUER at the issuer the provider publishes, exactly "
                 "as it appears in its discovery document.",
            context={"provider": "oidc", "issuer": issuer},
        )

    _discovery_cache = document
    _discovery_fetched_at = time.monotonic()
    return document


def generate_pkce() -> tuple[str, str]:
    """``(code_verifier, code_challenge)`` for PKCE S256.

    S256 rather than ``plain``: with ``plain`` the challenge *is* the verifier, so
    anyone who saw the authorization request can complete the exchange, which is
    the whole thing PKCE exists to stop.
    """
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(
    *, redirect_uri: str, state: str, nonce: str, code_challenge: str
) -> str:
    """Where to send the browser to begin the handshake."""
    document = discovery()
    params = {
        "response_type": "code",
        "client_id": settings.oidc_client_id.strip(),
        "redirect_uri": redirect_uri,
        "scope": settings.oidc_scopes.strip() or "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{document['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(*, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Trade the authorization code for tokens at the issuer's token endpoint.

    A confidential client sends its secret; a public client (no secret
    configured) relies on PKCE alone, which is the current recommendation for
    browser-driven flows and is why the secret is optional rather than required.
    """
    document = discovery()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.oidc_client_id.strip(),
        "code_verifier": code_verifier,
    }
    secret = settings.oidc_client_secret.get_secret_value()
    if secret:
        data["client_secret"] = secret

    try:
        with httpx.Client(
            timeout=settings.oidc_timeout_seconds, verify=_verify_tls()
        ) as client:
            response = client.post(document["token_endpoint"], data=data)
    except Exception as exc:  # noqa: BLE001
        raise IdentityProviderUnavailable(
            "The single sign-on provider could not be reached to complete the "
            "sign-in.",
            detail=type(exc).__name__,
            context={"provider": "oidc"},
        ) from exc

    if response.status_code != 200:
        # The body can echo the client secret back in an error description on
        # some issuers, so it is not propagated — only the status.
        logger.error("OIDC token exchange returned HTTP %s.", response.status_code)
        if response.status_code >= 500:
            # The issuer is unwell. Reporting this as a refusal would record an
            # outage in the audit trail as `denied` — the outcome reserved for a
            # credential rejection, and the one that looks like an attack — and
            # would tell the operator to check a client registration that is
            # fine. The distinction is the same one the LDAP path already makes.
            raise IdentityProviderUnavailable(
                "The single sign-on provider could not complete the sign-in.",
                detail=f"token endpoint returned HTTP {response.status_code}",
                hint="The issuer returned a server error. Nothing about the "
                     "account or this console's configuration is implied.",
                context={"provider": "oidc"},
            )
        raise PermissionDenied(
            "The single sign-on provider refused to complete the sign-in.",
            detail=f"token endpoint returned HTTP {response.status_code}",
            hint="Usually a mismatched redirect URI or client secret. Check the "
                 "client registration at the issuer.",
            context={"provider": "oidc"},
        )
    return response.json()


def verify_id_token(id_token: str, *, nonce: str | None) -> dict[str, Any]:
    """Validate the ID token completely, or raise. Returns its claims.

    See the module docstring for what each check prevents. Nothing in the token
    is read before this function returns.
    """
    import jwt  # imported here so the module loads where SSO is not configured

    document = discovery()
    jwks_uri = document["jwks_uri"]
    try:
        jwk_client = _jwk_clients.get(jwks_uri)
        if jwk_client is None:
            # ssl_context, not just a timeout: urllib (which PyJWKClient uses)
            # will otherwise fall back to the system trust store and ignore both
            # OIDC_CA_CERTIFICATE_FILE and OIDC_VERIFY_TLS. See _tls_context.
            jwk_client = jwt.PyJWKClient(
                jwks_uri,
                cache_keys=True,
                timeout=settings.oidc_timeout_seconds,
                ssl_context=_tls_context(),
            )
            _jwk_clients[jwks_uri] = jwk_client
        signing_key = jwk_client.get_signing_key_from_jwt(id_token)
    except Exception as exc:  # noqa: BLE001
        # Dropped from the cache so a transient failure does not leave a client
        # that has memoised nothing useful and will be reused forever.
        _jwk_clients.pop(jwks_uri, None)
        raise IdentityProviderUnavailable(
            "The single sign-on provider's signing keys could not be read.",
            detail=type(exc).__name__,
            hint="Check the console's network path to the issuer, and "
                 "OIDC_CA_CERTIFICATE_FILE if it uses a private CA.",
            context={"provider": "oidc"},
        ) from exc

    try:
        claims = jwt.decode(
            id_token,
            signing_key.key,
            # Never the token's own `alg`. See ALLOWED_ALGORITHMS.
            algorithms=list(ALLOWED_ALGORITHMS),
            audience=settings.oidc_client_id.strip(),
            issuer=document["issuer"],
            options={
                "require": ["exp", "iat", "iss", "aud", "sub"],
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_signature": True,
            },
            leeway=settings.oidc_clock_skew_seconds,
        )
    except jwt.PyJWTError as exc:
        # A token that fails validation is a refusal, not a provider outage. The
        # exception type is logged and not returned: it distinguishes an expired
        # token from a bad signature, which is useful to an operator reading logs
        # and is an oracle to whoever is submitting the tokens.
        logger.warning("OIDC ID token rejected: %s", type(exc).__name__)
        raise PermissionDenied(
            "The single sign-on assertion could not be validated.",
            hint="If this persists, the console's clock or its registered client "
                 "id may not match the issuer's.",
            context={"provider": "oidc"},
        ) from exc

    # Checked after signature validation, never before: comparing a nonce out of
    # an unverified token tells an attacker whether they guessed it.
    expected = nonce or ""
    presented = str(claims.get("nonce") or "")
    # Compared as bytes. `compare_digest` refuses a `str` carrying any non-ASCII
    # character, and the TypeError would escape this refusal as a 500 instead of
    # the audited denial a replay is supposed to produce — the `nonce` claim is
    # the attacker's to write, so one non-ASCII character would buy them an
    # unrecorded server error in place of a recorded security event.
    if expected and not secrets.compare_digest(expected.encode(), presented.encode()):
        logger.warning("OIDC nonce mismatch; refusing the sign-in.")
        raise PermissionDenied(
            "The single sign-on assertion did not match this sign-in attempt.",
            hint="This is what a replayed assertion looks like. Start the "
                 "sign-in again.",
            context={"provider": "oidc"},
        )
    return claims


def _claim(claims: dict[str, Any], name: str) -> Any:
    return claims.get(name) if name else None


def identity_from_claims(claims: dict[str, Any]) -> OidcIdentity:
    """Reduce verified claims to the identity this console stores.

    The groups claim being **absent** and being **empty** are kept apart all the
    way through, because they mean different things to role mapping: an empty
    list is "this person is in no groups", and an absent claim is "this issuer
    did not tell us", which must not demote anyone. Most issuers omit the claim
    entirely unless the scope was requested and the client is configured to
    receive it, so the absent case is the common one during setup — exactly when
    an administrator would otherwise be quietly downgraded.
    """
    subject = str(claims.get("sub") or "").strip()
    if not subject:
        # Unreachable while `require: ["sub"]` holds, checked anyway: it is the
        # value the whole account binding hangs on, and an empty one would bind
        # every account to the same identity.
        raise PermissionDenied(
            "The single sign-on assertion carried no subject.",
            context={"provider": "oidc"},
        )

    username = (
        _claim(claims, settings.oidc_username_claim)
        or _claim(claims, "email")
        or subject
    )
    email = _claim(claims, settings.oidc_email_claim)
    display_name = _claim(claims, settings.oidc_display_name_claim) or None

    # Absent stays absent, and a bare string is split rather than wrapped: see
    # `sso.parse_groups`, which the other three providers use for the same
    # reason.
    groups = sso.parse_groups(_claim(claims, settings.oidc_groups_claim))

    return OidcIdentity(
        subject=subject,
        username=str(username),
        display_name=str(display_name) if display_name else None,
        email=str(email) if email else None,
        groups=groups,
    )


def check_group_allowlist(identity: OidcIdentity) -> None:
    """Refuse a verified identity that is not in a permitted group.

    ``OIDC_ALLOWED_GROUPS`` empty means every account the issuer authenticates
    may use the console, which is the right default for a deployment whose issuer
    already only knows the right people. When it is set and the groups claim is
    **absent**, the sign-in is refused — the opposite of the role-mapping rule,
    and for the reason :func:`app.identity.sso.check_group_allowlist` states.
    """
    sso.check_group_allowlist(
        identity,
        allowed=settings.oidc_allowed_groups,
        provider=NAME,
        source_hint=(
            f"The issuer did not include the {settings.oidc_groups_claim!r} "
            "claim. Add the claim to the client's token mapping, request the "
            "scope that carries it, or clear OIDC_ALLOWED_GROUPS."
        ),
    )


# --------------------------------------------------------------------------- #
# The registry interface (see app.identity.sso)
# --------------------------------------------------------------------------- #


def begin(*, callback_url: str, next_path: str) -> Begin:
    """Start the Authorization Code + PKCE handshake.

    The four values that have to survive the round trip — ``state``, ``nonce``,
    the PKCE verifier and the exact ``redirect_uri`` — are minted here and sealed
    by the caller. The redirect URI is sealed rather than recomputed at the
    callback because OAuth requires the two to be byte-identical, and a
    recomputed one differs the moment a forwarded header does, producing an
    ``invalid_grant`` that reads as a credential problem.
    """
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = generate_pkce()
    return Begin(
        url=authorization_url(
            redirect_uri=callback_url,
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        ),
        handshake=handshake_service.Handshake(
            provider=NAME,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            redirect_uri=callback_url,
            next_path=next_path,
        ),
    )


def complete(*, handshake, params) -> OidcIdentity:
    """Exchange the code and verify the ID token completely. See the docstrings
    on :func:`exchange_code` and :func:`verify_id_token` for what each check
    prevents; nothing in the token is read before all of them have passed."""
    tokens = exchange_code(
        code=params.get("code") or "",
        code_verifier=handshake.code_verifier,
        redirect_uri=handshake.redirect_uri,
    )
    id_token = tokens.get("id_token")
    if not id_token:
        # Not "the sign-in failed": the issuer completed the exchange and
        # asserted nothing about who it was for, which is a different fact and
        # the one an administrator has to act on.
        raise PermissionDenied(
            "The identity provider returned no ID token, so it asserted nothing "
            "about who signed in.",
            hint="The client is registered as a plain OAuth 2.0 client rather "
                 "than an OpenID Connect one, or the 'openid' scope is missing "
                 "from OIDC_SCOPES. A provider that has no ID token to give can "
                 "be configured as OAUTH_* instead.",
            context={"provider": NAME},
        )
    identity = identity_from_claims(verify_id_token(id_token, nonce=handshake.nonce))
    check_group_allowlist(identity)
    return identity


__all__ = [
    "ALLOWED_ALGORITHMS",
    "BINDING",
    "CALLBACK_SUFFIX",
    "NAME",
    "OidcIdentity",
    "admin_group",
    "authorization_url",
    "begin",
    "check_group_allowlist",
    "complete",
    "configured_callback_url",
    "discovery",
    "enabled",
    "exchange_code",
    "generate_pkce",
    "identity_from_claims",
    "label",
    "require_enabled",
    "reset_discovery_cache",
    "verify_id_token",
]
