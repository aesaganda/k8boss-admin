"""
Authentication strategy for building a Kubernetes client configuration.

Client construction depends on this interface and never on a specific scheme, so
adding OIDC, client certificates, or a cloud IAM exchange (EKS/AKS/GKE) means
writing an :class:`AuthProvider` and registering it in :func:`get_auth_provider`
— not editing ``client.py``.

The factory raises rather than falling back to anonymous access for an unknown
type. An anonymous client against a permissive cluster half-works, which turns a
configuration mistake into an intermittent permissions mystery discovered weeks
later; a clear failure at registration time names the mistake while the operator
is still looking at the form.
"""

from __future__ import annotations

import abc
import logging

from kubernetes import client as k8s_client

logger = logging.getLogger(__name__)

# Stored ``Cluster.authentication_type`` values that mean "a bearer token".
# Three spellings because a Kubernetes ServiceAccount token, an OpenShift
# ServiceAccount token and a generically-labelled one authenticate identically;
# keeping the operator's chosen label is worth more than normalising it away.
AUTH_SERVICE_ACCOUNT_TOKEN = "service_account_token"
AUTH_KUBERNETES_TOKEN = "kubernetes_token"
AUTH_OPENSHIFT_TOKEN = "openshift_token"

TOKEN_AUTH_TYPES = frozenset(
    {AUTH_SERVICE_ACCOUNT_TOKEN, AUTH_KUBERNETES_TOKEN, AUTH_OPENSHIFT_TOKEN}
)

# Display metadata for the UI. Adding a scheme here is enough for it to render
# with a proper name; unknown values still render, generically, rather than
# blanking the field.
_AUTH_DISPLAY: dict[str, tuple[str, str]] = {
    AUTH_SERVICE_ACCOUNT_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    AUTH_KUBERNETES_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    AUTH_OPENSHIFT_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    "kubeconfig": ("KUBECONFIG", "Kubeconfig"),
    "oidc": ("OIDC", "OIDC"),
    "client_certificate": ("CLIENT_CERTIFICATE", "Client Certificate"),
}


class AuthError(Exception):
    """The stored authentication configuration cannot produce a usable client."""


def auth_metadata(authentication_type: str | None) -> dict[str, str]:
    """UI-facing description of an auth type. Never contains credential material."""
    if not authentication_type:
        return {"type": "UNKNOWN", "displayName": "Unknown"}
    key = authentication_type.strip().lower()
    if key in _AUTH_DISPLAY:
        canonical, display = _AUTH_DISPLAY[key]
        return {"type": canonical, "displayName": display}
    return {"type": key.upper(), "displayName": key.replace("_", " ").title()}


class AuthProvider(abc.ABC):
    """Applies credentials onto a Kubernetes ``Configuration``."""

    @abc.abstractmethod
    def apply(self, configuration: "k8s_client.Configuration") -> None:
        """Mutate ``configuration`` so it authenticates as this provider's identity."""

    def clear(self) -> None:
        """Best-effort wipe of in-memory credential material after ``apply``.

        A no-op for providers that hold nothing; overridden where there is
        something to wipe. Defined on the base class so callers can invoke it
        unconditionally instead of type-testing, which is how the one provider
        that needed it got missed.
        """


class ServiceAccountTokenAuth(AuthProvider):
    """Bearer-token authentication.

    Identical for Kubernetes and OpenShift ServiceAccounts — both present a
    bearer token to the API server endpoint.
    """

    def __init__(self, token: str):
        if not token:
            raise AuthError(
                "A bearer token is required for token authentication, and none is "
                "stored for this cluster. Re-register the cluster with a token."
            )
        self._token = self._normalize(token)

    @staticmethod
    def _normalize(token: str) -> str:
        """Strip whitespace and an accidental ``Bearer `` prefix.

        Operators paste tokens out of ``kubectl describe secret`` and out of
        curl examples. A leading "Bearer " survives into the Authorization
        header as ``Bearer Bearer eyJ...`` and the API server answers 401, which
        the console would otherwise report as "the token is invalid" — true but
        useless. Normalising here makes the paste work.
        """
        token = token.strip()
        if token.lower().startswith("bearer "):
            return token[7:].strip()
        return token

    def apply(self, configuration: "k8s_client.Configuration") -> None:
        configuration.api_key = {"authorization": self._token}
        configuration.api_key_prefix = {"authorization": "Bearer"}

    def clear(self) -> None:
        self._token = ""


def get_auth_provider(authentication_type: str, *, token: str | None = None) -> AuthProvider:
    """Return the provider for a stored ``authentication_type``.

    Raises:
        AuthError: naming the supported types, because the operator reading this
            message is looking at a form with a free-text field and needs the
            list, not the fact that theirs was wrong.
    """
    if authentication_type in TOKEN_AUTH_TYPES:
        return ServiceAccountTokenAuth(token or "")

    raise AuthError(
        f"Unsupported authentication type {authentication_type!r}. Supported "
        "types: " + ", ".join(sorted(TOKEN_AUTH_TYPES)) + "."
    )
