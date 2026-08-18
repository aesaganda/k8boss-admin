"""
Namespaces (§5) — the list every other page's filter is built from.

One route, two reads, and the whole file exists for the difference between them.

The namespace listing is the endpoint's **primary** read: if it fails there is no
answer to give, so it raises and ``app.api.exception_handlers`` renders the §1.3
envelope with the hint naming the missing grant. The pod tally is **secondary**:
losing it costs one column, not the page, so it is collected and every row's
``pod_count`` becomes ``None`` while the reason lands in ``unavailable[]``.

``pod_count`` is the field this module is careful about. §5 says it is ``null``,
not ``0``, when pods could not be listed, and the reason is that the two states
are acted on in opposite directions: a namespace showing ``0`` pods is the one an
operator deletes during a cleanup, and "we were refused the pod listing" must
never render as "there is nothing in here". A genuinely empty namespace still
reports ``0`` — that is a real zero, and flattening it to ``null`` would be the
same lie pointing the other way.

One cluster-wide pod listing, not one per namespace. A cluster with three hundred
namespaces would otherwise make three hundred round trips to fill in a single
column, and the API read deadline fires long before the last of them.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Iterator

from fastapi import APIRouter
from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import get_core_v1
from app.resources import shaping
from app.resources.envelope import collect, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["namespaces"])


@contextlib.contextmanager
def _api_errors(**context: Any) -> Iterator[None]:
    """Turn an ``ApiException`` raised inside the block into a typed AdminError.

    The context is the point: an ``rbac_denied`` that does not name the verb and
    the resource sends an operator to read a ClusterRole line by line. Nested
    inside :func:`~app.resources.envelope.collect` it is also what gives the
    ``unavailable`` entry ``forbidden`` rather than the catch-all ``unreachable``,
    because ``collect`` reads the reason off the mapped error class.
    """
    try:
        yield
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


def _pod_counts() -> dict[str, int]:
    """Pods per namespace, from one cluster-wide listing.

    Every pod counts, terminated ones included. That is deliberately *not* what
    :mod:`app.services.nodes` does: there the number is compared against a node's
    ``capacity.pods`` and a completed Job pod holds no slot, so terminated pods
    are excluded. Here the number answers "what is in this namespace", which is
    the question ``kubectl get pods -n x`` answers, and it includes the four
    thousand ``Completed`` pods a CronJob left behind — that pile is usually the
    reason someone opened this page.
    """
    listing = get_core_v1().list_pod_for_all_namespaces()
    counts: dict[str, int] = {}
    for pod in shaping.get_field(listing, "items", default=[]) or []:
        namespace = shaping.get_field(pod, "metadata", "namespace")
        if not namespace:
            # A pod with no namespace cannot be attributed to one. Dropping it is
            # the only honest option: adding it to a "" bucket would either
            # vanish silently or, worse, be matched by a namespace whose name is
            # falsy in some future refactor.
            continue
        key = str(namespace)
        counts[key] = counts.get(key, 0) + 1
    return counts


def namespace_row(namespace: Any, *, pod_count: int | None) -> dict[str, Any]:
    """The §5 namespace row.

    ``status`` is ``status.phase`` — ``Active`` or ``Terminating`` — with one
    correction. A namespace whose deletion has been accepted carries a
    ``deletionTimestamp`` and its phase is set to ``Terminating`` by the
    namespace controller, but the two are written by different actors and there
    is a window where the timestamp is set and the phase still says ``Active``.
    A namespace reported as ``Active`` while it is being torn down is the row an
    operator deploys into, and then spends an afternoon working out why the
    Deployment they created disappeared. The timestamp wins.

    ``labels`` and ``annotations`` are always dicts, never ``None``, so the
    frontend can call ``Object.entries`` on them without a guard.
    """
    phase = shaping.get_field(namespace, "status", "phase")
    deletion = shaping.get_field(namespace, "metadata", "deletionTimestamp")
    status = "Terminating" if deletion else phase

    created = shaping.get_field(namespace, "metadata", "creationTimestamp")
    return {
        "name": shaping.get_field(namespace, "metadata", "name"),
        "status": status,
        "labels": dict(shaping.get_field(namespace, "metadata", "labels", default={}) or {}),
        "annotations": dict(
            shaping.get_field(namespace, "metadata", "annotations", default={}) or {}
        ),
        "age_seconds": shaping.age_seconds(created),
        "pod_count": pod_count,
        "creationTimestamp": shaping.rfc3339(created),
    }


@router.get("/namespaces")
def list_namespaces() -> dict[str, Any]:
    """§5 namespace listing, as the §1.2 envelope.

    ``pod_count`` is ``None`` on every row when the pod listing failed, with the
    reason in ``unavailable[]`` and ``partial: true``. It is never ``0`` in that
    case — see the module docstring.
    """
    unavailable: list[dict[str, Any]] = []

    with _api_errors(verb="list", group="", resource="namespaces"):
        listing = get_core_v1().list_namespace()
        namespaces = list(shaping.get_field(listing, "items", default=[]) or [])

    # None, not {}: the empty dict is what a cluster with no pods produces, and
    # `counts.get(name, 0)` on it would report every namespace as holding zero.
    # Leaving the variable at None is what makes the failure survive into the row.
    counts: dict[str, int] | None = None
    with collect(unavailable, "", "pods"), _api_errors(
        verb="list", group="", resource="pods"
    ):
        counts = _pod_counts()

    rows = [
        namespace_row(
            namespace,
            pod_count=(
                None
                if counts is None
                else counts.get(str(shaping.get_field(namespace, "metadata", "name")), 0)
            ),
        )
        for namespace in namespaces
    ]

    # Sorted by name: the API server does not guarantee a stable order between
    # two identical listings, and a namespace picker that reshuffles under the
    # cursor gets the wrong namespace selected.
    rows.sort(key=lambda row: row["name"] or "")
    return envelope(rows, unavailable=unavailable)


__all__ = ["list_namespaces", "namespace_row", "router"]
