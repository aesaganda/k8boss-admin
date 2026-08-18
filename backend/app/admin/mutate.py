"""
**The single write funnel.** Everything that changes a cluster calls
:func:`mutate`, and :func:`mutate` is the only thing in this codebase that calls
an ``apply_fn``.

§0 of the contract makes four promises about every mutation: it is preflighted,
it can be dry-run, it produces a diff, and it is audited — including when it
fails. Those four could be a convention that every write endpoint follows. They
are not, because a convention followed by twelve endpoints is a convention that
nine of them follow after the next refactor, and the three that stopped are
indistinguishable from the outside. Here the four are one function with one
order, and an endpoint that skipped them would have to be written to bypass this
module in a way a reviewer can see.

The order is load-bearing, top to bottom:

1. **The mutations gate.** ``ADMIN_ALLOW_MUTATIONS=false`` refuses a real write
   *before the cluster is touched* (§1.6). A dry run is permitted in read-only
   mode — inspecting what would change is a read, and that is what makes the
   console useful in an audit posture.
2. **Preflight.** ``SelfSubjectAccessReview`` for this exact target, so a denial
   names the missing grant rather than relaying a bare "forbidden" (§0.2). It
   runs for dry runs too: the API server requires the same permission to project
   a write as to perform one, and finding out at the confirm step rather than at
   the preview step is the worse of the two.
3. **Apply.** The caller's closure, told whether this is a dry run. It is the
   only code that touches the cluster.
4. **Diff.** Live versus the API server's own projection (§1.5).
5. **Audit.** Every terminal state, not just success.

``applied`` is true only when ``dry_run`` is false *and* the call succeeded.
Nothing else in the response may be read as evidence that a cluster changed: a
successful dry run returns a full projected object, a resourceVersion and a diff,
and a UI that read those as success would report a change that did not happen.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from kubernetes.client.rest import ApiException

from app.admin import preflight
from app.admin.diff import build_diff, digest
from app.audit import recorder
from app.config import settings
from app.errors import AdminError, Conflict, MutationsDisabled, from_api_exception
from app.resources.catalog import normalize_group
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: ``apply_fn(dry_run) -> (projected_object_or_None, warnings)``.
#:
#: The object is the API server's own answer — for a create or a replace, what it
#: says the object would become; for a delete, ``None``, because §4 requires the
#: delete diff to be ``before=live, after=null``. ``warnings`` is the API
#: server's ``Warning:`` headers, verbatim (§1.5).
ApplyFn = Callable[[bool], "tuple[dict[str, Any] | None, list[str]]"]


def _target(
    group: str, version: str, plural: str, namespace: str | None, name: str | None,
    subresource: str | None,
) -> dict[str, Any]:
    """The §1.5 / §10 ``target``, with the group in its real Kubernetes spelling.

    Real spelling — the empty string for the core group — and not the ``core``
    wire name from §1.4: this dict is stored in the audit trail as the record of
    what was addressed on the API server, and ``core`` is a URL encoding that
    §1.4 says nothing downstream of the route ever sees. A UI rendering it writes
    ``group || "core"``; a UI reading ``core`` out of an audit row would have no
    way to know whether the write really targeted a group of that name.
    """
    target: dict[str, Any] = {
        "group": normalize_group(group),
        "version": version,
        "resource": plural,
        "namespace": namespace,
        "name": name,
    }
    if subresource:
        target["subresource"] = subresource
    return target


def _resource_version(after: dict[str, Any] | None, before: dict[str, Any] | None) -> str | None:
    """The resourceVersion the UI should send back on the confirming call.

    From the projection when there is one — on a dry run the API server echoes
    the *current* version, which is exactly what the confirming ``PUT`` needs to
    carry — and from the live object otherwise, which is the delete case. Null
    when neither is available rather than an empty string, because ``""`` is a
    meaningful resourceVersion to the API server ("any version"), and handing the
    UI a value that disables optimistic concurrency would silently undo rule 4.
    """
    for candidate in (after, before):
        version = get_field(candidate, "metadata", "resourceVersion")
        if version:
            return str(version)
    return None


def _audit(
    *, verb: str, target: dict[str, Any], dry_run: bool, outcome: str,
    detail: str | None, diff_digest: str | None = None, error: AdminError | None = None,
) -> int | None:
    """One audit row. Never raises — see :mod:`app.audit`."""
    return recorder.record(
        verb=verb,
        target=target,
        dry_run=dry_run,
        outcome=outcome,
        detail=detail,
        diff_digest=diff_digest,
        # code *and* message: the code is what an incident review filters on, the
        # message is what a human reads. Storing only one of them means either
        # the trail cannot be searched or it cannot be understood.
        error=None if error is None else f"{error.code}: {error.message}",
    )


def mutate(
    *,
    verb: str,
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    name: str | None,
    dry_run: bool,
    apply_fn: ApplyFn,
    before: dict[str, Any] | None = None,
    subresource: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Run one mutation through the gate, the preflight, the diff and the trail.

    Args:
        verb: the RBAC verb actually being performed (``create``, ``patch``,
            ``update``, ``delete``). Preflighted as given, so a call that names
            the wrong verb preflights the wrong permission — which is why every
            caller in :mod:`app.admin` passes the verb it is about to use rather
            than a friendly name for the action.
        before: the live object, read by the caller *before* applying. ``None``
            for a create. It is the left side of the diff and the fallback source
            of ``resourceVersion``.
        subresource: ``scale``, ``status``, … Included in the preflight (RBAC
            names ``deployments/scale`` separately from ``deployments``) and in
            the audit target.
        detail: the human sentence for the audit row — "replicas 3 -> 5". The
            diff digest proves *what* changed; this says it in a form that fits
            in a table.

    Returns:
        The §1.5 mutation response.

    Raises:
        MutationsDisabled: a real write while the console is read-only.
        RBACDenied: preflight refused.
        AdminError: anything the apply itself raised, re-raised after being
            audited. Failures are recorded before they propagate, because the
            question after an incident is "who tried", and a trail holding only
            the writes that worked cannot answer it.
    """
    target = _target(group, version, plural, namespace, name, subresource)

    # 1. The mutations gate — before the cluster is touched (§1.6).
    if not dry_run and not settings.admin_allow_mutations:
        error = MutationsDisabled(context={**target, "verb": verb})
        # Audited as a denial. Someone attempting production writes against a
        # read-only console is a fact worth keeping: either the deployment is
        # configured wrongly or the caller believes it is not read-only, and both
        # are answered by this row existing.
        _audit(verb=verb, target=target, dry_run=dry_run, outcome="denied",
               detail=detail, error=error)
        logger.warning(
            "Refused %s %s/%s: ADMIN_ALLOW_MUTATIONS is false.",
            verb, plural, name,
        )
        raise error

    # 2. Preflight (§0.2). A clean denial and a review that could not be
    #    evaluated are different errors, and preflight.require keeps them apart —
    #    both are audited, because both mean the write did not happen.
    try:
        preflight.require(
            verb, group, plural, namespace=namespace, name=name, subresource=subresource
        )
    except AdminError as e:
        _audit(verb=verb, target=target, dry_run=dry_run, outcome="denied",
               detail=detail, error=e)
        raise

    # 3. Apply. The only step that reaches the cluster.
    try:
        after, warnings = apply_fn(dry_run)
    except ApiException as e:
        # A defensive net: every caller in this package maps its own exceptions
        # so the error names the target. One that did not would otherwise reach
        # the global handler, which knows only the request path — and would
        # therefore produce an rbac_denied naming nothing.
        mapped = from_api_exception(e, context={**target, "verb": verb})
        _audit(verb=verb, target=target, dry_run=dry_run,
               outcome="conflict" if isinstance(mapped, Conflict) else "failed",
               detail=detail, error=mapped)
        raise mapped from e
    except AdminError as e:
        _audit(verb=verb, target=target, dry_run=dry_run,
               outcome="conflict" if isinstance(e, Conflict) else "failed",
               detail=detail, error=e)
        raise

    # 4. Diff, and 5. audit the success.
    diff = build_diff(before, after)
    diff_digest = digest(diff)
    outcome = "dry_run" if dry_run else "applied"
    audit_id = _audit(verb=verb, target=target, dry_run=dry_run, outcome=outcome,
                      detail=detail, diff_digest=diff_digest)

    if warnings:
        logger.info("API server warnings on %s %s/%s: %s", verb, plural, name,
                    "; ".join(warnings))

    return {
        "dryRun": dry_run,
        # True only when a real write succeeded. Derived here rather than passed
        # in, so no caller can report a dry run as applied.
        "applied": not dry_run,
        "verb": verb,
        "target": target,
        "diff": diff,
        "resourceVersion": _resource_version(after, before),
        "warnings": list(warnings or []),
        # Null when the record could not be written — which means the change is
        # real and its trail entry is missing, and the UI says so rather than
        # showing a plausible id.
        "auditId": audit_id,
    }


__all__ = ["ApplyFn", "mutate"]
