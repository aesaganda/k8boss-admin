"""
The workload endpoints (§6).

Two things are asserted here that no amount of row-shaping unit testing can
reach:

* **A read that failed is reported, not absorbed.** One kind the console may not
  list must degrade to an ``unavailable`` entry with ``partial: true`` while the
  other five still answer, and a pod listing that fails must turn every
  ``restarts_24h`` into ``null`` rather than ``0``. An endpoint that returns
  ``items: []`` for a listing it was refused is the defect this project is named
  after.
* **An action a kind cannot have is refused before the cluster is touched.**
  Forwarding a scale to a DaemonSet returns the API server's own 404 on the
  missing ``/scale`` subresource, which reads as "your DaemonSet is gone". The
  console answers 422 naming the kind instead.

The write paths are exercised only as far as the delegation into
``app.admin.*``: the single write funnel is tested where it lives, and asserting
it twice would let the copy here go stale and start passing for the wrong
reason.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from kubernetes.client.rest import ApiException

import app.api.workloads as workloads_api
from tests.conftest import obj

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _template(image="ghcr.io/acme/checkout:1.9.2", labels=None):
    return obj(
        metadata=obj(labels=labels if labels is not None else {"app": "checkout"}),
        spec=obj(
            containers=[obj(
                name="app", image=image,
                ports=[obj(name="http", container_port=8080, protocol="TCP")],
                resources=obj(requests={"cpu": "500m"}, limits={"cpu": "1"}),
                env=[obj(name="LOG_LEVEL")],
                readiness_probe=obj(), liveness_probe=obj(),
            )],
            init_containers=[obj(name="migrate", image="ghcr.io/acme/migrations:1.9.2")],
            service_account_name="checkout",
            node_selector={"kubernetes.io/os": "linux"},
            volumes=[obj(name="cache", empty_dir=obj())],
            tolerations=[],
        ),
    )


def _deployment(name="checkout", namespace="prod", desired=2, ready=2):
    return obj(
        metadata=obj(
            name=name, namespace=namespace, uid="dep-uid", generation=4,
            labels={"app": name}, annotations={"deployment.kubernetes.io/revision": "14"},
            creation_timestamp=NOW - timedelta(days=7),
        ),
        spec=obj(
            replicas=desired,
            selector=obj(match_labels={"app": name}, match_expressions=[]),
            template=_template(labels={"app": name}),
            strategy=obj(type="RollingUpdate",
                         rolling_update=obj(max_surge="25%", max_unavailable="25%")),
        ),
        status=obj(
            observed_generation=4, ready_replicas=ready, updated_replicas=desired,
            available_replicas=ready,
            conditions=[obj(type="Available", status="True", reason="MinimumReplicasAvailable",
                            message="Deployment has minimum availability.",
                            last_transition_time=NOW - timedelta(days=1))],
        ),
    )


def _statefulset():
    return obj(
        metadata=obj(name="pg", namespace="data", uid="sts-uid", generation=1, labels={},
                     annotations={}, creation_timestamp=NOW),
        spec=obj(replicas=1, selector=obj(match_labels={"app": "pg"}, match_expressions=[]),
                 template=_template("postgres:16", labels={"app": "pg"}),
                 update_strategy=obj(type="RollingUpdate", rolling_update=obj(partition=0))),
        status=obj(observed_generation=1, ready_replicas=1, updated_replicas=1,
                   available_replicas=1),
    )


def _pod(name="checkout-7d9-abc", restarts=0, labels=None, owner_uid=None):
    return obj(
        metadata=obj(
            name=name, namespace="prod", labels=labels or {"app": "checkout"},
            creation_timestamp=NOW - timedelta(hours=2),
            owner_references=[obj(kind="ReplicaSet", name="checkout-7d9", uid=owner_uid)]
            if owner_uid else [],
        ),
        spec=obj(node_name="ip-10-0-1-4", containers=[obj(name="app")]),
        status=obj(
            phase="Running", pod_ip="10.4.2.9", qos_class="Burstable",
            start_time=NOW - timedelta(hours=2),
            container_statuses=[obj(name="app", image="ghcr.io/acme/checkout:1.9.2",
                                    ready=True, restart_count=restarts,
                                    state=obj(running=obj(started_at=NOW)),
                                    last_state=obj())],
            init_container_statuses=[],
        ),
    )


def _service():
    return obj(
        metadata=obj(name="checkout", namespace="prod"),
        spec=obj(type="ClusterIP", cluster_ip="10.96.0.14", selector={"app": "checkout"},
                 ports=[obj(name="http", port=80, target_port=8080, protocol="TCP",
                            node_port=None)]),
    )


def _empty(*_args, **_kwargs):
    # Positional as well as keyword: the namespaced listings are called
    # `list_namespaced_deployment("prod")`, which is what
    # test_a_namespace_filter_uses_the_namespaced_listings asserts on. A
    # keyword-only stub raised TypeError inside the request and surfaced as a
    # 500, so the test failed for the one reason it was not testing.
    return obj(items=[])


def _stub_listings(fake, *, deployments=(), statefulsets=(), daemonsets=(), jobs=(),
                   cronjobs=(), replicasets=(), pods=()):
    """Stub every listing the unfiltered workload endpoint makes.

    All six plus pods, always: the fake raises on an unstubbed call by design, so
    a listing this endpoint makes and a test forgets is a loud failure rather
    than a quietly missing row.
    """
    fake.apps_v1.returns("list_deployment_for_all_namespaces", obj(items=list(deployments)))
    fake.apps_v1.returns("list_stateful_set_for_all_namespaces", obj(items=list(statefulsets)))
    fake.apps_v1.returns("list_daemon_set_for_all_namespaces", obj(items=list(daemonsets)))
    fake.apps_v1.returns("list_replica_set_for_all_namespaces", obj(items=list(replicasets)))
    fake.batch_v1.returns("list_job_for_all_namespaces", obj(items=list(jobs)))
    fake.batch_v1.returns("list_cron_job_for_all_namespaces", obj(items=list(cronjobs)))
    fake.core_v1.returns("list_pod_for_all_namespaces", obj(items=list(pods)))


# --------------------------------------------------------------------------- #
# GET /api/workloads
# --------------------------------------------------------------------------- #

def test_the_list_spans_every_kind_in_one_envelope(client, fake_k8s):
    _stub_listings(fake_k8s, deployments=[_deployment()], statefulsets=[_statefulset()],
                   pods=[_pod()])

    body = client.get("/api/workloads").json()

    assert [(row["kind"], row["name"]) for row in body["items"]] == [
        ("StatefulSet", "pg"), ("Deployment", "checkout"),
    ], "rows are sorted by namespace, then kind, then name — a stable table"
    assert body["partial"] is False
    assert body["unavailable"] == []
    assert body["continue"] is None


def test_one_forbidden_kind_degrades_that_kind_and_nothing_else(client, fake_k8s):
    """A half-permissioned ServiceAccount still gets a useful page, and the row
    it cannot see is named rather than absent."""
    _stub_listings(fake_k8s, deployments=[_deployment()], pods=[_pod()])
    fake_k8s.apps_v1.raises(
        "list_daemon_set_for_all_namespaces",
        ApiException(status=403, reason="Forbidden"),
    )

    response = client.get("/api/workloads")
    body = response.json()

    assert response.status_code == 200
    assert [row["kind"] for row in body["items"]] == ["Deployment"]
    assert body["partial"] is True
    assert [(entry["group"], entry["resource"], entry["reason"]) for entry in body["unavailable"]] == [
        ("apps", "daemonsets", "forbidden"),
    ]


def test_restarts_are_null_when_pods_could_not_be_listed(client, fake_k8s):
    """0 would read as a workload that has not restarted. We did not look."""
    _stub_listings(fake_k8s, deployments=[_deployment()])
    fake_k8s.core_v1.raises("list_pod_for_all_namespaces",
                            ApiException(status=403, reason="Forbidden"))

    body = client.get("/api/workloads").json()

    assert body["items"][0]["restarts_24h"] is None
    assert body["partial"] is True
    assert ("", "pods", "forbidden") in [
        (entry["group"], entry["resource"], entry["reason"]) for entry in body["unavailable"]
    ]


def test_restarts_are_zero_when_pods_were_listed_and_none_restarted(client, fake_k8s):
    _stub_listings(fake_k8s, deployments=[_deployment()], pods=[_pod(restarts=0)])

    body = client.get("/api/workloads").json()

    assert body["items"][0]["restarts_24h"] == 0
    assert body["partial"] is False


def test_restarts_are_counted_from_the_pods_the_selector_picks(client, fake_k8s):
    _stub_listings(
        fake_k8s,
        deployments=[_deployment()],
        pods=[_pod(restarts=3), _pod(name="payments-1", restarts=9, labels={"app": "payments"})],
    )

    assert client.get("/api/workloads").json()["items"][0]["restarts_24h"] == 3


def test_a_namespace_filter_uses_the_namespaced_listings(client, fake_k8s):
    """Not a client-side filter over every namespace: on a large cluster that is
    a listing the API server times out on, reported as an unreachable cluster."""
    for method in ("list_namespaced_deployment", "list_namespaced_stateful_set",
                   "list_namespaced_daemon_set", "list_namespaced_replica_set"):
        fake_k8s.apps_v1.returns(method, _empty)
    for method in ("list_namespaced_job", "list_namespaced_cron_job"):
        fake_k8s.batch_v1.returns(method, _empty)
    fake_k8s.core_v1.returns("list_namespaced_pod", _empty)
    fake_k8s.apps_v1.returns("list_namespaced_deployment", obj(items=[_deployment()]))

    body = client.get("/api/workloads?namespace=prod").json()

    assert [row["name"] for row in body["items"]] == ["checkout"]
    assert fake_k8s.apps_v1.called("list_namespaced_deployment")[0][0] == ("prod",)


@pytest.mark.parametrize("value", ["Deployment", "deployment", "deployments", "DEPLOYMENTS"])
def test_the_kind_filter_accepts_the_kind_and_the_plural(client, fake_k8s, value):
    """The row says ``Deployment`` and the path says ``deployments``; a UI built
    from either must not get an empty list, which would read as 'none exist'."""
    fake_k8s.apps_v1.returns("list_deployment_for_all_namespaces", obj(items=[_deployment()]))
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))

    body = client.get(f"/api/workloads?kind={value}").json()

    assert [row["kind"] for row in body["items"]] == ["Deployment"]


def test_an_unknown_kind_is_rejected_rather_than_ignored(client, fake_k8s):
    """Ignoring it returns everything under a filter the caller believes is
    applied; returning nothing claims the cluster is empty."""
    response = client.get("/api/workloads?kind=pods")

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert "Deployment" in body["hint"]


# --------------------------------------------------------------------------- #
# GET /api/workloads/{plural}/{namespace}/{name}
# --------------------------------------------------------------------------- #

def _stub_detail(fake, *, pods=(), services=()):
    fake.apps_v1.returns("read_namespaced_deployment", _deployment())
    fake.core_v1.returns("list_namespaced_pod", obj(items=list(pods)))
    fake.core_v1.returns("list_namespaced_service", obj(items=list(services)))


def test_detail_assembles_pods_conditions_services_and_rollout(client, fake_k8s):
    _stub_detail(fake_k8s, pods=[_pod()], services=[_service()])

    body = client.get("/api/workloads/deployments/prod/checkout").json()

    assert body["workload"]["status"] == "Healthy"
    assert [pod["name"] for pod in body["pods"]] == ["checkout-7d9-abc"]
    assert [condition["type"] for condition in body["conditions"]] == ["Available"]
    assert body["services"] == [{
        "name": "checkout", "type": "ClusterIP", "clusterIP": "10.96.0.14",
        "ports": [{"name": "http", "port": 80, "targetPort": 8080,
                   "protocol": "TCP", "nodePort": None}],
    }]
    assert body["rollout"] == {"revision": 14, "strategy": "RollingUpdate",
                               "maxSurge": "25%", "maxUnavailable": "25%"}
    assert body["partial"] is False
    assert body["unavailable"] == []


def test_detail_restarts_are_null_when_the_pods_could_not_be_listed(client, fake_k8s):
    """The detail endpoint derives `restarts_24h` itself, and had no test for it.

    The list endpoint's version of this is covered. This is the other half: a
    separate derivation, in a different function, off a different pod listing.
    Coercing it to `0` passed the whole suite.

    `0` here says the workload has not restarted in 24 hours. That is the row an
    operator scrolls past while looking for the thing that is wrong, and the
    read that would have told them otherwise is the one that just failed.
    """
    fake_k8s.apps_v1.returns("read_namespaced_deployment", _deployment())
    fake_k8s.core_v1.raises("list_namespaced_pod",
                            ApiException(status=403, reason="Forbidden"))
    fake_k8s.core_v1.returns("list_namespaced_service", obj(items=[]))

    body = client.get("/api/workloads/deployments/prod/checkout").json()

    assert body["workload"]["restarts_24h"] is None
    # Not blind: the endpoint says which read it could not make.
    assert body["partial"] is True
    assert ("", "pods", "forbidden") in [
        (entry["group"], entry["resource"], entry["reason"]) for entry in body["unavailable"]
    ]
    # `pods` is [] here rather than null, and that is deliberate: the detail
    # payload pairs it with `partial` and the `unavailable` entry above, which
    # is what §1.1 requires the empty list to be accompanied by. The banner is
    # the signal, not the list's type.
    assert body["pods"] == []


def test_detail_restarts_are_zero_when_the_pods_were_read_and_none_restarted(
    client, fake_k8s,
):
    """The real zero, so the null above cannot be satisfied by always-null."""
    _stub_detail(fake_k8s, pods=[_pod(restarts=0)], services=[])

    body = client.get("/api/workloads/deployments/prod/checkout").json()

    assert body["workload"]["restarts_24h"] == 0
    assert body["partial"] is False


def test_detail_lists_pods_with_a_server_side_label_selector(client, fake_k8s):
    _stub_detail(fake_k8s, pods=[_pod()], services=[])

    client.get("/api/workloads/deployments/prod/checkout")

    args, kwargs = fake_k8s.core_v1.called("list_namespaced_pod")[0]
    assert args == ("prod",)
    assert kwargs == {"label_selector": "app=checkout"}


def test_detail_spec_block_covers_every_container_list(client, fake_k8s):
    _stub_detail(fake_k8s, pods=[], services=[])

    spec = client.get("/api/workloads/deployments/prod/checkout").json()["spec"]

    assert [(c["name"], c["type"]) for c in spec["containers"]] == [
        ("migrate", "init"), ("app", "container"),
    ]
    assert spec["containers"][1]["probes"] == {
        "liveness": True, "readiness": True, "startup": False,
    }
    assert spec["containers"][1]["env_count"] == 1
    assert spec["serviceAccount"] == "checkout"
    assert spec["volumes"] == [{"name": "cache", "type": "emptyDir"}]


def test_detail_degrades_one_key_when_services_cannot_be_listed(client, fake_k8s):
    """The workload still renders; the gap is named. Blanking the page because a
    secondary read failed makes the primary answer unreachable."""
    fake_k8s.apps_v1.returns("read_namespaced_deployment", _deployment())
    fake_k8s.core_v1.returns("list_namespaced_pod", obj(items=[_pod()]))
    fake_k8s.core_v1.raises("list_namespaced_service",
                            ApiException(status=403, reason="Forbidden"))

    response = client.get("/api/workloads/deployments/prod/checkout")
    body = response.json()

    assert response.status_code == 200
    assert body["workload"]["name"] == "checkout"
    assert body["pods"] != []
    assert body["services"] == []
    assert body["partial"] is True
    assert [(e["resource"], e["reason"]) for e in body["unavailable"]] == [("services", "forbidden")]


def test_detail_reports_a_missing_workload_as_not_found(client, fake_k8s):
    """The primary read is not collected: an empty shell with a footnote is not
    an answer to 'show me this workload'."""
    fake_k8s.apps_v1.raises("read_namespaced_deployment",
                            ApiException(status=404, reason="Not Found"))

    response = client.get("/api/workloads/deployments/prod/ghost")

    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_a_plural_that_is_not_a_workload_kind_is_not_found(client, fake_k8s):
    response = client.get("/api/workloads/pods/prod/checkout")

    assert response.status_code == 404
    body = response.json()
    assert body["error"] == "not_found"
    assert "deployments" in body["hint"]


# --------------------------------------------------------------------------- #
# Writes: refused here, or delegated to the single funnel
# --------------------------------------------------------------------------- #

def test_scaling_a_daemonset_is_refused_by_name(client, fake_k8s):
    """422 naming the kind, not a relayed 404 on the missing /scale subresource —
    which reads as 'your DaemonSet is gone'."""
    response = client.post("/api/workloads/daemonsets/kube-system/cilium/scale",
                           json={"replicas": 3, "dryRun": True})

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert "DaemonSet" in body["message"]
    assert "node" in body["hint"]
    assert body["context"]["kind"] == "DaemonSet"


@pytest.mark.parametrize("plural,kind", [("jobs", "Job"), ("cronjobs", "CronJob")])
def test_scaling_a_batch_kind_is_refused_by_name(client, fake_k8s, plural, kind):
    response = client.post(f"/api/workloads/{plural}/prod/nightly/scale", json={"replicas": 1})

    assert response.status_code == 422
    assert kind in response.json()["message"]


def test_suspending_a_deployment_is_refused_by_name(client, fake_k8s):
    response = client.post("/api/workloads/deployments/prod/checkout/suspend",
                           json={"suspend": True})

    assert response.status_code == 422
    assert "Deployment" in response.json()["message"]


def test_restarting_a_cronjob_is_refused_by_name(client, fake_k8s):
    response = client.post("/api/workloads/cronjobs/prod/nightly/restart", json={})

    assert response.status_code == 422
    assert "CronJob" in response.json()["message"]


def test_rolling_back_a_job_is_refused_by_name(client, fake_k8s):
    response = client.post("/api/workloads/jobs/prod/migrate/rollback", json={"revision": 2})

    assert response.status_code == 422
    assert "Job" in response.json()["message"]


def test_scale_delegates_to_the_write_funnel(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        workloads_api, "scale_workload",
        lambda *args: calls.append(args) or {"dryRun": args[4], "applied": False},
    )

    response = client.post("/api/workloads/deployments/prod/checkout/scale",
                           json={"replicas": 5, "dryRun": False})

    assert response.status_code == 200
    assert calls == [("deployments", "prod", "checkout", 5, False)]


def test_a_write_body_without_dry_run_is_a_dry_run(client, monkeypatch):
    """§1.5 defaults dryRun to true, and the default has to be the safe one: a
    client that forgets the field must not write to a cluster."""
    calls = []
    monkeypatch.setattr(workloads_api, "scale_workload",
                        lambda *args: calls.append(args) or {"dryRun": True})
    monkeypatch.setattr(workloads_api, "restart_workload",
                        lambda *args: calls.append(args) or {"dryRun": True})

    client.post("/api/workloads/deployments/prod/checkout/scale", json={"replicas": 5})
    client.post("/api/workloads/deployments/prod/checkout/restart")

    assert calls == [
        ("deployments", "prod", "checkout", 5, True),
        ("deployments", "prod", "checkout", True),
    ]


def test_a_negative_replica_count_is_rejected(client):
    response = client.post("/api/workloads/deployments/prod/checkout/scale",
                           json={"replicas": -1})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"


def test_suspend_requires_an_explicit_intent(client):
    """No default: one direction would suspend a production CronJob from an empty
    body, the other would resume one somebody suspended during an incident."""
    response = client.post("/api/workloads/cronjobs/prod/nightly/suspend", json={})

    assert response.status_code == 422


def test_suspend_delegates_with_both_flags(client, monkeypatch):
    calls = []
    monkeypatch.setattr(workloads_api, "suspend_workload",
                        lambda *args: calls.append(args) or {"dryRun": args[4]})

    client.post("/api/workloads/cronjobs/prod/nightly/suspend",
                json={"suspend": True, "dryRun": False})

    assert calls == [("cronjobs", "prod", "nightly", True, False)]


def test_rollout_history_is_delegated_for_a_valid_plural(client, monkeypatch):
    monkeypatch.setattr(workloads_api, "rollout_history",
                        lambda *args: {"current": 14, "revisions": [], "asked": args})

    body = client.get("/api/workloads/deployments/prod/checkout/rollout").json()

    assert body["current"] == 14
    assert body["asked"] == ["deployments", "prod", "checkout"]


def test_rollout_history_for_an_unknown_plural_is_not_found(client, monkeypatch):
    """An unknown plural is a 404, not an empty history — an empty history claims
    the object exists and has never been rolled."""
    monkeypatch.setattr(workloads_api, "rollout_history",
                        lambda *args: pytest.fail("must not reach the rollout reader"))

    assert client.get("/api/workloads/widgets/prod/checkout/rollout").status_code == 404


def test_rollback_delegates_the_revision(client, monkeypatch):
    calls = []
    monkeypatch.setattr(workloads_api, "rollback_workload",
                        lambda *args: calls.append(args) or {"dryRun": args[4]})

    client.post("/api/workloads/deployments/prod/checkout/rollback",
                json={"revision": 13, "dryRun": True})

    assert calls == [("deployments", "prod", "checkout", 13, True)]
