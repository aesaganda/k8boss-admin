"""
Node taints and labels (§24).

Both writes are two lines of YAML, and §4's editor could already send them. What
is tested here is the part the editor cannot do, which is every claim this
feature makes about the pods that are already on the machine:

* **A `NoExecute` taint deletes pods, and the delete is not an eviction.** It
  does not go through `pods/eviction`, so PodDisruptionBudgets do not apply — the
  opposite of what §5's drain dialog spends three paragraphs teaching the same
  operator. That sentence has to reach the confirmation screen whether the pods
  go immediately or on a timer, so it is asserted on both.

* **`tolerationSeconds` is a third state.** A pod tolerating a taint for 300
  seconds is neither staying nor going now. Reporting it in either bucket makes
  the node look settled when it is five minutes from emptying.

* **A taint whose *value* changed deletes as surely as a new one.** Pods
  tolerating the old value with `operator: Equal` stop tolerating it, and a
  "changed" row that produced an empty deletion list would be the quietest
  possible way to empty a node.

* **An unreadable pod listing is `null`, never `[]`.** `[]` renders as "this
  taint deletes nothing" over a node nobody counted, which is §0.1's corollary
  pointed at the most destructive write in the module — so it is a consequence
  the operator must acknowledge by name, not a silence.

* **A label change evicts nothing, and that is the trap.** Node affinity is
  `requiredDuringSchedulingIgnoredDuringExecution`; nothing is re-evaluated for a
  running pod. The consequence says so in those words, because an operator who
  removed a label to move a workload has not moved it.
"""

from __future__ import annotations

import copy

import pytest
from kubernetes.client.rest import ApiException

from app.admin import node_scheduling as ns
from app.audit import recorder
from app.errors import Conflict, Invalid, MutationsDisabled, RBACDenied
from app.resources.shaping import (
    node_label_dependencies,
    taint_tolerated_by,
    toleration_tolerates_taint,
)
from tests.conftest import obj

NODE = "ip-10-0-1-4"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def taint(key="dedicated", value="gpu", effect="NoExecute"):
    return {"key": key, "value": value, "effect": effect}


def toleration(key="dedicated", *, value="gpu", operator="Equal", effect="NoExecute",
               seconds=None):
    return obj(
        key=key, value=value, operator=operator, effect=effect,
        toleration_seconds=seconds,
    )


def pod(name, *, namespace="prod", tolerations=(), owner=None, node_selector=None,
        affinity=None):
    return obj(
        metadata=obj(
            name=name, namespace=namespace, labels={"app": name},
            annotations={}, owner_references=([owner] if owner else []),
        ),
        spec=obj(
            node_name=NODE,
            tolerations=list(tolerations),
            node_selector=node_selector,
            affinity=affinity,
            containers=[obj(name="app", resources=None)],
        ),
        status=obj(phase="Running"),
    )


def owner(kind, name="owner"):
    return obj(kind=kind, name=name, controller=True)


def required_affinity(*keys):
    return obj(node_affinity=obj(
        required_during_scheduling_ignored_during_execution=obj(
            node_selector_terms=[obj(
                match_expressions=[obj(key=k, operator="In", values=["x"]) for k in keys],
                match_fields=[],
            )],
        ),
        preferred_during_scheduling_ignored_during_execution=[],
    ))


def node_object(*, taints=None, labels=None, resource_version="884213"):
    body: dict = {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": NODE,
            "resourceVersion": resource_version,
            "labels": dict(labels) if labels is not None else {
                "node-role.kubernetes.io/worker": "",
                "team": "payments",
            },
        },
        "spec": {},
        "status": {"capacity": {"cpu": "16"}},
    }
    if taints:
        body["spec"]["taints"] = copy.deepcopy(taints)
    return body


