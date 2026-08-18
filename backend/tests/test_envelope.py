"""
The §1.2 envelope, and the mechanism that keeps "empty is never blind" true.

Rule 1 of the contract is stated in prose and enforced by two functions, and
these are the tests that make the enforcement mechanical rather than editorial:

* ``partial`` is **derived** from ``unavailable`` — true if and only if it is
  non-empty. Not passed, not settable, not forgettable. The invariant is asserted
  from both ends, including the assertion that no caller *can* pass it.
* :func:`~app.resources.envelope.collect` records ``forbidden``, ``unreachable``
  and ``timeout`` as three distinct reasons, because the operator's next move
  differs in each case: fix RBAC, check the network path, or narrow the query.
  Collapsing them into one token is a smaller lie than returning an empty list,
  and still sends someone to debug the wrong system.
* An exception inside a ``collect`` block never decays into a bare empty list.
  The variable the block was assigning is left at ``None``, which is what makes
  "we could not look" survive into the row as ``null`` rather than as ``0``.

The last test in this file is the one that matters most: a bug in *this process*
— a ``TypeError`` from a shaper, a ``KeyError`` from a rename — must propagate,
not be recorded as "the cluster was unreachable". Reporting our own defect as a
cluster fault is the same confidently wrong answer pointed at the wrong system.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import ApiException

from app.errors import (
    ClusterUnreachable,
    Invalid,
    NotFound,
    NoClusterSelected,
    RBACDenied,
    Unsupported,
)
from app.resources.envelope import (
    UNAVAILABLE_REASONS,
    collect,
    envelope,
    reason_for_error,
    unavailable_entry,
)


# --------------------------------------------------------------------------- #
# partial is derived, never passed
# --------------------------------------------------------------------------- #

def test_partial_is_false_when_nothing_was_lost():
    result = envelope([{"name": "a"}])

    assert result["partial"] is False
    assert result["unavailable"] == []


def test_partial_is_true_the_moment_unavailable_has_an_entry():
    result = envelope(
        [], unavailable=[unavailable_entry("", "secrets", "forbidden", namespace="prod")]
    )

    assert result["partial"] is True
    assert result["items"] == []


def test_partial_is_true_even_when_items_came_back():
    """A page that answered *and* lost something is still partial.

    The dangerous shape is a full-looking table with one silent hole in it, so
    the banner is driven by `unavailable`, never by whether `items` is empty.
    """
    result = envelope(
        [{"name": "a"}, {"name": "b"}],
        unavailable=[unavailable_entry("apps", "deployments", "forbidden")],
    )

    assert result["partial"] is True
    assert len(result["items"]) == 2


def test_partial_cannot_be_passed_by_a_caller():
    """The invariant is mechanical: there is no parameter to get wrong.

    A `partial=` keyword would eventually be set to False next to a populated
    `unavailable` list — most likely by a handler that appended to the list after
    building the envelope. Rejecting the keyword outright is what makes that
    impossible rather than merely discouraged.
    """
    with pytest.raises(TypeError):
        envelope([], partial=False)  # type: ignore[call-arg]


@pytest.mark.parametrize("count", [0, 1, 2, 5])
def test_partial_tracks_the_length_of_unavailable_exactly(count):
    entries = [unavailable_entry("", "pods", "forbidden") for _ in range(count)]

    assert envelope([], unavailable=entries)["partial"] is (count > 0)


# --------------------------------------------------------------------------- #
# Envelope mechanics
# --------------------------------------------------------------------------- #

def test_an_empty_continue_token_becomes_null():
    """The API server sends "" for a complete listing; the contract says null.

    A UI that treats "" as truthy renders a "next page" control that fetches the
    first page again, forever.
    """
    assert envelope([], cont="")["continue"] is None
    assert envelope([], cont="tok")["continue"] == "tok"


def test_remaining_stays_none_rather_than_becoming_zero():
    """`remainingItemCount` is absent when the API server cannot cheaply count.

    Reporting 0 there would say "this is the last page" about a listing that has
    a continue token.
    """
    assert envelope([], cont="tok")["remaining"] is None
    assert envelope([], cont="tok", remaining=412)["remaining"] == 412


def test_items_are_materialised_inside_the_envelope():
    """A generator that raises must fail here, not during JSON serialisation.

    Serialisation happens after the response has begun; the error escapes as a
    truncated body and the handler that could have reported it is long gone.
    """
    def exploding():
        yield {"name": "a"}
        raise RuntimeError("shaper blew up on the second row")

    with pytest.raises(RuntimeError):
        envelope(exploding())


def test_the_envelope_does_not_alias_the_caller_s_unavailable_list():
    """Appending after the fact must not silently change a built envelope.

    If it aliased, `partial` would be false next to a populated list — the exact
    state the derivation exists to prevent.
    """
    sink: list = []
    result = envelope([], unavailable=sink)
    sink.append(unavailable_entry("", "pods", "forbidden"))

    assert result["unavailable"] == []
    assert result["partial"] is False


# --------------------------------------------------------------------------- #
# unavailable entries
# --------------------------------------------------------------------------- #

def test_every_key_of_an_entry_is_always_present():
    """The frontend reads `entry.namespace` unconditionally."""
    entry = unavailable_entry("", "pods", "forbidden")

    assert set(entry) == {"group", "resource", "namespace", "reason", "detail"}
    assert entry["namespace"] is None
    assert entry["detail"] is None


def test_an_unrecognised_reason_is_refused_rather_than_defaulted():
    """A token outside the closed vocabulary would render as the catch-all.

    That silently downgrades a `forbidden` into "the cluster is unreachable" and
    sends the operator to debug their network instead of their RBAC.
    """
    with pytest.raises(ValueError) as excinfo:
        unavailable_entry("", "pods", "denied")

    assert "denied" in str(excinfo.value)


def test_the_core_group_is_recorded_as_the_empty_string():
    """Never the §1.4 wire spelling `core`, which exists only for URL segments."""
    entry = unavailable_entry("", "secrets", "forbidden", namespace="prod")

    assert entry["group"] == ""
    assert entry["namespace"] == "prod"


# --------------------------------------------------------------------------- #
# collect: the three reasons that must stay distinct
# --------------------------------------------------------------------------- #

def test_forbidden_is_recorded_as_forbidden(caplog):
    sink: list = []
    pod_count = None

    with collect(sink, "", "pods", namespace="prod") as state:
        raise ApiException(status=403, reason="Forbidden")

    assert pod_count is None
    assert state.failed is True
    assert sink == [
        {
            "group": "", "resource": "pods", "namespace": "prod",
            "reason": "forbidden", "detail": "Forbidden",
        }
    ]


def test_an_unreachable_cluster_is_recorded_as_unreachable():
    """status=0 from the kubernetes client means no HTTP exchange happened."""
    sink: list = []

    with collect(sink, "", "pods"):
        raise ApiException(status=0, reason="SSLError: certificate verify failed")

    assert sink[0]["reason"] == "unreachable"


@pytest.mark.parametrize("status", [408, 504])
def test_a_deadline_is_recorded_as_timeout_not_unreachable(status):
    """The operator's next move differs: narrow the query, don't check DNS.

    504 in particular is mapped to `upstream_error` by `from_api_exception` —
    the right §1.3 code, since the API server did fail — but for a partial read
    the useful distinction is timeout versus unreachable, so `collect` overrides
    it on status.
    """
    sink: list = []

    with collect(sink, "", "pods"):
        raise ApiException(status=status, reason="Gateway Timeout")

    assert sink[0]["reason"] == "timeout"


def test_the_transport_s_own_timeout_is_recorded_as_timeout():
    """`app.k8s.client` stamps context["cause"] when it knows which it was.

    ClusterUnreachable covers both "did not answer" and "answered too slowly";
    throwing that distinction away here would waste the work the transport did to
    make it.
    """
    sink: list = []

    with collect(sink, "", "pods"):
        raise ClusterUnreachable(
            "The cluster API server did not answer within the read deadline.",
            detail="read timeout", context={"cause": "timeout"},
        )

    assert sink[0]["reason"] == "timeout"
    assert sink[0]["detail"] == "read timeout"


def test_an_unreachable_admin_error_stays_unreachable():
    sink: list = []

    with collect(sink, "", "pods"):
        raise ClusterUnreachable("nope", context={"cause": "unreachable"})

    assert sink[0]["reason"] == "unreachable"


def test_a_missing_resource_is_recorded_as_not_found():
    sink: list = []

    with collect(sink, "metrics.k8s.io", "nodes"):
        raise ApiException(status=404, reason="Not Found")

    assert sink[0]["reason"] == "not_found"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RBACDenied("no"), "forbidden"),
        (NotFound("no"), "not_found"),
        (ClusterUnreachable("no"), "unreachable"),
        (NoClusterSelected("no"), "not_registered"),
        (Unsupported("no"), "unsupported"),
        (Invalid("no"), "unreachable"),
    ],
)
def test_every_error_class_maps_into_the_closed_vocabulary(error, expected):
    """A new error class cannot quietly start rendering as the catch-all.

    `unavailable_entry` refuses anything outside the vocabulary, so a mapping
    that drifted would raise rather than mislabel — but only if the mapping is
    exercised, which is what this is for.
    """
    assert reason_for_error(error) == expected
    assert expected in UNAVAILABLE_REASONS


# --------------------------------------------------------------------------- #
# collect: what it must NOT do
# --------------------------------------------------------------------------- #

def test_a_failed_read_leaves_the_variable_at_none_not_at_zero():
    """This is the whole idiom, and the reason it is shaped as a context manager.

    `try/except: pod_count = 0` is the bug it replaces: an unreadable namespace
    and an idle one become the same row, and the idle-looking one is what an
    operator deletes.
    """
    sink: list = []
    pod_count: int | None = None

    with collect(sink, "", "pods", namespace="prod"):
        raise ApiException(status=403, reason="Forbidden")
        pod_count = 0  # pragma: no cover - unreachable by construction

    assert pod_count is None
    assert pod_count != 0


def test_a_successful_block_records_nothing_and_keeps_its_value():
    sink: list = []
    pod_count = None

    with collect(sink, "", "pods") as state:
        pod_count = 0

    assert pod_count == 0
    assert sink == []
    assert state.failed is False
    assert state.entry is None
    assert state.error is None


def test_a_bug_in_this_process_is_not_recorded_as_a_cluster_failure():
    """A TypeError from a shaper propagates. It is not the cluster's fault.

    Recording it as `unreachable` would produce a green-ish page with a footnote
    blaming the cluster, and the real defect — ours — would never be seen.
    """
    sink: list = []

    with pytest.raises(TypeError):
        with collect(sink, "", "pods"):
            raise TypeError("pod_row() got an unexpected keyword argument")

    assert sink == []


def test_a_keyerror_from_a_rename_propagates_too():
    sink: list = []

    with pytest.raises(KeyError):
        with collect(sink, "", "pods"):
            {"a": 1}["b"]

    assert sink == []


def test_the_collected_state_carries_the_mapped_error_for_callers_that_need_it():
    sink: list = []

    with collect(sink, "", "pods", namespace="prod") as state:
        raise ApiException(status=403, reason="Forbidden")

    assert state.failed is True
    assert state.error is not None
    assert state.error.code == "rbac_denied"
    assert state.entry is sink[0]


def test_several_blocks_accumulate_into_one_sink():
    """Losing one optional read must not lose the others, or their reasons."""
    sink: list = []

    with collect(sink, "", "pods"):
        raise ApiException(status=403, reason="Forbidden")
    with collect(sink, "discovery.k8s.io", "endpointslices"):
        raise ApiException(status=504, reason="Gateway Timeout")
    with collect(sink, "", "services"):
        pass

    assert [entry["reason"] for entry in sink] == ["forbidden", "timeout"]
    assert envelope([], unavailable=sink)["partial"] is True
