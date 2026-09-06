"""
Disruption budget read models (§28) — what a budget protects, and what it blocks.

`app.resources.shaping.poddisruptionbudget_row` is registered in the shaper
registry, so §4's generic browser already returns the typed row. This module
exists for the three questions that row cannot answer from the object in front
of it, because each needs a pod listing:

* **Does this budget select anything?** A PodDisruptionBudget whose selector
  matches no pod is inert. It looks identical in YAML to one guarding a
  production database, and the difference is one label key. A team reads the
  object, sees `minAvailable: 2`, and believes it has availability protection it
  does not have. This is §8.4's "a policy that selects nothing is inert",
  pointed at availability instead of at network isolation.

* **Is any pod covered by two budgets?** Kubernetes does not support that, and
  the way it does not support it is worth reading twice: the eviction API
  **refuses the eviction outright** for a pod matched by more than one budget,
  regardless of what either budget's numbers say. Both objects can read
  `disruptionsAllowed: 5` and the pod is still un-evictable. Nothing on either
  budget says so, `kubectl describe` does not say so, and the symptom is a node
  drain that fails on one pod with a message about a condition nobody set.

* **What does nothing cover?** The inversion, and the one an operator asks
  before an upgrade rather than during it. A multi-replica workload with no
  budget is evicted freely — which is often correct and is never *stated*
  anywhere, so it cannot be checked.

Three properties are load-bearing.

**This module reports what is declared, never what is enforced.** The eviction
subresource is the enforcer, and `disruptionsAllowed` is a number the disruption
controller wrote at some past moment. So nothing here returns a field called
`safe`, `protected` or `will_block`: `blocking_now` means "the controller's last
written count was zero", which is a fact about the API server's contents and the
strongest true statement available. §5's drain plan asks the same objects a
different question — *will this specific eviction be refused* — and answers it
per pod at the moment of the drain.

**A pod whose selection could not be decided is `None`, never `False`.**
:func:`app.resources.shaping.label_selector_matches` is tri-state and its `None`
propagates all the way to the response. This module deliberately does **not**
use :func:`app.services.workloads.selector_matches`, which resolves an
unmodelled operator to `False`. That is the right direction there — attributing
other workloads' pods to a row is worse than attributing none — and the wrong
direction here, because `False` manufactures the exact "this budget protects
nothing" sentence §28 exists to make trustworthy.

**A count we could not derive is `None`, never `0`.** `selected_pods: 0` is the
finding. `selected_pods: null` is a pod listing that did not answer, and the two
render differently for the same reason §0.1 gives about every other count in
this tree.
"""

from __future__ import annotations

import logging
from typing import Any

from app.k8s.client import get_core_v1
from app.resources import catalog, reader, shaping
from app.resources.envelope import collect, envelope

logger = logging.getLogger(__name__)

#: Findings that need the pod listing. The object-only ones live in the shaper.
PDB_SELECTS_NOTHING = "pdb_selects_nothing"
PDB_SELECTION_UNKNOWN = "pdb_selection_unknown"
PDB_OVERLAPS = "pdb_overlaps"


def _items(listing: Any) -> list[Any]:
    return list(shaping.get_field(listing, "items", default=[]) or [])


def _list_budgets(namespace: str | None) -> list[Any]:
    """Every PodDisruptionBudget in scope, over the raw path.

    There is no typed `policy/v1` client in the bundle, so this takes the same
    route §5's drain plan takes for the same objects. Deliberately **not**
    :func:`app.resources.reader.list_resource`, whose ``limit`` defaults to 500:
    a truncated budget listing would drop budgets from the overlap index, and
    the overlap finding's whole value is that it is exhaustive — a pod reported
    as covered once, when a five-hundred-and-first budget also covers it, is the
    un-evictable pod this module exists to find, described as fine.
    """
    return _items(catalog.raw_get(
        reader.resource_path("policy", "v1", "poddisruptionbudgets", namespace=namespace)
    ))


def _list_pods(namespace: str | None) -> list[Any]:
    """One listing, not one per budget.

    A namespace with forty budgets would otherwise make forty identical round
    trips to answer one page, and the read deadline fires long before the last.

    Unpaged for :func:`_list_budgets`'s reason: a capped pod listing turns
    ``selected_pods`` into a floor while it still reads as a total, and ``0``
    from a truncated page is the "this budget protects nothing" sentence
    delivered about a budget that protects plenty.
    """
    api = get_core_v1()
    if namespace:
        return _items(api.list_namespaced_pod(namespace))
    return _items(api.list_pod_for_all_namespaces())


def _covers(budget: Any, pod: Any) -> bool | None:
    """Does ``budget`` cover ``pod``? Tri-state; ``None`` cannot be decided.

    A PodDisruptionBudget covers pods **in its own namespace only**, so the
    namespace comparison happens before the selector and a cross-namespace pair
    is a plain ``False`` rather than something the selector is consulted about.

    An **empty** selector matches every pod in the namespace, which is the
    API's own rule and is spelled exactly like a selector somebody forgot to
    fill in. `label_selector_matches` implements that rule; this function does
    not second-guess it, because a budget covering a whole namespace is a real
    and deliberate configuration.

    A **missing** selector is `None` rather than "covers nothing": the field is
    required, so its absence means an object this console does not understand,
    and reporting that as an inert budget is a claim about somebody's
    availability made by a console that could not read the object.
    """
    if shaping.get_field(budget, "metadata", "namespace") != shaping.get_field(
        pod, "metadata", "namespace"
    ):
        return False
    return shaping.label_selector_matches(
        shaping.get_field(budget, "spec", "selector"),
        shaping.get_field(pod, "metadata", "labels", default={}),
    )


