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
* ``AuditRecord`` rows are hash-chained (:mod:`app.audit.integrity`). The guard
  above stops *this process* rewriting a row; it says nothing about a ``psql``
  session. The chain does not prevent that either — it makes it **detectable**,
  which is the honest thing an application can promise about a table it does not
  own the storage of.
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

    # ADR-0007. When true, every cluster call this console makes on behalf of a
    # signed-in operator carries `Impersonate-User` and `Impersonate-Group`, so
    # the API server evaluates authorization, admission and its own audit record
    # as that person rather than as this console's ServiceAccount.
    #
    # Per-cluster and off by default, because the grant it needs — `impersonate`
    # on `users` — is cluster-admin by proxy when it carries no `resourceNames`,
    # and because it is only defensible where the cluster and the console
    # believe the *same* issuer. A deployment with one cluster whose
    # `--oidc-issuer-url` matches the console's and a second with no OIDC at all
    # must be able to have this on for the first and off for the second, which a
    # console-wide flag could not express.
    #
    # Surfaced in every ClusterPublic response beside `skip_tls_verify`, and for
    # the same reason: a setting that changes who the cluster thinks is asking
    # can never be silently in effect.
    #
    # Nullable, unlike `skip_tls_verify`, because it was added to a schema that
    # already had rows and `schema_upgrade` only adds nullable columns — a NOT
    # NULL with a default is a table rewrite, which is the migration that module
    # says out loud it does not perform. NULL means "registered before this
    # setting existed", which is off; every read goes through `bool()` so the
    # third state never reaches a decision. This is the one place in the schema
    # where a null boolean is not a tri-state, and it is safe only because the
    # absent answer and the false answer call for the same behaviour.
    impersonation_enabled = Column(Boolean, nullable=True, default=False)

    # §13. The cluster's wildcard DNS domain — the "apps.<cluster>.example.com"
    # that a generated exposure hostname is built under. A property of the
    # cluster's DNS, never of the console, which is why it lives here and not in
    # Settings: two registered clusters have two different wildcards, and one
    # console-wide value would generate a hostname that resolves on one of them
    # and nowhere on the other.
    #
    # NULL means "we do not know one", and that is not the same as "there isn't
    # one". Both render the same way — the route dialog offers no generated
    # hostname — because offering `shop-web.apps.example.com` on a cluster with
    # no matching wildcard record produces an exposure that is created, looks
    # correct, and routes nothing: §14's failure with a hostname instead of a
    # controller.
    app_domain = Column(String(253), nullable=True)

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
            "impersonation_enabled": bool(self.impersonation_enabled),
            "app_domain": self.app_domain,
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

    # The identity provider's own stable identifier for this person — the OIDC
    # `sub` claim. Null for local and LDAP accounts, which are keyed by username.
    #
    # It exists because a username is not an identity across providers. Once a
    # second issuer can mint logins, "alice" at a low-trust IdP must not resolve
    # to the "alice" the corporate IdP created and inherit her role. Binding the
    # row to the subject on first federated login turns that from a silent
    # account takeover into a refusal (see app.identity.oidc.provision).
    external_id = Column(String(255), nullable=True, index=True)

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
            "external_id": self.external_id,
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

    # ADR-0007. What the identity provider said about this person, kept verbatim
    # for the life of this session and nowhere else.
    #
    # On the *session* rather than on `users` because it is a property of one
    # sign-in: an operator's groups change, and the row that outlives the
    # assertion must not go on asserting what the assertion said last month.
    #
    # `idp_username` is the claim as the issuer sent it, before
    # `normalize_username` casefolds it for the console's own account table. The
    # console's username is this application's key; `Impersonate-User` has to be
    # the string the *cluster* would derive from the same token, and a cluster
    # whose `--oidc-username-claim` yields `Alice@example.com` does not know
    # anybody called `alice@example.com`.
    idp_username = Column(String(255), nullable=True)

    # A JSON array, and NULL is load-bearing: it means the issuer did not send a
    # groups claim, which is not the same as sending an empty one. Impersonating
    # with no groups when the claim was merely absent strips every group-derived
    # permission the operator holds and reports the result as permissions they
    # lack — so an absent claim refuses instead. `[]` is a real answer and
    # impersonates fine.
    idp_groups = Column(Text, nullable=True)


#: ``category`` values. Two kinds of record live in one table because they answer
#: one question — "who did what to this console and its clusters" — and splitting
#: them into two tables would mean an incident review has to remember to read
#: both. The column exists so a filter can separate them without parsing JSON,
#: which is the one thing in this schema that would be engine-divergent.
CATEGORY_CLUSTER = "cluster"   # a write aimed at a Kubernetes API server
CATEGORY_CONSOLE = "console"   # a sign-in, a sign-out, a console user change
CATEGORIES: frozenset[str] = frozenset({CATEGORY_CLUSTER, CATEGORY_CONSOLE})


