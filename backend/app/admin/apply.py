"""
Create, replace and delete, for anything the cluster serves (§4).

These are the three generic writes behind the YAML editor and the resource
browser's delete button. Each one reads the live object, hands
:func:`app.admin.mutate.mutate` a closure that performs the write, and lets the
funnel do the gate, the preflight, the diff and the audit. Nothing here decides
whether a write is allowed; everything here decides *what* the write is.

Four things this module is responsible for getting right:

**``dryRun=All`` is a query parameter on the real call, not a separate code
path.** The projection the operator confirms is produced by the same URL, body
and content type as the write that follows it. A dry run that took a different
route through this file would eventually diverge from the real one, and the diff
shown at the confirm step would be a diff of something else.

**Optimistic concurrency is enforced twice** (§0.4). Once here, against the
object we just read — which produces a 409 carrying
``context.currentResourceVersion`` and a *fresh* diff against live, so the editor
can show what moved underneath instead of asking the operator to retype their
edit. And once by the API server, because we write the caller's
``resourceVersion`` into the submitted object: the window between our read and
our write is small, and a check that only closes it locally is a check that
loses the race it exists to detect.

**The submitted document is checked against the URL it was posted to.** A
``kind: Service`` sent to ``/apis/apps/v1/deployments`` is rejected here naming
both, rather than forwarded so the API server can answer with a schema error
about a field nobody wrote.

**``Warning:`` headers are captured, not dropped.** Deprecated API versions,
fields that will be removed, admission plugins rewriting the object — the API
server says all of it in a header that the typed clients discard. §1.5 promises
it verbatim, so the write path reads the raw response.
"""

from __future__ import annotations

import copy
import logging
import re
from typing import Any

import yaml
from kubernetes.client.rest import ApiException

from app.admin.diff import build_diff
from app.admin.mutate import mutate
from app.errors import Conflict, Invalid, Unsupported, UpstreamError, from_api_exception
from app.k8s.client import get_api_client
from app.resources import catalog, reader
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: The value the API server expects for a projected write. Not a boolean: the
#: field is a list of "dry run stages", and ``All`` is the only one defined.
DRY_RUN_ALL = "All"

#: RFC 7234 warn-header form: ``299 - "the message"``. Parsed with a regex rather
#: than split on commas because warning texts routinely *contain* commas
#: ("spec.template.spec.containers[0].resources, and 2 other fields"), and a
#: comma split turns one accurate warning into two false ones.
_WARNING_RE = re.compile(r'\d{3}\s+\S+\s+"((?:[^"\\]|\\.)*)"')

#: §4's delete propagation choices, passed to the API server unchanged.
PROPAGATION_POLICIES: tuple[str, ...] = ("Background", "Foreground", "Orphan")

#: The three patch content types this package uses, named rather than inlined
#: because the difference between them is the difference between "replace this
#: leaf" and "replace this list", and a call site that got it wrong would produce
#: a patch that applies cleanly and means something else.
#:
#: * **merge** (RFC 7386) — replaces the named leaves, leaves the rest alone.
#:   What "set replicas to 5" means.
#: * **strategic merge** — Kubernetes' own, which merges lists by their key
#:   field. What ``kubectl rollout undo`` applies for a StatefulSet or DaemonSet,
#:   because a ControllerRevision stores exactly such a patch.
#: * **json patch** (RFC 6902) — explicit operations against paths. The only one
#:   that can *replace a whole subtree*, which is what restoring a Deployment's
#:   historical pod template requires: a merge patch would leave behind any
#:   container the old revision did not have.
MERGE_PATCH = "application/merge-patch+json"
STRATEGIC_MERGE_PATCH = "application/strategic-merge-patch+json"
JSON_PATCH = "application/json-patch+json"


# --------------------------------------------------------------------------- #
# Raw REST, because the typed clients throw the response headers away
# --------------------------------------------------------------------------- #

def parse_warnings(headers: Any) -> list[str]:
    """The API server's ``Warning:`` headers, unquoted, in order.

    Returns ``[]`` when there are none — which here genuinely means "the server
    warned about nothing", not "we could not look": the headers are part of the
    same response as the object, so if we have one we have the other.

    Multiple warnings arrive either as repeated headers or as one comma-joined
    value, depending on the HTTP stack; both are handled, and a value that
    matches no warn-header form at all is returned raw rather than dropped —
    an unparseable warning is still a warning.
    """
    if headers is None:
        return []
    try:
        items = headers.items() if hasattr(headers, "items") else list(headers)
    except Exception:  # noqa: BLE001 - a stand-in with an unusual shape
        logger.debug("Could not iterate response headers of type %s", type(headers).__name__)
        return []

    warnings: list[str] = []
    for key, value in items:
        if str(key).lower() != "warning" or not value:
            continue
        matched = _WARNING_RE.findall(str(value))
        if matched:
            warnings.extend(
                text.replace('\\"', '"').replace("\\\\", "\\") for text in matched
            )
        else:
            warnings.append(str(value))
    return warnings


