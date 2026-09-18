"""
Identity providers, configured from the console (§12.8, ADR-0011).

One row per kind — ``kind`` is unique in the table, so "two OIDC issuers" is
impossible at the storage layer and §12.4's collision story cannot arise. The
environment stays as the fallback: a kind with no row reads ``LDAP_*`` /
``OIDC_*`` / … exactly as before, so no existing deployment changes behaviour by
upgrading into this.

Administrator-only, every state change audited, and secret values never
returned. ``PUT`` is create-or-replace rather than a ``POST``/``PUT`` pair
because the kind *is* the identity of the row: there is no id to allocate, and
two endpoints would differ only in which of them returned 409.

**Nothing here goes through the write funnel**, and that is the same call §12.6
makes for user administration: ``app/admin/mutate.py`` gates, preflights against
a cluster's RBAC, dry-runs against an API server and diffs the projection. None
of the five steps has a meaning for a row in this console's own database, and
routing through it would need a fake cluster and a fake preflight to get to the
audit call. What this module owes the trail is the audit row, which it writes on
every terminal state including the refusals.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from fastapi import APIRouter, Depends, Path, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.audit import recorder
from app.database import get_db
from app.errors import AdminError, NotFound
from app.identity import inventory, provider_config, provider_store
from app.identity.dependencies import require_admin
from app.resources.envelope import envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/providers", tags=["authentication"])

#: The kind in the URL, matched against the kinds this build knows. A pattern
#: rather than a lookup so an unknown kind is refused before it reaches a
#: database query, and so the path cannot carry anything but a known word.
_KIND = Path(..., pattern="^(" + "|".join(provider_config.KINDS) + ")$")


class ProviderBody(BaseModel):
    """One provider's configuration, as the console's form submits it.

    ``values`` carries the kind's fields by name, secret and non-secret
    together — the form does not know the difference and should not have to.
    :func:`_split` sorts them by the spec, which is the only thing that knows.

    **An omitted secret keeps what is stored; an empty one clears it.** That
    asymmetry is the whole reason this body is not a plain replace: the edit form
    cannot show a stored password (see :mod:`app.identity.inventory`), so it
    cannot send one back, and a replace would wipe the bind password every time
    somebody fixed a typo in the search base. Sending ``""`` is how the field is
    deliberately emptied, and the response says which secrets are stored so the
    result is never a guess.
    """

    enabled: bool = False
    values: dict[str, Any] = Field(default_factory=dict)


def _split(kind: str, values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Submitted values, sorted into the non-secret ones and the secret ones."""
    secret_names = {
        field.name for field in provider_config.spec(kind).fields if field.secret
    }
    config = {k: v for k, v in values.items() if k not in secret_names}
    secrets = {k: v for k, v in values.items() if k in secret_names}
    return config, secrets


def _audit(
    verb: str, kind: str, detail: str, *, outcome: str = "applied",
    error: str | None = None,
) -> None:
    recorder.record_console_event(
        verb=verb,
        resource="identity_providers",
        name=kind,
        outcome=outcome,
        detail=detail,
        error=error,
    )


