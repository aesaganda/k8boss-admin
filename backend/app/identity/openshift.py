"""
OpenShift's built-in OAuth server as a console identity provider.

Lets an operator sign in with the identity they already use for ``oc login`` —
the cluster's own OAuth server, fronting whatever identity providers the cluster
administrator configured (htpasswd, LDAP, GitHub, Keycloak, …). This console
never sees the underlying credential.

Ported from K8Boss, where the same flow exists as a database-configured
provider; the argument below is the same and the configuration is this
console's, from the environment. ``docs/adr-0002-lineage.md`` covers why the
two trees share no code.

## Why this is neither the OIDC provider nor the generic OAuth one

**Not OIDC.** OpenShift's built-in OAuth server is an OAuth 2.0 authorization
server, not an OpenID Connect provider, and three things differ — each of which
breaks the OIDC path outright:

* Metadata lives at ``/.well-known/oauth-authorization-server`` (RFC 8414) on
  the **API server**, not at ``/.well-known/openid-configuration`` on an issuer.
  OIDC discovery 404s.
* The token response carries **no ``id_token``** and the server publishes no
  JWKS. The access token is opaque, so there is no signature to verify and no
  claims to read.
* Scopes are OpenShift's own vocabulary (``user:info``, ``user:full``,
  ``role:<role>:<ns>``). ``openid profile email`` is rejected as unknown.

**Not the generic OAuth provider either**, although it could almost be
configured as one. Two reasons it is worth its own module rather than a
README paragraph telling operators to point ``OAUTH_*`` at a cluster:

* The endpoints are *derivable*. One setting — the API server URL — yields the
  authorization endpoint, the token endpoint and the userinfo read, because
  RFC 8414 metadata is served from it. Configuring three URLs by hand where one
  suffices is three chances to point half a flow at a different cluster.
* ``users/~`` is not a userinfo document. It is a Kubernetes object, and the
  fields worth having are in ``metadata`` — including the ``uid`` that makes the
  account binding survive a recycled username. A field-path configuration could
  express that and would leave every operator to work it out.

## Where identity comes from, and why an unsigned token is enough here

``GET /apis/user.openshift.io/v1/users/~`` with the freshly issued access token
returns the authenticated user's name, UID and group memberships.

The trust argument is not OIDC's and is worth stating rather than assuming: the
access token was minted moments ago by the cluster's own OAuth server, over a
TLS connection this console verified, and it is the **cluster** — not this
console — that resolves that token to a user. A token this console was fed
rather than issued gets 401 at ``users/~``. So a 200 there is the cluster
asserting this identity, which is the same authority an ID token signature
conveys, from the same party that will later authorize the impersonated calls.

That last clause is why this is the one provider besides OIDC whose sessions may
impersonate (ADR-0007): the username and groups are not an external issuer's
opinion about a cluster identity, they are the cluster's own record of one.

**The access token is used once and never stored.** It is spent on the single
``users/~`` read and then dropped; the console issues its own session from that
point. A stolen console database therefore yields no cluster credentials, and a
console logout has nothing cluster-side to revoke.

Client registration is either a cluster-scoped ``OAuthClient`` resource or a
ServiceAccount acting as an OAuth client — see the README.
"""

from __future__ import annotations

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

#: Registry identity. See :mod:`app.identity.sso`.
NAME = "openshift"
BINDING = "query"
CALLBACK_SUFFIX = "callback"

#: RFC 8414, served by the API server rather than by an issuer.
DISCOVERY_PATH = "/.well-known/oauth-authorization-server"

#: The virtual endpoint that returns the *calling* user's own ``User`` object.
USER_PATH = "/apis/user.openshift.io/v1/users/~"

#: How long the authorization-server metadata is trusted before it is fetched
#: again. A cluster's OAuth endpoints effectively never move; the cache exists so
#: that a brief API-server hiccup does not fail a sign-in that had no other
#: reason to fail.
_DISCOVERY_TTL_SECONDS = 900

