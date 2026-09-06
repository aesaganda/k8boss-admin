"""
Drain (§5) — the plan, the refusal, and the per-pod truth.

Three properties are asserted here, and each of them is a specific way a drain
can lie to the person who pressed the button:

* **The plan classifies every pod before anything moves.** A DaemonSet pod is
  skipped because its controller would put it straight back; a mirror pod is
  skipped because the kubelet owns it and no eviction can touch it; an emptyDir
  pod and an unmanaged pod are *blocked*, because in both cases something is
  lost and only the operator can decide that is acceptable.

* **Blocked with ``force: false`` refuses, and refuses before cordoning.** 422
  with the plan attached. Cordoning first and then refusing would leave the node
  silently rejecting new work while the operator went off to resolve the
  blockers — a change they did not ask for and were not told about.

* **A drain that lost pods does not report success.** Three failed evictions come
  back as ``applied: true, failed: 3, drained: false`` with a per-pod reason, not
  as a drained node. "Drained" over three stuck pods is the sentence that gets a
  machine terminated with a database on it, and it is the exact failure this
  codebase's defect standard is written against.
"""

from __future__ import annotations

import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from app.admin.nodes import (
    ACTION_BLOCKED,
    ACTION_EVICT,
    ACTION_SKIP,
    MIRROR_POD_ANNOTATION,
    build_plan,
    classify_pod,
    drain_node,
)
from app.api.exception_handlers import register_exception_handlers
from app.api.nodes import router as nodes_router
from app.errors import Invalid
from tests.conftest import obj

NODE_NAME = "ip-10-0-1-4"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _pod(
    name,
    namespace="prod",
    *,
    owner=None,
    annotations=None,
    deleting=False,
    phase="Running",
    volumes=(),
    labels=None,
):
    return obj(
        metadata=obj(
            name=name,
            namespace=namespace,
            labels={"app": name} if labels is None else labels,
            annotations=annotations or {},
            deletion_timestamp="2026-08-18T09:14:00Z" if deleting else None,
            owner_references=([owner] if owner else []),
        ),
        spec=obj(node_name=NODE_NAME, containers=[obj(name="app", resources=None)],
                 init_containers=[], volumes=list(volumes)),
        status=obj(phase=phase),
    )


def _owner(kind, name="owner"):
    return obj(kind=kind, name=name, controller=True)


def _emptydir_volume():
    return obj(name="cache", empty_dir=obj(medium=""))


def _pdb(name, *, namespace="prod", match_labels=None, allowed=1, with_status=True,
         expressions=None):
    """A PodDisruptionBudget in the raw JSON shape the API server returns."""
    selector = {"matchLabels": match_labels or {"app": "checkout"}}
    if expressions is not None:
        selector = {"matchExpressions": expressions}
    budget = {
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"selector": selector},
    }
    if with_status:
        budget["status"] = {"disruptionsAllowed": allowed}
    return budget


NODE_OBJECT = {
    "apiVersion": "v1",
    "kind": "Node",
    "metadata": {"name": NODE_NAME, "resourceVersion": "884213",
                 "labels": {"node-role.kubernetes.io/worker": ""}},
    "spec": {},
    "status": {"capacity": {"cpu": "16", "memory": "64Gi", "pods": "110"}},
}


class FakeCluster:
    """Routes the raw ``call_api`` calls the node writer makes.

    One dispatcher for the whole write path — node read, node patch, PDB listing,
    eviction — because the assertions that matter are about *which* of those
    happened. ``patches`` being empty is how "the refusal came before the cordon"
    is proved, and ``evicted`` is how a per-pod result is tied to a real call.
    """

    def __init__(self, *, pdbs=(), pdb_error=None, eviction_errors=None):
        self.pdbs = list(pdbs)
        self.pdb_error = pdb_error
        self.eviction_errors = dict(eviction_errors or {})
        self.patches: list[dict] = []
        self.evicted: list[tuple[str, str]] = []

    def __call__(self, path, method, *args, **kwargs):
        if method == "GET" and path.endswith("/poddisruptionbudgets"):
            if self.pdb_error is not None:
                raise self.pdb_error
            return {"items": copy.deepcopy(self.pdbs)}
        if method == "GET" and "/nodes/" in path:
            return copy.deepcopy(NODE_OBJECT)
        if method == "PATCH" and "/nodes/" in path:
            body = kwargs.get("body")
            self.patches.append(body)
            after = copy.deepcopy(NODE_OBJECT)
            after["spec"].update(body.get("spec", {}))
            return after, 200, {"Warning": '299 - "node ip-10-0-1-4 is now unschedulable"'}
        if method == "POST" and path.endswith("/eviction"):
            parts = path.split("/")
            namespace, pod = parts[4], parts[6]
            self.evicted.append((namespace, pod))
            error = self.eviction_errors.get(pod)
            if error is not None:
                raise error
            return {}
        raise AssertionError(f"Unexpected cluster call: {method} {path}")