class LoginAttempt(Base):
    """One in-flight or failed sign-in, counted by the throttle.

    **Why this is not simply a COUNT over the audit trail.** That was the first
    design, and it has two holes that only show up under the conditions the
    throttle exists for:

    * *Check-then-act.* The audit row for a rejection is written **after** the
      password is verified, so a burst of simultaneous requests all run their
      COUNT before any of them has recorded anything, all see the same number,
      and all proceed. Measured: 30 concurrent guesses against a documented
      budget of 3, none refused, every one of them reaching PBKDF2.
    * *A failed INSERT disabled the limiter silently.* If the audit write could
      not happen — disk full, a hot standby, INSERT revoked while SELECT was
      retained — the COUNT returned a genuine, indistinguishable `0`, and the
      login endpoint became the unmetered password oracle the throttle exists to
      prevent, with nothing recorded and no signal that counting had stopped.

    A row here is inserted **before** the password check, which makes the count
    atomic per attempt rather than a read of somebody else's writes, and makes
    the limiter independent of whether the audit write succeeds. It is deleted
    again when the sign-in succeeds, so a working account never accumulates a
    budget against itself.

    This is a rate-limiting bucket, not an audit record. It is deliberately
    *not* append-only, holds nothing but a username and a timestamp, and is
    pruned. The trail remains the record of what happened; §10 is unaffected.
    """

    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # The submitted username, normalised where it could be. Nothing has verified
    # it — that is the whole point of counting it.
    actor = Column(String(255), nullable=False)
    ts = Column(DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        # The only query: recent attempts for one actor.
        Index("ix_login_attempt_actor_ts", "actor", "ts"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LoginAttempt id={self.id} actor={self.actor!r}>"


class AuditRecord(Base):
    """One attempted write, recorded whether or not it reached the cluster.

    Append-only. Denials and failures are recorded too — an audit trail of only
    the successful writes answers "what changed" but not "who tried", and the
    second question is the one asked after an incident.
    """

    __tablename__ = "audit_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(DateTime, nullable=False, default=utcnow)

    # cluster | console.
    #
    # Rows written before this column existed hold NULL, and are **not**
    # back-filled — this table is append-only, and a migration that learned to
    # UPDATE it would establish that the guard can be worked around. A null is
    # instead *read* as `cluster` (see to_row_dict and the query filter), which
    # is what all but a handful of pre-upgrade rows are and what every one of
    # them is closer to than `console`.
    #
    # The alternative was to leave the filter matching only non-null values, and
    # that is the actual defect: a filter for "cluster writes" would silently
    # omit every record written before the upgrade, and a filter for "console
    # records" would omit them too — rows that exist, that an operator is looking
    # straight at in the unfiltered view, and that no filter can reach.
    category = Column(String(16), nullable=True, default=CATEGORY_CLUSTER)

    # Verified session username when application auth is enabled. In legacy
    # proxy mode this is the advisory X-K8Boss-User value, or "anonymous".
    actor = Column(String(255), nullable=False, default="anonymous")
    source_ip = Column(String(64), nullable=True)

    # ADR-0007. The cluster identity this write was actually made as, when the
    # cluster impersonates; NULL when it was made as the console's
    # ServiceAccount, which is every row this table held before ADR-0007 and
    # every row on every cluster that has not opted in.
    #
    # Both are recorded because they answer different questions. `actor` is who
    # used this console and is a name only this application can vouch for.
    # `impersonated_user` is the name the *API server* saw, evaluated
    # authorization against, and wrote into its own audit log — so an incident
    # review can join this trail to the cluster's by a value neither side
    # invented, which is a materially stronger claim than ADR-0003 can make
    # about a table this application owns.
    #
    # NULL is never rendered as "unknown". It means the console acted as itself,
    # which is a fact, and the §10 row says so in those words.
    impersonated_user = Column(String(255), nullable=True)

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

    # -- tamper evidence (app.audit.integrity) ---------------------------
    #
    # Both are NULL for a row written before chaining existed, and for the
    # last-resort path where the record was preserved but could not be linked.
    # NULL is a third state and is reported as one: a verifier says "unchained",
    # never "verified" and never "broken". Claiming either about a row we cannot
    # speak for is the defect this whole mechanism exists to avoid making.
    #
    # UNIQUE on prev_hash is load-bearing, not a tidiness constraint. It is what
    # makes a fork impossible rather than merely detectable: two replicas that
    # read the same chain tip compute the same prev_hash, and the second INSERT
    # is refused by the database instead of silently branching the chain. The
    # writer retries against the new tip. Multiple NULLs are permitted by a
    # UNIQUE index on both SQLite and PostgreSQL, so unchained rows do not
    # collide with each other.
    prev_hash = Column(String(64), nullable=True, unique=True)
    event_hash = Column(String(64), nullable=True, unique=True)

    __table_args__ = (
        # The audit page is always scoped to a cluster and paged by descending
        # id, so the composite index serves both the filter and the ordering
        # from one scan. Separately on ts for the `since` filter, which is not
        # cluster-scoped.
        Index("ix_audit_cluster_id", "cluster_id", "id"),
        Index("ix_audit_ts", "ts"),
        # The audit page separates console sign-ins from cluster writes, and the
        # login throttle counts recent console denials for one actor. Both are
        # (category, ts) scans.
        Index("ix_audit_category_ts", "category", "ts"),
        # The throttle's exact query: recent records for one actor. Without it,
        # every login attempt table-scans a table that only ever grows.
        Index("ix_audit_actor_ts", "actor", "ts"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AuditRecord id={self.id} {self.verb} {self.outcome}>"

    def to_row_dict(self) -> dict[str, Any]:
        """The §10 audit row."""
        return {
            "id": self.id,
            "ts": rfc3339(self.ts),
            # Never null on the wire. A consumer that had to special-case a null
            # here would be re-deriving the same "this predates the column"
            # reasoning at every call site, and the first one to forget renders
            # it as a blank cell.
            "category": self.category or CATEGORY_CLUSTER,
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
            # ADR-0007. NULL on the wire too, and it means "this console acted
            # as itself" rather than "we do not know". Not defaulted to `actor`:
            # that would claim the API server saw a name it never saw, which is
            # the attribution ADR-0003 refuses to manufacture.
            "impersonated_user": self.impersonated_user,
            # The chain link, echoed so an exported trail can be verified by
            # something that is not this console. Both null means the row is
            # outside the chain, which GET /api/audit/verify reports as
            # `unchained` rather than folding into either verdict.
            "prev_hash": self.prev_hash,
            "event_hash": self.event_hash,
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
