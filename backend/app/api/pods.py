"""
The pod detail page's reads (§7.5–§7.7) and §31's scheduling answer.

Thin, like every router in this package: parse, call, return. What each of the
three refuses to say lives one layer down, in :mod:`app.services.pods`.

These sit beside :mod:`app.api.logs` and :mod:`app.api.debug`, which already own
``/api/pods/{namespace}/{name}/…`` paths. Kept in their own module because they
are *reads of the object*, while those two are streams and a write — different
failure modes, different permissions, and a router that mixed them would be the
one place a reviewer has to read the whole file to find out whether something
mutates.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Path

from app.services import pods as pods_service
from app.services import scheduling as scheduling_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["pods"])


@router.get("/pods/{namespace}/{name}")
def get_pod(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
) -> dict[str, Any]:
    """§7.5 — the §6 PodRow plus what a detail page needs and a table does not.

    Adds ``init_containers`` (which the row deliberately omits, because they
    would change what the Logs and Terminal pickers offer), ``conditions``,
    ``volumes``, the scheduling fields, and per-container ports, resources and
    ``last_terminated`` — the field that turns "restarted 14 times" into
    "OOMKilled".

    The pod is this endpoint's only read, so a failure is a §1.3 error rather
    than a partial answer: there is no honest detail page for a pod we could not
    read.
    """
    return pods_service.get_pod(namespace, name)


@router.get("/pods/{namespace}/{name}/environment")
def get_pod_environment(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
) -> dict[str, Any]:
    """§7.6 — every environment variable each container will see, and where it comes from.

    **Secret values are never returned by this endpoint**, under any setting.
    Key names are, because a key name is not the secret and hiding it would make
    the row unidentifiable; the value carries ``value_state: "withheld"`` so the
    UI says *why* it is blank rather than showing an empty variable.

    ``value_state`` is the field to branch on: ``literal``, ``resolved``,
    ``withheld``, ``unreadable`` and ``runtime``. The last two are the ones that
    keep this honest — a ConfigMap we were refused is ``unreadable`` and named in
    ``unavailable[]``, and a ``fieldRef`` the kubelet substitutes at start is
    ``runtime``, not an empty string.
    """
    return pods_service.get_pod_environment(namespace, name)


@router.get("/pods/{namespace}/{name}/metrics")
def get_pod_metrics(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
) -> dict[str, Any]:
    """§7.7 — live CPU and memory per container, against what each one asked for.

    A cluster with no ``metrics.k8s.io`` is an ordinary fact, not an error: the
    sample is a **secondary** read, so the response is a 200 whose ``usage``
    values are ``null`` with an ``unsupported`` entry in ``unavailable[]``
    naming the group. The requests and limits still render, because they come
    from the pod.

    Every unmeasured number is ``null``. A pod drawn at zero cores reads as idle,
    and idle is what gets things turned off.
    """
    return pods_service.get_pod_metrics(namespace, name)


@router.get("/pods/{namespace}/{name}/scheduling")
def get_pod_scheduling(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
) -> dict[str, Any]:
    """§31 — why is this pod Pending, and what would change it.

    `waiting_on` comes first because it splits the question in two. A Pending
    pod that already has `spec.nodeName` has been **placed**: it is waiting on
    the kubelet — an image, a volume, an init container — and node capacity is
    the wrong place to look. Only `waiting_on: "scheduler"` is a scheduling
    problem.

    `scheduler` is the scheduler's own `FailedScheduling` message, verbatim,
    with the age of the attempt that produced it. It is a **snapshot**: a node
    added since does not rewrite it. And **`null` there means no such event is
    readable right now** — events age out of etcd within the hour — never that
    the pod has not been rejected.

    `nodes[]` is this console's own re-derivation and says so: each node is
    `ruled_out` with reasons or `no_reason_found`. **There is no `fits`.** The
    scheduler weighs affinity, topology spread, volume zone, extended resources
    and every plugin the cluster runs; none of that is evaluated here, so
    "nothing ruled this node out" is the strongest honest statement and is not
    the same as "it has room". `capacity_checked: false` marks the nodes that
    were not even judged on room, because the pod listing did not answer.

    Reads only, and every one but the pod itself is secondary: a refused node
    listing costs the table, names itself in `unavailable[]`, and leaves the
    rest of the answer standing.
    """
    return scheduling_service.why_pending(namespace, name)


__all__ = [
    "get_pod",
    "get_pod_environment",
    "get_pod_metrics",
    "get_pod_scheduling",
    "router",
]