_discovery_cache: dict[str, Any] | None = None
_discovery_fetched_at: float = 0.0


def enabled() -> bool:
    """True when this deployment has a usable OpenShift OAuth configuration.

    The API server URL and the client id are both required: either alone
    produces a button that cannot work, and a button that leads to an error
    reads as a broken console rather than an unconfigured one.
    """
    return bool(
        settings.openshift_enabled
        and settings.openshift_api_url.strip()
        and settings.openshift_client_id.strip()
    )


def require_enabled() -> None:
    if not enabled():
        raise sso.not_configured(
            NAME,
            "Set OPENSHIFT_ENABLED, OPENSHIFT_API_URL and OPENSHIFT_CLIENT_ID, "
            "then restart the console.",
        )


def label() -> str:
    return settings.openshift_button_label


def admin_group() -> str:
    return settings.openshift_admin_group


def configured_callback_url() -> str:
    return settings.openshift_redirect_url.strip()


def scopes() -> str:
    return settings.openshift_scopes.strip() or "user:info"


def api_url() -> str:
    return settings.openshift_api_url.strip().rstrip("/")


def _verify_tls() -> bool | str:
    return sso.tls_verify(
        verify=settings.openshift_verify_tls,
        ca_file=settings.openshift_ca_certificate_file,
        provider=NAME,
    )


def reset_discovery_cache() -> None:
    """Forget the cached authorization-server metadata.

    Called when the configuration changes and by tests. A cached document
    outliving a reconfigured API server would keep sending operators to the
    previous cluster's login page.
    """
    global _discovery_cache, _discovery_fetched_at
    _discovery_cache = None
    _discovery_fetched_at = 0.0


