"""
Cluster status endpoint (§19).

Thin, like every router here: no parameters, one call, one response. The five
reads and every decision about what they mean live in
:mod:`app.services.cluster_status`, whose docstring is where the argument for
each signal — and for the ones deliberately absent — is written down.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.services import cluster_status as cluster_status_service

router = APIRouter(prefix="/api", tags=["cluster-status"])


@router.get("/cluster-status")
def get_cluster_status() -> dict[str, Any]:
    """§19 — the control plane's health, from the five APIs vanilla serves.

    Each section is collected independently, so a refused listing costs that
    section alone: its key is `null`, the reason is in `unavailable[]` and
    `partial` is true. A `null` section is never rendered as healthy — "we could
    not read the admission webhooks" and "this cluster has no admission
    webhooks" are answers an operator acts on very differently.
    """
    return cluster_status_service.get_cluster_status()


__all__ = ["get_cluster_status", "router"]
