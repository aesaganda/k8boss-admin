"""
Console authentication in the audit trail, and the sign-in throttle (§10, §12.5).

The trail used to hold cluster writes and a handful of user-administration rows.
It now holds sign-ins too, and the reason is the one that motivates the whole
table: **"who tried" is the question asked after an incident**, and a trail with
no record of a failed sign-in cannot answer "was somebody guessing at this
account" — which is also, not coincidentally, the input the throttle counts.

Two properties get most of the attention here:

* **A rejected sign-in is recorded against the submitted username.** Recording
  every failure as `anonymous` would collapse a thousand attempts on one account
  into an undifferentiated pile, which is the same as not recording them.
* **A provider outage is `failed`, not `denied`.** The two are different facts,
  they send an operator to different places, and only one of them counts toward
  a lockout.
"""

from __future__ import annotations

import pytest

from app import database
from app.audit import recorder
from app.config import settings
from app.errors import IdentityProviderUnavailable, TooManyAttempts
from app.identity import throttle
from app.identity.service import create_local_user

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "ldap_enabled", False)
    monkeypatch.setattr(settings, "oidc_enabled", False)
    return settings


def console_records(verb: str | None = None) -> list[dict]:
    rows = recorder.query(category="console", limit=1000)["items"]
    return [row for row in rows if verb is None or row["verb"] == verb]


def attempt(client, username: str, password: str):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# --------------------------------------------------------------------------- #
# Sign-ins are recorded
# --------------------------------------------------------------------------- #

def test_a_successful_sign_in_is_recorded(client, db_session, auth_enabled):
    create_local_user(db_session, username="erens", password=PASSWORD, role="admin")

    assert attempt(client, "erens", PASSWORD).status_code == 200

    (record,) = console_records("login")
    assert record["outcome"] == "applied"
    assert record["actor"] == "erens"
    assert record["category"] == "console"
    assert record["dry_run"] is False
    # No cluster attribution: a sign-in has nothing to do with whichever cluster
    # the switcher happened to be on, and attributing it to one would put the row
    # somewhere an operator filtering for sign-ins would never look.
    assert record["cluster_id"] is None
    assert record["target"]["resource"] == "sessions"


def test_a_rejected_sign_in_is_recorded_against_the_submitted_username(
    client, db_session, auth_enabled
):
    """`anonymous` would make every failure identical and the trail useless.

    The record is a *claim* — nothing verified that username — and §10 says so.
    But "somebody made forty attempts on erens's account" is the sentence an
    incident review needs, and it is unavailable from forty rows reading
    `anonymous`.
    """
    create_local_user(db_session, username="erens", password=PASSWORD)

    assert attempt(client, "erens", "not-the-password").status_code == 401

    (record,) = console_records("login")
    assert record["outcome"] == "denied"
    assert record["actor"] == "erens"


def test_a_sign_in_for_an_account_that_does_not_exist_is_still_recorded(
    client, auth_enabled
):
    """Otherwise account enumeration is invisible.

    The *response* deliberately does not reveal whether the username exists. The
    trail does, because it is read by the operator rather than by the caller, and
    a run of attempts against names that do not exist is the shape of an
    enumeration sweep.
    """
    assert attempt(client, "does-not-exist", PASSWORD).status_code == 401

    (record,) = console_records("login")
    assert record["outcome"] == "denied"
    assert record["actor"] == "does-not-exist"


def test_a_malformed_username_still_produces_a_findable_record(client, auth_enabled):
    assert attempt(client, "   ", PASSWORD).status_code == 401

    (record,) = console_records("login")
    assert record["actor"]
    assert record["outcome"] == "denied"


def test_a_directory_outage_is_recorded_as_failed_not_denied(
    client, auth_enabled, monkeypatch
):
    """Different fact, different verdict, different consequence.

    `denied` means the credentials were refused and counts toward a lockout.
    `failed` means this console could not complete the check — and a directory
    that is down must not lock every operator out for the throttle window on top
    of being down.
    """
    monkeypatch.setattr(settings, "ldap_enabled", True)

    def unavailable(username, password):
        raise IdentityProviderUnavailable()

    monkeypatch.setattr("app.identity.ldap.authenticate", unavailable)

    response = client.post(
        "/api/auth/login",
        json={"username": "erens", "password": PASSWORD, "source": "ldap"},
    )

    assert response.status_code == 502
    (record,) = console_records("login")
    assert record["outcome"] == "failed"
    assert "identity_provider_unavailable" in record["error"]


