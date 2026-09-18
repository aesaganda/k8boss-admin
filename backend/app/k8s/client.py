"""
Per-cluster Kubernetes client construction and caching.

Clients are built from a registered cluster's stored API server endpoint and
encrypted bearer token — never from a kubeconfig on the server. A kubeconfig
fallback exists for local development only, and only when no cluster is
registered at all.

There is no global singleton API client. :class:`ClusterClientManager` keeps a
small per-cluster cache keyed by the cluster row's ``updated_at``, so editing a
cluster transparently rebuilds its clients instead of serving the old endpoint
until someone restarts the process. Credentials are decrypted at build time and
dropped immediately afterwards.

The one thing worth reading twice is :func:`_cluster_api_client`.
"""

from __future__ import annotations

import functools
import logging
import os
import tempfile
from dataclasses import dataclass, field

from kubernetes import client, config
from kubernetes.dynamic import DynamicClient
from urllib3.exceptions import MaxRetryError, ReadTimeoutError

from app.config import settings
from app.errors import ClusterUnreachable, NoClusterSelected, NotFound
from app.k8s.auth import AuthError, get_auth_provider
from app.k8s.context import get_current_cluster_id, get_current_principal
from app.k8s.impersonation import decide, headers_for_current_request, set_current

logger = logging.getLogger(__name__)

# Set once the first implicit multi-cluster resolution has been reported.
_warned_implicit_cluster = False


def _is_streaming(args, kwargs) -> bool:
    """Is this ``rest_client.request`` call a long-lived stream?

    Watches, ``follow`` log streams and exec channels are the calls that are
    *supposed* to sit idle for minutes, so they cannot share the normal read
    deadline — a 30-second deadline on a log stream tears down every viewer of a
    quiet pod and reports it as an error.

    Three independent signals, because the kubernetes client reaches this
    function by more than one route and missing a stream would break log viewing
    in a way that only shows up on quiet pods: ``_preload_content=False``
    (streaming), a ``watch``/``follow`` query parameter (which arrives
    positionally as the third argument or as a kwarg), and the parameter already
    spliced into the URL.
    """
    if kwargs.get("_preload_content") is False:
        return True
    query_params = kwargs.get("query_params")
    if query_params is None and len(args) > 2:
        query_params = args[2]
    try:
        for key, value in (query_params or []):
            if str(key) in ("watch", "follow") and str(value).lower() in ("true", "1"):
                return True
    except (TypeError, ValueError):
        pass
    url = kwargs.get("url") or (args[1] if len(args) > 1 else "")
    url = str(url)
    return "watch=true" in url or "follow=true" in url


