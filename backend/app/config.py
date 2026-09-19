"""
Application configuration, loaded from environment variables.

Every setting has a safe default, and "safe" here means *read-only*:
``admin_allow_mutations`` and ``secret_reveal_enabled`` both default to False so
a console that is deployed without a deliberate decision cannot write to a
cluster or hand out Secret values. Turning either on is an act; leaving it alone
is not.

Credentials are never logged. ``encryption_key`` is read here and consumed by
``app.crypto``; nothing else in the process should touch it.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed settings.

    Field names map to upper-cased environment variables (``database_url`` <-
    ``DATABASE_URL``). The security-relevant gates additionally declare an
    explicit alias so a rename of the Python attribute can never silently change
    the name of the environment variable an operator has already set in a Helm
    values file — a rename that quietly re-enabled mutations would be exactly the
    confidently-wrong behaviour this console exists to avoid.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- identity ---------------------------------------------------------
    app_version: str = Field(
        default="0.1.0",
        description="Reported by GET /api/health. The UI shows it in the About panel.",
    )

    # -- storage ----------------------------------------------------------
    database_url: str = Field(
        default="sqlite:///./k8boss_admin.db",
        description=(
            "SQLAlchemy URL. SQLite for dev, PostgreSQL in cluster. The console "
            "stores only its own state here: registered clusters and the audit "
            "trail. Cluster contents are never cached in this database."
        ),
    )

    # -- credential encryption -------------------------------------------
    encryption_key: str = Field(
        default="",
        description=(
            "Secret used to derive the Fernet key that encrypts stored cluster "
            "tokens. Empty means app.crypto persists a generated key next to the "
            "database; see that module for why it must be persisted rather than "
            "regenerated per boot."
        ),
    )
    encryption_key_file: str = Field(
        default="",
        description=(
            "Override for the generated key file path. Empty means 'next to the "
            "SQLite database file', or ./k8boss_admin.key when the database is "
            "not SQLite."
        ),
    )

    # -- write gates ------------------------------------------------------
    admin_allow_mutations: bool = Field(
        default=False,
        validation_alias=AliasChoices("ADMIN_ALLOW_MUTATIONS", "admin_allow_mutations"),
        description=(
            "Master switch for every write to a cluster. False (the default) makes "
            "writes return 403 mutations_disabled BEFORE the cluster is touched, "
            "and GET /api/health reports mutations=disabled so the UI disables the "
            "buttons instead of offering them and failing. Dry-run stays permitted "
            "in read-only mode: inspecting what would change is a read, and that is "
            "what makes the console useful in an audit posture."
        ),
    )
    secret_reveal_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("SECRET_REVEAL_ENABLED", "secret_reveal_enabled"),
        description=(
            "Allows the single-object Secret read to return values when asked with "
            "?reveal=true. Separate from admin_allow_mutations because reading a "
            "Secret is a privileged act with a different blast radius from writing "
            "a Deployment, and an operator may reasonably want one without the "
            "other. Every reveal is audited regardless."
        ),
    )
    debug_image: str = Field(
        default="busybox:1.36",
        validation_alias=AliasChoices("ADMIN_DEBUG_IMAGE", "debug_image"),
        min_length=1,
        max_length=512,
        description=(
            "The image a debug (ephemeral) container is attached with when the "
            "operator does not name one. Not a gate — attaching one at all needs "
            "admin_allow_mutations and patch pods/ephemeralcontainers — but a "
            "default worth setting: an air-gapped cluster cannot pull "
            "busybox:1.36 from Docker Hub, and the failure it produces is an "
            "ImagePullBackOff on a pod somebody is already debugging."
        ),
    )
    node_debug_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ADMIN_NODE_DEBUG_ENABLED", "node_debug_enabled"),
        description=(
            "Allows a node debug pod: a pod pinned to one node with the host "
            "filesystem mounted and the host PID namespace shared. Its own gate, "
            "on top of admin_allow_mutations, for the same reason "
            "secret_reveal_enabled has one — this is a larger blast radius than "
            "the rest of the write surface put together, and an operator may "
            "reasonably want every other write without it. A shell in such a pod "
            "is root on the machine: it can read every Secret the kubelet has "
            "written to disk, edit static pod manifests, and see every process on "
            "the node. Leaving it off is not a restriction on what an operator "
            "may do, it is a decision about what this console can be used to do."
        ),
    )
    node_debug_namespace: str = Field(
        default="default",
        validation_alias=AliasChoices("ADMIN_NODE_DEBUG_NAMESPACE", "node_debug_namespace"),
        min_length=1,
        max_length=253,
        description=(
            "Namespace the node debug pod is created in. Configurable because the "
            "namespace decides which PodSecurity level admits it: a cluster that "
            "enforces `restricted` everywhere needs one namespace labelled "
            "`privileged` for this to be possible at all, and pinning the console "
            "to that namespace keeps the exception in one auditable place rather "
            "than wherever the operator's namespace selector happened to be."
        ),
    )

    node_debug_max_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("ADMIN_NODE_DEBUG_MAX_SECONDS", "node_debug_max_seconds"),
        ge=0,
        le=86400,
        description=(
            "Wall-clock limit on a node debug pod's container, as "
            "spec.activeDeadlineSeconds. 0 means unbounded. This is the one "
            "mitigation available for the hazard this feature cannot otherwise "
            "close: nothing deletes the pod when the operator walks away, so an "
            "unattended shell with the node's filesystem attached would otherwise "
            "run until somebody noticed. Be clear about what it does — the "
            "kubelet stops the *container* at the deadline and marks the pod "
            "Failed; the pod object stays and still has to be removed."
        ),
    )

    # -- the CLI pod (§15) -------------------------------------------------
    cli_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ADMIN_CLI_ENABLED", "cli_enabled"),
        description=(
            "Allows a CLI pod: a pod carrying kubectl (or oc) that the console "
            "opens a shell into, so an operator can run a command this console "
            "has no page for without leaving it for a laptop. Its own gate on "
            "top of admin_allow_mutations, because everything typed in that "
            "shell happens OUTSIDE the write funnel — no preflight naming the "
            "permission, no dry run, no diff, no resourceVersion check, and no "
            "audit record of what changed. The trail records that a shell was "
            "opened, by whom and for how long; it cannot record the kubectl "
            "delete typed into it. An operator may reasonably want every other "
            "write in this console without wanting that."
        ),
    )
    cli_namespace: str = Field(
        default="default",
        validation_alias=AliasChoices("ADMIN_CLI_NAMESPACE", "cli_namespace"),
        min_length=1,
        max_length=253,
        description=(
            "Namespace the CLI pod is created in, and where cli_service_account "
            "is looked for. Configurable because the namespace decides both "
            "which PodSecurity level admits the pod and which ServiceAccounts "
            "are bindable to it — keeping both in one place an administrator "
            "chose, rather than wherever the namespace selector happened to be."
        ),
    )
    cli_image: str = Field(
        default="alpine/k8s:1.34.9",
        validation_alias=AliasChoices("ADMIN_CLI_IMAGE", "cli_image"),
        min_length=1,
        max_length=512,
        description=(
            "Image the CLI pod runs. Two hard requirements, neither of which "
            "this console can check and neither of which it pretends to: kubectl "
            "(or oc) on the PATH, and a /bin/sh — the pod's command is a shell "
            "loop, because a kubectl image's own entrypoint IS kubectl and would "
            "exit immediately. An image missing either starts and then answers "
            "'command not found', which is at least legible. "
            "The default is Alpine-based, so its BusyBox sh and sleep both "
            "behave. Pick a kubectl within one minor version of the cluster — "
            "that is Kubernetes' own supported skew, and this default will drift "
            "out of it. Set an image carrying oc for OpenShift, and one in your "
            "own registry for an air-gapped cluster, where the default cannot be "
            "pulled and the failure is an ImagePullBackOff on a pod somebody is "
            "waiting for."
        ),
    )
    cli_service_account: str = Field(
        default="default",
        validation_alias=AliasChoices("ADMIN_CLI_SERVICE_ACCOUNT", "cli_service_account"),
        min_length=1,
        max_length=253,
        description=(
            "ServiceAccount the CLI pod binds, and therefore the identity every "
            "kubectl command typed in it runs as. This is the ONLY control over "
            "what that shell can do: Kubernetes has no RBAC verb covering which "
            "ServiceAccount a pod may bind, so a caller holding `create pods` in "
            "this namespace can bind an account far more privileged than "
            "themselves, and no ClusterRole can narrow it afterwards. The "
            "default is the namespace's own `default` account, which holds no "
            "permissions at all — kubectl in the pod is refused by the API "
            "server for everything until a cluster admin deliberately binds a "
            "Role. That is a feature that arrives useless rather than one that "
            "arrives dangerous."
        ),
    )
    cli_max_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices("ADMIN_CLI_MAX_SECONDS", "cli_max_seconds"),
        ge=0,
        le=86400,
        description=(
            "Wall-clock limit on the CLI pod's container, as "
            "spec.activeDeadlineSeconds. 0 means unbounded. Be clear about what "
            "it does: the kubelet stops the container at the deadline and marks "
            "the pod Failed; the pod object stays and still has to be removed. "
            "It bounds the window in which an unattended shell with a cluster "
            "credential is possible, and does not clean up after itself."
        ),
    )

    # -- the shipped router (§14) ------------------------------------------
    router_manage_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("ADMIN_ROUTER_MANAGE_ENABLED", "router_manage_enabled"),
        description=(
            "Allows this console to install, upgrade and remove the HAProxy "
            "ingress controller it ships (§14). Its own gate on top of "
            "admin_allow_mutations, and the reason is the size of what an "
            "install creates: a cluster-scoped ClusterRole that can read every "
            "Secret in the cluster — which is what any ingress controller needs "
            "to terminate TLS, and which RBAC cannot narrow — plus a process on "
            "the cluster's ingress path. An operator may reasonably want every "
            "other write in this console without wanting it to put a proxy on "
            "their cluster. Off does not hide the feature: the install plan and "
            "its diff still render, because reading what would be created is a "
            "read, and deciding whether to turn this on requires it."
        ),
    )
    router_namespace: str = Field(
        default="k8boss-router",
        validation_alias=AliasChoices("ADMIN_ROUTER_NAMESPACE", "router_namespace"),
        min_length=1,
        max_length=253,
        description=(
            "Default namespace the shipped router is installed into, and where "
            "status looks when the cluster carries no installation to discover "
            "it from. Only a default: the namespace an installed router actually "
            "lives in is read back off its ClusterRoleBinding, so this setting "
            "changing does not make the console lose track of a router that is "
            "already running."
        ),
    )

    # -- the operator portal (§16) -----------------------------------------
    portal_install_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ADMIN_PORTAL_INSTALL_ENABLED", "portal_install_enabled"
        ),
        description=(
            "Allows this console to create Operator Lifecycle Manager "
            "Subscriptions from the operator portal (§16). Its own gate on top "
            "of admin_allow_mutations, and the reason is what a Subscription "
            "hands over: OLM installs software this console did not review, "
            "under whatever RBAC that operator's ClusterServiceVersion asks for "
            "— routinely cluster-wide, and granted by OLM rather than by the "
            "caller. An operator may reasonably want every other write in this "
            "console without wanting it to be the place third-party software "
            "enters their cluster. Off does not hide the portal: the catalog, "
            "the rendered Subscription and its diff all still render, because "
            "reading what would be created is a read, and deciding whether to "
            "turn this on requires it."
        ),
    )

    # -- installing Operator Lifecycle Manager (§33) -----------------------
    olm_install_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "ADMIN_OLM_INSTALL_ENABLED", "olm_install_enabled"
        ),
        description=(
            "Allows this console to install Operator Lifecycle Manager itself "
            "(§33) onto a cluster that does not run it — the second and last "
            "bundle this console ships, on the terms "
            "docs/adr-0008-shipped-olm.md records. Its own gate on top of "
            "admin_allow_mutations, separate from portal_install_enabled, "
            "because the two are different sizes of decision: subscribing "
            "writes one object into an API the cluster already serves, while "
            "this creates the API — eight CustomResourceDefinitions, two "
            "controllers, and a ClusterRole granting every verb on every "
            "resource in the cluster including escalate and bind, which is the "
            "widest grant this product has ever created. A deployment may "
            "reasonably want the portal without wanting the console to be able "
            "to install a cluster's operator control plane. Off does not hide "
            "it: the plan, the manifests and the diff all still render, "
            "because reading what would be created is a read, and deciding "
            "whether to turn this on requires it."
        ),
    )
    olm_establish_timeout_seconds: float = Field(
        default=90.0,
        validation_alias=AliasChoices(
            "ADMIN_OLM_ESTABLISH_TIMEOUT_SECONDS", "olm_establish_timeout_seconds"
        ),
        ge=1.0,
        le=600.0,
        description=(
            "How long an install waits for the eight CustomResourceDefinitions "
            "it just created to report Established before giving up (§33). Not "
            "a reconcile loop: one bounded wait inside one request, after which "
            "the install stops and reports what it did rather than writing "
            "phase two into APIs that do not exist yet. The default is generous "
            "— establishment is normally sub-second — because the cost of "
            "waiting too long is a slow request and the cost of not waiting "
            "long enough is eighteen misleading 404s."
        ),
    )

    # -- console authentication ------------------------------------------
    auth_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("AUTH_ENABLED", "auth_enabled"),
        description=(
            "Require a managed local or LDAP identity for every API request. "
            "False preserves the legacy authenticating-proxy deployment model."
        ),
    )
    auth_session_ttl_hours: int = Field(default=12, ge=1, le=168)
    auth_cookie_name: str = Field(default="k8boss_admin_session", min_length=1, max_length=64)
    auth_cookie_secure: bool = Field(
        default=False,
        description="Mark the session cookie Secure. Enable for every HTTPS deployment.",
    )
    auth_bootstrap_username: str = Field(default="", max_length=255)
    auth_bootstrap_password: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Initial local administrator password. Used only when the users table is empty; "
            "it never overwrites an existing account."
        ),
    )

    # LDAP is an alternate login provider. Authorization remains local: a
    # configured LDAP group maps its members to the admin role, and every other
    # successful LDAP login maps to user.
    ldap_enabled: bool = Field(default=False)
    ldap_url: str = Field(default="", max_length=2048)
    ldap_start_tls: bool = Field(default=False)
    ldap_tls_validate: bool = Field(default=True)
    ldap_ca_certificate_file: str = Field(default="", max_length=2048)
    ldap_bind_dn: str = Field(default="", max_length=1024)
    ldap_bind_password: SecretStr = Field(default=SecretStr(""))
    ldap_user_search_base: str = Field(default="", max_length=1024)
    ldap_user_search_filter: str = Field(default="(uid={username})", max_length=1024)
    ldap_username_attribute: str = Field(default="uid", max_length=128)
    ldap_display_name_attribute: str = Field(default="cn", max_length=128)
    ldap_email_attribute: str = Field(default="mail", max_length=128)
    ldap_admin_group_dn: str = Field(
        default="",
        max_length=1024,
        description=(
            "Members of this group DN get the admin role. Applied only when the "
            "directory actually returns the membership attribute: when it does "
            "not, the stored role is kept rather than reset, because an absent "
            "attribute is 'we could not look', not 'in no groups', and treating "
            "them alike silently demoted directory administrators."
        ),
    )
    ldap_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # -- sign-in rate limiting -------------------------------------------
    # Counted from the audit trail rather than from process memory, so the limit
    # holds across replicas and survives a restart. See app.identity.throttle.
    auth_throttle_max_attempts: int = Field(
        default=10,
        ge=0,
        le=1000,
        validation_alias=AliasChoices(
            "AUTH_THROTTLE_MAX_ATTEMPTS", "auth_throttle_max_attempts"
        ),
        description=(
            "Failed sign-ins permitted per username per window before the "
            "console answers 429 too_many_attempts. 0 disables the throttle, "
            "which leaves POST /api/auth/login an unmetered password oracle."
        ),
    )
    auth_throttle_window_seconds: int = Field(
        default=300,
        ge=10,
        le=86_400,
        description="Length of the sign-in throttle window, in seconds.",
    )

    # -- OpenID Connect single sign-on -----------------------------------
    #
    # One issuer per deployment, configured from the environment exactly as LDAP
    # is. Multiple concurrent issuers is a real design change — a table, a CRUD
    # surface, per-row encrypted secrets and a subject-collision story across
    # issuers — not a config key, so the console says it does not do that rather
    # than half-doing it.
    oidc_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OIDC_ENABLED", "oidc_enabled"),
        description=(
            "Offer OpenID Connect single sign-on. Requires AUTH_ENABLED, plus "
            "OIDC_ISSUER and OIDC_CLIENT_ID; without all three the login page "
            "shows no SSO button, because a button that cannot work reads as a "
            "broken console rather than an unconfigured one."
        ),
    )
    oidc_issuer: str = Field(
        default="",
        max_length=2048,
        description=(
            "Issuer URL. Its /.well-known/openid-configuration supplies every "
            "endpoint, so the flow cannot be half-configured across two "
            "deployments of the same provider."
        ),
    )
    oidc_client_id: str = Field(default="", max_length=512)
    oidc_client_secret: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Optional. Omit for a public client, where PKCE alone protects the "
            "code exchange — the current recommendation for browser-driven flows."
        ),
    )
    oidc_scopes: str = Field(
        default="openid profile email",
        max_length=512,
        description=(
            "Space-separated scopes. Add the provider's groups scope here if "
            "OIDC_ADMIN_GROUP or OIDC_ALLOWED_GROUPS is used — most issuers omit "
            "the groups claim entirely unless it was asked for."
        ),
    )
    oidc_redirect_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "The absolute callback URL registered at the issuer. Empty derives it "
            "from the incoming request's forwarded host, which is right behind a "
            "single well-configured ingress and wrong behind anything that "
            "rewrites Host — set it explicitly for any deployment where the "
            "public URL is not what the pod sees."
        ),
    )
    oidc_username_claim: str = Field(default="preferred_username", max_length=128)
    oidc_email_claim: str = Field(default="email", max_length=128)
    oidc_display_name_claim: str = Field(default="name", max_length=128)
    oidc_groups_claim: str = Field(
        default="groups",
        max_length=128,
        description=(
            "Claim carrying group membership. A claim that is absent from the "
            "token is treated as 'not reported' rather than as 'no groups': the "
            "first leaves an existing role alone, the second would demote."
        ),
    )
    oidc_admin_group: str = Field(
        default="",
        max_length=512,
        description=(
            "Members of this group get the admin role. Empty means every SSO "
            "account gets the user role and administrators are managed locally."
        ),
    )
    oidc_allowed_groups: str = Field(
        default="",
        max_length=2048,
        description=(
            "Comma-separated groups permitted to use the console at all. Empty "
            "means any account the issuer authenticates may sign in. When it is "
            "set and the token carries no groups claim, sign-in is REFUSED — "
            "unlike role mapping, an allowlist must fail closed, or it stops "
            "working exactly when the issuer is misconfigured."
        ),
    )
    oidc_verify_tls: bool = Field(
        default=True,
        description=(
            "Verify the issuer's TLS certificate. Disabling it means the signed "
            "assertions this console validates were delivered over a channel "
            "anyone on the path can control; it is logged at WARNING on every "
            "discovery fetch."
        ),
    )
    oidc_ca_certificate_file: str = Field(
        default="",
        max_length=2048,
        description="CA bundle path for an issuer using a private CA.",
    )
    oidc_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
        description=(
            "Deadline for discovery, JWKS and token-endpoint calls. Bounded for "
            "the same reason the Kubernetes client is: a black-holed connection "
            "otherwise parks a threadpool worker forever."
        ),
    )
    oidc_clock_skew_seconds: int = Field(
        default=60,
        ge=0,
        le=600,
        description=(
            "Leeway on exp/iat. Without any, a console whose clock is thirty "
            "seconds behind its issuer rejects every freshly minted token and the "
            "symptom reads as a broken identity provider."
        ),
    )
    oidc_button_label: str = Field(
        default="Single sign-on",
        max_length=64,
        description="Text on the login page's SSO button.",
    )

    # -- Generic OAuth 2.0 single sign-on ---------------------------------
    #
    # An OAuth 2.0 authorization server that is *not* an OpenID Connect
    # provider: GitHub, GitLab, Gitea, Bitbucket, a bare Keycloak client with
    # OIDC turned off. It issues an access token and no ID token, publishes no
    # JWKS and usually no discovery document, so identity comes from a userinfo
    # endpoint read with the freshly-minted token rather than from a signature
    # this console can check. See app.identity.oauth for why that is a different
    # trust argument rather than a weaker version of the same one.
    oauth_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OAUTH_ENABLED", "oauth_enabled"),
        description=(
            "Offer generic OAuth 2.0 single sign-on. Requires AUTH_ENABLED plus "
            "OAUTH_AUTHORIZATION_URL, OAUTH_TOKEN_URL, OAUTH_USERINFO_URL and "
            "OAUTH_CLIENT_ID; without all of them the login page shows no "
            "button, because a button that cannot work reads as a broken "
            "console rather than an unconfigured one."
        ),
    )
    oauth_authorization_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "Where the browser is sent to begin the flow. Configured rather than "
            "discovered: a bare OAuth 2.0 server is not required to publish "
            "metadata anywhere, and RFC 8414 is served by a minority of them."
        ),
    )
    oauth_token_url: str = Field(default="", max_length=2048)
    oauth_userinfo_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "The endpoint read with the access token to learn who signed in. "
            "Required: without it the token is an opaque string that asserts "
            "nothing, and a console that issued a session from it would be "
            "signing people in on the strength of a successful HTTP call."
        ),
    )
    oauth_client_id: str = Field(default="", max_length=512)
    oauth_client_secret: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Optional. Omit for a public client, where PKCE alone protects the "
            "code exchange. Most OAuth 2.0 servers that are not OIDC providers "
            "require it."
        ),
    )
    oauth_scopes: str = Field(
        default="",
        max_length=512,
        description=(
            "Space-separated scopes. Empty sends none, which is what several "
            "servers want; 'openid profile email' is an OIDC vocabulary and is "
            "rejected as unknown by some of the servers this provider exists "
            "for, so it is deliberately not the default."
        ),
    )
    oauth_redirect_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "The absolute callback URL registered at the provider. Empty derives "
            "it from the forwarded host — right behind one well-configured "
            "ingress and wrong behind anything that rewrites Host."
        ),
    )
    oauth_subject_field: str = Field(
        default="sub",
        max_length=128,
        description=(
            "Userinfo field holding the provider's stable identifier for the "
            "person. It is what the account is bound to, so a provider that "
            "names it something else — GitHub's 'id', GitLab's 'id' — must say "
            "so here or a renamed account becomes a different person. Dotted "
            "paths ('data.viewer.id') address a nested field."
        ),
    )
    oauth_username_field: str = Field(default="preferred_username", max_length=128)
    oauth_email_field: str = Field(default="email", max_length=128)
    oauth_display_name_field: str = Field(default="name", max_length=128)
    oauth_groups_field: str = Field(
        default="groups",
        max_length=128,
        description=(
            "Field carrying group membership. A field the userinfo document does "
            "not contain is treated as 'not reported' rather than as 'no "
            "groups': the first leaves an existing role alone, the second would "
            "demote."
        ),
    )
    oauth_admin_group: str = Field(default="", max_length=512)
    oauth_allowed_groups: str = Field(
        default="",
        max_length=2048,
        description=(
            "Comma-separated groups permitted to use the console at all. When it "
            "is set and the userinfo document carries no groups field, sign-in "
            "is REFUSED — an allowlist must fail closed."
        ),
    )
    oauth_verify_tls: bool = Field(default=True)
    oauth_ca_certificate_file: str = Field(default="", max_length=2048)
    oauth_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    oauth_button_label: str = Field(default="OAuth 2.0", max_length=64)

    # -- OpenShift built-in OAuth single sign-on --------------------------
    #
    # The cluster's own OAuth server, fronting whatever identity providers the
    # cluster administrator configured. Not the OIDC provider and not the
    # generic OAuth one: metadata is RFC 8414 on the API server, the scopes are
    # OpenShift's own vocabulary, and identity comes from the cluster's
    # `users/~` virtual endpoint. See app.identity.openshift.
    openshift_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("OPENSHIFT_ENABLED", "openshift_enabled"),
        description=(
            "Offer sign-in through the cluster's built-in OAuth server. Requires "
            "AUTH_ENABLED plus OPENSHIFT_API_URL and OPENSHIFT_CLIENT_ID."
        ),
    )
    openshift_api_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "The cluster API server, e.g. https://api.cluster.example.com:6443. "
            "Both the authorization-server metadata and the users/~ read are "
            "taken from it, so the flow cannot be half-configured across two "
            "clusters."
        ),
    )
    openshift_client_id: str = Field(
        default="",
        max_length=512,
        description=(
            "The OAuthClient resource name, or 'system:serviceaccount:<ns>:<sa>' "
            "for a ServiceAccount acting as an OAuth client."
        ),
    )
    openshift_client_secret: SecretStr = Field(default=SecretStr(""))
    openshift_scopes: str = Field(
        default="user:info",
        max_length=512,
        description=(
            "OpenShift's own scope vocabulary. 'user:info' is the least "
            "privileged scope that can read users/~ and grants access to nothing "
            "else in the cluster — a token minted for a console login cannot "
            "list pods or read secrets. 'openid profile email' is an OIDC "
            "vocabulary and OpenShift rejects it as unknown."
        ),
    )
    openshift_redirect_url: str = Field(default="", max_length=2048)
    openshift_admin_group: str = Field(default="", max_length=512)
    openshift_allowed_groups: str = Field(
        default="",
        max_length=2048,
        description=(
            "Comma-separated groups permitted to use the console at all. Note "
            "that OpenShift attaches system:authenticated and "
            "system:authenticated:oauth to every OAuth login, so listing either "
            "admits every account the cluster authenticates."
        ),
    )
    openshift_verify_tls: bool = Field(
        default=True,
        description=(
            "Verify the API server's TLS certificate. A cluster with a "
            "self-signed serving certificate needs OPENSHIFT_CA_CERTIFICATE_FILE "
            "rather than this turned off: the identity this flow returns is only "
            "worth what the channel it arrived on is."
        ),
    )
    openshift_ca_certificate_file: str = Field(default="", max_length=2048)
    openshift_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    openshift_button_label: str = Field(default="OpenShift", max_length=64)

    # -- SAML 2.0 single sign-on ------------------------------------------
    #
    # SP-initiated Redirect/POST, one identity provider per deployment. The
    # assertion is verified against a configured certificate and read back out
    # of the signature's own verified subtree — see app.identity.saml for why
    # reading it from anywhere else is the attack this whole flow turns on.
    saml_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("SAML_ENABLED", "saml_enabled"),
        description=(
            "Offer SAML 2.0 single sign-on. Requires AUTH_ENABLED, "
            "SAML_IDP_SSO_URL, SAML_IDP_CERTIFICATE and AUTH_COOKIE_SECURE — the "
            "last because the assertion arrives on a cross-site POST, which "
            "carries no SameSite=Lax cookie, so the handshake cookie has to be "
            "SameSite=None and a browser discards that without Secure. A "
            "plain-HTTP deployment therefore cannot complete a SAML sign-in, and "
            "the button is withheld rather than shown broken."
        ),
    )
    saml_idp_entity_id: str = Field(
        default="",
        max_length=2048,
        description=(
            "The identity provider's entityID. Checked against the assertion's "
            "Issuer: without it any certificate this console trusts could sign "
            "an assertion for any issuer."
        ),
    )
    saml_idp_sso_url: str = Field(
        default="",
        max_length=2048,
        description="The IdP's HTTP-Redirect single sign-on endpoint.",
    )
    saml_idp_certificate: str = Field(
        default="",
        max_length=32_768,
        description=(
            "The IdP's signing certificate: PEM, or the bare base64 body as it "
            "appears in IdP metadata. Several may be concatenated, which is what "
            "makes a signing-key rotation a config change rather than an outage."
        ),
    )
    saml_sp_entity_id: str = Field(
        default="",
        max_length=2048,
        description=(
            "This console's entityID, as registered at the IdP. Empty uses "
            "SAML_ACS_URL, which is the common convention."
        ),
    )
    saml_acs_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "The absolute Assertion Consumer Service URL registered at the IdP. "
            "Empty derives it from the forwarded host. It is checked against the "
            "assertion's Recipient, which is required rather than checked when "
            "present, so a derived value that does not match what was registered "
            "fails the sign-in rather than quietly accepting an assertion "
            "addressed elsewhere. The response wrapper's Destination is not "
            "checked: it sits outside the signature, so it is the replayer's to "
            "write."
        ),
    )
    saml_username_attribute: str = Field(
        default="",
        max_length=256,
        description=(
            "Assertion attribute holding the username. Empty uses the Subject's "
            "NameID, which every IdP sends."
        ),
    )
    saml_email_attribute: str = Field(default="email", max_length=256)
    saml_display_name_attribute: str = Field(default="displayName", max_length=256)
    saml_groups_attribute: str = Field(
        default="groups",
        max_length=256,
        description=(
            "Attribute carrying group membership. An attribute the assertion "
            "does not carry is 'not reported', not 'no groups'."
        ),
    )
    saml_admin_group: str = Field(default="", max_length=512)
    saml_allowed_groups: str = Field(default="", max_length=2048)
    saml_clock_skew_seconds: int = Field(
        default=60,
        ge=0,
        le=600,
        description=(
            "Leeway on NotBefore/NotOnOrAfter. SAML condition windows are often "
            "five minutes wide, so a console whose clock is a minute behind its "
            "IdP rejects every assertion and the symptom reads as a broken IdP."
        ),
    )
    saml_button_label: str = Field(default="SAML single sign-on", max_length=64)

    # -- Kubernetes transport --------------------------------------------
    # There were no deadlines on the k8boss client at first, and a black-holed
    # connection (an API server behind a firewall that drops rather than resets,
    # a NAT that lost the conntrack entry) left the socket blocked forever. Every
    # sync route reaching Kubernetes runs in the shared threadpool, so those
    # threads never came back and the pod stopped serving while its liveness
    # probe -- which touches no cluster -- kept answering healthy.
    k8s_connect_timeout_seconds: float = Field(
        default=5.0,
        description="TCP connect deadline for Kubernetes API calls.",
    )
    k8s_read_timeout_seconds: float = Field(
        default=30.0,
        description="Read deadline for non-streaming Kubernetes API calls.",
    )
    k8s_watch_read_timeout_seconds: float = Field(
        default=600.0,
        description=(
            "Read deadline for watches and log/exec streams, which are legitimately "
            "idle between events. A pod that logs nothing for five minutes is normal; "
            "applying the 30s read deadline to its log stream would tear down every "
            "viewer and report it as an error. Long enough never to fire in normal "
            "operation, short enough that a black-holed socket is eventually "
            "reclaimed rather than leaking a thread forever."
        ),
    )

    # -- local development kubeconfig fallback ---------------------------
    # Used only when no cluster is registered. Production registers clusters
    # explicitly; this exists so `uvicorn app.main:app` against a kind cluster
    # works with no setup.
    kubeconfig_path: str = Field(
        default="~/.kube/config",
        description="Kubeconfig used when no cluster is registered. Ignored in-cluster.",
    )
    kube_context: str | None = Field(
        default=None,
        description="Kubeconfig context name. None = the file's current-context.",
    )
    in_cluster_mode: bool = Field(
        default=False,
        description=(
            "Authenticate with the pod's own mounted ServiceAccount when no cluster "
            "is registered. Set by the Helm chart."
        ),
    )

    # -- §34 onboarding ---------------------------------------------------
    auto_discover_local_cluster: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "ADMIN_AUTO_DISCOVER_LOCAL", "auto_discover_local_cluster",
        ),
        description=(
            "Register the kubeconfig's local cluster at startup when nothing is "
            "registered yet. On by default because the alternative for somebody "
            "with a kind or k3d cluster is a ServiceAccount, a token and four "
            "commands before the console shows anything at all — and because "
            "what it can adopt is narrow: only when the registry is EMPTY, only "
            "a context a local tool wrote (kind, k3d, minikube, Docker Desktop "
            "and the rest of app.k8s.kubeconfig.LOCAL_DISTRIBUTIONS, plus k3s "
            "and RKE2, which name nothing and are recognised by the fixed path "
            "they write their kubeconfig to), only when "
            "its API server is a loopback or private address, and only when "
            "exactly one such context qualifies. A remote context is never "
            "adopted, at any count: a console that boots and quietly registers "
            "the production cluster a developer happens to have a context for is "
            "a worse failure than one that asks. Set false to require every "
            "registration to be an explicit act."
        ),
    )

    # -- HTTP -------------------------------------------------------------
    cors_origins: str = Field(
        default="",
        description=(
            "Comma-separated allowed origins for split-origin deployments. Empty "
            "(the default) means no cross-origin access at all, which is what a "
            "console whose SPA nginx serves same-origin needs — and what `npm run "
            "dev` needs too, because Vite proxies /api and the browser never sees "
            "a cross-origin request. The previous default listed :5173, :3000 and "
            ":8080; none of those is this console (:5174 is), and :5173 is K8Boss "
            "— a different application routinely run on the same machine. Paired "
            "with allow_credentials=True in app.main, that shipped a credentialed "
            "grant over this console's API to a page served by somebody else's "
            "dev server, and granted nothing to the origin that might have wanted "
            "one."
        ),
    )
    log_level: str = Field(
        default="INFO",
        description="Root log level: DEBUG | INFO | WARNING | ERROR.",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """``cors_origins`` split into the list CORSMiddleware wants."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


# Process-wide settings instance. Imported directly (``from app.config import
# settings``) rather than injected, because there is exactly one process
# configuration and threading it through every signature buys nothing.
settings = Settings()
