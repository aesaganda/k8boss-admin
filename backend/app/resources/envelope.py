"""
The §1.2 list envelope, and the mechanism that keeps it honest.

Rule 1 of the contract is *empty is never blind*: ``items: []`` means the cluster
has none of the thing, and a read that could not happen is reported in
``unavailable[]`` with ``partial: true``. Stating that rule is easy; the failure
mode is that a busy handler writes ``except ApiException: return []`` and the
page renders a confident, empty, wrong answer.

So the rule is mechanical here rather than editorial:

* :func:`envelope` **computes** ``partial`` from ``unavailable``. There is no
  parameter for it, so no caller can set ``partial: false`` next to a populated
  ``unavailable`` list, and none can forget to set it to true.
* :func:`collect` is a context manager that swallows exactly the two failure
  types a Kubernetes read produces and turns each into an ``unavailable`` entry.
  A handler writes the read inside the ``with`` block and leaves the variable it
  was assigning at ``None`` — so the "could not look" case falls out of the
  control flow instead of needing to be remembered.

The division of labour with the error envelope (§1.3) is worth stating once,
because both are ways of reporting a failed read:

* The **primary** read of an endpoint failing means the endpoint failed. It
  raises, and ``app.api.exception_handlers`` renders §1.3 with the ``hint`` that
  names the missing RBAC grant. ``GET /api/resources/core/v1/secrets`` with no
  permission is a 403, not an empty table with a footnote.
* Every **secondary** read — the pod tally next to a namespace, the endpoint
  count next to a Service, one group of a multi-group discovery — is collected.
  Losing one must not lose the rest, and the rest must not pretend the lost one
  was empty.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

from kubernetes.client.rest import ApiException

from app.errors import AdminError, from_api_exception

logger = logging.getLogger(__name__)

#: The closed vocabulary of §1.2 ``unavailable[].reason``. Callers branch on
#: these, so the set is fixed by the contract and adding to it is a contract
#: change, not an implementation detail.
UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "forbidden",
        "not_found",
        "unreachable",
        "timeout",
        "not_registered",
        "unsupported",
        # The read was never attempted, because this console could not build the
        # query that would answer it — §6's workload detail meets this when a
        # LabelSelector uses an operator it cannot render, or selects
        # everything. Distinct from `unsupported`, which says the *cluster* does
        # not serve something and is rendered as an ordinary fact; this one is a
        # limit of the console and the section is genuinely unknown.
        "unrenderable",
    }
)

# HTTP statuses that mean "the API server ran out of time", as distinct from
# "the API server could not be reached". 504 comes from an aggregation layer or
# a proxy in front of the API server; 408 from the API server itself. Both are
# reported as `timeout` because the operator's next move differs from an
# unreachable endpoint: retry or narrow the query, rather than check DNS, the CA
# and the network path.
_TIMEOUT_STATUSES = frozenset({408, 504})

#: Placeholder used where a whole group's resources could not be enumerated and
#: so no individual resource name is known. Reads as "everything under here".
ALL_RESOURCES = "*"


@dataclass
class Collected:
    """What a :func:`collect` block did, for callers that need to branch on it.

    Most callers do not: they leave their result variable at its ``None`` initial
    value and that is the whole story. This exists for the ones that want to say
    something specific afterwards — "we saw 12 of 14 namespaces" — without
    re-deriving it from the sink, which would also pick up entries other blocks
    recorded.
    """

    failed: bool = False
    entry: dict[str, Any] | None = None
    error: AdminError | None = None


def unavailable_entry(
    group: str,
    resource: str,
    reason: str,
    detail: str | None = None,
    namespace: str | None = None,
) -> dict[str, Any]:
    """One §1.2 ``unavailable[]`` record.

    ``group`` is the *real* Kubernetes group name — the empty string for the core
    group, never the ``core`` wire spelling from §1.4, which exists only because
    an empty string cannot be a URL path segment.

    ``reason`` is validated against :data:`UNAVAILABLE_REASONS` rather than
    coerced to a default, because the frontend branches on it: an unrecognised
    token would render as the catch-all and quietly downgrade, say, a *forbidden*
    into "the cluster is unreachable" — sending the operator to debug their
    network instead of their RBAC. Every caller passes either a literal from this
    module or the output of :func:`reason_for_error`, so this raising is a
    programming-error guard, not a runtime path.

    Every key is always present, ``detail`` and ``namespace`` as ``None`` where
    they do not apply, so the frontend can read ``entry.namespace``
    unconditionally.
    """
    if reason not in UNAVAILABLE_REASONS:
        raise ValueError(
            f"{reason!r} is not a §1.2 unavailable reason. "
            f"Use one of: {', '.join(sorted(UNAVAILABLE_REASONS))}."
        )
    return {
        "group": group,
        "resource": resource,
        "namespace": namespace,
        "reason": reason,
        "detail": detail,
    }


def envelope(
    items: Iterable[Any],
    *,
    cont: str | None = None,
    remaining: int | None = None,
    unavailable: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the §1.2 collection envelope.

    ``partial`` is derived, never passed: it is true if and only if
    ``unavailable`` is non-empty. That invariant is what the frontend's
    persistent "some of this could not be read" banner hangs off (§11.1), and a
    handler that could set it independently would eventually set it wrong — most
    likely by appending to ``unavailable`` after building the envelope.

    ``items`` is materialised with ``list()`` so a generator that raises halfway
    fails *here*, inside the handler that can still report it, rather than during
    JSON serialisation, where the response has already begun and the error
    escapes as a truncated body.
    """
    entries = list(unavailable or [])
    return {
        "items": list(items),
        "continue": cont or None,
        "remaining": remaining,
        "partial": bool(entries),
        "unavailable": entries,
    }


