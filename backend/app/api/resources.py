"""
Generic resource access (§4) — the endpoint that reaches everything else.

Typed endpoints exist for the resources that need shaped rows, but a console
that can only show the resources someone wrote a page for is a console that
cannot answer "what is this operator's CRD doing". Everything the cluster serves
is reachable here, through discovery rather than a hard-coded list, so a CRD
installed five minutes ago is browsable without a release of this project.

Three decisions in this module are worth knowing before reading it:

**The core group is spelled ``core`` in URLs and ``""`` everywhere else.** §1.4.
:func:`app.resources.catalog.normalize_group` is applied once, on the way in, in
every route. Nothing below the route sees ``core``.

**List rows are typed where §8 defines a type.** ``/api/resources/core/v1/services``
returns the §8 Service row, not a raw manifest, because that is what §8 says the
UI receives when it asks. ``?shape=raw`` returns the trimmed manifest for the
callers that want one — the YAML editor, a diff, an export. Secrets are redacted
in both shapes; there is no combination of parameters that puts a Secret value in
a list response.

**Writes do not happen here.** POST, PUT and DELETE delegate to
:mod:`app.admin.apply`, which routes them through the single write funnel where
the mutations gate, the preflight, the dry-run and the audit record live. A write
implemented in this file would be a write that skipped all four.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.admin import apply as apply_service
from app.admin import preflight
from app.audit import recorder
from app.config import settings
from app.errors import ClusterUnreachable, MutationsDisabled, RBACDenied, UpstreamError
from app.resources import catalog, reader, shaping
from app.resources.envelope import collect, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["resources"])

# EndpointSlice paging for Service endpoint counts. Bounded because the count is
# a decoration on a table: spending an unbounded number of round trips on it
# would let a large cluster turn the Services page into a timeout, and a timeout
# reports nothing where a null count reports one missing column.
_SLICE_PAGE_SIZE = 500
_MAX_SLICE_PAGES = 10

#: EndpointSlices name their Service with this label. The relationship is not
#: derivable from the slice's own name, which carries a random suffix.
_SLICE_SERVICE_LABEL = "kubernetes.io/service-name"


class CreateResourceRequest(BaseModel):
    """Body of ``POST /api/resources/{group}/{version}/{plural}`` (§4)."""

    yaml: str = Field(description="The object to create, as YAML or JSON.")
    namespace: str | None = Field(
        default=None,
        description=(
            "Namespace for a namespaced resource. Ignored for cluster-scoped "
            "ones; a namespace inside the YAML wins where both are given."
        ),
    )
    dryRun: bool = Field(  # noqa: N815 - the contract spells it camelCase on the wire
        default=True,
        description=(
            "Default true per §1.5. A client that wants to write has to say so; a "
            "client that forgot the field gets a projection and a diff, not a "
            "change to a production cluster."
        ),
    )


class UpdateResourceRequest(BaseModel):
    """Body of ``PUT /api/resources/{group}/{version}/{plural}/{name}`` (§4)."""

    yaml: str = Field(description="The full replacement object, as YAML or JSON.")
    namespace: str | None = Field(default=None)
    resourceVersion: str = Field(  # noqa: N815 - wire spelling
        description=(
            "The resourceVersion the editor loaded. Required, not optional: rule 4 "
            "makes optimistic concurrency mandatory on update, and a field that "
            "could be omitted would be omitted — turning every concurrent edit "
            "into a silent last-write-wins overwrite of somebody else's change."
        ),
    )
    dryRun: bool = Field(default=True)  # noqa: N815 - wire spelling


def _tally_endpoint_slices(
    scope: str | None,
) -> dict[tuple[str | None, str | None], int]:
    """Page through EndpointSlices, returning addresses per ``(namespace, service)``.

    Raises rather than returning a short tally when the page budget runs out. A
    partial tally is the dangerous middle case: it would show a healthy Service
    as having zero backends purely because its slice fell on the far side of a
    page boundary, which is precisely the confident wrong answer an operator then
    acts on by restarting something that was fine.

    Not-ready addresses are counted. ``conditions.ready`` unset means ready per
    the EndpointSlice API, and the count is "how many endpoints back this
    Service", not "how many are passing readiness" — the second is a different
    question and would need its own column to avoid being read as the first.
    """
    tally: dict[tuple[str | None, str | None], int] = {}
    cont: str | None = None
    for page in range(_MAX_SLICE_PAGES):
        result = reader.list_resource(
            "discovery.k8s.io", "v1", "endpointslices",
            namespace=scope, limit=_SLICE_PAGE_SIZE, cont=cont,
        )
        for slice_obj in result["items"]:
            labels = shaping.get_field(slice_obj, "metadata", "labels", default={}) or {}
            service = labels.get(_SLICE_SERVICE_LABEL)
            if not service:
                continue
            key = (shaping.get_field(slice_obj, "metadata", "namespace"), service)
            addresses = sum(
                len(shaping.get_field(endpoint, "addresses", default=[]) or [])
                for endpoint in shaping.get_field(slice_obj, "endpoints", default=[]) or []
            )
            tally[key] = tally.get(key, 0) + addresses
        cont = result["continue"]
        if not cont:
            return tally
        logger.debug("EndpointSlice tally continuing past page %d", page + 1)

    raise ClusterUnreachable(
        "There are more EndpointSlices than this endpoint will page through, "
        "so endpoint counts are unknown.",
        detail=(
            f"Stopped after {_MAX_SLICE_PAGES} pages of {_SLICE_PAGE_SIZE}. "
            "Endpoint counts are omitted rather than reported from a partial tally."
        ),
        hint="Filter the listing to one namespace to get endpoint counts.",
        context={"cause": "timeout", "resource": "endpointslices"},
    )


def _endpoint_counts(
    items: list[dict[str, Any]],
    unavailable: list[dict[str, Any]],
) -> dict[tuple[str | None, str | None], int] | None:
    """The tally for the Services in ``items``, or ``None`` if it could not be made.

    ``None`` and ``{}`` are different answers and the caller depends on the
    difference. ``None`` means the EndpointSlices could not be read, so every
    row's ``endpoint_count`` is ``null`` and the reason is in ``unavailable``.
    ``{}`` means they were read and no slice matched, so every row's count is a
    genuine ``0``.

    Collapsing the two — which is what returning an empty mapping on failure
    does — makes ``null`` mean "no backends" as often as it means "we could not
    look", and §8 defines it as only the second. A headless or ExternalName
    Service has no EndpointSlice at all and really does back nothing; showing it
    as an em dash hides exactly the fact an operator opened the page to find.
    """
    if not items:
        return {}

    namespaces = {
        shaping.get_field(item, "metadata", "namespace") for item in items
    }
    # One namespace on the page means the caller filtered to it, so the slice
    # listing can be filtered too — far cheaper than a cluster-wide read whose
    # result is then thrown away for every other namespace.
    scope = namespaces.pop() if len(namespaces) == 1 else None

    counts: dict[tuple[str | None, str | None], int] | None = None
    with collect(unavailable, "discovery.k8s.io", "endpointslices", namespace=scope):
        counts = _tally_endpoint_slices(scope)
    return counts


def _shape_list(
    group: str,
    plural: str,
    items: list[dict[str, Any]],
    unavailable: list[dict[str, Any]],
    *,
    shape: str,
) -> list[dict[str, Any]]:
    """Apply the §8/§6 row shape, or return trimmed manifests for ``shape=raw``.

    Secrets are redacted on both paths. :func:`app.resources.shaping.secret_row`
    never reads a value, and ``raw`` runs the object through
    :func:`app.resources.shaping.redact_secret` — so the guarantee holds by
    construction rather than by every future caller remembering it.
    """
    is_secret = (group, plural) == ("", "secrets")
    if shape == "raw":
        return [shaping.redact_secret(item) for item in items] if is_secret else items

    shaper = shaping.shaper_for(group, plural)
    if shaper is None:
        return items
    if (group, plural) == ("", "services"):
        counts = _endpoint_counts(items, unavailable)
        return [
            shaping.service_row(
                item,
                # `counts is None` is "the slices could not be read" and every row
                # gets null; a successful tally with no entry for this Service is
                # a real zero. The default on `.get` is what carries that apart.
                endpoint_count=(
                    None
                    if counts is None
                    else counts.get(
                        (
                            shaping.get_field(item, "metadata", "namespace"),
                            shaping.get_field(item, "metadata", "name"),
                        ),
                        0,
                    )
                ),
            )
            for item in items
        ]
    return [shaper(item) for item in items]


def _secret_gate(
    obj: dict[str, Any] | None,
    *,
    namespace: str | None,
    name: str,
    reveal: bool,
) -> dict[str, Any] | None:
    """Apply §8's reveal rules to a single-object Secret read.

    Reading a Secret's values is a privileged act and is treated as one: it needs
    the deployment to have opted in, it is preflighted like a write, and it lands
    in the audit trail on every terminal state — revealed, refused by the switch,
    denied, or undecided. An audit trail that only holds successful reads cannot
    answer "did anyone try", which is the question asked after an incident, and
    the attempt worth seeing most is the one made while the feature was off.

    The gate is ``SECRET_REVEAL_ENABLED`` rather than ``ADMIN_ALLOW_MUTATIONS``.
    §8 phrases the condition as "mutations are enabled", but ``app.config``
    defines the two flags separately and says why: reading a Secret and writing a
    Deployment have different blast radii and an operator may reasonably want one
    without the other. Requiring the write flag would make the narrower, more
    conservative setting unusable.
    """
    if not reveal:
        return shaping.redact_secret(obj)

    target = {
        "group": "", "version": "v1", "resource": "secrets",
        "namespace": namespace, "name": name,
    }
    if not settings.secret_reveal_enabled:
        disabled = MutationsDisabled(
            "Revealing Secret values is disabled on this deployment.",
            hint=(
                "Set SECRET_REVEAL_ENABLED=true to allow it. Key names and sizes "
                "are available without it."
            ),
            context={**target, "verb": "get"},
        )
        # Recorded as failed rather than denied: the deployment refused, not an
        # authorizer, and a row that called this a denial sends whoever reads the
        # trail to audit a ClusterRole that was never consulted. Recorded at all
        # because "somebody kept asking for production Secret values while the
        # switch was off" is what a probe looks like, and without this row it is
        # the one attempt the trail cannot show.
        recorder.record(
            verb="get", target=target, dry_run=False, outcome="failed",
            detail="Secret values requested while SECRET_REVEAL_ENABLED is false",
            error=disabled.message,
        )
        raise disabled

    try:
        preflight.require("get", "", "secrets", namespace=namespace, name=name)
    except RBACDenied as e:
        recorder.record(
            verb="get", target=target, dry_run=False, outcome="denied",
            detail="Secret values requested", error=e.message,
        )
        raise
    except (ClusterUnreachable, UpstreamError) as e:
        # We could not establish whether the caller may read this Secret. Failed
        # rather than denied, for §9's reason: nobody refused anything, and a
        # trail that called an authorizer outage a denial reports a permissions
        # decision that was never made.
        recorder.record(
            verb="get", target=target, dry_run=False, outcome="failed",
            detail="Secret values requested; the access review could not be decided",
            error=e.message,
        )
        raise

    recorder.record(
        verb="get", target=target, dry_run=False, outcome="applied",
        detail="Secret values revealed",
    )
    return obj


@router.get("/resources/catalog")
def get_catalog() -> dict[str, Any]:
    """§4 catalog — every API resource this cluster serves.

    Declared before the ``{group}/{version}/{plural}`` routes for readability
    only; they cannot collide, because this path has one segment after
    ``resources`` and they have three.
    """
    items, unavailable = catalog.discover()
    return envelope(items, unavailable=unavailable)


@router.get("/resources/{group}/{version}/{plural}")
def list_resources(
    group: str,
    version: str,
    plural: str,
    namespace: str | None = Query(
        None, description="Namespaced resources only. Omit to list across all namespaces."
    ),
    labelSelector: str | None = Query(None),  # noqa: N803 - wire spelling
    fieldSelector: str | None = Query(None),  # noqa: N803 - wire spelling
    limit: int = Query(
        500, ge=1, le=5000,
        description=(
            "Chunk size. Bounded above because a single unbounded listing on a "
            "large cluster exceeds the API read deadline and returns nothing at "
            "all, where a bounded one returns a page and a continue token."
        ),
    ),
    cont: str | None = Query(None, alias="continue"),
    shape: Literal["auto", "raw"] = Query(
        "auto",
        description=(
            "`auto` returns the §8/§6 typed row where one is defined and the "
            "trimmed manifest otherwise; `raw` always returns the trimmed "
            "manifest. Secret values appear in neither."
        ),
    ),
) -> dict[str, Any]:
    """List any resource the cluster serves, as the §1.2 envelope."""
    normalized = catalog.normalize_group(group)
    result = reader.list_resource(
        normalized, version, plural,
        namespace=namespace, label_selector=labelSelector,
        field_selector=fieldSelector, limit=limit, cont=cont,
    )
    unavailable = list(result["unavailable"])
    rows = _shape_list(normalized, plural, result["items"], unavailable, shape=shape)
    # Rebuilt rather than mutated: `partial` is derived inside `envelope`, so an
    # entry appended by the shaping pass has to go through it to be reflected.
    return envelope(
        rows, cont=result["continue"], remaining=result["remaining"], unavailable=unavailable,
    )


@router.get("/resources/{group}/{version}/{plural}/{name}/yaml", response_class=None)
def get_resource_yaml(
    group: str,
    version: str,
    plural: str,
    name: str,
    namespace: str | None = Query(None),
    reveal: bool = Query(False, description="Secrets only; see the single-object read."),
):
    """§4 single-object read rendered as ``text/plain`` YAML, ready for the editor.

    Goes through the same Secret gate as the JSON read. A YAML endpoint that
    skipped it would be a complete bypass of §8's reveal rules — the same bytes,
    a different Content-Type.
    """
    from fastapi.responses import PlainTextResponse

    normalized = catalog.normalize_group(group)
    obj = reader.get_resource(normalized, version, plural, name, namespace=namespace)
    if (normalized, plural) == ("", "secrets"):
        obj = _secret_gate(obj, namespace=namespace, name=name, reveal=reveal)
    return PlainTextResponse(reader.to_yaml(obj), media_type="text/plain; charset=utf-8")


@router.get("/resources/{group}/{version}/{plural}/{name}")
def get_resource(
    group: str,
    version: str,
    plural: str,
    name: str,
    namespace: str | None = Query(None),
    reveal: bool = Query(
        False,
        description=(
            "Secrets only. Returns values when SECRET_REVEAL_ENABLED is set and a "
            "preflight on `get secrets` passes; the read is audited either way. "
            "Without it, each key is present with a null value — the key names "
            "are not the secret, and dropping `data` entirely would show a Secret "
            "that appears to be empty."
        ),
    ),
) -> Any:
    """§4 single-object read: the full object, minus ``managedFields``."""
    normalized = catalog.normalize_group(group)
    obj = reader.get_resource(normalized, version, plural, name, namespace=namespace)
    if (normalized, plural) == ("", "secrets"):
        return _secret_gate(obj, namespace=namespace, name=name, reveal=reveal)
    return obj


@router.post("/resources/{group}/{version}/{plural}")
def create_resource(
    group: str, version: str, plural: str, request: CreateResourceRequest,
) -> dict[str, Any]:
    """§4 create. Returns the §1.5 mutation response."""
    return apply_service.create_from_yaml(
        catalog.normalize_group(group), version, plural,
        request.namespace, request.yaml, request.dryRun,
    )


@router.put("/resources/{group}/{version}/{plural}/{name}")
def update_resource(
    group: str, version: str, plural: str, name: str, request: UpdateResourceRequest,
) -> dict[str, Any]:
    """§4 replace, with mandatory optimistic concurrency.

    A ``resourceVersion`` that no longer matches is ``409 conflict`` carrying
    ``context.currentResourceVersion`` and a fresh diff against live, so the
    editor can show what changed underneath rather than asking the operator to
    retype an edit they already made.
    """
    return apply_service.update_from_yaml(
        catalog.normalize_group(group), version, plural,
        request.namespace, name, request.yaml, request.resourceVersion, request.dryRun,
    )


@router.delete("/resources/{group}/{version}/{plural}/{name}")
def delete_resource(
    group: str,
    version: str,
    plural: str,
    name: str,
    namespace: str | None = Query(None),
    propagationPolicy: Literal["Background", "Foreground", "Orphan"] = Query(  # noqa: N803
        "Background",
        description=(
            "Passed to the API server unchanged. `Orphan` leaves the dependents "
            "behind, which is a materially different outcome from the default and "
            "is why this is explicit rather than inferred."
        ),
    ),
    dryRun: bool = Query(  # noqa: N803 - wire spelling
        True,
        description=(
            "Default true, like every other write. A delete dry-run returns the "
            "live object as `diff.before` and null as `diff.after`, so the confirm "
            "dialog shows exactly what disappears."
        ),
    ),
) -> dict[str, Any]:
    """§4 delete. Returns the §1.5 mutation response."""
    return apply_service.delete_resource(
        catalog.normalize_group(group), version, plural,
        namespace, name, propagationPolicy, dryRun,
    )
