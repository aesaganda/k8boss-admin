"""
Which sign-in methods this deployment is configured with, as data.

§12.8. The Users page shows the accounts that exist; this answers the question
that page raises and cannot answer by itself — *how* can anybody sign in here,
and which group made this person an administrator. Both were previously only
visible by reading the container's environment, which is not where an operator
looking at a console user is.

**Read-only, and that is the feature rather than a shortcut.** Every provider is
configured from the environment (§12.4), one of each kind per deployment, and
the contract says out loud that several concurrent providers of the same kind is
a design change — a table, a CRUD surface, per-row encrypted secrets and a
subject-collision story across issuers — and not something this console
half-does. An Edit button here would be a button that cannot write: the values
live in the process environment, a console that offered to change them would be
offering to change a copy, and "saved" would be the confidently wrong answer
this project is built against. What the panel does instead is name the variable
to change, so the operator ends up in the right place.

**Not public.** Everything here is precisely what ``GET /api/auth/config``
withholds — an issuer, an API server address, a directory URL, the configured
groups — because that endpoint is the one unauthenticated route in the API and
returning them there would let anyone who can reach the console enumerate its
identity infrastructure. The route in front of this module requires the ``admin``
console role for the same reason §12.6 does.
"""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.identity import sso

#: Where each single sign-on provider points, per provider name.
#:
#: A mapping here rather than an ``endpoint()`` member on the provider modules:
#: the registry in :mod:`app.identity.sso` is the interface the *routes* drive,
#: and a sign-in flow does not need to know how to describe itself. Adding a
#: member for a panel would make every future provider implement a display
#: accessor before it could authenticate anybody.
#:
#: The cost is that a fifth provider can be registered and missing from here,
#: which would render as a provider pointing nowhere. ``tests/test_sessions.py``
#: asserts the two stay in step, so that is caught by a red test rather than by
#: an operator reading a hole.
_SSO_ENDPOINTS = {
    "oidc": lambda: settings.oidc_issuer,
    "oauth": lambda: settings.oauth_authorization_url,
    "openshift": lambda: settings.openshift_api_url,
    "saml": lambda: settings.saml_idp_sso_url,
}

#: The environment prefix that configures each method, quoted to the operator.
_SETTINGS_PREFIXES = {
    "local": "AUTH_",
    "ldap": "LDAP_",
    "oidc": "OIDC_",
    "oauth": "OAUTH_",
    "openshift": "OPENSHIFT_",
    "saml": "SAML_",
}


def _text(value: str | None) -> str | None:
    """A configured string, or ``None`` when nothing is set.

    Empty string is every one of these settings' default, and it means "not
    configured". Passing it through would render as a blank cell, which reads as
    a value the console failed to show rather than one nobody set.
    """
    text = (value or "").strip()
    return text or None


def sign_in_methods() -> list[dict[str, Any]]:
    """Every method this console can authenticate with, configured or not.

    The unconfigured ones are included deliberately. An administrator asking
    "can we use our SAML provider here" is asking about a method this build
    supports and this deployment has not set up, and a list that showed only what
    is switched on cannot tell that apart from a method the console does not have
    at all. ``enabled`` carries the difference, and a provider is enabled only
    when **every** value its flow needs is present — the same rule the login
    page's buttons follow, so the panel and the buttons cannot disagree.
    """
    rows: list[dict[str, Any]] = [
        {
            "name": "local",
            "label": "Local accounts",
            # Always available when the console authenticates at all, which it
            # is doing if this response is being served. It is the method that
            # cannot be switched off: `ensure_bootstrap_admin` refuses to start
            # an AUTH_ENABLED deployment with neither a local administrator nor
            # a directory.
            "enabled": True,
            "endpoint": None,
            "admin_group": None,
            "settings_prefix": _SETTINGS_PREFIXES["local"],
        },
        {
            "name": "ldap",
            "label": "LDAP / Active Directory",
            "enabled": bool(settings.ldap_enabled),
            "endpoint": _text(settings.ldap_url),
            "admin_group": _text(settings.ldap_admin_group_dn),
            "settings_prefix": _SETTINGS_PREFIXES["ldap"],
        },
    ]
    rows.extend(
        {
            "name": module.NAME,
            # The provider's own button text, so the panel names each method the
            # way the login page does. A deployment that relabelled its OIDC
            # button "Okta" is a deployment where "OpenID Connect" is the wrong
            # answer to which one this is.
            "label": module.label(),
            "enabled": module.enabled(),
            "endpoint": _text(_SSO_ENDPOINTS[module.NAME]())
            if module.NAME in _SSO_ENDPOINTS
            else None,
            "admin_group": _text(module.admin_group()),
            "settings_prefix": _SETTINGS_PREFIXES.get(
                module.NAME, f"{module.NAME.upper()}_"
            ),
        }
        for module in sso.providers().values()
    )
    return rows
