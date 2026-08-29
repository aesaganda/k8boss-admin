"""
Network policy endpoints (§8.4) — the two questions a listing cannot answer.

Thin, like every router here: it parses the URL and delegates to
:mod:`app.services.network`, which owns the rows and their nulls.

**There is no list endpoint in this module, and that is deliberate.**
NetworkPolicies are listed through §4 — ``GET /api/resources/networking.k8s.io/v1/networkpolicies``
— because :func:`app.resources.shaping.networkpolicy_row` is registered in the
shaper registry, so the generic path already returns the typed §8 row with its
paging, its label selectors and its ``continue`` cursor. A second listing here
would be a second shaping of the same object that could disagree with the first,
which is the failure ``docs/architecture.md`` names about typed and generic reads
diverging.

**There are no writes in this module either.** Creating, editing and deleting a
NetworkPolicy goes through §4's ``POST``/``PUT``/``DELETE``, which route into
:mod:`app.admin.apply` and therefore through the single mutation funnel — the
mutations gate, the preflight, the dry-run diff and the audit record, in that
order. A ``POST /api/network/policies`` implemented here would be a write that
skipped all four, and a NetworkPolicy applied without a diff shown first is
exactly the change whose blast radius nobody sees until traffic stops.

What this module does own is the correlation between policies and pods, which
needs a read the shaper is not allowed to make.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path, Query

from app.services.network import get_network_policy, namespace_isolation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["network"])


@router.get("/network/policies/{namespace}/{name}")
def get_policy(
    namespace: str = Path(..., description="The policy's namespace."),
    name: str = Path(..., description="The policy's name."),
) -> dict[str, Any]:
    """One NetworkPolicy, plus the pods it selects (§8.4).

    ``selected_pods`` is ``null``, not ``[]``, when the pod listing failed or a
    selector could not be evaluated — with the reason in ``unavailable[]`` and
    ``partial: true``. An empty list is the finding that a policy governs
    nothing and gets it deleted as dead; a failed read must not look like one.
    """
    return get_network_policy(namespace, name)


@router.get("/network/isolation")
def get_isolation(
    namespace: str | None = Query(
        None,
        description=(
            "Restrict to one namespace. Omit for every pod in the cluster — the "
            "two reads are one listing each either way, and the cluster-wide "
            "view is the one that finds a namespace nobody wrote a policy for."
        ),
    ),
) -> dict[str, Any]:
    """Every pod, and which NetworkPolicies select it (§8.4).

    The rows are pods, not policies, because the question this endpoint exists
    for is which pods **nothing** selects — and a pod nothing selects appears on
    no policy's page. Kubernetes' default is allow, so an unselected pod accepts
    traffic from anywhere in the cluster.

    ``isolated`` says a policy selecting the pod declares that direction. It does
    **not** say the cluster enforces it: NetworkPolicy is implemented by the CNI
    plugin, no API reports whether this cluster's does, and a console field that
    implied otherwise would be a security claim resting on nothing.
    """
    return namespace_isolation(namespace)


__all__ = ["get_isolation", "get_policy", "router"]
