"""
What sign-in methods this deployment has, where each one points, and where it
was configured — as data an administrator can read and edit (§12.8, ADR-0011).

The Users page shows the accounts that exist; this answers the question that
page raises and cannot: *how can anybody sign in here, and which group made
this person an administrator.* Before ADR-0011 the only place those answers
lived was the container's environment, which is not where somebody looking at a
console user is.

## Three states, not two

A card here reports ``enabled`` — what an operator switched on — and ``usable``
— whether a sign-in through it can actually complete. They are different
questions and collapsing them is a lie in one direction or the other:

* enabled and usable: the login page offers it.
* enabled and **not** usable: a required value is missing. The login page
  withholds the button (a button that leads to an error reads as a broken
  console rather than an unconfigured one), so without this distinction the
  screen would say "enabled" while nothing appeared on the login page and
  nothing anywhere explained the gap. ``missing`` names the fields.
* not enabled: switched off, whatever else is filled in.

``usable`` is not recomputed here. It is each provider module's own
``enabled()``, so the panel and the login page's buttons cannot disagree — one
rule, one place.

## What is never returned

Secret values. A row reports which of its secret fields **have** something
stored (``secrets_stored``) and whether the stored blob can still be decrypted
(``secrets_unreadable``), and never the values themselves — not even to an
administrator, because a screen that can display a bind password is a screen
that puts one in a browser cache and a screenshot.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.identity import provider_config, provider_store, sso

#: The label the console gives its own password table. Not a provider: nobody
#: configures it and it cannot be switched off.
LOCAL_LABEL = "Local accounts"


def _usable(kind: str, cfg: provider_config.ProviderConfig) -> bool:
    """Whether a sign-in through this kind can complete right now.

    Delegated to the provider module for the four single sign-on kinds, which is
    the whole point: ``enabled()`` is what the login page consults, so asking it
    here means the two can never disagree. LDAP has no registry entry — it has
    no handshake and no callback — and its condition is the fields its
    search-and-bind actually needs.
    """
    module = sso.get(kind)
    if module is not None:
        return bool(module.enabled())
    return bool(
        cfg.enabled
        and str(cfg.url).strip()
        and str(cfg.user_search_base).strip()
        and "{username}" in str(cfg.user_search_filter)
    )


def _missing(kind: str, cfg: provider_config.ProviderConfig) -> list[str]:
    """Required fields with nothing in them, so "incomplete" can say which."""
    return [
        field.name
        for field in provider_config.spec(kind).fields
        if field.required and not str(cfg.values.get(field.name, "")).strip()
    ]


def _describe(
    kind: str, row: Any | None, cfg: provider_config.ProviderConfig
) -> dict[str, Any]:
    spec = provider_config.spec(kind)
    secret_fields = [field.name for field in spec.fields if field.secret]
    stored_secrets = provider_store.decrypt_secrets(row) if row is not None else {}
    button_label = (
        str(cfg.values.get(spec.label_field, "")).strip()
        if spec.label_field
        else ""
    )
    return {
        # `name` rather than `kind`, kept from §12.8's first shape so an
        # already-loaded build of the SPA keeps rendering the panel.
        "name": kind,
        "title": spec.title,
        "summary": spec.summary,
        "caveat": spec.caveat,
        "label": button_label or spec.title,
        "enabled": bool(cfg.enabled),
        "usable": _usable(kind, cfg),
        "missing": _missing(kind, cfg),
        # Which of the two possible configurations answered. "Why is this on"
        # has two answers and this is the only thing that tells them apart.
        "source": cfg.source,
        # Whether a row exists at all, which is what decides whether DELETE has
        # anything to remove and whether deleting falls back to something.
        "stored": row is not None,
        "endpoint": _text(cfg.values.get(spec.endpoint_field)) if spec.endpoint_field else None,
        "admin_group": (
            _text(cfg.values.get(spec.admin_group_field))
            if spec.admin_group_field
            else None
        ),
        "settings_prefix": f"{kind.upper()}_",
        "editable": True,
        # Everything the edit form needs, minus every secret. The form is
        # rendered from `fields`, so a field the flow reads cannot be missing
        # from the screen that configures it.
        "fields": provider_config.schema(kind),
        "values": {
            field.name: cfg.values[field.name]
            for field in spec.fields
            if not field.secret
        },
        "secrets_stored": [name for name in secret_fields if stored_secrets.get(name)],
        # True when a blob exists and could not be decrypted — the encryption
        # key changed. Reported rather than shown as "configured", because the
        # second sends an administrator to debug the directory instead of the
        # key.
        "secrets_unreadable": row is not None and not provider_store.secrets_readable(row),
    }


def _text(value: Any) -> str | None:
    """A configured string, or ``None`` when nothing is set.

    Empty string is every one of these fields' default and means "not
    configured". Passing it through would render as a blank cell, which reads as
    a value the console failed to show rather than one nobody set.
    """
    text = str(value or "").strip()
    return text or None


def sign_in_methods(db: Session) -> list[dict[str, Any]]:
    """Every method this console can authenticate with, configured or not.

    The unconfigured ones are included deliberately: an administrator asking
    "can we use our SAML provider here" is asking about a method this build
    supports and this deployment has not set up, and a list showing only what is
    switched on cannot tell that apart from a method the console does not have.

    Takes the request's session and reads every row in one query, rather than
    letting each kind resolve itself: five short-lived sessions would answer the
    same question five times, and a row added between two of them would produce
    a listing that never existed.

    **A failed read is not swallowed here.** ``resolve()`` falls back to the
    environment on the sign-in path because a login page that cannot render is
    worse than a stale one; this is the configuration screen, where quietly
    showing environment values while the database is unreadable would be the
    wrong answer delivered confidently to somebody about to act on it. The
    exception propagates and §1.3 renders it.
    """
    rows = provider_store.list_rows(db)
    methods: list[dict[str, Any]] = [
        {
            "name": "local",
            "title": LOCAL_LABEL,
            "summary": (
                "This console's own accounts, with passwords hashed here. "
                "Always available: a deployment with neither a local "
                "administrator nor a directory refuses to start."
            ),
            "caveat": None,
            "label": LOCAL_LABEL,
            "enabled": True,
            "usable": True,
            "missing": [],
            "source": provider_config.SOURCE_ENVIRONMENT,
            "stored": False,
            "endpoint": None,
            "admin_group": None,
            "settings_prefix": "AUTH_",
            # The one method with nothing to configure. Reported rather than
            # omitted, because an operator who cannot see local accounts on
            # this screen concludes they are not a way in.
            "editable": False,
            "fields": [],
            "values": {},
            "secrets_stored": [],
            "secrets_unreadable": False,
        }
    ]
    for kind in provider_config.KINDS:
        row = rows.get(kind)
        cfg = (
            provider_config.from_row(
                kind,
                enabled=row.enabled,
                config=row.config or {},
                secrets=provider_store.decrypt_secrets(row),
            )
            if row is not None
            else provider_config.from_env(kind)
        )
        methods.append(_describe(kind, row, cfg))
    return methods
