"""Rollout history only ever lists revisions the workload actually owns (§6).

`_list` narrows server-side by the workload's label selector and `_owned_by`
then confirms ownership by uid, because — in that function's own words — "a
hand-written selector can legitimately match another workload's pods and would
otherwise put a stranger's revisions in this history".

That second half had no test. The whole suite passed with `_owned_by` returning
**True** for an item when the workload's uid could not be read, which is the
inversion that hurts: every ReplicaSet the selector matched would be listed as
this Deployment's history, and one of them is what an operator rolls back to.
A rollback is a write to somebody's cluster made on the strength of this list.

The uid is missing when `metadata.uid` could not be read off the live object.
`None` there means "we do not know what we are looking at", and the §0 rule for
that is the same as everywhere else: refuse to answer, rather than answer
confidently with everything that happened to match.
"""

from __future__ import annotations

from app.admin import rollout


def _revision(uid: str | None, name: str = "checkout-7d4f") -> dict:
    owner = [{"uid": uid, "kind": "Deployment", "name": "checkout"}] if uid else []
    return {"metadata": {"name": name, "ownerReferences": owner}}


def test_a_revision_owned_by_this_workload_is_listed():
    assert rollout._owned_by(_revision("uid-checkout"), "uid-checkout") is True


def test_a_revision_owned_by_another_workload_is_not_listed():
    """The selector matched it; the uid says it belongs to something else."""
    assert rollout._owned_by(_revision("uid-payments"), "uid-checkout") is False


def test_an_unreadable_workload_uid_owns_nothing():
    """Not "owns everything the selector matched" — the inversion that survived.

    With `uid` None there is nothing to compare against. Claiming ownership
    would fill the history with whatever the label selector happened to reach.
    """
    assert rollout._owned_by(_revision("uid-payments"), None) is False
    assert rollout._owned_by(_revision(None), None) is False


def test_a_revision_with_no_owner_references_is_not_listed():
    """An orphaned ReplicaSet is a stranger, not an ancestor."""
    assert rollout._owned_by({"metadata": {"name": "orphan"}}, "uid-checkout") is False


def test_ownership_survives_a_uid_that_arrives_as_a_non_string():
    """Compared as strings: the dynamic client is not consistent about this."""
    assert rollout._owned_by(_revision(12345), "12345") is True


def test_a_stranger_matched_by_the_selector_stays_out_of_the_history(monkeypatch):
    """The guard where it is actually applied, not just the predicate.

    Two ReplicaSets come back from a list narrowed by a hand-written selector
    that reaches both. Only the owned one is a revision of this Deployment.
    """
    ours = _revision("uid-checkout", "checkout-1")
    ours["metadata"]["annotations"] = {rollout.REVISION_ANNOTATION: "4"}
    stranger = _revision("uid-payments", "payments-9")
    stranger["metadata"]["annotations"] = {rollout.REVISION_ANNOTATION: "11"}

    monkeypatch.setattr(rollout, "_list", lambda *a, **k: [ours, stranger])

    live = {
        "metadata": {"uid": "uid-checkout", "annotations": {rollout.REVISION_ANNOTATION: "4"}},
        "spec": {"selector": {"matchLabels": {"tier": "web"}}},
    }
    current, rows, by_revision = rollout._deployment_revisions("prod", "checkout", live)

    assert current == 4
    assert [row["revision"] for row in rows] == [4]
    assert 11 not in by_revision


def test_an_unreadable_uid_yields_no_revisions_rather_than_everything(monkeypatch):
    """The end an operator would see if the inversion shipped: an empty history.

    Empty is the honest answer here and it is not blind — the rollout panel has
    nothing to offer, rather than offering another workload's revisions as this
    one's.
    """
    stranger = _revision("uid-payments", "payments-9")
    stranger["metadata"]["annotations"] = {rollout.REVISION_ANNOTATION: "11"}

    monkeypatch.setattr(rollout, "_list", lambda *a, **k: [stranger])

    live = {"metadata": {"annotations": {}}, "spec": {"selector": {"matchLabels": {"tier": "web"}}}}
    current, rows, by_revision = rollout._deployment_revisions("prod", "checkout", live)

    assert rows == []
    assert by_revision == {}
    assert current is None
