"""
The pod detail reads (§7.5–§7.7).

What this file pins down, and why each one is a test rather than a comment:

* **The detail enriches the §6 row, it does not restate it.** ``ready`` and
  ``restarts`` still exclude ephemeral containers after the enrichment, because
  the enrichment writes new keys onto the shaper's entries rather than building
  its own. A refactor that rebuilt them would show a healthy pod as degraded for
  the duration of somebody's debug shell, and nothing else in the suite would
  notice.

* **The environment never returns a Secret value.** Not with the reveal setting
  on, not through ``envFrom``, not anywhere. The test asserts the fixture's
  secret material is absent from the whole serialised response, which is the only
  assertion that survives someone adding a field.

* **A ConfigMap we could not read is a named blank, not a missing variable.** The
  row stays, ``value_state`` is ``unreadable``, and ``unavailable[]`` says which
  object. A viewer that dropped the row would have an operator conclude the
  variable is not set.

* **No metrics is `null`, never `0`.** A cluster with no ``metrics.k8s.io`` gets
  a 200 with `usage: null` and an `unsupported` entry — not a 501 that hides the
  requests, and not a pod drawn at zero cores. Zero reads as idle, and idle is
  what gets things turned off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from kubernetes.client.rest import ApiException

from app.errors import RBACDenied, Unsupported
from app.resources import catalog
from app.services import pods as pods_service
from tests.conftest import obj

NAMESPACE = "prod"
POD = "checkout-7d9f6c-abcde"

POD_PATH = f"/api/v1/namespaces/{NAMESPACE}/pods/{POD}"
METRICS_PATH = f"/apis/metrics.k8s.io/v1beta1/namespaces/{NAMESPACE}/pods/{POD}"

#: The one string a Secret value would have to be for the leak test to mean
#: anything. Distinctive so a substring search cannot match a field name.
SECRET_VALUE = "cGFzc3dvcmQtZG8tbm90LWxlYWs="

METRICS_CATALOG_ITEM = {
    "group": "metrics.k8s.io",
    "version": "v1beta1",
    "kind": "PodMetrics",
    "resource": "pods",
    "namespaced": True,
    "verbs": ["get", "list"],
    "shortNames": [],
    "categories": [],
    "apiVersion": "metrics.k8s.io/v1beta1",
    "preferred": True,
}


def live_pod(**overrides):
    """The pod as the API server hands it back — a plain JSON dict.

    A dict rather than a ``V1Pod`` because :func:`app.resources.reader.read_object`
    deliberately reaches past the typed clients, so this is the shape the code
    under test actually receives.
    """
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": POD,
            "namespace": NAMESPACE,
            "uid": "5b1f7c0e-0f0a-4c1b-9a2e-b1d2c3e4f5a6",
            "resourceVersion": "884213",
            "creationTimestamp": "2026-08-01T09:15:00Z",
            "labels": {"app": "checkout"},
            "annotations": {"kubectl.kubernetes.io/default-container": "app"},
        },
        "spec": {
            "nodeName": "ip-10-0-1-4",
            "serviceAccountName": "checkout",
            "restartPolicy": "Always",
            "priorityClassName": "high",
            "nodeSelector": {"kubernetes.io/os": "linux"},
            "initContainers": [
                {"name": "migrate", "image": "ghcr.io/acme/migrate:1.9.2"},
            ],
            "containers": [
                {
                    "name": "app",
                    "image": "ghcr.io/acme/checkout:1.9.2",
                    "ports": [{"name": "http", "containerPort": 8080, "protocol": "TCP"}],
                    "resources": {
                        "requests": {"cpu": "250m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "512Mi"},
                    },
                    "env": [
                        {"name": "LOG_LEVEL", "value": "info"},
                        {"name": "POD_IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
                        {
                            "name": "DB_PASSWORD",
                            "valueFrom": {"secretKeyRef": {"name": "checkout-db", "key": "password"}},
                        },
                        {
                            "name": "FEATURE_FLAGS",
                            "valueFrom": {"configMapKeyRef": {"name": "checkout-config", "key": "flags"}},
                        },
                    ],
                    "envFrom": [{"configMapRef": {"name": "checkout-config"}}],
                },
                {"name": "envoy", "image": "envoyproxy/envoy:v1.29"},
            ],
            "volumes": [
                {"name": "config", "configMap": {"name": "checkout-config"}},
                {"name": "data", "persistentVolumeClaim": {"claimName": "checkout-data"}},
            ],
        },
        "status": {
            "phase": "Running",
            "podIP": "10.244.3.17",
            "hostIP": "10.0.1.4",
            "startTime": "2026-08-01T09:15:04Z",
            "qosClass": "Burstable",
            "conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": "2026-08-01T09:15:30Z"},
            ],
            "initContainerStatuses": [
                {
                    "name": "migrate",
                    "image": "ghcr.io/acme/migrate:1.9.2",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"terminated": {"exitCode": 0, "reason": "Completed"}},
                },
            ],
            "containerStatuses": [
                {
                    "name": "app",
                    "image": "ghcr.io/acme/checkout:1.9.2",
                    "imageID": "docker-pullable://ghcr.io/acme/checkout@sha256:aaa",
                    "ready": True,
                    "restartCount": 3,
                    "state": {"running": {"startedAt": "2026-08-01T09:15:20Z"}},
                    "lastState": {
                        "terminated": {
                            "exitCode": 137,
                            "reason": "OOMKilled",
                            "finishedAt": "2026-08-01T09:15:18Z",
                        },
                    },
                },
                {
                    "name": "envoy",
                    "image": "envoyproxy/envoy:v1.29",
                    "ready": True,
                    "restartCount": 0,
                    "state": {"running": {"startedAt": "2026-08-01T09:15:19Z"}},
                },
            ],
        },
    }
    pod.update(overrides)
    return pod


class FakeApiServer:
    """``ApiClient.call_api`` answering per path, so a wrong read is a failure.

    Both endpoints under test issue GETs, and the pod and the metrics sample must
    be distinguishable: a fake that answered both identically would let a test
    pass while the code read the wrong object.
    """

    def __init__(self, *, pod=None, sample=None):
        self.requests: list[SimpleNamespace] = []
        self.pod = live_pod() if pod is None else pod
        self.sample = sample
        self.raises: dict[str, BaseException] = {}

    def __call__(self, path, method, **kwargs):
        self.requests.append(SimpleNamespace(path=path, method=method))
        if path in self.raises:
            raise self.raises[path]
        if path == POD_PATH:
            payload = self.pod
        elif path == METRICS_PATH:
            if self.sample is None:
                raise ApiException(status=404, reason="Not Found")
            payload = self.sample
        else:  # pragma: no cover - a read this module does not make
            raise AssertionError(f"unexpected {method} {path}")
        if kwargs.get("_return_http_data_only"):
            return payload
        return payload, 200, {}

    def paths(self):
        return [request.path for request in self.requests]


def sample(*, containers=(("app", "120m", "180Mi"), ("envoy", "15m", "40Mi")), window="30s"):
    """A ``PodMetrics`` object as metrics-server serves it."""
    return {
        "apiVersion": "metrics.k8s.io/v1beta1",
        "kind": "PodMetrics",
        "metadata": {"name": POD, "namespace": NAMESPACE},
        "timestamp": "2026-08-28T11:04:00Z",
        "window": window,
        "containers": [
            {"name": name, "usage": {"cpu": cpu, "memory": memory}}
            for name, cpu, memory in containers
        ],
    }


@pytest.fixture
def server(monkeypatch, fake_k8s):
    """A fake API server serving the pod, with ``metrics.k8s.io`` discovered."""
    api_server = FakeApiServer()
    fake_k8s.api_client.returns("call_api", api_server)
    monkeypatch.setattr(catalog, "discover", lambda: ([METRICS_CATALOG_ITEM], []))
    return api_server


@pytest.fixture(autouse=True)
def _clean_catalog_cache():
    """Discovery is cached per cluster for a minute; tests must not share it."""
    catalog.invalidate_cache(all_clusters=True)
    yield
    catalog.invalidate_cache(all_clusters=True)


# --------------------------------------------------------------------------- #
# §7.5 the detail
# --------------------------------------------------------------------------- #

def test_the_detail_carries_the_row_and_the_fields_a_table_has_no_room_for(server):
    row = pods_service.get_pod(NAMESPACE, POD)

    # The §6 row, unchanged.
    assert row["phase"] == "Running"
    assert row["ready"] == "2/2"
    assert row["restarts"] == 3
    assert row["node"] == "ip-10-0-1-4"

    # And the detail on top of it.
    assert row["service_account"] == "checkout"
    assert row["priority_class"] == "high"
    assert row["host_ip"] == "10.0.1.4"
    assert row["labels"] == {"app": "checkout"}
    assert [condition["type"] for condition in row["conditions"]] == ["Ready"]
    assert row["volumes"] == [
        {"name": "config", "kind": "configMap", "source": "checkout-config"},
        {"name": "data", "kind": "persistentVolumeClaim", "source": "checkout-data"},
    ]
    assert row["unavailable"] == [] and row["partial"] is False


def test_a_restart_carries_the_reason_the_previous_instance_died_of(server):
    """"Restarted 3 times" is the symptom; `OOMKilled` is the answer, and §6's
    row has nowhere to put it."""
    row = pods_service.get_pod(NAMESPACE, POD)

    app = next(c for c in row["containers"] if c["name"] == "app")
    assert app["last_terminated"]["reason"] == "OOMKilled"
    assert app["last_terminated"]["exit_code"] == 137
    assert app["requests"] == {"cpu": "250m", "memory": "256Mi"}
    assert app["ports"] == [
        {"name": "http", "container_port": 8080, "protocol": "TCP", "host_port": None},
    ]


def test_a_container_that_declares_no_resources_says_so_rather_than_zero(server):
    """A container with no requests is BestEffort — first in line to be evicted.
    Rendering that as `cpu: 0` describes it as having asked for nothing and got
    it, which is a different and much calmer fact."""
    row = pods_service.get_pod(NAMESPACE, POD)

    envoy = next(c for c in row["containers"] if c["name"] == "envoy")
    assert envoy["requests"] is None
    assert envoy["limits"] is None


def test_init_containers_are_their_own_list(server):
    """Merged into `containers`, a completed init container reads as a
    Terminated app container — a red row on every healthy pod that has ever run
    a migration."""
    row = pods_service.get_pod(NAMESPACE, POD)

    assert [c["name"] for c in row["containers"]] == ["app", "envoy"]
    (init,) = row["init_containers"]
    assert init == {
        **init,
        "name": "migrate",
        "kind": "init",
        "state": "Terminated",
        "reason": "Completed",
    }


def test_a_debug_container_does_not_move_the_ready_fraction(server):
    """The enrichment writes onto the shaper's entries rather than rebuilding
    them, so §7.4's rule survives: an ephemeral container is in the list and out
    of the arithmetic."""
    pod = live_pod()
    pod["spec"]["ephemeralContainers"] = [{"name": "debugger-x4k2p", "image": "busybox:1.36"}]
    pod["status"]["ephemeralContainerStatuses"] = [
        {"name": "debugger-x4k2p", "state": {"running": {"startedAt": "2026-08-28T10:00:00Z"}}},
    ]
    server.pod = pod

    row = pods_service.get_pod(NAMESPACE, POD)

    assert row["ready"] == "2/2"
    debugger = next(c for c in row["containers"] if c["name"] == "debugger-x4k2p")
    assert debugger["kind"] == "ephemeral"
    assert debugger["started_at"] == "2026-08-28T10:00:00Z"


def test_a_pod_that_could_not_be_read_raises_rather_than_rendering_a_shell(server):
    """§0.1 at the level of a whole page: an empty detail with a name at the top
    of it looks like a pod that exists and is empty."""
    server.raises[POD_PATH] = ApiException(status=403, reason="Forbidden")

    with pytest.raises(RBACDenied):
        pods_service.get_pod(NAMESPACE, POD)


def test_the_detail_is_served_over_http(client, cluster_id, server):
    response = client.get(f"/api/pods/{NAMESPACE}/{POD}", params={"cluster_id": cluster_id})

    assert response.status_code == 200
    assert response.json()["name"] == POD


# --------------------------------------------------------------------------- #
# §7.6 the environment
# --------------------------------------------------------------------------- #

def _configmap(data):
    return obj(data=data, binary_data=None)


def _secret(keys):
    return obj(data={key: SECRET_VALUE for key in keys}, string_data=None)


@pytest.fixture
def sources(fake_k8s):
    """The ConfigMap and Secret this pod's containers reference."""
    fake_k8s.core_v1.returns(
        "read_namespaced_config_map",
        _configmap({"flags": "checkout-v2", "TIMEOUT": "30s"}),
    )
    fake_k8s.core_v1.returns("read_namespaced_secret", _secret(["password", "username"]))
    return fake_k8s.core_v1