def discovery() -> dict[str, Any]:
    """The cluster's authorization-server metadata, cached briefly.

    A fetch failure is ``identity_provider_unavailable`` (502), never a sign-in
    rejection. "The cluster could not answer" and "your credentials were wrong"
    send an operator to two completely different places, and the second is a dead
    end when the first is true.
    """
    global _discovery_cache, _discovery_fetched_at
    if (
        _discovery_cache is not None
        and (time.monotonic() - _discovery_fetched_at) < _DISCOVERY_TTL_SECONDS
    ):
        return _discovery_cache

    base = api_url()
    url = f"{base}{DISCOVERY_PATH}"
    try:
        with httpx.Client(
            timeout=settings.openshift_timeout_seconds, verify=_verify_tls()
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            document = response.json()
    except Exception as exc:  # noqa: BLE001 - every transport failure means the same thing
        logger.error(
            "OpenShift OAuth discovery failed against %s: %s", url, type(exc).__name__,
        )
        raise IdentityProviderUnavailable(
            "The cluster's OAuth server configuration could not be read.",
            detail=f"{type(exc).__name__} fetching {url}",
            hint="Check OPENSHIFT_API_URL, the console's network path to the API "
                 "server, and OPENSHIFT_CA_CERTIFICATE_FILE if the cluster uses a "
                 "private CA. A 404 here usually means the URL points at the "
                 "console route rather than at the API server.",
            context={"provider": NAME, "api_url": base},
        ) from exc

    missing = [
        key
        for key in ("authorization_endpoint", "token_endpoint")
        if not document.get(key)
    ]
    if missing:
        # Reported as unavailable rather than cached and used. An incomplete
        # document would otherwise fail later, in the middle of the handshake,
        # where the error reads as "your sign-in is bad" instead of "this cluster
        # is not serving complete metadata".
        raise IdentityProviderUnavailable(
            "The cluster returned incomplete OAuth server metadata.",
            detail=f"missing: {', '.join(missing)}",
            context={"provider": NAME, "api_url": base},
        )

    _discovery_cache = document
    _discovery_fetched_at = time.monotonic()
    return document


def authorization_url(*, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Where to send the browser to begin the handshake.

    ``nonce`` is deliberately absent: it is an OIDC ID-token binding, there is no
    ID token here, and a parameter nothing will ever check is the kind of thing a
    later reader mistakes for a protection that is in force. ``state`` and PKCE
    are what bind the response to this sign-in.
    """
    document = discovery()
    params = {
        "response_type": "code",
        "client_id": settings.openshift_client_id.strip(),
        "redirect_uri": redirect_uri,
        "scope": scopes(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{document['authorization_endpoint']}?{urlencode(params)}"


def exchange_code(*, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
    """Trade the authorization code for an access token at the cluster."""
    document = discovery()
    return sso.exchange_authorization_code(
        token_url=document["token_endpoint"],
        client_id=settings.openshift_client_id.strip(),
        client_secret=settings.openshift_client_secret.get_secret_value(),
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        verify=_verify_tls(),
        timeout=settings.openshift_timeout_seconds,
        provider=NAME,
    )


def fetch_user(access_token: str) -> dict[str, Any]:
    """Read the authenticated user's own ``User`` object from the cluster."""
    url = f"{api_url()}{USER_PATH}"
    try:
        with httpx.Client(
            timeout=settings.openshift_timeout_seconds, verify=_verify_tls()
        ) as client:
            response = client.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
            )
    except Exception as exc:  # noqa: BLE001
        raise IdentityProviderUnavailable(
            "The cluster could not be reached to identify the account that just "
            "signed in.",
            detail=type(exc).__name__,
            context={"provider": NAME},
        ) from exc

    if response.status_code in (401, 403):
        # The token authenticated and cannot read its own user, which is almost
        # always a scope problem. Naming it beats a generic "sign-in failed" the
        # administrator has no way to act on.
        raise PermissionDenied(
            "The cluster refused to say who the account that just signed in is.",
            detail=f"users/~ returned HTTP {response.status_code}",
            hint=f"The OAuth client's token needs the 'user:info' scope; "
                 f"OPENSHIFT_SCOPES is currently {scopes()!r}.",
            context={"provider": NAME},
        )
    if response.status_code != 200:
        raise IdentityProviderUnavailable(
            "The cluster's users/~ endpoint did not answer.",
            detail=f"HTTP {response.status_code}",
            context={"provider": NAME},
        )
    document = response.json()
    if not isinstance(document, dict):
        raise IdentityProviderUnavailable(
            "The cluster's users/~ endpoint did not return an object.",
            context={"provider": NAME},
        )
    return document


def identity_from_user(user: dict[str, Any]) -> FederatedIdentity:
    """Build the console's identity from the cluster's ``User`` object."""
    meta = user.get("metadata") or {}
    username = str(meta.get("name") or "").strip()
    if not username:
        raise PermissionDenied(
            "The cluster returned a user object with no name.",
            context={"provider": NAME},
        )

    return FederatedIdentity(
        # metadata.uid is the stable per-cluster identity. A username can be
        # re-issued to a different person once the User object is deleted, so the
        # rebind guard in `provision_federated_user` keys on the uid and refuses
        # the second person rather than handing them the first person's console
        # role. Falling back to the username keeps a cluster that omits the uid
        # working, at that guard's expense — which is the right trade only
        # because every real cluster sets it.
        subject=str(meta.get("uid") or username),
        username=username,
        display_name=str(user.get("fullName") or "").strip() or None,
        # OpenShift's User object carries no email address — the cluster
        # genuinely does not know it. Leaving it unset is the honest answer;
        # synthesising user@cluster-domain would be a fabricated attribute that
        # every later reader takes for a verified one.
        email=None,
        # Includes the virtual groups OpenShift attaches to every OAuth login
        # (`system:authenticated`, `system:authenticated:oauth`) as well as real
        # Group memberships. Passed through unfiltered so that genuinely useful
        # cluster groups such as `system:cluster-admins` stay mappable — with the
        # footgun that listing a virtual group in OPENSHIFT_ALLOWED_GROUPS admits
        # every account the cluster authenticates. The README says so.
        #
        # `groups` absent from the object is `None`, not `()`: it means the
        # cluster did not tell us, and the role mapping must leave a stored role
        # alone rather than demote an administrator.
        groups=sso.parse_groups(user.get("groups")),
    )


def check_group_allowlist(identity: FederatedIdentity) -> None:
    sso.check_group_allowlist(
        identity,
        allowed=settings.openshift_allowed_groups,
        provider=NAME,
        source_hint=(
            "The cluster's user object carried no group list. Grant the OAuth "
            "client the 'user:info' scope, or clear OPENSHIFT_ALLOWED_GROUPS."
        ),
    )


def test_connection() -> dict[str, Any]:
    """Reach the cluster's OAuth metadata and report what will still bite later.

    Reaching the document proves the API server is there; it does not prove a
    sign-in will work. Both remaining failure modes surface at the *user's* first
    attempt, where nobody can act on them, so they are reported here — as
    warnings, because neither is fatal on every cluster and this check cannot
    tell which.
    """
    try:
        document = discovery()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": (
            f"Could not read {DISCOVERY_PATH} from the API server: "
            f"{type(exc).__name__}: {exc}"
        )}

    warnings: list[str] = []
    if "S256" not in (document.get("code_challenge_methods_supported") or []):
        warnings.append("the cluster does not advertise PKCE S256 support")
    advertised = document.get("scopes_supported") or []
    if advertised:
        # `role:<role>:<namespace>` scopes are parameterised and are never
        # enumerated in scopes_supported — checking them here would produce a
        # false warning on a correct configuration.
        unknown = [
            scope for scope in scopes().split()
            if scope not in advertised and not scope.startswith("role:")
        ]
        if unknown:
            warnings.append(
                f"scope(s) the cluster does not advertise: {', '.join(unknown)}"
            )

    message = f"Reached the OpenShift OAuth server at {document.get('issuer')!r}."
    if warnings:
        message += " Warning: " + "; ".join(warnings) + "."
    return {
        "ok": True,
        "message": message,
        "warnings": warnings,
        "authorization_endpoint": document["authorization_endpoint"],
    }


