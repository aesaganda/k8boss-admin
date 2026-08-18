"""
Persistent models: registered clusters, and the audit trail.

Two invariants are enforced here rather than in review:

* ``Cluster.to_public_dict`` is built from an explicit allowlist and asserts that
  no credential-bearing column got into the result. A serializer that walks the
  model's columns would start leaking the day someone adds a second credential
  field, and the test that catches it is one nobody thought to write.
* ``AuditRecord`` is append-only, enforced by a session hook that refuses to
  flush an update or a delete. An audit trail that can be edited answers a
  different question from the one operators believe they are asking it.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    event,
)

from sqlalchemy.orm import Session as OrmSession

from app.database import Base

logger = logging.getLogger(__name__)


def utcnow() -> datetime.datetime:
    """Naive UTC now. One helper so every column agrees on the timezone."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def rfc3339(dt: datetime.datetime | None) -> str | None:
    """Serialize a stored datetime as the contract's RFC 3339 UTC form.

    The schema stores naive UTC. Serialized without an offset, JavaScript's
    ``new Date()`` parses it as LOCAL time and shifts every timestamp by the
    viewer's UTC offset — which in k8boss made fresh heartbeats render as
    "Stale", i.e. a correct value displayed as a wrong one. The trailing ``Z``
    is not decoration.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Columns on Cluster that hold, or can hold, credential material. Named here so
# to_public_dict can assert against the list instead of trusting that whoever
# adds the next one also remembers to exclude it.
_CLUSTER_SECRET_COLUMNS = ("token_encrypted",)


class Cluster(Base):
    """A registered cluster the console administers.

    The bearer token is stored encrypted (``app.crypto``) and never leaves this
    process. Connections are built on demand from ``api_server`` plus the
    decrypted token; there is no kubeconfig on the server.
    """

    __tablename__ = "clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, unique=True)
    platform = Column(String(50), nullable=False, default="kubernetes")
    api_server = Column(String(1024), nullable=False)
    authentication_type = Column(
        String(50), nullable=False, default="service_account_token"
    )

    # Encrypted at rest. Never returned by any endpoint; see to_public_dict.
    token_encrypted = Column(Text, nullable=True)

    # The CA is public material by definition — it is the certificate the API
    # server presents to anyone who connects — so it is stored in the clear. The
    # API still never returns it, only ``has_ca_certificate``, because a UI has
    # no use for the bytes and echoing stored input back is how a field that is
    # public today becomes a leak after someone repurposes it.
    ca_certificate = Column(Text, nullable=True)
    skip_tls_verify = Column(Boolean, nullable=False, default=False)

    # Last observed connectivity. Written by POST /api/clusters/{id}/test and by
    # any endpoint that successfully reaches the cluster.
    #
    # "unknown" is a real state and is NOT merged into "disconnected": a cluster
    # registered a minute ago and never tested has not failed, and reporting it
    # as failing sends an operator to debug a healthy cluster. GET /api/health
    # keeps the two apart for the same reason.
    status = Column(String(50), nullable=False, default="unknown")
    status_detail = Column(Text, nullable=True)
    server_version = Column(String(64), nullable=True)
    last_connected = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Cluster id={self.id} name={self.name!r} status={self.status}>"

    def to_public_dict(self) -> dict[str, Any]:
        """The §3 ``ClusterPublic`` body. Cannot contain credential material.

        Built from a literal allowlist, then asserted against the known secret
        columns. Both halves matter: the allowlist is what keeps a new column
        from appearing in responses by default, and the assertion is what turns
        "someone added the new secret to the allowlist by habit" from a silent
        credential leak into a failed request and a stack trace. The check costs
        a set intersection per cluster row.
        """
        public = {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "api_server": self.api_server,
            "authentication_type": self.authentication_type,
            "has_ca_certificate": bool(self.ca_certificate),
            "skip_tls_verify": bool(self.skip_tls_verify),
            "status": self.status,
            "server_version": self.server_version,
            "last_connected": rfc3339(self.last_connected),
            "created_at": rfc3339(self.created_at),
            "updated_at": rfc3339(self.updated_at),
        }
        leaked = set(public) & set(_CLUSTER_SECRET_COLUMNS)
        if leaked:
            raise RuntimeError(
                "Cluster.to_public_dict would have returned credential columns "
                f"{sorted(leaked)}. Refusing to serialize."
            )
        return public


class User(Base):
    """A console identity, sourced locally or synchronized from LDAP."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    email = Column(String(320), nullable=True)
    role = Column(String(32), nullable=False, default="user")
    auth_source = Column(String(32), nullable=False, default="local")
    password_hash = Column(Text, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    last_login = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    updated_at = Column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def to_public_dict(self) -> dict[str, Any]:
        """Serialize identity metadata without password material."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "role": self.role,
            "auth_source": self.auth_source,
            "active": bool(self.active),
            "last_login": rfc3339(self.last_login),
            "created_at": rfc3339(self.created_at),
            "updated_at": rfc3339(self.updated_at),
        }


class AuthSession(Base):
    """A revocable browser session; the bearer token itself is never stored."""

    __tablename__ = "auth_sessions"

    token_hash = Column(String(64), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    csrf_token = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow)


class AuditRecord(Base):
    """One attempted write, recorded whether or not it reached the cluster.

    Append-only. Denials and failures are recorded too — an audit trail of only
    the successful writes answers "what changed" but not "who tried", and the
    second question is the one asked after an incident.
    """

    __tablename__ = "audit_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=utcnow)

    # Verified session username when application auth is enabled. In legacy
    # proxy mode this is the advisory X-K8Boss-User value, or "anonymous".
    actor = Column(String(255), nullable=False, default="anonymous")
    source_ip = Column(String(64), nullable=True)

    cluster_id = Column(Integer, nullable=True)
    # Denormalised on purpose. A cluster can be de-registered, and an audit row
    # that then renders as "cluster 7" is unreadable exactly when it is needed.
    # The name at the time of the write is the fact worth keeping; a join to a
    # deleted row is not.
    cluster_name = Column(String(255), nullable=True)

    verb = Column(String(32), nullable=False)
    # {group, version, resource, namespace, name} — JSON rather than five
    # columns because it is echoed back verbatim in §10 and never filtered on;
    # the query surface is (cluster_id, actor, outcome, since) only. JSON is a
    # native type on both SQLite and PostgreSQL, so this stays engine-neutral.
    target = Column(JSON, nullable=False, default=dict)

    dry_run = Column(Boolean, nullable=False, default=True)
    # applied | dry_run | denied | failed | conflict
    outcome = Column(String(32), nullable=False)
    detail = Column(Text, nullable=True)
    # sha256 of the unified diff. The diff itself is deliberately not stored: it
    # can contain Secret data and ConfigMap payloads, and an audit table is a
    # much less carefully guarded place than the cluster those values came from.
    # The digest still proves that what was confirmed is what was applied.
    diff_digest = Column(String(80), nullable=True)
    error = Column(Text, nullable=True)

    __table_args__ = (
        # The audit page is always scoped to a cluster and paged by descending
        # id, so the composite index serves both the filter and the ordering
        # from one scan. Separately on ts for the `since` filter, which is not
        # cluster-scoped.
        Index("ix_audit_cluster_id", "cluster_id", "id"),
        Index("ix_audit_ts", "ts"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditRecord id={self.id} {self.verb} {self.outcome}>"

    def to_row_dict(self) -> dict[str, Any]:
        """The §10 audit row."""
        return {
            "id": self.id,
            "ts": rfc3339(self.ts),
            "actor": self.actor,
            "cluster_id": self.cluster_id,
            "cluster_name": self.cluster_name,
            "verb": self.verb,
            "target": dict(self.target or {}),
            "dry_run": bool(self.dry_run),
            "outcome": self.outcome,
            "detail": self.detail,
            "diff_digest": self.diff_digest,
            "error": self.error,
            "source_ip": self.source_ip,
        }


class AuditImmutableError(RuntimeError):
    """Raised when something tries to modify or delete an existing audit record."""


def _reject_audit_mutation(session, flush_context, instances) -> None:
    """before_flush hook: audit records are insert-only.

    Enforced in the ORM rather than trusted to convention, because the cost of
    being wrong is asymmetric — a rejected flush is a stack trace in a test,
    while a silently rewritten audit row is an incident investigation reaching a
    false conclusion. Deletes are refused for the same reason: §10 states there
    is no delete endpoint, and this is what makes that statement true rather
    than merely intended.
    """
    offenders = [obj for obj in session.dirty if isinstance(obj, AuditRecord)
                 and session.is_modified(obj, include_collections=False)]
    offenders += [obj for obj in session.deleted if isinstance(obj, AuditRecord)]
    if offenders:
        ids = sorted(str(getattr(o, "id", "?")) for o in offenders)
        raise AuditImmutableError(
            "Audit records are append-only; refusing to update or delete "
            f"record(s) {', '.join(ids)}."
        )


def install_audit_append_only_guard() -> None:
    """Register the append-only hook. Idempotent.

    Bound to the ``Session`` class, not to ``SessionLocal``: a hook on one
    sessionmaker protects only the sessions that factory produces, and the tests
    (and any future second engine) build their own. A guarantee that holds for
    most sessions is not a guarantee.
    """
    if not event.contains(OrmSession, "before_flush", _reject_audit_mutation):
        event.listen(OrmSession, "before_flush", _reject_audit_mutation)