def variables(result, container):
    entry = next(item for item in result["items"] if item["container"] == container)
    return {variable["name"]: variable for variable in entry["variables"] if variable["name"]}


def test_a_literal_a_configmap_a_secret_and_a_fieldref_are_four_different_answers(server, sources):
    result = pods_service.get_pod_environment(NAMESPACE, POD)
    app = variables(result, "app")

    assert app["LOG_LEVEL"]["value"] == "info"
    assert app["LOG_LEVEL"]["value_state"] == "literal"

    assert app["FEATURE_FLAGS"]["value"] == "checkout-v2"
    assert app["FEATURE_FLAGS"]["value_state"] == "resolved"

    # Withheld, not empty. An empty value would say the container starts with a
    # blank password, which is a bug report somebody would go and file.
    assert app["DB_PASSWORD"]["value"] is None
    assert app["DB_PASSWORD"]["value_state"] == "withheld"
    assert app["DB_PASSWORD"]["source"] == {
        "kind": "secretKeyRef", "name": "checkout-db", "key": "password", "optional": None,
    }

    # The kubelet substitutes this when the container starts; the API server
    # never stores the result, so there is no value and we do not invent one.
    assert app["POD_IP"]["value"] is None
    assert app["POD_IP"]["value_state"] == "runtime"
    assert app["POD_IP"]["source"]["name"] == "status.podIP"


