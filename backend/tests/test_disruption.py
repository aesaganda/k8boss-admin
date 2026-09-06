"""
Disruption budgets — what they protect, and what they block (§28).

A PodDisruptionBudget is the one object in Kubernetes whose *failure* mode is
silence. A budget that covers nothing looks exactly like one guarding a
database; a budget that can never allow an eviction looks exactly like one that
allows plenty; and a pod covered by two budgets is un-evictable while both
objects report themselves healthy. None of that appears in `kubectl describe`,
and all of it appears halfway through a node drain.

Each assertion here is about a pair that a listing collapses:

  covers nothing vs unread   `selected_pods: 0` is the finding — this budget
                             constrains no eviction. `null` is a pod listing
                             that did not answer. Rendering the second as the
                             first deletes a working budget as dead.

  never vs not now           `maxUnavailable: 0` can never permit an eviction,
                             at any replica count, ever. `disruptionsAllowed: 0`
                             usually clears on its own. One is a config bug and
                             the other is Tuesday.

  one budget vs two          Kubernetes refuses the eviction outright for a pod
                             covered by two budgets, whatever either says. Both
                             read `disruptionsAllowed: 1` and the pod cannot be
                             evicted by anyone.

  declared vs enforced       The eviction subresource is the enforcer. These are
                             the objects it consults, as a controller last wrote
                             them.

  undecidable vs false       A selector this console cannot evaluate is `None`.
                             Resolving it to "does not match" manufactures the
                             exact "protects nothing" sentence §28 exists to
                             make trustworthy.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.resources.shaping import (
    EVICTION_POLICY_DEFAULT,
    PDB_BLOCKING_NOW,
    PDB_NEVER_ALLOWS,
    PDB_NO_CONSTRAINT,
    PDB_STATUS_STALE,
    poddisruptionbudget_row,
)
from app.services import disruption
from tests.conftest import obj

NAMESPACE = "prod"


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def budget(
    name="api",
    *,
    min_available=None,
    max_unavailable=None,
    selector=None,
    expected=2,
    allowed=1,
    current=2,
    desired=1,
    generation=1,
    observed=1,
    disrupted=None,
    policy=None,
    namespace=NAMESPACE,
):
    spec = {"selector": {"matchLabels": {"app": "api"}} if selector is None else selector}
    if min_available is not None:
        spec["minAvailable"] = min_available
    if max_unavailable is not None:
        spec["maxUnavailable"] = max_unavailable
    if policy is not None:
        spec["unhealthyPodEvictionPolicy"] = policy
    status = {}
    for key, value in (
        ("expectedPods", expected), ("disruptionsAllowed", allowed),
        ("currentHealthy", current), ("desiredHealthy", desired),
        ("observedGeneration", observed),
    ):
        if value is not None:
            status[key] = value
    if disrupted:
        status["disruptedPods"] = {name: "2026-09-06T07:00:00Z" for name in disrupted}
    metadata = {"name": name, "namespace": namespace}
    if generation is not None:
        metadata["generation"] = generation
    return {"metadata": metadata, "spec": spec, "status": status}


def pod(name, *, app="api", namespace=NAMESPACE):
    return {"metadata": {"name": name, "namespace": namespace, "labels": {"app": app}}}


@pytest.fixture
def cluster(monkeypatch):
    """Stub the two listings; every test decides what each returns."""
    state = {"budgets": [], "pods": [], "pods_fail": None}

    def list_budgets(namespace):
        return list(state["budgets"])

    def list_pods(namespace):
        if state["pods_fail"] is not None:
            raise state["pods_fail"]
        return list(state["pods"])

    monkeypatch.setattr(disruption, "_list_budgets", list_budgets)
    monkeypatch.setattr(disruption, "_list_pods", list_pods)
    return state


def codes(row):
    return [finding["code"] for finding in row["findings"]]


def by_name(result, name):
    return next(row for row in result["items"] if row["name"] == name)


# --------------------------------------------------------------------------- #
# The shaper — findings derivable from the object alone
# --------------------------------------------------------------------------- #

def test_max_unavailable_zero_can_never_allow_an_eviction():
    """Unconditional, at any replica count. A drain over these pods does not
    slow down — it does not finish."""
    row = poddisruptionbudget_row(budget(max_unavailable=0, allowed=0))

    assert PDB_NEVER_ALLOWS in codes(row)
    (finding,) = [f for f in row["findings"] if f["code"] == PDB_NEVER_ALLOWS]
    assert "at any replica count" in finding["detail"]


def test_zero_percent_is_the_same_thing_spelled_the_way_a_template_renders_it():
    row = poddisruptionbudget_row(budget(max_unavailable="0%", allowed=0))

    assert PDB_NEVER_ALLOWS in codes(row)


def test_min_available_at_the_pod_count_can_never_allow_and_says_it_is_conditional():
    """Unlike maxUnavailable: 0 this stops being true if the workload scales up,
    so the sentence must not claim a permanent property."""
    row = poddisruptionbudget_row(budget(min_available=2, expected=2, allowed=0))

    (finding,) = [f for f in row["findings"] if f["code"] == PDB_NEVER_ALLOWS]
    assert "at this pod count" in finding["detail"]
    assert "Scaling the workload up" in finding["detail"]


def test_min_available_100_percent_is_named_as_its_own_case():
    """A percentage does not look like a comparison against a replica count
    until somebody does the arithmetic."""
    row = poddisruptionbudget_row(budget(min_available="100%", expected=3, allowed=0))

    assert PDB_NEVER_ALLOWS in codes(row)


def test_a_budget_the_controller_has_not_reached_yet_is_not_called_permanent():
    """`minAvailable` compared against a pod count we do not have is a guess,
    and the guess fires on **every** freshly created budget: `expectedPods` is
    absent until the disruption controller writes it, so treating absence as 0
    makes `minAvailable: 1 >= 0` true and reports a brand-new, correct budget as
    one that can never allow an eviction.

    That is the alarm everyone learns to ignore, arriving on the objects most
    likely to be looked at — the ones somebody just created."""
    row = poddisruptionbudget_row(budget(
        min_available=1, expected=None, allowed=None, current=None,
        desired=None, observed=None,
    ))

    assert row["findings"] == []
    assert row["expected_pods"] is None


def test_an_ordinary_budget_raises_nothing():
    """The common case, and the reason the findings above are worth anything: a
    list that fires on every budget is one nobody reads."""
    assert poddisruptionbudget_row(budget(min_available=1, expected=3, allowed=2))[
        "findings"
    ] == []


def test_min_available_below_the_pod_count_is_not_a_never():
    row = poddisruptionbudget_row(budget(min_available=1, expected=2, allowed=1))

    assert PDB_NEVER_ALLOWS not in codes(row)


def test_a_budget_with_no_constraint_protects_nothing():
    """The API server accepts it and the controller permits every eviction."""
    row = poddisruptionbudget_row(budget(min_available=None, max_unavailable=None))

    assert PDB_NO_CONSTRAINT in codes(row)


def test_blocking_now_is_a_different_finding_from_never():
    """One is a config bug and the other is Tuesday. Collapsing them either
    panics somebody about a healthy budget or hides a permanent one."""
    row = poddisruptionbudget_row(budget(min_available=1, expected=3, allowed=0))

    assert PDB_BLOCKING_NOW in codes(row)
    assert PDB_NEVER_ALLOWS not in codes(row)
    (finding,) = [f for f in row["findings"] if f["code"] == PDB_BLOCKING_NOW]
    assert "usually temporary" in finding["detail"]


def test_a_never_budget_is_not_also_reported_as_blocking_now():
    """Both are true and only one is actionable; two rows would make the
    operator pick."""
    row = poddisruptionbudget_row(budget(max_unavailable=0, allowed=0))

    assert codes(row).count(PDB_NEVER_ALLOWS) == 1
    assert PDB_BLOCKING_NOW not in codes(row)


@pytest.mark.parametrize("allowed", [None])
def test_an_unwritten_disruption_count_is_null_never_zero(allowed):
    """A freshly created budget has no status at all. Drawing that as 0 reports
    a budget as blocking a drain it may be about to permit."""
    row = poddisruptionbudget_row(budget(min_available=1, expected=3, allowed=allowed))

    assert row["disruptions_allowed"] is None
    assert PDB_BLOCKING_NOW not in codes(row)


def test_stale_status_says_the_numbers_answer_for_the_previous_spec():
    row = poddisruptionbudget_row(budget(min_available=1, generation=4, observed=2))

    assert row["status_stale"] is True
    assert PDB_STATUS_STALE in codes(row)


def test_status_freshness_is_unknown_when_the_controller_has_written_nothing():
    """Not "fresh": reporting that as up-to-date would vouch for numbers that do
    not exist."""
    row = poddisruptionbudget_row(budget(min_available=1, generation=1, observed=None))

    assert row["status_stale"] is None
    assert PDB_STATUS_STALE not in codes(row)


def test_an_absent_eviction_policy_reports_the_behaviour_it_means():
    """`IfHealthyBudget` is the default and is the *stricter* one — the reverse
    of what people assume from the word "policy". Reporting null would read as
    unknown for a field whose absence has one defined meaning."""
    assert poddisruptionbudget_row(budget(min_available=1))[
        "unhealthy_pod_eviction_policy"
    ] == EVICTION_POLICY_DEFAULT
    assert poddisruptionbudget_row(budget(min_available=1, policy="AlwaysAllow"))[
        "unhealthy_pod_eviction_policy"
    ] == "AlwaysAllow"


def test_disrupted_pods_are_named_because_they_explain_a_low_count():
    row = poddisruptionbudget_row(budget(min_available=1, disrupted=["api-0"]))

    assert row["disrupted_pods"] == ["api-0"]


# --------------------------------------------------------------------------- #
# The correlation — what needs a pod listing
# --------------------------------------------------------------------------- #

def test_a_budget_whose_selector_matches_nothing_is_the_finding(cluster):
    """It looks identical in YAML to one guarding a production database, and
    the difference is one label key."""
    cluster["budgets"] = [budget("ghost", min_available=1,
                                 selector={"matchLabels": {"app": "gone"}}, expected=0)]
    cluster["pods"] = [pod("api-0")]

    row = by_name(disruption.budgets(NAMESPACE), "ghost")

    assert row["selected_pods"] == 0
    assert disruption.PDB_SELECTS_NOTHING in codes(row)


def test_a_failed_pod_listing_is_null_and_never_zero(cluster):
    """§0.1's corollary at its sharpest: `0` here gets a working budget deleted
    as dead."""
    cluster["budgets"] = [budget("api", min_available=1)]
    cluster["pods_fail"] = ApiException(status=403, reason="Forbidden")

    result = disruption.budgets(NAMESPACE)
    row = by_name(result, "api")

    assert row["selected_pods"] is None
    assert disruption.PDB_SELECTS_NOTHING not in codes(row)
    assert result["partial"] is True
    assert any(entry["resource"] == "pods" for entry in result["unavailable"])


def test_the_budgets_own_numbers_survive_a_failed_pod_listing(cluster):
    """They came from an object that answered. Blanking them would lose the
    findings that need no pods at all."""
    cluster["budgets"] = [budget("api", max_unavailable=0, allowed=0)]
    cluster["pods_fail"] = ApiException(status=403, reason="Forbidden")

    row = by_name(disruption.budgets(NAMESPACE), "api")

    assert row["max_unavailable"] == 0
    assert PDB_NEVER_ALLOWS in codes(row)


def test_an_empty_selector_covers_every_pod_in_the_namespace(cluster):
    """The API's own rule, and it is spelled exactly like a selector somebody
    forgot to fill in."""
    cluster["budgets"] = [budget("all", min_available=1, selector={})]
    cluster["pods"] = [pod("api-0"), pod("web-0", app="web")]

    assert by_name(disruption.budgets(NAMESPACE), "all")["selected_pods"] == 2


def test_a_budget_does_not_cover_pods_in_another_namespace(cluster):
    cluster["budgets"] = [budget("api", min_available=1)]
    cluster["pods"] = [pod("api-0"), pod("api-9", namespace="staging")]

    assert by_name(disruption.budgets(None), "api")["selected_pods"] == 1


def test_an_undecidable_selector_is_unknown_rather_than_no_match(cluster):
    """`False` here would manufacture the "protects nothing" sentence §28 exists
    to make trustworthy, so an operator this console does not model has to reach
    the response as its own state. The matcher is tri-state for exactly this."""
    cluster["budgets"] = [budget("api", min_available=1, selector={
        "matchExpressions": [{"key": "app", "operator": "Wibble", "values": ["api"]}],
    })]
    cluster["pods"] = [pod("api-0")]

    row = by_name(disruption.budgets(NAMESPACE), "api")

    assert row["selected_pods"] is None
    assert row["undecidable_pods"] == 1
    assert disruption.PDB_SELECTION_UNKNOWN in codes(row)
    assert disruption.PDB_SELECTS_NOTHING not in codes(row)
    (finding,) = [f for f in row["findings"] if f["code"] == disruption.PDB_SELECTION_UNKNOWN]
    assert "not** a report that the budget covers nothing" in finding["detail"]


# --------------------------------------------------------------------------- #
# The overlap — the finding no single budget can carry
# --------------------------------------------------------------------------- #

def test_two_budgets_covering_one_pod_make_it_unevictable(cluster):
    """Kubernetes does not support overlapping budgets: the eviction API refuses
    the pod outright, whatever either budget's disruptionsAllowed says. Both
    objects here report themselves perfectly healthy."""
    cluster["budgets"] = [
        budget("api", min_available=1, allowed=1),
        budget("api-extra", max_unavailable=1, allowed=1),
    ]
    cluster["pods"] = [pod("api-0")]

    result = disruption.budgets(NAMESPACE)

    for name in ("api", "api-extra"):
        row = by_name(result, name)
        assert disruption.PDB_OVERLAPS in codes(row), name
        assert row["disruptions_allowed"] == 1, "both look healthy — that is the point"
    assert result["overlappingPods"] == [
        {"pod": "prod/api-0", "budgets": ["prod/api", "prod/api-extra"]},
    ]


def test_each_overlapping_budget_names_the_other(cluster):
    cluster["budgets"] = [budget("api", min_available=1), budget("api-extra", min_available=1)]
    cluster["pods"] = [pod("api-0")]

    row = by_name(disruption.budgets(NAMESPACE), "api")
    (finding,) = [f for f in row["findings"] if f["code"] == disruption.PDB_OVERLAPS]

    assert "prod/api-extra" in finding["detail"]
    assert "prod/api" not in finding["detail"].replace("prod/api-extra", "")


def test_one_budget_per_pod_is_not_an_overlap(cluster):
    cluster["budgets"] = [
        budget("api", min_available=1),
        budget("web", min_available=1, selector={"matchLabels": {"app": "web"}}),
    ]
    cluster["pods"] = [pod("api-0"), pod("web-0", app="web")]

    result = disruption.budgets(NAMESPACE)

    assert result["overlappingPods"] == []
    assert all(disruption.PDB_OVERLAPS not in codes(row) for row in result["items"])


def test_budgets_in_different_namespaces_do_not_overlap(cluster):
    """Both are called `api` and both select `app=api`. They cover different
    pods, and a console that keyed on the name alone would report every
    multi-namespace deployment as broken."""
    cluster["budgets"] = [budget("api"), budget("api", namespace="staging")]
    cluster["pods"] = [pod("api-0"), pod("api-0", namespace="staging")]

    assert disruption.budgets(None)["overlappingPods"] == []


def test_the_overlap_index_is_null_when_the_pods_could_not_be_read(cluster):
    """`[]` would say the console checked and found none."""
    cluster["budgets"] = [budget("api", min_available=1)]
    cluster["pods_fail"] = ApiException(status=403, reason="Forbidden")

    assert disruption.budgets(NAMESPACE)["overlappingPods"] is None


# --------------------------------------------------------------------------- #
# The listing and the endpoint
# --------------------------------------------------------------------------- #

def test_a_cluster_with_no_budgets_is_an_empty_list_not_a_failure(cluster):
    """Nothing constrains eviction here — the finding, and a real answer."""
    result = disruption.budgets(NAMESPACE)

    assert result["items"] == []
    assert result["partial"] is False


def test_a_refused_budget_listing_raises_rather_than_emptying_the_page(cluster,
                                                                       monkeypatch):
    """An empty table there would say "nothing constrains eviction on this
    cluster", which is the finding rather than the failure."""
    def boom(namespace):
        raise ApiException(status=403, reason="Forbidden")

    monkeypatch.setattr(disruption, "_list_budgets", boom)

    with pytest.raises(ApiException):
        disruption.budgets(NAMESPACE)


def test_rows_are_ordered_by_namespace_then_name(cluster):
    cluster["budgets"] = [
        budget("zebra", namespace="staging"), budget("api"), budget("beta"),
    ]
    cluster["pods"] = []

    names = [(r["namespace"], r["name"]) for r in disruption.budgets(None)["items"]]
    assert names == [("prod", "api"), ("prod", "beta"), ("staging", "zebra")]


def test_the_endpoint_returns_the_envelope(client, cluster_id, cluster, fake_k8s):
    cluster["budgets"] = [budget("api", max_unavailable=0, allowed=0)]
    cluster["pods"] = [pod("api-0")]

    response = client.get("/api/disruption/budgets",
                          params={"cluster_id": cluster_id, "namespace": NAMESPACE})

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["name"] == "api"
    assert PDB_NEVER_ALLOWS in [f["code"] for f in body["items"][0]["findings"]]
    assert body["overlappingPods"] == []
