"""
Cluster registration, connection test and overview (§3).

Three properties carry most of the weight here:

* **The token never comes back.** Asserted by scanning whole response bodies for
  the stored secret rather than by checking that a named field is absent — a
  field-name check passes the day someone adds a debug field or echoes the
  request back.
* **A failed connection test is a result, not an error.** 200 with
  ``reachable: false`` and an error envelope saying why, and ``permissions: null``
  rather than ``[]``, because an empty list would assert something about RBAC
  that we never got close enough to observe.
* **One failing overview collector nulls its own key.** Never zero, never a 500,
  and always with an ``unavailable`` entry naming what could not be read.
"""

from __future__ import annotations

import sys
import types

import pytest

from app import database
from app.crypto import decrypt
from app.errors import ClusterUnreachable, RBACDenied
from app.models import Cluster
from tests.conftest import FIXTURE_TOKEN, obj


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _contains(value, needle: str) -> bool:
    """Does ``needle`` appear anywhere in a decoded JSON structure?

    Recursive on purpose. The interesting leak is not ``{"token": "..."}``, which
    anyone would notice; it is a credential that arrives nested inside a
    diagnostic detail string or an error context three levels down.
    """
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(_contains(v, needle) for v in value.values()) or any(
            _contains(k, needle) for k in value
        )
    if isinstance(value, (list, tuple)):
        return any(_contains(item, needle) for item in value)
    return False


def _stored_token(cluster_id: int) -> str:
    session = database.SessionLocal()
    try:
        return decrypt(session.get(Cluster, cluster_id).token_encrypted)
    finally:
        session.close()


CREATE_BODY = {
    "name": "prod-eu",
    "platform": "kubernetes",
    "api_server": "https://api.prod-eu.example:6443",
    "authentication_type": "service_account_token",
    "token": FIXTURE_TOKEN,
    "ca_certificate": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
    "skip_tls_verify": False,
}


@pytest.fixture
def stub_preflight(monkeypatch):
    """Stand in for ``app.admin.preflight``, which another module owns.

    Registered in ``sys.modules`` because ``/test`` imports it lazily inside the
    handler. These tests are about what registration does with the preflight
    results, not about the review itself.
    """
    package = types.ModuleType("app.admin")
    module = types.ModuleType("app.admin.preflight")
    recorded: list[list[dict]] = []

    def check_many(checks):
        recorded.append(checks)
        return [
            {
                "verb": check["verb"],
                "group": check.get("group", ""),
                "resource": check["resource"],
                "namespace": check.get("namespace"),
                "allowed": check["verb"] == "list",
                "reason": "no RBAC policy matched" if check["verb"] != "list" else "allowed",
                "evaluationError": None,
                "hint": None,
            }
            for check in checks
        ]

    module.check_many = check_many
    package.preflight = module
    monkeypatch.setitem(sys.modules, "app.admin", package)
    monkeypatch.setitem(sys.modules, "app.admin.preflight", module)
    module.recorded = recorded
    return module


@pytest.fixture
def plain_kubernetes(monkeypatch):
    """Discovery answers, and says this cluster is not OpenShift.

    §13's connection test asks the cluster whether it publishes a wildcard app
    domain, which only OpenShift does. Stubbed rather than left to the fake's
    assertion because the branch under test is the *ordinary* one — a plain
    Kubernetes cluster has no `config.openshift.io`, and the test needs that to
    stay a quiet `None` rather than becoming an `unavailable` entry. A fake that
    answered anything here would hide the difference.
    """
    from app.resources import catalog

    def fake_raw_get(path, *, query=None):
        if path == "/api":
            return {"versions": ["v1"]}
        if path == "/apis":
            # No groups at all: the smallest cluster that is still a cluster,
            # and the one where `config.openshift.io` is unambiguously absent
            # rather than merely unread.
            return {"groups": []}
        if path == "/api/v1":
            return {"resources": []}
        raise AssertionError(f"discovery asked for an unstubbed path: {path}")

    monkeypatch.setattr(catalog, "raw_get", fake_raw_get)
    return fake_raw_get