class FakeCluster:
    """Routes the raw ``call_api`` calls §24 makes: the node read and the patch.

    ``patches`` staying empty is how "the refusal came before the write" is
    proved — the assertion that matters for every refusal in this module, since a
    consequence acknowledged after the cluster changed is not a confirmation.
    """

    def __init__(self, node=None, *, patch_error=None):
        self.node = node if node is not None else node_object()
        self.patch_error = patch_error
        self.patches: list[dict] = []

    def __call__(self, path, method, *args, **kwargs):
        if method == "GET" and "/nodes/" in path:
            return copy.deepcopy(self.node)
        if method == "PATCH" and "/nodes/" in path:
            if self.patch_error is not None:
                raise self.patch_error
            body = kwargs.get("body") or {}
            self.patches.append({
                "body": body,
                "query": dict(kwargs.get("query_params") or []),
                "content_type": (kwargs.get("header_params") or {}).get("Content-Type"),
            })
            after = copy.deepcopy(self.node)
            after["spec"].update(body.get("spec") or {})
            metadata = body.get("metadata") or {}
            for key, value in (metadata.get("labels") or {}).items():
                if value is None:
                    after["metadata"]["labels"].pop(key, None)
                else:
                    after["metadata"]["labels"][key] = value
            return after, 200, {}
        raise AssertionError(f"Unexpected cluster call: {method} {path}")


@pytest.fixture
def cluster(fake_k8s, db_engine, allow_mutations):
    """A fake cluster wired for the write path, preflight allowing everything.

    Preflight denial has its own coverage where the funnel lives; asserting it
    again in every feature module lets the copy go stale and start passing for
    the wrong reason. The one denial test below is about §24 reaching the funnel
    at all, not about what the funnel does with the answer.
    """
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review", obj(status=obj(allowed=True))
    )

    def install(fake=None, pods=(), *, pod_error=None):
        fake = fake if fake is not None else FakeCluster()
        fake_k8s.api_client.returns("call_api", fake)
        if pod_error is not None:
            fake_k8s.core_v1.raises("list_pod_for_all_namespaces", pod_error)
        else:
            fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=list(pods)))
        return fake

    return install


def codes(entries):
    return [entry["code"] for entry in entries]


def audit_rows():
    return recorder.query(limit=50)["items"]


# --------------------------------------------------------------------------- #
# Toleration matching — the API server's rules, not an approximation
# --------------------------------------------------------------------------- #

def test_a_toleration_with_no_effect_tolerates_every_effect():
    """The rule an approximation gets wrong in the dangerous direction.

    A blank `effect` is a wildcard. Reading it as "matches nothing" would list
    pods as being deleted that are going nowhere, on the screen where an operator
    decides whether to drain instead.
    """
    tol = toleration(effect=None)

    assert toleration_tolerates_taint(tol, taint(effect="NoExecute")) is True
    assert toleration_tolerates_taint(tol, taint(effect="NoSchedule")) is True


def test_an_empty_key_is_a_wildcard_only_with_exists():
    """`{operator: Exists}` with no key tolerates everything — what §5.5's debug
    pod carries. With `Equal` the API server rejects it, so it is not treated as
    a wildcard here either."""
    assert toleration_tolerates_taint(
        obj(key=None, operator="Exists", value=None, effect=None, toleration_seconds=None),
        taint(key="anything"),
    ) is True
    assert toleration_tolerates_taint(
        obj(key=None, operator="Equal", value=None, effect=None, toleration_seconds=None),
        taint(key="anything"),
    ) is False


def test_operator_defaults_to_equal_and_compares_the_value():
    tol = obj(key="dedicated", operator=None, value="gpu", effect=None,
              toleration_seconds=None)

    assert toleration_tolerates_taint(tol, taint(value="gpu")) is True
    assert toleration_tolerates_taint(tol, taint(value="cpu")) is False


def test_an_unbounded_toleration_beats_a_bounded_one():
    """The taint manager treats a matching toleration with no `tolerationSeconds`
    as infinite. A pod carrying both stays, and reporting it as "goes in 30s"
    would put it on a deletion list it is not on."""
    tolerated, seconds = taint_tolerated_by(
        pod("api", tolerations=[toleration(seconds=30), toleration(seconds=None)]),
        taint(),
    )

    assert (tolerated, seconds) == (True, None)


def test_several_bounded_tolerations_report_the_soonest():
    tolerated, seconds = taint_tolerated_by(
        pod("api", tolerations=[toleration(seconds=600), toleration(seconds=90)]),
        taint(),
    )

    assert (tolerated, seconds) == (True, 90)


def test_a_pod_with_no_tolerations_tolerates_nothing():
    assert taint_tolerated_by(pod("api"), taint()) == (False, None)


# --------------------------------------------------------------------------- #
# The deletion plan
# --------------------------------------------------------------------------- #

