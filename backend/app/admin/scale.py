"""
Scale, restart and suspend (§6) — the three workload actions that are a patch.

All three go through :func:`app.admin.mutate.mutate` like every other write. What
lives here is the knowledge of *which* patch each action is, and the refusal to
send one to a kind that has no such thing:

* **Scale** writes ``spec.replicas`` on the ``/scale`` subresource. Not on the
  object: ``deployments/scale`` is a separate RBAC resource, so a ServiceAccount
  can be allowed to resize a Deployment without being allowed to edit it, and
  patching the object would need the broader grant for no reason. The diff is
  then a four-line Scale object rather than a whole Deployment, which is the
  clearest possible rendering of "3 becomes 5".
* **Restart** stamps ``spec.template.metadata.annotations`` with a timestamp,
  which is exactly what ``kubectl rollout restart`` does: it changes the pod
  template, the controller notices a new template, and it rolls the pods. There
  is no restart verb in the Kubernetes API — a console that offered one would
  have to invent something, and this is the mechanism operators already know.
* **Suspend** writes ``spec.suspend``, which only Jobs and CronJobs have.

A kind without the capability is refused **here** with 422 naming the kind, not
forwarded. Sending a scale to a DaemonSet returns the API server's own 404 on the
missing ``/scale`` path, which an operator reads as "my DaemonSet is gone" — a
true-sounding answer to a question nobody asked. ``app.api.workloads`` refuses the
same cases at the edge with a per-kind explanation; the check is repeated here
because this module is also reachable from the nodes lane and from anything
future, and a capability check that lives only in a route is a check that the
second caller does not get.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from app.admin.apply import patch_fn, read_object
from app.admin.mutate import mutate
from app.errors import Invalid
from app.resources.shaping import get_field
from app.services.workloads import KindSpec, resolve_plural

logger = logging.getLogger(__name__)

#: The annotation ``kubectl rollout restart`` writes is
#: ``kubectl.kubernetes.io/restartedAt``. §6 fixes ours as
#: ``k8boss-admin/restartedAt`` — deliberately a different key, so that an
#: operator reading a pod template can tell which tool rolled the workload, and
#: so this console cannot silently overwrite the timestamp kubectl left there.
RESTART_ANNOTATION = "k8boss-admin/restartedAt"

def _read(
    spec: KindSpec, namespace: str, name: str, *, subresource: str | None = None,
) -> dict[str, Any]:
    """The live object (or subresource) this patch is about to change."""
    return read_object(
        spec.group, spec.version, spec.plural, name,
        namespace=namespace, subresource=subresource,
    )


def _patch_fn(spec: KindSpec, namespace: str, name: str, body: Any, *,
              subresource: str | None = None):
    """One merge patch against this workload, as the funnel's ``apply_fn``."""
    return patch_fn(
        spec.group, spec.version, spec.plural, name, body,
        namespace=namespace, subresource=subresource,
    )


def _refuse(spec: KindSpec, action: str, explanation: str, namespace: str, name: str):
    """422 ``invalid`` naming the kind — never a relayed 404 from the API server.

    422 and not 501 ``unsupported``: ``unsupported`` means the *cluster* does not
    serve the API, which sends an operator to look at their cluster. The cluster
    is fine; the request asked a kind for something that kind does not have.
    """
    return Invalid(
        f"A {spec.kind} cannot be {action}.",
        detail=explanation,
        hint=explanation,
        context={
            "kind": spec.kind, "group": spec.group, "resource": spec.plural,
            "namespace": namespace, "name": name, "action": action,
        },
    )


# --------------------------------------------------------------------------- #
# Scale
# --------------------------------------------------------------------------- #