@pytest.fixture
def healthy_cluster(fake_k8s):
    """A fake cluster that answers every overview collector."""
    fake_k8s.version_api.returns("get_code", obj(git_version="v1.31.4"))
    fake_k8s.core_v1.returns("list_node", obj(items=[
        obj(
            spec=obj(unschedulable=None),
            status=obj(
                conditions=[obj(type="Ready", status="True")],
                capacity={"cpu": "16", "memory": "64Gi", "pods": "110"},
            ),
        ),
        obj(
            spec=obj(unschedulable=True),
            status=obj(
                conditions=[obj(type="Ready", status="False")],
                capacity={"cpu": "8", "memory": "32Gi", "pods": "110"},
            ),
        ),
    ]))
    fake_k8s.core_v1.returns("list_namespace", obj(items=[obj(), obj(), obj()]))
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[
        obj(status=obj(phase="Running"), spec=obj(containers=[
            obj(resources=obj(requests={"cpu": "500m", "memory": "512Mi"})),
            obj(resources=obj(requests={"cpu": "250m", "memory": "256Mi"})),
        ])),
        obj(status=obj(phase="Pending"), spec=obj(containers=[
            obj(resources=obj(requests={"cpu": "1", "memory": "1Gi"})),
        ])),
        # Terminal: holds nothing, and counting it would overstate commitment.
        obj(status=obj(phase="Succeeded"), spec=obj(containers=[
            obj(resources=obj(requests={"cpu": "4", "memory": "8Gi"})),
        ])),
    ]))
    fake_k8s.apps_v1.returns("list_deployment_for_all_namespaces", obj(items=[obj()] * 5))
    fake_k8s.apps_v1.returns("list_stateful_set_for_all_namespaces", obj(items=[obj()] * 2))
    fake_k8s.apps_v1.returns("list_daemon_set_for_all_namespaces", obj(items=[obj()]))
    fake_k8s.batch_v1.returns("list_job_for_all_namespaces", obj(items=[]))
    fake_k8s.batch_v1.returns("list_cron_job_for_all_namespaces", obj(items=[obj()] * 3))
    return fake_k8s


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def test_create_returns_the_public_shape(client):
    response = client.post("/api/clusters", json=CREATE_BODY)
    assert response.status_code == 201

    body = response.json()
    assert set(body) == {
        "id", "name", "platform", "api_server", "authentication_type",
        "has_ca_certificate", "skip_tls_verify", "app_domain", "status",
        "server_version", "last_connected", "created_at", "updated_at",
    }
    assert body["has_ca_certificate"] is True
    # Never tested yet — and that is not the same as "failed".
    assert body["status"] == "unknown"
    assert body["server_version"] is None
    assert body["created_at"].endswith("Z")


def test_the_token_never_appears_in_any_clusters_response(
    client, stub_preflight, healthy_cluster, plain_kubernetes
):
    """The central guarantee of §3, checked across every response the resource
    can produce, including the error path."""
    created = client.post("/api/clusters", json=CREATE_BODY)
    cluster_id = created.json()["id"]

    responses = [
        created,
        client.get("/api/clusters"),
        client.put(f"/api/clusters/{cluster_id}", json={"name": "prod-eu-2"}),
        client.put(f"/api/clusters/{cluster_id}", json={"token": FIXTURE_TOKEN}),
        client.post(f"/api/clusters/{cluster_id}/test"),
        client.get(f"/api/clusters/{cluster_id}/overview"),
    ]

    for response in responses:
        assert not _contains(response.json(), FIXTURE_TOKEN), (
            f"{response.request.method} {response.request.url} leaked the token"
        )
        # The CA is public material, but it is stored input and echoing stored
        # input back is how a field that is harmless today becomes a leak later.
        assert not _contains(response.json(), "BEGIN CERTIFICATE")


def test_list_returns_the_standard_envelope(client):
    client.post("/api/clusters", json=CREATE_BODY)
    body = client.get("/api/clusters").json()

    assert set(body) >= {"items", "continue", "remaining", "partial", "unavailable"}
    assert len(body["items"]) == 1
    assert body["partial"] is False
    assert body["unavailable"] == []


def test_update_without_a_token_keeps_the_stored_one(client, registered_cluster):
    """A UI that round-trips the whole object must not wipe the credential."""
    response = client.put(
        f"/api/clusters/{registered_cluster.id}",
        json={"name": "prod-eu-renamed", "api_server": "https://api.new.example:6443"},
    )
    assert response.status_code == 200
    assert response.json()["name"] == "prod-eu-renamed"
    assert _stored_token(registered_cluster.id) == FIXTURE_TOKEN


def test_update_with_a_token_replaces_it(client, registered_cluster):
    client.put(f"/api/clusters/{registered_cluster.id}", json={"token": "rotated-token"})
    assert _stored_token(registered_cluster.id) == "rotated-token"


