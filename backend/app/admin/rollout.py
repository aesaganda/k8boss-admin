"""
Revision history and rollback (§6).

Kubernetes stores a workload's history in two entirely different places, and this
module's whole job is knowing which:

* A **Deployment**'s revisions are its ``ReplicaSet``s. Each one carries the
  revision number in the ``deployment.kubernetes.io/revision`` annotation, and
  its ``spec.template`` *is* the historical pod template. Rolling back means
  putting that template back on the Deployment.
* A **StatefulSet**'s and a **DaemonSet**'s revisions are ``ControllerRevision``
  objects. Each carries an integer ``revision`` and a ``data`` blob holding the
  patch that produced it. Rolling back means applying that patch — which is
  precisely what ``kubectl rollout undo`` does for these kinds.

**A kind with no history returns the §6 unsupported envelope, never an empty
list.** ``{"current": null, "revisions": []}`` on a Job would state that the Job
exists and has never been rolled out. It has no revision concept at all, and the
difference between "no history" and "no such thing as history here" is exactly
the difference this project refuses to blur. ``unavailable[]`` with
``reason: "unsupported"`` is §1.2's word for it, and §1.2 says the UI renders it
as "not present" rather than as an error.

**Rollback replaces the whole pod template, it does not merge into it.** A merge
patch would leave behind any container, volume or env var the old revision did
not have — producing a workload that is neither the old revision nor the new one,
reported as a successful rollback. Deployments therefore get an RFC 6902
``replace`` of ``/spec/template``; StatefulSets and DaemonSets get their
ControllerRevision's stored patch applied as the strategic merge it was built to
be.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes.client.rest import ApiException

from app.admin.apply import (
    JSON_PATCH,
    STRATEGIC_MERGE_PATCH,
    patch_fn,
    read_object,
    request_json,
)
from app.admin.mutate import mutate
from app.errors import Invalid, NotFound, UpstreamError, from_api_exception
from app.resources import reader
from app.resources.envelope import collect, unavailable_entry
from app.resources.shaping import get_field, rfc3339
from app.services.workloads import KindSpec, label_selector_string, resolve_plural

logger = logging.getLogger(__name__)

#: Where a Deployment keeps a ReplicaSet's revision number.
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

#: kubectl's ``--record`` annotation. Read, never written: this console records
#: who changed what in its own audit trail (§10), which cannot be edited by
#: whoever made the change.
CHANGE_CAUSE_ANNOTATION = "kubernetes.io/change-cause"

#: The label the Deployment controller adds to a ReplicaSet's pod template. It
#: must be stripped from a template being restored — it identifies the *old*
#: ReplicaSet, and putting it on the Deployment's template makes the controller
#: compute a hash of a template that already contains its own hash.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"

#: Bound on one history listing. History is a panel, not a report: a workload
#: with a thousand ReplicaSets (a CI pipeline deploying on every commit against
#: a high revisionHistoryLimit) would otherwise turn one page load into a listing
#: the read deadline kills, which surfaces as an unreachable cluster.
_MAX_REVISIONS = 500


def _unsupported(spec: KindSpec, namespace: str, name: str) -> dict[str, Any]:
    """The §6 shape for a kind that has no revision history at all."""
    return {
        "current": None,
        "revisions": [],
        "partial": True,
        "unavailable": [
            unavailable_entry(
                spec.group, spec.plural, "unsupported",
                detail=(
                    f"A {spec.kind} has no revision history: Kubernetes stores none for "
                    "this kind, so there is nothing to list and nothing to roll back to."
                ),
                namespace=namespace,
            )
        ],
    }


def _template_images(template: Any) -> list[str] | None:
    """Every image in a pod template, init containers included.

    ``None`` — not ``[]`` — when there is no template to read. An empty list says
    "this revision ran no containers", which is not a thing a pod template can
    be; the caller renders null as "could not be read from the stored revision".
    """
    if template is None:
        return None
    spec = get_field(template, "spec")
    if spec is None:
        return None
    images: list[str] = []
    for key in ("initContainers", "containers", "ephemeralContainers"):
        for container in get_field(spec, key, default=[]) or []:
            image = get_field(container, "image")
            # Order-preserving de-duplication: a sidecar stack reads in the order
            # it is declared, and sorting makes two revisions running the same
            # images look different.
            if image and image not in images:
                images.append(image)
    return images


def _list(group: str, version: str, plural: str, namespace: str,
          label_selector: str | None, context: dict[str, Any]) -> list[dict[str, Any]]:
    """List a revision-carrying resource, server-side filtered by the workload's selector.

    Server-side, because a namespace with a thousand ReplicaSets belonging to
    forty Deployments should not be transferred in full to render one panel. The
    selector narrows it to this workload's own; ownership is then confirmed by
    uid, because a hand-written selector can legitimately match another
    workload's pods and would otherwise put a stranger's revisions in this
    history.
    """
    path = reader.resource_path(group, version, plural, namespace=namespace)
    try:
        payload, _warnings = request_json(
            "GET", path,
            query=[("labelSelector", label_selector), ("limit", _MAX_REVISIONS)],
        )
    except ApiException as e:
        raise from_api_exception(e, context=context) from e
    if not isinstance(payload, dict):
        raise UpstreamError(
            "The cluster returned something that is not a Kubernetes list.",
            detail=f"GET {path} returned {type(payload).__name__}.",
            context=context,
        )
    return [item for item in (payload.get("items") or []) if isinstance(item, dict)]


def _owned_by(item: dict[str, Any], uid: str | None) -> bool:
    """Is this revision object owned by the workload we are looking at?"""
    if not uid:
        return False
    return any(
        str(get_field(ref, "uid") or "") == str(uid)
        for ref in (get_field(item, "metadata", "ownerReferences", default=[]) or [])
    )


def _revision_number(value: Any) -> int | None:
    """A revision as an int, or None when it is missing or not a number.

    Not coerced to 0: revision numbers start at 1, and a 0 in the list would sort
    to the bottom and read as the oldest revision rather than as one whose number
    we could not read.
    """
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        logger.debug("Unparseable revision number %r", value)
        return None


def _deployment_revisions(
    namespace: str, name: str, live: dict[str, Any],
) -> tuple[int | None, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """``(current, rows, replicaset_by_revision)`` for a Deployment."""
    uid = get_field(live, "metadata", "uid")
    selector = label_selector_string(get_field(live, "spec", "selector"))
    context = {
        "verb": "list", "group": "apps", "version": "v1", "resource": "replicasets",
        "namespace": namespace, "name": name,
    }
    replica_sets = [
        item for item in _list("apps", "v1", "replicasets", namespace, selector, context)
        if _owned_by(item, uid)
    ]

    rows: list[dict[str, Any]] = []
    by_revision: dict[int, dict[str, Any]] = {}
    for replica_set in replica_sets:
        annotations = get_field(replica_set, "metadata", "annotations", default={}) or {}
        revision = _revision_number(annotations.get(REVISION_ANNOTATION))
        if revision is None:
            # A ReplicaSet the Deployment owns but that carries no revision
            # annotation is not a revision we can offer to roll back to, and
            # listing it without a number would put an unselectable row in the
            # table. Logged rather than silently dropped.
            logger.info(
                "ReplicaSet %s/%s is owned by %s but has no %s annotation; "
                "it is not listed as a revision.",
                namespace, get_field(replica_set, "metadata", "name"), name,
                REVISION_ANNOTATION,
            )
            continue
        by_revision[revision] = replica_set
        rows.append({
            "revision": revision,
            "created": rfc3339(get_field(replica_set, "metadata", "creationTimestamp")),
            "images": _template_images(get_field(replica_set, "spec", "template")),
            "change_cause": annotations.get(CHANGE_CAUSE_ANNOTATION),
        })

    live_annotations = get_field(live, "metadata", "annotations", default={}) or {}
    current = _revision_number(live_annotations.get(REVISION_ANNOTATION))
    return current, rows, by_revision


def _controller_revisions(
    namespace: str, name: str, live: dict[str, Any],
) -> tuple[int | None, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """``(current, rows, controllerrevision_by_revision)`` for a StatefulSet or DaemonSet."""
    uid = get_field(live, "metadata", "uid")
    selector = label_selector_string(get_field(live, "spec", "selector"))
    context = {
        "verb": "list", "group": "apps", "version": "v1",
        "resource": "controllerrevisions", "namespace": namespace, "name": name,
    }
    revisions = [
        item
        for item in _list("apps", "v1", "controllerrevisions", namespace, selector, context)
        if _owned_by(item, uid)
    ]

    rows: list[dict[str, Any]] = []
    by_revision: dict[int, dict[str, Any]] = {}
    by_name: dict[str, int] = {}
    for item in revisions:
        revision = _revision_number(get_field(item, "revision"))
        if revision is None:
            continue
        annotations = get_field(item, "metadata", "annotations", default={}) or {}
        by_revision[revision] = item
        by_name[str(get_field(item, "metadata", "name") or "")] = revision
        rows.append({
            "revision": revision,
            "created": rfc3339(get_field(item, "metadata", "creationTimestamp")),
            # The stored patch, not a whole object: `data.spec.template` is
            # present for both kinds. When it is not — a revision written by a
            # controller we do not model — images is null rather than empty.
            "images": _template_images(get_field(item, "data", "spec", "template")),
            "change_cause": annotations.get(CHANGE_CAUSE_ANNOTATION),
        })

    # A StatefulSet names its current revision, by ControllerRevision *name*, in
    # status. A DaemonSet names none at all — so for it, the highest revision is
    # the current one, which is exact rather than approximate: the DaemonSet
    # controller renumbers a recurring template to the new maximum, so the
    # highest revision is always the one in effect, including immediately after a
    # rollback.
    current: int | None = None
    for field in ("updateRevision", "currentRevision"):
        pointer = get_field(live, "status", field)
        if pointer and str(pointer) in by_name:
            current = by_name[str(pointer)]
            break
    if current is None and by_revision:
        current = max(by_revision)
    return current, rows, by_revision


def _history(
    spec: KindSpec, namespace: str, name: str,
) -> tuple[dict[str, Any], int | None, list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """The live object plus its revisions. Raises rather than degrading.

    Used directly by :func:`rollback_workload`, where a revision list we could
    not read is not a degraded panel but a rollback that must not proceed —
    picking a template out of an incomplete listing is how an operator rolls back
    to the wrong revision.
    """
    live = read_object(spec.group, spec.version, spec.plural, name, namespace=namespace)
    if spec.kind == "Deployment":
        current, rows, by_revision = _deployment_revisions(namespace, name, live)
    else:
        current, rows, by_revision = _controller_revisions(namespace, name, live)
    # Newest first: the panel's first row is the revision in effect, and the one
    # below it is what a rollback would most often target.
    rows.sort(key=lambda row: row["revision"], reverse=True)
    return live, current, rows, by_revision


def rollout_history(plural: str, namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/workloads/{plural}/{namespace}/{name}/rollout`` (§6).

    Two independent facts — which revision is current, and what the revisions
    are — so a failure to list the revisions degrades that list and names the
    gap, rather than losing the current revision along with it. The workload's
    own read is *not* collected: without it there is no panel to degrade and the
    operator needs the real 403 or 404.
    """
    spec = resolve_plural(plural)
    if not spec.revisioned:
        return _unsupported(spec, namespace, name)

    unavailable: list[dict[str, Any]] = []
    current: int | None = None
    rows: list[dict[str, Any]] = []

    live = read_object(spec.group, spec.version, spec.plural, name, namespace=namespace)
    if spec.kind == "Deployment":
        current = _revision_number(
            (get_field(live, "metadata", "annotations", default={}) or {}).get(
                REVISION_ANNOTATION
            )
        )
        with collect(unavailable, "apps", "replicasets", namespace=namespace):
            current, rows, _ = _deployment_revisions(namespace, name, live)
    else:
        with collect(unavailable, "apps", "controllerrevisions", namespace=namespace):
            current, rows, _ = _controller_revisions(namespace, name, live)

    rows.sort(key=lambda row: row["revision"], reverse=True)
    return {
        "current": current,
        "revisions": rows,
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


def _rollback_patch(
    spec: KindSpec, revision: int, source: dict[str, Any],
) -> tuple[Any, str]:
    """``(body, content_type)`` that restores ``source``'s template.

    A Deployment's historical template is replaced wholesale with an RFC 6902
    ``replace`` on ``/spec/template``. A merge patch is not equivalent and the
    difference is not theoretical: merging leaves behind any container, volume or
    environment variable the old revision did not have, producing a workload that
    is neither revision and reporting it as a successful rollback.

    A StatefulSet's or DaemonSet's ControllerRevision already *is* a patch — that
    is what ``data`` holds — so it is applied as the strategic merge it was
    built as, exactly as ``kubectl rollout undo`` does.
    """
    if spec.kind == "Deployment":
        template = get_field(source, "spec", "template")
        if not isinstance(template, dict):
            raise UpstreamError(
                f"Revision {revision} has no pod template to restore.",
                detail=(
                    "The ReplicaSet holding this revision carries no spec.template, so "
                    "there is nothing to roll back to."
                ),
                context={"resource": spec.plural, "revision": revision},
            )
        restored = dict(template)
        metadata = dict(restored.get("metadata") or {})
        labels = dict(metadata.get("labels") or {})
        if labels.pop(POD_TEMPLATE_HASH_LABEL, None) is not None:
            # Left in place, the Deployment controller would hash a template that
            # already contains the previous hash, producing a *new* revision that
            # matches no existing ReplicaSet — a rollback that creates a fourth
            # revision instead of returning to the second.
            metadata["labels"] = labels
            restored["metadata"] = metadata
        return [{"op": "replace", "path": "/spec/template", "value": restored}], JSON_PATCH

    data = get_field(source, "data")
    if not isinstance(data, dict) or not data:
        raise UpstreamError(
            f"Revision {revision} has no stored patch to apply.",
            detail=(
                "The ControllerRevision holding this revision has an empty `data` field, "
                "so the state it represents cannot be reconstructed."
            ),
            context={"resource": spec.plural, "revision": revision},
        )
    return data, STRATEGIC_MERGE_PATCH


def rollback_workload(
    plural: str, namespace: str, name: str, revision: int, dry_run: bool,
) -> dict[str, Any]:
    """``POST /api/workloads/{plural}/{namespace}/{name}/rollback`` (§6).

    The diff is the live object against the projection of the restored template,
    which is what §6 asks for and what makes the confirm step meaningful: an
    operator confirming "roll back to 13" is really confirming a change of image
    tags, resource limits and environment, and the diff is where those are.

    Rolling back to the revision already in effect is not an error — it produces
    ``diff.changed: false``, and §1.5 has the UI offer "nothing would change"
    rather than a confirm button.
    """
    spec = resolve_plural(plural)
    if not spec.revisioned:
        raise Invalid(
            f"A {spec.kind} cannot be rolled back.",
            detail=(
                f"Kubernetes keeps no revision history for a {spec.kind}, so there is "
                "no earlier state to restore."
            ),
            hint="Kinds with rollout history: Deployment, StatefulSet, DaemonSet.",
            context={
                "kind": spec.kind, "group": spec.group, "resource": spec.plural,
                "namespace": namespace, "name": name, "action": "rolled back",
            },
        )

    live, current, rows, by_revision = _history(spec, namespace, name)
    source = by_revision.get(int(revision))
    if source is None:
        available = ", ".join(str(row["revision"]) for row in rows) or "none"
        raise NotFound(
            f'{spec.kind} "{name}" has no revision {revision}.',
            detail=f"Revisions held by the cluster: {available}.",
            hint=(
                "Older revisions are pruned by spec.revisionHistoryLimit; a revision "
                "that has aged out cannot be restored from the cluster."
            ),
            context={
                "group": spec.group, "resource": spec.plural, "namespace": namespace,
                "name": name, "revision": revision, "current": current,
            },
        )

    body, content_type = _rollback_patch(spec, int(revision), source)
    return mutate(
        verb="patch",
        group=spec.group,
        version=spec.version,
        plural=spec.plural,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=patch_fn(
            spec.group, spec.version, spec.plural, name, body,
            namespace=namespace, content_type=content_type,
        ),
        before=live,
        detail=f"rollback {current if current is not None else 'unknown'} -> {revision}",
    )


__all__ = [
    "CHANGE_CAUSE_ANNOTATION",
    "POD_TEMPLATE_HASH_LABEL",
    "REVISION_ANNOTATION",
    "rollback_workload",
    "rollout_history",
]
