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

    # -- HTTP -------------------------------------------------------------
    cors_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000,http://localhost:8080",
        description="Comma-separated allowed origins for the SPA dev server and bundle host.",
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
