"""
Shared test fixtures.

Three things every backend test needs, provided once here so that no test has to
build them and none of them drift apart:

* an app wired to a throwaway in-memory database,
* a fake Kubernetes client bundle,
* a registered cluster row.

The fake Kubernetes clients have one deliberate sharp edge: **an unstubbed method
raises.** A fake that returned an empty list for anything it was not asked about
would make "empty is never blind" untestable — the test would pass whether the
code reported the failure or swallowed it, which is the exact bug class this
project cares most about. If a test hits the assertion, the fix is to stub the
call (or to notice that the code is making one it should not).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

# Set before any `app.*` import: app.config builds its Settings at import time,
# and app.database builds the engine from those settings at import time too.
# conftest.py is imported before any test module, so this is the last moment the
# environment can still be decided.
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("ENCRYPTION_KEY", "k8boss-admin-test-key")
os.environ.setdefault("ADMIN_ALLOW_MUTATIONS", "false")
os.environ.setdefault("SECRET_REVEAL_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.crypto import encrypt  # noqa: E402
from app.database import Base, get_db  # noqa: E402
from app.k8s.client import manager  # noqa: E402
from app.models import Cluster  # noqa: E402


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

@pytest.fixture
def db_engine(monkeypatch):
    """A per-test in-memory SQLite database, installed over ``app.database``.

    ``StaticPool`` plus a single shared connection: without it every SQLAlchemy
    checkout of ``sqlite://`` opens a *new* empty database, so the tables created
    here would be invisible to the request that follows and every test would fail
    with "no such table" for reasons that look nothing like the cause.

    Patching the module attributes (rather than only overriding the FastAPI
    dependency) matters because several components open their own sessions
    outside the request lifecycle — ``ClusterClientManager`` when it resolves a
    cluster, and ``/api/health`` so that a database failure can be reported
    rather than raised.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)

    Base.metadata.create_all(bind=engine)
    # The same two listeners production installs, installed the same way. A test
    # database without them would let a test pass while asserting a property the
    # deployed app does not have — which is the only thing worse than no test.
    from app.audit.integrity import install_audit_chain
    from app.models import install_audit_append_only_guard

    install_audit_append_only_guard()
    install_audit_chain()

    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """An open session on the test database. Closed for you."""
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

@pytest.fixture
def client(db_engine):
    """A ``TestClient`` for the real app, bound to the test database.

    Constructed without ``with``, so the lifespan does **not** run: the lifespan
    calls ``create_tables()`` against the process-wide engine, which would create
    the schema in the wrong database and, worse, leave a file behind. Nothing in
    the request path depends on the lifespan having run.
    """
    from app.main import app

    app.dependency_overrides[get_db] = _override_get_db
    manager.reset()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        manager.reset()


def _override_get_db():
    session = database.SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def allow_mutations(monkeypatch):
    """Flip ``ADMIN_ALLOW_MUTATIONS`` on for one test.

    Patches the live Settings object rather than the environment, because
    Settings is built once at import and re-reading the environment later would
    have no effect — a test that set the variable and saw nothing change would
    look like a bug in the gate rather than in the test.
    """
    monkeypatch.setattr(settings, "admin_allow_mutations", True)
    return settings


# --------------------------------------------------------------------------- #
# Fake Kubernetes
# --------------------------------------------------------------------------- #

def obj(**fields) -> SimpleNamespace:
    """Build an attribute-access stand-in for a Kubernetes model object.

    Nested structures are written by nesting calls
    (``obj(status=obj(phase="Running"))``); plain dicts are left as dicts,
    because that is what the real client hands back for ``capacity``,
    ``requests``, ``labels`` and ``annotations``.
    """
    return SimpleNamespace(**fields)


