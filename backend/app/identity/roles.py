"""
Mapping directory group membership onto a console role.

Two providers need this — LDAP and OIDC — and they must agree, so it lives in one
module rather than twice inside two provider implementations that would drift the
first time one of them grew a special case.

**The whole module exists for the tri-state.** A provider can report three
different things about a person's groups, and only two of them are a list:

``("cn=admins,…", "cn=staff,…")``
    We asked the directory and this is the answer.
``()``
    We asked the directory and the person is in no groups we can see. A real,
    known-empty membership.
``None``
    **We could not look.** The directory answered the bind but did not return the
    membership attribute at all — an ACL that hides ``memberOf`` from the search
    account, a referral chase that was disabled, a server-side size limit that
    truncated the entry.

The first two map to a role. The third must not, and the failure it prevents is
specific and was live in this codebase: ``LDAPProfile.is_admin`` was a bool, an
absent ``memberOf`` produced ``False``, and ``service.authenticate`` wrote that
over the stored role. A directory administrator whose entry omitted the attribute
for one request was **silently demoted to `user` on login** — and the next login
that did return the attribute promoted them back, so the symptom was a console
that intermittently lost administrators with nothing in the logs to connect it to
the directory. That is the repo's own rule ("the caller must be able to tell
'nothing happened' from 'we could not look'") broken inside the identity layer,
where it is least visible and most expensive.

:func:`role_from_groups` therefore returns ``None`` for the third case, and the
callers keep the role the account already had.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: The console's roles. `admin` gates user administration and the audit export;
#: both roles retain the console's normal cluster capabilities, still constrained
#: by Kubernetes preflight and the deployment-wide mutation gate.
ADMIN = "admin"
USER = "user"
ROLES: frozenset[str] = frozenset({ADMIN, USER})

#: Assigned to a federated account whose groups resolved but matched nothing.
DEFAULT_ROLE = USER


def normalize_group(value: str) -> str:
    """Case-folded, whitespace-trimmed group identifier.

    Directory group DNs are case-insensitive by specification and are returned
    with inconsistent casing by real servers (Active Directory in particular
    echoes whatever casing the attribute was written with). Comparing them
    verbatim means an administrator group configured as ``CN=Admins,DC=corp``
    fails to match the ``cn=admins,dc=corp`` the server returned — a
    configuration that looks right, produces no error, and grants nobody
    anything.
    """
    return (value or "").strip().casefold()


def role_from_groups(
    groups: tuple[str, ...] | list[str] | None,
    *,
    admin_group: str,
    provider: str,
) -> str | None:
    """The console role these groups imply, or ``None`` when they are unknowable.

    Args:
        groups: what the provider reported. ``None`` means the membership
            attribute or claim was absent — see the module docstring.
        admin_group: the configured group whose members become administrators.
            Empty means no group grants admin, so every resolved membership maps
            to :data:`DEFAULT_ROLE`.
        provider: ``ldap`` or ``oidc``, for the log line only.

    Returns:
        ``admin``, ``user``, or ``None``. ``None`` is not a refusal and not a
        default — it means this function has nothing to say, and the caller must
        preserve whatever role the account already carried rather than writing
        one.
    """
    if groups is None:
        logger.warning(
            "The %s provider returned no group membership, so no console role "
            "can be derived from it. The account's stored role is left unchanged "
            "— writing the default here would demote an administrator whenever "
            "the directory omitted the attribute.", provider,
        )
        return None

    wanted = normalize_group(admin_group)
    if not wanted:
        return DEFAULT_ROLE

    resolved = {normalize_group(group) for group in groups}
    return ADMIN if wanted in resolved else DEFAULT_ROLE


__all__ = [
    "ADMIN",
    "DEFAULT_ROLE",
    "ROLES",
    "USER",
    "normalize_group",
    "role_from_groups",
]
