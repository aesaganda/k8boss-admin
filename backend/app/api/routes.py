"""
Route and router endpoints (§13, §14).

Two path families, one module, because they are two halves of one feature: §13
is *the exposures*, and §14 is *the thing that serves them*. Splitting them
would put the question "will anything actually serve this Ingress I just wrote"
on the far side of a module boundary from the write, and that question is the
one the Routes screen exists to answer honestly.

Thin, like every router in this package. Reads delegate to
:mod:`app.services.routes`; writes delegate to :mod:`app.admin.routes` and
:mod:`app.admin.router`, both of which reach a cluster only through
:func:`app.admin.mutate.mutate`. Nothing here builds a request body for the API
server, and nothing here decides whether a write is allowed.

One thing *is* decided here, and only here: the wire shapes. ``dryRun`` defaults
to true on every write body, spelled both ways, for the reason §6's
``MutationBody`` gives — a client that forgot the field must get a projection,
and a client that sent ``dry_run`` must not silently get one when it asked for a
write.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin import router as router_service
from app.admin import routes as routes_admin
from app.services import routes as routes_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["routes"])

#: Backend keys, as a Literal so FastAPI rejects an unknown one at the edge with
#: a 422 that names the valid values, rather than letting it reach
#: `resolve_backend` and produce the same answer one layer deeper.
BackendKey = Literal["openshift", "ingress", "gateway"]


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class TargetModel(BaseModel):
    """One Service an exposure sends traffic to."""

    model_config = ConfigDict(populate_by_name=True)

    service: str = Field(..., min_length=1, max_length=253)
    port: int | str | None = Field(
        None,
        description=(
            "Service port number or name. A number and the string form of that "
            "number are the same thing here and are normalised; a non-numeric "
            "string is a port *name*, which Ingress accepts and Gateway API "
            "does not."
        ),
    )
    weight: int | None = Field(
        None, ge=0, le=256,
        description=(
            "Relative share of traffic. Null, not 100, when the exposure has one "
            "target: a weight rendered on a single-backend exposure reads as a "
            "traffic split that was never configured."
        ),
    )


class TLSModelRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    termination: Literal["edge", "passthrough", "reencrypt"] | None = None
    insecurePolicy: Literal["None", "Allow", "Redirect"] | None = None  # noqa: N815
    secretName: str | None = Field(  # noqa: N815
        None, max_length=253,
        description=(
            "A Secret holding the certificate. The only way to give one through "
            "this model: there is deliberately no inline certificate/key pair "
            "here, because a private key sent this way would travel through the "
            "request, the projection, the diff and browser memory on a path "
            "`redact_secret` cannot guard. Write `spec.tls.certificate` in the "
            "YAML view if a Route genuinely needs it — that document is written "
            "verbatim. See `app.admin.routes.TLSModel`."
        ),
    )
    destinationCACertificate: str | None = Field(  # noqa: N815
        None,
        description=(
            "The CA that signed the pod's serving certificate. Public material, "
            "and required for a reencrypt exposure: without it the router has "
            "nothing to verify the backend against."
        ),
    )


class ExposureModel(BaseModel):
    """The §13 exposure — what the form collects, before it is any API's object."""

    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=253)
    namespace: str = Field(..., min_length=1, max_length=253)
    targets: list[TargetModel] = Field(..., min_length=1, max_length=routes_admin.MAX_TARGETS)
    host: str | None = Field(None, max_length=253)
    subdomain: str | None = Field(None, max_length=253)
    path: str | None = Field(None, max_length=1024)
    pathType: Literal["Prefix", "Exact"] = "Prefix"  # noqa: N815
    tls: TLSModelRequest = Field(default_factory=TLSModelRequest)
    wildcardPolicy: Literal["None", "Subdomain"] | None = None  # noqa: N815
    ingressClassName: str | None = Field(None, max_length=253)  # noqa: N815
    parentRefs: list[dict[str, Any]] = Field(default_factory=list)  # noqa: N815
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class RenderRequest(BaseModel):
    """Body of ``POST /api/routes/render``. Not a write — see the handler."""

    model_config = ConfigDict(populate_by_name=True)

    backend: BackendKey
    spec: ExposureModel | None = Field(
        None,
        description=(
            "The exposure to compile. Omit it — sending only `document` — to "
            "take the document verbatim, which is what the YAML view does once "
            "the operator has edited it by hand."
        ),
    )
    document: str | None = Field(
        None,
        description=(
            "The YAML currently in the editor. With `spec`, the form's fields "
            "are patched into it rather than replacing it, so anything the form "
            "does not model survives a trip through the form view. Without "
            "`spec`, this is written exactly as given."
        ),
    )


