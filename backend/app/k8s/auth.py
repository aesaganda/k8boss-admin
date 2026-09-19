"""
Authentication strategy for building a Kubernetes client configuration.

Client construction depends on this interface and never on a specific scheme, so
adding OIDC or a cloud IAM exchange (EKS/AKS/GKE) means writing an
:class:`AuthProvider` and registering it in :func:`get_auth_provider` — not
editing ``client.py``. §34's client-certificate support is what that sentence
looked like when it was cashed in: one class here, one branch in the factory,
and two fields in ``build_configuration``.

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
# Five spellings because a Kubernetes ServiceAccount token, an OpenShift
# ServiceAccount token, a generically-labelled one, the form's "Bearer token"
# option and a token lifted out of a kubeconfig all authenticate identically;
# keeping the label the operator chose — or the one the import wrote — is worth
# more than normalising it away, and every one of them is a spelling something
# in this repository already sends.
AUTH_SERVICE_ACCOUNT_TOKEN = "service_account_token"
AUTH_KUBERNETES_TOKEN = "kubernetes_token"
AUTH_OPENSHIFT_TOKEN = "openshift_token"
#: What the registration form's "Bearer token" option sends. It authenticates
#: exactly like the three above and was missing from this set for as long as the
#: form offered it — so choosing it produced `422 Unsupported authentication
#: type 'bearer_token'` from `_validate`, which is a console rejecting its own
#: dropdown. Accepting the spelling is the fix; normalising it away would throw
#: out the label the operator chose.
AUTH_BEARER_TOKEN = "bearer_token"
#: §34. A context imported from a kubeconfig whose user carries a `token`.
#: Spelled apart from the three hand-registered labels so `origin` is not the
#: only thing that says where a credential came from.
AUTH_KUBECONFIG_TOKEN = "kubeconfig_token"

TOKEN_AUTH_TYPES = frozenset(
    {
        AUTH_SERVICE_ACCOUNT_TOKEN,
        AUTH_KUBERNETES_TOKEN,
        AUTH_OPENSHIFT_TOKEN,
        AUTH_BEARER_TOKEN,
        AUTH_KUBECONFIG_TOKEN,
    }
)

#: §34. Client-certificate authentication, which is what `kind`, `k3d`,
#: minikube and Docker Desktop write into a kubeconfig — none of them mints a
#: ServiceAccount token, so without this the local clusters this console most
#: wants to adopt with no setup were the ones it could not represent at all.
AUTH_CLIENT_CERTIFICATE = "client_certificate"

#: Every type `get_auth_provider` can build. The registration form validates
#: against this rather than against TOKEN_AUTH_TYPES, so a client-certificate
#: cluster can be edited after it is imported.
SUPPORTED_AUTH_TYPES = TOKEN_AUTH_TYPES | {AUTH_CLIENT_CERTIFICATE}

# Display metadata for the UI. Adding a scheme here is enough for it to render
# with a proper name; unknown values still render, generically, rather than
# blanking the field.
_AUTH_DISPLAY: dict[str, tuple[str, str]] = {
    AUTH_SERVICE_ACCOUNT_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    AUTH_KUBERNETES_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    AUTH_OPENSHIFT_TOKEN: ("SERVICE_ACCOUNT_TOKEN", "Service Account Token"),
    AUTH_BEARER_TOKEN: ("BEARER_TOKEN", "Bearer Token"),
    AUTH_KUBECONFIG_TOKEN: ("KUBECONFIG_TOKEN", "Kubeconfig Token"),
    "kubeconfig": ("KUBECONFIG", "Kubeconfig"),
    "oidc": ("OIDC", "OIDC"),
    AUTH_CLIENT_CERTIFICATE: ("CLIENT_CERTIFICATE", "Client Certificate"),
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


class ClientCertificateAuth(AuthProvider):
    """X.509 client-certificate authentication (§34).

    The credential a local cluster hands you. `kind`, `k3d`, minikube and Docker
    Desktop all write a client certificate and key into the kubeconfig and mint
    no token at all, so a console that only spoke bearer tokens could not
    represent the clusters it most wanted to adopt without setup — the
    documented workaround was to create a ServiceAccount and mint a token, which
    is the friction this exists to remove.

    Unlike a token, the key is not applied onto the ``Configuration`` directly:
    the kubernetes client reads both halves from *paths*. Writing those files is
    :func:`app.k8s.client.build_configuration`'s job, because it already owns
    the temp file the CA needs and the cleanup that deletes it; this provider
    carries the PEM and states which fields it belongs in.
    """

    def __init__(self, certificate: str, key: str):
        if not certificate or not key:
            raise AuthError(
                "Client-certificate authentication needs both a certificate and "
                "a private key, and this cluster is stored with only one of "
                "them. Re-import it from your kubeconfig, or re-register it with "
                "a ServiceAccount token."
            )
        self.certificate = certificate.strip() + "\n"
        self.key = key.strip() + "\n"

    def apply(self, configuration: "k8s_client.Configuration") -> None:
        """No-op on the configuration itself; see the class docstring.

        Deliberately not a `pass` with no explanation: a reader checking that
        every provider applies something needs to find the reason here rather
        than conclude the credential is being dropped.
        """

    def clear(self) -> None:
        self.certificate = ""
        self.key = ""


def get_auth_provider(
    authentication_type: str,
    *,
    token: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> AuthProvider:
    """Return the provider for a stored ``authentication_type``.

    Raises:
        AuthError: naming the supported types, because the operator reading this
            message is looking at a form with a free-text field and needs the
            list, not the fact that theirs was wrong.
    """
    if authentication_type in TOKEN_AUTH_TYPES:
        return ServiceAccountTokenAuth(token or "")
    if authentication_type == AUTH_CLIENT_CERTIFICATE:
        return ClientCertificateAuth(client_certificate or "", client_key or "")

    raise AuthError(
        f"Unsupported authentication type {authentication_type!r}. Supported "
        "types: " + ", ".join(sorted(SUPPORTED_AUTH_TYPES)) + "."
    )
