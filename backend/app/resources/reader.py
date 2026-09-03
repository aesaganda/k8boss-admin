"""
Generic reads: list, get, trim, render as YAML (§4).

Every read resolves through :mod:`app.resources.catalog` first, so it knows
whether the resource is namespaced and which verbs it supports before it builds
a URL. That ordering costs one cached lookup and buys two things: a request for
a resource this cluster does not serve is answered ``501 unsupported`` with a
sentence about the cluster, instead of a 404 from the API server that reads like
the *object* is missing; and a namespace passed for a cluster-scoped resource is
refused rather than quietly ignored.

**Trimming is not cosmetic.** ``metadata.managedFields`` is a server-side-apply
bookkeeping structure that is routinely larger than the object it annotates —
on a Deployment reconciled by two controllers it is comfortably two thirds of
the payload — and nothing in this console renders it. It is dropped from every
response, list and single alike. ``last-applied-configuration`` is a full
serialised copy of the object embedded in its own annotations, so it doubles a
list payload; it is dropped from list responses and kept in the single-object
read, where an operator editing YAML has a legitimate reason to see what kubectl
last applied.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml
from kubernetes.client.rest import ApiException

from app.errors import Invalid, Unsupported, UpstreamError, from_api_exception
from app.resources import catalog
from app.resources.envelope import envelope

# Re-exported, not redefined. The constant moved to `shaping` because
# `redact_secret` has to strip the same annotation — a Secret applied with
# `kubectl apply` keeps a full copy of its own values in it — and two modules
# each spelling a magic string is how one of them ends up stripping a slightly
# different key.
from app.resources.shaping import LAST_APPLIED_ANNOTATION
from app.resources.transport import request_json

logger = logging.getLogger(__name__)


def resource_path(
    group: str,
    version: str,
    plural: str,
    *,
    namespace: str | None = None,
    name: str | None = None,
    subresource: str | None = None,
) -> str:
    """Build the REST path for a resource, collection or subresource.

    The core group lives under ``/api/{version}`` and every other group under
    ``/apis/{group}/{version}`` — the one structural asymmetry in the Kubernetes
    REST surface, and the reason §1.4 needs a wire spelling for a group whose
    real name is the empty string. ``group`` here is the *real* name; callers
    pass it through :func:`app.resources.catalog.normalize_group` at the edge.
    """
    q = catalog.quote_segment
    parts = [f"/api/{q(version)}" if not group else f"/apis/{q(group)}/{q(version)}"]
    if namespace:
        parts.append(f"namespaces/{q(namespace)}")
    parts.append(q(plural))
    if name:
        parts.append(q(name))
    if subresource:
        parts.append(q(subresource))
    return "/".join(parts)


def _require_verb(info: dict[str, Any], verb: str, context: dict[str, Any]) -> None:
    """Refuse a verb the resource does not advertise.

    Not an optimisation — the API server would answer 405 and
    ``from_api_exception`` would map that to ``unsupported`` anyway. It is about
    the message: "``selfsubjectaccessreviews`` cannot be listed, only created"
    is actionable, and "method not allowed" is not.

    A resource that advertises no verbs at all is not second-guessed: some
    aggregated APIs report an empty list and serve the verb regardless, and
    refusing on that basis would break a working resource on the strength of its
    own incomplete self-description.
    """
    verbs = info.get("verbs") or []
    if verbs and verb not in verbs:
        raise Unsupported(
            f"{info['resource']} does not support {verb} on this cluster.",
            detail=f"Verbs advertised by discovery: {', '.join(sorted(verbs))}.",
            context={**context, "verb": verb},
        )


def _check_scope(info: dict[str, Any], namespace: str | None, context: dict[str, Any]) -> None:
    """Reject a namespace on a cluster-scoped resource.

    Ignoring it is the tempting alternative and is a confidently wrong answer:
    the UI would show every node in the cluster under a heading that says
    "namespace: prod", and nothing in the response would contradict it.
    """
    if namespace and not info["namespaced"]:
        raise Invalid(
            f"{info['resource']} is cluster-scoped and cannot be filtered by namespace.",
            hint=f"Drop the namespace parameter to list {info['resource']}.",
            context={**context, "namespace": namespace},
        )


def trim(obj: dict[str, Any] | None, *, for_list: bool) -> dict[str, Any] | None:
    """Strip the fields §4 says never reach the client.

    Always removes ``metadata.managedFields``. With ``for_list=True`` also
    removes the ``last-applied-configuration`` annotation.

    Pure: the input is copied down to the keys that are modified, so a caller
    that trims for a list row and then wants the full object for a diff still
    has one. The alternative — mutating in place — has exactly one failure mode
    and it is a bad one: a single-object read that happened to share a dict with
    a list row would silently start returning list-trimmed content.

    The ``annotations`` key is kept even when removing ``last-applied`` empties
    it. An object whose only annotation was kubectl's copy would otherwise be
    indistinguishable in a list from one with no annotations at all, and its
    detail view — which keeps the annotation — would then show a key the list
    view said did not exist.
    """
    if not isinstance(obj, dict):
        return obj
    trimmed = dict(obj)
    metadata = trimmed.get("metadata")
    if isinstance(metadata, dict):
        metadata = dict(metadata)
        metadata.pop("managedFields", None)
        if for_list:
            annotations = metadata.get("annotations")
            if isinstance(annotations, dict) and LAST_APPLIED_ANNOTATION in annotations:
                annotations = dict(annotations)
                annotations.pop(LAST_APPLIED_ANNOTATION, None)
                metadata["annotations"] = annotations
        trimmed["metadata"] = metadata
    return trimmed


def to_yaml(obj: dict[str, Any] | None) -> str:
    """Render an object as the YAML the editor and the §1.5 diff show.

    ``None`` renders as the empty string, not as ``null``. That case is a delete
    dry-run, whose ``diff.after`` is ``None`` (§4): a unified diff against the
    literal text ``null`` would show one line being added where the object is in
    fact being removed.

    Three dump options each fix a specific annoyance:

    * ``sort_keys=False`` keeps the API server's field order
      (``apiVersion``/``kind``/``metadata``/``spec``/``status``). Alphabetical
      order puts ``status`` above ``spec`` and buries ``kind`` in the middle,
      which makes a diff far harder to read than it needs to be.
    * ``width`` is set very wide so a long image reference or container argument
      is not line-wrapped. Wrapped YAML still parses, but it breaks copy-paste
      into a shell and it produces diff hunks that move when an unrelated field
      changes length.
    * ``default_flow_style=False`` keeps everything block-style, which is what
      every Kubernetes manifest in the wild looks like.
    """
    if obj is None:
        return ""
    return yaml.safe_dump(
        obj, sort_keys=False, default_flow_style=False, allow_unicode=True, width=4096,
    )


def list_resource(
    group: str,
    version: str,
    plural: str,
    *,
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    limit: int = 500,
    cont: str | None = None,
) -> dict[str, Any]:
    """List a resource, returning the full §1.2 envelope.

    ``unavailable`` is always empty here and ``partial`` always false, which is
    deliberate rather than an omission: this endpoint makes exactly one read, so
    it either answered or it failed. A failure raises and is rendered as the
    §1.3 error envelope, which carries the ``hint`` naming the RBAC grant that
    would fix it — strictly more useful than a 200 with an empty table and a
    footnote. The ``unavailable`` mechanism is for endpoints that make several
    reads and can lose one; callers that decorate these rows with a secondary
    read (endpoint counts, pod tallies) add their own entries on top.

    ``namespace=None`` on a namespaced resource lists across all namespaces,
    which is the cluster-wide view the console's resource browser opens on.
    """
    normalized = catalog.normalize_group(group)
    info = catalog.resolve(normalized, version, plural)
    context = {
        "verb": "list", "group": normalized, "version": version,
        "resource": plural, "namespace": namespace,
    }
    _check_scope(info, namespace, context)
    _require_verb(info, "list", context)

    path = resource_path(
        normalized, version, plural,
        namespace=namespace if info["namespaced"] else None,
    )
    try:
        payload = catalog.raw_get(
            path,
            query=[
                ("labelSelector", label_selector),
                ("fieldSelector", field_selector),
                ("limit", limit),
                ("continue", cont),
            ],
        )
    except ApiException as e:
        raise from_api_exception(e, context=context) from e

    metadata = payload.get("metadata") or {}
    items = [trim(item, for_list=True) for item in (payload.get("items") or [])]
    return envelope(
        items,
        # The API server sends "" rather than omitting the key when a listing is
        # complete; the contract says null. `or None` is the whole translation.
        cont=metadata.get("continue") or None,
        # remainingItemCount is absent when the API server cannot cheaply count
        # what is left. Absent stays None: reporting 0 would say "this is the
        # last page" on a listing that has a continue token.
        remaining=metadata.get("remainingItemCount"),
    )


def get_resource(
    group: str,
    version: str,
    plural: str,
    name: str,
    *,
    namespace: str | None = None,
) -> dict[str, Any]:
    """Read one object, trimmed of ``managedFields`` only.

    Keeping ``last-applied-configuration`` here is the difference from
    :func:`list_resource`: this is the payload the YAML editor loads, and an
    operator comparing their edit against what kubectl last applied needs it.
    """
    normalized = catalog.normalize_group(group)
    info = catalog.resolve(normalized, version, plural)
    context = {
        "verb": "get", "group": normalized, "version": version,
        "resource": plural, "namespace": namespace, "name": name,
    }
    if info["namespaced"] and not namespace:
        raise Invalid(
            f"{plural} is namespaced; a namespace is required to read one by name.",
            hint="Pass ?namespace= with the object's namespace.",
            context=context,
        )
    _check_scope(info, namespace, context)
    _require_verb(info, "get", context)

    path = resource_path(
        normalized, version, plural, name=name,
        namespace=namespace if info["namespaced"] else None,
    )
    try:
        payload = catalog.raw_get(path)
    except ApiException as e:
        raise from_api_exception(e, context=context) from e
    return trim(payload, for_list=False)


def read_object(
    group: str,
    version: str,
    plural: str,
    name: str,
    *,
    namespace: str | None = None,
    subresource: str | None = None,
) -> dict[str, Any]:
    """Read the object a write is about to change, as a plain JSON dict.

    Deliberately *not* :func:`app.resources.reader.get_resource`: that path
    resolves the resource through discovery first, which is right for the generic
    browser (it must find out whether the resource exists and is namespaced) and
    wrong for the typed workload writes, where group, version and scope are
    already known from a :class:`~app.services.workloads.KindSpec`. Paying a
    discovery round trip to re-learn "apps/v1 deployments is namespaced" would
    put a cache-cold dependency in front of every scale.

    Also the only way to reach a **subresource**: ``/scale`` is not an object the
    catalog lists, and the reader has no vocabulary for it.
    """
    context = {
        "verb": "get", "group": group, "version": version, "resource": plural,
        "namespace": namespace, "name": name, "subresource": subresource,
    }
    path = resource_path(
        group, version, plural, namespace=namespace, name=name, subresource=subresource,
    )
    try:
        payload, _warnings = request_json("GET", path)
    except ApiException as e:
        raise from_api_exception(e, context=context) from e
    if not isinstance(payload, dict):
        raise UpstreamError(
            "The cluster returned something that is not a Kubernetes object.",
            detail=f"GET {path} returned {type(payload).__name__}.",
            hint="Check whether a proxy in front of the API server is answering instead of it.",
            context=context,
        )
    return payload


__all__ = [
    "LAST_APPLIED_ANNOTATION",
    "get_resource",
    "list_resource",
    "resource_path",
    "to_yaml",
    "trim",
]