@pytest.fixture
def cluster(fake_k8s, db_engine, allow_mutations):
    """A fake cluster wired for the write path, with preflight allowing everything.

    Preflight is stubbed to allow so that these tests are about drain semantics.
    Denial has its own coverage where the funnel lives; asserting it again here
    would let this copy go stale and start passing for the wrong reason.
    """
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review", obj(status=obj(allowed=True))
    )

    def install(cluster: FakeCluster, pods):
        fake_k8s.api_client.returns("call_api", cluster)
        fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=list(pods)))
        return cluster

    return install


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def _classify(pod, *, ignore_daemonsets=True, delete_emptydir_data=False, budgets=None):
    return classify_pod(
        pod,
        ignore_daemonsets=ignore_daemonsets,
        delete_emptydir_data=delete_emptydir_data,
        budgets=budgets,
    )


def test_daemonset_pod_is_skipped_when_ignored():
    """Skipped, not evicted: the DaemonSet controller recreates it here immediately.

    Evicting it would churn the pod and change nothing about the node, and
    counting it as drained would be a claim that it left.
    """
    entry = _classify(_pod("cilium-x4k2", "kube-system", owner=_owner("DaemonSet")))
    assert entry["action"] == ACTION_SKIP
    assert "DaemonSet" in entry["reason"]


def test_daemonset_pod_blocks_when_not_ignored():
    entry = _classify(
        _pod("cilium-x4k2", "kube-system", owner=_owner("DaemonSet")),
        ignore_daemonsets=False,
    )
    assert entry["action"] == ACTION_BLOCKED
    assert "ignoreDaemonSets" in entry["reason"]


def test_mirror_pod_is_always_skipped():
    """No flag changes this: the kubelet owns it, and the API server cannot evict it."""
    entry = _classify(
        _pod("kube-apiserver-cp", "kube-system",
             annotations={MIRROR_POD_ANNOTATION: "0d1f"}),
        ignore_daemonsets=False,
        delete_emptydir_data=True,
    )
    assert entry["action"] == ACTION_SKIP
    assert "mirror pod" in entry["reason"]


def test_terminating_pod_is_skipped():
    entry = _classify(_pod("checkout-old", owner=_owner("ReplicaSet"), deleting=True))
    assert entry["action"] == ACTION_SKIP
    assert entry["reason"] == "already terminating"


@pytest.mark.parametrize("phase", ["Succeeded", "Failed"])
def test_finished_pods_are_skipped_even_unmanaged(phase):
    """A finished bare pod is not a blocker: it holds nothing and is going nowhere."""
    entry = _classify(_pod("backfill", phase=phase))
    assert entry["action"] == ACTION_SKIP
    assert phase in entry["reason"]


def test_emptydir_blocks_unless_the_operator_opts_in():
    pod = _pod("pg-0", "data", owner=_owner("StatefulSet"), volumes=[_emptydir_volume()])

    blocked = _classify(pod)
    assert blocked["action"] == ACTION_BLOCKED
    assert "emptyDir" in blocked["reason"]

    allowed = _classify(pod, delete_emptydir_data=True)
    assert allowed["action"] == ACTION_EVICT
    assert allowed["reason"] is None


def test_unmanaged_bare_pod_blocks():
    """Nothing owns it, so nothing recreates it elsewhere. Evicting it deletes it."""
    entry = _classify(_pod("debug-shell"))
    assert entry["action"] == ACTION_BLOCKED
    assert "unmanaged" in entry["reason"]


