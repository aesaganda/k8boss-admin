"""
CLI pods (§15) — the endpoints behind the masthead's terminal.

Three routes and no fourth. The shell itself is §7's
``WS /api/ws/pods/{namespace}/{name}/exec``, unchanged: this router creates the
pod to exec into, lists the ones already there, and removes them. A CLI-specific
exec socket would have been a second place to get the mutations gate, the
``pods/exec`` preflight and the open/close audit records right, and the second
place is the one that stops being right.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import cli_pod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["cli"])


class CliPodRequest(MutationBody):
    """``POST /api/cli`` body (§15).

    One field. Everything else about the pod — the ServiceAccount above all — is
    fixed by the deployment's configuration and shown in full in the diff before
    it is created. A knob per field would be a way to assemble a pod nobody
    reviewed, and letting a caller name the ServiceAccount would hand the choice
    of *what this shell can do* to whoever opens the dialog, which is exactly the
    decision that belongs to whoever configured the console.
    """

    model_config = ConfigDict(populate_by_name=True)

    image: str | None = Field(
        None,
        max_length=512,
        description=(
            "The CLI image. Omit for the console's configured default "
            "(ADMIN_CLI_IMAGE). It must carry kubectl or oc on its PATH."
        ),
    )


@router.get("/cli")
def get_cli_pods() -> dict[str, Any]:
    """CLI pods this console created (§15).

    A §1.2 envelope plus ``enabled``, ``enabledDetail``, ``namespace``,
    ``image``, ``serviceAccount`` and ``container``. The UI needs every one of
    them: the first two to disable the action *with the reason* rather than
    offering it and producing a 403, and the rest because they are what the
    operator is agreeing to — where the pod appears, what it runs, and which
    account decides what kubectl in it can reach.

    ``items: []`` is a real zero — the namespace was listed and holds none. A
    listing that could not happen raises (§0.1); a panel that showed "no
    session" because it failed to read the namespace would have an operator
    starting a second pod beside the one already running.
    """
    return cli_pod.list_cli_pods()


@router.post("/cli")
def create_cli_pod(body: CliPodRequest) -> dict[str, Any]:
    """Create a CLI pod (§15). §1.5 response plus the pod.

    Gated twice — ``ADMIN_ALLOW_MUTATIONS`` *and* ``ADMIN_CLI_ENABLED`` — and
    the whole manifest is the diff, so the ServiceAccount this pod binds is on
    screen before the operator confirms. That account is the only thing deciding
    what a shell here can do to the cluster, and Kubernetes offers no RBAC verb
    that gates which account a pod may bind; see :mod:`app.admin.cli_pod`.

    This endpoint always creates. Reuse of a Running pod is the UI's decision,
    made from the listing above, because a POST that sometimes creates and
    sometimes does not cannot report ``applied`` honestly.
    """
    return cli_pod.create_cli_pod(image=body.image, dry_run=body.dry_run)


@router.delete("/cli/{pod}")
def delete_cli_pod(
    pod: str,
    dryRun: bool = Query(  # noqa: N803 - §1.5 wire spelling
        True, description="Project the delete and return the diff without performing it.",
    ),
) -> dict[str, Any]:
    """Remove a CLI pod this console created (§15). §1.5 mutation response.

    Only pods carrying this console's label — anything else is ``404 not_found``
    from this route rather than a delete, because otherwise this would be a
    namespaced pod-delete wearing a friendlier URL.

    A query parameter rather than a body: DELETE bodies are handled
    inconsistently by proxies and HTTP clients, and a ``dryRun`` that silently
    went missing would turn a projection into a deletion.
    """
    return cli_pod.remove_cli_pod(pod, dry_run=dryRun)