def test_a_pod_that_does_not_tolerate_is_deleted_with_no_delay():
    """`delay_seconds: 0` is a **real** zero — the taint manager removes it as
    soon as the taint is written, and nothing was left unread to produce it."""
    rows = ns.deletion_plan([pod("api")], [taint()])

    assert len(rows) == 1
    assert rows[0]["delay_seconds"] == 0
    assert rows[0]["pod"] == "api"


def test_a_pod_tolerating_for_a_time_is_on_the_list_with_its_delay():
    rows = ns.deletion_plan([pod("api", tolerations=[toleration(seconds=300)])], [taint()])

    assert [row["delay_seconds"] for row in rows] == [300]


def test_a_pod_that_tolerates_unconditionally_is_not_on_the_list():
    rows = ns.deletion_plan([pod("api", tolerations=[toleration()])], [taint()])

    assert rows == []


@pytest.mark.parametrize("order", [("a", "b"), ("b", "a")], ids=["sooner-last", "sooner-first"])
def test_a_pod_hit_by_two_taints_is_reported_once_against_the_sooner(order):
    """Two things at once, and both orders, because either is a real defect.

    Listing the pod twice doubles the count on the confirmation screen, and the
    count is what the operator reads. Picking the wrong one of the two puts a
    deadline on the row that is not the deadline — which reads as five minutes
    of warning where there are sixty seconds. Running both orders is what stops
    a "keep the first" or "keep the last" implementation passing on the half of
    the inputs that happens to agree with it.
    """
    rows = ns.deletion_plan(
        [pod("api", tolerations=[toleration(key="a", seconds=600),
                                 toleration(key="b", seconds=60)])],
        [taint(key=order[0]), taint(key=order[1])],
    )

    assert len(rows) == 1
    assert rows[0]["delay_seconds"] == 60
    assert rows[0]["taint"]["key"] == "b"


def test_the_plan_names_the_owning_controller_or_says_there_is_none():
    rows = ns.deletion_plan(
        [pod("api"), pod("cilium", owner=owner("DaemonSet", "cilium"))], [taint()],
    )
    by_name = {row["pod"]: row for row in rows}

    assert by_name["api"]["controller"] is None
    assert by_name["cilium"]["controller"] == {"kind": "DaemonSet", "name": "cilium"}


# --------------------------------------------------------------------------- #
# The taint diff
# --------------------------------------------------------------------------- #

def test_a_changed_value_is_changed_not_an_add_and_a_remove():
    """One taint to the API server, whose uniqueness rule is (key, effect)."""
    diff = ns.diff_taints([taint(value="gpu")], [taint(value="tpu")])

    assert diff["added"] == [] and diff["removed"] == []
    assert diff["changed"] == [{"before": taint(value="gpu"), "after": taint(value="tpu")}]


def test_a_revalued_noexecute_taint_is_newly_enforced():
    """The subtle one. Pods tolerating `dedicated=gpu` with `operator: Equal`
    stop tolerating `dedicated=tpu`, so a value edit empties the node exactly as
    a new taint would — and "changed" does not sound like it."""
    diff = ns.diff_taints([taint(value="gpu")], [taint(value="tpu")])

    assert ns.newly_enforced(diff) == [taint(value="tpu")]


def test_a_new_noschedule_taint_is_not_newly_enforced():
    """NoSchedule is consulted when a pod is *placed*. Nothing already running
    here moves, so nothing belongs on a deletion list."""
    diff = ns.diff_taints([], [taint(effect="NoSchedule")])

    assert ns.newly_enforced(diff) == []


def test_the_same_key_may_carry_two_effects():
    diff = ns.diff_taints(
        [taint(effect="NoSchedule")], [taint(effect="NoSchedule"), taint(effect="NoExecute")],
    )

    assert diff["added"] == [taint(effect="NoExecute")]
    assert diff["removed"] == []


# --------------------------------------------------------------------------- #
# Taint consequences
# --------------------------------------------------------------------------- #

def _taint_consequences(current, requested, pods):
    diff = ns.diff_taints(current, requested)
    deleting = (
        ns.deletion_plan(pods, ns.newly_enforced(diff)) if pods is not None else None
    )
    return ns.taint_consequences(diff, deleting)