def test_every_blocking_reason_is_reported_not_just_the_first():
    """Two problems need two decisions.

    Surfacing one at a time sends the operator round the confirm loop twice, and
    the second trip is the one where they stop reading and reach for ``force``.
    """
    entry = _classify(_pod("debug-shell", volumes=[_emptydir_volume()]))
    assert entry["action"] == ACTION_BLOCKED
    assert "emptyDir" in entry["reason"]
    assert "unmanaged" in entry["reason"]


def test_pdb_with_no_disruptions_left_blocks():
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[_pdb("checkout-pdb", allowed=0)],
    )
    assert entry["action"] == ACTION_BLOCKED
    assert "checkout-pdb" in entry["reason"]
    assert entry["pdb"] == "checkout-pdb"


def test_pdb_with_room_does_not_block_but_is_named():
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[_pdb("checkout-pdb", allowed=2)],
    )
    assert entry["action"] == ACTION_EVICT
    assert entry["pdb"] == "checkout-pdb"


def test_pdb_that_does_not_select_the_pod_is_ignored():
    entry = _classify(
        _pod("cache-0", owner=_owner("StatefulSet"), labels={"app": "cache"}),
        budgets=[_pdb("checkout-pdb", match_labels={"app": "checkout"}, allowed=0)],
    )
    assert entry["action"] == ACTION_EVICT
    assert entry["pdb"] is None
    assert entry["pdbUnknown"] == []


def test_a_budget_whose_selector_cannot_be_read_is_unknown_not_absent():
    """The gap this closes. There used to be a second, two-state label-selector
    matcher, and this call site used it — so a budget with a `matchExpressions`
    operator the console does not model came back as a plain "does not select",
    the plan said no budget covered the pod, and the operator found out from an
    eviction the API server refused, mid-drain.

    `pdb: null` still means "no budget covers this pod". `pdbUnknown` is the new
    third state and is never folded into it."""
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[_pdb("odd-pdb", allowed=0, expressions=[
            {"key": "app", "operator": "Gt", "values": ["3"]},
        ])],
    )

    assert entry["pdb"] is None
    assert entry["pdbUnknown"] == ["odd-pdb"]


def test_an_unreadable_selector_does_not_block_the_drain():
    """Deliberately *not* a blocker, for the reason an unwritten
    `disruptionsAllowed` is not one: the eviction subresource is the enforcer,
    and refusing a whole drain over a selector this console merely could not
    parse is how `force` becomes reflex — which is worse than the risk it
    avoids."""
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[_pdb("odd-pdb", allowed=0, expressions=[
            {"key": "app", "operator": "Gt", "values": ["3"]},
        ])],
    )

    assert entry["action"] == ACTION_EVICT
    assert entry["reason"] is None


def test_a_readable_blocking_budget_still_blocks_alongside_an_unreadable_one():
    """One budget the console cannot parse must not suppress another it can."""
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[
            _pdb("odd-pdb", allowed=1, expressions=[
                {"key": "app", "operator": "Gt", "values": ["3"]},
            ]),
            _pdb("checkout-pdb", allowed=0),
        ],
    )

    assert entry["action"] == ACTION_BLOCKED
    assert entry["pdb"] == "checkout-pdb"
    assert entry["pdbUnknown"] == ["odd-pdb"]


def test_pdb_without_a_computed_status_is_named_but_does_not_block():
    """The eviction API is the enforcer; the plan says what it checked.

    Blocking on a status the PDB controller has not written yet would fail drains
    for a condition that clears itself in seconds, and would teach operators that
    ``force`` is the normal path.
    """
    entry = _classify(
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"}),
        budgets=[_pdb("checkout-pdb", with_status=False)],
    )
    assert entry["action"] == ACTION_EVICT
    assert entry["pdb"] == "checkout-pdb"