def request_json(
    method: str,
    path: str,
    *,
    query: list[tuple[str, Any]] | None = None,
    body: Any = None,
    content_type: str = "application/json",
) -> tuple[Any, list[str]]:
    """One authenticated write against the cluster, returning ``(body, warnings)``.

    The write-side counterpart of :func:`app.resources.catalog.raw_get`, and
    separate from it for one reason: this one asks for the response *headers*.
    ``_return_http_data_only=False`` makes ``call_api`` return
    ``(data, status, headers)``, which is the only way to reach the ``Warning:``
    values §1.5 promises to relay.

    ``response_type="object"`` matters as much here as it does on the read side:
    with ``None`` the generated client discards the body, and a create would
    return nothing to diff against — which would render as "this write changes
    nothing".
    """
    response = get_api_client().call_api(
        path,
        method,
        query_params=[(key, value) for key, value in (query or []) if value is not None],
        header_params={"Accept": "application/json", "Content-Type": content_type},
        body=body,
        response_type="object",
        auth_settings=["BearerToken"],
        _return_http_data_only=False,
    )
    # A stand-in (or a future client) that hands back only the body still works:
    # the warnings are then unknown, and an empty list is the honest rendering of
    # "this transport does not surface headers", not a claim that none were sent.
    if isinstance(response, tuple) and len(response) == 3:
        data, _status, headers = response
        return data, parse_warnings(headers)
    return response, []


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
    path = reader.resource_path(
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


def patch_fn(
    group: str,
    version: str,
    plural: str,
    name: str,
    body: Any,
    *,
    namespace: str | None = None,
    subresource: str | None = None,
    content_type: str = MERGE_PATCH,
):
    """Build the ``apply_fn`` the funnel calls: one PATCH, ``dryRun=All`` when asked.

    The projection and the real write differ by one query parameter and nothing
    else, so what the operator confirms in the dry run is produced by the very
    request that will be replayed against the cluster. Two code paths — one that
    builds a preview and one that writes — is how a console ends up showing a
    diff of something it is not about to do.
    """
    path = reader.resource_path(
        group, version, plural, namespace=namespace, name=name, subresource=subresource,
    )
    context = {
        "verb": "patch", "group": group, "version": version, "resource": plural,
        "namespace": namespace, "name": name, "subresource": subresource,
    }

    def apply_fn(is_dry_run: bool) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            patched, warnings = request_json(
                "PATCH", path,
                query=[("dryRun", DRY_RUN_ALL if is_dry_run else None)],
                body=body,
                content_type=content_type,
            )
        except ApiException as e:
            raise from_api_exception(e, context=context) from e
        return patched, warnings

    return apply_fn


def create_fn(
    group: str,
    version: str,
    plural: str,
    body: Any,
    *,
    namespace: str | None = None,
    name: str | None = None,
):
    """Build the ``apply_fn`` the funnel calls for a create: one POST.

    The counterpart of :func:`patch_fn`, and here for the same reason that one
    is: so that every create in this codebase is the *same request*, differing
    only in the body. :func:`create_from_yaml` builds its object by parsing an
    operator's document; :mod:`app.admin.node_debug` builds one in Python. Two
    hand-rolled POSTs would be two places for ``dryRun`` to be spelled, and the
    one that got it wrong would preview a write it was not about to make.

    ``name`` is for the error context only — it is not put in the path, because
    a create posts to the *collection*. It may be ``None`` for an object using
    ``generateName``, where the name is not known until the API server answers.
    """
    path = reader.resource_path(group, version, plural, namespace=namespace)
    context = {
        "verb": "create", "group": group, "version": version, "resource": plural,
        "namespace": namespace, "name": name,
    }

    def apply_fn(is_dry_run: bool) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            created, warnings = request_json(
                "POST", path,
                query=[("dryRun", DRY_RUN_ALL if is_dry_run else None)],
                body=body,
            )
        except ApiException as e:
            raise from_api_exception(e, context=context) from e
        return created, warnings

    return apply_fn


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #

def parse_document(text: str) -> dict[str, Any]:
    """Parse the editor's YAML (or JSON — YAML is a superset) into one object.

    Every failure here is a 422 with the parser's own message: an editor that
    says "invalid" without saying which line has a tab in it is an editor people
    stop using.

    A multi-document stream is refused rather than silently reduced to its first
    document. The alternative applies one object, reports success, and leaves the
    other three unwritten with nothing in the response to say so.
    """
    try:
        documents = [doc for doc in yaml.safe_load_all(text) if doc is not None]
    except yaml.YAMLError as e:
        raise Invalid(
            "The submitted document is not valid YAML.",
            detail=str(e),
            hint="Check the indentation and quoting at the position named above.",
            context={"parameter": "yaml"},
        ) from e

    if not documents:
        raise Invalid(
            "The submitted document is empty.",
            hint="Paste the object to apply, or cancel.",
            context={"parameter": "yaml"},
        )
    if len(documents) > 1:
        raise Invalid(
            f"The submitted document contains {len(documents)} objects; this endpoint applies one.",
            hint=(
                "Apply them one at a time. A partial apply reported as success is "
                "the failure this refusal exists to prevent."
            ),
            context={"parameter": "yaml", "documents": len(documents)},
        )
    document = documents[0]
    if not isinstance(document, dict):
        raise Invalid(
            "The submitted document is not a Kubernetes object.",
            detail=f"Parsed as {type(document).__name__}, expected a mapping with apiVersion and kind.",
            context={"parameter": "yaml"},
        )
    return document


def _expected_api_version(group: str, version: str) -> str:
    return version if not group else f"{group}/{version}"


def _check_document_matches_url(
    document: dict[str, Any], info: dict[str, Any], group: str, version: str, plural: str,
) -> None:
    """Refuse a document that describes something other than what the URL addresses.

    The API server would refuse it too, with a message about the fields of the
    resource *it* was asked for — which reads as "your Deployment is malformed"
    when what happened is that a Service was pasted into a Deployment's editor.
    Naming both sides costs one comparison and saves the operator the guess.
    """
    expected_version = _expected_api_version(group, version)
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    context = {
        "group": group, "version": version, "resource": plural,
        "apiVersion": api_version, "kind": kind,
    }

    if not api_version or not kind:
        raise Invalid(
            "The submitted object needs both `apiVersion` and `kind`.",
            detail=f"Expected apiVersion: {expected_version}, kind: {info['kind']}.",
            context=context,
        )
    if api_version != expected_version:
        raise Invalid(
            f"The URL addresses {expected_version} but the document declares {api_version}.",
            hint=(
                f"Post it to /api/resources/{catalog.wire_group(document_group(api_version))}"
                f"/{document_version(api_version)}/… , or change the document's apiVersion."
            ),
            context=context,
        )
    if info["kind"] and kind != info["kind"]:
        raise Invalid(
            f"The URL addresses {info['kind']} objects but the document declares kind {kind}.",
            context=context,
        )


def document_group(api_version: str) -> str:
    """``apps/v1`` -> ``apps``; ``v1`` -> ``""`` (the core group)."""
    return api_version.rsplit("/", 1)[0] if "/" in api_version else ""


def document_version(api_version: str) -> str:
    """``apps/v1`` -> ``v1``; ``v1`` -> ``v1``."""
    return api_version.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- #
# Resource resolution
# --------------------------------------------------------------------------- #

def _require_verb(info: dict[str, Any], verb: str, context: dict[str, Any]) -> None:
    """Refuse a verb discovery says the resource does not have.

    Same reasoning as :func:`app.resources.reader._require_verb`, restated for
    the write side: the API server would answer 405 and that would map to
    ``unsupported`` anyway, but "bindings cannot be deleted, only created" is
    actionable where "method not allowed" is not. A resource advertising no verbs
    at all is not second-guessed — some aggregated APIs report an empty list and
    serve the verb regardless.
    """
    verbs = info.get("verbs") or []
    if verbs and verb not in verbs:
        raise Unsupported(
            f"{info['resource']} does not support {verb} on this cluster.",
            detail=f"Verbs advertised by discovery: {', '.join(sorted(verbs))}.",
            context={**context, "verb": verb},
        )


def _resolve_namespace(
    info: dict[str, Any],
    document_namespace: str | None,
    request_namespace: str | None,
    context: dict[str, Any],
) -> str | None:
    """Decide which namespace this write targets, or refuse to guess.

    A namespace inside the document wins over the one on the request: the
    document is what the operator wrote, and the request's namespace is often
    just whichever namespace the browser happened to be filtered to.

    For a cluster-scoped resource the request's namespace is *ignored* (again:
    the browser sends its current filter on everything) but one inside the
    document is a refusal. A ClusterRole with ``metadata.namespace: prod`` is a
    misunderstanding, the API server silently drops the field, and the operator
    goes on believing they created something namespaced.
    """
    if not info["namespaced"]:
        if document_namespace:
            raise Invalid(
                f"{info['resource']} is cluster-scoped, but the document sets "
                f'metadata.namespace: "{document_namespace}".',
                hint=(
                    "Remove metadata.namespace. The API server would ignore it, "
                    "leaving an object that is not in the namespace you named."
                ),
                context={**context, "namespace": document_namespace},
            )
        return None

    namespace = document_namespace or request_namespace
    if not namespace:
        raise Invalid(
            f"{info['resource']} is namespaced; this write needs a namespace.",
            hint="Set metadata.namespace in the document, or pass `namespace` in the request.",
            context=context,
        )
    return namespace


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #

def create_from_yaml(
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    yaml_text: str,
    dry_run: bool,
) -> dict[str, Any]:
    """``POST /api/resources/{group}/{version}/{plural}`` (§4).

    ``before`` is ``None`` — there is nothing live to diff against — so the
    unified diff shows the whole projected object as an addition, which is
    exactly what a create is.
    """
    normalized = catalog.normalize_group(group)
    info = catalog.resolve(normalized, version, plural)
    context = {"verb": "create", "group": normalized, "version": version, "resource": plural}
    _require_verb(info, "create", context)

    document = parse_document(yaml_text)
    _check_document_matches_url(document, info, normalized, version, plural)

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    target_namespace = _resolve_namespace(
        info, metadata.get("namespace"), namespace, context,
    )
    name = metadata.get("name") or None
    if not name and not metadata.get("generateName"):
        raise Invalid(
            "The submitted object has neither metadata.name nor metadata.generateName.",
            hint="Give it a name, or set generateName to let the API server pick one.",
            context=context,
        )

    body = copy.deepcopy(document)
    if target_namespace:
        # Written into the body as well as the URL. The API server rejects a
        # mismatch, and relying on the URL alone would let a document that names
        # a different namespace be created somewhere the operator did not read.
        body.setdefault("metadata", {})["namespace"] = target_namespace

    return mutate(
        verb="create",
        group=normalized,
        version=version,
        plural=plural,
        namespace=target_namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=create_fn(
            normalized, version, plural, body,
            namespace=target_namespace, name=name,
        ),
        before=None,
        detail=f"create {info['kind'] or plural} {name or '(generated name)'}",
    )


# --------------------------------------------------------------------------- #
# Replace
# --------------------------------------------------------------------------- #

def update_from_yaml(
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    name: str,
    yaml_text: str,
    resource_version: str,
    dry_run: bool,
) -> dict[str, Any]:
    """``PUT /api/resources/{group}/{version}/{plural}/{name}`` (§4).

    The live object is read first and used for three things: the left side of the
    diff, the optimistic-concurrency comparison, and — when that comparison fails
    — the *fresh* diff carried on the 409, which is what lets the editor show the
    operator what changed underneath their edit.

    The read is not wrapped in ``collect``: if we cannot see the object we are
    about to replace, there is no safe way to continue. A blind ``PUT`` is the
    one thing rule 4 exists to forbid.
    """
    normalized = catalog.normalize_group(group)
    info = catalog.resolve(normalized, version, plural)
    context = {
        "verb": "update", "group": normalized, "version": version,
        "resource": plural, "name": name,
    }
    _require_verb(info, "update", context)

    document = parse_document(yaml_text)
    _check_document_matches_url(document, info, normalized, version, plural)

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    document_name = metadata.get("name")
    if document_name and document_name != name:
        raise Invalid(
            f'The URL names "{name}" but the document names "{document_name}".',
            hint=(
                "A replace cannot rename an object. Change the document's "
                "metadata.name back, or create the new object and delete the old one."
            ),
            context={**context, "documentName": document_name},
        )
    target_namespace = _resolve_namespace(
        info, metadata.get("namespace"), namespace, {**context, "verb": "update"},
    )

    live = reader.get_resource(normalized, version, plural, name, namespace=target_namespace)
    current_version = get_field(live, "metadata", "resourceVersion")

    body = copy.deepcopy(document)
    body_metadata = body.setdefault("metadata", {})
    body_metadata["name"] = name
    if target_namespace:
        body_metadata["namespace"] = target_namespace
    # The caller's resourceVersion, not the one we just read. The API server then
    # enforces the same check we do below, which closes the window between our
    # read and our write — a window a purely local check loses the race in.
    body_metadata["resourceVersion"] = resource_version

    path = reader.resource_path(
        normalized, version, plural, namespace=target_namespace, name=name,
    )

    def apply_fn(is_dry_run: bool) -> tuple[dict[str, Any] | None, list[str]]:
        # Inside the closure, so the funnel audits the conflict as an attempted
        # write (§10 outcome `conflict`) rather than letting it escape unrecorded
        # from a pre-check. "Someone tried to save a stale edit to prod" is a
        # fact the trail should hold.
        if str(current_version or "") != str(resource_version or ""):
            fresh = build_diff(live, document)
            raise Conflict(
                f'"{name}" changed since it was loaded.',
                detail=(
                    f"The editor holds resourceVersion {resource_version}; the "
                    f"cluster now has {current_version}."
                ),
                hint=(
                    "Reload the object and reapply the change. The `diff` in this "
                    "response is your submitted document against the current live one."
                ),
                context={
                    **context,
                    "namespace": target_namespace,
                    "currentResourceVersion": current_version,
                    "submittedResourceVersion": resource_version,
                    "diff": fresh,
                },
            )
        try:
            updated, warnings = request_json(
                "PUT", path,
                query=[("dryRun", DRY_RUN_ALL if is_dry_run else None)],
                body=body,
            )
        except ApiException as e:
            raise from_api_exception(
                e, context={**context, "namespace": target_namespace},
            ) from e
        return updated, warnings

    return mutate(
        verb="update",
        group=normalized,
        version=version,
        plural=plural,
        namespace=target_namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=apply_fn,
        before=live,
        detail=f"replace {info['kind'] or plural} {name}",
    )


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #

def delete_resource(
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    name: str,
    propagation_policy: str,
    dry_run: bool,
) -> dict[str, Any]:
    """``DELETE /api/resources/{group}/{version}/{plural}/{name}`` (§4).

    The live object is read first so the diff can be ``before=live, after=null``:
    a confirm dialog for a delete has one job, which is to show exactly what
    disappears. A delete that could not read its target first would offer the
    operator a confirmation of nothing.
    """
    if propagation_policy not in PROPAGATION_POLICIES:
        raise Invalid(
            f"{propagation_policy!r} is not a deletion propagation policy.",
            hint="Use one of: " + ", ".join(PROPAGATION_POLICIES) + ".",
            context={"parameter": "propagationPolicy", "value": propagation_policy},
        )

    normalized = catalog.normalize_group(group)
    info = catalog.resolve(normalized, version, plural)
    context = {
        "verb": "delete", "group": normalized, "version": version,
        "resource": plural, "name": name,
    }
    _require_verb(info, "delete", context)

    target_namespace = _resolve_namespace(info, None, namespace, context)
    live = reader.get_resource(normalized, version, plural, name, namespace=target_namespace)
    path = reader.resource_path(
        normalized, version, plural, namespace=target_namespace, name=name,
    )

    def apply_fn(is_dry_run: bool) -> tuple[dict[str, Any] | None, list[str]]:
        try:
            _status, warnings = request_json(
                "DELETE", path,
                query=[
                    ("propagationPolicy", propagation_policy),
                    ("dryRun", DRY_RUN_ALL if is_dry_run else None),
                ],
            )
        except ApiException as e:
            raise from_api_exception(
                e, context={**context, "namespace": target_namespace},
            ) from e
        # The API server answers a delete with either a Status or the deleted
        # object; neither is the "after" state. §4 fixes that as null, so the
        # diff reads as a removal rather than as a replacement by a Status.
        return None, warnings

    return mutate(
        verb="delete",
        group=normalized,
        version=version,
        plural=plural,
        namespace=target_namespace,
        name=name,
        dry_run=dry_run,
        apply_fn=apply_fn,
        before=live,
        detail=f"delete {info['kind'] or plural} {name} (propagation: {propagation_policy})",
    )


__all__ = [
    "DRY_RUN_ALL",
    "JSON_PATCH",
    "MERGE_PATCH",
    "PROPAGATION_POLICIES",
    "STRATEGIC_MERGE_PATCH",
    "create_fn",
    "create_from_yaml",
    "delete_resource",
    "parse_document",
    "parse_warnings",
    "patch_fn",
    "read_object",
    "request_json",
    "update_from_yaml",
]
