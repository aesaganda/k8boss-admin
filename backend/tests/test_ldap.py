"""
LDAP search-and-bind.

Four properties are worth a test here, and they are the four that go wrong
quietly:

* **Credentials are never put on the wire in clear text.** The refusal happens
  before a socket is opened, so a misconfigured deployment cannot leak a
  directory password once and then be corrected.
* **The password proves itself by binding as the entry's own DN.** Comparing
  anything client-side, or accepting a successful *search* as proof, is an
  authentication bypass.
* **A missing ``memberOf`` is ``None``, not ``()``.** An empty tuple says "this
  person is in no groups", which :mod:`app.identity.service` writes over the
  stored role — silently demoting a directory administrator on any login where
  the attribute did not come back.
* **Ambiguity is a refusal.** Two entries matching one username means the filter
  is wrong; binding as whichever came back first authenticates one person's
  password against another person's account.

The directory is faked at the ``ldap3`` module boundary rather than mocked
inside ``app.identity.ldap``, so the real filter escaping, the real ``Tls``
object and the real call sequence (open, StartTLS, bind, search, bind, unbind)
are all exercised.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError

from app.config import settings
from app.errors import IdentityProviderUnavailable
from app.identity import ldap as ldap_module


class FakeEntry:
    """An ldap3 search result: attribute presence is distinct from its value."""

    def __init__(self, dn: str, **attributes: list[str]):
        self.entry_dn = dn
        self.entry_attributes = list(attributes)
        self._attributes = attributes

    def __getitem__(self, attribute: str):
        values = self._attributes[attribute]
        return SimpleNamespace(value=values[0] if values else None, values=list(values))


class FakeConnection:
    """Records everything the module does with one connection."""

    def __init__(self, directory, user, password, **kwargs):
        self.directory = directory
        self.user = user
        self.password = password
        self.kwargs = kwargs
        self.searches: list[dict] = []
        self.bound = False
        self.unbound = False
        self.started_tls = False
        self.entries: list[FakeEntry] = []

    def open(self) -> bool:
        return self.directory.open_ok

    def start_tls(self) -> bool:
        self.started_tls = self.directory.start_tls_ok
        return self.directory.start_tls_ok

    def bind(self) -> bool:
        # The search account and the user being authenticated are the same call
        # on two different connections; which one this is decides the answer.
        allowed = (
            self.directory.user_bind_ok
            if self.directory.connections[0] is not self
            else self.directory.search_bind_ok
        )
        self.bound = bool(allowed)
        return self.bound

    def search(self, base, search_filter, **kwargs) -> bool:
        self.searches.append({"base": base, "filter": search_filter, **kwargs})
        if self.directory.search_raises is not None:
            raise self.directory.search_raises
        self.entries = list(self.directory.entries)
        return self.directory.search_ok

    def unbind(self) -> None:
        self.unbound = True


class FakeDirectory:
    def __init__(
        self,
        entries=(),
        *,
        open_ok=True,
        start_tls_ok=True,
        search_bind_ok=True,
        user_bind_ok=True,
        search_ok=True,
        search_raises=None,
    ):
        self.entries = list(entries)
        self.open_ok = open_ok
        self.start_tls_ok = start_tls_ok
        self.search_bind_ok = search_bind_ok
        self.user_bind_ok = user_bind_ok
        self.search_ok = search_ok
        self.search_raises = search_raises
        self.connections: list[FakeConnection] = []
        self.servers: list[dict] = []

    def connection(self, server, user=None, password=None, **kwargs):
        connection = FakeConnection(self, user, password, **kwargs)
        self.connections.append(connection)
        return connection

    def server(self, host, **kwargs):
        self.servers.append({"host": host, **kwargs})
        return SimpleNamespace(host=host, **kwargs)


ENTRY = FakeEntry(
    "uid=directory.user,ou=people,dc=example,dc=test",
    uid=["directory.user"],
    cn=["Directory User"],
    mail=["directory.user@example.test"],
    memberOf=[
        "cn=platform admins,ou=groups,dc=example,dc=test",
        "cn=everyone,ou=groups,dc=example,dc=test",
    ],
)


@pytest.fixture
def ldap_settings(monkeypatch):
    """A correctly configured directory over ldaps://."""
    monkeypatch.setattr(settings, "ldap_url", "ldaps://directory.example.test:636")
    monkeypatch.setattr(settings, "ldap_start_tls", False)
    monkeypatch.setattr(settings, "ldap_user_search_base", "ou=people,dc=example,dc=test")
    monkeypatch.setattr(settings, "ldap_user_search_filter", "(uid={username})")
    monkeypatch.setattr(settings, "ldap_bind_dn", "cn=search,dc=example,dc=test")
    monkeypatch.setattr(settings, "ldap_ca_certificate_file", "")
    # Restored by monkeypatch even where a test assigns over them, so a test
    # that widens the configuration cannot leak it into the next one.
    monkeypatch.setattr(settings, "ldap_username_attribute", "uid")
    monkeypatch.setattr(settings, "ldap_display_name_attribute", "cn")
    monkeypatch.setattr(settings, "ldap_email_attribute", "mail")
    monkeypatch.setattr(settings, "ldap_connect_timeout_seconds", 5.0)
    return settings