def test_update_refuses_an_explicitly_empty_token(client, registered_cluster):
    """Distinct from omitting it: the operator asked for something impossible,
    and silently keeping the old token would be a different action than the one
    they requested."""
    response = client.put(f"/api/clusters/{registered_cluster.id}", json={"token": ""})
    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
    assert _stored_token(registered_cluster.id) == FIXTURE_TOKEN


def test_editing_a_cluster_invalidates_its_cached_transport(client, registered_cluster):
    """``updated_at`` is the client cache key, so an edit must rebuild the client.

    Without this, changing a cluster's API server or rotating its token keeps
    talking to the old endpoint with the old credential until someone restarts
    the process — while the UI, showing the new address, attributes the old
    cluster's data to it.
    """
    from app.k8s.client import manager

    original = manager.get_clients(registered_cluster.id)
    assert manager._cache[registered_cluster.id] is original

    client.put(f"/api/clusters/{registered_cluster.id}", json={"token": "rotated"})
    assert registered_cluster.id not in manager._cache

    rebuilt = manager.get_clients(registered_cluster.id)
    assert rebuilt.cache_key != original.cache_key


def test_duplicate_name_is_a_conflict(client):
    client.post("/api/clusters", json=CREATE_BODY)
    response = client.post("/api/clusters", json=CREATE_BODY)
    assert response.status_code == 409
    assert response.json()["error"] == "conflict"


def test_delete_removes_the_cluster(client, registered_cluster):
    assert client.delete(f"/api/clusters/{registered_cluster.id}").status_code == 204
    assert client.get("/api/clusters").json()["items"] == []
    assert client.put(f"/api/clusters/{registered_cluster.id}", json={"name": "x"}).status_code == 404