def test_the_deletion_consequence_says_poddisruptionbudgets_do_not_apply():
    """The sentence this whole module exists for. Two clicks away, §5's drain
    dialog tells the same operator that eviction honours budgets and that `force`
    will not get them past one. Both are true; this write is the exception, and
    it has to say so where the operator is looking."""
    entries = _taint_consequences([], [taint()], [pod("api")])
    deletion = next(e for e in entries if e["code"] == ns.WARN_TAINT_DELETES_PODS)

    assert "PodDisruptionBudgets do not apply" in deletion["consequence"]
    assert "not an eviction" in deletion["consequence"]


def test_the_budget_sentence_is_reached_when_every_pod_goes_on_a_timer():
    """A regression guard on the ordering: with no immediate deletions the
    consequence still has to fire, or the one fact that distinguishes this write
    from a drain is withheld whenever the pods happen to carry tolerations."""
    entries = _taint_consequences(
        [], [taint()], [pod("api", tolerations=[toleration(seconds=300)])],
    )

    assert ns.WARN_TAINT_DELETES_PODS in codes(entries)
    assert ns.WARN_TAINT_DELAYED in codes(entries)


def test_an_unreadable_pod_listing_is_its_own_consequence():
    """`deleting: null` must not pass as "this deletes nothing". The operator
    acknowledges by name that the console does not know what the button does."""
    entries = _taint_consequences([], [taint()], None)

    assert ns.WARN_TAINT_PODS_UNKNOWN in codes(entries)
    assert ns.WARN_TAINT_DELETES_PODS not in codes(entries)


def test_an_unreadable_listing_is_not_a_consequence_when_nothing_is_enforced():
    """Adding a NoSchedule taint touches no running pod, so not having counted
    them is not a gap in anything the operator is being asked to accept."""
    entries = _taint_consequences([], [taint(effect="NoSchedule")], None)

    assert ns.WARN_TAINT_PODS_UNKNOWN not in codes(entries)


def test_unmanaged_pods_get_their_own_acknowledgement():
    entries = _taint_consequences([], [taint()], [pod("api")])
    unmanaged = next(e for e in entries if e["code"] == ns.WARN_TAINT_DELETES_UNMANAGED)

    assert "prod/api" in unmanaged["consequence"]


def test_daemonset_pods_get_their_own_acknowledgement():
    """The DaemonSet controller tolerates the node's condition taints and nothing
    else, so a taint you write takes out log shipping and the CNI agent and does
    not put them back."""
    entries = _taint_consequences(
        [], [taint()], [pod("cilium", owner=owner("DaemonSet", "cilium"))],
    )

    assert ns.WARN_TAINT_DELETES_DAEMONSET in codes(entries)


def test_removing_a_taint_says_the_node_stops_excluding_work():
    entries = _taint_consequences([taint(effect="NoSchedule")], [], [])

    assert ns.WARN_TAINT_REMOVED in codes(entries)


def test_removing_the_control_plane_taint_is_named_separately():
    """Not the same act as removing a taint somebody added this morning."""
    control_plane = {"key": "node-role.kubernetes.io/control-plane",
                     "value": None, "effect": "NoSchedule"}
    entries = _taint_consequences([control_plane], [], [])

    assert ns.WARN_TAINT_CONTROL_PLANE_OPENED in codes(entries)


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def test_node_label_dependencies_reads_both_placement_rules():
    dependencies = node_label_dependencies(pod(
        "api",
        node_selector={"disktype": "ssd"},
        affinity=required_affinity("topology.kubernetes.io/zone"),
    ))

    assert dependencies == ["disktype", "topology.kubernetes.io/zone"]


def test_preferred_affinity_is_not_a_dependency():
    """A tie-breaker, not a requirement. Reporting it beside a hard rule would
    overstate what removing the label costs."""
    affinity = obj(
        node_affinity=obj(
            required_during_scheduling_ignored_during_execution=None,
            preferred_during_scheduling_ignored_during_execution=[
                obj(weight=10, preference=obj(
                    match_expressions=[obj(key="rack", operator="In", values=["a"])],
                    match_fields=[],
                )),
            ],
        ),
    )

    assert node_label_dependencies(pod("api", affinity=affinity)) == []


