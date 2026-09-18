"""
The sign-in throttle's housekeeping (§12.5).

`login_attempts` is a bucket, not a record: nothing reads a row that has left
the window, so the rows are dropped on the way past rather than by a background
timer somebody would have to own. What this file is about is the **trigger** for
that drop, which was keyed to the one thing the attacker controls — and so never
fired for them.

The behaviour of the limit itself lives in `test_auth_events.py`, beside the
audit rows a refused sign-in produces.
"""

from __future__ import annotations

import datetime

from sqlalchemy import select

from app import database
from app.identity import throttle
from app.models import LoginAttempt, utcnow


def reserved_actors() -> list[str]:
    session = database.SessionLocal()
    try:
        return list(session.execute(select(LoginAttempt.actor)).scalars())
    finally:
        session.close()


def test_expired_rows_are_pruned_even_when_every_attempt_uses_a_new_username(
    db_engine, monkeypatch
):
    """The prune cannot be triggered by one actor's streak.

    It was `count % _PRUNE_EVERY == 0`, which only ever fires for an actor who
    already has rows inside the window. The caller this table exists to measure
    is an unauthenticated sweep trying a different username every time, so every
    count stayed at 1, the prune never ran once, and the table grew without
    bound on a public endpoint.

    The sample is pinned rather than left to chance, so what this measures is
    the trigger's *input* and not a coin landing the right way up.
    """
    monkeypatch.setattr(throttle.random, "randrange", lambda _n: 0)
    session = database.SessionLocal()
    session.add(
        LoginAttempt(actor="last-week", ts=utcnow() - datetime.timedelta(days=7))
    )
    session.commit()
    session.close()

    throttle.reserve("sweep-0001")

    assert reserved_actors() == ["sweep-0001"]


def test_the_prune_leaves_reservations_that_are_still_inside_the_window(
    db_engine, monkeypatch
):
    """Only the trigger moved.

    A prune that took live rows with it would be the limiter handing out the
    reset it exists to withhold — and it would do it on a schedule nobody reading
    the login path would think to look at.
    """
    monkeypatch.setattr(throttle.random, "randrange", lambda _n: 0)
    throttle.reserve("erens")

    throttle.reserve("dilek")

    assert sorted(reserved_actors()) == ["dilek", "erens"]
