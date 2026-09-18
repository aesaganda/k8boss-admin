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
from app.resources import catalog, reader, shaping
from app.resources.envelope import collect, envelope
from app.resources.shaping import namespace_row

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

    Read over the raw path rather than through the typed client, because the
    typed client's cost here is not the network: it builds a ``V1Pod`` — and
    every container, volume and status inside it — for each of the ten thousand
    pods, which is seconds of CPU spent to produce a handful of integers, all of
    it inside the API read deadline this page shares with the namespace listing.
    Two keys of each object are looked at, so the dicts the API server already
    sent are enough. The failure path survives the swap: ``raw_get`` raises the
    API server's own ``ApiException`` exactly as the typed call does, and its one
    extra failure — a body that is not a Kubernetes object, which is what a proxy
    answering in the API server's place looks like — is an ``AdminError``. The
    caller collects both, so either way ``counts`` stays ``None`` and no
    namespace is reported as holding zero pods on the strength of a read that
    did not happen.
    """
    listing = catalog.raw_get(reader.resource_path("", "v1", "pods"))
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
