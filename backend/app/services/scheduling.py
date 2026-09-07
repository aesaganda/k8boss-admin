"""
§31 — why is this pod Pending.

The most common question anybody asks a Kubernetes console, and the one this
console answered with the word `Pending` and nothing else. The answer exists —
the scheduler computed it and wrote it into an event — and it is three clicks
away in a list nobody filters.

**The authoritative answer is the scheduler's, and it is quoted rather than
derived.** `0/5 nodes are available: 3 Insufficient cpu, 2 node(s) had
untolerated taint {gpu: true}` is the scheduler's own verdict over the whole
predicate chain, including the plugins this console does not model. It is
surfaced verbatim.

Three things about that message are true and never said, and each of them turns
it from an answer into a wrong one:

**It is a snapshot, not a status.** The event carries the moment the last
scheduling attempt failed. A node added since, a pod deleted since, a taint
removed since — none of them update it. An operator reading a forty-minute-old
message as the current state goes looking for capacity that has been there for
half an hour.

**Its absence is not innocence.** Events age out of etcd — one hour by default —
so a pod pending since this morning has an explanation that expired, not no
explanation. `scheduler: null` here means *no FailedScheduling event is readable
right now*, and this module says so rather than letting the null read as "the
scheduler has not rejected it".

**Half of Pending is not about the scheduler at all.** A pod with
`spec.nodeName` already set has been placed; it is waiting on the kubelet —
pulling an image, mounting a volume, running an init container. Sending that
operator to look at node capacity is sending them to the wrong machine entirely,
so the first thing this endpoint reports is which of the two it is.

**And the per-node table is a re-derivation, which is why it never says a node
fits.** For each node this module checks what it can check: cordoned, NotReady,
a `NoSchedule`/`NoExecute` taint the pod does not tolerate, a `nodeSelector` that
does not match, and requests that exceed allocatable minus what is already
requested there. Each is a reason to **rule the node out**. Nothing here is a
reason to rule one *in*: the scheduler also weighs affinity and anti-affinity,
topology spread, volume node affinity and zone, extended resources, host ports
and every scheduling plugin the cluster runs — none of which this evaluates. So
a node comes back `ruled_out` with its reasons, or `no_reason_found`, and the
second is never rendered as "this node has room". A console that promoted it
would send somebody to argue with the scheduler about a node it had already
rejected for a reason this console cannot see.
"""

from __future__ import annotations

import logging
from typing import Any

from app.k8s.client import get_core_v1, get_storage_v1
from app.resources import reader, shaping
from app.resources.envelope import collect
from app.services import events as events_service
from app.services.nodes import (
    active_pods_by_node,
    cpu_cores,
    memory_bytes,
    node_ready,
    pod_capacity,
    pod_requests,
    requested_total,
)

logger = logging.getLogger(__name__)

#: The event reason the scheduler writes when it cannot place a pod. It is the
#: only one this module reads: `Scheduled` says it succeeded, and every other
#: reason on a pod belongs to the kubelet, which is a different question.
FAILED_SCHEDULING = "FailedScheduling"

#: What the pod is waiting for. Three answers, and the first two send an operator
#: to two different places.
WAITING_ON_SCHEDULER = "scheduler"
WAITING_ON_KUBELET = "kubelet"
WAITING_ON_NOTHING = "nothing"

#: Per-node verdicts. There is deliberately no `fits`.
RULED_OUT = "ruled_out"
NO_REASON_FOUND = "no_reason_found"

#: Rule-out codes. Each is something this console can check from the objects in
#: front of it, and each is a reason the scheduler would also reject the node.
NODE_CORDONED = "node_cordoned"
NODE_NOT_READY = "node_not_ready"
NODE_TAINTED = "node_untolerated_taint"
NODE_SELECTOR_MISMATCH = "node_selector_mismatch"
NODE_INSUFFICIENT_CPU = "node_insufficient_cpu"
NODE_INSUFFICIENT_MEMORY = "node_insufficient_memory"
NODE_POD_SLOTS_FULL = "node_pod_slots_full"

