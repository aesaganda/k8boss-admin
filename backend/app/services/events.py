"""
Events (§5) — "what just happened", which is the only question this page answers.

The read model. ``app/api/events.py`` is the four query parameters and a
call to :func:`list_events`; everything that decides *what the answer is*
lives here, with the rest of the typed read models, because that is where a
reader looks for it and where the next page that wants an event row will.

Three decisions in here are worth reading before the code, because each of them
is the difference between a correct answer and a plausible one.

**Newest first has to be computed, not requested.** The API server returns
Events in etcd key order — ``namespace/name``, where an Event's name is
``<object>.<hex>`` — and it offers no server-side sort. A ``limit`` handed to the
API server therefore returns an *arbitrary* 200 events, and sorting those by time
produces a page headed "most recent" that is nothing of the sort: the newest
event on the cluster is very likely not in it. So this endpoint scans up to a
bounded number of events, sorts them itself, and returns the newest ``limit`` of
what it scanned. When the scan hits its budget it says so in ``unavailable[]``
rather than quietly presenting the newest of a subset as the newest overall.

**Half the events on a modern cluster do not have ``lastTimestamp``.** Anything
written through ``events.k8s.io/v1`` — which is what ``client-go``'s current
event recorder uses, so increasingly everything — carries ``eventTime`` and,
when it repeats, ``series.lastObservedTime``, leaving the legacy
``firstTimestamp``/``lastTimestamp``/``count`` fields null in the core/v1 view.
Sorting on ``lastTimestamp`` alone silently buries every one of those events at
the bottom of the list with a blank age column, which is exactly the failure this
page cannot have: the events an operator is looking for during an incident are
the new ones. :func:`_last_seen` and :func:`_first_seen` fall through both
spellings, and :func:`_event_count` and :func:`_source` do the same for the
fields that moved with them.

**Filters are applied server-side.** Filtering after a bounded scan would make
"the 200 newest Warnings" mean "the Warnings among the 200 newest events" — a
different question, usually answered with far fewer rows than exist, and with
nothing in the response to say so.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes.client.rest import ApiException

from app.errors import from_api_exception
from app.k8s.client import get_core_v1
from app.resources import shaping
from app.resources.envelope import collect, envelope, unavailable_entry

logger = logging.getLogger(__name__)

#: Chunk size for the scan. Large enough that an ordinary cluster is one or two
#: round trips, small enough that a single page cannot blow the read deadline.
_SCAN_PAGE_SIZE = 500

#: Page budget. 5000 events is more than any cluster produces inside the window
#: this page is read over, and it bounds the work a single request can do —
#: without it, one request against a cluster in a crash loop pages forever and
#: holds a threadpool worker while it does.
_MAX_SCAN_PAGES = 10

#: Default page size (§5).
DEFAULT_LIMIT = 200


def _escape_selector_value(value: str) -> str:
    """Escape a value for a Kubernetes field selector.

    ``,`` separates selectors and ``=`` separates a key from its value, so both
    have to be escaped inside a value, and the escape character has to be escaped
    first. Kubernetes object names cannot contain any of the three, which is
    exactly why this is easy to skip — and why it would then sit there until an
    ``involvedObjectName`` arrived from somewhere less disciplined than the API
    server and turned one filter into two.
    """
    return value.replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=")


def _field_selector(
    *, involved_kind: str | None, involved_name: str | None, event_type: str | None
) -> str | None:
    """The §5 filters as one field selector, or ``None`` when nothing was asked.

    ``involvedObject.kind``, ``involvedObject.name`` and ``type`` are all
    registered field selectors on Events and have been since 1.0, so this is a
    server-side filter on every cluster this console can talk to. See the module
    docstring for why doing it client-side would be wrong rather than merely
    slower.
    """
    parts = []
    if involved_kind:
        parts.append(f"involvedObject.kind={_escape_selector_value(involved_kind)}")
    if involved_name:
        parts.append(f"involvedObject.name={_escape_selector_value(involved_name)}")
    if event_type:
        parts.append(f"type={_escape_selector_value(event_type)}")
    return ",".join(parts) if parts else None


def _last_seen(event: Any) -> str | None:
    """When this event was last observed, whichever API wrote it.

    Order is most-specific first: a ``series`` means the event has repeated and
    its ``lastObservedTime`` is the newest observation; ``lastTimestamp`` is the
    legacy equivalent; ``eventTime`` is a single new-style occurrence; and
    ``creationTimestamp`` is the floor, because an object that exists was created
    at some point even if every other field is empty.

    Returning ``None`` remains possible and is honest — an event with no usable
    timestamp sorts to the *bottom*, not the top, since an unknown age must not
    be presented as "just now" on a page whose entire purpose is recency.
    """
    return shaping.rfc3339(
        shaping.get_field(event, "series", "lastObservedTime")
        or shaping.get_field(event, "lastTimestamp")
        or shaping.get_field(event, "eventTime")
        or shaping.get_field(event, "metadata", "creationTimestamp")
    )


def _first_seen(event: Any) -> str | None:
    """When this event was first observed. Same fallback chain, first-observation end."""
    return shaping.rfc3339(
        shaping.get_field(event, "firstTimestamp")
        or shaping.get_field(event, "eventTime")
        or shaping.get_field(event, "metadata", "creationTimestamp")
    )


def _event_count(event: Any) -> int | None:
    """How many times this event has occurred.

    ``count`` for the legacy API, ``series.count`` for a repeating new-style
    event, and ``1`` for a new-style event with no series — that last one is
    derived, not invented: ``series`` is set precisely when an event repeats, so
    its absence alongside an ``eventTime`` *is* the API saying "once". Reporting
    ``null`` there would put an em dash on nearly every row of a modern cluster
    and would claim we do not know something the object states.

    ``None`` survives for the case where the object carries no count, no series
    and no ``eventTime``, where we genuinely cannot tell. It is never ``0``: an
    event that occurred zero times is not an event.
    """
    count = shaping.get_field(event, "count")
    if isinstance(count, int) and count > 0:
        return count
    series_count = shaping.get_field(event, "series", "count")
    if isinstance(series_count, int) and series_count > 0:
        return series_count
    if shaping.get_field(event, "eventTime") is not None:
        return 1
    return None


def _source(event: Any) -> dict[str, Any]:
    """``{component, host}``, falling back to the new API's reporting fields.

    ``source`` is the legacy pair. ``events.k8s.io`` replaced it with
    ``reportingComponent`` / ``reportingInstance`` and leaves ``source`` empty, so
    reading only the first spelling gives a blank Source column for every event
    from a current controller — which reads as "nobody reported this".
    """
    return {
        "component": (
            shaping.get_field(event, "source", "component")
            or shaping.get_field(event, "reportingComponent")
        ),
        "host": (
            shaping.get_field(event, "source", "host")
            or shaping.get_field(event, "reportingInstance")
        ),
    }


def event_row(event: Any) -> dict[str, Any]:
    """The §5 event row.

    ``message`` reads ``note`` as a fallback: the field was renamed in
    ``events.k8s.io/v1`` and the core/v1 view maps it back, but an object that
    reached here straight from the newer API would otherwise render with no text
    at all — an event row with no message is worse than no row.
    """
    involved = shaping.get_field(event, "involvedObject")
    return {
        "namespace": shaping.get_field(event, "metadata", "namespace"),
        "type": shaping.get_field(event, "type"),
        "reason": shaping.get_field(event, "reason"),
        "message": shaping.get_field(event, "message") or shaping.get_field(event, "note"),
        "count": _event_count(event),
        "first_seen": _first_seen(event),
        "last_seen": _last_seen(event),
        "involved": {
            "kind": shaping.get_field(involved, "kind"),
            "name": shaping.get_field(involved, "name"),
            "namespace": shaping.get_field(involved, "namespace"),
            "uid": shaping.get_field(involved, "uid"),
        },
        "source": _source(event),
    }


def _list_page(
    *, namespace: str | None, field_selector: str | None, cont: str | None
) -> Any:
    """One page of events, namespaced or cluster-wide."""
    core = get_core_v1()
    if namespace:
        return core.list_namespaced_event(
            namespace,
            field_selector=field_selector,
            limit=_SCAN_PAGE_SIZE,
            _continue=cont,
        )
    return core.list_event_for_all_namespaces(
        field_selector=field_selector,
        limit=_SCAN_PAGE_SIZE,
        _continue=cont,
    )


def _fetch_page(
    *,
    namespace: str | None,
    field_selector: str | None,
    cont: str | None,
    context: dict[str, Any],
) -> Any:
    """The **first** page, with the API server's own failure mapped to §1.3.

    Used only for the primary read, where the response is going to be an error
    envelope rather than a partial answer, and where the ``context`` is worth
    carrying: it is what turns "forbidden" into a hint naming ``list`` on
    ``core/events``. Later pages go to ``collect`` with the raw ``ApiException``,
    because ``collect`` classifies a partial read's reason and needs the HTTP
    status to tell a timeout from an unreachable endpoint.
    """
    try:
        return _list_page(namespace=namespace, field_selector=field_selector, cont=cont)
    except ApiException as e:
        raise from_api_exception(
            e, context={k: v for k, v in context.items() if v is not None}
        ) from e


def _scan(
    *, namespace: str | None, field_selector: str | None
) -> tuple[list[Any], int | None, list[dict[str, Any]]]:
    """Page through events up to the budget. Returns ``(events, remaining, unavailable)``.

    The first page is the endpoint's primary read and its failure raises: there
    is no partial answer to give when nothing was read, and a 403 rendered as an
    empty event list says "nothing has happened on this cluster" to an operator
    who is looking at it because something did.

    A *later* page failing is different — we hold real events — so it is recorded
    in ``unavailable`` and the rows we have are returned. Both cases are
    distinguishable by the caller, which is the whole point: the response says
    which question it failed to answer rather than answering a smaller one
    silently.
    """
    events: list[Any] = []
    unavailable: list[dict[str, Any]] = []
    remaining: int | None = None
    cont: str | None = None
    context = {"verb": "list", "group": "", "resource": "events", "namespace": namespace}

    for page_index in range(_MAX_SCAN_PAGES):
        if page_index == 0:
            # The primary read. Nothing has been read yet, so there is no partial
            # answer to degrade to; it raises, and it is mapped with the full
            # target context so the §1.3 hint names `list` on `core/events`
            # rather than a generic "access".
            listing = _fetch_page(
                namespace=namespace, field_selector=field_selector,
                cont=cont, context=context,
            )
        else:
            # Every later page is a secondary read: real events are already in
            # hand, and losing the rest costs completeness rather than the
            # answer. `collect` takes the raw ApiException on purpose — it owns
            # the error-to-reason mapping, including the refinement that a 504
            # is a `timeout` and not an `unreachable`, and a second copy of that
            # here would drift into telling an operator to check DNS about a
            # failure another endpoint tells them to narrow their query about.
            listing = None
            with collect(unavailable, "", "events", namespace=namespace) as state:
                listing = _list_page(
                    namespace=namespace, field_selector=field_selector, cont=cont
                )
            if state.failed:
                # Recorded with what we did manage, because "some events, and
                # here is what stopped us" is a different claim from "these are
                # the events".
                state.entry["detail"] = (
                    f"Only the first {len(events)} events could be read; the "
                    f"listing failed while continuing. {state.entry['detail']}"
                )
                logger.warning(
                    "Event scan stopped after %d events on page %d: %s",
                    len(events), page_index + 1, state.error.message,
                )
                return events, remaining, unavailable

        events.extend(shaping.get_field(listing, "items", default=[]) or [])
        metadata = shaping.get_field(listing, "metadata")
        remaining = shaping.get_field(metadata, "remainingItemCount")
        cont = shaping.get_field(metadata, "continue") or None
        if not cont:
            # The listing is complete, so nothing was missed and `remaining` is
            # meaningless here — the API server sends it only while more remains.
            return events, None, unavailable

    # Budget exhausted with more events behind the cursor. Reported rather than
    # presented as a complete answer: everything below is sorted newest-first
    # over what was *scanned*, and the newest event on the cluster may not be in
    # it. Saying so is the difference between a bounded answer and a wrong one.
    unavailable.append(
        unavailable_entry(
            "", "events", "timeout",
            detail=(
                f"Stopped after scanning {len(events)} events "
                f"({_MAX_SCAN_PAGES} pages of {_SCAN_PAGE_SIZE}). The API server "
                "does not sort events, so 'newest first' covers only the events "
                "scanned. Narrow the listing with namespace, type or "
                "involvedObject filters."
            ),
            namespace=namespace,
        )
    )
    return events, remaining, unavailable


def list_events(
    *,
    namespace: str | None = None,
    involved_kind: str | None = None,
    involved_name: str | None = None,
    event_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """§5 events, newest ``last_seen`` first, as the §1.2 envelope.

    ``continue`` is always ``null``, and that is deliberate rather than an
    omission. The Kubernetes cursor pages in etcd order while this response is in
    time order; handing it back would let a caller "page" into a different
    ordering than the one it was given, interleaving the two. When the scan was
    truncated, ``partial`` is true and ``unavailable`` says so — which is the
    honest version of "there is more".
    """
    field_selector = _field_selector(
        involved_kind=involved_kind,
        involved_name=involved_name,
        event_type=event_type,
    )
    events, remaining, unavailable = _scan(
        namespace=namespace, field_selector=field_selector
    )

    rows = [event_row(event) for event in events]
    # Sorted on the rendered RFC 3339 string, which is fixed-width UTC and
    # therefore sorts lexicographically exactly as it sorts chronologically. The
    # leading `is not None` term keeps events with no derivable timestamp at the
    # bottom under `reverse=True`: an unknown age must not be shown as the most
    # recent thing that happened. The trailing name is a stable tie-break for
    # events that share a second — arbitrary, but the same arbitrary order on
    # every refresh, so the table does not reshuffle under the cursor.
    rows.sort(
        key=lambda row: (
            row["last_seen"] is not None,
            row["last_seen"] or "",
            row["involved"]["name"] or "",
        ),
        reverse=True,
    )

    if len(rows) > limit:
        # Truncating a locally sorted list is not a partial read: everything in
        # the window was scanned and compared, and the rows dropped are older
        # than every row kept. `remaining` reports what the API server said was
        # behind the cursor, which is a different number and a different claim.
        rows = rows[:limit]

    return envelope(rows, remaining=remaining, unavailable=unavailable)


MAX_LIMIT = _SCAN_PAGE_SIZE * _MAX_SCAN_PAGES

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "event_row", "list_events"]
