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

1. **The gate.** ``ADMIN_ALLOW_MUTATIONS=false`` refuses a real write *before
   the cluster is touched* (§1.6), and so does a feature's own switch —
   ``ADMIN_NODE_DEBUG_ENABLED`` and the three like it — which the caller hands
   in as a :class:`FeatureGate` rather than checks for itself. A dry run is
   permitted in read-only mode — inspecting what would change is a read, and
   that is what makes the console useful in an audit posture — unless the
   gate says its projection is the sensitive thing (§5.5, §15).
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

The feature switches live here for the same reason the four promises do. Five
features once carried their own copy of step one — build the error, write the
denial row, log, raise — each with its own wording, its own target and its own
answer to whether a dry run is withheld, and each a row the next refactor could
drop without a failing test. A :class:`FeatureGate` is that policy as data, and
the step that enforces it is this one function, so a sixth feature gets its
denial row by construction and the one difference that is real — which switch
withholds the preview — is stated where a reviewer can compare all five.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from kubernetes.client.rest import ApiException

from app.admin import preflight
from app.admin.diff import build_diff, digest
from app.audit import recorder
from app.config import settings
from app.errors import (
    AdminError,
    Conflict,
    MutationsDisabled,
    RBACDenied,
    from_api_exception,
)
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

#: The audit sentence, or a callable producing it when the row is written.
#:
#: A plain string for almost every write, because what will change is known
#: before the call. A callable for the one write whose true sentence is only
#: available afterwards: §5.4's drain evicts N pods *inside* its ``apply_fn``,
#: and how many the API server refused is not knowable until they have been
#: tried. A fixed sentence there records "drain node-5" with outcome ``applied``
#: over a node that drained nothing — the honesty §1.5's ``drained: false``
#: gives the response, missing from the trail that outlives it.
#:
#: It is resolved in :func:`_audit`, so it is resolved on **every** terminal
#: state including the refusals, and must therefore be safe to call before the
#: apply has run.
Detail = "str | Callable[[], str | None] | None"


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
    detail: "str | Callable[[], str | None] | None",
    diff_digest: str | None = None, error: AdminError | None = None,
) -> int | None:
    """One audit row. Never raises — see :mod:`app.audit`.

    ``detail`` is resolved here rather than at the call site, so a write whose
    sentence is only true after the apply gets the same treatment on the
    failure paths as on the success one. See :data:`Detail`.
    """
    return recorder.record(
        verb=verb,
        target=target,
        dry_run=dry_run,
        detail=detail() if callable(detail) else detail,
        outcome=outcome,
        diff_digest=diff_digest,
        # code *and* message: the code is what an incident review filters on, the
        # message is what a human reads. Storing only one of them means either
        # the trail cannot be searched or it cannot be understood.
        error=None if error is None else f"{error.code}: {error.message}",
    )


def _outcome_for(error: AdminError) -> str:
    """The §10 outcome an error *is*, decided from the error and not from the step.

    Which step raised is not the question the trail is asked. Preflight returns
    ``upstream_error`` when the ``SelfSubjectAccessReview`` could not be decided,
    and filing that as ``denied`` records "this operator was refused" where the
    truth is "we could not find out whether this operator may act" — the two
    :mod:`app.admin.preflight` exists to keep apart, collapsed again months later
    with the authority of a record, and sending somebody from the audit page to
    widen a ClusterRole that was already correct. The apply step had the mirror
    of it: an API server 403 arriving as an unmapped ``ApiException`` is a
    denial, and deciding from the exception type filed it as ``failed``.

    ``mutations_disabled`` has no branch here on purpose: :func:`_require_open`
    writes its own row and raises before step 2, so this is never asked about
    one, and a branch that can never run is a branch a reader has to disprove.
    """
    if isinstance(error, RBACDenied):
        return "denied"
    if isinstance(error, Conflict):
        return "conflict"
    return "failed"


def audit_conflict(
    *,
    verb: str,
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    name: str | None,
    dry_run: bool,
    detail: "str | Callable[[], str | None] | None",
    error: Conflict,
    subresource: str | None = None,
) -> None:
    """Rule 5's row for a rule 4 conflict raised *before* the funnel runs.

    Six writes compare the ``resourceVersion`` they were handed against the live
    one and raise ``409`` before the first :func:`mutate`, so the funnel — which
    records every other terminal state — never runs, and the trail held nothing
    to say that two operators were editing the same object during an incident.
    That is the question rule 4 exists to make answerable, asked of the trail
    that outlives the response the second operator saw.

    The row is assembled by :func:`_audit` like every other one, from request
    context: a helper that took the actor as an argument is a helper that can be
    passed the wrong one. Never raises — see :mod:`app.audit`. A failed INSERT
    must not turn a 409 into a 500, which would tell the operator the console is
    broken rather than that their edit is stale.
    """
    _audit(
        verb=verb,
        target=_target(group, version, plural, namespace, name, subresource),
        dry_run=dry_run,
        outcome="conflict",
        detail=detail,
        error=error,
    )

# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Switch:
    """One deployment setting that can refuse a write, and what it says when it does.

    ``enabled`` is read from ``settings`` by whoever builds the switch, at
    request time and never at import time. A switch that remembered the value
    it saw when the module loaded would go on refusing writes the operator has
    since allowed — or, worse, go on allowing writes they have since forbidden.

    ``withholds_dry_run`` is the one policy decision a switch carries. §1.6's
    rule is that a dry run is a read, so a closed switch still permits it: an
    operator deciding whether to open the switch has to be able to see what it
    would let the console write. Two features override that, because their
    projection *is* the sensitive thing — a node debug pod's manifest is a
    working recipe for a privileged pod (§5.5), and a CLI pod's is the offer of
    a kubectl terminal (§15) — and there the switch withholds the preview too.
    Which switch does which is stated as data, on the switch, rather than as a
    docstring on each feature's own copy of the gate; that is what makes the
    difference something a reviewer can compare across all five.
    """

    setting: str
    enabled: bool
    detail: str | None = None
    withholds_dry_run: bool = False


@dataclass(frozen=True)
class FeatureGate:
    """The switches in front of one feature's writes, evaluated at step one.

    ``switches`` is ordered: the console-wide ``ADMIN_ALLOW_MUTATIONS`` first,
    then the feature's own. A refusal names the *first* closed switch, and that
    is the one the operator has to change first — "writes are off" is a
    different conversation from "writes are on and this one thing is not", and
    the two send an operator to two different lines of the same file.

    ``message`` is the headline of the refusal and ``hint`` the sentence naming
    every setting it takes to open the gate; both are the feature's own words,
    because the error is read by someone who clicked a button, not by someone
    reading this module. ``enabled_detail`` is what :meth:`state` says when
    every switch is open.
    """

    feature: str
    switches: tuple[Switch, ...]
    message: str | None = None
    hint: str | None = None
    enabled_detail: str = "Writes are enabled on this deployment."

    def closed(self, *, dry_run: bool) -> Switch | None:
        """The switch that refuses this call, or ``None`` when it may proceed."""
        for switch in self.switches:
            if switch.enabled:
                continue
            if dry_run and not switch.withholds_dry_run:
                continue
            return switch
        return None

    def state(self) -> dict[str, Any]:
        """The ``gate`` object a feature's status endpoint returns: ``enabled`` and ``detail``.

        Reported to the UI rather than only enforced, so a button is disabled
        *with the reason* (rule 11.4) instead of offered and answered with a
        403. This answers "may I write", so it names the first closed switch
        whether or not that switch would also withhold a dry run; the dry run
        gets its own answer from the dry run.
        """
        for switch in self.switches:
            if not switch.enabled:
                return {"enabled": False, "detail": switch.detail}
        return {"enabled": True, "detail": self.enabled_detail}


def read_only_switch(
    *, detail: str | None = None, withholds_dry_run: bool = False,
) -> Switch:
    """``ADMIN_ALLOW_MUTATIONS`` as a :class:`Switch`, read now.

    Every gate starts with this one. ``detail`` is the feature's own sentence
    about what a read-only console still offers of it — the install plan and
    its diff, the projected pod — because that differs per feature, and one
    generic sentence would promise a preview that §5.5 withholds.
    """
    return Switch(
        "ADMIN_ALLOW_MUTATIONS", settings.admin_allow_mutations,
        detail=detail, withholds_dry_run=withholds_dry_run,
    )


def _default_gate() -> FeatureGate:
    """The gate every write has when its caller names none: read-only, or not."""
    return FeatureGate(feature="this write", switches=(read_only_switch(),))


def _require_open(
    gate: FeatureGate, *, verb: str, target: dict[str, Any], dry_run: bool,
    detail: "str | Callable[[], str | None] | None",
    context: dict[str, Any] | None = None,
) -> None:
    """Step one: refuse on the first closed switch, audited, before the cluster is touched.

    ``mutations_disabled`` and never ``rbac_denied``, whichever switch closed:
    the operator's permissions are irrelevant to this refusal, and telling them
    otherwise sends them to argue with a cluster admin about a ClusterRole that
    is already correct.
    """
    switch = gate.closed(dry_run=dry_run)
    if switch is None:
        return
    error = MutationsDisabled(
        gate.message, detail=switch.detail, hint=gate.hint,
        context={**target, "verb": verb, **(context or {})},
    )
    # Audited as a denial. Someone attempting a write against a console where
    # that write is switched off is a fact worth keeping: either the deployment
    # is configured wrongly or the caller believes it is not, and both are
    # answered by this row existing. Never raises — see `app.audit`.
    _audit(verb=verb, target=target, dry_run=dry_run, outcome="denied",
           detail=detail, error=error)
    logger.warning(
        "Refused %s (%s %s/%s): %s is false.",
        gate.feature, verb, target["resource"], target["name"], switch.setting,
    )
    raise error


