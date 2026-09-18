"""
Login rate limiting (§12.5), counted from a reservation table.

Without a throttle, ``POST /api/auth/login`` is an unmetered password oracle: the
console's PBKDF2 cost is the only thing between an attacker and an offline-speed
online guessing attack, and PBKDF2 is tuned to be survivable per request, not per
million.

**The counter is a database table, not process memory.** A per-process counter
resets on every restart and is per-replica, so a three-replica deployment gives
an attacker three times the budget and a pod recycle gives them a fresh one. The
limit has to be a property of the console, not of the pod that happened to
answer.

**It is a dedicated table rather than a COUNT over the audit trail**, and that is
the correction that matters. Counting audit rows was the first design and it had
two holes, both of which appear only under the conditions the throttle exists
for:

* *Check-then-act.* The audit row for a rejection is written **after** the
  password is verified, so a burst of simultaneous requests all run their COUNT
  before any of them has recorded anything, all see the same number, and all
  proceed. Measured against the real app: 30 concurrent guesses at a documented
  budget of 3, none refused, every one reaching PBKDF2 — which also defeats the
  CPU property this module claims, since all sync handlers share one threadpool.
* *A failed audit INSERT disabled the limiter silently.* Disk full, a hot
  standby, INSERT revoked while SELECT was retained: the COUNT then returned a
  genuine, indistinguishable ``0``, and the endpoint became exactly the oracle
  this module exists to prevent — with no attempts recorded and nothing saying
  that counting had stopped.

Reserving first closes both. Each attempt inserts its own row **before** the
password is checked, so its count includes itself and a concurrent burst counts
each other; and the limiter no longer depends on the audit write succeeding,
because it is no longer reading the audit table.

**When the reservation cannot be taken, the request is allowed through.** That is
the uncomfortable direction and it is the correct one: a database failure that
started refusing every sign-in would lock every operator out of the console
during exactly the kind of incident when they need it, and the password check
still stands behind this. A failure logs its *consequence* — "brute-force
protection is not in effect" — rather than a generic query error, because the two
send an operator to different places and only one of them is urgent.
"""

from __future__ import annotations

import datetime
import logging
import random

from sqlalchemy import delete, func, select

from app import database
from app.config import settings
# The exception lives in app.errors with every other stable code, because the
# frontend branches on `error` and a code defined beside its one raiser is a code
# the vocabulary table does not know about.
from app.errors import TooManyAttempts
from app.models import LoginAttempt, utcnow

logger = logging.getLogger(__name__)

#: How often a reservation pass also prunes rows that have left every window.
#: The table is a bucket, not a record: nothing reads a row older than the window,
#: and keeping them would grow a table forever to answer a question about the last
#: five minutes. Pruned opportunistically rather than on a timer so there is no
#: background task to own.
_PRUNE_EVERY = 4


def _window_start() -> datetime.datetime:
    return utcnow() - datetime.timedelta(seconds=settings.auth_throttle_window_seconds)


def _count(db, actor: str) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.actor == actor[:255],
                LoginAttempt.ts >= _window_start(),
            )
        ).scalar()
        or 0
    )