def reason_for_error(error: AdminError) -> str | None:
    """Map an :class:`~app.errors.AdminError` to its §1.2 reason token.

    Mostly delegates to ``AdminError.unavailable_reason``, with one refinement
    the error class itself cannot make: ``ClusterUnreachable`` covers both "the
    endpoint did not answer" and "the endpoint answered too slowly", and
    ``app.k8s.client`` distinguishes them by stamping ``context["cause"]``. The
    §1.2 vocabulary has separate ``unreachable`` and ``timeout`` tokens precisely
    so the UI can tell an operator to check the network in one case and to narrow
    their query in the other; collapsing them here would throw away a distinction
    the transport layer went to the trouble of making.

    ``None`` when the error is not a statement about whether we could read —
    see :attr:`app.errors.AdminError.unavailable_reason`. A caller holding a
    ``None`` must propagate the error rather than invent a token for it.
    """
    if error.context.get("cause") == "timeout":
        return "timeout"
    return error.unavailable_reason


@contextlib.contextmanager
def collect(
    sink: list[dict[str, Any]],
    group: str,
    resource: str,
    *,
    namespace: str | None = None,
) -> Iterator[Collected]:
    """Run an optional read; record its failure in ``sink`` instead of raising.

    Usage is deliberately shaped so the honest answer is the lazy one::

        pod_count = None                                   # "we do not know"
        with collect(unavailable, "", "pods", namespace=name):
            pod_count = len(core.list_namespaced_pod(name).items)

    If the listing fails, ``pod_count`` is still ``None`` — which §5 requires,
    and which renders as an em dash rather than as ``0``. Writing this with
    ``try/except: pod_count = 0`` is the bug: an idle-looking namespace and an
    unreadable one become the same row.

    **What is caught, and what is not.** ``AdminError`` (which is what
    ``app.k8s.client`` raises for a DNS, TLS or deadline failure) and
    ``ApiException`` (which is what the API server's own 4xx/5xx become) are
    recorded and suppressed: those are statements about the *cluster*, and the
    rest of the response is still worth returning. Anything else — a
    ``TypeError`` from a shaper, a ``KeyError`` from a rename — propagates,
    because recording a bug in this process as "the cluster was unreachable" is
    the same class of confidently wrong answer this whole module exists to
    prevent, pointed at the wrong system.

    **An ``AdminError`` that is not a statement about availability propagates
    too**, and for exactly that reason. ``invalid`` and ``conflict`` mean the
    cluster answered and the request this console built was wrong; §12's
    authentication codes are about the person signed in, not a cluster read.
    None of them can honestly be rendered as "we could not look at this", so
    :func:`reason_for_error` returns ``None`` for them and this re-raises rather
    than picking the nearest token. The previous behaviour — a fallback to
    ``unreachable`` — turned every such bug into a degraded column with a
    sentence about somebody's network in it, which is where a bug of that shape
    lives forever.

    Yields a :class:`Collected` describing what happened, for the callers that
    need to say something about it beyond leaving a ``None`` behind.
    """
    state = Collected()
    context = {"group": group, "resource": resource, "namespace": namespace}
    try:
        yield state
    except AdminError as e:
        reason = reason_for_error(e)
        if reason is None:
            # Not a statement about whether we could read. See the docstring:
            # rendering it as one would hide a defect in this process behind a
            # sentence about the cluster.
            raise
        state.failed = True
        state.error = e
        state.entry = unavailable_entry(
            group, resource, reason, detail=e.detail or e.message,
            namespace=namespace,
        )
        sink.append(state.entry)
        logger.warning(
            "Partial read: %s/%s%s could not be read (%s): %s",
            group or "core", resource,
            f" in {namespace}" if namespace else "",
            state.entry["reason"], e.message,
            extra={"error_code": e.code, "context": {**context, **e.context}},
        )
    except ApiException as e:
        status = getattr(e, "status", None)
        mapped = from_api_exception(e, context=context)
        # Status check before the mapped reason: from_api_exception routes 504
        # to upstream_error (its job is the §1.3 code, where "the API server
        # failed" is the right answer), but for a partial read the operator-
        # facing distinction that matters is timeout versus unreachable.
        reason = "timeout" if status in _TIMEOUT_STATUSES else reason_for_error(mapped)
        if reason is None:
            # A 422 or 409 from a secondary read: the API server answered, and
            # what it refused was the request we built. Same reasoning as above.
            raise mapped from e
        state.failed = True
        state.error = mapped
        state.entry = unavailable_entry(
            group, resource, reason, detail=mapped.detail or mapped.message,
            namespace=namespace,
        )
        sink.append(state.entry)
        logger.warning(
            "Partial read: %s/%s%s could not be read (%s, upstream status %s)",
            group or "core", resource,
            f" in {namespace}" if namespace else "",
            reason, status,
            extra={"error_code": mapped.code, "context": context},
        )


__all__ = [
    "ALL_RESOURCES",
    "Collected",
    "UNAVAILABLE_REASONS",
    "collect",
    "envelope",
    "reason_for_error",
    "unavailable_entry",
]
