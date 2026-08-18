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
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.admin.nodes import cordon_node, drain_node
from app.services.nodes import get_node, list_nodes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["nodes"])


class _MutationBody(BaseModel):
    """Shared base for the §1.5 write bodies on this router.

    ``dryRun`` defaults to true and the alias accepts ``dry_run`` too. Both are
    safety rather than convenience: a client that omits the field gets a
    projection, and a client that sends the snake_case spelling is understood
    instead of silently falling back to the default — which for this field would
    mean an operator asking for a real drain, receiving a dry run, and being told
    ``applied: false`` about a node they believe is now empty.
    """

    model_config = ConfigDict(populate_by_name=True)

    dry_run: bool = Field(
        True,
        alias="dryRun",
        description="Send dryRun=All to the API server and return the projected diff.",
    )


class CordonRequest(_MutationBody):
    unschedulable: bool = Field(
        ...,
        # Required, with no default. Defaulting either way makes an empty body a
        # cluster change: to true it cordons a healthy node, to false it
        # uncordons one somebody drained during an incident. The caller says
        # which, or the request is rejected.
        description="True to cordon the node, false to uncordon it.",
    )


class DrainRequest(_MutationBody):
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