def test_removing_a_label_says_nothing_is_evicted():
    """The trap. An operator who removed a label to move a workload has not
    moved it — node affinity is IgnoredDuringExecution and the pod is untouched
    until something else restarts it."""
    diff = ns.diff_labels({"team": "payments"}, {})
    entries = ns.label_consequences(diff, [])
    removed = next(e for e in entries if e["code"] == ns.WARN_LABEL_REMOVED)

    assert "IgnoredDuringExecution" in removed["consequence"]
    assert "keep running" in removed["consequence"]


def test_touching_a_reserved_label_is_named():
    diff = ns.diff_labels({"topology.kubernetes.io/zone": "eu-west-1a"}, {})
    entries = ns.label_consequences(diff, [])

    assert ns.WARN_LABEL_RESERVED in codes(entries)


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        ({}, {"node-role.kubernetes.io/infra": ""}),
        ({"node-role.kubernetes.io/infra": ""}, {}),
    ],
    ids=["added", "removed"],
)
def test_changing_a_role_label_says_it_changes_what_the_node_reports(current, requested):
    """Both directions. A role label appearing changes the ROLES column that
    `kubectl get nodes` and this console's own node list read from exactly as
    much as one disappearing, so a check over removals alone would stay silent on
    half of it."""
    entries = ns.label_consequences(ns.diff_labels(current, requested), [])

    assert ns.WARN_LABEL_ROLE_CHANGED in codes(entries)


def test_pods_placed_by_a_rule_naming_the_key_are_reported():
    diff = ns.diff_labels({"disktype": "ssd"}, {})
    dependents = ns.label_dependents([pod("api", node_selector={"disktype": "ssd"})],
                                     ["disktype"])
    entries = ns.label_consequences(diff, dependents)
    depend = next(e for e in entries if e["code"] == ns.WARN_LABEL_PODS_DEPEND)

    assert "prod/api" in depend["consequence"]


def test_an_unreadable_listing_is_its_own_label_consequence():
    diff = ns.diff_labels({"disktype": "ssd"}, {})
    entries = ns.label_consequences(diff, None)

    assert ns.WARN_LABEL_PODS_UNKNOWN in codes(entries)
    assert ns.WARN_LABEL_PODS_DEPEND not in codes(entries)


# --------------------------------------------------------------------------- #
# The patches
# --------------------------------------------------------------------------- #

def test_an_empty_taint_list_is_sent_as_null():
    """`null` renders in the diff as the key disappearing, which is what removing
    every taint is. `[]` renders as the key surviving with nothing in it."""
    patch = ns.build_taint_patch([], resource_version=None)

    assert patch == {"spec": {"taints": None}}


def test_a_removed_label_is_sent_as_an_explicit_null():
    """A merge patch over a map *merges*: a key the caller dropped survives
    unless it is set to null. Getting this wrong is a delete that silently does
    not happen."""
    diff = ns.diff_labels({"team": "payments", "keep": "yes"}, {"keep": "yes"})
    patch = ns.build_label_patch(diff, {"keep": "yes"}, resource_version="884213")

    assert patch["metadata"]["labels"] == {"keep": "yes", "team": None}
    assert patch["metadata"]["resourceVersion"] == "884213"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def test_an_unknown_effect_is_refused_with_the_three_that_exist():
    with pytest.raises(Invalid) as caught:
        ns.validate_taints({"taints": [taint(effect="NoExecuteNow")]})

    assert "NoExecute" in caught.value.message
    assert caught.value.context["parameter"] == "taints"


def test_a_repeated_key_and_effect_pair_is_refused():
    with pytest.raises(Invalid) as caught:
        ns.validate_taints({"taints": [taint(), taint(value="other")]})

    assert "repeats" in caught.value.message


def test_a_missing_taint_list_is_refused_rather_than_read_as_empty():
    """An absent list would otherwise mean "remove every taint on this node"."""
    with pytest.raises(Invalid):
        ns.validate_taints({})


def test_a_non_string_label_value_is_refused_by_name():
    """The API server refuses this with a schema error naming a type. Kubernetes
    label values are strings and quoting is the fix, so the message says which
    key and says that."""
    with pytest.raises(Invalid) as caught:
        ns.validate_labels({"labels": {"version": 3}})

    assert "'version'" in caught.value.message
    assert "quote" in (caught.value.hint or "")


# --------------------------------------------------------------------------- #
# Through the funnel
# --------------------------------------------------------------------------- #