def _cluster_api_client(
    configuration: client.Configuration | None = None,
    *,
    impersonatable: bool = False,
) -> client.ApiClient:
    """Build an ``ApiClient`` with deadlines, typed failures and ADR-0007 headers.

    Three things happen here, all in exactly one place on purpose.

    **Deadlines.** The kubernetes client is constructed with no request timeout,
    so a black-holed connection — an API server behind a firewall that drops
    rather than resets, a NAT that lost the conntrack entry — blocks the socket
    forever. Sync route handlers run in the shared threadpool, so those threads
    never come back and the console stops serving while its own liveness probe,
    which touches no cluster, keeps answering healthy. Injecting the deadline
    here rather than at every call site means a new endpoint cannot forget it.

    **Typed transport failures.** The kubernetes client only turns HTTP
    *responses* into ``ApiException``. A DNS failure, refused connection or TLS
    handshake error is raised by urllib3 straight through the API method and
    reaches no ``except ApiException`` anywhere. In k8boss that produced an
    opaque 500 from Starlette's ServerErrorMiddleware, which sits *outside*
    CORSMiddleware — so the browser reported ``TypeError: Failed to fetch`` and
    the real reason never reached the operator. Translating to
    :class:`app.errors.ClusterUnreachable` here is what lets
    ``app.api.exception_handlers`` render an accurate 502 with CORS headers.

    Translated at this level, and only here, so the attribution stays honest:
    this is the only place a *cluster* API client is built, and reporting some
    other component's connection failure as an unreachable cluster would be a
    confidently wrong answer of its own.

    **Impersonation (ADR-0007).** ``impersonatable`` decides whether this
    transport may ever carry ``Impersonate-User``, and it is a property of the
    transport rather than of the request for one reason: it makes the wrong
    answer unreachable instead of merely unlikely. A transport built from
    credentials that are not a registered cluster's — the connection test's
    unsaved form values, the local kubeconfig a developer runs against — cannot
    impersonate no matter what any contextvar says, so no future endpoint can
    arrange for an operator's identity to be asserted through a credential
    nobody registered.

    On a transport that *is* eligible, the headers come from the per-request
    decision made in :meth:`ClusterClientManager.get_clients`, which either
    produced them or raised. Merging them here rather than at the call sites is
    what stops a new endpoint from forgetting: there is no route to the API
    server that does not pass through this function.
    """
    api_client = (
        client.ApiClient(configuration=configuration)
        if configuration is not None
        else client.ApiClient()
    )
    inner = api_client.rest_client.request

    @functools.wraps(inner)
    def request(*args, **kwargs):
        if impersonatable:
            # `headers` is the fourth positional parameter of
            # `RESTClientObject.request` and is passed as a keyword by every
            # route the generated client takes. Both are handled anyway: a
            # positional call that silently skipped impersonation would make
            # one code path act as the console on a cluster the operator
            # believes is acting as them.
            if "headers" in kwargs or len(args) <= 3:
                kwargs["headers"] = headers_for_current_request(kwargs.get("headers"))
            else:
                args = (*args[:3], headers_for_current_request(args[3]), *args[4:])
        # setdefault semantics: a caller that already passed a deadline wins.
        if "_request_timeout" not in kwargs:
            read = (
                settings.k8s_watch_read_timeout_seconds
                if _is_streaming(args, kwargs)
                else settings.k8s_read_timeout_seconds
            )
            kwargs["_request_timeout"] = (settings.k8s_connect_timeout_seconds, read)
        try:
            return inner(*args, **kwargs)
        except MaxRetryError as e:
            # e.reason carries the useful cause (NameResolutionError, refused
            # connection, certificate verification failure); e itself
            # stringifies to the retry wrapper, which tells an operator nothing.
            raise ClusterUnreachable(
                "The cluster API server could not be reached.",
                detail=str(e.reason or e),
                hint=(
                    "Check the API server URL, that the CA certificate matches the "
                    "server, and that this pod has network access to it."
                ),
                context={"cause": "unreachable"},
            ) from e
        except ReadTimeoutError as e:
            # Distinguished from the above because the operator's next step
            # differs: the endpoint resolved and accepted the connection, and
            # then the API server did not answer in time. Reporting that as
            # "unreachable" sends them to check DNS and firewalls, which are
            # provably fine.
            raise ClusterUnreachable(
                "The cluster API server did not answer within the read deadline.",
                detail=str(e),
                hint=(
                    "The API server accepted the connection but did not respond in "
                    f"{settings.k8s_read_timeout_seconds:.0f}s. It may be overloaded, "
                    "or the listing may be too large — narrow it with a namespace or "
                    "a smaller limit."
                ),
                context={"cause": "timeout"},
            ) from e

    api_client.rest_client.request = request
    return api_client