def _pod_key(pod: Any) -> str:
    return (
        f"{shaping.get_field(pod, 'metadata', 'namespace')}/"
        f"{shaping.get_field(pod, 'metadata', 'name')}"
    )


def _budget_key(budget: Any) -> str:
    return (
        f"{shaping.get_field(budget, 'metadata', 'namespace')}/"
        f"{shaping.get_field(budget, 'metadata', 'name')}"
    )


def budgets(namespace: str | None = None) -> dict[str, Any]:
    """``GET /api/disruption/budgets`` (§28).

    The budget listing is **primary and raises**: there is no useful page for a
    cluster whose budgets could not be read, and an empty table there would say
    "nothing constrains eviction here", which is the finding rather than the
    failure.

    The pod listing is **collected**. Losing it costs every pod-derived field —
    which becomes `null`, never `0` — and leaves each budget's own declared
    numbers on screen, because those came from an object that answered.
    """
    unavailable: list[dict[str, Any]] = []

    budget_objects = _list_budgets(namespace)

    pods: list[Any] | None = None
    with collect(unavailable, "", "pods", namespace=namespace):
        pods = _list_pods(namespace)

    # pod key -> the budgets covering it. Built once over the cross product
    # rather than per budget, because the overlap finding needs the inverse
    # index and computing it twice would let the two disagree.
    covering: dict[str, list[str]] = {}
    counts: dict[str, int | None] = {}
    undecided: dict[str, int] = {}

    for budget in budget_objects:
        key = _budget_key(budget)
        if pods is None:
            counts[key] = None
            continue
        matched = 0
        for pod in pods:
            verdict = _covers(budget, pod)
            if verdict is None:
                undecided[key] = undecided.get(key, 0) + 1
                continue
            if verdict:
                matched += 1
                covering.setdefault(_pod_key(pod), []).append(key)
        # A budget with an undecidable pod has a count that is a floor, not a
        # total — so it is reported as unknown rather than as a number that
        # would read as complete.
        counts[key] = None if key in undecided else matched

    overlapping = {
        pod: names for pod, names in covering.items() if len(names) > 1
    }
    overlapped_budgets: dict[str, set[str]] = {}
    for pod, names in overlapping.items():
        for name in names:
            overlapped_budgets.setdefault(name, set()).update(
                other for other in names if other != name
            )

    rows: list[dict[str, Any]] = []
    for budget in budget_objects:
        key = _budget_key(budget)
        row = shaping.poddisruptionbudget_row(budget)
        row["selected_pods"] = counts.get(key)
        row["undecidable_pods"] = undecided.get(key) or None

        findings = list(row["findings"])
        if counts.get(key) == 0:
            findings.append({
                "code": PDB_SELECTS_NOTHING,
                "label": "This budget covers no pods",
                "detail": (
                    "Its selector matches nothing in this namespace, so it "
                    "constrains no eviction. The object exists, reads as "
                    "protection, and provides none — usually one label key "
                    "apart from the workload it was written for."
                ),
            })
        if key in undecided:
            findings.append({
                "code": PDB_SELECTION_UNKNOWN,
                "label": "Which pods this covers could not be decided",
                "detail": (
                    f"{undecided[key]} pod(s) could not be evaluated against "
                    "this selector — it uses a matchExpressions operator this "
                    "console does not model. The count above is unknown rather "
                    "than a total, and this is **not** a report that the budget "
                    "covers nothing."
                ),
            })
        if key in overlapped_budgets:
            others = ", ".join(sorted(overlapped_budgets[key]))
            findings.append({
                "code": PDB_OVERLAPS,
                "label": "A pod here is covered by more than one budget",
                "detail": (
                    f"These pods are also covered by {others}. Kubernetes does "
                    "not support overlapping budgets: the eviction API refuses "
                    "the eviction outright for such a pod, whatever either "
                    "budget's disruptionsAllowed says. A drain over them fails "
                    "on that pod and nothing on either object explains why."
                ),
            })
        row["findings"] = findings
        rows.append(row)

    rows.sort(key=lambda r: (str(r["namespace"] or ""), str(r["name"] or "")))

    return envelope(
        rows,
        unavailable=unavailable,
    ) | {
        # The inverse index, kept beside the rows because it is the answer to a
        # question no single row can carry: which *pods* are un-evictable by the
        # overlap rule. `None` when the pod listing failed — never `{}`, which
        # would say the console checked and found none.
        "overlappingPods": (
            None if pods is None
            else [
                {"pod": pod, "budgets": sorted(names)}
                for pod, names in sorted(overlapping.items())
            ]
        ),
    }


__all__ = [
    "PDB_OVERLAPS",
    "PDB_SELECTION_UNKNOWN",
    "PDB_SELECTS_NOTHING",
    "budgets",
]