@pytest.fixture
def directory(monkeypatch, ldap_settings):
    """Install a fake directory in place of ldap3's Connection and Server."""
    fake = FakeDirectory(entries=[ENTRY])
    monkeypatch.setattr("ldap3.Connection", fake.connection)
    monkeypatch.setattr("ldap3.Server", fake.server)
    return fake


def authenticate(username="directory.user", password="directory-password"):
    return ldap_module.authenticate(username, password)


# --------------------------------------------------------------------------- #
# Configuration is refused before a socket is opened
# --------------------------------------------------------------------------- #

def test_credentials_are_never_sent_in_clear_text(directory):
    """ldap:// without StartTLS puts the directory password on the wire. The
    refusal is here rather than at the connection so the leak cannot happen
    once."""
    settings.ldap_url = "ldap://directory.example.test:389"
    settings.ldap_start_tls = False

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "clear text" in caught.value.message
    assert directory.connections == [], "no connection was opened"


@pytest.mark.parametrize(
    "url", ["", "https://directory.example.test", "ldaps://", "directory.example.test"],
)
def test_a_url_that_is_not_an_ldap_endpoint_is_refused(directory, url):
    settings.ldap_url = url

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    # The message says what is wrong with the URL; the *hint* names the
    # variable, and only because this configuration came from the environment.
    # ADR-0011: the same refusal on a deployment whose directory was typed into
    # the console points at that screen instead, because telling somebody to
    # edit LDAP_URL when a stored row is what is in effect sends them to change
    # something that changes nothing.
    assert "ldap:// or ldaps://" in caught.value.message
    assert "LDAP_URL" in (caught.value.hint or "")
    assert directory.connections == []


def test_a_missing_search_base_is_refused_rather_than_searching_the_root(directory):
    """An empty base is a legal LDAP search of the whole directory, which is a
    very different query from the one the operator meant to configure."""
    settings.ldap_user_search_base = ""

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "search base is required" in caught.value.message
    assert "LDAP_USER_SEARCH_BASE" in (caught.value.hint or "")
    assert directory.connections == []


def test_a_filter_without_the_username_placeholder_is_refused(directory):
    """Otherwise the filter matches whatever it matches, independently of who is
    logging in — and the first entry it returns is the account being bound."""
    settings.ldap_user_search_filter = "(objectClass=person)"

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "{username}" in caught.value.message
    assert directory.connections == []


# --------------------------------------------------------------------------- #
# The successful path
# --------------------------------------------------------------------------- #

