"""
Acting as the operator instead of as the console (ADR-0007).

This is the one feature in the tree whose *absence* was documented for months
before it existed, and the ADR that documented it set six conditions rather than
suggestions. Each has a test here, named for the condition, because the failure
mode of this feature is not a broken page — it is a console that quietly acts
with more authority than the person using it, which looks exactly like a console
that works.

The pairs that matter, and what collapsing each one costs:

  impersonated vs not     A read that fell back to the ServiceAccount shows an
                          operator data their own RBAC forbids. The console
                          refuses instead, and the refusal names why.

  absent vs empty groups  An issuer that sent no groups claim is not an issuer
                          that said "none". Impersonating on the second reading
                          strips every group-derived permission the operator
                          holds and reports the result as permissions they lack.

  repeated vs joined      `Impersonate-Group: a,b` is one group of that literal
                          name to Go's http.Header. The request succeeds, the
                          authorizer sees a group nobody is in, and the denials
                          look like a cluster problem.

  actor vs subject        `actor` is who used this console. The impersonated
                          user is the name the API server itself evaluated. An
                          audit row carrying only the first cannot be joined to
                          the cluster's own trail.

  hashed vs verifiable    Adding a column to the audit hash would break every
                          row written before it existed — a false tamper alarm
                          delivered by a schema change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from urllib3 import HTTPHeaderDict

from app.audit import integrity, recorder
from app.config import settings
from app.errors import ImpersonationUnavailable, Invalid, RBACDenied
from app.identity.service import Principal
from app.k8s import impersonation
from app.k8s.client import _cluster_api_client, manager
from app.k8s.context import reset_current_principal, set_current_principal
from app.models import AuditRecord
from tests.conftest import obj

USER = "Alice@example.com"
GROUPS = ("platform-admins", "oncall")


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def principal(
    *, auth_source="oidc", idp_username=USER, idp_groups=GROUPS, username="alice@example.com",
) -> Principal:
    return Principal(
        id=7, username=username, display_name="Alice", email=USER,
        role="admin", auth_source=auth_source,
        idp_username=idp_username, idp_groups=idp_groups,
    )


def cluster(*, enabled=True):
    return obj(
        id=1, name="prod-eu", impersonation_enabled=enabled, updated_at=None,
    )


@pytest.fixture
def oidc_console(monkeypatch):
    """A deployment that authenticates operators through OpenID Connect."""
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", True)
    return settings


@pytest.fixture(autouse=True)
def _clean_decision():
    """No decision leaks between tests; each one pins its own."""
    token = impersonation.set_current(None)
    yield
    impersonation.reset_current(token)


# --------------------------------------------------------------------------- #
# Condition 1 — per-cluster, opt-in, off by default
# --------------------------------------------------------------------------- #

def test_a_cluster_that_did_not_ask_is_not_impersonated(oidc_console):
    """The default, and the state of every cluster registered before ADR-0007."""
    decision = impersonation.decide(cluster(enabled=False), principal())

    assert decision.active is False
    assert decision.subject == impersonation.SERVICE_ACCOUNT_SUBJECT
    assert decision.impersonation is None


def test_a_null_flag_is_off_rather_than_a_third_state(oidc_console):
    """`impersonation_enabled` is nullable because it was added to a populated
    table. NULL means "registered before the setting existed", and the only safe
    reading of that is off."""
    assert impersonation.decide(obj(impersonation_enabled=None), principal()).active is False


def test_the_flag_is_surfaced_like_skip_tls_verify(client):
    """A setting that changes who the cluster thinks is asking can never be
    silently in effect."""
    created = client.post("/api/clusters", json={
        "name": "prod-eu", "api_server": "https://api.example:6443",
        "token": "t", "impersonation_enabled": False,
    })

    assert created.json()["impersonation_enabled"] is False


def test_turning_it_on_without_oidc_is_refused_at_the_form(client, monkeypatch):
    """Accepted, it would produce a cluster that refuses every operator at the
    request — accurate, and arriving on a page belonging to somebody who did not
    change the setting and cannot see it."""
    monkeypatch.setattr(settings, "auth_enabled", False)

    response = client.post("/api/clusters", json={
        "name": "prod-eu", "api_server": "https://api.example:6443",
        "token": "t", "impersonation_enabled": True,
    })

    assert response.status_code == 422
    body = response.json()
    assert body["context"]["field"] == "impersonation_enabled"
    assert "OpenID Connect" in body["message"]


def test_turning_it_on_with_oidc_is_accepted(oidc_console):
    """The validator directly rather than through the endpoint: with
    `auth_enabled` on, the endpoint answers 401 before it ever reaches this
    check, and a test that worked around that would be testing the session
    fixture instead of the rule."""
    from app.api.clusters import _validate_impersonation

    _validate_impersonation(True)  # does not raise


def test_ldap_only_authentication_is_still_refused(monkeypatch):
    """`auth_enabled` alone is not enough: a console that authenticates only
    against LDAP has no issuer a cluster could also believe."""
    from app.api.clusters import _validate_impersonation

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "oidc_enabled", False)

    with pytest.raises(Invalid) as caught:
        _validate_impersonation(True)

    assert caught.value.context["field"] == "impersonation_enabled"


# --------------------------------------------------------------------------- #
# Condition 2 — OIDC sessions only, and a refusal rather than a fallback
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("source", ["local", "ldap"])
def test_a_password_session_cannot_become_a_cluster_identity(oidc_console, source):
    """If this console's own user table could cause a request to arrive as
    `alice`, that table has become an identity provider the cluster trusts."""
    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), principal(auth_source=source))

    assert caught.value.context["reason"] == "auth_source"
    assert "single sign-on" in caught.value.message
    assert "user table" in caught.value.hint


def test_legacy_proxy_mode_cannot_impersonate(monkeypatch):
    """`X-K8Boss-User` is advisory and caller-controlled. Impersonating from it
    would let anyone who can reach the port pick a cluster identity."""
    monkeypatch.setattr(settings, "auth_enabled", False)

    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), None)

    assert caught.value.context["reason"] == "auth_disabled"
    assert "X-K8Boss-User" in caught.value.hint


def test_an_anonymous_request_is_refused_not_served_as_the_console(oidc_console):
    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), None)

    assert caught.value.context["reason"] == "no_session"


def test_a_session_predating_the_feature_is_refused_by_name(oidc_console):
    """Sessions created before ADR-0007 carry no issuer username, and this
    console will not invent one from its own normalised account name."""
    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), principal(idp_username=None))

    assert caught.value.context["reason"] == "no_idp_username"
    assert "Sign out and back in" in caught.value.hint


def test_the_refusal_is_not_rbac_denied(oidc_console):
    """403 with its own code: the operator's cluster permissions are not the
    problem, and reporting a denial sends them to widen a ClusterRole that was
    already correct."""
    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), principal(auth_source="local"))

    assert caught.value.code == "impersonation_unavailable"
    assert caught.value.http_status == 403
    assert not isinstance(caught.value, RBACDenied)


# --------------------------------------------------------------------------- #
# Condition 3 — absent groups refuses, empty groups does not
# --------------------------------------------------------------------------- #

def test_an_absent_groups_claim_refuses(oidc_console):
    """The distinction `identity_from_claims` keeps, now load-bearing on a
    cluster: impersonating with no groups when the claim was merely omitted
    strips every group-derived permission the operator holds."""
    with pytest.raises(ImpersonationUnavailable) as caught:
        impersonation.decide(cluster(), principal(idp_groups=None))

    assert caught.value.context["reason"] == "groups_absent"
    assert "not an empty one" in caught.value.hint


def test_an_empty_groups_claim_impersonates(oidc_console):
    """"This person is in no groups" is a real answer and is honoured as one."""
    decision = impersonation.decide(cluster(), principal(idp_groups=()))

    assert decision.active is True
    assert decision.impersonation.groups == (impersonation.AUTHENTICATED_GROUP,)


# --------------------------------------------------------------------------- #
# The identity that is sent
# --------------------------------------------------------------------------- #

def test_the_issuers_username_is_sent_not_the_consoles_normalised_one(oidc_console):
    """A cluster whose --oidc-username-claim yields `Alice@example.com` does not
    know anybody called `alice@example.com`."""
    decision = impersonation.decide(cluster(), principal())

    assert decision.impersonation.username == "Alice@example.com"
    assert decision.subject == "Alice@example.com"


def test_system_authenticated_is_added_because_the_api_server_would_have(oidc_console):
    """The one group sent that the issuer did not state. The API server attaches
    it to every request it authenticates itself and does *not* attach it to an
    impersonated one — so omitting it produces accurate denials for permissions
    the operator demonstrably holds, and the reliable end of that is a
    ClusterRole widened to fix a problem that was never RBAC."""
    decision = impersonation.decide(cluster(), principal())

    assert decision.impersonation.groups == (
        "platform-admins", "oncall", "system:authenticated",
    )


def test_an_issuer_that_already_states_it_does_not_get_it_twice(oidc_console):
    decision = impersonation.decide(
        cluster(), principal(idp_groups=("system:authenticated", "oncall")),
    )

    assert decision.impersonation.groups == ("system:authenticated", "oncall")


def test_no_uid_or_extra_headers_are_invented(oidc_console):
    """Both exist in the API. This console has no issuer-stated value for either,
    and inventing one is what the ADR rejects."""
    headers = impersonation.decide(cluster(), principal()).impersonation.apply({})

    assert not [key for key in headers if key.lower().startswith("impersonate-uid")]
    assert not [key for key in headers if key.lower().startswith("impersonate-extra")]


# --------------------------------------------------------------------------- #
# The headers on the wire
# --------------------------------------------------------------------------- #

def test_groups_are_repeated_headers_and_never_comma_joined(oidc_console):
    """`Impersonate-Group: a,b` is a single group of that literal name to Go's
    http.Header. The request succeeds, the authorizer sees a group nobody is in,
    and the denials look like a cluster problem."""
    headers = impersonation.decide(cluster(), principal()).impersonation.apply(
        {"Accept": "application/json"},
    )

    assert isinstance(headers, HTTPHeaderDict)
    assert headers.getlist("Impersonate-Group") == [
        "platform-admins", "oncall", "system:authenticated",
    ]
    assert headers["Impersonate-User"] == "Alice@example.com"
    # The caller's own headers survive.
    assert headers["Accept"] == "application/json"
    # And the trap: anything that normalises these back to a plain dict
    # reintroduces exactly the bug above.
    assert "," in dict(headers)["Impersonate-Group"]


@pytest.fixture
def sent_headers(monkeypatch):
    """Capture what the real transport wrapper would put on the wire.

    ``_cluster_api_client`` binds ``rest_client.request`` at build time, so the
    recorder is installed on the class *before* the wrapper closes over it. That
    exercises the shipped wrapper rather than a re-implementation of it — this
    is the one function that decides whether a call carries somebody's identity,
    and a test that reimplemented its logic would agree with itself forever.
    """
    from kubernetes.client.rest import RESTClientObject

    captured: list[Any] = []

    def recorder(self, method, url, query_params=None, headers=None, **kwargs):
        captured.append(headers)
        return None

    monkeypatch.setattr(RESTClientObject, "request", recorder)

    def send(*, impersonatable: bool, positional: bool = False):
        captured.clear()
        api_client = _cluster_api_client(impersonatable=impersonatable)
        if positional:
            api_client.rest_client.request(
                "GET", "https://api.example/api/v1/pods", None,
                {"Accept": "application/json"},
            )
        else:
            api_client.rest_client.request(
                "GET", "https://api.example/api/v1/pods",
                headers={"Accept": "application/json"},
            )
        return captured[-1]

    return send


def test_an_eligible_transport_carries_the_headers(oidc_console, sent_headers):
    """Merged in the one wrapper every call passes through, so a new endpoint
    cannot forget and a new typed client cannot bypass."""
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    headers = sent_headers(impersonatable=True)

    assert headers["Impersonate-User"] == USER
    assert headers.getlist("Impersonate-Group") == [
        "platform-admins", "oncall", "system:authenticated",
    ]


def test_headers_passed_positionally_are_not_a_way_round_it(oidc_console, sent_headers):
    """`headers` is the fourth positional parameter. A positional call that
    silently skipped impersonation would make one code path act as the console
    on a cluster the operator believes is acting as them."""
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    headers = sent_headers(impersonatable=True, positional=True)

    assert headers["Impersonate-User"] == USER
    assert headers["Accept"] == "application/json"


def test_a_transport_that_may_never_impersonate_ignores_the_decision(
    oidc_console, sent_headers,
):
    """Structural rather than conditional: the connection test's unsaved
    credentials and the local kubeconfig cannot assert somebody's cluster
    identity no matter what any contextvar says."""
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    headers = sent_headers(impersonatable=False)

    assert "Impersonate-User" not in headers


def test_a_cluster_that_did_not_ask_sends_no_headers(oidc_console, sent_headers):
    impersonation.set_current(impersonation.decide(cluster(enabled=False), principal()))

    assert "Impersonate-User" not in sent_headers(impersonatable=True)


# --------------------------------------------------------------------------- #
# Condition 4 — no silent fallback, and the exemptions are a list
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "who, reason",
    [
        (principal(auth_source="local"), "auth_source"),
        (principal(idp_groups=None), "groups_absent"),
        (None, "no_session"),
    ],
)
def test_a_refused_session_never_receives_a_transport(
    oidc_console, monkeypatch, who, reason,
):
    """The whole of "refuses rather than falls back": `get_clients` raises
    before any bundle is returned, so there is no object a caller could have
    used as the console — not a degraded one, not a cached one, none."""
    monkeypatch.setattr(manager, "_load_cluster", lambda cluster_id: cluster())
    token = set_current_principal(who)
    try:
        with pytest.raises(ImpersonationUnavailable) as caught:
            manager.get_clients(1)
    finally:
        reset_current_principal(token)

    assert caught.value.context["reason"] == reason


def test_an_eligible_session_does_receive_one(oidc_console, monkeypatch):
    """The other half: the refusal is about the session, not about the feature."""
    monkeypatch.setattr(manager, "_load_cluster", lambda cluster_id: cluster())
    monkeypatch.setattr(manager, "create_client", lambda c, **kw: obj(
        cache_key="k", close=lambda: None,
    ))
    token = set_current_principal(principal())
    try:
        manager.get_clients(1)
    finally:
        reset_current_principal(token)

    assert impersonation.get_current().subject == USER


def test_as_service_account_suppresses_and_restores(oidc_console):
    """The only suppression in the tree, and it is scoped to a block."""
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    assert "Impersonate-User" in impersonation.headers_for_current_request({})
    with impersonation.as_service_account("a documented console call"):
        assert "Impersonate-User" not in impersonation.headers_for_current_request({})
    assert "Impersonate-User" in impersonation.headers_for_current_request({})


def test_the_service_account_exemptions_are_exactly_these(oidc_console):
    """ADR-0007 requires the calls that stay as the console be a **list** rather
    than an emergent property of whoever wrote the last endpoint. This test is
    that list. A new use of `as_service_account` fails here until it is added
    with a reason, which makes the exemption a reviewed act.

    The two that are not `as_service_account` — the connection test and the
    local kubeconfig — are structural instead: they build transports that may
    never impersonate at all, and `test_a_transport_that_may_never_impersonate…`
    covers them.
    """
    import subprocess

    found = subprocess.run(
        ["grep", "-rn", "as_service_account(", "app/"],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    call_sites = sorted(
        line.split(":")[0] for line in found
        if "app/k8s/impersonation.py" not in line
    )

    assert call_sites == ["app/resources/catalog.py"], (
        "A new ServiceAccount exemption was added. ADR-0007 requires each one be "
        "named and justified; add it here with the reason it cannot be "
        "impersonated."
    )


# --------------------------------------------------------------------------- #
# Condition 5 — the audit row records both, and the preflight names its subject
# --------------------------------------------------------------------------- #

def test_the_audit_row_records_the_console_user_and_the_cluster_identity(
    db_engine, oidc_console,
):
    """Two names answering two questions. `actor` is who used this console and
    is a name only this application can vouch for; `impersonated_user` is what
    the API server evaluated and wrote into its own log, so an incident review
    can join the two trails by a value neither side invented."""
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    recorder.record(
        verb="patch", target={"resource": "deployments", "name": "api"},
        dry_run=False, outcome="applied",
    )

    row = recorder.query(limit=1)["items"][0]
    assert row["impersonated_user"] == USER
    assert row["actor"] != row["impersonated_user"], (
        "the console's actor and the cluster's subject are different facts"
    )


def test_a_non_impersonated_write_records_null_rather_than_the_actor(
    db_engine, oidc_console,
):
    """NULL means "this console acted as itself", which is a fact. Defaulting it
    to `actor` would claim the API server saw a name it never saw — the
    attribution ADR-0003 refuses to manufacture."""
    impersonation.set_current(impersonation.decide(cluster(enabled=False), principal()))

    recorder.record(
        verb="patch", target={"resource": "deployments", "name": "api"},
        dry_run=False, outcome="applied",
    )

    assert recorder.query(limit=1)["items"][0]["impersonated_user"] is None


def test_the_impersonated_identity_is_not_taken_as_an_argument(db_engine, oidc_console):
    """Read from the request context like `actor`, and for the same reason: a
    caller that can pass the identity a write was made as can pass the wrong
    one."""
    import inspect

    assert "impersonated_user" not in inspect.signature(recorder.record).parameters


def test_a_preflight_result_says_whose_permission_it_answered_for(
    fake_k8s, oidc_console,
):
    """A SelfSubjectAccessReview answers "may *this credential*". Until ADR-0007
    that credential was always the console's — correct about the wrong subject,
    and §11.4's disabled buttons could not say whose permission was missing."""
    from app.admin import preflight

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason="", evaluation_error=None, denied=False,
    )))
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    assert preflight.check("patch", "apps", "deployments")["subject"] == USER