def test_signing_out_is_recorded(client, db_session, auth_enabled):
    create_local_user(db_session, username="erens", password=PASSWORD)
    session = attempt(client, "erens", PASSWORD).json()

    response = client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": session["csrfToken"]}
    )

    assert response.status_code == 204
    (record,) = console_records("logout")
    assert record["actor"] == "erens"
    assert record["outcome"] == "applied"


def test_a_logout_with_no_session_records_nothing(client, auth_enabled):
    """A second tab, a bookmarked call, a page reloaded after expiry.

    Nothing was revoked, so a sign-out row would say somebody signed out who was
    never signed in.
    """
    client.post("/api/auth/logout")

    assert console_records("logout") == []


def test_sign_in_records_are_separable_from_cluster_writes(client, db_session, auth_enabled):
    """The `category` column exists so this filter needs no JSON parsing.

    Filtering inside the JSON `target` would need a different SQL accessor on
    SQLite and PostgreSQL, and a filter written against one silently matches
    nothing on the other — an empty table under a filter the caller believes is
    applied.
    """
    create_local_user(db_session, username="erens", password=PASSWORD)
    attempt(client, "erens", PASSWORD)
    recorder.record(
        verb="patch",
        target={"group": "apps", "version": "v1", "resource": "deployments",
                "namespace": "prod", "name": "checkout"},
        dry_run=False,
        outcome="applied",
    )

    console = recorder.query(category="console")["items"]
    cluster = recorder.query(category="cluster")["items"]

    assert [row["verb"] for row in console] == ["login"]
    assert [row["verb"] for row in cluster] == ["patch"]
    assert recorder.query()["items"] != console


def test_console_records_are_reachable_when_a_cluster_is_selected(
    client, db_session, auth_enabled, registered_cluster
):
    """The scoping fix, and the failure it prevents.

    Console records carry no cluster. The frontend's request builder appends
    `cluster_id` to every call and drops empty values, so from a session with a
    cluster selected there was no value meaning "unscoped" — and every sign-in
    row would be permanently invisible in the UI while the page looked like it
    was working.
    """
    create_local_user(db_session, username="erens", password=PASSWORD)
    attempt(client, "erens", PASSWORD)

    scoped = client.get("/api/audit", params={"cluster_id": registered_cluster.id})
    unscoped = client.get("/api/audit", params={"cluster_id": 0})

    assert [row["verb"] for row in scoped.json()["items"]] == []
    assert [row["verb"] for row in unscoped.json()["items"]] == ["login"]


# --------------------------------------------------------------------------- #
# The throttle
# --------------------------------------------------------------------------- #

def test_repeated_failures_are_throttled(client, db_session, auth_enabled, monkeypatch):
    """Without this, POST /api/auth/login is an unmetered password oracle.

    PBKDF2's cost is tuned to be survivable per request, not per million.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 3)
    create_local_user(db_session, username="erens", password=PASSWORD)

    for _ in range(3):
        assert attempt(client, "erens", "wrong").status_code == 401

    throttled = attempt(client, "erens", "wrong")

    assert throttled.status_code == 429
    assert throttled.json()["error"] == "too_many_attempts"
    assert throttled.json()["context"]["retryAfterSeconds"] > 0


def test_a_throttled_request_does_not_reach_the_password_check(
    client, db_session, auth_enabled, monkeypatch
):
    """Otherwise the throttle still lets an attacker burn the console's CPU.

    Checked before `authenticate`, so a refused request costs one indexed COUNT
    rather than a 310,000-round PBKDF2 verification.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)
    for _ in range(2):
        attempt(client, "erens", "wrong")

    called = []
    monkeypatch.setattr(
        "app.api.auth.authenticate",
        lambda *a, **k: called.append(1),
    )

    assert attempt(client, "erens", "wrong").status_code == 429
    assert called == []


def test_the_correct_password_is_still_refused_while_throttled(
    client, db_session, auth_enabled, monkeypatch
):
    """The point of a lockout. A throttle the real password walks through is not one."""
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)
    for _ in range(2):
        attempt(client, "erens", "wrong")

    assert attempt(client, "erens", PASSWORD).status_code == 429