def test_plan_is_sorted_for_a_human_reader():
    """The list is compared against the same list thirty seconds ago."""
    plan = build_plan(
        [
            _pod("zeta", "prod", owner=_owner("ReplicaSet")),
            _pod("alpha", "prod", owner=_owner("ReplicaSet")),
            _pod("mid", "data", owner=_owner("ReplicaSet")),
        ],
        ignore_daemonsets=True,
        delete_emptydir_data=False,
        budgets={},
    )
    assert [(e["namespace"], e["pod"]) for e in plan] == [
        ("data", "mid"), ("prod", "alpha"), ("prod", "zeta"),
    ]


# --------------------------------------------------------------------------- #
# Refusal
# --------------------------------------------------------------------------- #

def test_blocked_without_force_refuses_and_returns_the_plan(cluster):
    """422 ``invalid`` carrying the plan, so the operator sees what to resolve."""
    fake = cluster(FakeCluster(), [
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
        _pod("debug-shell"),
    ])

    with pytest.raises(Invalid) as raised:
        drain_node(
            NODE_NAME, dry_run=False, grace_period_seconds=None,
            ignore_daemonsets=True, delete_emptydir_data=False, force=False,
        )

    error = raised.value
    assert error.http_status == 422
    assert error.code == "invalid"
    assert error.context["blocked"] == 1
    blocked = [e for e in error.context["plan"] if e["action"] == ACTION_BLOCKED]
    assert [e["pod"] for e in blocked] == ["debug-shell"]
    assert "debug-shell" in error.detail

    # The refusal came *before* the cordon. Cordoning and then refusing would
    # leave the node quietly rejecting new work after a call the operator was
    # told had failed.
    assert fake.patches == []
    assert fake.evicted == []


def test_force_proceeds_past_blocked_pods(cluster):
    fake = cluster(FakeCluster(), [
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
        _pod("debug-shell"),
    ])

    result = drain_node(
        NODE_NAME, dry_run=False, grace_period_seconds=30,
        ignore_daemonsets=True, delete_emptydir_data=False, force=True,
    )

    assert result["applied"] is True
    assert result["drained"] is True
    assert result["blocked"] == 1
    assert result["evicted"] == 2
    assert sorted(pod for _ns, pod in fake.evicted) == ["checkout-7d9-abc", "debug-shell"]
    # Cordoned as part of the drain, so the scheduler does not refill the node
    # behind us.
    assert fake.patches == [{"spec": {"unschedulable": True}}]


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