def test_a_denial_names_the_subject_it_was_refused_for(fake_k8s, oidc_console):
    """"Cannot patch deployments" sends whoever reads it to check the console's
    ServiceAccount, which on an impersonating cluster is not the subject the
    authorizer refused."""
    from app.admin import preflight

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=False, reason="no RBAC policy matched", evaluation_error=None, denied=True,
    )))
    impersonation.set_current(impersonation.decide(cluster(), principal()))

    with pytest.raises(RBACDenied) as caught:
        preflight.require("patch", "apps", "deployments", namespace="prod")

    assert caught.value.message.startswith(f"{USER} cannot patch")
    assert caught.value.context["subject"] == USER


def test_without_impersonation_the_subject_is_the_console(fake_k8s):
    """The state every cluster was already in, now written down rather than
    silently true."""
    from app.admin import preflight

    fake_k8s.authorization_v1.returns("create_self_subject_access_review", obj(status=obj(
        allowed=True, reason="", evaluation_error=None, denied=False,
    )))

    result = preflight.check("patch", "apps", "deployments")
    assert result["subject"] == impersonation.SERVICE_ACCOUNT_SUBJECT


# --------------------------------------------------------------------------- #
# The hash chain — a new column must not become a false tamper alarm
# --------------------------------------------------------------------------- #