def require_open(
    gate: FeatureGate,
    *,
    verb: str,
    group: str,
    version: str,
    plural: str,
    namespace: str | None,
    name: str | None,
    dry_run: bool,
    detail: "str | Callable[[], str | None] | None",
    subresource: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    """Step one of :func:`mutate`, hoisted for a writer that must refuse before it reads.

    §14's install reads eight objects, §16's subscribe reads a catalog and §17's
    create reads the namespace before the first ``apply_fn`` exists. For each, a
    refusal that waited for the first :func:`mutate` would arrive late: after a
    404 about a package the caller was never going to be allowed to subscribe
    to, or — for a loop over eight objects that reports each one — as eight
    denial rows and eight failed objects for one attempt. So they call this
    first, with the target the real write would have. It is the same function
    :func:`mutate` runs at step one and not a second gate: the row it writes and
    the error it raises are the ones the funnel would have written and raised.
    """
    _require_open(
        gate, verb=verb, dry_run=dry_run, detail=detail, context=context,
        target=_target(group, version, plural, namespace, name, subresource),
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
    detail: "str | Callable[[], str | None] | None" = None,
    gate: FeatureGate | None = None,
    also_requires: tuple[str, ...] = (),
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
            in a table. A callable is resolved when the row is written, for a
            write whose sentence is only true afterwards; see :data:`Detail`.
        gate: the feature's own switches, when it has any (§5.5, §14, §15, §16,
            §17). Evaluated here at step one, so the refusal is audited against
            this write's real target and a feature cannot forget the row.
            ``None`` means the write has only the console-wide switch.
        also_requires: subresources of *this* object that the write needs the
            same verb on, beyond the verb on the object itself. §13's
            ``routes/custom-host`` is the case: OpenShift gates *choosing a
            hostname* behind its own RBAC subresource, so a ServiceAccount can
            hold ``create routes``, pass the review above, and still have the
            API server refuse the write — leaving the operator told they cannot
            create Routes, a permission the review just confirmed. Checked here
            rather than by the caller so it happens **after** the gate: a
            read-only console must answer ``mutations_disabled``, not send
            somebody to fix a ClusterRole that was never the obstacle.

    Returns:
        The §1.5 mutation response.

    Raises:
        MutationsDisabled: a real write while the console is read-only, or any
            write a feature's own switch refuses.
        RBACDenied: preflight refused.
        AdminError: anything the apply itself raised, re-raised after being
            audited. Failures are recorded before they propagate, because the
            question after an incident is "who tried", and a trail holding only
            the writes that worked cannot answer it.
    """
    target = _target(group, version, plural, namespace, name, subresource)

    # 1. The gate — before the cluster is touched (§1.6). The caller's, when the
    #    feature has a switch of its own; the read-only switch alone otherwise.
    _require_open(gate or _default_gate(), verb=verb, target=target,
                  dry_run=dry_run, detail=detail)

    # 2. Preflight (§0.2). A clean denial and a review that could not be
    #    evaluated are different errors, and preflight.require keeps them apart —
    #    both are audited, because both mean the write did not happen, and
    #    `_outcome_for` keeps them apart in the row as well as in the response.
    #    A hard-coded `denied` here re-made the collapse preflight refuses, in
    #    the one place that outlives the operator who saw the 502.
    #
    #    Each `also_requires` subresource is reviewed too, and a denial there is
    #    audited against *that* subresource rather than the object: a row saying
    #    `create routes` was denied, when what was refused was
    #    `routes/custom-host`, is the misattribution this whole check exists to
    #    prevent, repeated in the trail.
    for required, audited in [
        (subresource, target),
        *(
            (extra, _target(group, version, plural, namespace, name, extra))
            for extra in also_requires
        ),
    ]:
        try:
            preflight.require(
                verb, group, plural,
                namespace=namespace, name=name, subresource=required,
            )
        except AdminError as e:
            _audit(verb=verb, target=audited, dry_run=dry_run,
                   outcome=_outcome_for(e), detail=detail, error=e)
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
               outcome=_outcome_for(mapped), detail=detail, error=mapped)
        raise mapped from e
    except AdminError as e:
        _audit(verb=verb, target=target, dry_run=dry_run,
               outcome=_outcome_for(e), detail=detail, error=e)
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


__all__ = [
    "ApplyFn",
    "Detail",
    "FeatureGate",
    "Switch",
    "audit_conflict",
    "mutate",
    "read_only_switch",
    "require_open",
]
