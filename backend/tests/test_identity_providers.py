"""
Identity providers configured from the console (§12.8, ADR-0011).

Every test here is about a pair that a convenient implementation would collapse,
and on this surface collapsing one decides who can sign in to the console:

  row vs environment        A stored row is what is in effect. A disabled row
                            beats an enabled variable, and deleting the row
                            gives the deployment's own configuration back rather
                            than turning the provider off.
  enabled vs usable         What an operator switched on, versus whether a
                            sign-in can complete. The login page withholds the
                            button for the second, so a screen reporting only the
                            first says "enabled" while nothing appears.
  omitted vs empty          An omitted secret keeps the stored one; an empty one
                            clears it. A plain replace would wipe the bind
                            password every time somebody fixed a typo elsewhere.
  stored vs readable        A secret whose blob no longer decrypts is reported as
                            unreadable, never as configured — the second sends an
                            administrator to debug the directory instead of the
                            encryption key.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import crypto
from app.config import settings
from app.identity import provider_config, provider_store
from app.identity.service import create_local_user
from app.models import AuditRecord, IdentityProvider

LDAP_VALUES = {
    "url": "ldaps://stored.example.test:636",
    "user_search_base": "ou=people,dc=stored,dc=test",
    "user_search_filter": "(sAMAccountName={username})",
    "username_attribute": "sAMAccountName",
    "bind_dn": "cn=console,ou=services,dc=stored,dc=test",
    "bind_password": "directory-service-password",
    "admin_group_dn": "cn=console-admins,ou=groups,dc=stored,dc=test",
}


@pytest.fixture
def auth_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_cookie_secure", False)
    monkeypatch.setattr(settings, "ldap_enabled", False)
    return settings


@pytest.fixture
def admin(client, db_session, auth_enabled):
    """A signed-in administrator, with the CSRF header every write needs."""
    create_local_user(
        db_session,
        username="admin",
        password="correct-horse-battery-staple",
        role="admin",
    )
    response = client.post(
        "/api/auth/login",
        json={
            "username": "admin",
            "password": "correct-horse-battery-staple",
            "source": "local",
        },
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrfToken"]}


def _save(client, headers, kind="ldap", *, enabled=True, values=None):
    return client.put(
        f"/api/auth/providers/{kind}",
        json={"enabled": enabled, "values": values if values is not None else LDAP_VALUES},
        headers=headers,
    )


def _row(client, kind="ldap"):
    body = client.get("/api/auth/providers").json()
    return next(entry for entry in body["items"] if entry["name"] == kind)


# --------------------------------------------------------------------------- #
# The row is what is in effect
# --------------------------------------------------------------------------- #


def test_a_stored_row_is_what_the_sign_in_path_reads(client, admin, monkeypatch):
    """The whole point of ADR-0011: the console's copy wins over the variable.

    Without this the screen would be a form that stores something nothing reads
    — the worst possible outcome, because it looks like it worked.
    """
    monkeypatch.setattr(settings, "ldap_url", "ldaps://from-the-environment:636")

    assert _save(client, admin).status_code == 200

    cfg = provider_config.resolve("ldap")
    assert cfg.source == "database"
    assert cfg.url == "ldaps://stored.example.test:636"
    assert cfg.enabled is True
    # The secret is readable by the flow that needs it, and by nothing else.
    assert cfg.bind_password == "directory-service-password"


def test_a_disabled_row_beats_an_enabled_environment_variable(client, admin, monkeypatch):
    """An operator who switches a directory off must stay switched off.

    The alternative is a provider that comes back on because of a variable in a
    Compose file nobody in the room has read.
    """
    monkeypatch.setattr(settings, "ldap_enabled", True)
    monkeypatch.setattr(settings, "ldap_url", "ldaps://from-the-environment:636")

    assert _save(client, admin, enabled=False).status_code == 200

    cfg = provider_config.resolve("ldap")
    assert cfg.enabled is False
    assert cfg.source == "database"
    assert client.get("/api/auth/config").json()["ldapEnabled"] is False


def test_deleting_a_row_falls_back_to_the_environment_rather_than_off(
    client, admin, monkeypatch
):
    """Delete means "go back to what the deployment ships with".

    Falling back to *off* instead would make a delete the one action that can
    silently remove the only way into a console, and the operator would have
    read the word "delete" as "delete my edits".
    """
    monkeypatch.setattr(settings, "ldap_enabled", True)
    monkeypatch.setattr(settings, "ldap_url", "ldaps://from-the-environment:636")
    _save(client, admin, enabled=False)
    assert provider_config.resolve("ldap").enabled is False

    deleted = client.delete("/api/auth/providers/ldap", headers=admin)

    assert deleted.status_code == 204
    cfg = provider_config.resolve("ldap")
    assert cfg.source == "environment"
    assert cfg.enabled is True
    assert cfg.url == "ldaps://from-the-environment:636"


def test_deleting_what_was_never_stored_is_a_404(client, admin):
    """"The stored configuration is gone" and "there was nothing stored" are
    different answers, and only one of them changed how this console
    authenticates."""
    response = client.delete("/api/auth/providers/oidc", headers=admin)
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_the_login_page_offers_a_directory_configured_in_the_console(
    client, admin, monkeypatch
):
    """§12.1 reads the resolved configuration too.

    A directory an administrator configured here and a login page that still
    says the deployment has none is the same class of failure as a form that
    stores something nothing reads — and it is the half an operator sees first.
    """
    monkeypatch.setattr(settings, "ldap_enabled", False)
    _save(client, admin)

    body = client.get("/api/auth/config").json()

    assert body["ldapEnabled"] is True
    assert "ldap" in body["methods"]
    # Still no configuration detail on the unauthenticated endpoint.
    assert "stored.example.test" not in client.get("/api/auth/config").text


def test_one_row_per_kind_no_matter_how_many_times_it_is_saved(client, admin, db_session):
    """`kind` is unique in the table, so the storage layer is what enforces it.

    §12.4's refusal was about two providers of one kind colliding on a subject;
    it cannot arise if a second one cannot exist.
    """
    _save(client, admin)
    _save(client, admin, values={**LDAP_VALUES, "url": "ldaps://second.example.test:636"})

    db_session.expire_all()
    rows = db_session.scalars(select(IdentityProvider)).all()
    assert len(rows) == 1
    assert rows[0].config["url"] == "ldaps://second.example.test:636"


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_a_stored_secret_is_never_returned_and_never_stored_in_the_clear(
    client, admin, db_session
):
    _save(client, admin)

    listing = client.get("/api/auth/providers")

    assert "directory-service-password" not in listing.text
    row = _row(client)
    assert row["secrets_stored"] == ["bind_password"]
    assert row["secrets_unreadable"] is False
    assert "bind_password" not in row["values"]
    db_session.expire_all()
    stored = db_session.scalar(select(IdentityProvider))
    assert "directory-service-password" not in (stored.secrets_encrypted or "")
    assert "directory-service-password" not in str(stored.config)


def test_an_omitted_secret_is_kept_and_an_empty_one_is_cleared(client, admin):
    """The form cannot show a stored password, so it cannot send one back.

    A plain replace would therefore wipe the bind password every time somebody
    corrected a typo in the search base — and the next sign-in would report a
    credential rejection from a directory that is fine.
    """
    _save(client, admin)

    without_secret = {name: value for name, value in LDAP_VALUES.items()
                      if name != "bind_password"}
    _save(client, admin, values={**without_secret, "email_attribute": "mail"})
    assert provider_config.resolve("ldap").bind_password == "directory-service-password"

    _save(client, admin, values={**LDAP_VALUES, "bind_password": ""})
    assert provider_config.resolve("ldap").bind_password == ""
    assert _row(client)["secrets_stored"] == []


def test_a_secret_that_no_longer_decrypts_is_reported_rather_than_shown_as_set(
    client, admin, monkeypatch
):
    """The encryption key changed. Two wrong answers, opposite directions.

    Reporting the password as stored sends an administrator to debug the
    directory; silently sending an empty one makes the directory report a
    credential rejection. So the row says the stored secrets are unreadable, and
    the field falls back to the environment — which is a real configuration.
    """
    _save(client, admin)
    monkeypatch.setattr(settings, "ldap_bind_password", settings.ldap_bind_password)

    def _fail(_ciphertext):
        raise ValueError("wrong key")

    monkeypatch.setattr(crypto, "decrypt", _fail)

    row = _row(client)
    assert row["secrets_unreadable"] is True
    assert row["secrets_stored"] == []
    # The rest of the row still resolves: one unreadable secret does not cost
    # the whole configuration.
    assert row["endpoint"] == "ldaps://stored.example.test:636"


# --------------------------------------------------------------------------- #
# Validation, at the write
# --------------------------------------------------------------------------- #


def test_plaintext_ldap_is_refused_when_it_is_saved_not_when_somebody_signs_in(
    client, admin
):
    """`app.identity.ldap` refuses this at bind time. So does the save.

    Otherwise the console reports a directory as configured and the refusal
    surfaces on somebody else's login attempt, long after the operator who
    typed it has gone.
    """
    response = _save(
        client, admin, values={**LDAP_VALUES, "url": "ldap://plaintext.example.test:389"}
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert body["context"]["field"] == "url"
    assert "clear text" in body["message"]


def test_a_missing_required_field_is_refused_only_when_the_row_is_enabled(client, admin):
    """Half-filling a provider and leaving it off is how a configuration is staged."""
    incomplete = {name: value for name, value in LDAP_VALUES.items()
                  if name != "user_search_base"}

    refused = _save(client, admin, enabled=True, values=incomplete)
    assert refused.status_code == 422
    assert refused.json()["context"]["field"] == "user_search_base"

    staged = _save(client, admin, enabled=False, values=incomplete)
    assert staged.status_code == 200
    assert staged.json()["enabled"] is False


def test_a_field_the_spec_does_not_declare_is_refused(client, admin):
    """A JSON column is not a free-for-all: what nothing reads cannot be stored."""
    response = _save(client, admin, values={**LDAP_VALUES, "sneaky_field": "x"})
    assert response.status_code == 422
    assert response.json()["context"]["field"] == "sneaky_field"


def test_an_unknown_kind_is_refused_before_it_reaches_the_database(client, admin):
    response = client.put(
        "/api/auth/providers/kerberos",
        json={"enabled": True, "values": {}},
        headers=admin,
    )
    assert response.status_code == 422


def test_an_https_issuer_is_required_for_openid_connect(client, admin):
    response = _save(
        client, admin, kind="oidc", enabled=True,
        values={"issuer": "http://idp.example.test", "client_id": "console",
                "username_claim": "preferred_username"},
    )
    assert response.status_code == 422
    assert response.json()["context"]["field"] == "issuer"


# --------------------------------------------------------------------------- #
# What the screen reports
# --------------------------------------------------------------------------- #


def test_enabled_and_usable_are_reported_separately(client, admin, monkeypatch):
    """Switched on, and still not offered on the login page, is a real state.

    A provider missing a required value gets no button — a button that leads to
    an error reads as a broken console rather than an unconfigured one — so a
    screen that reported only `enabled` would say "on" while nothing appeared
    and nothing anywhere explained the gap.
    """
    monkeypatch.setattr(settings, "oidc_enabled", True)
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.test")
    monkeypatch.setattr(settings, "oidc_client_id", "")

    row = _row(client, "oidc")

    assert row["enabled"] is True
    assert row["usable"] is False
    assert row["missing"] == ["client_id"]
    assert client.get("/api/auth/config").json()["oidcEnabled"] is False


def test_the_form_is_described_by_the_backend_that_reads_it(client, admin):
    """The form is rendered from `fields`, so a field the flow reads cannot be
    missing from the screen that configures it."""
    row = _row(client, "saml")
    names = [field["name"] for field in row["fields"]]

    assert names == [field.name for field in provider_config.spec("saml").fields]
    assert row["caveat"] and "AUTH_COOKIE_SECURE" in row["caveat"]
    certificate = next(field for field in row["fields"] if field["name"] == "idp_certificate")
    assert certificate["type"] == "text"
    assert certificate["required"] is True


# --------------------------------------------------------------------------- #
# Who may do this, and what the trail says
# --------------------------------------------------------------------------- #


def test_configuring_a_provider_is_administrator_only(client, db_session, auth_enabled):
    create_local_user(
        db_session, username="member", password="correct-horse-battery-staple"
    )
    login = client.post(
        "/api/auth/login",
        json={"username": "member", "password": "correct-horse-battery-staple",
              "source": "local"},
    )
    headers = {"X-CSRF-Token": login.json()["csrfToken"]}

    for response in (
        client.get("/api/auth/providers"),
        client.put("/api/auth/providers/ldap",
                   json={"enabled": True, "values": LDAP_VALUES}, headers=headers),
        client.delete("/api/auth/providers/ldap", headers=headers),
    ):
        assert response.status_code == 403
        assert response.json()["error"] == "permission_denied"


def test_every_terminal_state_is_audited_and_no_row_carries_a_secret(
    client, admin, db_session
):
    """Including the refusals: "who tried to point our directory somewhere else"
    is the question this table is read for."""
    _save(client, admin)
    _save(client, admin, values={**LDAP_VALUES, "email_attribute": "mail"})
    _save(client, admin, values={**LDAP_VALUES, "url": "ldap://plaintext.example.test"})
    client.delete("/api/auth/providers/ldap", headers=admin)

    records = db_session.scalars(
        select(AuditRecord)
        .where(AuditRecord.category == "console")
        .order_by(AuditRecord.id)
    ).all()
    providers = [r for r in records if r.target["resource"] == "identity_providers"]
    assert [(r.verb, r.outcome) for r in providers] == [
        ("create", "applied"),
        ("patch", "applied"),
        ("patch", "denied"),
        ("delete", "applied"),
    ]
    assert all(r.target["name"] == "ldap" for r in providers)
    assert all(r.actor == "admin" for r in providers)
    # The detail names the fields that were submitted, never their values.
    assert all("directory-service-password" not in (r.detail or "") for r in providers)
    assert "bind_password" in providers[0].detail


def test_the_bind_path_reads_the_stored_row_and_says_where_to_fix_it(
    client, admin, db_session, monkeypatch
):
    """The seam that matters: `ldap.authenticate` reads the row, not the variable.

    Written through the store rather than through the endpoint, because the
    endpoint refuses a plaintext URL and this test is about the *reader*. The
    refusal it provokes then proves two things at once: the bind path resolved
    the stored configuration, and the hint points at the console rather than at
    LDAP_URL — telling somebody to edit a variable while a stored row is in
    effect sends them to change something that changes nothing.
    """
    from app.errors import IdentityProviderUnavailable
    from app.identity import ldap as ldap_provider

    monkeypatch.setattr(settings, "ldap_url", "ldaps://from-the-environment:636")
    provider_store.upsert(
        db_session,
        kind="ldap",
        enabled=True,
        config={**LDAP_VALUES, "url": "ldap://stored-plaintext.example.test:389",
                "start_tls": False, "bind_password": None},
        secrets={},
    )

    with pytest.raises(IdentityProviderUnavailable) as caught:
        ldap_provider.authenticate("somebody", "a-password")

    assert "clear text" in caught.value.message
    assert "Identity providers" in (caught.value.hint or "")
    assert "LDAP_URL" not in caught.value.message


def test_the_store_is_the_only_thing_that_touches_the_table(client, admin):
    """A sanity check on the seam: the row the API wrote is the row the resolver
    reads, through `provider_store` in both directions."""
    _save(client, admin)
    row = provider_store.get("ldap")
    assert row is not None
    assert row.kind == "ldap"
    assert provider_store.decrypt_secrets(row) == {
        "bind_password": "directory-service-password"
    }
    assert provider_store.secrets_readable(row) is True
