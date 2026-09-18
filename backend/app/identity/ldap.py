"""LDAP search-and-bind authentication with encrypted transport required."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from urllib.parse import urlparse

from app.errors import IdentityProviderUnavailable
from app.identity import provider_config

logger = logging.getLogger(__name__)

#: This provider's key in `provider_config.SPECS`. LDAP is not in the single
#: sign-on registry — it has no handshake and no callback — but it is configured
#: the same way as the four that are, so it carries the same name.
NAME = "ldap"


def _cfg() -> provider_config.ProviderConfig:
    """This deployment's ldap configuration: the stored row, else the environment.

    Resolved per call rather than held, because a cached provider configuration
    outlives the edit that changed it — see :mod:`app.identity.provider_store`
    on why nothing there is cached.
    """
    return provider_config.resolve(NAME)



@dataclass(frozen=True)
class LDAPProfile:
    """What one successful search-and-bind learned about a directory user."""

    username: str
    display_name: str | None
    email: str | None
    #: Group DNs as the directory returned them, or ``None`` when the directory
    #: did not return the membership attribute at all.
    #:
    #: The two are not the same fact and the caller must not be able to confuse
    #: them. ``()`` means "we asked and this person is in no groups"; ``None``
    #: means "we could not look" — an ACL hiding ``memberOf`` from the search
    #: account, a disabled referral chase, a truncated entry.
    #:
    #: This used to be ``is_admin: bool``, and an absent attribute produced
    #: ``False``, which :func:`app.identity.service.authenticate` then wrote over
    #: the stored role. A directory administrator was silently demoted on any
    #: login where the attribute did not come back, and restored on the next one
    #: that did — an intermittent loss of administrators with nothing connecting
    #: it to the directory. See :mod:`app.identity.roles`.
    groups: tuple[str, ...] | None


def _value(entry, attribute: str) -> str | None:
    if not attribute or attribute not in entry.entry_attributes:
        return None
    value = entry[attribute].value
    return str(value).strip() if value is not None and str(value).strip() else None


def authenticate(username: str, password: str) -> LDAPProfile | None:
    """Search for a directory user, then prove the password by binding as its DN."""
    cfg = _cfg()
    try:
        from ldap3 import Connection, Server, SUBTREE, Tls
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError as exc:  # pragma: no cover - packaging failure
        raise IdentityProviderUnavailable(
            "LDAP support is enabled but the ldap3 package is not installed."
        ) from exc

    parsed = urlparse(cfg.url)
    if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
        raise IdentityProviderUnavailable(
            "The directory URL must be an ldap:// or ldaps:// URL.",
            hint=provider_config.configure_hint(cfg, "LDAP_URL"),
        )
    use_ssl = parsed.scheme == "ldaps"
    if not use_ssl and not cfg.start_tls:
        raise IdentityProviderUnavailable(
            "LDAP credentials may not be sent in clear text. Use ldaps:// or "
            "enable StartTLS.",
            hint=provider_config.configure_hint(cfg, "LDAP_URL or LDAP_START_TLS"),
        )
    if not cfg.user_search_base:
        raise IdentityProviderUnavailable(
            "A user search base is required to find anybody in the directory.",
            hint=provider_config.configure_hint(cfg, "LDAP_USER_SEARCH_BASE"),
        )
    if "{username}" not in cfg.user_search_filter:
        raise IdentityProviderUnavailable(
            "The user search filter must contain the {username} placeholder, or "
            "it matches the same entry for everybody.",
            hint=provider_config.configure_hint(cfg, "LDAP_USER_SEARCH_FILTER"),
        )

    tls = Tls(
        validate=ssl.CERT_REQUIRED if cfg.tls_validate else ssl.CERT_NONE,
        ca_certs_file=cfg.ca_certificate_file or None,
    )
    server = Server(
        parsed.hostname,
        port=parsed.port or (636 if use_ssl else 389),
        use_ssl=use_ssl,
        tls=tls,
        connect_timeout=cfg.connect_timeout_seconds,
    )

    def connect(user: str = "", secret: str = ""):
        connection = Connection(
            server,
            user=user or None,
            password=secret or None,
            receive_timeout=cfg.connect_timeout_seconds,
            raise_exceptions=False,
        )
        if not connection.open():
            raise IdentityProviderUnavailable("The LDAP server did not accept a connection.")
        if cfg.start_tls and not use_ssl and not connection.start_tls():
            connection.unbind()
            raise IdentityProviderUnavailable("The LDAP server did not establish StartTLS.")
        return connection

    search_connection = None
    user_connection = None
    try:
        search_connection = connect(
            cfg.bind_dn,
            cfg.bind_password,
        )
        if not search_connection.bind():
            raise IdentityProviderUnavailable("The LDAP search account could not bind.")

        attributes = list(
            dict.fromkeys(
                filter(
                    None,
                    (
                        cfg.username_attribute,
                        cfg.display_name_attribute,
                        cfg.email_attribute,
                        "memberOf",
                    ),
                )
            )
        )
        search_filter = cfg.user_search_filter.replace(
            "{username}", escape_filter_chars(username)
        )
        if not search_connection.search(
            cfg.user_search_base,
            search_filter,
            search_scope=SUBTREE,
            attributes=attributes,
            size_limit=2,
        ):
            raise IdentityProviderUnavailable("The LDAP user search failed.")
        if len(search_connection.entries) != 1:
            return None

        entry = search_connection.entries[0]
        user_connection = connect(entry.entry_dn, password)
        if not user_connection.bind():
            return None

        # The attribute being absent from the entry is reported as None, not as
        # an empty membership. Role mapping is the caller's job (roles.py), and
        # it needs to be able to tell "in no groups" from "we did not get to
        # see the groups" — the second must leave a stored role alone.
        if "memberOf" in entry.entry_attributes:
            groups: tuple[str, ...] | None = tuple(
                str(value) for value in entry["memberOf"].values
            )
        else:
            groups = None
            logger.warning(
                "The LDAP entry for %r carried no memberOf attribute. Console "
                "role mapping will be skipped for this login and the stored role "
                "kept. If this is not expected, the search account probably "
                "cannot read memberOf on that entry.", username,
            )
        return LDAPProfile(
            username=_value(entry, cfg.username_attribute) or username,
            display_name=_value(entry, cfg.display_name_attribute),
            email=_value(entry, cfg.email_attribute),
            groups=groups,
        )
    except IdentityProviderUnavailable:
        raise
    except LDAPException as exc:
        logger.warning("LDAP operation failed: %s", type(exc).__name__)
        raise IdentityProviderUnavailable() from exc
    finally:
        if user_connection is not None:
            user_connection.unbind()
        if search_connection is not None:
            search_connection.unbind()