def reserve(actor: str) -> int | None:
    """Record an attempt and return how many are now in the window.

    ``None`` means the reservation could not be taken — see the module docstring
    for why that is not folded into a number. A count says "this many attempts";
    a null says "we could not look", and the caller logs the difference rather
    than acting on a number it does not have.

    The row is inserted **before** the count, so the returned number includes
    this attempt. That is what makes a simultaneous burst count each other rather
    than all reading the same stale zero.
    """
    db = None
    try:
        # Inside the try, not before it. Opening the session is itself a database
        # operation and fails first when the database is what is down — exactly
        # the case this function exists to survive.
        db = database.SessionLocal()
        db.add(LoginAttempt(actor=actor[:255], ts=utcnow()))
        db.commit()

        count = _count(db, actor)
        # Sampled across reservations, not keyed to this actor's streak. The
        # trigger used to be `count % _PRUNE_EVERY == 0`, which only ever fires
        # for an actor who already has rows in the window — and the attacker
        # this table exists to measure sweeps a *different* username every
        # attempt, so every count stayed at 1, the prune never ran, and the
        # table grew without bound on an unauthenticated endpoint.
        if random.randrange(_PRUNE_EVERY) == 0:
            _prune(db)
        return count
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.error(
            "Could not reserve a sign-in attempt for %r. BRUTE-FORCE PROTECTION "
            "IS NOT IN EFFECT for this request: the sign-in is being allowed to "
            "proceed to the password check rather than locking every operator "
            "out while the console's database is unwell.",
            actor, exc_info=True,
        )
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - nothing left to salvage
                logger.debug("Throttle session rollback failed", exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


def _prune(db) -> None:
    """Drop reservations that have left every window. Never fails a sign-in."""
    try:
        db.execute(delete(LoginAttempt).where(LoginAttempt.ts < _window_start()))
        db.commit()
    except Exception:  # noqa: BLE001 - housekeeping must not refuse a login
        logger.warning("Could not prune old sign-in reservations", exc_info=True)
        db.rollback()


def release(actor: str) -> None:
    """Clear an actor's reservations after a successful sign-in.

    A working account must not accumulate a budget against itself: without this,
    ``AUTH_THROTTLE_MAX_ATTEMPTS`` successful sign-ins inside one window would
    lock an operator out of their own console.

    Best-effort. A failure here costs one operator a wait; raising would cost
    them a sign-in that actually succeeded, and reporting a failure for something
    that worked is the wrong direction to be wrong in.
    """
    db = None
    try:
        db = database.SessionLocal()
        db.execute(delete(LoginAttempt).where(LoginAttempt.actor == actor[:255]))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not clear sign-in reservations for %r after a successful "
            "sign-in; they will expire with the window.", actor, exc_info=True,
        )
    finally:
        if db is not None:
            db.close()


def check(actor: str) -> None:
    """Reserve this attempt and raise :class:`TooManyAttempts` if it is over budget.

    Keyed on the submitted username alone, not on username plus source address.
    Adding the address makes the limit trivially evadable from a botnet while
    doing nothing about the case that matters — many attempts against one
    account — and the cost of the stricter key is that one user can lock out
    their own sign-ins for the window, which is recoverable by waiting and is
    cleared outright by one successful sign-in.

    **Only the local/LDAP password path calls this.** Single sign-on failures are
    deliberately not counted: an operator whose SSO identity this console refuses
    an account (a username owned by a local account, a group that is not
    permitted) would otherwise lock out the password login of the person who owns
    that username — and those attempts were authenticated at the issuer rather
    than guessed here.

    Enabled by default. ``AUTH_THROTTLE_MAX_ATTEMPTS=0`` turns it off, which is a
    deliberate act.
    """
    if settings.auth_throttle_max_attempts <= 0:
        return
    attempts = reserve(actor)
    if attempts is None:
        return
    if attempts > settings.auth_throttle_max_attempts:
        logger.warning(
            "Throttling sign-in for %r: %d attempts in the last %d seconds.",
            actor, attempts, settings.auth_throttle_window_seconds,
        )
        raise TooManyAttempts(
            "Too many sign-in attempts for this account. Wait "
            f"{max(1, settings.auth_throttle_window_seconds // 60)} minute(s) and "
            "try again.",
            hint="The limit counts attempts for this username across every "
                 "console replica, so it does not reset when a pod does. A "
                 "successful sign-in clears it immediately.",
            context={
                "retryAfterSeconds": settings.auth_throttle_window_seconds,
                "windowSeconds": settings.auth_throttle_window_seconds,
                "maxAttempts": settings.auth_throttle_max_attempts,
            },
        )


def recent_attempts(actor: str) -> int | None:
    """Reservations in the window for ``actor``, without taking one.

    ``None`` when the count could not be taken, for the same reason
    :func:`reserve` returns it: a zero says nobody has tried, and a null says we
    could not look.
    """
    db = None
    try:
        db = database.SessionLocal()
        return _count(db, actor)
    except Exception:  # noqa: BLE001
        logger.error("Could not count sign-in reservations for %r", actor, exc_info=True)
        return None
    finally:
        if db is not None:
            db.close()


__all__ = ["TooManyAttempts", "check", "recent_attempts", "release", "reserve"]