@dataclass
class ClusterClients:
    """Typed API clients for one cluster, all sharing a single ``ApiClient``.

    One transport per cluster, so the deadline and error translation installed by
    :func:`_cluster_api_client` apply to every call regardless of which typed
    client made it.
    """

    cluster_id: int | None
    platform: str
    cache_key: str
    api_client: client.ApiClient
    core_v1: client.CoreV1Api
    apps_v1: client.AppsV1Api
    batch_v1: client.BatchV1Api
    networking_v1: client.NetworkingV1Api
    rbac_v1: client.RbacAuthorizationV1Api
    storage_v1: client.StorageV1Api
    authorization_v1: client.AuthorizationV1Api
    version_api: client.VersionApi
    _ca_temp_path: str | None = field(default=None, repr=False)
    _dynamic: DynamicClient | None = field(default=None, repr=False)

    @property
    def dynamic(self) -> DynamicClient:
        """Lazily built dynamic client.

        Lazy because constructing it performs discovery against the API server:
        building it eagerly would make every cluster registration pay a round
        trip that most requests never need, and would turn a transient discovery
        failure into "the cluster cannot be used at all" rather than "the generic
        resource browser is unavailable".
        """
        if self._dynamic is None:
            self._dynamic = DynamicClient(self.api_client)
        return self._dynamic

    def stream_core_v1(self) -> client.CoreV1Api:
        """A ``CoreV1Api`` whose ``ApiClient`` nothing else shares, for ``kubernetes.stream``.

        ``kubernetes.stream.stream`` opens its channel by assigning a websocket
        function over ``ApiClient.request`` and restoring it in a ``finally``.
        ``ApiClient.__call_api`` reaches the transport only through that
        attribute, so on the cached per-cluster bundle the assignment is
        process-wide for the length of the handshake: another thread arriving in
        that window is handed the websocket function instead of its own request,
        and comes back as ``ApiException(status=0)`` — which ``from_api_exception``
        reads as ``cluster_unreachable`` and the operator is told to go and check
        a CA certificate that is fine.

        Worse, the restore writes back a *captured* value rather than deleting
        the attribute. Two overlapping handshakes therefore leave the websocket
        function installed for good: the second captures the first's function as
        the thing to restore, the first restores the real method, and the second
        then puts the websocket function back permanently. Every later call on
        that cluster — listings, the §0.2 preflights, the whole write funnel, the
        dynamic client — is routed into it, so the console reports a cluster it
        can no longer read as unreachable until the process restarts, while
        ``kubectl`` against the same API server works.

        **A fresh client per call, never cached.** Caching it would put two
        concurrent exec sessions back on one transport, which is the bug.

        **A plain ``ApiClient``, deliberately not** :func:`_cluster_api_client`.
        The stream replaces ``request`` above ``rest_client``, so that wrapper's
        deadline, error translation and impersonation headers cannot run on this
        path whatever we install here; installing them would be a comment
        claiming a behaviour the code does not have. The ``Configuration`` is
        shared rather than rebuilt, so there is no second token to decrypt, no
        second CA file to write and no second TLS handshake — and
        ``update_params_for_auth`` still applies the bearer token, because that
        happens in ``__call_api`` before ``request`` is reached.
        """
        return client.CoreV1Api(client.ApiClient(configuration=self.api_client.configuration))

    def close(self) -> None:
        """Release the transport and the CA temp file. Safe to call twice."""
        try:
            self.api_client.close()
        except Exception:  # noqa: BLE001 - closing must never mask the real error
            logger.debug("Ignoring error while closing ApiClient for cluster %s",
                         self.cluster_id, exc_info=True)
        if self._ca_temp_path and os.path.exists(self._ca_temp_path):
            try:
                os.unlink(self._ca_temp_path)
            except OSError:
                logger.debug("Could not remove CA temp file %s", self._ca_temp_path)


def build_configuration(
    *,
    api_server: str,
    authentication_type: str,
    token: str | None,
    ca_certificate: str | None,
    skip_tls_verify: bool,
) -> tuple[client.Configuration, str | None]:
    """Build a ``Configuration`` from stored cluster parameters.

    Returns the configuration and the path of the temporary CA file, if one was
    written, so the caller can delete it when the client is discarded. The
    kubernetes client reads the CA from a *path*, not from memory, which is the
    only reason a temp file exists here.
    """
    configuration = client.Configuration()
    configuration.host = api_server.rstrip("/")

    auth = get_auth_provider(authentication_type, token=token)
    try:
        auth.apply(configuration)
    finally:
        # Wipe the provider's copy as soon as it has been applied. The token now
        # lives only in the Configuration this function returns.
        auth.clear()

    ca_temp_path: str | None = None
    if skip_tls_verify:
        # Explicitly requested by the operator at registration and surfaced in
        # every ClusterPublic response, so it can never be silently in effect.
        configuration.verify_ssl = False
    elif ca_certificate:
        fd, ca_temp_path = tempfile.mkstemp(suffix=".crt", prefix="k8boss-admin-ca-")
        with os.fdopen(fd, "w") as handle:
            handle.write(ca_certificate)
        configuration.ssl_ca_cert = ca_temp_path
        configuration.verify_ssl = True
    else:
        # No CA supplied: verify against the system trust store. Falling back to
        # verify_ssl=False here would make "I forgot to paste the CA" silently
        # equivalent to "I chose to skip verification".
        configuration.verify_ssl = True

    return configuration, ca_temp_path


