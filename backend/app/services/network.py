"""
Network policy read models (§8.4) — who a policy selects, and who nothing selects.

The generic §4 endpoint already returns the typed NetworkPolicy row, because
:func:`app.resources.shaping.networkpolicy_row` is registered in the shaper
registry. This module exists for the two questions that row cannot answer from
the object in front of it, because both need a pod listing:

* **What does this policy actually select?** A NetworkPolicy whose
  ``podSelector`` matches no pod is inert. It looks identical in YAML to one
  guarding a production database, and the difference is one label key.
* **What does nothing select?** This is the question network policy exists for.
  A pod that no policy selects is *unrestricted* — Kubernetes' default is allow —
  and finding those is not a matter of reading the policies, it is a matter of
  reading every pod and subtracting.

Three properties are load-bearing.

**This module reports what is declared, never what is enforced.** NetworkPolicy
objects are inert unless the cluster's CNI plugin implements them, and no API the
console can reach says whether it does: a cluster running flannel without
``kube-network-policies`` accepts, stores and serves these objects while
forwarding every packet they claim to drop. So nothing here returns a field
called ``enforced``, ``blocked`` or ``protected``. ``ingress.isolated`` means
"some policy selecting this pod declares an ingress section", which is a fact
about the API server's contents and is the strongest true statement available.
The UI carries the caveat; see ``docs/safety-model.md`` §11.

**A pod whose selection could not be decided is ``None``, never ``False``.**
:func:`app.resources.shaping.label_selector_matches` is tri-state, and its
``None`` propagates all the way to the response with an ``unavailable`` entry
carrying ``unsupported``. Collapsing it to "not selected" would print the
sentence this whole module exists to make trustworthy — *nothing protects this
pod* — about a pod that may well be protected.

**Isolation is a union, not an intersection.** Several policies selecting one pod
add their allowances together; they never narrow each other. So one policy
permitting everything makes the combined effect ``allow_all`` no matter how
restrictive its neighbours are, and that is what :func:`_combined_effect`
computes. A reader who expects the rules to compose like a firewall's
first-match-wins chain gets this backwards, which is why it is computed here once
rather than in each caller.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterable, Iterator

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import get_core_v1, get_networking_v1
from app.resources import shaping
from app.resources.envelope import collect, envelope, unavailable_entry

logger = logging.getLogger(__name__)

#: Detail on the ``unsupported`` entry a undecidable selector produces. One
#: spelling, because two callers each writing their own would eventually
#: disagree about what the operator is being told.
_UNDECIDABLE_DETAIL = (
    "A pod selector on this cluster uses a matchExpressions operator this "
    "console cannot evaluate, so which pods are selected is unknown rather "
    "than empty."
)


@contextlib.contextmanager
def _api_errors(**context: Any) -> Iterator[None]:
    """Turn an ``ApiException`` raised inside the block into a typed AdminError.

    Same helper, and the same reasoning, as :mod:`app.api.namespaces`: an
    ``rbac_denied`` that does not name the verb and the resource sends an
    operator to read a ClusterRole line by line. Nested inside
    :func:`~app.resources.envelope.collect` it is also what gives the
    ``unavailable`` entry ``forbidden`` rather than the catch-all ``unreachable``.
    """
    try:
        yield
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


def _items(listing: Any) -> list[Any]:
    return list(shaping.get_field(listing, "items", default=[]) or [])


def _list_policies(namespace: str | None) -> list[Any]:
    """Every NetworkPolicy in ``namespace``, or in the cluster when it is ``None``."""
    api = get_networking_v1()
    if namespace:
        return _items(api.list_namespaced_network_policy(namespace))
    return _items(api.list_network_policy_for_all_namespaces())


def _list_pods(namespace: str | None) -> list[Any]:
    """Every pod in ``namespace``, or in the cluster when it is ``None``.

    One listing, not one per policy. A namespace with forty policies would
    otherwise make forty identical round trips to answer one page, and the API
    read deadline fires long before the last of them.
    """
    api = get_core_v1()
    if namespace:
        return _items(api.list_namespaced_pod(namespace))
    return _items(api.list_pod_for_all_namespaces())


def _selects(policy: Any, pod: Any) -> bool | None:
    """Does ``policy`` select ``pod``? Tri-state; ``None`` is "cannot be decided".

    A NetworkPolicy selects pods **in its own namespace only** — ``podSelector``
    is not cluster-wide, and a console that matched across namespaces would
    report a policy in ``dev`` as protecting a pod in ``prod``. The namespace
    comparison happens before the selector, so a cross-namespace pair is a plain
    ``False`` rather than something the selector has to be consulted about.
    """
    if shaping.get_field(policy, "metadata", "namespace") != shaping.get_field(
        pod, "metadata", "namespace"
    ):
        return False
    return shaping.label_selector_matches(
        shaping.get_field(policy, "spec", "podSelector"),
        shaping.get_field(pod, "metadata", "labels", default={}),
    )


def _combined_effect(effects: Iterable[str]) -> str:
    """Fold several governing policies' effects into the one the cluster would apply.

    Union semantics: allowances add. ``allow_all`` anywhere wins outright, and
    ``deny_all`` only survives when *every* governing policy denies everything —
    a single restricted neighbour turns the pair into ``restricted``, because the
    pod can now receive the traffic that neighbour permits.
    """
    values = list(effects)
    if "allow_all" in values:
        return "allow_all"
    if all(value == "deny_all" for value in values):
        return "deny_all"
    return "restricted"


def _direction_state(
    rows: list[dict[str, Any]], key: str, *, undecided: bool
) -> dict[str, Any]:
    """``{isolated, effect, policies}`` for one direction over the policies on a pod.

    ``rows`` are the §8.3 rows of the policies that *definitely* select the pod;
    ``undecided`` says whether some other policy's selector could not be
    evaluated against it.

    The two flags are separate because they fail in opposite directions. A pod
    definitely selected by a governing policy **is** isolated, whatever else is
    undecided — so ``isolated`` stays ``True`` and only ``effect`` goes ``None``,
    since an undecided policy could widen what is permitted. A pod with no
    definitely-governing policy and something undecided is ``isolated: None``:
    not "unrestricted", which is the sentence an operator would act on.
    """
    governing = [row for row in rows if row[key]["governed"]]
    if governing:
        return {
            "isolated": True,
            "effect": None if undecided else _combined_effect(
                row[key]["effect"] for row in governing
            ),
            "policies": [row["name"] for row in governing],
        }
    if undecided:
        return {"isolated": None, "effect": None, "policies": []}
    return {"isolated": False, "effect": None, "policies": []}


def get_network_policy(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/network/policies/{namespace}/{name}`` (§8.4).

    The §8.3 row, plus the pods the policy selects.

    ``selected_pods`` is ``None`` — never ``[]`` — when the pod listing failed or
    when a selector could not be evaluated, and the reason is in
    ``unavailable[]``. An empty list here says *this policy governs nothing*,
    which is the finding that gets a policy deleted as dead; it must never be
    what a failed read looks like.

    The policy read is this endpoint's **primary** read and raises. The pod
    listing is secondary: losing it costs the panel, not the page, and the
    declared rules are still worth showing to an operator who cannot list pods.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(
        verb="get", group="networking.k8s.io", resource="networkpolicies",
        namespace=namespace, name=name,
    ):
        policy = get_networking_v1().read_namespaced_network_policy(name, namespace)

    row = shaping.networkpolicy_row(policy)

    # None, not []: `pods` staying None is what carries a failed listing into a
    # null `selected_pods` instead of an empty one.
    pods: list[Any] | None = None
    with collect(unavailable, "", "pods", namespace=namespace), _api_errors(
        verb="list", group="", resource="pods", namespace=namespace
    ):
        pods = _list_pods(namespace)

    selected: list[Any] | None = None
    if pods is not None:
        matched: list[Any] = []
        undecided = False
        for pod in pods:
            verdict = _selects(policy, pod)
            if verdict is None:
                undecided = True
                break
            if verdict:
                matched.append(pod)
        if undecided:
            unavailable.append(
                unavailable_entry(
                    "networking.k8s.io", "networkpolicies", "unsupported",
                    detail=_UNDECIDABLE_DETAIL, namespace=namespace,
                )
            )
        else:
            selected = matched

    row["selected_pods"] = (
        None if selected is None else [shaping.pod_row(pod) for pod in selected]
    )
    row["selected_pod_count"] = None if selected is None else len(selected)
    row["unavailable"] = unavailable
    row["partial"] = bool(unavailable)
    return row


def namespace_isolation(namespace: str | None = None) -> dict[str, Any]:
    """``GET /api/network/isolation`` (§8.4) — every pod, and what selects it.

    The inverse listing: pods are the rows, policies are the decoration. That
    ordering is the point of the endpoint. Reading the policy list tells an
    operator what they wrote; reading this tells them what they missed, and the
    pods nothing selects are the rows that never appear on any policy's page.

    **Both reads are primary and both raise.** Losing the pod listing leaves no
    rows. Losing the *policy* listing would leave rows that say ``isolated:
    false`` for every pod in the namespace — a page confidently reporting that
    nothing is protected, assembled entirely from a read that failed. Degrading
    to nulls instead was the alternative and it is worse than a clean error: the
    page would render, the operator would scan a table of em dashes, and the
    reason would be one banner away from the number they were looking at.

    A pod's ``host_network`` flag is carried through untouched and not acted on.
    Most CNI implementations do not apply NetworkPolicy to host-network pods, but
    "most" is not a claim this console can make about a plugin it cannot see, so
    the flag is surfaced and the sentence is left to the operator.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(
        verb="list", group="networking.k8s.io", resource="networkpolicies",
        namespace=namespace,
    ):
        policies = _list_policies(namespace)

    with _api_errors(verb="list", group="", resource="pods", namespace=namespace):
        pods = _list_pods(namespace)

    # Shaped once, in step with `policies`, rather than per pod: a namespace with
    # forty policies and four hundred pods would otherwise reshape every policy
    # four hundred times to produce the same rows.
    policy_rows = [shaping.networkpolicy_row(policy) for policy in policies]
    any_undecided = False
    pod_rows: list[dict[str, Any]] = []

    # ponytail: the full pod x policy cross product, the same shape §28's budget
    # correlation keeps and for the same reason. Grouping the policies by
    # namespace first is the upgrade — a dict of namespace -> (policy, row)
    # pairs built once above this loop — and it measured the same order of
    # improvement there. It is not built yet because the pod listing and its
    # deserialization dominate this page by several times, so the index would
    # take out a term that is not the bottleneck while adding a second structure
    # that has to stay in step with `policy_rows`. Measure the read first.
    for pod in pods:
        selecting: list[dict[str, Any]] = []
        undecided = False
        for policy, policy_row in zip(policies, policy_rows):
            verdict = _selects(policy, pod)
            if verdict is None:
                undecided = True
                any_undecided = True
                continue
            if verdict:
                selecting.append(policy_row)

        row = shaping.pod_row(pod)
        row["labels"] = {
            str(k): v
            for k, v in (shaping.get_field(pod, "metadata", "labels", default={}) or {}).items()
        }
        row["host_network"] = bool(shaping.get_field(pod, "spec", "hostNetwork", default=False))
        row["policies"] = [policy["name"] for policy in selecting]
        row["ingress"] = _direction_state(selecting, "ingress", undecided=undecided)
        row["egress"] = _direction_state(selecting, "egress", undecided=undecided)
        pod_rows.append(row)

    if any_undecided:
        unavailable.append(
            unavailable_entry(
                "networking.k8s.io", "networkpolicies", "unsupported",
                detail=_UNDECIDABLE_DETAIL, namespace=namespace,
            )
        )

    # Sorted so two identical reads render in the same order: the API server
    # guarantees no ordering between listings, and a table that reshuffles under
    # the cursor gets the wrong row clicked.
    pod_rows.sort(key=lambda row: ((row.get("namespace") or ""), (row.get("name") or "")))

    result = envelope(pod_rows, unavailable=unavailable)
    result["namespace"] = namespace
    result["policy_count"] = len(policies)
    result["summary"] = _isolation_summary(pod_rows)
    return result


def _isolation_summary(pod_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Tallies for the headline, counting the three states separately.

    ``unknown`` is its own number rather than being folded into either side.
    Adding it to ``unrestricted`` overstates the exposure and adding it to
    ``isolated`` understates it; keeping it apart is the only arrangement where
    the three numbers sum to the pod count and none of them is a guess.
    """
    summary: dict[str, Any] = {"pod_count": len(pod_rows)}
    for direction in ("ingress", "egress"):
        states = [row[direction]["isolated"] for row in pod_rows]
        summary[direction] = {
            "isolated": sum(1 for state in states if state is True),
            "unrestricted": sum(1 for state in states if state is False),
            "unknown": sum(1 for state in states if state is None),
        }
    summary["host_network"] = sum(1 for row in pod_rows if row.get("host_network"))
    return summary


__all__ = ["get_network_policy", "namespace_isolation"]