def audited(verb: str):
    """Record the refusals as well as the successes.

    The same argument §12.6's decorator makes, for a surface where it is sharper:
    every refusal here is somebody failing to change *how this console decides
    who you are*. A validation error that left nothing behind would make "who
    tried to point our OIDC at a different issuer" unanswerable, which is the
    question an incident review asks of this table.

    A decorator rather than try/except in three handlers, so a fourth cannot be
    written without the question "and where is its audit call?" being visible at
    the definition.
    """
    def decorate(handler):
        @functools.wraps(handler)
        def wrapper(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            except AdminError as error:
                _audit(
                    verb,
                    kwargs.get("kind", "(unknown)"),
                    "Refused a change to an identity provider.",
                    outcome="denied",
                    error=f"{error.code}: {error.message}",
                )
                raise
        return wrapper
    return decorate


@router.get("")
def list_providers(
    _admin=Depends(require_admin), db: Session = Depends(get_db)
) -> dict:
    """§12.8. Every sign-in method, with where it points and where it came from.

    Administrator-only, and that is the same decision §12.1 makes from the other
    side: every value here — an issuer, a directory URL, an API server address, a
    group DN — is what the public discovery endpoint withholds so that nobody who
    can merely reach the console can enumerate its identity infrastructure.
    """
    return envelope(inventory.sign_in_methods(db))


@router.put("/{kind}")
# `patch` on a refusal: what was attempted is a change to this deployment's
# configuration, and whether a row happened to exist yet is not the interesting
# part of a row that records somebody being refused.
@audited("patch")
def save_provider(
    body: ProviderBody,
    kind: str = _KIND,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Create or replace one kind's stored configuration.

    The submitted values are merged over what is stored **before** validation,
    so a form that sends four fields is not a request to blank the other ten,
    and ``required`` is checked against the effective configuration rather than
    against the fragment that arrived.

    Validated at the write, not at the next sign-in: a console that accepted a
    directory URL it will refuse to send credentials to, and reported that as
    saved, would have moved the failure onto somebody else's login attempt.
    """
    spec = provider_config.spec(kind)
    existing = provider_store.get(kind)
    stored_config = dict(existing.config or {}) if existing is not None else {}
    stored_secrets = provider_store.decrypt_secrets(existing) if existing is not None else {}

    submitted_config, submitted_secrets = _split(kind, body.values)
    merged = {**stored_config, **submitted_config}
    secrets = {**stored_secrets, **submitted_secrets}

    # Validated together, so a rule that spans a secret and a non-secret field
    # sees both. Only the non-secret half is stored in `config`.
    cleaned = provider_config.validate(
        kind, {**merged, **secrets}, enabled=body.enabled
    )
    secret_names = {field.name for field in spec.fields if field.secret}
    config_values = {k: v for k, v in cleaned.items() if k not in secret_names}
    secret_values = {k: str(v) for k, v in cleaned.items() if k in secret_names}

    provider_store.upsert(
        db, kind=kind, enabled=body.enabled, config=config_values, secrets=secret_values
    )
    # Field *names*, never values: this line is read by whoever is reviewing the
    # trail, and a detail carrying a bind password would put one in a table that
    # by design can never be edited or deleted.
    changed = ", ".join(sorted(body.values)) or "nothing"
    _audit(
        "patch" if existing is not None else "create",
        kind,
        f"{'Updated' if existing is not None else 'Configured'} the "
        f"{spec.title} identity provider "
        f"({'enabled' if body.enabled else 'disabled'}); fields submitted: {changed}.",
    )
    # Read back through the same describer the listing uses, so the response a
    # form gets after saving is the row it will render next — including `usable`
    # and `missing`, which is how "saved, and still not offered on the login
    # page, because the client secret is empty" arrives as an answer rather than
    # as a mystery.
    return next(
        entry for entry in inventory.sign_in_methods(db) if entry["name"] == kind
    )


@router.delete("/{kind}", status_code=204)
@audited("delete")
def delete_provider(
    kind: str = _KIND,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> Response:
    """Remove one kind's stored configuration.

    **This is "go back to what the deployment ships with", not "turn it off".**
    With the row gone, :func:`app.identity.provider_config.resolve` reads the
    environment again, so on a deployment carrying ``LDAP_*`` variables the
    directory comes back exactly as it was before anybody used this screen. The
    console says so before the click; saying it only afterwards would make a
    delete that re-enabled a provider look like a bug.

    A kind with nothing stored is a 404 rather than a silent success: "the
    stored configuration is gone" and "there was nothing stored" are different
    answers, and the second one leaves this deployment authenticating exactly as
    it did a moment ago.
    """
    if not provider_store.delete(db, kind):
        raise NotFound(
            "No stored configuration for that identity provider. This "
            "deployment reads that kind from its environment.",
            context={"resource": "identity_providers", "name": kind},
        )
    _audit(
        "delete",
        kind,
        f"Removed the stored configuration for the "
        f"{provider_config.spec(kind).title} identity provider; it now reads "
        f"this deployment's {kind.upper()}_* environment settings.",
    )
    return Response(status_code=204)