def scale_workload(
    plural: str, namespace: str, name: str, replicas: int, dry_run: bool,
) -> dict[str, Any]:
    """``POST /api/workloads/{plural}/{namespace}/{name}/scale`` (§6).

    The audit detail is ``replicas 3 -> 5``, read straight off the live Scale
    object rather than reconstructed from the request: the number the operator
    typed is in the request, and the number it replaced is only knowable from the
    cluster. A trail that recorded only the new value could not answer "what was
    it before the incident".
    """
    spec = resolve_plural(plural)
    if not spec.scalable:
        raise _refuse(
            spec, "scaled",
            f"A {spec.kind} has no /scale subresource, so there is no replica count to set.",
            namespace, name,
        )
    if replicas < 0:
        raise Invalid(
            f"A replica count cannot be negative (got {replicas}).",
            context={"parameter": "replicas", "value": replicas},
        )

    before = _read(spec, namespace, name, subresource="scale")
    current = get_field(before, "spec", "replicas")

    return mutate(
        verb="patch",
        group=spec.group,
        version=spec.version,
        plural=spec.plural,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        subresource="scale",
        apply_fn=_patch_fn(
            spec, namespace, name, {"spec": {"replicas": replicas}}, subresource="scale",
        ),
        before=before,
        # `current` is None only when the API server omitted spec.replicas from
        # the Scale object, which it does not do — but rendering None as 0 would
        # put "replicas 0 -> 5" in the trail for a workload that was running.
        detail=f"replicas {current if current is not None else 'unknown'} -> {replicas}",
    )


# --------------------------------------------------------------------------- #
# Restart
# --------------------------------------------------------------------------- #

def restart_workload(plural: str, namespace: str, name: str, dry_run: bool) -> dict[str, Any]:
    """``POST /api/workloads/{plural}/{namespace}/{name}/restart`` (§6).

    Stamps the pod template with the current time, which is the whole mechanism:
    the template changes, so the controller rolls the pods. The diff shows one
    added or changed annotation line, which is an honest picture of the write —
    the *consequence* is every pod being replaced, and that is what the operator
    is confirming.
    """
    spec = resolve_plural(plural)
    if not spec.restartable:
        raise _refuse(
            spec, "restarted",
            f"kubectl rollout restart does not apply to a {spec.kind}: it has no pod "
            "template of its own to re-roll.",
            namespace, name,
        )

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = _read(spec, namespace, name)

    return mutate(
        verb="patch",
        group=spec.group,
        version=spec.version,
        plural=spec.plural,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=_patch_fn(
            spec, namespace, name,
            {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: stamp}}}}},
        ),
        before=before,
        detail=f"rollout restart ({RESTART_ANNOTATION}={stamp})",
    )


# --------------------------------------------------------------------------- #
# Suspend
# --------------------------------------------------------------------------- #

def suspend_workload(
    plural: str, namespace: str, name: str, suspend: bool, dry_run: bool,
) -> dict[str, Any]:
    """``POST /api/workloads/{plural}/{namespace}/{name}/suspend`` (§6).

    Jobs and CronJobs only. ``spec.suspend`` is absent rather than false on an
    object that has never been suspended, so the before-value is normalised to
    ``False`` for the audit sentence — here that is safe, because the field's own
    default is documented as false and its absence carries no other meaning.
    """
    spec = resolve_plural(plural)
    if not spec.suspendable:
        raise _refuse(
            spec, "suspended",
            f"Only Jobs and CronJobs have spec.suspend; a {spec.kind} does not.",
            namespace, name,
        )

    before = _read(spec, namespace, name)
    current = bool(get_field(before, "spec", "suspend", default=False))

    return mutate(
        verb="patch",
        group=spec.group,
        version=spec.version,
        plural=spec.plural,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=_patch_fn(spec, namespace, name, {"spec": {"suspend": bool(suspend)}}),
        before=before,
        detail=f"suspend {str(current).lower()} -> {str(bool(suspend)).lower()}",
    )


__all__ = [
    "RESTART_ANNOTATION",
    "restart_workload",
    "scale_workload",
    "suspend_workload",
]
