"""
Workload endpoints (§6) — the console's flagship view.

Two reads and five writes. The reads live in ``app.services.workloads``, which
owns the unified row; the writes live in ``app.admin.*``, which owns the single
mutation funnel. This module is deliberately thin: it validates what the URL and
the body claim, then delegates. Nothing here talks to a cluster directly, and
nothing here writes — a route that built its own patch would bypass the
mutations gate, the preflight, the dry-run diff and the audit record in one
step, and every one of those is a promise the contract makes in §0.

What *is* done here is refusing an action a kind cannot have, before the request
reaches the cluster. Scaling a DaemonSet is not a permission problem and not a
cluster problem: the subresource does not exist. Forwarding it would return the
API server's own 404 on ``/scale``, which reads as "your DaemonSet is missing" —
a true-sounding answer to a question nobody asked. So the rejection is a 422
naming the kind and saying what to do instead.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.bodies import MutationBody
from app.admin.rollout import rollback_workload, rollout_history
from app.admin.scale import restart_workload, scale_workload, suspend_workload
from app.errors import Invalid
from app.services.workloads import (
    KindSpec,
    get_workload_detail,
    list_workloads,
    resolve_plural,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["workloads"])


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class ScaleRequest(MutationBody):
    replicas: int = Field(
        ...,
        ge=0,
        # A ceiling, not a policy. kubectl has none, but this field arrives from
        # a text input: a fat-fingered 50000 is a cluster-wide outage that the
        # scheduler will spend hours delivering, and no real workload of these
        # kinds is above four figures. Rejected as `invalid` with the number in
        # the message, so the operator sees what they typed.
        le=10000,
        description="Desired replica count.",
    )


class RestartRequest(MutationBody):
    """No fields beyond ``dryRun`` — a restart has no parameters."""


class SuspendRequest(MutationBody):
    suspend: bool = Field(
        ...,
        # Required, with no default. Defaulting to true would let an empty body
        # suspend a production CronJob, and defaulting to false would let one
        # resume a CronJob somebody suspended during an incident. The caller has
        # to say which.
        description="True to suspend the workload, false to resume it.",
    )


class RollbackRequest(MutationBody):
    revision: int = Field(
        ...,
        ge=1,
        # Named explicitly rather than offering "the previous one": §11.3 says no
        # button writes to a cluster without having shown a diff first, and a
        # diff against "whatever came before" is not something an operator can
        # confirm. The revision list is one GET away.
        description="The revision to roll back to, from GET .../rollout.",
    )


# --------------------------------------------------------------------------- #
# Capability rejections
# --------------------------------------------------------------------------- #
#
# Per-kind sentences rather than one generic message. "A DaemonSet cannot be
# scaled" tells an operator they are wrong; saying why, and what the equivalent
# action is, tells them what to do next — and this console's whole argument is
# that the second one is worth writing down.

_NO_SCALE: dict[str, str] = {
    "DaemonSet": (
        "A DaemonSet has no scale subresource: it runs one pod per matching node, "
        "so its size comes from node selection. Change its nodeSelector, affinity "
        "or tolerations to change where it runs."
    ),
    "Job": (
        "A Job has no scale subresource. Its size is spec.completions and "
        "spec.parallelism, which are set when it is created."
    ),
    "CronJob": (
        "A CronJob has no replicas of its own — it creates Jobs on a schedule. "
        "Suspend it to stop it creating them."
    ),
}

_NO_RESTART: dict[str, str] = {
    "Job": (
        "A Job's pod template is immutable once it is created, so there is nothing "
        "to re-roll. Create a new Job, or suspend and delete this one."
    ),
    "CronJob": (
        "A CronJob does not run pods itself. Its next Job will use the current "
        "template; restart the Jobs it has already created if you need to."
    ),
    "ReplicaSet": (
        "kubectl rollout restart does not apply to a ReplicaSet. Restart the "
        "Deployment that owns it — restarting the ReplicaSet directly is undone by "
        "the Deployment controller on its next reconcile."
    ),
}

_NO_SUSPEND: dict[str, str] = {
    "Deployment": "Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.",
    "StatefulSet": "Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.",
    "DaemonSet": (
        "Only Jobs and CronJobs have spec.suspend. To stop a DaemonSet running, "
        "change its nodeSelector so it matches no nodes."
    ),
    "ReplicaSet": "Only Jobs and CronJobs have spec.suspend. Scale this to zero instead.",
}

_NO_ROLLBACK: dict[str, str] = {
    "Job": "A Job has no revision history: it runs once with the template it was created with.",
    "CronJob": (
        "A CronJob has no revision history. The Jobs it created keep the template "
        "they were created with; edit the CronJob to change future ones."
    ),
    "ReplicaSet": (
        "A ReplicaSet is itself one revision of a Deployment. Roll back the "
        "Deployment that owns it and it will scale this ReplicaSet back up."
    ),
}


def _reject(spec: KindSpec, action: str, explanations: dict[str, str], *,
            namespace: str, name: str) -> None:
    """422 ``invalid``, naming the kind and what to do instead.

    422 and not 501: ``unsupported`` means the *cluster* does not serve the API,
    which would send an operator to look at their cluster. The cluster is fine —
    the request asked a kind for something that kind does not have.
    """
    raise Invalid(
        f"A {spec.kind} cannot be {action}.",
        detail=explanations.get(
            spec.kind, f"{spec.kind} does not support this action."
        ),
        hint=explanations.get(spec.kind),
        context={
            "kind": spec.kind,
            "group": spec.group,
            "resource": spec.plural,
            "namespace": namespace,
            "name": name,
            "action": action,
        },
    )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

@router.get("/workloads")
def get_workloads(
    namespace: str | None = Query(
        None, description="Restrict to one namespace. Omitted means every namespace."
    ),
    kind: str | None = Query(
        None,
        description=(
            "One of Deployment, StatefulSet, DaemonSet, Job, CronJob, ReplicaSet. "
            "The plural spelling is accepted too. An unknown value is rejected "
            "rather than ignored."
        ),
    ),
) -> dict:
    """Every workload of every kind, in one §1.2 envelope.

    A kind the console may not list degrades to an ``unavailable`` entry and
    leaves the other five listed, because "the cluster has no CronJobs" and "we
    are not allowed to look at CronJobs" are answers to different questions.
    """
    return list_workloads(namespace=namespace, kind=kind)


@router.get("/workloads/{plural}/{namespace}/{name}")
def get_workload(
    plural: str = Path(..., description="deployments | statefulsets | daemonsets | jobs | cronjobs | replicasets"),
    namespace: str = Path(...),
    name: str = Path(...),
) -> dict:
    """One workload with its pods, conditions, Services and rollout settings.

    Each of those is collected independently: a namespace where Services cannot
    be listed still renders the workload, with the gap named in ``unavailable``.
    """
    return get_workload_detail(plural, namespace, name)


@router.get("/workloads/{plural}/{namespace}/{name}/rollout")
def get_rollout(plural: str, namespace: str, name: str) -> dict:
    """Revision history (§6).

    Kinds with no history are not an error here — the contract has them return
    ``{"current": null, "revisions": [], "unavailable": [{"reason": "unsupported"}]}``
    so the UI can render "no rollout history for a Job" rather than a red box.
    Validating the plural first still matters: an unknown one is a 404, not an
    empty history, which would claim the object exists and has never been rolled.
    """
    resolve_plural(plural)
    return rollout_history(plural, namespace, name)


# --------------------------------------------------------------------------- #
# Writes — validated here, executed by the single funnel in app.admin.mutate
# --------------------------------------------------------------------------- #

@router.post("/workloads/{plural}/{namespace}/{name}/scale")
def scale(plural: str, namespace: str, name: str, body: ScaleRequest) -> dict:
    """Set the replica count. §1.5 mutation response."""
    spec = resolve_plural(plural)
    if not spec.scalable:
        _reject(spec, "scaled", _NO_SCALE, namespace=namespace, name=name)
    return scale_workload(plural, namespace, name, body.replicas, body.dry_run)


@router.post("/workloads/{plural}/{namespace}/{name}/restart")
def restart(plural: str, namespace: str, name: str, body: RestartRequest | None = None) -> dict:
    """Roll the pods by stamping the pod template, as ``kubectl rollout restart`` does.

    An absent body is a dry run, not a restart: the safe reading of "the client
    sent nothing" is that it asked for nothing to happen.
    """
    spec = resolve_plural(plural)
    if not spec.restartable:
        _reject(spec, "restarted", _NO_RESTART, namespace=namespace, name=name)
    body = body or RestartRequest()
    return restart_workload(plural, namespace, name, body.dry_run)


@router.post("/workloads/{plural}/{namespace}/{name}/suspend")
def suspend(plural: str, namespace: str, name: str, body: SuspendRequest) -> dict:
    """Set ``spec.suspend``. Jobs and CronJobs only."""
    spec = resolve_plural(plural)
    if not spec.suspendable:
        _reject(spec, "suspended", _NO_SUSPEND, namespace=namespace, name=name)
    return suspend_workload(plural, namespace, name, body.suspend, body.dry_run)


@router.post("/workloads/{plural}/{namespace}/{name}/rollback")
def rollback(plural: str, namespace: str, name: str, body: RollbackRequest) -> dict:
    """Restore a previous revision's pod template. §1.5 mutation response."""
    spec = resolve_plural(plural)
    if not spec.revisioned:
        _reject(spec, "rolled back", _NO_ROLLBACK, namespace=namespace, name=name)
    return rollback_workload(plural, namespace, name, body.revision, body.dry_run)
