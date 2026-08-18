"""
Login rate limiting, counted from the audit trail (§12.7).

Without a throttle, ``POST /api/auth/login`` is an unmetered password oracle: the
console's PBKDF2 cost is the only thing between an attacker and an offline-speed
online guessing attack, and PBKDF2 is tuned to be survivable per request, not per
million.

**The counter is the audit trail, not a process-local dict.** Two reasons:

* A per-process counter resets on every restart and is per-replica, so a
  three-replica deployment gives an attacker three times the budget and a pod
  recycle gives them a fresh one. The audit table is shared PostgreSQL in any
  real deployment, so the limit is a property of the console rather than of the
  pod that happened to answer.
* The rows already exist. Every rejected sign-in is recorded by §10 whether or
  not anything counts them, so the throttle costs one indexed ``COUNT`` on the
  login path and adds no state to maintain, expire or leak.

**When the count cannot be taken, the request is allowed through.** That is the
uncomfortable direction and it is the correct one here: a database hiccup that
started refusing every login would lock every operator out of the console during
exactly the kind of incident when they need it, and the password check still
stands behind this. So a failed count logs its *consequence* at ERROR — "brute
force protection is not in effect" — rather than a generic query error, because
the two send an operator to different places and only one of them is urgent.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select

from app import database
from app.config import settings
# The exception lives in app.errors with every other stable code, because the
# frontend branches on `error` and a code defined beside its one raiser is a code
# the vocabulary table does not know about.
from app.errors import TooManyAttempts
from app.models import CATEGORY_CONSOLE, AuditRecord, utcnow

logger = logging.getLogger(__name__)


def _window_start() -> datetime.datetime:
    return utcnow() - datetime.timedelta(seconds=settings.auth_throttle_window_seconds)


def recent_failures(actor: str) -> int | None:
    """Failed sign-ins recorded for ``actor`` inside the window, or ``None``.

    ``None`` means the count could not be taken — see the module docstring for
    why that is not folded into ``0``. A zero says "nobody has tried"; a null says
    "we could not look", and the caller logs the difference rather than acting on
    a number it does not have.
    """
    db = None
    try:
        # Inside the try, not before it. Opening the session is itself a database
        # operation and fails first when the database is the thing that is down —
        # exactly the case this function exists to survive. With the call outside,
        # the exception escapes to the login route and every sign-in 500s, which
        # is the lockout the module docstring says it refuses to cause.
        db = database.SessionLocal()
        return int(
            db.execute(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.category == CATEGORY_CONSOLE,
                    AuditRecord.verb == "login",
                    AuditRecord.outcome == "denied",
                    AuditRecord.actor == actor,
                    AuditRecord.ts >= _window_start(),
                )
            ).scalar()
            or 0
        )
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.error(
            "Could not count recent sign-in failures for %r. BRUTE-FORCE "
            "PROTECTION IS NOT IN EFFECT for this request: the sign-in is being "
            "allowed to proceed to the password check rather than locking every "
            "operator out while the console's database is unwell.",
            actor, exc_info=True,
        )
        return None
    finally:
        if db is not None:
            db.close()


def check(actor: str) -> None:
    """Raise :class:`TooManyAttempts` if ``actor`` has spent the budget.

    Keyed on the submitted username alone, not on username plus source address.
    Adding the address makes the limit trivially evadable from a botnet while
    doing nothing about the case that matters — many attempts against one
    account — and the cost of the stricter key is that one user can lock out
    their own sign-ins for the window, which is recoverable by waiting.

    Enabled by default. ``AUTH_THROTTLE_MAX_ATTEMPTS=0`` turns it off, which is a
    deliberate act and is logged at startup.
    """
    if settings.auth_throttle_max_attempts <= 0:
        return
    failures = recent_failures(actor)
    if failures is None:
        return
    if failures >= settings.auth_throttle_max_attempts:
        logger.warning(
            "Throttling sign-in for %r: %d failures in the last %d seconds.",
            actor, failures, settings.auth_throttle_window_seconds,
        )
        raise TooManyAttempts(
            "Too many sign-in attempts for this account. Wait "
            f"{settings.auth_throttle_window_seconds // 60} minute(s) and try again.",
            hint="The limit counts failed attempts for this username across "
                 "every console replica, so it does not reset when a pod does.",
            context={
                "retryAfterSeconds": settings.auth_throttle_window_seconds,
                "windowSeconds": settings.auth_throttle_window_seconds,
                "maxAttempts": settings.auth_throttle_max_attempts,
            },
        )


__all__ = ["TooManyAttempts", "check", "recent_failures"]
