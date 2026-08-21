"""
Debug containers (§7.4) — the two routes over :mod:`app.admin.debug`.

Thin, like every router in this package: parse, call, envelope. The knowledge of
what an ephemeral container is, which patch attaches one and what a cluster that
cannot serve them should be told lives one layer down.

Two routes rather than one because they answer two different questions and need
two different permissions. The listing answers *what is already in this pod* and
needs only ``get pods``, so a read-only console still shows an operator that
somebody attached a debugger to the payments pod an hour ago. The POST answers
*attach one*, and is a write in every sense: it is gated, preflighted on
``patch pods/ephemeralcontainers``, dry-run-able and audited by the funnel.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Body, Path
from pydantic import BaseModel, ConfigDict, Field

from app.admin import debug as debug_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["debug"])


class DebugRequest(BaseModel):
    """``POST /api/pods/{namespace}/{name}/debug`` body.

    ``dryRun`` defaults to **true**, and the alias accepts ``dry_run`` too, for
    the reason :class:`app.api.workloads._MutationBody` documents: a client that
    forgets the field gets a projection rather than a write, and one that sends
    the snake_case spelling is understood rather than silently defaulted.
    """

    model_config = ConfigDict(populate_by_name=True)

    dry_run: bool = Field(
        True,
        alias="dryRun",
        description="Send dryRun=All to the API server and return the projected diff.",
    )
    image: str | None = Field(
        None,
        max_length=512,
        description=(
            "The debug image. Omit to use the console's configured default "
            "(ADMIN_DEBUG_IMAGE). Pick one that carries the tools you need — the "
            "point of a debug container is that the pod's own image does not."
        ),
    )
    container: str | None = Field(
        None,
        max_length=63,
        alias="container",
        description=(
            "Name for the debug container. Omit to have one generated as "
            "`debugger-xxxxx`. Refused with 422 if the pod already has a "
            "container of that name — containers, init containers and ephemeral "
            "containers share one namespace of names."
        ),
    )
    target_container: str | None = Field(
        None,
        alias="targetContainer",
        max_length=63,
        description=(
            "A container of this pod whose process namespace the debug container "
            "should share, so `ps` sees the application's processes. Omit for "
            "network and volumes only, which is the widely supported case: not "
            "every runtime implements process namespace targeting, and where it "
            "is unimplemented the field is ignored rather than refused."
        ),
    )
    command: list[str] | None = Field(
        None,
        description=(
            "argv to run instead of the image's entrypoint, argument by argument. "
            "A list, not a string: joining one and letting a shell split it would "
            "make quoting decide what runs inside somebody's pod."
        ),
    )
    tty: bool = Field(
        True,
        description=(
            "Allocate a TTY. True is right for a shell; false for a debug image "
            "whose entrypoint writes a report and exits."
        ),
    )


@router.get("/pods/{namespace}/{name}/debug")
def get_debug_containers(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
) -> dict[str, Any]:
    """Every debug (ephemeral) container already in this pod (§7.4).

    A §1.2 envelope plus ``supported`` and ``supportDetail``. ``supported`` is
    three-valued: ``true``, ``false``, and ``null`` when the cluster's discovery
    document could not be read — which is not the same as "this cluster cannot
    do it", and rendering it as such would send an operator to plan an upgrade
    they may not need.

    ``items: []`` is a real zero: the pod was read and has no ephemeral
    containers. A pod that could not be read raises instead of answering with an
    empty list, per §0.1.
    """
    return debug_service.list_debug_containers(namespace, name)


@router.post("/pods/{namespace}/{name}/debug")
def attach_debug_container(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
    body: DebugRequest = Body(default_factory=DebugRequest),
) -> dict[str, Any]:
    """Attach a debug container to a running pod (§7.4).

    Returns the §1.5 mutation response with ``container``, ``image``,
    ``targetContainer``, ``command`` and ``tty`` added, so the caller can open a
    terminal on what it just created without parsing the generated name back out
    of the diff.

    **An ephemeral container cannot be removed.** The API has no verb for it; it
    lives as long as the pod does. That is why this is a dry-run-then-confirm
    action rather than a button, and why the projected diff is worth reading:
    what the operator is consenting to is permanent for the life of the pod.
    """
    return debug_service.attach_debug_container(
        namespace,
        name,
        image=body.image,
        container=body.container,
        target_container=body.target_container,
        command=body.command,
        tty=body.tty,
        dry_run=body.dry_run,
    )


__all__ = ["DebugRequest", "attach_debug_container", "get_debug_containers", "router"]