def test_a_secret_key_reference_is_answered_without_reading_the_secret(server, sources):
    """The key is already in the spec and the value is never returned, so the
    read would add nothing — and not making it is why this tab renders in full on
    a console with no `get secrets` grant."""
    pods_service.get_pod_environment(NAMESPACE, POD)

    assert sources.called("read_namespaced_secret") == []


def test_no_secret_value_reaches_the_response_even_through_env_from(server, sources):
    """An environment viewer is the screen people share. Asserted over the whole
    serialised body rather than field by field, so a future field cannot
    reintroduce the leak past a narrower assertion."""
    pod = live_pod()
    pod["spec"]["containers"][0]["envFrom"] = [
        {"configMapRef": {"name": "checkout-config"}},
        {"secretRef": {"name": "checkout-db"}, "prefix": "DB_"},
    ]
    server.pod = pod

    result = pods_service.get_pod_environment(NAMESPACE, POD)

    assert SECRET_VALUE not in json.dumps(result)
    app = variables(result, "app")
    # The key names are there — they are not the secret, and without them the
    # rows would be unidentifiable.
    assert app["DB_password"]["value_state"] == "withheld"
    assert app["DB_username"]["value_state"] == "withheld"


def test_an_unreadable_configmap_leaves_a_named_blank_not_a_missing_variable(server, fake_k8s):
    """§0.1 inside a container's environment, which is the last place anybody
    would think to look for a swallowed error."""
    fake_k8s.core_v1.raises(
        "read_namespaced_config_map", ApiException(status=403, reason="Forbidden"),
    )

    result = pods_service.get_pod_environment(NAMESPACE, POD)
    app = variables(result, "app")

    assert app["FEATURE_FLAGS"]["value"] is None
    assert app["FEATURE_FLAGS"]["value_state"] == "unreadable"
    assert result["partial"] is True
    (entry,) = result["unavailable"]
    assert entry["resource"] == "configmaps" and entry["reason"] == "forbidden"