def test_a_successful_search_and_bind_returns_the_directory_profile(directory):
    profile = authenticate()

    assert profile == ldap_module.LDAPProfile(
        username="directory.user",
        display_name="Directory User",
        email="directory.user@example.test",
        groups=(
            "cn=platform admins,ou=groups,dc=example,dc=test",
            "cn=everyone,ou=groups,dc=example,dc=test",
        ),
    )


def test_the_password_is_proved_by_binding_as_the_entrys_own_dn(directory):
    """The search account's bind proves nothing about the person logging in, and
    a client-side comparison of anything at all would be a bypass."""
    authenticate()

    search_connection, user_connection = directory.connections
    assert search_connection.user == "cn=search,dc=example,dc=test"
    assert user_connection.user == ENTRY.entry_dn
    assert user_connection.password == "directory-password"
    assert search_connection.password != "directory-password", (
        "the user's password is only ever sent on their own bind"
    )


def test_the_search_asks_for_every_configured_attribute_exactly_once(directory):
    """memberOf is always requested; a duplicate would be sent to directories
    that reject a repeated attribute in one search."""
    settings.ldap_username_attribute = "cn"
    settings.ldap_display_name_attribute = "cn"

    authenticate()

    (search,) = directory.connections[0].searches
    assert search["attributes"] == ["cn", "mail", "memberOf"]
    assert search["base"] == "ou=people,dc=example,dc=test"
    assert search["size_limit"] == 2, (
        "two, not one: the ambiguous case has to be detectable rather than "
        "truncated away by the server"
    )


def test_the_username_is_escaped_into_the_filter(directory):
    """An unescaped `*` turns `(uid=admin*)` into a wildcard search, and the
    entry it finds is bound with the submitted password."""
    authenticate(username="ad*min)(")

    (search,) = directory.connections[0].searches
    assert search["filter"] == "(uid=ad\\2amin\\29\\28)"


def test_start_tls_is_negotiated_when_the_url_is_plain_ldap(directory):
    settings.ldap_url = "ldap://directory.example.test:389"
    settings.ldap_start_tls = True

    assert authenticate() is not None
    assert all(connection.started_tls for connection in directory.connections)
    assert directory.servers[0]["port"] == 389
    assert directory.servers[0]["use_ssl"] is False


def test_ldaps_defaults_to_port_636_and_needs_no_start_tls(directory):
    settings.ldap_url = "ldaps://directory.example.test"

    authenticate()

    assert directory.servers[0]["port"] == 636
    assert directory.servers[0]["use_ssl"] is True
    assert not any(connection.started_tls for connection in directory.connections)


# --------------------------------------------------------------------------- #
# memberOf: absent is not empty
# --------------------------------------------------------------------------- #

def test_an_absent_member_of_attribute_is_none_not_an_empty_membership(
    directory, caplog,
):
    """`()` means "in no groups", which the caller writes over the stored role.
    An ACL hiding memberOf from the search account would then demote an
    administrator on one login and restore them on the next."""
    directory.entries = [FakeEntry("uid=x,ou=people,dc=example,dc=test", uid=["x"])]

    with caplog.at_level("WARNING"):
        profile = authenticate(username="x")

    assert profile.groups is None
    assert "memberOf" in caplog.text, "the operator needs to know role mapping was skipped"


def test_a_directory_that_reports_no_groups_is_an_empty_tuple(directory):
    """The other half of the distinction: we asked, and the answer was none."""
    directory.entries = [
        FakeEntry("uid=x,ou=people,dc=example,dc=test", uid=["x"], memberOf=[])
    ]

    assert authenticate(username="x").groups == ()


# --------------------------------------------------------------------------- #
# Attribute values
# --------------------------------------------------------------------------- #

def test_the_submitted_username_is_used_when_the_directory_returns_none(directory):
    """The session has to be keyed to something; a null username would key it to
    nothing."""
    directory.entries = [
        FakeEntry("uid=x,ou=people,dc=example,dc=test", cn=["X"], memberOf=[])
    ]

    assert authenticate(username="submitted.name").username == "submitted.name"