class ClusterClientManager:
    """Builds and caches per-cluster API clients."""

    def __init__(self) -> None:
        self._cache: dict[int, ClusterClients] = {}
        self.active_cluster_id: int | None = None
        self._local: ClusterClients | None = None

    # -- cluster resolution ------------------------------------------------

    @staticmethod
    def _load_cluster(cluster_id: int):
        # Imported lazily: app.models imports app.database, and importing either
        # at module scope would make this module's import order depend on the
        # ORM being ready.
        from app.database import SessionLocal
        from app.models import Cluster

        db = SessionLocal()
        try:
            return db.get(Cluster, cluster_id)
        finally:
            db.close()

    @staticmethod
    def _infer_default_cluster_id() -> int | None:
        """Lowest-id registered cluster, used when nothing named one.

        With one registered cluster this is unambiguous. With several it is an
        arbitrary pick that the response does not disclose, so a caller who
        omitted ``cluster_id`` gets real data about *some* cluster with no way to
        tell which — on a fleet that is a misattributed answer, not a missing
        one. Warned once per process because the condition is structural, not a
        transient blip worth repeating per request.
        """
        from app.database import SessionLocal
        from app.models import Cluster

        db = SessionLocal()
        try:
            cluster = db.query(Cluster).order_by(Cluster.id.asc()).first()
            if cluster is None:
                return None
            global _warned_implicit_cluster
            if not _warned_implicit_cluster:
                total = db.query(Cluster).count()
                if total > 1:
                    _warned_implicit_cluster = True
                    logger.warning(
                        "A request named no cluster and was resolved to the lowest-id "
                        "registered cluster (id=%s) while %s clusters are registered. "
                        "The response does not say which cluster it describes. Pass an "
                        "explicit ?cluster_id= on multi-cluster deployments.",
                        cluster.id, total,
                    )
            return cluster.id
        finally:
            db.close()

    def resolve_cluster_id(self, cluster_id: int | None = None) -> int | None:
        """Explicit id, then the operator-selected active cluster, then inference."""
        if cluster_id is not None:
            return cluster_id
        if self.active_cluster_id is not None:
            return self.active_cluster_id
        return self._infer_default_cluster_id()

    # -- construction ------------------------------------------------------

    @staticmethod
    def _cache_key(cluster) -> str:
        """Identity of a cluster *configuration*, not of a cluster.

        Keyed on ``updated_at`` so an edited endpoint, CA or token invalidates
        the cached transport by construction. Without it, changing a cluster's
        API server keeps talking to the old one until the process restarts —
        and the UI, showing the new endpoint, reports data from the old one.
        """
        stamp = cluster.updated_at.isoformat() if cluster.updated_at else ""
        return f"{cluster.id}:{stamp}"

    def create_client(self, cluster, *, impersonatable: bool = True) -> ClusterClients:
        """Build a fresh bundle for a Cluster row. The token is not retained.

        ``impersonatable`` defaults to True because a bundle built from a
        registered row is the one kind that may carry ADR-0007 headers. The
        connection test passes False; see :meth:`get_clients_for_cluster`.
        """
        from app.crypto import decrypt

        token = ""
        try:
            if cluster.token_encrypted:
                token = decrypt(cluster.token_encrypted)
            configuration, ca_temp_path = build_configuration(
                api_server=cluster.api_server,
                authentication_type=cluster.authentication_type,
                token=token,
                ca_certificate=cluster.ca_certificate,
                skip_tls_verify=bool(cluster.skip_tls_verify),
            )
        finally:
            # Drop the plaintext from this frame whichever way we leave it,
            # including on the AuthError path, so a traceback rendered by a
            # debugger or an error reporter never carries a live token in a
            # local variable.
            token = ""

        api_client = _cluster_api_client(configuration, impersonatable=impersonatable)
        bundle = _bundle(
            api_client,
            cluster_id=cluster.id,
            platform=cluster.platform or "kubernetes",
            cache_key=self._cache_key(cluster),
            ca_temp_path=ca_temp_path,
        )
        logger.info("Built Kubernetes clients for cluster id=%s", cluster.id)
        return bundle

    def build_transient(
        self,
        *,
        api_server: str,
        authentication_type: str,
        token: str | None,
        ca_certificate: str | None,
        skip_tls_verify: bool,
        platform: str = "kubernetes",
    ) -> ClusterClients:
        """Build an uncached bundle from plaintext parameters.

        For testing a configuration that has not been saved yet. Never cached:
        an unsaved configuration has no ``updated_at`` to key on, so caching it
        would make a subsequent real request use credentials that exist nowhere
        but in one operator's browser form.
        """
        configuration, ca_temp_path = build_configuration(
            api_server=api_server,
            authentication_type=authentication_type,
            token=token,
            ca_certificate=ca_certificate,
            skip_tls_verify=skip_tls_verify,
        )
        return _bundle(
            # Never impersonatable: these credentials are an unsaved form's, and
            # asserting somebody's cluster identity through a credential nobody
            # has registered is not a thing this console should be able to do
            # even by accident.
            _cluster_api_client(configuration, impersonatable=False),
            cluster_id=None,
            platform=platform,
            cache_key="transient",
            ca_temp_path=ca_temp_path,
        )

    def get_clients(self, cluster_id: int | None = None) -> ClusterClients:
        """Cached or freshly built clients for the resolved cluster.

        Raises:
            NotFound: an explicit cluster id that is not registered — a stale
                bookmark or a deleted cluster, which is a different problem from
                having registered none.
            NoClusterSelected: nothing is registered and no local kubeconfig is
                usable.
            ClusterUnreachable: only from the *calls*, never from this function;
                building a client performs no I/O.
        """
        resolved = self.resolve_cluster_id(cluster_id)
        if resolved is None:
            return self._get_local_clients()

        cluster = self._load_cluster(resolved)
        if cluster is None:
            raise NotFound(
                f"Cluster {resolved} is not registered.",
                hint="It may have been removed. Pick a cluster from the switcher.",
                context={"resource": "clusters", "name": str(resolved)},
            )

        # ADR-0007, before anything is cached or returned. `decide` either
        # produces headers or raises, so a session that cannot supply a cluster
        # identity never receives a transport it could have used as the console.
        #
        # Pinned on a contextvar rather than on the bundle because the bundle is
        # shared between operators and the decision is not. The bundle's own
        # `impersonatable` flag is what keeps the two from being confused: a
        # transport that must never impersonate ignores this value entirely.
        set_current(decide(cluster, get_current_principal()))

        key = self._cache_key(cluster)
        cached = self._cache.get(cluster.id)
        if cached is not None and cached.cache_key == key:
            return cached
        if cached is not None:
            cached.close()

        bundle = self.create_client(cluster)
        self._cache[cluster.id] = bundle
        return bundle

    def get_clients_for_cluster(self, cluster) -> ClusterClients:
        """Uncached clients for a specific Cluster row, bypassing the cache.

        Used by the connection test, which must exercise the row as it stands
        rather than whatever transport happens to be cached — otherwise "test"
        would report on the configuration the operator just replaced.

        **Never impersonated (ADR-0007), and this is one of the exemptions the
        ADR requires be named.** The connection test answers "can this console
        reach and authenticate to this cluster, and what may it do there", which
        is a question about the stored credential. Running it as the operator
        would report on the operator's permissions instead and leave the thing
        actually being tested unexercised — and a cluster whose ServiceAccount
        token had expired would still report a clean test for every operator
        whose own RBAC happened to be fine.
        """
        return self.create_client(cluster, impersonatable=False)

    # -- local kubeconfig fallback (development only) ----------------------

    def _get_local_clients(self) -> ClusterClients:
        """In-cluster ServiceAccount or local kubeconfig, when nothing is registered.

        Exists so ``uvicorn app.main:app`` against a kind cluster works with no
        setup. It is never reached once a cluster is registered, so it cannot
        mask a misconfigured registration by quietly answering from a developer's
        kubeconfig instead.

        **Never impersonated (ADR-0007), and named as an exemption.** There is
        no `Cluster` row here, so there is nothing carrying the per-cluster
        opt-in and nothing that could have been opted in — impersonation is a
        setting on a registered cluster, and this path exists precisely because
        none is registered. Both `_cluster_api_client` calls below therefore
        take the default `impersonatable=False`.
        """
        if self._local is not None:
            return self._local
        try:
            if settings.in_cluster_mode:
                config.load_incluster_config()
                api_client = _cluster_api_client()
            else:
                config.load_kube_config(
                    config_file=os.path.expanduser(settings.kubeconfig_path),
                    context=settings.kube_context,
                )
                api_client = _cluster_api_client(client.Configuration.get_default_copy())
        except Exception as e:  # noqa: BLE001 - many unrelated failure types
            logger.info(
                "No registered cluster and no usable local kubeconfig (%s).",
                type(e).__name__,
            )
            raise NoClusterSelected(
                "No cluster is registered and no local kubeconfig could be loaded.",
                detail=f"{type(e).__name__}: {e}",
                hint="Register a cluster with its API server URL and a bearer token.",
            ) from e

        self._local = _bundle(
            api_client, cluster_id=None, platform="kubernetes", cache_key="local",
            ca_temp_path=None,
        )
        return self._local

    # -- lifecycle ---------------------------------------------------------

    def set_active(self, cluster_id: int | None) -> None:
        self.active_cluster_id = cluster_id

    def invalidate(self, cluster_id: int) -> None:
        """Drop one cluster's cached transport, closing it."""
        bundle = self._cache.pop(cluster_id, None)
        if bundle is not None:
            bundle.close()

    def reset(self) -> None:
        """Drop every cached transport. Used at shutdown and between tests."""
        for bundle in self._cache.values():
            bundle.close()
        self._cache.clear()
        if self._local is not None:
            self._local.close()
            self._local = None
        self.active_cluster_id = None