@pytest.mark.parametrize(
    "payload,field",
    [
        ({**CREATE_BODY, "api_server": "api.example:6443"}, "api_server"),
        ({**CREATE_BODY, "authentication_type": "oidc"}, "authentication_type"),
    ],
)
def test_a_registration_that_could_never_build_a_client_is_rejected_at_the_form(
    client, payload, field
):
    """Rejected here, not at the first request — where the error would be about
    an unreachable cluster and would send the operator to look at their network."""
    response = client.post("/api/clusters", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert body["context"]["field"] == field


def test_missing_cluster_is_not_found(client):
    response = client.get("/api/clusters/999/overview")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #

def test_test_reports_reachability_version_and_baseline_permissions(
    client, registered_cluster, fake_k8s, stub_preflight, plain_kubernetes
):
    fake_k8s.version_api.returns("get_code", obj(git_version="v1.31.4"))

    response = client.post(f"/api/clusters/{registered_cluster.id}/test")
    assert response.status_code == 200

    body = response.json()
    assert body["reachable"] is True
    assert body["server_version"] == "v1.31.4"
    assert isinstance(body["latency_ms"], (int, float))

    from app.api.clusters import BASELINE_PREFLIGHT_CHECKS

    assert len(body["permissions"]) == len(BASELINE_PREFLIGHT_CHECKS)
    # §9's baseline set is not only "can we read": it must also answer which
    # buttons will work, so a half-permissioned ServiceAccount shows up now
    # rather than mid-incident.
    submitted = {(c["verb"], c.get("group"), c["resource"]) for c in stub_preflight.recorded[0]}
    assert ("patch", "apps", "deployments") in submitted
    assert ("create", "core", "pods") in submitted
    assert ("get", "core", "secrets") in submitted


def test_a_successful_test_records_connectivity_on_the_cluster(
    client, registered_cluster, fake_k8s, stub_preflight, plain_kubernetes
):
    fake_k8s.version_api.returns("get_code", obj(git_version="v1.30.2"))
    client.post(f"/api/clusters/{registered_cluster.id}/test")

    row = client.get("/api/clusters").json()["items"][0]
    assert row["status"] == "connected"
    assert row["server_version"] == "v1.30.2"
    assert row["last_connected"] is not None


def test_an_unreachable_cluster_is_a_result_not_a_500(client, registered_cluster, fake_k8s):
    fake_k8s.version_api.raises(
        "get_code",
        ClusterUnreachable(detail="NameResolutionError: api.prod-eu.example"),
    )

    response = client.post(f"/api/clusters/{registered_cluster.id}/test")
    assert response.status_code == 200

    body = response.json()
    assert body["reachable"] is False
    assert body["server_version"] is None
    assert body["error"]["error"] == "cluster_unreachable"
    assert "NameResolutionError" in body["error"]["detail"]
    # null, not []: an empty list would claim the ServiceAccount holds none of
    # the baseline permissions. We never got close enough to ask.
    assert body["permissions"] is None


def test_a_failed_test_is_recorded_as_disconnected_with_a_reason(
    client, registered_cluster, fake_k8s
):
    fake_k8s.version_api.raises("get_code", ClusterUnreachable(detail="connection refused"))
    client.post(f"/api/clusters/{registered_cluster.id}/test")

    assert client.get("/api/clusters").json()["items"][0]["status"] == "disconnected"
    degraded = client.get("/api/health").json()["degraded"]
    assert degraded[0]["reason"] == "unreachable"


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #

def test_overview_collects_every_sub_object(client, registered_cluster, healthy_cluster):
    response = client.get(f"/api/clusters/{registered_cluster.id}/overview")
    assert response.status_code == 200

    body = response.json()
    assert body["server_version"] == "v1.31.4"
    assert body["platform"] == "kubernetes"
    assert body["nodes"] == {"total": 2, "ready": 1, "unschedulable": 1}
    assert body["namespaces"] == 3
    assert body["workloads"] == {
        "deployments": 5, "statefulsets": 2, "daemonsets": 1, "jobs": 0, "cronjobs": 3,
    }
    assert body["pods"] == {
        "total": 3, "running": 1, "pending": 1, "failed": 0, "succeeded": 1,
    }
    assert body["capacity"] == {
        "cpu_cores": 24.0, "memory_bytes": 96 * 2 ** 30, "pods": 220,
    }
    # 500m + 250m + 1 = 1.75 cores; the Succeeded pod holds nothing.
    assert body["requested"]["cpu_cores"] == 1.75
    assert body["requested"]["memory_bytes"] == (512 + 256) * 2 ** 20 + 2 ** 30
    assert body["unavailable"] == []


def test_one_failing_collector_nulls_its_own_key_and_says_so(
    client, registered_cluster, healthy_cluster
):
    """The canonical degradation case (§3). 200, a null key, an unavailable entry
    — and crucially not a zero, which would render as a cluster with no nodes."""
    healthy_cluster.core_v1.raises("list_node", RBACDenied(
        "The console cannot list nodes on this cluster.",
        detail='nodes is forbidden: User "sa" cannot list nodes',
    ))

    response = client.get(f"/api/clusters/{registered_cluster.id}/overview")
    assert response.status_code == 200

    body = response.json()
    assert body["nodes"] is None
    # Capacity is derived from the same listing, so it is unknown too — and the
    # failure is reported once, because there was one fault, not two.
    assert body["capacity"] is None
    assert len(body["unavailable"]) == 1

    entry = body["unavailable"][0]
    assert entry["resource"] == "nodes"
    assert entry["reason"] == "forbidden"
    assert "cannot list nodes" in entry["detail"]

    # Everything else still answered. Degradation is isolated, never total.
    assert body["namespaces"] == 3
    assert body["workloads"]["deployments"] == 5
    assert body["pods"]["total"] == 3


def test_every_collector_can_fail_independently(client, registered_cluster, healthy_cluster):
    healthy_cluster.version_api.raises("get_code", ClusterUnreachable())
    healthy_cluster.core_v1.raises("list_namespace", RBACDenied())
    healthy_cluster.apps_v1.raises("list_deployment_for_all_namespaces", RBACDenied())
    healthy_cluster.core_v1.raises("list_pod_for_all_namespaces", RBACDenied())

    body = client.get(f"/api/clusters/{registered_cluster.id}/overview").json()

    assert body["server_version"] is None
    assert body["namespaces"] is None
    assert body["workloads"] is None
    assert body["pods"] is None
    assert body["requested"] is None
    # The one collector that did answer still answers.
    assert body["nodes"]["total"] == 2
    assert len(body["unavailable"]) == 4


def test_an_unparseable_quantity_withholds_the_total_rather_than_undercounting(
    client, registered_cluster, healthy_cluster
):
    """A total that silently skipped what it could not parse is a number an
    operator will make a capacity decision on, and it would be wrong with no
    indication that it was."""
    healthy_cluster.core_v1.returns("list_node", obj(items=[
        obj(spec=obj(unschedulable=False), status=obj(
            conditions=[obj(type="Ready", status="True")],
            capacity={"cpu": "16", "memory": "sixty-four gigs", "pods": "110"},
        )),
    ]))

    body = client.get(f"/api/clusters/{registered_cluster.id}/overview").json()
    assert body["capacity"] is None
    assert body["nodes"] is None
    assert body["unavailable"][0]["resource"] == "nodes"