def test_a_whitespace_only_attribute_is_read_as_absent(directory):
    """A display name of three spaces renders as a blank row in the user list,
    which reads as a broken console rather than as an empty directory field."""
    directory.entries = [
        FakeEntry(
            "uid=x,ou=people,dc=example,dc=test",
            uid=["x"], cn=["   "], mail=[""], memberOf=[],
        )
    ]

    profile = authenticate(username="x")

    assert profile.display_name is None
    assert profile.email is None


def test_an_unconfigured_attribute_name_is_not_looked_up(directory):
    settings.ldap_email_attribute = ""

    assert authenticate().email is None


# --------------------------------------------------------------------------- #
# Refusals and failures
# --------------------------------------------------------------------------- #

def test_a_wrong_password_is_a_refusal_not_a_provider_failure(directory):
    """None means "not this password"; raising would tell the caller the
    directory is down and every login would report an outage."""
    directory.user_bind_ok = False

    assert authenticate() is None


def test_no_matching_entry_is_a_refusal(directory):
    directory.entries = []

    assert authenticate() is None
    assert len(directory.connections) == 1, "nothing to bind as"


def test_two_matching_entries_are_a_refusal_rather_than_a_guess(directory):
    """Binding as whichever came back first authenticates one person's password
    against another person's account."""
    directory.entries = [ENTRY, FakeEntry("uid=other,dc=example,dc=test", uid=["other"])]

    assert authenticate() is None
    assert len(directory.connections) == 1


def test_a_search_account_that_cannot_bind_is_a_provider_failure(directory):
    """Not a refusal: the person's password was never tested, and reporting
    "wrong password" would send them to reset a credential that is correct."""
    directory.search_bind_ok = False

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "search account" in caught.value.message


def test_a_failed_search_is_a_provider_failure_not_an_empty_result(directory):
    """An unreadable search returning None would present a directory-side ACL
    problem as "no such user"."""
    directory.search_ok = False

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "search failed" in caught.value.message


def test_a_server_that_refuses_the_connection_is_a_provider_failure(directory):
    directory.open_ok = False

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "did not accept a connection" in caught.value.message


def test_a_failed_start_tls_never_falls_back_to_clear_text(directory):
    """Downgrading here is the one thing that must not happen: the password
    would go out unencrypted on a connection the operator asked to protect."""
    settings.ldap_url = "ldap://directory.example.test:389"
    settings.ldap_start_tls = True
    directory.start_tls_ok = False

    with pytest.raises(IdentityProviderUnavailable) as caught:
        authenticate()

    assert "StartTLS" in caught.value.message
    assert directory.connections[0].unbound, "the unprotected connection is dropped"


def test_an_ldap_protocol_failure_is_reported_as_a_provider_outage(directory, caplog):
    directory.search_raises = LDAPSocketOpenError("connection reset")

    with caplog.at_level("WARNING"), pytest.raises(IdentityProviderUnavailable):
        authenticate()

    assert "LDAPSocketOpenError" in caplog.text
    assert "connection reset" not in caplog.text, (
        "the exception's own text can carry a DN; the type is what is logged"
    )


def test_every_connection_is_unbound_even_when_the_search_fails(directory):
    """A connection left open holds a directory session per failed login, and a
    login page under a password-spraying attempt is exactly when that matters."""
    directory.search_raises = LDAPException("boom")

    with pytest.raises(IdentityProviderUnavailable):
        authenticate()

    assert [connection.unbound for connection in directory.connections] == [True]


def test_both_connections_are_unbound_after_a_successful_login(directory):
    authenticate()

    assert [connection.unbound for connection in directory.connections] == [True, True]


def test_the_connect_timeout_is_applied_to_both_the_socket_and_the_response(directory):
    """A directory that accepts a connection and then never answers hangs the
    login request until the client gives up, which is the login page timing out
    with no message."""
    settings.ldap_connect_timeout_seconds = 2.5

    authenticate()

    assert directory.servers[0]["connect_timeout"] == 2.5
    assert directory.connections[0].kwargs["receive_timeout"] == 2.5
