"""
What each identity provider's configuration *is*, and where this deployment's
copy of it comes from.

ADR-0011. Until it, every provider was environment configuration read straight
off :data:`app.config.settings` at ~76 call sites across five modules, and
§12.4 refused to make that editable because "several providers of the same kind"
is a genuine design change. That refusal is narrowed here rather than dropped:
**one row per kind**, editable from the console, with the environment kept as
the fallback. The collision story §12.4 named — two issuers asserting the same
subject onto one account — cannot arise, because there is still only ever one
OIDC issuer, one directory, one SAML provider.

## The resolution rule, which is the whole module

For each kind, in order:

1. a row in ``identity_providers`` — including a row with ``enabled = false``,
   which means *this deployment has decided that kind is off*;
2. otherwise the ``LDAP_*`` / ``OIDC_*`` / … environment settings.

A row wins over the environment even when it is disabled, and deleting the row
falls back to the environment rather than to "off". Both halves matter: an
operator who switches a provider off in the console must not have it switched
back on by a variable in a Compose file they have never read, and an operator
who deletes a row must get the deployment's own configuration back rather than
silently losing the only way in. Every read reports which source answered,
because "why is this enabled" has two possible answers and the console is the
only thing that can tell them apart.

## Why one spec instead of five dataclasses

The 76 fields are declared once, here, and three things read that declaration:
the environment fallback (``{kind}_{name}`` is the settings attribute, so no
field has to name its own variable and none can name it wrongly), the console's
edit form (rendered from :func:`schema`), and the write validation. Five
hand-written dataclasses plus a separate form schema would be two lists of 76
things that have to agree, and the failure when they drift is the kind this
project cares about: a field an administrator can edit and nothing reads, or a
field the flow needs and the form never shows.

:class:`ProviderConfig` resolves attribute access through the spec, so provider
modules read ``cfg.issuer`` where they used to read ``settings.oidc_issuer``,
and an undeclared name raises ``AttributeError`` at the call rather than
evaluating to ``None``. That distinction is deliberate: a misspelled field that
reads as ``None`` is a verification step that silently stops happening.

**Secrets never leave this layer as values.** :class:`ProviderConfig` carries
them because the flow needs them; :func:`schema` and every API response report
only *whether* one is stored. See :mod:`app.identity.provider_store` for the
encryption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.config import settings
from app.errors import Invalid

logger = logging.getLogger(__name__)

#: Where a resolved configuration came from. Reported on every read, because
#: "this provider is enabled and I cannot find where that was decided" is the
#: question the field exists to answer.
SOURCE_DATABASE = "database"
SOURCE_ENVIRONMENT = "environment"


@dataclass(frozen=True)
class FieldSpec:
    """One configurable value, and everything its three readers need to know."""

    name: str
    #: ``str``, ``bool``, ``int``, ``float``, or ``text`` — a multi-line string
    #: rendered as a textarea. ``text`` is a display distinction only; the
    #: stored value is a string either way.
    type: str = "str"
    #: Stored encrypted and returned by nothing. See the module docstring.
    secret: bool = False
    #: Required for the flow to work at all. Enforced when a row is saved as
    #: enabled, so the console refuses a configuration that would otherwise
    #: fail at somebody else's first sign-in.
    required: bool = False
    #: Overrides the label derived from ``name``.
    label: str | None = None
    #: One sentence in the form. The house rule applies: say what goes wrong,
    #: not what the field is.
    help: str | None = None
    placeholder: str | None = None

    @property
    def title(self) -> str:
        return self.label or self.name.replace("_", " ").capitalize()


def _f(name: str, **kwargs: Any) -> FieldSpec:
    return FieldSpec(name, **kwargs)


@dataclass(frozen=True)
class ProviderSpec:
    """One kind of identity provider: its fields, and how to describe a row of it."""

    kind: str
    #: What the console calls this kind. Not the button text — that is a field
    #: on four of the five, because a deployment relabels its OIDC button
    #: "Okta" and "OpenID Connect" is then the wrong answer to which one it is.
    title: str
    #: One sentence saying what this kind actually is, shown on its card.
    summary: str
    fields: tuple[FieldSpec, ...]
    #: The field shown as "points at". An address is what makes a row
    #: identifiable at a glance; a panel of rows without one is a panel nobody
    #: can safely act on.
    endpoint_field: str | None
    #: The field naming the group whose members get the console ``admin`` role
    #: — the answer to "why is this person an administrator", which the Users
    #: table raises and cannot give.
    admin_group_field: str | None
    #: The field holding the sign-in button's text, when the kind has one.
    label_field: str | None = None
    #: A precondition that is **not** a field of this provider. SAML's is the
    #: only one, and it is the one nobody guesses.
    caveat: str | None = None

    def field_map(self) -> dict[str, FieldSpec]:
        return {spec.name: spec for spec in self.fields}


_TLS_HELP = (
    "Turning verification off means the identity this console is about to trust "
    "was delivered over a channel anyone on the path can control."
)
_ALLOWED_GROUPS_HELP = (
    "Comma-separated. Empty means every account this provider authenticates may "
    "use the console. When it is set and the provider reports no group "
    "membership at all, the sign-in is refused rather than allowed — an "
    "allowlist that admitted everyone whenever the claim went missing would stop "
    "working exactly when the provider is misconfigured."
)
_ADMIN_GROUP_HELP = (
    "Members get the console admin role at every login. When the provider does "
    "not report membership, the stored role is kept rather than reset, so a "
    "directory that stops returning groups does not demote your administrators."
)

#: Every kind this console can authenticate with, in the order the console shows
#: them. ``local`` is deliberately not here: this console's own password table is
#: not a provider anybody configures, and it cannot be switched off —
#: ``ensure_bootstrap_admin`` refuses to start an authenticating deployment with
#: neither a local administrator nor a directory.
SPECS: dict[str, ProviderSpec] = {
    "ldap": ProviderSpec(
        kind="ldap",
        title="LDAP / Active Directory",
        summary=(
            "Search-and-bind against a directory: a service account finds one "
            "user DN, then the console binds as that DN with the submitted "
            "password."
        ),
        endpoint_field="url",
        admin_group_field="admin_group_dn",
        fields=(
            _f("url", required=True, label="Server URL",
               placeholder="ldaps://directory.example.com:636",
               help="Plain ldap:// is refused unless StartTLS is on: a bind "
                    "sends the operator's password, and this console will not "
                    "send one in clear text."),
            _f("start_tls", type="bool", label="StartTLS",
               help="Needed for an ldap:// URL. Ignored for ldaps://, which is "
                    "already encrypted."),
            _f("tls_validate", type="bool",
               label="Validate the server certificate", help=_TLS_HELP),
            _f("ca_certificate_file", label="CA certificate file",
               help="A path inside the console's container. Empty uses the "
                    "system trust store."),
            _f("bind_dn", label="Search account DN",
               placeholder="cn=console,ou=services,dc=example,dc=com",
               help="Empty searches anonymously, which most directories refuse."),
            _f("bind_password", secret=True, label="Search account password"),
            _f("user_search_base", required=True, label="User search base",
               placeholder="ou=people,dc=example,dc=com"),
            _f("user_search_filter", required=True, label="User search filter",
               placeholder="(sAMAccountName={username})",
               help="Must contain {username}. Active Directory uses "
                    "sAMAccountName; the default (uid) is an OpenLDAP "
                    "convention and finds nobody in AD."),
            _f("username_attribute", required=True, label="Username attribute",
               placeholder="sAMAccountName"),
            _f("display_name_attribute", label="Display name attribute",
               placeholder="displayName"),
            _f("email_attribute", label="Email attribute", placeholder="mail"),
            _f("admin_group_dn", label="Administrator group DN",
               placeholder="cn=console-admins,ou=groups,dc=example,dc=com",
               help=_ADMIN_GROUP_HELP),
            _f("connect_timeout_seconds", type="float",
               label="Connect timeout (seconds)"),
        ),
    ),
    "oidc": ProviderSpec(
        kind="oidc",
        title="OpenID Connect",
        summary=(
            "An OIDC issuer. Identity comes from a signed ID token, verified "
            "completely: signature, issuer, audience, expiry and nonce."
        ),
        endpoint_field="issuer",
        admin_group_field="admin_group",
        label_field="button_label",
        fields=(
            _f("issuer", required=True, label="Issuer URL",
               placeholder="https://idp.example.com/realms/main",
               help="Must be https, and must equal the `iss` claim in the "
                    "tokens it signs byte for byte — this console checks that."),
            _f("client_id", required=True, label="Client ID"),
            _f("client_secret", secret=True, label="Client secret"),
            _f("scopes", label="Scopes",
               placeholder="openid profile email groups"),
            _f("redirect_url", label="Redirect URL",
               help="Empty derives it from the request. Set it when a proxy "
                    "rewrites the host, because the value registered with the "
                    "issuer has to match exactly."),
            _f("username_claim", required=True, label="Username claim",
               placeholder="preferred_username"),
            _f("email_claim", label="Email claim", placeholder="email"),
            _f("display_name_claim", label="Display name claim",
               placeholder="name"),
            _f("groups_claim", label="Groups claim", placeholder="groups"),
            _f("admin_group", label="Administrator group",
               help=_ADMIN_GROUP_HELP),
            _f("allowed_groups", label="Permitted groups",
               help=_ALLOWED_GROUPS_HELP),
            _f("verify_tls", type="bool",
               label="Validate the issuer's certificate", help=_TLS_HELP),
            _f("ca_certificate_file", label="CA certificate file"),
            _f("timeout_seconds", type="float", label="Request timeout (seconds)"),
            _f("clock_skew_seconds", type="int",
               label="Permitted clock skew (seconds)"),
            _f("button_label", label="Sign-in button text",
               help="Two providers both labelled “Single sign-on” is a "
                    "choice nobody can make."),
        ),
    ),
    "oauth": ProviderSpec(
        kind="oauth",
        title="OAuth 2.0",
        summary=(
            "A plain OAuth 2.0 authorization server — GitHub, GitLab, a "
            "non-OIDC Keycloak client. Identity comes from a userinfo endpoint "
            "read with the access token, so the channel is the whole trust "
            "argument: there is no signature to check."
        ),
        endpoint_field="authorization_url",
        admin_group_field="admin_group",
        label_field="button_label",
        fields=(
            _f("authorization_url", required=True, label="Authorization URL",
               placeholder="https://github.com/login/oauth/authorize"),
            _f("token_url", required=True, label="Token URL",
               placeholder="https://github.com/login/oauth/access_token"),
            _f("userinfo_url", required=True, label="Userinfo URL",
               placeholder="https://api.github.com/user",
               help="Required. Without it the flow ends holding an opaque "
                    "string, and a session issued at that point would be a "
                    "sign-in on the strength of a successful HTTP call rather "
                    "than of any statement about who the person is."),
            _f("client_id", required=True, label="Client ID"),
            _f("client_secret", secret=True, label="Client secret"),
            _f("scopes", label="Scopes", placeholder="read:user user:email"),
            _f("redirect_url", label="Redirect URL"),
            _f("subject_field", required=True, label="Subject field",
               placeholder="id",
               help="The provider's own stable identifier for the person. The "
                    "account is bound to this rather than to the username, "
                    "because a username is a label a provider may re-issue to "
                    "somebody else. Dots descend into a nested document."),
            _f("username_field", required=True, label="Username field",
               placeholder="login"),
            _f("email_field", label="Email field", placeholder="email"),
            _f("display_name_field", label="Display name field",
               placeholder="name"),
            _f("groups_field", label="Groups field"),
            _f("admin_group", label="Administrator group",
               help=_ADMIN_GROUP_HELP),
            _f("allowed_groups", label="Permitted groups",
               help=_ALLOWED_GROUPS_HELP),
            _f("verify_tls", type="bool",
               label="Validate the server's certificate", help=_TLS_HELP),
            _f("ca_certificate_file", label="CA certificate file"),
            _f("timeout_seconds", type="float", label="Request timeout (seconds)"),
            _f("button_label", label="Sign-in button text"),
        ),
    ),
    "openshift": ProviderSpec(
        kind="openshift",
        title="OpenShift",
        summary=(
            "The cluster's own built-in OAuth server. Identity comes from "
            "`users/~` read from the cluster with the access token, so the "
            "person signing in is the person the cluster already knows."
        ),
        endpoint_field="api_url",
        admin_group_field="admin_group",
        label_field="button_label",
        fields=(
            _f("api_url", required=True, label="API server URL",
               placeholder="https://api.cluster.example.com:6443"),
            _f("client_id", required=True, label="OAuth client ID",
               placeholder="k8boss-admin",
               help="An OAuthClient object in the cluster, whose redirectURIs "
                    "must include this console's callback."),
            _f("client_secret", secret=True, label="OAuth client secret"),
            _f("scopes", label="Scopes", placeholder="user:info"),
            _f("redirect_url", label="Redirect URL"),
            _f("admin_group", label="Administrator group",
               help=_ADMIN_GROUP_HELP),
            _f("allowed_groups", label="Permitted groups",
               help=_ALLOWED_GROUPS_HELP),
            _f("verify_tls", type="bool",
               label="Validate the API server's certificate", help=_TLS_HELP),
            _f("ca_certificate_file", label="CA certificate file"),
            _f("timeout_seconds", type="float", label="Request timeout (seconds)"),
            _f("button_label", label="Sign-in button text"),
        ),
    ),
    "saml": ProviderSpec(
        kind="saml",
        title="SAML 2.0",
        summary=(
            "A SAML 2.0 identity provider. Identity is read out of the subtree "
            "whose signature was verified, never out of the document as posted "
            "— which is what defeats XML Signature Wrapping."
        ),
        endpoint_field="idp_sso_url",
        admin_group_field="admin_group",
        label_field="button_label",
        caveat=(
            "SAML also needs AUTH_COOKIE_SECURE, which is environment "
            "configuration and not a field here. The assertion arrives on a "
            "cross-site form POST, so the handshake cookie has to be "
            "SameSite=None, and every current browser discards one that is not "
            "also Secure. On a plain-HTTP deployment the button is withheld "
            "rather than offered: it would fail at “the sign-in did not "
            "match”, which reads as a broken identity provider."
        ),
        fields=(
            _f("idp_entity_id", label="IdP entity ID"),
            _f("idp_sso_url", required=True, label="IdP sign-on URL",
               placeholder="https://idp.example.com/sso/saml"),
            _f("idp_certificate", type="text", required=True,
               label="IdP signing certificate",
               placeholder="-----BEGIN CERTIFICATE-----",
               help="PEM. Every assertion's signature is checked against this. "
                    "It is public material, not a secret, so it is stored in "
                    "the clear like a cluster's CA."),
            _f("sp_entity_id", label="Console entity ID",
               help="What this console calls itself to the IdP. Published at "
                    "/api/auth/saml/metadata so nobody has to transcribe it."),
            _f("acs_url", label="Assertion consumer service URL"),
            _f("username_attribute", required=True,
               label="Username attribute"),
            _f("email_attribute", label="Email attribute"),
            _f("display_name_attribute", label="Display name attribute"),
            _f("groups_attribute", label="Groups attribute"),
            _f("admin_group", label="Administrator group",
               help=_ADMIN_GROUP_HELP),
            _f("allowed_groups", label="Permitted groups",
               help=_ALLOWED_GROUPS_HELP),
            _f("clock_skew_seconds", type="int",
               label="Permitted clock skew (seconds)"),
            _f("button_label", label="Sign-in button text"),
        ),
    ),
}

#: The kinds, in display order.
KINDS: tuple[str, ...] = tuple(SPECS)


def spec(kind: str) -> ProviderSpec:
    """The spec for a kind, or ``422 invalid`` for a name this console has none of."""
    try:
        return SPECS[kind]
    except KeyError:
        raise Invalid(
            f"{kind!r} is not an identity provider kind this console supports.",
            hint="One of: " + ", ".join(KINDS) + ".",
            context={"field": "kind"},
        ) from None


@dataclass(frozen=True)
class ProviderConfig:
    """One provider's resolved configuration, and where it came from.

    Attribute access goes through the kind's :class:`ProviderSpec`, so a
    provider module reads ``cfg.issuer`` where it used to read
    ``settings.oidc_issuer``. A name the spec does not declare raises
    ``AttributeError`` rather than returning ``None``.
    """

    kind: str
    source: str
    enabled: bool
    values: Mapping[str, Any] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        # Only reached for names that are not real attributes, so `kind`,
        # `source`, `enabled` and `values` never arrive here.
        values = object.__getattribute__(self, "values")
        if name in values:
            return values[name]
        kind = object.__getattribute__(self, "kind")
        raise AttributeError(
            f"{name!r} is not a configuration field of the {kind!r} identity "
            f"provider. Declared: {', '.join(sorted(values))}."
        )


def _coerce(field_spec: FieldSpec, raw: Any) -> Any:
    """One stored or submitted value, as the field's type.

    A value that cannot be coerced falls back to the type's zero and logs rather
    than raising, because this runs on **reads** as well as writes and a row
    that somehow holds a bad number must not make the login page unreachable.
    Writes go through :func:`validate` first, which does raise, so anything that
    reaches here malformed was not written by this console.
    """
    if field_spec.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)
    if field_spec.type in {"int", "float"}:
        try:
            return int(raw) if field_spec.type == "int" else float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring a non-numeric %r on an identity provider row.",
                field_spec.name,
            )
            return 0 if field_spec.type == "int" else 0.0
    if raw is None:
        return ""
    return str(raw)


def from_env(kind: str) -> ProviderConfig:
    """The deployment's environment configuration for one kind.

    The settings attribute is ``{kind}_{field}`` by construction, so no field
    declares its own variable name and none can declare it wrongly. A
    ``SecretStr`` is unwrapped here and nowhere else.
    """
    provider = spec(kind)
    values: dict[str, Any] = {}
    for field_spec in provider.fields:
        raw = getattr(settings, f"{kind}_{field_spec.name}", None)
        reveal = getattr(raw, "get_secret_value", None)
        values[field_spec.name] = _coerce(field_spec, reveal() if reveal else raw)
    return ProviderConfig(
        kind=kind,
        source=SOURCE_ENVIRONMENT,
        enabled=bool(getattr(settings, f"{kind}_enabled", False)),
        values=values,
    )


def from_row(
    kind: str,
    *,
    enabled: bool,
    config: Mapping[str, Any],
    secrets: Mapping[str, Any],
) -> ProviderConfig:
    """One stored row as a resolved configuration.

    A field the row does not carry falls back to the **environment's** value
    rather than to the type's zero. That is what makes editing one field in the
    console safe on a deployment already configured from variables: saving a row
    does not blank out every field the form did not send. It also means a field
    added by a future release keeps reading from the environment until somebody
    saves it, instead of silently becoming empty on upgrade.
    """
    provider = spec(kind)
    fallback = from_env(kind)
    values: dict[str, Any] = {}
    for field_spec in provider.fields:
        stored = secrets if field_spec.secret else config
        if field_spec.name in stored:
            values[field_spec.name] = _coerce(field_spec, stored[field_spec.name])
        else:
            values[field_spec.name] = fallback.values[field_spec.name]
    return ProviderConfig(
        kind=kind, source=SOURCE_DATABASE, enabled=bool(enabled), values=values
    )


def resolve(kind: str) -> ProviderConfig:
    """This deployment's configuration for one kind: the row, else the environment.

    **A failed database read falls back to the environment and logs**, rather
    than raising. This is on the path of every sign-in and of the public
    discovery endpoint, so raising would take the login page down over a table
    that many deployments do not even use — and the environment is a real
    configuration, not a guess. The administrator-facing listing (§12.8)
    deliberately does *not* swallow it: a configuration screen quietly showing
    environment values while the database is unreadable would be the wrong
    answer delivered confidently, in the one place somebody is about to act on
    it.
    """
    from sqlalchemy.exc import SQLAlchemyError

    from app.identity import provider_store

    try:
        row = provider_store.get(kind)
    except SQLAlchemyError:
        logger.warning(
            "Could not read the stored configuration for the %r identity "
            "provider; falling back to this deployment's environment settings, "
            "so a provider edited in the console may not be in effect.",
            kind, exc_info=True,
        )
        return from_env(kind)
    if row is None:
        return from_env(kind)
    return from_row(
        kind,
        enabled=row.enabled,
        config=row.config or {},
        secrets=provider_store.decrypt_secrets(row),
    )


def configure_hint(cfg: ProviderConfig, env_vars: str) -> str:
    """Where to go and change this, given where the configuration came from.

    A refusal that says "set OIDC_ISSUER and restart the console" is the wrong
    instruction on a deployment whose issuer was typed into the console: the
    variable is not what is in effect, editing it changes nothing, and the
    operator concludes the console ignores its own settings. The inverse is
    equally wrong — pointing at a screen on a deployment configured entirely
    from a Compose file sends somebody to edit a row that does not exist.

    So the hint follows :attr:`ProviderConfig.source`, which is the only thing
    that knows which of the two answered.
    """
    if cfg.source == SOURCE_DATABASE:
        return (
            f"Edit the {spec(cfg.kind).title} provider under Administration "
            "\u2192 Identity providers. This deployment's stored configuration is "
            f"what is in effect, not {env_vars}."
        )
    return f"Set {env_vars}, then restart the console."


def schema(kind: str) -> list[dict[str, Any]]:
    """The kind's fields, as the console's form renders them.

    Carries no values — only what each field is. The form is generated from this
    rather than hand-written per kind, which is what stops a field the flow
    reads from being missing on the screen that configures it.
    """
    return [
        {
            "name": field_spec.name,
            "label": field_spec.title,
            "type": field_spec.type,
            "secret": field_spec.secret,
            "required": field_spec.required,
            "help": field_spec.help,
            "placeholder": field_spec.placeholder,
        }
        for field_spec in spec(kind).fields
    ]


def validate(kind: str, values: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    """Coerce and check a submitted configuration, or raise ``Invalid``.

    Checked **at the write**, not at the next sign-in. A console that accepted a
    directory URL it will refuse to send credentials to, and reported that as
    saved, would have moved the failure onto somebody else's login attempt — and
    the operator who typed it would be long gone by the time it surfaced.

    ``required`` and the per-kind rules are enforced only for a row saved as
    **enabled**. Half-filling a provider and leaving it off is how an
    administrator stages a configuration, and refusing that would push them back
    into editing a file.
    """
    provider = spec(kind)
    declared = provider.field_map()
    unknown = sorted(set(values) - set(declared))
    if unknown:
        raise Invalid(
            "Not a configuration field of this identity provider: "
            + ", ".join(unknown),
            hint="Fields: " + ", ".join(declared) + ".",
            context={"field": unknown[0]},
        )

    cleaned: dict[str, Any] = {}
    for name, raw in values.items():
        field_spec = declared[name]
        value = _coerce(field_spec, raw)
        if isinstance(value, str):
            value = value.strip()
            # Bounded because every one of these lands in a column and comes
            # from a caller. The PEM certificate is the long one and fits.
            if len(value) > 8192:
                raise Invalid(
                    f"{field_spec.title} is too long.", context={"field": name}
                )
        cleaned[name] = value

    if enabled:
        for field_spec in provider.fields:
            if field_spec.required and not str(cleaned.get(field_spec.name, "")).strip():
                raise Invalid(
                    f"{field_spec.title} is required to enable {provider.title}.",
                    context={"field": field_spec.name},
                )
        _validate_kind(provider, cleaned)
    return cleaned


def _validate_kind(provider: ProviderSpec, values: Mapping[str, Any]) -> None:
    """The per-kind rules the flow would otherwise discover at sign-in.

    Every one mirrors a refusal that already exists further in. This is the same
    check, moved to the moment somebody can still fix it.
    """
    if provider.kind == "ldap":
        url = str(values.get("url", ""))
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme not in {"ldap", "ldaps"}:
            raise Invalid(
                "The server URL must be an ldap:// or ldaps:// URL.",
                context={"field": "url"},
            )
        if scheme == "ldap" and not values.get("start_tls"):
            # `app.identity.ldap` refuses this at bind time. Refusing it here as
            # well is the difference between "this cannot be saved, and here is
            # why" and a directory that looks configured until the first
            # operator tries to use it.
            raise Invalid(
                "LDAP credentials may not be sent in clear text. Use ldaps:// "
                "or enable StartTLS.",
                context={"field": "url"},
            )
        if "{username}" not in str(values.get("user_search_filter", "")):
            raise Invalid(
                "The user search filter must contain the {username} "
                "placeholder, or it matches the same entry for everybody.",
                context={"field": "user_search_filter"},
            )
    elif provider.kind == "oidc":
        if not str(values.get("issuer", "")).startswith("https://"):
            raise Invalid(
                "The issuer URL must be https. An ID token is worth no more "
                "than the channel that delivered the keys it was verified with.",
                context={"field": "issuer"},
            )
    elif provider.kind in {"oauth", "openshift"}:
        for name in ("authorization_url", "token_url", "userinfo_url", "api_url"):
            value = str(values.get(name, ""))
            if value and not value.startswith("https://"):
                raise Invalid(
                    f"{provider.field_map()[name].title} must be https. Neither "
                    "of these two providers has a signature to check, so the "
                    "channel is the whole trust argument.",
                    context={"field": name},
                )