def test_failed_evictions_are_reported_per_pod_and_never_as_success(cluster):
    """The assertion this whole module exists for.

    Two of four evictions are refused. The response says so — per pod, with a
    reason each — and ``drained`` is false. A single aggregate "success" here is
    how a node with a database still on it gets terminated.
    """
    fake = cluster(
        FakeCluster(eviction_errors={
            "pg-0": ApiException(status=429, reason="Too Many Requests"),
            "cache-0": ApiException(status=500, reason="Internal Server Error"),
        }),
        [
            _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
            _pod("checkout-7d9-def", owner=_owner("ReplicaSet")),
            _pod("pg-0", "data", owner=_owner("StatefulSet")),
            _pod("cache-0", "data", owner=_owner("StatefulSet")),
        ],
    )

    result = drain_node(
        NODE_NAME, dry_run=False, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    assert result["applied"] is True          # we did write to the cluster
    assert result["drained"] is False         # and the node is not drained
    assert result["evicted"] == 2
    assert result["failed"] == 2

    by_pod = {entry["pod"]: entry for entry in result["plan"]}
    assert by_pod["checkout-7d9-abc"]["result"] == "evicted"
    assert by_pod["pg-0"]["result"] == "failed"
    assert by_pod["cache-0"]["result"] == "failed"

    # 429 on the eviction subresource means a PodDisruptionBudget refused the
    # disruption, not that we are being rate limited. Reporting it as throttling
    # would tell the operator to wait for something that will never change.
    assert by_pod["pg-0"]["error_code"] == "disruption_budget"
    assert "PodDisruptionBudget" in by_pod["pg-0"]["error"]
    assert by_pod["cache-0"]["error_code"] == "upstream_error"

    # Every pod was attempted: stopping at the first failure would leave a
    # half-drained node and no list of what remained.
    assert len(fake.evicted) == 4


def test_the_audit_row_says_the_node_was_not_drained(cluster):
    """The response was honest and the record that outlives it was not.

    A drain is one funnel call performing many writes: the cordon patch, which
    the funnel preflights and diffs, and then one eviction per pod inside the
    `apply_fn`. So the row's outcome is `applied` the moment the cordon lands,
    whatever the evictions did — and "drain node-1, applied" over a node that
    kept its database is the exact claim `drained: false` exists to stop the
    *response* making. The counts go in the sentence, written when the row is.
    """
    from app.audit import recorder

    cluster(
        FakeCluster(eviction_errors={
            "pg-0": ApiException(status=429, reason="Too Many Requests"),
        }),
        [
            _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
            _pod("pg-0", "data", owner=_owner("StatefulSet")),
        ],
    )

    drain_node(
        NODE_NAME, dry_run=False, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    (row,) = recorder.query(limit=50)["items"]
    assert row["outcome"] == "applied", "the cordon did land"
    assert "evicted 1" in row["detail"]
    assert "1 refused" in row["detail"]
    assert "NOT drained" in row["detail"], (
        "somebody reading this trail after a machine was terminated must not "
        "have to open the response to find out the drain did not finish"
    )


def test_a_clean_drain_says_so_without_the_refusal_wording(cluster):
    from app.audit import recorder

    cluster(FakeCluster(), [_pod("checkout-7d9-abc", owner=_owner("ReplicaSet"))])

    drain_node(
        NODE_NAME, dry_run=False, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    (row,) = recorder.query(limit=50)["items"]
    assert "evicted 1" in row["detail"]
    assert "refused" not in row["detail"]
    assert "NOT drained" not in row["detail"]


def test_a_refusal_before_the_evictions_records_only_what_was_attempted(cluster):
    """A sentence claiming counts for evictions that never ran would be worse
    than the fixed one it replaced."""
    from app.audit import recorder

    cluster(FakeCluster(), [
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
        _pod("debug-shell"),
    ])

    with pytest.raises(Invalid):
        drain_node(
            NODE_NAME, dry_run=False, grace_period_seconds=None,
            ignore_daemonsets=True, delete_emptydir_data=False, force=False,
        )

    rows = recorder.query(limit=50)["items"]
    for row in rows:
        assert "evicted" not in (row["detail"] or "")


def test_a_dry_run_never_claims_an_eviction_count(cluster):
    """Nothing is evicted on a projection, so the sentence must not imply one."""
    from app.audit import recorder

    cluster(FakeCluster(), [_pod("checkout-7d9-abc", owner=_owner("ReplicaSet"))])

    drain_node(
        NODE_NAME, dry_run=True, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    (row,) = recorder.query(limit=50)["items"]
    assert row["dry_run"] is True
    assert "evicted" not in row["detail"]
    assert "(dry run)" in row["detail"]


def test_one_failure_does_not_stop_the_others(cluster):
    fake = cluster(
        FakeCluster(eviction_errors={
            "checkout-7d9-abc": ApiException(status=429, reason="Too Many Requests"),
        }),
        [
            _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
            _pod("checkout-7d9-def", owner=_owner("ReplicaSet")),
        ],
    )

    result = drain_node(
        NODE_NAME, dry_run=False, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    assert result["failed"] == 1
    assert result["evicted"] == 1
    assert len(fake.evicted) == 2


def test_dry_run_plans_without_evicting(cluster):
    """A projection, and it says so: ``applied`` and ``drained`` both false."""
    fake = cluster(FakeCluster(), [
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
        _pod("cilium-x4k2", "kube-system", owner=_owner("DaemonSet")),
    ])

    result = drain_node(
        NODE_NAME, dry_run=True, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    assert result["dryRun"] is True
    assert result["applied"] is False
    assert result["drained"] is False
    assert result["evicted"] == 0
    assert fake.evicted == []
    # The cordon is projected too, so the diff the operator confirms is the API
    # server's own answer rather than one assembled here.
    assert fake.patches == [{"spec": {"unschedulable": True}}]
    assert [e["action"] for e in result["plan"]] == [ACTION_SKIP, ACTION_EVICT]
    assert all(entry["result"] is None for entry in result["plan"])
    assert result["diff"]["changed"] is True
    assert result["warnings"] == ["node ip-10-0-1-4 is now unschedulable"]


def test_unreadable_disruption_budgets_are_declared_not_assumed_away(cluster):
    """"We did not check the budgets" must not look like "the budgets are fine".

    The plan still runs — the eviction API enforces PDBs regardless — but
    ``pdb_checked`` is false and the gap is in ``unavailable``, so nothing in the
    response implies a check that did not happen.
    """
    cluster(
        FakeCluster(pdb_error=ApiException(status=403, reason="Forbidden")),
        [_pod("checkout-7d9-abc", owner=_owner("ReplicaSet"))],
    )

    result = drain_node(
        NODE_NAME, dry_run=True, grace_period_seconds=None,
        ignore_daemonsets=True, delete_emptydir_data=False, force=False,
    )

    assert result["pdb_checked"] is False
    assert result["partial"] is True
    assert result["unavailable"][0]["resource"] == "poddisruptionbudgets"
    assert result["unavailable"][0]["reason"] == "forbidden"
    assert result["plan"][0]["pdb"] is None


def test_a_pdb_blocked_pod_is_planned_as_blocked(cluster):
    cluster(
        FakeCluster(pdbs=[_pdb("checkout-pdb", allowed=0)]),
        [_pod("checkout-7d9-abc", owner=_owner("ReplicaSet"), labels={"app": "checkout"})],
    )

    with pytest.raises(Invalid) as raised:
        drain_node(
            NODE_NAME, dry_run=True, grace_period_seconds=None,
            ignore_daemonsets=True, delete_emptydir_data=False, force=False,
        )
    assert raised.value.context["pdb_checked"] is True
    assert "checkout-pdb" in raised.value.context["plan"][0]["reason"]


# --------------------------------------------------------------------------- #
# Over HTTP
# --------------------------------------------------------------------------- #

@pytest.fixture
def api(cluster):
    """A TestClient over only this lane's router.

    Deliberately not ``app.main``: importing the whole app pulls in every other
    router, so a lane that has not landed yet would fail these tests for reasons
    that have nothing to do with drain. The exception handlers are the real ones,
    which is the part being exercised — that an ``Invalid`` raised deep inside
    the write path reaches the client as the §1.3 envelope with the plan intact.
    """
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(nodes_router)
    return TestClient(app), cluster


def test_http_refusal_is_422_with_the_plan(api):
    client, install = api
    install(FakeCluster(), [
        _pod("checkout-7d9-abc", owner=_owner("ReplicaSet")),
        _pod("debug-shell"),
    ])

    response = client.post(
        f"/api/nodes/{NODE_NAME}/drain",
        json={"dryRun": False, "force": False, "ignoreDaemonSets": True},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert body["context"]["blocked"] == 1
    assert [e["pod"] for e in body["context"]["plan"] if e["action"] == ACTION_BLOCKED] == [
        "debug-shell"
    ]
    assert body["hint"]


def test_http_drain_defaults_to_a_dry_run(api):
    """An omitted ``dryRun`` is a projection, not an evacuation."""
    client, install = api
    fake = install(FakeCluster(), [_pod("checkout-7d9-abc", owner=_owner("ReplicaSet"))])

    response = client.post(f"/api/nodes/{NODE_NAME}/drain", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["dryRun"] is True
    assert body["applied"] is False
    assert fake.evicted == []


def test_http_cordon_returns_the_mutation_envelope(api):
    client, install = api
    fake = install(FakeCluster(), [])

    response = client.post(
        f"/api/nodes/{NODE_NAME}/cordon", json={"unschedulable": True, "dryRun": False},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    assert body["verb"] == "patch"
    assert body["target"]["resource"] == "nodes"
    assert body["diff"]["changed"] is True
    assert fake.patches == [{"spec": {"unschedulable": True}}]


def test_http_cordon_requires_the_operator_to_say_which_way(api):
    """An empty body must not mean "cordon" and must not mean "uncordon"."""
    client, _install = api
    response = client.post(f"/api/nodes/{NODE_NAME}/cordon", json={})
    assert response.status_code == 422
