"""
The pod detail page's three reads (§7.5–§7.7).

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


__all__ = ["get_pod", "get_pod_environment", "get_pod_metrics", "router"]
