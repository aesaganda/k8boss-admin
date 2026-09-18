"""
Credential encryption at rest, and the key's persistence.

The property under test is not "Fernet works" — it is that a key never changes
without an operator changing it. Every stored cluster token was written with the
key this module resolves, so a fresh key silently orphans all of them, and the
symptom an operator sees is every registered cluster failing to authenticate
after an unrelated restart. So the resolution order, the refusal to generate
over a key file that exists but cannot be read, and the "no ciphertext for an
absent credential" rule are each pinned here.

Every test resets the module memo, because ``_get_fernet`` caches for the life
of the process and a leaked key would make the next test pass or fail for a
reason that has nothing to do with what it asserts.
"""

from __future__ import annotations

import base64
import hashlib
import os
import time

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app import crypto
from app.config import settings


@pytest.fixture(autouse=True)
def clean_key_cache():
    """No key survives a test in either direction."""
    crypto.reset_cache()
    yield
    crypto.reset_cache()


@pytest.fixture
def no_configured_key(monkeypatch):
    """Force the key-file branch: ``ENCRYPTION_KEY`` unset, as in a dev run."""
    monkeypatch.setattr(settings, "encryption_key", "")
    return settings


# --------------------------------------------------------------------------- #
# Where the key comes from
# --------------------------------------------------------------------------- #

def test_a_configured_secret_is_derived_into_a_usable_fernet_key():
    """Operators paste an arbitrary string into a Secret. Requiring a valid
    Fernet key by hand means discovering it was not one at the first cluster
    registration."""
    key = crypto._derive_key("not-a-fernet-key, just a passphrase")

    assert Fernet(key).decrypt(Fernet(key).encrypt(b"x")) == b"x"


def test_the_same_secret_always_derives_the_same_key():
    """The whole point: a restart with the same ENCRYPTION_KEY reads what the
    previous process wrote."""
    assert crypto._derive_key("shared") == crypto._derive_key("shared")
    assert crypto._derive_key("shared") != crypto._derive_key("other")


def test_the_key_file_sits_next_to_the_sqlite_database_it_protects(monkeypatch):
    """A key in /tmp beside a database on a PersistentVolume survives exactly as
    long as the pod — the ephemeral-key failure in a different costume."""
    monkeypatch.setattr(settings, "encryption_key_file", "")
    monkeypatch.setattr(settings, "database_url", "sqlite:////var/lib/k8boss/admin.db")

    assert str(crypto.key_file_path()) == "/var/lib/k8boss/admin.db.key"


def test_query_parameters_are_not_part_of_the_database_filename(monkeypatch):
    monkeypatch.setattr(settings, "encryption_key_file", "")
    monkeypatch.setattr(
        settings, "database_url", "sqlite:///./admin.db?check_same_thread=false",
    )

    assert crypto.key_file_path().name == "admin.db.key"


@pytest.mark.parametrize(
    "database_url",
    ["sqlite://", "postgresql+psycopg2://user@db:5432/k8boss"],
    ids=["in-memory-sqlite", "postgres"],
)
def test_a_database_with_no_file_falls_back_to_the_working_directory(
    monkeypatch, database_url,
):
    """There is nothing to sit next to, and the working directory is the thing a
    container image controls."""
    monkeypatch.setattr(settings, "encryption_key_file", "")
    monkeypatch.setattr(settings, "database_url", database_url)

    assert crypto.key_file_path() == crypto.Path("./k8boss_admin.key")


def test_an_explicit_key_file_setting_wins_over_the_database_location(monkeypatch):
    monkeypatch.setattr(settings, "encryption_key_file", "/etc/k8boss/admin.key")
    monkeypatch.setattr(settings, "database_url", "sqlite:////var/lib/k8boss/admin.db")

    assert str(crypto.key_file_path()) == "/etc/k8boss/admin.key"


def test_a_configured_secret_is_preferred_over_an_existing_key_file(
    monkeypatch, tmp_path,
):
    """Otherwise a leftover dev key file would beat the Secret an operator just
    mounted, and the deployment would keep using a key nobody configured."""
    key_file = tmp_path / "admin.db.key"
    key_file.write_bytes(Fernet.generate_key())
    monkeypatch.setattr(settings, "encryption_key", "configured-secret")
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))

    assert crypto._load_or_create_key() == crypto._derive_key("configured-secret")


def test_a_short_configured_key_is_warned_about_with_the_command_that_generates_one(
    monkeypatch, caplog,
):
    """One unsalted SHA-256 makes a typed passphrase brute-forceable offline from
    a database dump. The operator can only act on that if the log line names the
    command that produces a real key, so assert the command is in it."""
    monkeypatch.setattr(settings, "encryption_key", "hunter2")

    with caplog.at_level("WARNING"):
        crypto._load_or_create_key()

    assert "Fernet.generate_key()" in caplog.text
    assert "hunter2" not in caplog.text, "never log credential material"


def test_a_generated_length_key_is_not_warned_about(monkeypatch, caplog):
    """A warning that fires for a correctly configured deployment is a warning
    operators learn to filter, and it takes the real one with it."""
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())

    with caplog.at_level("WARNING"):
        crypto._load_or_create_key()

    assert caplog.text == ""


def test_warning_about_a_short_key_does_not_change_what_it_derives():
    """The fix is a log line and nothing more, on purpose: re-deriving the key
    from the same secret would make every token in every existing database
    undecryptable, which reaches the operator as every cluster failing to
    authenticate after an upgrade."""
    assert crypto._derive_key("hunter2") == base64.urlsafe_b64encode(
        hashlib.sha256(b"hunter2").digest()
    )