def test_a_row_written_before_the_column_hashes_exactly_as_it_did(db_engine):
    """The failure this avoids: appending a name to HASHED_FIELDS changes the
    computation for *every* row ever written, including those whose stored
    digests were computed without it — and the whole table verifies as `broken`.
    That is the false "this row was modified" alarm `_canonical` exists to
    prevent, arriving by way of a schema change."""
    row = AuditRecord(
        ts=recorder.utcnow(), category="cluster", actor="alice", source_ip=None,
        cluster_id=1, cluster_name="prod-eu", verb="patch", target={},
        dry_run=False, outcome="applied", detail=None, diff_digest=None, error=None,
        impersonated_user=None,
    )
    with_column = integrity.compute_event_hash(row, integrity.GENESIS)

    payload = {
        field: integrity._canonical(getattr(row, field, None))
        for field in integrity.HASHED_FIELDS
    }
    payload["prev_hash"] = integrity.GENESIS
    import hashlib

    legacy = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        .encode("utf-8")
    ).hexdigest()

    assert with_column == legacy


@pytest.mark.parametrize(
    "before, after",
    [
        (None, "alice@example.com"),   # attribution invented on an old row
        ("alice@example.com", None),   # attribution erased
        ("alice@example.com", "bob@example.com"),  # attribution moved
    ],
)
def test_changing_the_impersonated_user_breaks_the_hash(db_engine, before, after):
    """The omission is not a hole. Every mutation of the field crosses the
    boundary — a key appears, disappears or changes value — and all three
    recompute to something other than what was stored."""
    row = AuditRecord(
        ts=recorder.utcnow(), category="cluster", actor="alice", source_ip=None,
        cluster_id=1, cluster_name="prod-eu", verb="patch", target={},
        dry_run=False, outcome="applied", detail=None, diff_digest=None, error=None,
        impersonated_user=before,
    )
    stored = integrity.compute_event_hash(row, integrity.GENESIS)

    row.impersonated_user = after
    assert integrity.compute_event_hash(row, integrity.GENESIS) != stored