# --------------------------------------------------------------------------- #
# The registry interface (see app.identity.sso)
# --------------------------------------------------------------------------- #


def begin(*, callback_url: str, next_path: str) -> Begin:
    state = secrets.token_urlsafe(24)
    verifier, challenge = sso.generate_pkce()
    return Begin(
        url=authorization_url(
            redirect_uri=callback_url, state=state, code_challenge=challenge
        ),
        handshake=handshake_service.Handshake(
            provider=NAME,
            state=state,
            # No ID token, so no nonce. See `authorization_url`.
            nonce="",
            code_verifier=verifier,
            redirect_uri=callback_url,
            next_path=next_path,
        ),
    )


def complete(*, handshake, params) -> FederatedIdentity:
    """Spend the code, then ask the cluster who the resulting token belongs to."""
    tokens = exchange_code(
        code=params.get("code") or "",
        code_verifier=handshake.code_verifier,
        redirect_uri=handshake.redirect_uri,
    )
    access_token = tokens.get("access_token")
    if not access_token:
        raise PermissionDenied(
            "The cluster's OAuth server returned no access token, so nothing "
            "could be read about who signed in.",
            context={"provider": NAME},
        )
    identity = identity_from_user(fetch_user(str(access_token)))
    check_group_allowlist(identity)
    return identity


__all__ = [
    "BINDING",
    "CALLBACK_SUFFIX",
    "DISCOVERY_PATH",
    "NAME",
    "USER_PATH",
    "admin_group",
    "api_url",
    "authorization_url",
    "begin",
    "check_group_allowlist",
    "complete",
    "configured_callback_url",
    "discovery",
    "enabled",
    "exchange_code",
    "fetch_user",
    "identity_from_user",
    "label",
    "require_enabled",
    "reset_discovery_cache",
    "scopes",
    "test_connection",
]