def test_a_key_the_configmap_does_not_have_is_absent_not_unreadable(server, fake_k8s):
    """The ConfigMap was read and has no such key: unless the reference is
    optional the kubelet refuses to start the container, and calling that
    "unreadable" sends somebody to check RBAC that is already correct."""
    fake_k8s.core_v1.returns("read_namespaced_config_map", obj(data={"other": "x"}, binary_data=None))

    result = pods_service.get_pod_environment(NAMESPACE, POD)
    app = variables(result, "app")

    assert app["FEATURE_FLAGS"]["value_state"] == "absent"
    assert app["FEATURE_FLAGS"]["value"] is None
    # Nothing failed, so nothing is in `unavailable` — the read succeeded.
    assert result["unavailable"] == [] and result["partial"] is False


def test_an_unreadable_env_from_says_a_set_of_variables_is_missing(server, fake_k8s):
    """We know variables are coming from that object and cannot name them.
    Reporting zero of them would be the confident wrong answer."""
    fake_k8s.core_v1.raises(
        "read_namespaced_config_map", ApiException(status=404, reason="Not Found"),
    )

    result = pods_service.get_pod_environment(NAMESPACE, POD)
    entry = next(item for item in result["items"] if item["container"] == "app")
    wholesale = [variable for variable in entry["variables"] if variable["all_keys"]]

    assert len(wholesale) == 1
    assert wholesale[0]["value_state"] == "unreadable"
    assert wholesale[0]["source"]["kind"] == "configMapRef"