def test_the_whole_trail_still_verifies_with_impersonated_rows_in_it(
    db_engine, oidc_console,
):
    """The end-to-end version: a chain mixing impersonated and console rows
    verifies intact, which is the property the omission rule exists to keep."""
    impersonation.set_current(impersonation.decide(cluster(enabled=False), principal()))
    recorder.record(verb="patch", target={"resource": "deployments"},
                    dry_run=False, outcome="applied")
    impersonation.set_current(impersonation.decide(cluster(), principal()))
    recorder.record(verb="delete", target={"resource": "pods"},
                    dry_run=False, outcome="applied")

    verdict = recorder.verify_chain()
    assert verdict["status"] == "intact", verdict


def test_the_connection_test_is_not_impersonated(monkeypatch):
    """ADR-0007 names this exemption, and it is structural rather than a
    contextvar: `get_clients_for_cluster` asks for a transport that may never
    impersonate.

    The failure it prevents is quiet. `POST /clusters/{id}/test` answers "can
    this console reach and authenticate to this cluster, and what may it do
    there" — a question about the **stored credential**. Run as the operator it
    reports the operator's permissions instead, so a cluster whose ServiceAccount
    token had expired would still test clean for every operator whose own RBAC
    happened to be fine, and the thing being tested goes unexercised.
    """
    asked: list[bool] = []
    monkeypatch.setattr(
        manager, "create_client",
        lambda c, *, impersonatable=True: asked.append(impersonatable),
    )

    manager.get_clients_for_cluster(cluster())

    assert asked == [False]


@pytest.mark.parametrize("stored", ["not json at all", '{"groups": []}', "17"])
def test_an_unreadable_groups_blob_is_absent_rather_than_empty(stored):
    """Both readings are wrong and only one is dangerous.

    Absent refuses impersonation and says why. Empty impersonates with no groups
    and reports the operator's real permissions as denied — the same failure as
    an omitted claim, arriving from this console's own storage instead of from
    the issuer. So a value this function cannot parse degrades toward the
    refusal.
    """
    from app.identity.service import _decode_groups

    assert _decode_groups(stored) is None


def test_a_stored_empty_list_really_is_empty():
    """The other half: `[]` round-trips as a real answer, not as a gap."""
    from app.identity.service import _decode_groups, _encode_groups

    assert _decode_groups(_encode_groups(())) == ()
    assert _decode_groups(_encode_groups(("a", "b"))) == ("a", "b")
    assert _encode_groups(None) is None
