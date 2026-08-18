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

from pydantic import AliasChoices, Field
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