def test_the_throttle_is_scoped_to_one_username(client, db_session, auth_enabled, monkeypatch):
    """One account being attacked must not sign everyone else out of the console."""
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)
    create_local_user(db_session, username="dilek", password=PASSWORD)
    for _ in range(3):
        attempt(client, "erens", "wrong")

    assert attempt(client, "dilek", PASSWORD).status_code == 200


def test_a_provider_outage_does_not_count_toward_the_lockout(
    client, auth_enabled, monkeypatch
):
    """A directory that is down must not also lock everyone out.

    The reservation has to be taken before the password check for the burst
    protection to hold, so it is *released* once the failure turns out to be the
    provider's rather than the caller's.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    monkeypatch.setattr(settings, "ldap_enabled", True)

    def unavailable(username, password):
        raise IdentityProviderUnavailable()

    monkeypatch.setattr("app.identity.ldap.authenticate", unavailable)
    for _ in range(4):
        client.post(
            "/api/auth/login",
            json={"username": "erens", "password": PASSWORD, "source": "ldap"},
        )

    # Still 502 (the directory is still down), never 429: the operator has not
    # been locked out on top of the outage.
    assert client.post(
        "/api/auth/login",
        json={"username": "erens", "password": PASSWORD, "source": "ldap"},
    ).status_code == 502


def test_a_successful_sign_in_is_not_counted(client, db_session, auth_enabled, monkeypatch):
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)

    for _ in range(5):
        assert attempt(client, "erens", PASSWORD).status_code == 200


def test_failures_outside_the_window_are_forgiven(
    client, db_session, auth_enabled, monkeypatch
):
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)
    for _ in range(3):
        attempt(client, "erens", "wrong")
    assert attempt(client, "erens", PASSWORD).status_code == 429

    # The window closes; the budget is restored without an administrator having
    # to intervene. A lockout that needed manual clearing would turn every
    # mistyped password into a support request.
    monkeypatch.setattr(settings, "auth_throttle_window_seconds", 10)
    monkeypatch.setattr(
        "app.identity.throttle._window_start",
        lambda: recorder.utcnow() + __import__("datetime").timedelta(seconds=1),
    )

    assert attempt(client, "erens", PASSWORD).status_code == 200


def test_the_throttle_can_be_turned_off_deliberately(
    client, db_session, auth_enabled, monkeypatch
):
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 0)
    create_local_user(db_session, username="erens", password=PASSWORD)

    for _ in range(6):
        assert attempt(client, "erens", "wrong").status_code == 401


def test_an_unreservable_attempt_allows_the_sign_in_and_says_what_it_cost(
    client, db_session, auth_enabled, monkeypatch, caplog
):
    """The uncomfortable direction, chosen deliberately.

    A database hiccup that started refusing every sign-in would lock every
    operator out of the console during exactly the kind of incident when they
    need it, and the password check still stands behind this. So the request
    proceeds — and the log names the *consequence* ("protection is not in
    effect") rather than a generic query error, because only one of those is
    urgent.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 1)
    create_local_user(db_session, username="erens", password=PASSWORD)
    monkeypatch.setattr(throttle, "reserve", lambda actor: None)

    with caplog.at_level("ERROR"):
        assert attempt(client, "erens", PASSWORD).status_code == 200