def test_each_referenced_object_is_read_once_however_many_containers_name_it(server, sources):
    """Eight containers naming one ConfigMap is eight chances to be denied and
    eight identical lines in the banner."""
    pod = live_pod()
    pod["spec"]["containers"][1]["envFrom"] = [{"configMapRef": {"name": "checkout-config"}}]
    server.pod = pod

    pods_service.get_pod_environment(NAMESPACE, POD)

    assert len(sources.called("read_namespaced_config_map")) == 1


def test_an_env_entry_shadowing_an_env_from_key_marks_the_loser(server, sources):
    """"This ConfigMap defines TIMEOUT and something else is overriding it" is
    the answer to a question people spend an afternoon on."""
    pod = live_pod()
    pod["spec"]["containers"][0]["env"].append({"name": "TIMEOUT", "value": "5s"})
    server.pod = pod

    result = pods_service.get_pod_environment(NAMESPACE, POD)
    entry = next(item for item in result["items"] if item["container"] == "app")
    timeouts = [variable for variable in entry["variables"] if variable["name"] == "TIMEOUT"]

    assert [variable["overridden"] for variable in timeouts] == [True, False]
    assert timeouts[-1]["value"] == "5s"


def test_the_environment_is_served_over_http(client, cluster_id, server, sources):
    response = client.get(
        f"/api/pods/{NAMESPACE}/{POD}/environment", params={"cluster_id": cluster_id},
    )

    assert response.status_code == 200
    assert [item["container"] for item in response.json()["items"]] == ["migrate", "app", "envoy"]


# --------------------------------------------------------------------------- #
# §7.7 the metrics
# --------------------------------------------------------------------------- #

def test_usage_is_reported_per_container_against_what_it_asked_for(server):
    server.sample = sample()

    result = pods_service.get_pod_metrics(NAMESPACE, POD)
    app = next(item for item in result["items"] if item["container"] == "app")

    assert app["usage"] == {"cpu_cores": pytest.approx(0.12), "memory_bytes": 180 * 1024**2}
    assert app["requests"] == {"cpu": "250m", "memory": "256Mi"}
    # app + envoy. `migrate` has terminated, so it is skipped rather than
    # treated as a hole in the arithmetic.
    assert result["pod"]["cpu_cores"] == pytest.approx(0.135)
    assert result["window_seconds"] == 30
    assert result["partial"] is False


def test_a_cluster_with_no_metrics_api_is_a_fact_not_an_error(monkeypatch, server):
    """§1.2's `unsupported`, which the UI renders as "not present on this
    cluster" and deliberately does not colour red. The requests still render,
    because they come from the pod."""
    monkeypatch.setattr(catalog, "discover", lambda: ([], []))

    result = pods_service.get_pod_metrics(NAMESPACE, POD)

    assert result["partial"] is True
    (entry,) = result["unavailable"]
    assert entry["reason"] == "unsupported" and entry["group"] == "metrics.k8s.io"
    assert all(item["usage"] is None for item in result["items"])
    assert result["pod"] == {"cpu_cores": None, "memory_bytes": None}
    app = next(item for item in result["items"] if item["container"] == "app")
    assert app["requests"] == {"cpu": "250m", "memory": "256Mi"}


def test_a_pod_with_no_sample_yet_is_not_reported_as_a_missing_pod(server):
    """The metrics API answers 404 for a pod it has not scraped. Relayed as-is it
    would read as "this pod is gone", next to the pod's own name."""
    server.sample = None  # the fake raises 404 for the metrics path

    result = pods_service.get_pod_metrics(NAMESPACE, POD)

    (entry,) = result["unavailable"]
    assert entry["reason"] == "not_found"
    assert "no sample for this pod yet" in entry["detail"]
    assert all(item["usage"] is None for item in result["items"])