def test_an_existing_key_file_is_read_rather_than_replaced(
    monkeypatch, tmp_path, no_configured_key,
):
    stored = Fernet.generate_key()
    key_file = tmp_path / "admin.db.key"
    key_file.write_bytes(stored + b"\n")
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))

    assert crypto._load_or_create_key() == stored, "trailing newline and all"


def test_an_unreadable_key_file_is_a_failure_not_a_reason_to_generate(
    monkeypatch, tmp_path, no_configured_key,
):
    """Generating here would make every stored token undecryptable, and `decrypt`
    reports that as an invalid token — so a file-permission problem would
    present itself to the operator as credential tampering."""
    key_file = tmp_path / "admin.db.key"
    key_file.write_bytes(Fernet.generate_key())
    key_file.chmod(0o000)
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))

    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits, so the OSError cannot be provoked")

    with pytest.raises(RuntimeError) as caught:
        crypto._load_or_create_key()

    assert "Refusing to generate a new key" in str(caught.value)
    key_file.chmod(0o600)
    assert key_file.read_bytes(), "the existing key was left in place"


def test_an_empty_key_file_is_a_half_finished_write_and_is_replaced(
    monkeypatch, tmp_path, no_configured_key,
):
    """Safe precisely because nothing was ever encrypted with an empty key."""
    key_file = tmp_path / "admin.db.key"
    key_file.write_bytes(b"")
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))

    key = crypto._load_or_create_key()

    assert key and key_file.read_bytes() == key


def test_a_generated_key_is_persisted_with_owner_only_permissions(
    monkeypatch, tmp_path, no_configured_key,
):
    """Persisted, because a key generated per boot orphans every token the
    previous process stored; mode 600, because it is a credential."""
    key_file = tmp_path / "nested" / "admin.db.key"
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))

    key = crypto._load_or_create_key()

    assert key_file.read_bytes() == key
    assert oct(key_file.stat().st_mode)[-3:] == "600"
    assert crypto._load_or_create_key() == key, "a second process reads it back"


def test_a_key_that_cannot_be_persisted_is_still_used_for_this_process(
    monkeypatch, tmp_path, no_configured_key, caplog,
):
    """Not fatal — the process works right now. The damage appears after a
    restart, so the log line has to say what it will cost."""
    key_file = tmp_path / "admin.db.key"
    monkeypatch.setattr(settings, "encryption_key_file", str(key_file))
    monkeypatch.setattr(
        crypto.Path, "write_bytes",
        lambda self, data: (_ for _ in ()).throw(PermissionError("read-only volume")),
    )

    with caplog.at_level("ERROR"):
        key = crypto._load_or_create_key()

    assert Fernet(key), "usable in memory"
    assert not key_file.exists()
    assert "UNDECRYPTABLE" in caplog.text


# --------------------------------------------------------------------------- #
# encrypt / decrypt
# --------------------------------------------------------------------------- #

def test_a_token_round_trips():
    assert crypto.decrypt(crypto.encrypt("eyJhbGciOi.token")) == "eyJhbGciOi.token"


@pytest.mark.parametrize("absent", ["", None])
def test_an_absent_credential_encrypts_to_the_empty_string_not_to_ciphertext(absent):
    """Ciphertext that decrypts to "" is indistinguishable in the database from a
    real stored credential, so every `has_token`-style check would read true."""
    assert crypto.encrypt(absent) == ""


@pytest.mark.parametrize("absent", ["", None])
def test_decrypting_nothing_yields_nothing(absent):
    assert crypto.decrypt(absent) == ""


def test_a_tampered_credential_raises_value_error_without_logging_the_ciphertext(caplog):
    ciphertext = crypto.encrypt("a-real-token")
    tampered = ciphertext[:-4] + ("AAAA" if not ciphertext.endswith("AAAA") else "BBBB")

    with caplog.at_level("ERROR"), pytest.raises(ValueError) as caught:
        crypto.decrypt(tampered)

    assert "Unable to decrypt" in str(caught.value)
    assert tampered not in caplog.text, "never log credential material"
    assert "ENCRYPTION_KEY" in caplog.text, (
        "the overwhelmingly common cause is a key that changed, so name the source"
    )


def test_a_credential_written_with_a_different_key_does_not_decrypt(monkeypatch):
    """The failure an operator hits after a replica generated its own key."""
    monkeypatch.setattr(settings, "encryption_key", "first-key")
    ciphertext = crypto.encrypt("a-real-token")

    crypto.reset_cache()
    monkeypatch.setattr(settings, "encryption_key", "second-key")

    with pytest.raises(ValueError):
        crypto.decrypt(ciphertext)


def test_a_ttl_token_is_rejected_once_it_has_expired():
    """Fernet checks the authenticated timestamp before parsing the payload, so
    there is no code path that reads the contents of an expired token."""
    fresh = crypto.encrypt("handshake-payload")
    stale = crypto._get_fernet().encrypt_at_time(
        b"handshake-payload", int(time.time()) - 600,
    ).decode("utf-8")

    assert crypto.decrypt_with_ttl(fresh, ttl_seconds=300) == "handshake-payload"
    with pytest.raises(InvalidToken):
        crypto.decrypt_with_ttl(stale, ttl_seconds=300)


def test_the_key_is_resolved_once_per_process():
    """The generate branch warns exactly once, and it can only do that if the
    memo holds."""
    first = crypto._get_fernet()

    assert crypto._get_fernet() is first
    crypto.reset_cache()
    assert crypto._get_fernet() is not first