#: The taint effects that stop a pod being placed. `PreferNoSchedule` does not:
#: it is a scoring signal, and a node carrying only that one still takes the pod
#: when nothing better exists. Ruling it out here would report a node as
#: unavailable that the scheduler is willing to use.
BLOCKING_EFFECTS = ("NoSchedule", "NoExecute")

#: How many events to scan for the pod's own FailedScheduling. The scheduler
#: rewrites one event with an increasing `count` rather than emitting a new one
#: per attempt, so the interesting one is near the top of a time-ordered list.
EVENT_LIMIT = 50


# --------------------------------------------------------------------------- #
# What the pod is waiting for
# --------------------------------------------------------------------------- #

def waiting_on(pod: Any) -> str:
    """``scheduler``, ``kubelet`` or ``nothing`` — the first question to answer.

    A `Pending` pod that already carries ``spec.nodeName`` has been **placed**.
    Whatever is wrong with it is on that node — an image that will not pull, a
    volume that will not mount, an init container that has not finished — and
    node capacity has nothing to do with it. The two are one phase in the API and
    two entirely different investigations, so they are two answers here.

    Anything that is not `Pending` is `nothing`: the pod is running, finished or
    gone, and its placement is settled. The endpoint still answers, because "why
    was this pod put *there*" is asked about running pods too, and answering it
    with an error would be answering a different question.
    """
    if shaping.get_field(pod, "status", "phase") != "Pending":
        return WAITING_ON_NOTHING
    return WAITING_ON_KUBELET if shaping.get_field(pod, "spec", "nodeName") else WAITING_ON_SCHEDULER


def scheduled_condition(pod: Any) -> dict[str, Any] | None:
    """The ``PodScheduled`` condition, verbatim, or ``None`` if absent.

    Absent is a real state and not an error: the API server writes no conditions
    at all until something has evaluated the pod, so a pod created a moment ago
    has none. It is returned as ``None`` rather than synthesised into a false
    "not scheduled", which would attribute a verdict to a scheduler that has not
    looked yet.
    """
    for condition in shaping.get_field(pod, "status", "conditions", default=[]) or []:
        if shaping.get_field(condition, "type") != "PodScheduled":
            continue
        return {
            "status": shaping.get_field(condition, "status"),
            "reason": shaping.get_field(condition, "reason"),
            "message": shaping.get_field(condition, "message"),
            "last_transition": shaping.rfc3339(
                shaping.get_field(condition, "lastTransitionTime")
            ),
        }
    return None


# --------------------------------------------------------------------------- #
# The scheduler's own answer
# --------------------------------------------------------------------------- #