def test_a_container_missing_from_the_sample_makes_the_pod_total_unknown(server):
    """Summing the parts we have and calling it the pod's usage understates it by
    however much the missing container is using, with nothing in the response
    saying a container is missing from the arithmetic."""
    server.sample = sample(containers=(("app", "120m", "180Mi"),))

    result = pods_service.get_pod_metrics(NAMESPACE, POD)

    envoy = next(item for item in result["items"] if item["container"] == "envoy")
    assert envoy["usage"] is None and envoy["sample_expected"] is True
    assert result["pod"] == {"cpu_cores": None, "memory_bytes": None}


def test_a_finished_init_container_is_not_counted_as_a_missing_sample(server):
    """metrics-server does not report a terminated init container, and requiring
    one would make every pod that has ever run a migration report unknown usage
    forever."""
    result = pods_service.get_pod_metrics(NAMESPACE, POD)
    migrate = next(item for item in result["items"] if item["container"] == "migrate")

    assert migrate["usage"] is None and migrate["sample_expected"] is False


def test_a_running_sidecar_init_container_does_count(server):
    """A `restartPolicy: Always` init container runs for the life of the pod and
    is using real resources. Leaving it out of the total understates the pod by
    however much the sidecar is using."""
    pod = live_pod()
    pod["status"]["initContainerStatuses"][0]["state"] = {
        "running": {"startedAt": "2026-08-01T09:15:05Z"},
    }
    server.pod = pod
    server.sample = sample()  # samples app and envoy, not migrate

    result = pods_service.get_pod_metrics(NAMESPACE, POD)
    migrate = next(item for item in result["items"] if item["container"] == "migrate")

    assert migrate["sample_expected"] is True
    assert result["pod"] == {"cpu_cores": None, "memory_bytes": None}


def test_a_discovery_we_could_not_read_is_never_reported_as_no_metrics(monkeypatch, server):
    """"This cluster has no metrics" and "we could not find out" send an operator
    to two different places, and only one of them is an install."""
    def blind():
        return [], [{
            "group": "*", "resource": "*", "namespace": None,
            "reason": "forbidden", "detail": "list of API groups was refused",
        }]

    monkeypatch.setattr(catalog, "discover", blind)

    result = pods_service.get_pod_metrics(NAMESPACE, POD)

    (entry,) = result["unavailable"]
    assert entry["reason"] == "forbidden"


def test_an_unparseable_quantity_is_unknown_rather_than_zero(server):
    """A spelling this console does not understand has told us nothing about the
    container. Zero would say it is idle."""
    server.sample = sample(containers=(("app", "120q", "180Mi"),), window="not-a-duration")

    result = pods_service.get_pod_metrics(NAMESPACE, POD)
    app = next(item for item in result["items"] if item["container"] == "app")

    assert app["usage"] == {"cpu_cores": None, "memory_bytes": 180 * 1024**2}
    assert result["pod"]["cpu_cores"] is None
    assert result["window_seconds"] is None


def test_the_metrics_are_served_over_http(client, cluster_id, server):
    server.sample = sample()

    response = client.get(
        f"/api/pods/{NAMESPACE}/{POD}/metrics", params={"cluster_id": cluster_id},
    )

    assert response.status_code == 200
    assert response.json()["timestamp"] == "2026-08-28T11:04:00Z"


def test_the_served_metrics_version_comes_from_discovery(monkeypatch, server):
    """Pinning v1beta1 would refuse a cluster that has moved on, while claiming
    the pin is the contract."""
    monkeypatch.setattr(
        catalog, "discover",
        lambda: ([{**METRICS_CATALOG_ITEM, "version": "v1", "preferred": True}], []),
    )
    server.sample = sample()
    server.raises = {}

    with pytest.raises(AssertionError, match="unexpected GET"):
        pods_service.get_pod_metrics(NAMESPACE, POD)


def test_resolve_owns_the_diagnosis_when_the_group_is_absent(monkeypatch, fake_k8s):
    """The unsupported error names the group rather than the console's own
    guess at what is missing."""
    monkeypatch.setattr(catalog, "discover", lambda: ([], []))

    with pytest.raises(Unsupported) as raised:
        pods_service._metrics_version()

    assert "metrics.k8s.io" in raised.value.message
