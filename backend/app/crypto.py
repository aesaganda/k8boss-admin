"""
Symmetric encryption for stored cluster credentials.

Cluster bearer tokens are encrypted at rest with Fernet (AES-128-CBC + HMAC).
The key comes from ``ENCRYPTION_KEY``; when that is unset the key is generated
**once** and persisted in a file next to the database.

That persistence is the whole point of this module. Generating a fresh key each
boot is trivially easy and looks harmless in a dev environment, and it is the
bug: every token written by the previous process becomes undecryptable, and the
symptom an operator sees is not "the key changed", it is every registered
cluster failing to connect after an unrelated restart. That reads as "K8Boss
broke my clusters". So: generate once, persist, warn once, and refuse to
generate over a key file we can see but cannot read.

The module is deliberately small so the storage backend can later be swapped for
Vault or a cloud secret manager by re-implementing ``encrypt``/``decrypt`` alone.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _derive_key(secret: str) -> bytes:
    """Derive a urlsafe-base64 32-byte Fernet key from an arbitrary secret.

    Lets ``ENCRYPTION_KEY`` be any string an operator can put in a Secret,
    instead of requiring them to produce a valid Fernet key by hand — a
    requirement that in practice gets satisfied by pasting something that is not
    one and discovering it at the first cluster registration.
    """
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def key_file_path() -> Path:
    """Where the generated key lives: next to the database it protects.

    "Next to the database" is not cosmetic. The key and the ciphertext are only
    meaningful together, so a backup, a volume snapshot or a `docker cp` of the
    data directory that captures one captures the other. A key in ``/tmp`` beside
    a database on a PersistentVolume survives exactly as long as the pod, which
    is the ephemeral-key failure in a different costume.
    """
    if settings.encryption_key_file:
        return Path(settings.encryption_key_file).expanduser()

    url = settings.database_url
    if url.startswith("sqlite"):
        # sqlite:///relative/path.db  or  sqlite:////absolute/path.db
        _, _, path = url.partition("///")
        path = path.split("?", 1)[0]
        if path and path != ":memory:":
            db_path = Path(path).expanduser()
            return db_path.with_name(db_path.name + ".key")
    # Non-SQLite (or in-memory SQLite): there is no file to sit next to, so use
    # the working directory, which is what a container image controls.
    return Path("./k8boss_admin.key")


def _load_or_create_key() -> bytes:
    """Resolve the Fernet key: explicit setting, then key file, then generate."""
    if settings.encryption_key:
        return _derive_key(settings.encryption_key)

    path = key_file_path()
    if path.exists():
        # A key file we can see but cannot read is NOT a reason to fall through
        # to the generate branch. A fresh key would make every stored token
        # undecryptable, and `decrypt` reports that as an invalid token — so a
        # file-permission problem would present itself as credential tampering.
        # Fail, and name the actual fault.
        try:
            data = path.read_bytes().strip()
        except OSError as e:
            raise RuntimeError(
                f"Encryption key file {path} exists but could not be read "
                f"({type(e).__name__}). Stored cluster credentials were encrypted "
                "with it and cannot be decrypted without it. Fix the file's "
                "ownership or permissions, or set ENCRYPTION_KEY to the same "
                "secret. Refusing to generate a new key, which would silently "
                "orphan every stored token."
            ) from e
        if data:
            return data
        # An empty key file is a half-finished write from a previous crash, not
        # a key. Falling through to generate is safe here precisely because
        # nothing was ever encrypted with an empty key.
        logger.warning("Encryption key file %s is empty; generating a new key.", path)

    key = Fernet.generate_key()
    # WARNING, and exactly once (this function runs once per process, guarded by
    # the _fernet memo). An operator who never sets ENCRYPTION_KEY needs to see
    # this line in the logs of the pod it happened in — but repeating it per
    # request would train them to filter it out, which is how the message stops
    # working.
    logger.warning(
        "No ENCRYPTION_KEY configured. Generated a credential-encryption key and "
        "persisted it at %s. This is a development convenience: in a cluster, "
        "set ENCRYPTION_KEY from a Secret so the key survives a rescheduled pod "
        "and is shared by every replica. Replicas that generate their own keys "
        "cannot decrypt each other's stored cluster tokens.",
        path,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        os.chmod(path, 0o600)
    except OSError as e:
        # Deliberately not fatal — the process works fine right now. But say what
        # it costs, because the damage only appears after a restart, when a
        # different generated key can no longer read what this one wrote, and by
        # then nothing connects this log line to that symptom.
        logger.error(
            "Could not persist the encryption key to %s (%s). Continuing with an "
            "IN-MEMORY key: every cluster token stored by this process becomes "
            "UNDECRYPTABLE after a restart, and the clusters will report as "
            "failing to authenticate. Set ENCRYPTION_KEY to make this durable.",
            path, type(e).__name__,
        )
    return key


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_or_create_key())
    return _fernet


def reset_cache() -> None:
    """Drop the memoised key. For tests that change ``settings.encryption_key``."""
    global _fernet
    _fernet = None


def encrypt(plaintext: str | None) -> str:
    """Encrypt a secret. Empty input encrypts to the empty string, not ciphertext.

    An empty token means "no credential stored", and round-tripping that through
    Fernet would produce ciphertext that decrypts to "" — indistinguishable in
    the database from a real stored credential, so `has_token`-style checks would
    all read true.
    """
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str | None) -> str:
    """Decrypt a stored credential. Raises ``ValueError`` on tamper or wrong key."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        # Never log the ciphertext or any key material. The key path is safe and
        # is the single most useful thing to know here, because the overwhelmingly
        # common cause is a key that changed, not a database that was tampered with.
        logger.error(
            "Failed to decrypt a stored cluster credential. The encryption key in "
            "use does not match the one that wrote it (key source: %s).",
            "ENCRYPTION_KEY" if settings.encryption_key else str(key_file_path()),
        )
        raise ValueError("Unable to decrypt the stored cluster credential.") from e
