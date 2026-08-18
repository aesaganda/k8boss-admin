"""
k8boss-admin — application entry point.

Middleware order is load-bearing, so it is spelled out here rather than left to
the reader of ``add_middleware`` calls. Starlette wraps each added middleware
*around* what came before, so the last one added is the outermost:

    CORS  ->  request logging  ->  cluster context  ->  exception handlers  ->  routes

CORS outermost is the point. Everything the app can produce — including a 502 for
an unreachable cluster and a 403 naming a missing RBAC grant — passes back out
through it and keeps its ``Access-Control-Allow-Origin`` header. When an error is
rendered *outside* CORS (which is what happens to anything reaching Starlette's
ServerErrorMiddleware) the browser blocks the response and ``fetch()`` rejects
with ``TypeError: Failed to fetch``: the operator sees a network error and the
real reason is never delivered. ``register_exception_handlers`` exists to make
sure nothing gets that far.

Cluster context sits inside request logging so the correlation id is set before
the context is pinned, and both are outside the routes that read them.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.exception_handlers import register_exception_handlers
from app.config import settings
from app.database import create_tables
from app.k8s.client import manager as cluster_manager
from app.k8s.context import ClusterContextMiddleware
from app.middleware.logging import RequestLoggingMiddleware, setup_logging

from app.api.access import router as access_router
from app.api.audit import router as audit_router
from app.api.clusters import router as clusters_router
from app.api.events import router as events_router
from app.api.exec_ws import router as exec_ws_router
from app.api.health import router as health_router
from app.api.logs import router as logs_router
from app.api.namespaces import router as namespaces_router
from app.api.nodes import router as nodes_router
from app.api.resources import router as resources_router
from app.api.workloads import router as workloads_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown."""
    setup_logging()
    logger.info(
        "k8boss-admin %s starting (mutations=%s, secret reveal=%s)",
        settings.app_version,
        "enabled" if settings.admin_allow_mutations else "disabled",
        "enabled" if settings.secret_reveal_enabled else "disabled",
    )
    if settings.admin_allow_mutations:
        # Not an INFO detail. With this on, any caller that can reach the API can
        # write to every registered cluster, and the only identity on the audit
        # trail is the advisory X-K8Boss-User header. That is a deliberate
        # posture and a serious accident, and the difference has to be visible in
        # the logs of the pod it is happening in.
        logger.warning(
            "ADMIN_ALLOW_MUTATIONS is enabled: this console can write to every "
            "registered cluster. There is no authentication in front of it, and "
            "audit attribution comes from the spoofable X-K8Boss-User header. "
            "Put an authenticating proxy in front of this deployment."
        )

    create_tables()
    logger.info("Database schema ready")

    try:
        yield
    finally:
        # Close every pooled transport and delete the CA temp files they wrote.
        # Left behind, those accumulate one file per cluster per process restart
        # in a container's writable layer.
        cluster_manager.reset()
        logger.info("k8boss-admin stopped")


app = FastAPI(
    title="k8boss-admin",
    description="Kubernetes administration console — read, preflight, dry-run, apply, audit.",
    version=settings.app_version,
    lifespan=lifespan,
)

# Innermost first. See the module docstring for why this order matters.
app.add_middleware(ClusterContextMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The SPA reads the correlation id off responses so a user can quote it in a
    # bug report; a cross-origin response header is invisible to JS unless it is
    # named here.
    expose_headers=["X-Request-ID"],
)

register_exception_handlers(app)

app.include_router(health_router)
app.include_router(clusters_router)
app.include_router(resources_router)
app.include_router(namespaces_router)
app.include_router(events_router)
app.include_router(workloads_router)
app.include_router(nodes_router)
app.include_router(access_router)
app.include_router(audit_router)
app.include_router(logs_router)
app.include_router(exec_ws_router)


if __name__ == "__main__":  # pragma: no cover - developer convenience
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