def test_a_dry_run_sends_dryrun_all_and_reports_applied_false(cluster):
    fake = cluster(FakeCluster(), [pod("api", tolerations=[toleration()])])

    result = ns.set_taints(NODE, {"taints": [taint()]}, dry_run=True)

    assert result["applied"] is False
    assert fake.patches[0]["query"]["dryRun"] == "All"
    assert fake.patches[0]["content_type"] == "application/merge-patch+json"


def test_a_confirmed_write_applies_and_is_audited(cluster):
    cluster(FakeCluster(), [pod("api", tolerations=[toleration()])])

    result = ns.set_taints(NODE, {"taints": [taint()]}, dry_run=False)

    assert result["applied"] is True
    row = audit_rows()[0]
    assert row["verb"] == "patch"
    assert "dedicated=gpu:NoExecute" in row["detail"]


def test_an_unacknowledged_consequence_refuses_before_the_cluster_is_touched(cluster):
    """A consequence accepted after the write is not a confirmation."""
    fake = cluster(FakeCluster(), [pod("api")])

    with pytest.raises(Invalid) as caught:
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=False)

    assert fake.patches == []
    assert ns.WARN_TAINT_DELETES_PODS in caught.value.context["unacknowledged"]


def test_naming_every_consequence_lets_the_write_through(cluster):
    fake = cluster(FakeCluster(), [pod("api")])
    plan = ns.plan_taints(NODE, {"taints": [taint()]})

    ns.set_taints(
        NODE, {"taints": [taint()]}, dry_run=False,
        acknowledge_consequences=[entry["code"] for entry in plan["consequences"]],
    )

    assert fake.patches[0]["body"]["spec"]["taints"] == [taint()]


def test_consequences_are_recomputed_at_write_time_not_trusted_from_the_plan(cluster):
    """Pods arrive on a node on their own. A plan taken over an empty node lists
    no consequences, and accepting that list must not authorise a write that now
    deletes something."""
    fake = cluster(FakeCluster(), [])
    empty_plan = ns.plan_taints(NODE, {"taints": [taint()]})
    assert empty_plan["consequences"] == []

    cluster(fake, [pod("api")])
    with pytest.raises(Invalid):
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=False,
                      acknowledge_consequences=[])

    assert fake.patches == []


def test_a_stale_resource_version_conflicts_with_the_current_taints_attached(cluster):
    cluster(FakeCluster(node_object(taints=[taint(effect="NoSchedule")])), [])

    with pytest.raises(Conflict) as caught:
        ns.set_taints(NODE, {"taints": [], "resourceVersion": "1"}, dry_run=True)

    assert caught.value.context["currentResourceVersion"] == "884213"
    assert caught.value.context["currentTaints"] == [taint(effect="NoSchedule")]


def test_a_write_that_changes_nothing_is_refused(cluster):
    cluster(FakeCluster(node_object(taints=[taint()])), [])

    with pytest.raises(Invalid):
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=True)


def test_the_plan_reports_no_change_as_blocked_rather_than_as_an_error(cluster):
    """The plan is the screen where the taints are decided. An error alone would
    withhold the current list at the moment it is the fact needed to pick a
    different one."""
    cluster(FakeCluster(node_object(taints=[taint()])), [])

    plan = ns.plan_taints(NODE, {"taints": [taint()]})

    assert plan["blocked"] is not None
    assert plan["current"] == [taint()]
    assert plan["consequences"] == []


def test_a_failed_pod_listing_leaves_the_plan_at_null_not_empty(cluster):
    """§0.1's corollary, on the read this module cares most about."""
    cluster(FakeCluster(), pod_error=ApiException(status=403, reason="Forbidden"))

    plan = ns.plan_taints(NODE, {"taints": [taint()]})

    assert plan["deleting"] is None
    assert plan["pods_checked"] is False
    assert plan["unavailable"] and plan["unavailable"][0]["resource"] == "pods"
    assert ns.WARN_TAINT_PODS_UNKNOWN in codes(plan["consequences"])


def test_the_plan_echoes_the_gate_so_the_dialog_knows_before_it_renders(cluster):
    cluster(FakeCluster(), [])

    plan = ns.plan_taints(NODE, {"taints": [taint()]})

    assert plan["gate"]["enabled"] is True


