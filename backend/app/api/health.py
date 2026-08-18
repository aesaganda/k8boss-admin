"""
Health and capability (§2).

This endpoint reports on **the console**, which is why ``status`` is only ever
``ok`` or ``degraded`` and never ``error``: if the process can answer at all,
the console is up. A cluster being unreachable is a fact about a cluster and
belongs in ``degraded[]``, not in the status of the thing reporting it. A probe
that fails because a customer's cluster is down would restart a perfectly
healthy pod.

``mutations`` is here rather than only on the write endpoints so the UI can
disable write affordances up front (§11.5) instead of offering buttons that fail
— an operator who clicks Scale and gets a 403 learns nothing about why.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy.orm import Session

from app import database
from app.config import settings
from app.models import Cluster

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


def _cluster_health(db: Session) -> tuple[dict, list[dict]]:
    """Registered/reachable counts plus a degraded entry per cluster that is not ok.

    Reads the **stored** connectivity state rather than probing every cluster on
    every call. Deliberate: health is polled by the UI, by Kubernetes probes and
    by whatever monitoring an operator points at it, and live-probing a fleet
    from a liveness endpoint turns one slow API server into a slow health check
    into a restarted pod. The stored state is written by
    ``POST /api/clusters/{id}/test`` and refreshed by any endpoint that
    successfully reaches a cluster.

    ``unknown`` gets its own reason and is not folded into ``unreachable``. A
    cluster registered a minute ago and never tested has not failed; reporting it
    as failing sends an operator to debug a healthy cluster, and after that
    happens twice they stop reading this list.
    """
    clusters = db.query(Cluster).order_by(Cluster.id.asc()).all()
    reachable = sum(1 for c in clusters if c.status == "connected")

    degraded: list[dict] = []
    for cluster in clusters:
        if cluster.status == "connected":
            continue
        if cluster.status == "unknown":
            degraded.append({
                "component": f"cluster:{cluster.id}",
                "reason": "not_tested",
                "detail": (
                    f"{cluster.name} has been registered but never successfully "
                    "reached. Run the connection test to find out whether it works."
                ),
            })
        else:
            degraded.append({
                "component": f"cluster:{cluster.id}",
                "reason": "unreachable",
                "detail": cluster.status_detail or (
                    f"{cluster.name} was unreachable at the last connection test."
                ),
            })

    return {"registered": len(clusters), "reachable": reachable}, degraded


@router.get("/health")
def health() -> dict:
    """Console health, version, write-mode and per-cluster reachability."""
    degraded: list[dict] = []
    db = None
    try:
        # Resolved off the module rather than imported by name so a test (or a
        # second engine) that swaps app.database.SessionLocal is followed here
        # too. A session opened outside a dependency also lets a failure to
        # CONNECT be reported in `degraded` instead of escaping as a 500 —
        # which is the one thing this endpoint must never do.
        db = database.SessionLocal()
        clusters, cluster_degraded = _cluster_health(db)
        degraded.extend(cluster_degraded)
    except Exception as e:  # noqa: BLE001 - any database fault, reported not raised
        # Nulls, not zeros. "registered: 0" would say the operator has no
        # clusters registered, which is a claim this code just failed to check —
        # and it is the claim that makes an empty cluster switcher look correct.
        logger.error("Health check could not read the cluster registry: %s", e)
        clusters = {"registered": None, "reachable": None}
        degraded.append({
            "component": "database",
            "reason": "unreachable",
            "detail": (
                f"The console's own database could not be read ({type(e).__name__}). "
                "Cluster counts are unknown, not zero."
            ),
        })
    finally:
        if db is not None:
            db.close()

    return {
        "status": "degraded" if degraded else "ok",
        "version": settings.app_version,
        "mutations": "enabled" if settings.admin_allow_mutations else "disabled",
        "clusters": clusters,
        "degraded": degraded,
    }