def scheduler_verdict(
    namespace: str, name: str, unavailable: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The pod's newest ``FailedScheduling`` event, with its age. Or ``None``.

    **``None`` is three different things and the response says so rather than
    picking one.** The scheduler may not have rejected this pod; it may have
    rejected it and the event may have aged out of etcd (one hour by default);
    or the event listing may have failed, in which case it is also in
    ``unavailable`` and the endpoint is `partial`. What ``None`` never means is
    "the scheduler is happy with this pod" — a pod pending since this morning
    has an explanation that expired, and rendering that as no explanation is the
    confidently wrong answer this module exists to avoid.

    ``age_seconds`` is the field that makes the message usable. The scheduler
    writes what it saw at that instant; a node added since, a pod deleted since,
    a taint removed since — none of them rewrite it. An old message is a record
    of a past attempt, not a statement about the cluster now.
    """
    listing: dict[str, Any] | None = None
    with collect(unavailable, "", "events", namespace=namespace):
        listing = events_service.list_events(
            namespace=namespace,
            involved_kind="Pod",
            involved_name=name,
            limit=EVENT_LIMIT,
        )
    if listing is None:
        return None

    # `list_events` degrades rather than raising, so its own `unavailable`
    # entries have to be carried across: an event scan that was truncated or
    # refused is exactly the case where "no FailedScheduling event" must not be
    # read as "the scheduler did not reject this".
    unavailable.extend(listing.get("unavailable") or [])

    for event in listing.get("items") or []:
        if event.get("reason") != FAILED_SCHEDULING:
            continue
        return {
            "reason": event.get("reason"),
            "message": event.get("message"),
            "count": event.get("count"),
            "last_seen": event.get("last_seen"),
            "first_seen": event.get("first_seen"),
            "age_seconds": shaping.age_seconds(event.get("last_seen")),
        }
    return None


# --------------------------------------------------------------------------- #
# Volumes, and the inversion that traps people
# --------------------------------------------------------------------------- #

def claim_rows(
    pod: Any, namespace: str, unavailable: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    """Every PersistentVolumeClaim the pod mounts, and whether it is bound.

    **The inversion.** An unbound claim usually blocks scheduling — but a claim
    whose StorageClass uses ``WaitForFirstConsumer`` is unbound *because* the pod
    is unscheduled, not the other way round. The provisioner waits for the
    scheduler to pick a node so it can create the volume in the right zone. An
    operator told "this pod is blocked on an unbound claim" there goes to fix
    storage, and storage is waiting on them to fix scheduling.

    So ``blocks_scheduling`` is tri-state: ``true`` for an unbound Immediate
    claim, ``false`` for an unbound ``WaitForFirstConsumer`` one (it is a
    symptom, not the cause), and ``null`` when the binding mode could not be
    established — which is what an unreadable StorageClass leaves, and it is not
    an answer either way.

    ``None`` for the whole list when the claim listing failed. Not ``[]``: this
    pod may mount four claims and an empty list would say it mounts none, on the
    screen where somebody is deciding whether storage is the problem.
    """
    wanted = [
        str(shaping.get_field(volume, "persistentVolumeClaim", "claimName"))
        for volume in shaping.get_field(pod, "spec", "volumes", default=[]) or []
        if shaping.get_field(volume, "persistentVolumeClaim", "claimName")
    ]
    if not wanted:
        return []

    claims: dict[str, Any] | None = None
    with collect(unavailable, "", "persistentvolumeclaims", namespace=namespace):
        listing = get_core_v1().list_namespaced_persistent_volume_claim(namespace)
        claims = {
            str(shaping.get_field(claim, "metadata", "name")): claim
            for claim in shaping.get_field(listing, "items", default=[]) or []
        }
    if claims is None:
        return None

    modes = _binding_modes(unavailable)
    rows = []
    for claim_name in wanted:
        claim = claims.get(claim_name)
        phase = shaping.get_field(claim, "status", "phase") if claim is not None else None
        class_name = (
            shaping.get_field(claim, "spec", "storageClassName")
            if claim is not None else None
        )
        mode = modes.get(str(class_name)) if modes is not None and class_name else None
        bound = phase == "Bound"
        rows.append({
            "name": claim_name,
            "exists": claim is not None,
            "phase": phase,
            "storage_class": class_name,
            "binding_mode": mode,
            "blocks_scheduling": _blocks_scheduling(claim, bound, mode),
        })
    return rows


def _blocks_scheduling(claim: Any, bound: bool, mode: str | None) -> bool | None:
    """Does this claim stop the pod being placed? Tri-state, deliberately.

    ``mode`` is ``None`` for both ways the binding mode can be unknown — the
    StorageClass listing was refused, or it answered and did not name this
    claim's class. They are one branch rather than two because they lead to the
    same honest answer, and a second condition that no input can tell apart from
    the first is a line nothing enforces.
    """
    if claim is None:
        # A pod mounting a claim that does not exist is not scheduled, and that
        # is not in doubt — the kubelet cannot mount what is not there.
        return True
    if bound:
        return False
    if mode is None:
        # Unbound, and the binding mode could not be established. It may be the
        # cause or it may be the symptom, and guessing sends the operator to one
        # of two systems with even odds.
        return None
    return mode != "WaitForFirstConsumer"


def _binding_modes(unavailable: list[dict[str, Any]]) -> dict[str, str] | None:
    """``{storage class: volumeBindingMode}``, or ``None`` if unreadable.

    ``None`` rather than ``{}``, because an empty map and an unreadable one lead
    to opposite readings of every unbound claim below.
    """
    modes: dict[str, str] | None = None
    with collect(unavailable, "storage.k8s.io", "storageclasses"):
        listing = get_storage_v1().list_storage_class()
        modes = {
            str(shaping.get_field(item, "metadata", "name")): str(
                shaping.get_field(item, "volumeBindingMode") or "Immediate"
            )
            for item in shaping.get_field(listing, "items", default=[]) or []
        }
    return modes


# --------------------------------------------------------------------------- #
# The per-node re-derivation
# --------------------------------------------------------------------------- #

def _selector_reason(pod: Any, node: Any) -> dict[str, Any] | None:
    """A ``nodeSelector`` key this node does not carry, or ``None``.

    Only `nodeSelector`, which is exact-match on labels and cheap to be right
    about. `nodeAffinity` is deliberately not evaluated: its operators, weights
    and `preferredDuringScheduling` terms are a small language, and a partial
    implementation would rule nodes out for terms it misread — which is worse
    than not checking, because it is wrong with a reason attached.
    """
    selector = shaping.get_field(pod, "spec", "nodeSelector", default={}) or {}
    if not isinstance(selector, dict) or not selector:
        return None
    labels = shaping.get_field(node, "metadata", "labels", default={}) or {}
    missing = {
        key: value for key, value in selector.items()
        if str(labels.get(key, "")) != str(value)
    }
    if not missing:
        return None
    return {
        "code": NODE_SELECTOR_MISMATCH,
        "detail": (
            "The pod's nodeSelector requires "
            + ", ".join(f"{k}={v}" for k, v in sorted(missing.items()))
            + ", which this node does not carry."
        ),
    }


def _taint_reasons(pod: Any, node: Any) -> list[dict[str, Any]]:
    """Every `NoSchedule`/`NoExecute` taint on the node the pod does not tolerate.

    `PreferNoSchedule` is skipped on purpose: it lowers the node's score and
    does not exclude it, so listing it as a rule-out would report a node as
    unavailable that the scheduler will happily use when nothing better exists.
    """
    reasons = []
    for taint in shaping.get_field(node, "spec", "taints", default=[]) or []:
        if shaping.get_field(taint, "effect") not in BLOCKING_EFFECTS:
            continue
        tolerated = any(
            shaping.toleration_tolerates_taint(toleration, taint)
            for toleration in shaping.get_field(pod, "spec", "tolerations", default=[]) or []
        )
        if tolerated:
            continue
        key = shaping.get_field(taint, "key")
        value = shaping.get_field(taint, "value")
        reasons.append({
            "code": NODE_TAINTED,
            "detail": (
                f"Taint {key}{'=' + str(value) if value else ''}:"
                f"{shaping.get_field(taint, 'effect')} is not tolerated by this pod."
            ),
        })
    return reasons


def _capacity_reasons(
    node: Any, wanted: dict[str, Any], used: dict[str, Any] | None, pod_count: int | None
) -> tuple[list[dict[str, Any]], bool]:
    """``(reasons, checked)`` — does the pod's request fit what is left here?

    ``checked`` is false whenever a term of the arithmetic is unknown: the pod
    listing failed, so nothing is known about what this node already holds, or a
    quantity on either side did not parse. **No reason is emitted in that case
    and none is withheld either** — the node simply is not judged on capacity,
    and the caller reports that rather than reporting a fit.

    Allocatable, not capacity. Capacity is the machine; allocatable is what the
    kubelet offers the scheduler after its own reservations, and the scheduler
    only ever compares against the second. Using capacity would report room on
    a node that has none.
    """
    if used is None:
        return [], False

    allocatable = shaping.get_field(node, "status", "allocatable", default={}) or {}
    reasons: list[dict[str, Any]] = []
    # Starts true and is falsified by any dimension that could not be compared.
    # The other way round — true as soon as *something* was compared — would
    # report a node as fully examined when the only quantity that mattered, the
    # one the pod actually asks for, was the unreadable one.
    checked = True

    free_cpu = _remaining(cpu_cores(allocatable.get("cpu")), used.get("cpu_cores"))
    if wanted.get("cpu_cores") is not None and free_cpu is None:
        checked = False
    if free_cpu is not None and wanted.get("cpu_cores") is not None:
        if wanted["cpu_cores"] > free_cpu:
            reasons.append({
                "code": NODE_INSUFFICIENT_CPU,
                "detail": (
                    f"Needs {wanted['cpu_cores']:g} cores; "
                    f"{free_cpu:g} of {cpu_cores(allocatable.get('cpu')):g} allocatable "
                    "are unrequested here."
                ),
            })

    free_mem = _remaining(memory_bytes(allocatable.get("memory")), used.get("memory_bytes"))
    if wanted.get("memory_bytes") is not None and free_mem is None:
        checked = False
    if free_mem is not None and wanted.get("memory_bytes") is not None:
        if wanted["memory_bytes"] > free_mem:
            reasons.append({
                "code": NODE_INSUFFICIENT_MEMORY,
                "detail": (
                    f"Needs {wanted['memory_bytes']} bytes; {int(free_mem)} are "
                    "unrequested here."
                ),
            })

    slots = pod_capacity(allocatable.get("pods"))
    if slots is None or pod_count is None:
        checked = False
    if slots is not None and pod_count is not None:
        if pod_count >= slots:
            reasons.append({
                "code": NODE_POD_SLOTS_FULL,
                "detail": (
                    f"{pod_count} of {slots} pod slots are taken, so this node "
                    "accepts no more pods whatever their size."
                ),
            })

    return reasons, checked


def _remaining(allocatable: Any, requested: Any) -> float | None:
    """``allocatable - requested``, or ``None`` when either side is unknown.

    ``None`` and not a fallback to allocatable. Treating an unknown requested
    total as zero would report a full node as empty — the direction that sends
    somebody to schedule onto it.
    """
    if allocatable is None or requested is None:
        return None
    return float(allocatable) - float(requested)


def node_verdicts(
    pod: Any, nodes: list[Any], grouped: dict[str, list[Any]] | None
) -> list[dict[str, Any]]:
    """Every node, ruled out with reasons or not ruled out at all.

    **There is no `fits`, and that is the whole design.** The scheduler's
    predicate chain includes inter-pod affinity and anti-affinity, topology
    spread constraints, volume node affinity and zone, extended and scalar
    resources, host port conflicts, pod overhead against a RuntimeClass, and
    every scheduling plugin the cluster has installed. None of that is evaluated
    here. `no_reason_found` therefore means exactly what it says — this console
    checked what it can check and found nothing — and promoting it to "this node
    has room" would send an operator to argue with the scheduler about a node it
    rejected for a reason they cannot see.
    """
    wanted = pod_requests(pod).as_row()
    rows = []
    for node in nodes:
        name = str(shaping.get_field(node, "metadata", "name") or "")
        reasons: list[dict[str, Any]] = []

        if shaping.get_field(node, "spec", "unschedulable"):
            reasons.append({
                "code": NODE_CORDONED,
                "detail": "The node is cordoned, so the scheduler places nothing new on it.",
            })
        # Tri-state, and only `False` rules the node out. A node whose Ready
        # condition is absent has not been observed rather than been found
        # unhealthy, and excluding it would invent a rejection.
        if node_ready(node) is False:
            reasons.append({
                "code": NODE_NOT_READY,
                "detail": "The node's Ready condition is False.",
            })

        reasons.extend(_taint_reasons(pod, node))
        selector = _selector_reason(pod, node)
        if selector:
            reasons.append(selector)

        on_node = grouped.get(name, []) if grouped is not None else None
        used = requested_total(on_node) if on_node is not None else None
        pod_count = len(on_node) if on_node is not None else None
        capacity, checked = _capacity_reasons(node, wanted, used, pod_count)
        reasons.extend(capacity)

        rows.append({
            "name": name,
            "verdict": RULED_OUT if reasons else NO_REASON_FOUND,
            "reasons": reasons,
            # False means the node was not judged on room at all, so its
            # `no_reason_found` is weaker still. The frontend says so rather than
            # letting the two look alike.
            "capacity_checked": checked,
        })

    rows.sort(key=lambda row: (row["verdict"] == NO_REASON_FOUND, row["name"]))
    return rows


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def why_pending(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/pods/{namespace}/{name}/scheduling`` (§31).

    The pod is the primary read and raises: there is no honest answer to "why is
    this pod pending" about a pod that could not be read. Everything after it is
    collected, so a refused node listing costs the table and names itself rather
    than emptying the page.

    Reads only. Nothing here writes, so there is no gate, no preflight and no
    audit row — this is `services/`, not `admin/`.
    """
    unavailable: list[dict[str, Any]] = []
    # `read_object` rather than `get_resource`: group, version and scope are
    # known here, so a discovery round trip to re-learn "core/v1 pods is
    # namespaced" would be paid on every request for nothing — and it maps a
    # denial to `get pods` rather than a bare "forbidden".
    pod = reader.read_object("", "v1", "pods", name, namespace=namespace)

    state = waiting_on(pod)
    verdict = (
        scheduler_verdict(namespace, name, unavailable)
        if state != WAITING_ON_NOTHING else None
    )

    nodes: list[Any] | None = None
    with collect(unavailable, "", "nodes"):
        nodes = list(
            shaping.get_field(get_core_v1().list_node(), "items", default=[]) or []
        )

    # Only when there are nodes to attribute them to. A pod listing fetched for
    # a node table that will not render is a round trip spent on nothing.
    grouped = active_pods_by_node(unavailable) if nodes is not None else None

    return {
        "namespace": namespace,
        "pod": name,
        "phase": shaping.get_field(pod, "status", "phase"),
        "waiting_on": state,
        "node": shaping.get_field(pod, "spec", "nodeName"),
        "pending_seconds": (
            shaping.age_seconds(shaping.get_field(pod, "metadata", "creationTimestamp"))
            if state != WAITING_ON_NOTHING else None
        ),
        "scheduled_condition": scheduled_condition(pod),
        # `null` is "no FailedScheduling event is readable right now" and never
        # "the scheduler has not rejected this pod" — see `scheduler_verdict`.
        "scheduler": verdict,
        "requests": pod_requests(pod).as_row(),
        "claims": claim_rows(pod, namespace, unavailable),
        # `null`, never `[]`: an empty node list would say the cluster has no
        # nodes, on the screen where somebody is working out why nothing will
        # take their pod.
        "nodes": node_verdicts(pod, nodes, grouped) if nodes is not None else None,
        "unavailable": unavailable,
        "partial": bool(unavailable),
    }


__all__ = [
    "FAILED_SCHEDULING",
    "NO_REASON_FOUND",
    "RULED_OUT",
    "WAITING_ON_KUBELET",
    "WAITING_ON_NOTHING",
    "WAITING_ON_SCHEDULER",
    "claim_rows",
    "node_verdicts",
    "scheduled_condition",
    "scheduler_verdict",
    "waiting_on",
    "why_pending",
]