def test_a_read_only_console_refuses_the_write_and_not_the_plan(
    fake_k8s, db_engine, monkeypatch,
):
    """`mutations_disabled`, not `rbac_denied`: the operator's permissions are
    irrelevant and telling them otherwise sends them to fix the wrong system.

    The dry run is deliberately **not** withheld. §24's switch is
    `ADMIN_ALLOW_MUTATIONS` alone with `withholds_dry_run` left false, so a
    read-only console still projects the change and still says which pods a
    taint would delete — which is the fact somebody wants before going to ask
    for the permission. Only the confirming call is refused.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "admin_allow_mutations", False)
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review", obj(status=obj(allowed=True))
    )
    fake_k8s.api_client.returns("call_api", FakeCluster())
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))

    assert ns.plan_taints(NODE, {"taints": [taint()]})["gate"]["enabled"] is False
    assert ns.set_taints(NODE, {"taints": [taint()]}, dry_run=True)["applied"] is False
    with pytest.raises(MutationsDisabled):
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=False)


def test_a_denied_preflight_stops_the_write(fake_k8s, db_engine, allow_mutations):
    """§24 goes through the funnel like everything else, so the preflight is the
    funnel's — this asserts it is reached, not what it does."""
    fake_k8s.authorization_v1.returns(
        "create_self_subject_access_review",
        obj(status=obj(allowed=False, denied=True, reason="no", evaluation_error=None)),
    )
    fake = FakeCluster()
    fake_k8s.api_client.returns("call_api", fake)
    fake_k8s.core_v1.returns("list_pod_for_all_namespaces", obj(items=[]))

    with pytest.raises(RBACDenied):
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=True)

    assert fake.patches == []


def test_a_label_write_sends_the_removal_as_null_and_audits_what_moved(cluster):
    fake = cluster(FakeCluster(), [])
    plan = ns.plan_labels(NODE, {"labels": {"node-role.kubernetes.io/worker": ""}})

    ns.set_labels(
        NODE, {"labels": {"node-role.kubernetes.io/worker": ""}}, dry_run=False,
        acknowledge_consequences=[entry["code"] for entry in plan["consequences"]],
    )

    assert fake.patches[0]["body"]["metadata"]["labels"]["team"] is None
    assert "-team" in audit_rows()[0]["detail"]


def test_a_label_plan_names_the_pods_a_removed_key_placed(cluster):
    cluster(FakeCluster(), [pod("api", node_selector={"team": "payments"})])

    plan = ns.plan_labels(NODE, {"labels": {"node-role.kubernetes.io/worker": ""}})

    assert [row["pod"] for row in plan["dependents"]] == ["api"]
    assert plan["dependents"][0]["keys"] == ["team"]


def test_a_failed_listing_leaves_label_dependents_null(cluster):
    cluster(FakeCluster(), pod_error=ApiException(status=500, reason="boom"))

    plan = ns.plan_labels(NODE, {"labels": {}})

    assert plan["dependents"] is None
    assert plan["pods_checked"] is False


def test_an_api_server_refusal_on_the_patch_propagates_and_is_audited(cluster, monkeypatch):
    """The funnel records failures before re-raising: the question after an
    incident is who tried."""
    cluster(FakeCluster(patch_error=ApiException(status=409, reason="Conflict")),
            [pod("api", tolerations=[toleration()])])

    with pytest.raises(Conflict):
        ns.set_taints(NODE, {"taints": [taint()]}, dry_run=False)

    # The funnel records the API server's own code, not a generic failure: the
    # row has to say whether somebody was refused or lost a race.
    assert audit_rows()[0]["outcome"] == "conflict"


def test_the_write_response_carries_the_deletion_list_it_was_confirmed_against(cluster):
    """`applied: true` says the taint list is stored. Which pods are on the way
    out is a separate fact, and the response keeps both so the client that
    recorded the confirmation has the list it accepted."""
    cluster(FakeCluster(), [pod("api")])

    result = ns.set_taints(
        NODE, {"taints": [taint()]}, dry_run=True,
        acknowledge_consequences=[
            ns.WARN_TAINT_DELETES_PODS, ns.WARN_TAINT_DELETES_UNMANAGED,
        ],
    )

    assert [row["pod"] for row in result["deleting"]] == ["api"]
    assert result["pods_checked"] is True
    assert ns.WARN_TAINT_DELETES_PODS in codes(result["consequences"])
