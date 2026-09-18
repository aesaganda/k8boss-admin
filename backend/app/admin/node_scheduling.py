"""
§24 — a node's taints and its labels: the two fields that decide what may run
on a machine, and the pair that §4's YAML editor can already write.

**Why this is not "edit the node YAML".** Both writes are small. What neither
the editor nor `kubectl taint` will tell you is what the write does to the pods
that are on the node right now, and for one of the four combinations the answer
is that they are destroyed.

**The taint that deletes.** `NoSchedule` and `PreferNoSchedule` are consulted
when the scheduler is *placing* a pod: adding one moves nothing that is already
running. `NoExecute` is evaluated against pods that are already there, and the
ones that do not tolerate it are removed by the taint manager in
kube-controller-manager. In a form the three are three entries in one dropdown.

That removal is a **delete, not an eviction**, and the distinction is this
module's reason to exist. §5's drain goes through the `pods/eviction`
subresource, which is where PodDisruptionBudgets are enforced — the console
says so in the drain dialog, in `docs/safety-model.md`, and in the sentence
telling operators that `force` will not get them past a budget. The taint
manager does not use that subresource. **A `NoExecute` taint takes an
application below its PodDisruptionBudget without being refused**, from a
dialog two clicks away from the one that has been teaching the operator the
opposite. A console that offered the taint write without saying that would have
handed them a bigger hammer while its own documentation described a smaller one.

**The toleration that expires.** A pod can tolerate a `NoExecute` taint for a
bounded time: `tolerationSeconds` on the matching toleration. Those pods are not
in the "goes now" set and not in the "stays" set — the node looks entirely
healthy for five minutes and then empties. Reported as its own delay rather than
folded into either.

**The label change that evicts nothing, and that is the trap.** Node affinity is
`requiredDuringSchedulingIgnoredDuringExecution` — the second half of that name
is load-bearing. Removing a label a running pod's `nodeSelector` requires does
not disturb the pod at all. It changes where that pod can be placed *next*, which
is discovered during the next rollout, by someone who is not the person who made
this change. So the plan names the pods on this node whose placement rules
mention a key being removed, and says in those words that nothing moves today.

**Nothing here is derived when it could be read.** Which pods do not tolerate a
taint is computed from the node's own pod listing, and if that listing fails the
answer is `null` with `pods_checked: false` — never an empty list, which would
render as "this taint deletes nothing" over a node whose pods were never counted.
That case is a consequence the operator has to acknowledge by name, because it is
the one where the console genuinely does not know what the button does.

The gate is `ADMIN_ALLOW_MUTATIONS` alone. Drain — the more destructive of the
two, and the one that empties a machine on purpose — carries no switch of its
own either, and a second gate on the lesser action would say the reverse of what
is true.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.apply import MERGE_PATCH, patch_fn
from app.admin.mutate import FeatureGate, audit_conflict, mutate, read_only_switch
from app.admin.nodes import controller_ref, list_node_pods, read_node
from app.errors import Conflict, Invalid
from app.resources.envelope import collect
from app.resources.shaping import (
    EFFECT_NO_EXECUTE,
    TAINT_EFFECTS,
    get_field,
    node_label_dependencies,
    taint_tolerated_by,
)

logger = logging.getLogger(__name__)

GROUP, VERSION, PLURAL = "", "v1", "nodes"

#: Consequence codes. Constants because the frontend branches on them to render
#: a checkbox, and a typo in a string literal is a confirmation nobody can give
#: for a write that goes ahead anyway.
WARN_TAINT_DELETES_PODS = "taint_deletes_pods"
WARN_TAINT_DELETES_UNMANAGED = "taint_deletes_unmanaged"
WARN_TAINT_DELETES_DAEMONSET = "taint_deletes_daemonset"
WARN_TAINT_DELAYED = "taint_delayed_deletion"
WARN_TAINT_PODS_UNKNOWN = "taint_pods_unknown"
WARN_TAINT_REMOVED = "taint_removed"
WARN_TAINT_CONTROL_PLANE_OPENED = "taint_control_plane_opened"

WARN_LABEL_REMOVED = "label_removed"
WARN_LABEL_RESERVED = "label_reserved_prefix"
WARN_LABEL_ROLE_CHANGED = "label_role_changed"
WARN_LABEL_PODS_DEPEND = "label_pods_depend"
WARN_LABEL_PODS_UNKNOWN = "label_pods_unknown"

#: The taints that keep ordinary work off a control-plane node. Removing one is
#: not the same act as removing a taint somebody added this morning, so it is
#: named rather than folded into the general "you have opened this node" line.
#: Both spellings, because a cluster upgraded across 1.24 carries the old one.
CONTROL_PLANE_TAINTS = (
    "node-role.kubernetes.io/control-plane",
    "node-role.kubernetes.io/master",
)

#: Label prefixes the cluster's own components own. The kubelet re-applies a
#: subset of these when it registers, which is *not* the same as "removing one
#: is harmless" — see :func:`label_consequences`.
RESERVED_LABEL_PREFIXES = (
    "kubernetes.io/",
    "k8s.io/",
    "beta.kubernetes.io/",
    "node.kubernetes.io/",
    "topology.kubernetes.io/",
    "node-role.kubernetes.io/",
)

#: The prefix `kubectl get nodes` reads to fill its ROLES column, and the one
#: :func:`app.services.nodes.node_roles` reads to fill this console's.
ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"

DAEMONSET_KIND = "DaemonSet"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate(what: str) -> FeatureGate:
    """One switch, and the dry run is not withheld.

    On a read-only console the plan is still the most useful screen here: which
    pods a taint would delete is a read, and it is the fact somebody wants before
    they go and ask for the permission to make the change.
    """
    return FeatureGate(
        feature=f"editing node {what}",
        message=f"Editing node {what} is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                f"writes nothing to a cluster. The plan is still available — the node's "
                f"current {what} and what changing them would do to the pods running "
                "here are reads."
            )),
        ),
        enabled_detail=f"This deployment permits editing node {what}.",
    )


def enabled_state(what: str = "taints") -> dict[str, Any]:
    """The ``gate`` object §24's plans return, so the UI disables with the reason."""
    return _gate(what).state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def validate_taints(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The requested taint list, normalised, or ``422 invalid``.

    **The whole list is sent on every write, including the taints that are not
    changing.** `spec.taints` is an atomic list in the API — a patch replaces it
    outright — so there is no add-one operation to expose, and inventing one
    here would mean this module reconstructing the list from a delta and the
    operator confirming a diff built from an assumption rather than from what
    they sent.

    The checks below are the ones that produce a better sentence than the API
    server's. Key syntax is left to the API server, which owns the qualified-name
    rules and names the offending key when it refuses.
    """
    if "taints" not in payload:
        raise _invalid(
            "taints is required.",
            parameter="taints",
            hint=(
                "Send the complete list the node should end up with. An empty "
                "list removes every taint."
            ),
        )
    raw = payload["taints"]
    if not isinstance(raw, list):
        raise _invalid("taints must be a list.", parameter="taints")

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _invalid(
                f"taints[{index}] must be an object with key, value and effect.",
                parameter="taints", index=index,
            )
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise _invalid(
                f"taints[{index}].key is required.",
                parameter="taints", index=index,
            )
        effect = item.get("effect")
        if effect not in TAINT_EFFECTS:
            raise _invalid(
                f"taints[{index}].effect must be one of {', '.join(TAINT_EFFECTS)}.",
                parameter="taints", index=index, effect=effect,
                hint=(
                    "NoSchedule and PreferNoSchedule affect where new pods are "
                    "placed. NoExecute also removes pods that are already running "
                    "here and do not tolerate it."
                ),
            )
        value = item.get("value")
        if value is not None and not isinstance(value, str):
            raise _invalid(
                f"taints[{index}].value must be a string when it is present.",
                parameter="taints", index=index,
            )
        # The API server's uniqueness rule is the (key, effect) pair, not the
        # key: `dedicated=a:NoSchedule` and `dedicated=b:NoExecute` are both
        # legal on one node. Caught here because the API server's message for it
        # arrives as a field-path error over a list index nobody typed.
        identity = (key, str(effect))
        if identity in seen:
            raise _invalid(
                f"taints[{index}] repeats {key}:{effect}.",
                parameter="taints", index=index,
                hint="A node may carry one taint per key and effect pair.",
            )
        seen.add(identity)
        out.append({"key": key, "value": value, "effect": effect})
    return out


def validate_labels(payload: dict[str, Any]) -> dict[str, str]:
    """The requested label map, or ``422 invalid``.

    Like the taint list, this is **the complete map the node should end up
    with**: a key the caller leaves out is a key this module deletes. The
    alternative — a patch of only what changed — cannot express a removal
    without a second field, and a form whose "delete" is a separate list from
    its "set" is a form where the two disagree.
    """
    if "labels" not in payload:
        raise _invalid(
            "labels is required.",
            parameter="labels",
            hint=(
                "Send the complete map the node should end up with. A key you "
                "leave out is removed."
            ),
        )
    raw = payload["labels"]
    if not isinstance(raw, dict):
        raise _invalid("labels must be an object.", parameter="labels")

    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            raise _invalid("Every label key must be a non-empty string.", parameter="labels")
        if not isinstance(value, str):
            # A YAML `version: 3` arrives here as an int and the API server
            # refuses it with a schema error naming a type, not a key. Kubernetes
            # label values are strings; quoting is the fix and this says so.
            raise _invalid(
                f"The value of {key!r} must be a string.",
                parameter="labels", key=key,
                hint="Label values are strings — quote numbers and booleans.",
            )
        out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Reading the node
# --------------------------------------------------------------------------- #

def _live(name: str) -> tuple[dict[str, Any], str | None]:
    """``(live node, its resourceVersion)``. A failed read propagates.

    Never a default: a taint list described against a node we could not read
    would put the operator's own input on both sides of the diff.
    """
    live = read_node(name)
    version = get_field(live, "metadata", "resourceVersion")
    return live, (str(version) if version else None)


def current_taints(node: Any) -> list[dict[str, Any]]:
    """The node's taints as ``{key, value, effect}``, in the order it holds them."""
    return [
        {
            "key": get_field(taint, "key"),
            "value": get_field(taint, "value"),
            "effect": get_field(taint, "effect"),
        }
        for taint in get_field(node, "spec", "taints", default=[]) or []
    ]


def current_labels(node: Any) -> dict[str, str]:
    """The node's labels. ``{}`` here is a real empty map — the node was read."""
    labels = get_field(node, "metadata", "labels", default={}) or {}
    return {str(k): str(v) for k, v in labels.items()} if isinstance(labels, dict) else {}


def _pods_on(name: str) -> tuple[list[Any] | None, list[dict[str, Any]]]:
    """``(pods, unavailable)`` — ``None`` for pods when the listing failed.

    §0.1's corollary, on the read that matters most in this module: an empty list
    of pods produces a plan that says a `NoExecute` taint deletes nothing, and
    that sentence is identical whether the node is genuinely idle or the listing
    was refused. The two are kept apart all the way into `pods_checked`.
    """
    unavailable: list[dict[str, Any]] = []
    pods: list[Any] | None = None
    with collect(unavailable, "", "pods"):
        pods = list_node_pods(name)
    return pods, unavailable


def _pod_identity(pod: Any) -> dict[str, Any]:
    """The namespace, name and owning controller of one pod, for a plan row."""
    ref = controller_ref(pod)
    return {
        "namespace": get_field(pod, "metadata", "namespace"),
        "pod": get_field(pod, "metadata", "name"),
        "controller": (
            {"kind": get_field(ref, "kind"), "name": get_field(ref, "name")}
            if ref is not None else None
        ),
    }


# --------------------------------------------------------------------------- #
# Taints — the diff and what it does to the pods
# --------------------------------------------------------------------------- #

def _identity(taint: dict[str, Any]) -> tuple[Any, Any]:
    return (taint.get("key"), taint.get("effect"))


def diff_taints(
    current: list[dict[str, Any]], requested: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """``{added, removed, changed}`` over the ``(key, effect)`` pairs.

    ``changed`` is a category of its own rather than a removal plus an addition
    because it behaves like one: a taint whose *value* moves is still one taint
    to the API server, and a pod tolerating the old value with ``operator:
    Equal`` stops tolerating it. That makes a value edit as capable of deleting
    pods as a brand new taint, which is not what "changed" sounds like.
    """
    have = {_identity(t): t for t in current}
    want = {_identity(t): t for t in requested}

    added = [t for identity, t in want.items() if identity not in have]
    removed = [t for identity, t in have.items() if identity not in want]
    changed = [
        {"before": have[identity], "after": t}
        for identity, t in want.items()
        if identity in have and (have[identity].get("value") or "") != (t.get("value") or "")
    ]
    return {"added": added, "removed": removed, "changed": changed}


def newly_enforced(diff: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The taints that will be applied to pods already running: new and re-valued.

    A value edit is included for the reason :func:`diff_taints` gives — the pods
    tolerating the old value are not tolerating the new one — and leaving it out
    would produce an empty deletion list for a change that empties a node.
    """
    return [
        taint for taint in ([*diff["added"]] + [c["after"] for c in diff["changed"]])
        if taint.get("effect") == EFFECT_NO_EXECUTE
    ]


def deletion_plan(pods: list[Any], taints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per pod that the given ``NoExecute`` taints remove. Pure.

    ``delay_seconds`` is a **real zero** when the pod does not tolerate the
    taint: the taint manager deletes it as soon as the taint is written, and
    nothing was left unread to produce that number. A pod tolerating with a
    ``tolerationSeconds`` gets that value instead, and the row is still in this
    list — it is going, later.

    A pod hit by more than one of the taints is reported once, against whichever
    takes it first. Listing it twice would double the count on the confirmation
    screen, and the count is what the operator reads.
    """
    rows: list[dict[str, Any]] = []
    for pod in pods:
        soonest: tuple[int, dict[str, Any]] | None = None
        for taint in taints:
            tolerated, seconds = taint_tolerated_by(pod, taint)
            if tolerated and seconds is None:
                continue
            delay = 0 if not tolerated else seconds or 0
            if soonest is None or delay < soonest[0]:
                soonest = (delay, taint)
        if soonest is None:
            continue
        delay, taint = soonest
        rows.append({**_pod_identity(pod), "taint": taint, "delay_seconds": delay})
    return rows


def taint_consequences(
    diff: dict[str, list[dict[str, Any]]],
    deleting: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """What this taint change means, each as a code the caller names back.

    ``deleting`` is ``None`` when the pod listing failed, and that produces a
    consequence of its own rather than silence — the one case where the console
    cannot say what the button does.
    """
    out: list[dict[str, Any]] = []
    enforced = newly_enforced(diff)

    if enforced and deleting is None:
        out.append({
            "code": WARN_TAINT_PODS_UNKNOWN,
            "label": "Which pods this deletes could not be worked out",
            "consequence": (
                "This change adds a NoExecute taint, which removes the pods on "
                "this node that do not tolerate it — and the pod listing for this "
                "node failed, so this console cannot tell you which pods those "
                "are or how many. It is not reporting that there are none."
            ),
            "mitigation": (
                "Retry once the listing works, or read the node's pods another "
                "way before confirming. The banner above names the read that "
                "failed."
            ),
        })

    if deleting:
        immediate = [row for row in deleting if row["delay_seconds"] == 0]
        delayed = [row for row in deleting if row["delay_seconds"] > 0]
        total = len(deleting)

        out.append({
            "code": WARN_TAINT_DELETES_PODS,
            "label": (
                f"{total} pod{'' if total == 1 else 's'} on this node "
                f"{'is' if total == 1 else 'are'} deleted by this taint"
                + ("" if immediate else " once their tolerations expire")
            ),
            "consequence": (
                "A NoExecute taint is applied to pods that are already running, "
                "and the taint manager removes the ones that do not tolerate it. "
                "That removal is a delete, not an eviction: it does not go "
                "through the pods/eviction subresource, so PodDisruptionBudgets "
                "do not apply to it. Drain is refused when it would take an "
                "application below its budget — this is not, and there is no "
                "setting here that changes that."
            ),
            "mitigation": (
                "Drain the node instead if you want budgets honoured, or use "
                "NoSchedule, which stops new work arriving here and leaves what "
                "is already running alone."
            ),
        })

        unmanaged = [row for row in deleting if row["controller"] is None]
        if unmanaged:
            count = len(unmanaged)
            out.append({
                "code": WARN_TAINT_DELETES_UNMANAGED,
                "label": (
                    f"{count} of them {'has' if count == 1 else 'have'} no controller "
                    "and will not come back anywhere"
                ),
                "consequence": (
                    "Nothing owns "
                    + ", ".join(f"{row['namespace']}/{row['pod']}" for row in unmanaged[:5])
                    + ("…" if count > 5 else "")
                    + ". A pod with no controller is deleted and that is the end "
                    "of it — no ReplicaSet, Job or DaemonSet recreates it here or "
                    "anywhere else."
                ),
                "mitigation": (
                    "Check whether any of them is holding something you need "
                    "before confirming."
                ),
            })

        daemonsets = [
            row for row in deleting
            if (row["controller"] or {}).get("kind") == DAEMONSET_KIND
        ]
        if daemonsets:
            count = len(daemonsets)
            out.append({
                "code": WARN_TAINT_DELETES_DAEMONSET,
                "label": (
                    f"{count} DaemonSet pod{'' if count == 1 else 's'} "
                    f"{'is' if count == 1 else 'are'} removed and not replaced here"
                ),
                "consequence": (
                    "The DaemonSet controller adds tolerations for the node's own "
                    "condition taints — not-ready, unreachable, disk pressure and "
                    "the rest — and for nothing else. A taint you write is not one "
                    "of those, so its pods are deleted like any others and the "
                    "controller will not place them back while the taint stands. "
                    "On this node that usually means losing log shipping, the CNI "
                    "agent or node metrics."
                ),
                "mitigation": (
                    "Add a matching toleration to the DaemonSets that must keep "
                    "running here before you write the taint."
                ),
            })

        if delayed:
            count = len(delayed)
            soonest = min(row["delay_seconds"] for row in delayed)
            out.append({
                "code": WARN_TAINT_DELAYED,
                "label": (
                    f"{count} of them {'goes' if count == 1 else 'go'} later, "
                    f"the first in {soonest}s"
                ),
                "consequence": (
                    "These pods carry a toleration with a tolerationSeconds, so "
                    "they are not removed when the taint lands — they are removed "
                    "when that timer expires. The node looks unaffected for as "
                    "long as the timer runs and then empties on its own, which is "
                    "after whoever wrote the taint has stopped watching."
                ),
                "mitigation": (
                    "Expect the node to keep changing after this write completes; "
                    "the pod list here is not final until the longest timer has "
                    "run out."
                ),
            })

    if diff["removed"]:
        names = ", ".join(
            f"{t['key']}{'=' + t['value'] if t.get('value') else ''}:{t['effect']}"
            for t in diff["removed"]
        )
        out.append({
            "code": WARN_TAINT_REMOVED,
            "label": f"This node stops excluding work: {names} removed",
            "consequence": (
                "The scheduler stops treating this node as reserved. Pods that "
                "have been waiting because nothing tolerated these taints can be "
                "placed here as soon as the change lands, including workloads "
                "that were never meant to run on this machine."
            ),
            "mitigation": (
                "Cordon the node first if you want the taint gone without new "
                "work arriving while you look at it."
            ),
        })

    opened = [t for t in diff["removed"] if t.get("key") in CONTROL_PLANE_TAINTS]
    if opened:
        out.append({
            "code": WARN_TAINT_CONTROL_PLANE_OPENED,
            "label": "This removes the taint that keeps ordinary workloads off a control-plane node",
            "consequence": (
                "Without "
                + ", ".join(str(t["key"]) for t in opened)
                + " the scheduler treats this machine as ordinary capacity. "
                "Application pods will be placed alongside the API server, etcd "
                "and the controller manager, and will compete with them for CPU "
                "and memory during exactly the incidents when the control plane "
                "needs it most."
            ),
            "mitigation": (
                "On a single-node or lab cluster this is deliberate. On a cluster "
                "with worker nodes it is almost never what was intended."
            ),
        })

    return out


# --------------------------------------------------------------------------- #
# Labels — the diff and what depends on it
# --------------------------------------------------------------------------- #

def diff_labels(
    current: dict[str, str], requested: dict[str, str]
) -> dict[str, Any]:
    """``{added, changed, removed}`` over label keys.

    ``changed`` carries both sides. The question after a bad rollout is what the
    value used to be, and a diff that recorded only the new one cannot answer it.
    """
    added = {k: v for k, v in requested.items() if k not in current}
    removed = {k: v for k, v in current.items() if k not in requested}
    changed = {
        k: {"before": current[k], "after": v}
        for k, v in requested.items()
        if k in current and current[k] != v
    }
    return {"added": added, "changed": changed, "removed": removed}


def label_dependents(pods: list[Any], keys: list[str]) -> list[dict[str, Any]]:
    """Pods on this node whose placement rules name one of ``keys``. Pure.

    Keys, not verdicts — see :func:`app.resources.shaping.node_label_dependencies`.
    A pod appears here because a rule that put it on this node mentions a label
    that is about to move, which is a reason to look, not a prediction that
    anything happens today.
    """
    wanted = set(keys)
    rows: list[dict[str, Any]] = []
    for pod in pods:
        matched = [key for key in node_label_dependencies(pod) if key in wanted]
        if matched:
            rows.append({**_pod_identity(pod), "keys": matched})
    return rows


def label_consequences(
    diff: dict[str, Any],
    dependents: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """What this label change means, each as a code the caller names back.

    Two sets of keys, and they are not the same set. ``touched`` is what is
    leaving or moving, which is what a running pod's placement rule could have
    depended on. ``named`` includes the keys being *added*, because a role label
    appearing changes the ROLES column exactly as much as one disappearing, and a
    reserved key being introduced is the same kind of collision with a component
    that owns it.
    """
    out: list[dict[str, Any]] = []
    touched = sorted({*diff["removed"], *diff["changed"]})
    named = sorted({*diff["added"], *diff["removed"], *diff["changed"]})

    if diff["removed"]:
        names = ", ".join(sorted(diff["removed"]))
        out.append({
            "code": WARN_LABEL_REMOVED,
            "label": f"{len(diff['removed'])} label(s) removed: {names}",
            "consequence": (
                "Nothing running on this node is disturbed by this. Node affinity "
                "is requiredDuringSchedulingIgnoredDuringExecution — the rule is "
                "checked when a pod is placed and never again — so pods that were "
                "scheduled here because of these labels keep running exactly as "
                "they are. What changes is the next placement: after the next "
                "restart, rollout or eviction those pods will not come back here."
            ),
            "mitigation": (
                "The effect shows up at the next rollout rather than now. If you "
                "are removing a label to move a workload off this node, that "
                "move has not happened — drain the node for that."
            ),
        })

    reserved = [k for k in named if k.startswith(RESERVED_LABEL_PREFIXES)]
    if reserved:
        out.append({
            "code": WARN_LABEL_RESERVED,
            "label": "This changes labels the cluster's own components own",
            "consequence": (
                ", ".join(reserved)
                + " — the kubelet re-applies some of these when it next registers "
                "and never re-applies others, and which is which depends on its "
                "flags and on a cloud provider that no API here reports. So this "
                "console cannot tell you whether a given one comes back. "
                "topology.kubernetes.io/zone is the one to be careful with: "
                "volume topology is matched against it, and a node that has lost "
                "it will not be chosen for a zonal volume."
            ),
            "mitigation": (
                "Prefer a label of your own for scheduling decisions and leave "
                "these to the components that set them."
            ),
        })

    roles = [k for k in named if k.startswith(ROLE_LABEL_PREFIX)]
    if roles:
        out.append({
            "code": WARN_LABEL_ROLE_CHANGED,
            "label": "This changes what this node reports as its role",
            "consequence": (
                ", ".join(roles)
                + " decides the ROLES column in `kubectl get nodes` and on this "
                "console's own node list. Changing it changes what every operator "
                "reading either of those believes this machine is for. It grants "
                "and removes nothing on its own — a node-role label is a label."
            ),
            "mitigation": (
                "If you are trying to stop work running here, a taint does that; "
                "this only renames the node."
            ),
        })

    if touched and dependents is None:
        out.append({
            "code": WARN_LABEL_PODS_UNKNOWN,
            "label": "Which pods depend on these labels could not be worked out",
            "consequence": (
                "The pod listing for this node failed, so this console cannot say "
                "whether anything running here was placed by a rule naming the "
                "labels you are changing. It is not reporting that nothing does."
            ),
            "mitigation": "Retry once the listing works, or check before confirming.",
        })
    elif dependents:
        count = len(dependents)
        out.append({
            "code": WARN_LABEL_PODS_DEPEND,
            "label": (
                f"{count} pod{'' if count == 1 else 's'} here "
                f"{'was' if count == 1 else 'were'} placed by a rule naming these labels"
            ),
            "consequence": (
                ", ".join(f"{row['namespace']}/{row['pod']}" for row in dependents[:5])
                + ("…" if count > 5 else "")
                + " declare a nodeSelector or a required node affinity that "
                "mentions a key you are changing. They keep running — the rule is "
                "not re-evaluated — but the scheduler will not place them here "
                "again once they restart."
            ),
            "mitigation": (
                "Check that each of them can be scheduled somewhere else, or that "
                "you meant to change where they run."
            ),
        })

    return out


# --------------------------------------------------------------------------- #
# Shared refusals
# --------------------------------------------------------------------------- #

def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed from the node and its pods as they are *now*, never trusted from
    the plan: pods arrive and leave on their own between the two calls, so the
    plan's "this deletes two pods" can be a different number by the time the
    write lands — and what the operator has to have accepted is the one that is
    true at write time.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This change has consequences that have not been acknowledged.",
        detail="; ".join(f"{entry['code']}: {entry['label']}" for entry in missing),
        hint=(
            "Re-send with acknowledgeConsequences naming each of "
            + ", ".join(entry["code"] for entry in missing)
            + "."
        ),
        context={
            "parameter": "acknowledgeConsequences",
            "unacknowledged": [entry["code"] for entry in missing],
        },
    )


def _require_version(
    name: str, sent: str | None, live: str | None, *, what: str, dry_run: bool,
    **extra: Any,
) -> None:
    """§0.4, locally, so the operator gets a fresh diff rather than a bare 409.

    Enforced again by the API server, because the patch carries the caller's
    ``resourceVersion``: this check loses the race between the read above and
    the write below, and the API server's does not.

    Audited before it raises, because it fires before the first :func:`mutate`
    and the funnel — which records every other terminal state — never runs. Rule
    5 covers the writes that conflicted as much as the ones that landed, and
    "two people were editing this node at once during the incident" is exactly
    what rule 4 exists to make answerable afterwards.
    """
    if not sent or not live or sent == live:
        return
    conflict = Conflict(
        f"The node {name} changed while you were reading it.",
        detail=f"You are editing version {sent}; the cluster has {live}.",
        hint="Reload the node and preview again against what it says now.",
        context={
            "group": GROUP, "version": VERSION, "resource": PLURAL,
            "name": name, "verb": "patch",
            "currentResourceVersion": live,
            **extra,
        },
    )
    audit_conflict(
        verb="patch", group=GROUP, version=VERSION, plural=PLURAL,
        namespace=None, name=name, dry_run=dry_run, error=conflict,
        detail=(
            f"node {what} {name}: refused, editing {sent} and the cluster has {live}"
        ),
    )
    raise conflict


def _blocked(refusal: Invalid) -> dict[str, Any]:
    return {
        "message": refusal.message,
        "hint": refusal.hint,
        "context": refusal.context,
    }


def _no_change(diff: dict[str, Any], what: str) -> Invalid | None:
    """The refusal for a request that changes nothing, or ``None``."""
    if any(diff.get(key) for key in ("added", "removed", "changed")):
        return None
    return _invalid(
        f"This node's {what} already match what you sent.",
        parameter=what,
        hint=f"Change a {what[:-1]}, or nothing needs to happen.",
    )


# --------------------------------------------------------------------------- #
# Taints — plan and write
# --------------------------------------------------------------------------- #

def build_taint_patch(
    taints: list[dict[str, Any]], *, resource_version: str | None
) -> dict[str, Any]:
    """The merge patch for ``spec.taints``, plus rule 4's version.

    An empty list is sent as ``null`` rather than ``[]``. Both clear the field,
    and the API server normalises either — but ``null`` renders in the diff as
    the key disappearing, which is what removing every taint is, while ``[]``
    renders as the key surviving with nothing in it.
    """
    patch: dict[str, Any] = {"spec": {"taints": taints or None}}
    if resource_version:
        patch["metadata"] = {"resourceVersion": resource_version}
    return patch


def _taint_detail(name: str, diff: dict[str, list[dict[str, Any]]]) -> str:
    """The audit sentence: what moved, not just that something did."""
    def spell(taint: dict[str, Any]) -> str:
        value = taint.get("value")
        return f"{taint.get('key')}{'=' + value if value else ''}:{taint.get('effect')}"

    parts = (
        [f"+{spell(t)}" for t in diff["added"]]
        + [f"~{spell(c['after'])} (was {c['before'].get('value') or ''})" for c in diff["changed"]]
        + [f"-{spell(t)}" for t in diff["removed"]]
    )
    return f"node taints {name}: " + (", ".join(parts) if parts else "no change")


def plan_taints(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/nodes/{name}/taints/plan`` (§24).

    Ungated and unaudited: two reads and set arithmetic. It does not dry-run the
    patch — a dry run is a write request the caller has not asked for yet, and it
    needs the preflight the funnel does.

    A request that changes nothing comes back as ``blocked`` rather than as a
    `422`, the way §20's and §21's do: this is the screen where the taints are
    *decided*, and answering "you changed nothing" with an error alone would
    withhold the current list at the moment it is the thing needed to pick a
    different one.
    """
    requested = validate_taints(payload)
    live, resource_version = _live(name)
    current = current_taints(live)
    diff = diff_taints(current, requested)

    pods, unavailable = _pods_on(name)
    deleting = (
        deletion_plan(pods, newly_enforced(diff)) if pods is not None else None
    )

    refusal = _no_change(diff, "taints")
    return {
        "name": name,
        "resourceVersion": resource_version,
        "current": current,
        "requested": requested,
        "added": diff["added"],
        "removed": diff["removed"],
        "changed": diff["changed"],
        # `null`, never `[]`, when the pod listing failed — see `_pods_on`.
        "deleting": deleting,
        "pods_checked": pods is not None,
        "unavailable": unavailable,
        # Never both: a blocked plan has no consequences to accept, and one that
        # is not blocked has nothing standing in the way.
        "blocked": _blocked(refusal) if refusal else None,
        "consequences": [] if refusal else taint_consequences(diff, deleting),
        "gate": enabled_state("taints"),
    }


def set_taints(
    name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/nodes/{name}/taints`` (§24) — through the single funnel.

    Order of refusals, each before the cluster is changed: request validation,
    the node read — which must answer — the concurrency check, the no-op check
    recomputed against the node as it is *now*, and the acknowledgement check
    over consequences recomputed the same way. Then :func:`mutate`, which gates,
    preflights ``patch core/nodes``, sends the patch with ``dryRun=All`` when this
    is a preview, diffs live against the API server's projection, and audits the
    outcome.

    **``applied: true`` means the taint list on the node is what you sent.** It
    does not mean any pod has gone yet: the taint manager acts on its own
    schedule, and a pod with a ``tolerationSeconds`` is still running by design.
    The ``deleting`` list in this response says who is on the way out.
    """
    requested = validate_taints(payload)
    live, live_version = _live(name)
    current = current_taints(live)

    sent_version = payload.get("resourceVersion")
    _require_version(name, sent_version, live_version, what="taints",
                     dry_run=dry_run, currentTaints=current)

    diff = diff_taints(current, requested)
    refusal = _no_change(diff, "taints")
    if refusal:
        raise refusal

    pods, unavailable = _pods_on(name)
    deleting = deletion_plan(pods, newly_enforced(diff)) if pods is not None else None
    consequences = taint_consequences(diff, deleting)
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = mutate(
        verb="patch",
        group=GROUP,
        version=VERSION,
        plural=PLURAL,
        namespace=None,
        name=name,
        dry_run=dry_run,
        gate=_gate("taints"),
        apply_fn=patch_fn(
            GROUP, VERSION, PLURAL, name,
            build_taint_patch(requested, resource_version=sent_version or live_version),
            content_type=MERGE_PATCH,
        ),
        before=live,
        detail=_taint_detail(name, diff),
    )
    result["current"] = current
    result["requested"] = requested
    result["deleting"] = deleting
    result["pods_checked"] = pods is not None
    result["unavailable"] = unavailable
    result["consequences"] = consequences
    return result


# --------------------------------------------------------------------------- #
# Labels — plan and write
# --------------------------------------------------------------------------- #

def build_label_patch(
    diff: dict[str, Any], requested: dict[str, str], *, resource_version: str | None
) -> dict[str, Any]:
    """The merge patch for ``metadata.labels``: every key sent, removals as ``null``.

    A merge patch over a map merges keys, so a key the caller dropped would
    survive unless it is explicitly set to ``null``. That is the one place where
    "send the whole map" needs help from the diff, and getting it wrong is a
    delete that silently does not happen.
    """
    labels: dict[str, Any] = dict(requested)
    for key in diff["removed"]:
        labels[key] = None
    patch: dict[str, Any] = {"metadata": {"labels": labels}}
    if resource_version:
        patch["metadata"]["resourceVersion"] = resource_version
    return patch


def _label_detail(name: str, diff: dict[str, Any]) -> str:
    parts = (
        [f"+{k}={v}" for k, v in sorted(diff["added"].items())]
        + [
            f"~{k}={c['after']} (was {c['before']})"
            for k, c in sorted(diff["changed"].items())
        ]
        + [f"-{k}" for k in sorted(diff["removed"])]
    )
    return f"node labels {name}: " + (", ".join(parts) if parts else "no change")


def plan_labels(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/nodes/{name}/labels/plan`` (§24). Ungated and unaudited."""
    requested = validate_labels(payload)
    live, resource_version = _live(name)
    current = current_labels(live)
    diff = diff_labels(current, requested)

    touched = sorted({*diff["removed"], *diff["changed"]})
    pods, unavailable = _pods_on(name)
    dependents = label_dependents(pods, touched) if pods is not None else None

    refusal = _no_change(diff, "labels")
    return {
        "name": name,
        "resourceVersion": resource_version,
        "current": current,
        "requested": requested,
        "added": diff["added"],
        "changed": diff["changed"],
        "removed": diff["removed"],
        # `null`, never `[]`, when the pod listing failed — see `_pods_on`.
        "dependents": dependents,
        "pods_checked": pods is not None,
        "unavailable": unavailable,
        "blocked": _blocked(refusal) if refusal else None,
        "consequences": [] if refusal else label_consequences(diff, dependents),
        "gate": enabled_state("labels"),
    }


def set_labels(
    name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/nodes/{name}/labels`` (§24) — through the single funnel.

    Same order of refusals as :func:`set_taints`, and the same recomputation of
    the consequences against the node as it is now.

    **``applied: true`` means the labels are stored, and nothing else.** No pod
    moves because of this write; what it changes is where the scheduler is
    willing to put them next time. That is the whole reason the plan names the
    pods that were placed by a rule mentioning these keys.
    """
    requested = validate_labels(payload)
    live, live_version = _live(name)
    current = current_labels(live)

    sent_version = payload.get("resourceVersion")
    _require_version(name, sent_version, live_version, what="labels",
                     dry_run=dry_run, currentLabels=current)

    diff = diff_labels(current, requested)
    refusal = _no_change(diff, "labels")
    if refusal:
        raise refusal

    touched = sorted({*diff["removed"], *diff["changed"]})
    pods, unavailable = _pods_on(name)
    dependents = label_dependents(pods, touched) if pods is not None else None
    consequences = label_consequences(diff, dependents)
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = mutate(
        verb="patch",
        group=GROUP,
        version=VERSION,
        plural=PLURAL,
        namespace=None,
        name=name,
        dry_run=dry_run,
        gate=_gate("labels"),
        apply_fn=patch_fn(
            GROUP, VERSION, PLURAL, name,
            build_label_patch(diff, requested, resource_version=sent_version or live_version),
            content_type=MERGE_PATCH,
        ),
        before=live,
        detail=_label_detail(name, diff),
    )
    result["current"] = current
    result["requested"] = requested
    result["dependents"] = dependents
    result["pods_checked"] = pods is not None
    result["unavailable"] = unavailable
    result["consequences"] = consequences
    return result


__all__ = [
    "CONTROL_PLANE_TAINTS",
    "RESERVED_LABEL_PREFIXES",
    "ROLE_LABEL_PREFIX",
    "build_label_patch",
    "build_taint_patch",
    "current_labels",
    "current_taints",
    "deletion_plan",
    "diff_labels",
    "diff_taints",
    "enabled_state",
    "label_consequences",
    "label_dependents",
    "newly_enforced",
    "plan_labels",
    "plan_taints",
    "set_labels",
    "set_taints",
    "taint_consequences",
    "validate_labels",
    "validate_taints",
]
