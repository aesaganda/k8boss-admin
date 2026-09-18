"""
Reading and writing the stored identity providers (ADR-0011).

The table is one row per kind (`app.models.IdentityProvider`) and this module is
the only thing that touches it. :mod:`app.identity.provider_config` resolves a
row into the configuration a provider module reads; the §12.8 router turns these
functions into endpoints.

**Nothing here is cached.** :func:`get` is called on the path of every sign-in
and of the public discovery endpoint, and a cached provider configuration
outlives the edit that changed it — the same argument §0.2 makes for never
caching an access review. An administrator who switches a directory off and
watches somebody sign in through it thirty seconds later has been told a lie by
a cache. The reads are a primary-key lookup on a table with at most five rows.

**Secrets are encrypted with the cluster-token key** (:mod:`app.crypto`),
because they are the same class of material: a credential this console holds on
the operator's behalf, in a database whose backups leave the machine. One
encrypted JSON object per row rather than a column per secret — which of a
kind's fields are secret is per-kind data in ``provider_config``, and a schema
enumerating them would be a second list to keep in step with it.

**An undecryptable secret is reported, never silently replaced.** If
``ENCRYPTION_KEY`` changed, the stored bind password cannot be recovered, and
the two wrong answers are opposite: sending an empty password makes the
directory report a credential rejection, and telling the administrator the
password is stored makes them debug the directory instead of the key. So
:func:`decrypt_secrets` omits what it cannot read — the field then falls back to
the environment, which is a real configuration — and :func:`describe` reports
``secrets_unreadable`` so the screen an administrator is looking at says so.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import crypto, database
from app.models import IdentityProvider

logger = logging.getLogger(__name__)


def get(kind: str) -> IdentityProvider | None:
    """The stored row for one kind, using a short-lived session of its own.

    Its own session because the callers that matter — ``oidc.enabled()`` inside
    the public discovery endpoint, ``ldap.authenticate`` inside a sign-in — have
    no request-scoped session to borrow, exactly as
    :func:`app.identity.service.load_session` does not.

    The returned row is detached: its attributes are read before the session
    closes, so a caller cannot lazy-load through it and get an error from a
    closed session on the login path.
    """
    db = database.SessionLocal()
    try:
        row = db.scalar(select(IdentityProvider).where(IdentityProvider.kind == kind))
        if row is not None:
            # Touch every column the resolver reads, then detach. Expunging
            # without this hands back an object whose next attribute access
            # raises DetachedInstanceError — on the sign-in path.
            _ = (row.kind, row.enabled, row.config, row.secrets_encrypted)
            db.expunge(row)
        return row
    finally:
        db.close()


def list_rows(db: Session) -> dict[str, IdentityProvider]:
    """Every stored row, by kind. Takes the request's session: this is a listing."""
    return {
        row.kind: row
        for row in db.scalars(select(IdentityProvider).order_by(IdentityProvider.kind))
    }


def decrypt_secrets(row: IdentityProvider) -> dict[str, str]:
    """The row's secret fields, or as many of them as the key can still read.

    Returns ``{}`` for a row that stores none. A blob that cannot be decrypted
    logs and returns ``{}`` rather than raising: this is on the sign-in path, and
    the caller's fallback (the environment's value) is a real configuration,
    while an exception here is a login page that will not render.
    """
    if not row.secrets_encrypted:
        return {}
    try:
        decoded = json.loads(crypto.decrypt(row.secrets_encrypted))
    except ValueError:
        # crypto.decrypt has already logged which key source failed, which is
        # the actionable part. Never log the ciphertext.
        logger.error(
            "The stored secrets for the %r identity provider could not be "
            "decrypted. Its secret fields will fall back to this deployment's "
            "environment settings, and the console will report them as "
            "unreadable rather than as configured.", row.kind,
        )
        return {}
    if not isinstance(decoded, dict):
        logger.error(
            "The stored secrets for the %r identity provider are not an object.",
            row.kind,
        )
        return {}
    return {str(key): str(value) for key, value in decoded.items()}


def secrets_readable(row: IdentityProvider) -> bool:
    """Whether this row's stored secrets can still be decrypted."""
    return not row.secrets_encrypted or bool(decrypt_secrets(row))


def _encrypt_secrets(values: Mapping[str, str]) -> str | None:
    """Encrypt the secret fields, or ``None`` when there are none to store.

    ``None`` rather than the ciphertext of ``{}``: an empty blob that decrypts
    to an empty object is indistinguishable in the database from a stored
    credential, and every "is a password set" check would read true. Same
    reasoning as ``crypto.encrypt`` refusing to encrypt an empty string.
    """
    present = {name: value for name, value in values.items() if value}
    if not present:
        return None
    return crypto.encrypt(json.dumps(present))


def upsert(
    db: Session,
    *,
    kind: str,
    enabled: bool,
    config: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> IdentityProvider:
    """Create or replace the row for one kind.

    ``config`` and ``secrets`` are the **merged, validated** values — the caller
    has already combined what was submitted with what was stored, so a form that
    did not send a field does not blank it. Doing the merge here instead would
    hide it from the endpoint that has to decide what "omitted" means for a
    secret, which is the one place it means something different: keep.
    """
    row = db.scalar(select(IdentityProvider).where(IdentityProvider.kind == kind))
    if row is None:
        row = IdentityProvider(kind=kind)
        db.add(row)
    row.enabled = bool(enabled)
    row.config = dict(config)
    row.secrets_encrypted = _encrypt_secrets(secrets)
    db.commit()
    db.refresh(row)
    return row


def delete(db: Session, kind: str) -> bool:
    """Remove the row for one kind. ``False`` when there was none.

    The distinction is the endpoint's to report: "there was nothing stored for
    that kind" and "the stored configuration is gone" are different answers, and
    the second one changes how this deployment authenticates.
    """
    row = db.scalar(select(IdentityProvider).where(IdentityProvider.kind == kind))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True