class FakeApi:
    """One typed Kubernetes API client, stubbed per method.

    An unstubbed call raises ``AssertionError`` naming the method. That is the
    point: a permissive fake makes a swallowed error indistinguishable from a
    genuinely empty cluster, and those two are the exact pair this project
    refuses to conflate.
    """

    def __init__(self, name: str):
        self._name = name
        self._returns: dict[str, object] = {}
        self._raises: dict[str, BaseException] = {}
        self.calls: list[tuple[str, tuple, dict]] = []

    def returns(self, method: str, value) -> "FakeApi":
        """Stub ``method``. A callable value is invoked with the call's arguments."""
        self._returns[method] = value
        return self

    def raises(self, method: str, error: BaseException) -> "FakeApi":
        """Make ``method`` raise — how a test produces a partial read."""
        self._raises[method] = error
        return self

    def called(self, method: str) -> list[tuple[tuple, dict]]:
        """Arguments of every call to ``method``, in order."""
        return [(args, kwargs) for name, args, kwargs in self.calls if name == method]

    def __getattr__(self, method: str):
        # Guard the dunder/private space so copy, pickle and pytest introspection
        # do not get handed a callable and conclude the object supports them.
        if method.startswith("_"):
            raise AttributeError(method)

        def call(*args, **kwargs):
            self.calls.append((method, args, kwargs))
            if method in self._raises:
                raise self._raises[method]
            if method in self._returns:
                value = self._returns[method]
                return value(*args, **kwargs) if callable(value) else value
            raise AssertionError(
                f"{self._name}.{method}() was called but not stubbed. Add "
                f'fake_k8s.{self._name}.returns("{method}", ...) — or .raises(...) '
                "if this test is about the failure path."
            )

        return call


class FakeClusterClients:
    """Stand-in for :class:`app.k8s.client.ClusterClients`.

    Carries the same attribute names as the real bundle so production code needs
    no test-only branch, and so a client added to the real bundle and forgotten
    here fails loudly with an ``AttributeError`` instead of silently.
    """

    def __init__(self, cluster_id: int | None = 1, platform: str = "kubernetes"):
        self.cluster_id = cluster_id
        self.platform = platform
        self.cache_key = "fake"
        self.api_client = FakeApi("api_client")
        self.core_v1 = FakeApi("core_v1")
        self.apps_v1 = FakeApi("apps_v1")
        self.batch_v1 = FakeApi("batch_v1")
        self.networking_v1 = FakeApi("networking_v1")
        self.rbac_v1 = FakeApi("rbac_v1")
        self.storage_v1 = FakeApi("storage_v1")
        self.authorization_v1 = FakeApi("authorization_v1")
        self.version_api = FakeApi("version_api")
        self.dynamic = FakeApi("dynamic")

    def close(self) -> None:
        """No transport to release; present so production cleanup paths work."""


@pytest.fixture
def fake_k8s(monkeypatch):
    """Install a fake client bundle for every cluster resolution.

    Both entry points are patched. ``get_clients`` is what the request path uses;
    ``get_clients_for_cluster`` is what the connection test uses to bypass the
    cache, and a test that patched only the first would have ``/test`` quietly
    open a real socket to whatever ``api_server`` the fixture invented.
    """
    fake = FakeClusterClients()
    monkeypatch.setattr(manager, "get_clients", lambda cluster_id=None: fake)
    monkeypatch.setattr(manager, "get_clients_for_cluster", lambda cluster: fake)
    return fake


# --------------------------------------------------------------------------- #
# Cluster registration
# --------------------------------------------------------------------------- #

# The token every fixture stores. Tests assert this exact string never appears in
# a response body, so it is deliberately distinctive: a substring search for
# "token" would match field names, and a search for "abc" would match anything.
FIXTURE_TOKEN = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.fixture-secret-do-not-leak"


@pytest.fixture
def registered_cluster(db_engine) -> Cluster:
    """A cluster row with an encrypted token, as if registered through the API."""
    session = database.SessionLocal()
    try:
        cluster = Cluster(
            name="prod-eu",
            platform="kubernetes",
            api_server="https://api.prod-eu.example:6443",
            authentication_type="service_account_token",
            token_encrypted=encrypt(FIXTURE_TOKEN),
            ca_certificate="-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----",
            skip_tls_verify=False,
            status="unknown",
        )
        session.add(cluster)
        session.commit()
        session.refresh(cluster)
        # Detached from the session so a test can read its attributes after the
        # session closes without tripping DetachedInstanceError.
        session.expunge(cluster)
        return cluster
    finally:
        session.close()


@pytest.fixture
def cluster_id(registered_cluster) -> int:
    """Id of the fixture cluster, for tests that only need the id."""
    return registered_cluster.id