class _ExposureWrite(MutationBody):
    """Shared body for creating and replacing an exposure.

    ``spec`` is optional and its absence is meaningful: with a ``document`` and
    no ``spec``, the document is written **verbatim**. That is what makes the
    YAML view honest — without it, a hand-edited document would have the form's
    fields recompiled over the top of it at write time, silently undoing the
    edit the UI had just locked the form to protect.
    """

    backend: BackendKey
    spec: ExposureModel | None = None
    document: str | None = None
    acknowledgeLossy: list[str] = Field(  # noqa: N815
        default_factory=list,
        description=(
            "Feature tokens whose loss the caller accepts. A write that asks for "
            "something the chosen backend cannot express is refused unless every "
            "such feature is named here — the same shape as `force` on a node "
            "drain, and for the same reason: consenting to a consequence has to "
            "be a separate act from requesting the change."
        ),
    )


class CreateRouteRequest(_ExposureWrite):
    pass


class UpdateRouteRequest(_ExposureWrite):
    resourceVersion: str = Field(  # noqa: N815
        ...,
        description=(
            "The resourceVersion the editor loaded. Required, not optional: rule "
            "4 makes optimistic concurrency mandatory on update, and a field "
            "that could be omitted would be omitted."
        ),
    )


class RouterOptionsRequest(BaseModel):
    """The §14 router install options."""

    model_config = ConfigDict(populate_by_name=True)

    namespace: str | None = Field(None, min_length=1, max_length=253)
    serviceType: Literal["LoadBalancer", "NodePort", "ClusterIP"] | None = None  # noqa: N815
    replicas: int | None = Field(None, ge=1, le=20)
    ingressClassName: str | None = Field(None, min_length=1, max_length=253)  # noqa: N815
    defaultClass: bool = Field(  # noqa: N815
        False,
        description=(
            "Make this the cluster's default IngressClass. The most consequential "
            "field here: it makes this router claim every Ingress in the cluster "
            "that names no class, including ones another controller is serving."
        ),
    )
    gatewayApi: bool = Field(  # noqa: N815
        False,
        description=(
            "Grant and enable the controller's Gateway API support. It implements "
            "TCPRoute only — turning this on does not make HTTPRoutes work."
        ),
    )
    image: str | None = Field(None, max_length=512)


class RouterInstallRequest(MutationBody, RouterOptionsRequest):
    pass


class RouterPlanRequest(RouterOptionsRequest):
    pass


# --------------------------------------------------------------------------- #
# §13 — exposures
# --------------------------------------------------------------------------- #

@router.get("/routes/capabilities")
def get_capabilities() -> dict[str, Any]:
    """§13 — which route backends this cluster serves, and what each can express.

    Declared before ``/routes/{backend}/...`` for readability only; they cannot
    collide, because this path has one segment after ``routes`` and those have
    three.
    """
    return routes_service.capabilities()


