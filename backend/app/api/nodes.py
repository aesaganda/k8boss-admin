"""
Node endpoints (§5) — the capacity view and the two node writes.

Thin, like every router here: it validates what the URL and the body claim and
delegates. Reads go to :mod:`app.services.nodes`, which owns the row and its
nulls; writes go to :mod:`app.admin.nodes`, which routes them through the single
mutation funnel. A route that patched a node itself would bypass the mutations
gate, the preflight, the dry-run diff and the audit record in one line.

What this module does own is the *defaults*, and on the drain endpoint the
defaults are the safety argument:

* ``dryRun`` defaults to **true**, so a client that forgot the field gets a plan
  and a diff rather than an evacuated node.
* ``force`` defaults to **false**, so a drain with anything blocked stops and
  explains itself.
* ``deleteEmptyDirData`` defaults to **false**, because the console does not get
  to decide that somebody's scratch volume was unimportant.
* ``ignoreDaemonSets`` defaults to **true**, matching ``kubectl drain --ignore-daemonsets``
  in the only sense that is useful: a DaemonSet pod cannot be drained off a node
  it is scheduled to, so blocking on it by default would make every drain fail on
  every cluster that runs a CNI.
* ``gracePeriodSeconds`` defaults to **null**, meaning "use each pod's own
  ``terminationGracePeriodSeconds``". A number here overrides what the
  application asked for, and ``0`` is an immediate SIGKILL — neither is something
  to arrive at by default.

§24's four endpoints add the same shape one layer along: a plan that is a read,
and a ``PUT`` that carries the consequences the operator accepted. Neither the
taint list nor the label map is validated by pydantic beyond its container type,
on purpose — :mod:`app.admin.node_scheduling` refuses them with sentences that
name the effect and say what ``NoExecute`` does, which a schema error over a list
index cannot.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import node_debug, node_scheduling
from app.admin.nodes import cordon_node, drain_node
from app.services.nodes import get_node, list_nodes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["nodes"])


class CordonRequest(MutationBody):
    unschedulable: bool = Field(
        ...,
        # Required, with no default. Defaulting either way makes an empty body a
        # cluster change: to true it cordons a healthy node, to false it
        # uncordons one somebody drained during an incident. The caller says
        # which, or the request is rejected.
        description="True to cordon the node, false to uncordon it.",
    )


class DrainRequest(MutationBody):
    grace_period_seconds: int | None = Field(
        None,
        alias="gracePeriodSeconds",
        ge=0,
        # Bounded above because this is a text input and the request holds a
        # threadpool worker for the duration of the drain. An hour is longer than
        # any sane terminationGracePeriodSeconds and short enough to be a bug
        # report rather than a hung console.
        le=3600,
        description=(
            "Override each pod's terminationGracePeriodSeconds. Null uses the "
            "pod's own value; 0 is an immediate SIGKILL."
        ),
    )
    ignore_daemonsets: bool = Field(
        True,
        alias="ignoreDaemonSets",
        description=(
            "Skip DaemonSet-managed pods. False makes them block the drain, which "
            "is only useful for confirming what is on the node."
        ),
    )
    delete_emptydir_data: bool = Field(
        False,
        alias="deleteEmptyDirData",
        description=(
            "Accept the loss of emptyDir contents on this node. False blocks pods "
            "that hold one."
        ),
    )
    force: bool = Field(
        False,
        description=(
            "Proceed past blocked pods. It does not override a PodDisruptionBudget "
            "— the API server enforces those, and a pod it refuses is reported as "
            "a per-pod failure."
        ),
    )


class NodeDebugRequest(MutationBody):
    """``POST /api/nodes/{name}/debug`` body (§5.5).

    Two fields, deliberately. Everything else about the pod is fixed by
    :func:`app.admin.node_debug.build_pod` and shown in full in the diff before
    it is created — a knob per privileged field would be a way to assemble a pod
    nobody reviewed, and §4's YAML editor already exists for anything this shape
    does not cover.
    """

    image: str | None = Field(
        None,
        max_length=512,
        description=(
            "The debug image. Omit for the console's configured default "
            "(ADMIN_DEBUG_IMAGE)."
        ),
    )
    writable_host_filesystem: bool = Field(
        False,
        alias="writableHostFilesystem",
        description=(
            "Mount the node's root filesystem read-write instead of read-only. "
            "False is the default and is a deliberate departure from `kubectl "
            "debug`, which always mounts it writable: most node debugging reads, "
            "and a read-only /host cannot rewrite a static pod manifest or leave "
            "a binary behind that runs as root at next boot."
        ),
    )


@router.get("/nodes")
def get_nodes() -> dict[str, Any]:
    """Every node with its capacity, allocatable and actual requests (§5).

    ``requested`` and ``pod_count`` are ``null`` — never ``0`` — when the pod
    listing failed, with the reason in ``unavailable[]`` and ``partial: true``. A
    node reporting zero requested cores and zero pods reads as idle, and an idle
    node is the one an operator picks to drain.
    """
    return list_nodes()


@router.get("/nodes/{name}")
def get_node_detail(
    name: str = Path(..., description="Node name, as reported by the API server."),
) -> dict[str, Any]:
    """One node, plus every pod scheduled to it (§5).

    ``pods`` is ``null``, not ``[]``, when the pod listing failed. This is the
    page read immediately before a drain, and an empty table here is the sentence
    that gets the button clicked.
    """
    return get_node(name)


@router.post("/nodes/{name}/cordon")
def cordon(name: str, body: CordonRequest) -> dict[str, Any]:
    """Mark the node un/schedulable. §1.5 mutation response.

    Nothing moves: pods already on the node keep running. That is the difference
    from drain, and it is why they are separate endpoints rather than a flag.
    """
    return cordon_node(name, body.unschedulable, body.dry_run)


@router.post("/nodes/{name}/drain")
def drain(name: str, body: DrainRequest) -> dict[str, Any]:
    """Cordon the node and evict what is on it. §1.5 response plus the plan (§5).

    The response carries a per-pod ``plan`` in every case, including the dry run
    and including the refusal — with ``blocked > 0`` and ``force: false`` the
    call is ``422 invalid`` and the plan travels in the error's ``context``, so
    the operator sees precisely what to resolve.

    ``applied`` follows §1.5 and means "we wrote to the cluster". ``drained`` is
    the field the UI headlines: true only when every eviction succeeded. A drain
    that lost three pods is ``applied: true, failed: 3, drained: false``, because
    reporting it as a success is how a machine with a database on it gets
    terminated.
    """
    return drain_node(
        name,
        dry_run=body.dry_run,
        grace_period_seconds=body.grace_period_seconds,
        ignore_daemonsets=body.ignore_daemonsets,
        delete_emptydir_data=body.delete_emptydir_data,
        force=body.force,
    )


class TaintPlanRequest(BaseModel):
    """The complete taint list the node should end up with."""

    model_config = ConfigDict(populate_by_name=True)

    taints: list[dict[str, Any]] = Field(
        ...,
        description=(
            "Every taint the node should carry afterwards, as "
            "{key, value, effect}. The whole list, not a delta: spec.taints is "
            "an atomic list in the API, so a patch replaces it outright and "
            "there is no add-one operation to expose. An empty list removes "
            "every taint."
        ),
    )
    resourceVersion: str | None = Field(  # noqa: N815
        None,
        description=(
            "The node's resourceVersion as read. Sent back on the write, where "
            "it rides inside the merge patch and §0.4 is enforced twice."
        ),
    )


class TaintRequest(MutationBody, TaintPlanRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned, named. Recomputed "
            "server-side against the node as it is at write time — pods arrive "
            "and leave on their own, so 'this deletes two pods' can be a "
            "different number by the time the write lands."
        ),
    )


class LabelPlanRequest(BaseModel):
    """The complete label map the node should end up with."""

    model_config = ConfigDict(populate_by_name=True)

    labels: dict[str, Any] = Field(
        ...,
        description=(
            "Every label the node should carry afterwards. The whole map: a key "
            "left out is a key this removes. Values are strings — a number or a "
            "boolean is refused here rather than by the API server, which names "
            "a type instead of the key."
        ),
    )
    resourceVersion: str | None = Field(  # noqa: N815
        None, description="The node's resourceVersion as read. See TaintPlanRequest.",
    )


class LabelRequest(MutationBody, LabelPlanRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description="Every consequence code the plan returned, named.",
    )


@router.post("/nodes/{name}/taints/plan")
def get_taint_plan(request: TaintPlanRequest, name: str) -> dict[str, Any]:
    """§24 — the taint diff, and which pods a NoExecute taint would delete.

    Ungated and unaudited: two reads and set arithmetic. It writes nothing and
    does not dry-run the patch — a dry run is a write request the caller has not
    made yet.

    ``deleting`` is ``null``, never ``[]``, when the node's pod listing failed,
    and ``pods_checked`` says which of the two happened. An empty list here means
    the taint removes nothing; a null means nobody counted.
    """
    return node_scheduling.plan_taints(name, request.model_dump(by_alias=True))


@router.put("/nodes/{name}/taints")
def set_taints(request: TaintRequest, name: str) -> dict[str, Any]:
    """§24 — replace the node's taints, through the funnel. §1.5 response.

    ``PUT`` because §0.4 applies: the caller sends the version they were looking
    at, it is checked here — which is what produces a `409` carrying the taints
    as they are *now* — and it rides inside the merge patch, so the API server
    refuses a stale write too.

    **`applied: true` means the taint list is stored**, not that any pod has
    gone. The taint manager removes pods on its own schedule, and a pod with a
    ``tolerationSeconds`` is still running by design; ``deleting`` in the same
    response says who is on the way out.
    """
    return node_scheduling.set_taints(
        name,
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


@router.post("/nodes/{name}/labels/plan")
def get_label_plan(request: LabelPlanRequest, name: str) -> dict[str, Any]:
    """§24 — the label diff, and the pods placed here by a rule that names a key.

    Ungated and unaudited. ``dependents`` is ``null`` when the pod listing
    failed, for the reason ``deleting`` is on the taint plan.
    """
    return node_scheduling.plan_labels(name, request.model_dump(by_alias=True))


@router.put("/nodes/{name}/labels")
def set_labels(request: LabelRequest, name: str) -> dict[str, Any]:
    """§24 — replace the node's labels, through the funnel. §1.5 response.

    **Nothing is evicted by this write, and that is the thing to know.** Node
    affinity is ``requiredDuringSchedulingIgnoredDuringExecution``: the rule is
    checked when a pod is placed and never again, so pods scheduled here by a
    label you are removing keep running unchanged. What changes is where they can
    go next time, which is discovered at the next rollout.
    """
    return node_scheduling.set_labels(
        name,
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


@router.get("/nodes/{name}/debug")
def get_node_debug_pods(name: str) -> dict[str, Any]:
    """Debug pods this console created for this node (§5.5).

    A §1.2 envelope plus ``enabled``, ``enabledDetail`` and ``namespace``.
    ``enabled`` is the deployment's two gates answered together, so the UI can
    disable the action with the reason rather than offering it and producing a
    403; ``namespace`` is where a new pod would appear, which an operator should
    not have to guess about a privileged pod.

    ``items: []`` is a real zero — the namespace was listed. A listing that could
    not happen raises (§0.1).
    """
    return node_debug.list_node_debug_pods(name)


@router.post("/nodes/{name}/debug")
def create_node_debug_pod(name: str, body: NodeDebugRequest) -> dict[str, Any]:
    """Create a debug pod on this node (§5.5). §1.5 response plus the pod.

    **The most privileged object this console creates.** The pod is pinned to
    this node, tolerates every taint, shares the host PID and network namespaces
    and mounts the node's root filesystem at /host. A shell in it is effectively
    root on the machine.

    It is gated twice — ``ADMIN_ALLOW_MUTATIONS`` *and* ``ADMIN_NODE_DEBUG_ENABLED``
    — and the whole manifest is the diff, so every one of those properties is on
    screen before the operator confirms. Unlike §7.4's ephemeral containers, the
    pod it creates can and should be removed afterwards; see the DELETE below.
    """
    return node_debug.create_node_debug_pod(
        name,
        image=body.image,
        writable_host=body.writable_host_filesystem,
        dry_run=body.dry_run,
    )


@router.delete("/nodes/{name}/debug/{pod}")
def delete_node_debug_pod(
    name: str,
    pod: str,
    dryRun: bool = Query(  # noqa: N803 - §1.5 wire spelling
        True, description="Project the delete and return the diff without performing it.",
    ),
) -> dict[str, Any]:
    """Remove a debug pod this console created (§5.5). §1.5 mutation response.

    Only pods carrying this console's label *and* pinned to this node — anything
    else is ``404 not_found`` for this route rather than a delete, because
    otherwise this would be a general pod-delete with a node in its path.

    A query parameter rather than a body: DELETE bodies are inconsistently
    handled by proxies and HTTP clients, and a ``dryRun`` that silently went
    missing would turn a projection into a deletion.
    """
    return node_debug.remove_node_debug_pod(name, pod, dry_run=dryRun)
