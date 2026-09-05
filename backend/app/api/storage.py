"""
Storage writes — expanding a PersistentVolumeClaim (§20) and snapshotting one (§22).

Thin, like every router here: parse, call, envelope. Every decision about
whether a claim can grow, what growing it means, and what a green result does
*not* prove lives in :mod:`app.admin.pvc`, which reaches a cluster only through
:func:`app.admin.mutate.mutate`.

There is no listing endpoint here and there deliberately will not be one.
Claims, volumes and StorageClasses are browsed through §4's generic path, which
already returns the typed §8 rows because their shapers are registered on the
backend — a second listing in this file would be a second shaping of the same
object, free to drift from the first and impossible to notice from outside.

The gate is echoed onto the plan rather than fetched separately, for §16's
reason: a dialog that had to ask a second endpoint whether writing is permitted
would render its button before knowing.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import pvc as pvc_admin
from app.admin import snapshot as snapshot_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["storage"])

_CLAIM = "/storage/claims/{namespace}/{name}"


class ExpandPlanRequest(BaseModel):
    """The size to ask for, and the version the operator was looking at."""

    model_config = ConfigDict(populate_by_name=True)

    size: str = Field(
        ...,
        description=(
            "The requested capacity, as a Kubernetes quantity — 20Gi, 500M, 1Ti. "
            "Kept verbatim: the string that reaches the API server is the string "
            "that was typed, so the number in the diff is the number that was "
            "confirmed."
        ),
    )
    resourceVersion: str | None = Field(  # noqa: N815
        None,
        description=(
            "The claim's resourceVersion as read. Sent back on the write, where "
            "it rides inside the merge patch and §0.4 is enforced twice."
        ),
    )


class ExpandRequest(MutationBody, ExpandPlanRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned, named. Recomputed "
            "server-side against the claim as it is at write time — two of them "
            "are on every expansion and cannot be got past by an empty list."
        ),
    )


@router.post(_CLAIM + "/expand/plan")
def get_expand_plan(
    request: ExpandPlanRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The claim."),
) -> dict[str, Any]:
    """§20 — the claim's size and capacity, what has it mounted, and what growing it means.

    Ungated, like §17's and §18's plans: one claim read, one StorageClass read,
    one pod listing and arithmetic. Writes nothing and records no audit row.

    A size this claim cannot be given answers `200` with `blocked` set rather
    than `422` — the plan is the screen where the size is *decided*, and one
    that answered a too-small number with an error alone would withhold the
    current size, the capacity and the mounts at the moment those are the three
    facts needed. The write refuses.
    """
    return pvc_admin.plan(namespace, name, request.model_dump(by_alias=True))


class SnapshotPlanRequest(BaseModel):
    """The name for the snapshot, and optionally which class takes it."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(
        ...,
        description=(
            "The VolumeSnapshot's name. Not generated: the name is how anyone "
            "finds this snapshot again, and a generated one would be a string "
            "nobody recognises during the incident it was taken for."
        ),
    )
    snapshotClass: str | None = Field(  # noqa: N815
        None,
        description=(
            "The VolumeSnapshotClass to use. **Omitting it is meaningful** — it "
            "asks the controller for the cluster default, which is a real thing "
            "the API does, and is not the same as a class this console could not "
            "read. The plan keeps those two apart."
        ),
    )


class SnapshotRequest(MutationBody, SnapshotPlanRequest):
    """The plan's body plus the two fields that make it a write."""

    acknowledgeConsequences: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Every consequence code the plan returned, named. Recomputed "
            "server-side against the cluster at write time — the default snapshot "
            "class can change, and two of these are on every snapshot."
        ),
    )


@router.post(_CLAIM + "/snapshot/plan")
def get_snapshot_plan(
    request: SnapshotPlanRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The claim."),
) -> dict[str, Any]:
    """§22 — which class would take this snapshot, and what a snapshot is not.

    Ungated, like §17's, §18's, §20's and §21's plans: one claim read and one
    class listing. Writes nothing and records no audit row.

    `snapshotClass.deletion_policy` is tri-state — `Delete`, `Retain`, or `null`
    when the classes could not be read or the cluster marks no default. `null`
    is never rendered as either: one would warn about data loss that will not
    happen, the other would withhold a warning about data loss that will.
    """
    return snapshot_admin.plan(namespace, name, request.model_dump(by_alias=True))


@router.post(_CLAIM + "/snapshot")
def take_snapshot(
    request: SnapshotRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The claim."),
) -> dict[str, Any]:
    """§22 — create the VolumeSnapshot, through the funnel, with the diff first.

    **`applied: true` means the object exists, not that a snapshot was taken.**
    The controller does that afterwards and reports it by setting
    `status.readyToUse`, which starts out `null` and can end at `false` with an
    error. Nothing in this response is evidence that there is anything to restore
    from; §22's row is where that answer lives.
    """
    return snapshot_admin.take_snapshot(
        namespace,
        name,
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


@router.put(_CLAIM + "/size")
def expand_claim(
    request: ExpandRequest,
    namespace: str = Path(..., min_length=1, max_length=63, description="The namespace."),
    name: str = Path(..., min_length=1, max_length=253, description="The claim."),
) -> dict[str, Any]:
    """§20 — grow the claim, through the funnel, with the diff shown first.

    ``PUT`` rather than ``PATCH`` because §0.4's concurrency rule applies: the
    caller sends the version they were looking at, it is checked here — which is
    what produces a `409` carrying the claim's size *now* — and it rides inside
    the merge patch, so the API server refuses a stale write too.

    **`applied: true` means the claim requests the new size**, and nothing more.
    The volume grows when the storage provider grows it and the filesystem after
    that; `current.capacity` in the same response is what a workload has today.
    """
    return pvc_admin.expand(
        namespace,
        name,
        request.model_dump(by_alias=True, exclude={"acknowledgeConsequences", "dry_run"}),
        dry_run=request.dry_run,
        acknowledge_consequences=request.acknowledgeConsequences,
    )


__all__ = ["router"]