@router.get("/routes")
def list_routes(
    namespace: str | None = Query(
        None, description="Omit to list across all namespaces."
    ),
    backend: list[BackendKey] | None = Query(
        None,
        description=(
            "Restrict to these backends. Omit for all three. A backend named "
            "here that the cluster does not serve is reported in `backends[]` "
            "with its state, not silently dropped."
        ),
    ),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """§13 — every exposure on the cluster, from all backends, in one envelope."""
    return routes_service.list_routes(
        namespace=namespace, backends=backend, limit=limit,
    )


@router.get("/routes/{backend}/{namespace}/{name}")
def get_route(
    backend: BackendKey,
    namespace: str = Path(..., min_length=1),
    name: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """§13 — one exposure: the shaped row and the live manifest together."""
    return routes_service.get_route(backend, namespace, name)


@router.post("/routes/render")
def render_route(request: RenderRequest) -> dict[str, Any]:
    """§13 — compile an exposure into one object. **This writes nothing.**

    It is a POST because it carries a body, not because it changes anything. No
    mutations gate, no preflight, no audit row: there is nothing to preflight and
    nothing happened. The one thing it reads from the cluster is which version of
    the backend's API is served, because rendering against a guessed
    ``apiVersion`` produces a document the API server rejects for the wrong
    reason.
    """
    return routes_admin.render(
        request.backend,
        None if request.spec is None else request.spec.model_dump(by_alias=True),
        document=request.document,
    )


@router.post("/routes")
def create_route(request: CreateRouteRequest) -> dict[str, Any]:
    """§13 — create one exposure. Returns the §1.5 mutation response."""
    return routes_admin.create_route(
        request.backend,
        None if request.spec is None else request.spec.model_dump(by_alias=True),
        document=request.document,
        acknowledge_lossy=request.acknowledgeLossy,
        dry_run=request.dry_run,
    )


@router.put("/routes/{backend}/{namespace}/{name}")
def update_route(
    request: UpdateRouteRequest,
    backend: BackendKey,
    namespace: str = Path(..., min_length=1),
    name: str = Path(..., min_length=1),
) -> dict[str, Any]:
    """§13 — replace one exposure, with mandatory optimistic concurrency."""
    return routes_admin.update_route(
        backend,
        namespace,
        name,
        None if request.spec is None else request.spec.model_dump(by_alias=True),
        resource_version=request.resourceVersion,
        document=request.document,
        acknowledge_lossy=request.acknowledgeLossy,
        dry_run=request.dry_run,
    )


@router.delete("/routes/{backend}/{namespace}/{name}")
def delete_route(
    backend: BackendKey,
    namespace: str = Path(..., min_length=1),
    name: str = Path(..., min_length=1),
    dryRun: bool = Query(  # noqa: N803 - wire spelling
        True,
        description=(
            "A query parameter rather than a body, like §4's delete: DELETE "
            "bodies are handled inconsistently by proxies and HTTP clients, and "
            "a dryRun that went missing in transit would turn a projection into "
            "a hostname going out of service."
        ),
    ),
) -> dict[str, Any]:
    """§13 — delete one exposure. The diff shows the hostname that stops answering."""
    return routes_admin.delete_route(backend, namespace, name, dry_run=dryRun)


# --------------------------------------------------------------------------- #
# §14 — the shipped router
# --------------------------------------------------------------------------- #

@router.get("/router")
def get_router_status(
    namespace: str | None = Query(
        None,
        description=(
            "Where to look. Omit and the console reads it off the installed "
            "ClusterRoleBinding, falling back to ADMIN_ROUTER_NAMESPACE — so it "
            "finds a router installed into a namespace this deployment is no "
            "longer configured for."
        ),
    ),
) -> dict[str, Any]:
    """§14 — what is on the cluster right now. A live read, never cached."""
    return router_service.status(namespace)


@router.post("/router/plan")
def get_router_plan(request: RouterPlanRequest) -> dict[str, Any]:
    """§14 — the manifests that would be installed. Pure, ungated, writes nothing.

    Ungated deliberately: an operator deciding whether to set
    ``ADMIN_ROUTER_MANAGE_ENABLED`` has to be able to read what it would let the
    console create, and these manifests are a pinned copy of a public upstream
    bundle rather than anything secret.
    """
    return router_service.plan(request.model_dump(by_alias=True))


@router.post("/router")
def install_router(request: RouterInstallRequest) -> dict[str, Any]:
    """§14 — install or upgrade the shipped router.

    One endpoint for both, because there is no difference in what happens: every
    object is written to the version this console ships, each a create if absent
    and a replace if it is already ours. Returns a per-object report;
    ``installed`` is true only when every one of them landed on a real write.
    """
    return router_service.install(
        request.model_dump(by_alias=True), dry_run=request.dry_run,
    )


@router.delete("/router")
def uninstall_router(
    namespace: str | None = Query(None, min_length=1, max_length=253),
    ingressClassName: str | None = Query(None, min_length=1, max_length=253),  # noqa: N803
    dryRun: bool = Query(True),  # noqa: N803 - wire spelling
) -> dict[str, Any]:
    """§14 — remove the objects this console created.

    The Namespace is left standing and reported in ``retained``; see
    :func:`app.admin.router.uninstall` for why. Objects the console did not
    create are skipped, not deleted, and named in ``skipped``.
    """
    return router_service.uninstall(
        {"namespace": namespace, "ingressClassName": ingressClassName},
        dry_run=dryRun,
    )
