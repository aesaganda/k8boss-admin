"""
Disruption budget endpoints (§28) — what a budget covers, and what it blocks.

Thin, like every router here: it parses the URL and delegates to
:mod:`app.services.disruption`, which owns the rows and their nulls.

**There is no per-budget read and no write in this module**, for §8.4's reasons.
PodDisruptionBudgets are browsable through §4 —
``GET /api/resources/policy/v1/poddisruptionbudgets`` — because
:func:`app.resources.shaping.poddisruptionbudget_row` is registered in the shaper
registry, so the generic path already returns the typed row with its paging and
its object-derived findings. Creating, editing and deleting one goes through
§4's ``POST``/``PUT``/``DELETE`` and therefore through the single mutation
funnel. A write implemented here would skip the gate, the preflight, the dry-run
diff and the audit row — and a budget edited without a diff shown first is
exactly the change that turns a routine drain into a stalled upgrade.

What this module owns is the correlation between budgets and pods, which needs a
read the shaper is not allowed to make.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from app.services.disruption import budgets

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["disruption"])


@router.get("/disruption/budgets")
def list_budgets(
    namespace: str | None = Query(
        None,
        description=(
            "Restrict to one namespace. Omit for the whole cluster — the two "
            "reads are one listing each either way, and the cluster-wide view "
            "is the one that finds the budget nobody has looked at since the "
            "workload it named was renamed."
        ),
    ),
) -> dict[str, Any]:
    """Every PodDisruptionBudget, with what it actually covers (§28).

    Three fields carry the weight, and each is a pair a listing collapses:

    ``selected_pods: 0`` is the finding — this budget's selector matches
    nothing, so it constrains no eviction while reading as protection.
    ``selected_pods: null`` is a pod listing that did not answer, or a selector
    this console could not evaluate, and it is never rendered as zero.

    ``findings`` separates *can never allow* from *is not allowing now*. The
    first is arithmetic — ``maxUnavailable: 0``, or ``minAvailable`` at the pod
    count — and no amount of waiting fixes it. The second is the disruption
    controller's last written count, and usually clears on its own.

    ``overlappingPods`` is the one no single budget can carry. Kubernetes does
    not support two budgets covering one pod: the eviction API refuses that pod
    outright, whatever either budget's ``disruptionsAllowed`` says. Both objects
    can look perfectly healthy while a drain over them cannot finish, and
    nothing on either explains it. ``null`` there means the pod listing failed —
    never ``[]``, which would say the console checked and found none.

    Nothing here reports what is **enforced**. The eviction subresource is the
    enforcer; these are the objects it consults, read at some past moment by a
    controller. §5's drain plan asks the same objects the other question — will
    *this* eviction be refused — and answers it per pod, at the drain.
    """
    return budgets(namespace)


__all__ = ["router"]