def _bundle(
    api_client: client.ApiClient,
    *,
    cluster_id: int | None,
    platform: str,
    cache_key: str,
    ca_temp_path: str | None,
) -> ClusterClients:
    """Assemble every typed client over one ApiClient.

    One constructor for all three build paths (registered, transient, local), so
    a client added for a new endpoint cannot be present on some paths and absent
    on others — an omission that surfaces as an ``AttributeError`` deep inside a
    handler, which reads as a bug in the handler.
    """
    return ClusterClients(
        cluster_id=cluster_id,
        platform=platform,
        cache_key=cache_key,
        api_client=api_client,
        core_v1=client.CoreV1Api(api_client),
        apps_v1=client.AppsV1Api(api_client),
        batch_v1=client.BatchV1Api(api_client),
        networking_v1=client.NetworkingV1Api(api_client),
        rbac_v1=client.RbacAuthorizationV1Api(api_client),
        storage_v1=client.StorageV1Api(api_client),
        authorization_v1=client.AuthorizationV1Api(api_client),
        version_api=client.VersionApi(api_client),
        _ca_temp_path=ca_temp_path,
    )


# Process-wide manager.
manager = ClusterClientManager()


# -- context-resolved accessors ---------------------------------------------
# Every handler reaches Kubernetes through one of these. None takes a cluster
# id: the cluster comes from the request context (§1.1), so a handler cannot
# accidentally answer about a cluster other than the one it was asked about.