def test_recent_attempts_reports_none_rather_than_zero_when_it_cannot_look(
    db_engine, monkeypatch
):
    """`0` says nobody has tried. `None` says we could not look. Not the same fact."""
    monkeypatch.setattr(
        "app.database.SessionLocal", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert throttle.recent_attempts("erens") is None


def test_a_concurrent_burst_cannot_walk_through_the_limit(tmp_path, monkeypatch):
    """The check-then-act race, which a plain COUNT loses outright.

    Counting alone let every request in a simultaneous burst read the same number
    before any of them had recorded anything, so all of them proceeded: measured
    at 30 concurrent guesses against a documented budget of 3, none refused,
    every one reaching PBKDF2.

    Run against a **file-backed** SQLite database rather than the suite's shared
    in-memory one, because the shared fixture hands every thread the same
    connection (`StaticPool`) and SQLite then refuses the nested transaction —
    which would make this test fail for a reason that has nothing to do with the
    property under test, and would make it pass on a design that still raced.
    """
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import Base

    url = f"sqlite:///{tmp_path / 'throttle.db'}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database, "SessionLocal", sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 3)

    burst = 12
    start = threading.Barrier(burst)
    refused: list[bool] = []
    lock = threading.Lock()

    def guess():
        start.wait()
        try:
            throttle.check("erens")
            allowed = True
        except TooManyAttempts:
            allowed = False
        with lock:
            refused.append(not allowed)

    threads = [threading.Thread(target=guess) for _ in range(burst)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    engine.dispose()

    assert sum(refused) > 0, (
        f"a burst of {burst} simultaneous attempts against a budget of 3 was not "
        "throttled at all, which is the check-then-act race the reservation "
        "design exists to close"
    )
    # The budget is delivered, not merely approached: no more attempts got past
    # the gate than the limit allows.
    assert refused.count(False) <= 3, refused


def test_the_limit_survives_an_audit_trail_that_cannot_be_written(
    client, db_session, auth_enabled, monkeypatch
):
    """The throttle must not depend on the audit INSERT succeeding.

    When it counted audit rows, an INSERT that could not happen — disk full, a
    hot standby, INSERT revoked while SELECT was retained — made the COUNT return
    a genuine, indistinguishable `0`. The login endpoint became precisely the
    unmetered password oracle the throttle exists to prevent, with no attempts
    recorded and nothing saying that counting had stopped.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 3)
    create_local_user(db_session, username="erens", password=PASSWORD)
    monkeypatch.setattr(recorder, "record_console_event", lambda **kwargs: None)

    statuses = [attempt(client, "erens", "wrong").status_code for _ in range(6)]

    assert console_records("login") == []
    assert 429 in statuses, (
        "with the audit write failing, the throttle never engaged — the limiter "
        "was reading the table that had stopped accepting rows"
    )


def test_a_successful_sign_in_clears_the_budget(
    client, db_session, auth_enabled, monkeypatch
):
    """Otherwise an operator locks themselves out of their own console.

    Reservations are taken before the password is checked, so successful
    sign-ins take them too. Without a release, `max_attempts` successful sign-ins
    inside one window would refuse the next one.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 3)
    create_local_user(db_session, username="erens", password=PASSWORD)

    for _ in range(8):
        assert attempt(client, "erens", PASSWORD).status_code == 200

    assert throttle.recent_attempts("erens") == 0


def test_a_throttled_attempt_is_recorded_like_every_other_refusal(
    client, db_session, auth_enabled, monkeypatch
):
    """The burst that trips the lockout was the part of the trail with no rows.

    `throttle.check` raises, and it was called from above the block that owns
    login's audit calls — so an attacker guessing at one account appeared
    exactly `AUTH_THROTTLE_MAX_ATTEMPTS` times and then went silent for as long
    as they kept going. "Is somebody guessing at this account" is asked about
    precisely the silent part.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)

    statuses = [attempt(client, "erens", "wrong").status_code for _ in range(6)]

    assert statuses == [401, 401, 429, 429, 429, 429]
    rows = console_records("login")
    assert len(rows) == 6
    assert {row["outcome"] for row in rows} == {"denied"}
    assert {row["actor"] for row in rows} == {"erens"}

    refused = [row for row in rows if "too_many_attempts" in (row["error"] or "")]
    assert len(refused) == 4
    # The sibling rejection's sentence, repeated verbatim: a row without it reads
    # on the audit page as a verified actor, and nothing verified this username.
    assert all(
        "the submitted username, not a verified identity" in row["detail"]
        for row in refused
    )


def test_a_throttled_attempt_does_not_hand_its_own_budget_back(
    client, db_session, auth_enabled, monkeypatch
):
    """Releasing on a refusal would reset the limit every time it was tripped.

    The release exists for a failure that turns out not to be the caller's — a
    directory outage. A lockout *is* the caller's, and clearing the reservations
    that produced it gives whoever tripped it a fresh window on demand.
    """
    monkeypatch.setattr(settings, "auth_throttle_max_attempts", 2)
    create_local_user(db_session, username="erens", password=PASSWORD)

    for _ in range(4):
        attempt(client, "erens", "wrong")

    assert throttle.recent_attempts("erens") == 4
    assert attempt(client, "erens", PASSWORD).status_code == 429