def get_clients() -> ClusterClients:
    """Client bundle for the request's cluster."""
    return manager.get_clients(get_current_cluster_id())


def get_core_v1() -> client.CoreV1Api:
    """Pods, services, namespaces, nodes, configmaps, secrets, events, PVs, PVCs."""
    return get_clients().core_v1


def get_apps_v1() -> client.AppsV1Api:
    """Deployments, StatefulSets, DaemonSets, ReplicaSets, ControllerRevisions."""
    return get_clients().apps_v1


def get_batch_v1() -> client.BatchV1Api:
    """Jobs and CronJobs."""
    return get_clients().batch_v1


def get_networking_v1() -> client.NetworkingV1Api:
    """Ingresses and IngressClasses."""
    return get_clients().networking_v1


def get_rbac_v1() -> client.RbacAuthorizationV1Api:
    """Roles, ClusterRoles and their bindings."""
    return get_clients().rbac_v1


def get_storage_v1() -> client.StorageV1Api:
    """StorageClasses."""
    return get_clients().storage_v1


def get_authorization_v1() -> client.AuthorizationV1Api:
    """SelfSubjectAccessReview — the preflight every mutation goes through."""
    return get_clients().authorization_v1


def get_version_api() -> client.VersionApi:
    """Server version, for /api/health and cluster overview."""
    return get_clients().version_api


def get_dynamic() -> DynamicClient:
    """Dynamic client for any resource the cluster serves, including CRDs."""
    return get_clients().dynamic


def get_api_client() -> client.ApiClient:
    """Raw transport, for log streaming and exec, which bypass the typed clients."""
    return get_clients().api_client


__all__ = [
    "AuthError",
    "ClusterClientManager",
    "ClusterClients",
    "build_configuration",
    "get_api_client",
    "get_apps_v1",
    "get_authorization_v1",
    "get_batch_v1",
    "get_clients",
    "get_core_v1",
    "get_dynamic",
    "get_networking_v1",
    "get_rbac_v1",
    "get_storage_v1",
    "get_version_api",
    "manager",
]
