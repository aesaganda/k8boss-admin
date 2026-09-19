"""
§34 — onboarding: discovering a local cluster and adopting it.

The feature exists to remove four commands from a first run, so most of what is
worth testing is what it *refuses* to do:

* a context whose credential cannot be copied is **listed with the reason**, not
  dropped. §0.1 applies to a kubeconfig the same way it applies to a cluster: an
  operator whose only context is an EKS `exec` context has to be able to tell
  "this console will not run your credential plugin" from "you have no
  kubeconfig", and an omission says neither.
* a missing or unreadable kubeconfig is an `unavailable` entry and a 200. The
  unreadable case is the one that matters — a mode-600 file against a container
  running as another uid produced no signal at all before this.
* adoption never touches a registry that is not empty, never registers a remote
  context, and never picks between two local ones.
* neither the discovery listing nor any cluster response ever carries the client
  key. Asserted by scanning whole bodies, like §3's token test, because the
  interesting leak is never a field called `key`.
"""

from __future__ import annotations

import base64
import builtins
import os
import stat

import pytest

from app import database
from app.crypto import decrypt
from app.k8s import adoption, kubeconfig
from app.models import Cluster

# A distinctive private key, so a substring search for it in a response body
# means something. Not a real key — nothing here builds a TLS connection.
FIXTURE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nfixture-client-key-do-not-leak\n-----END RSA PRIVATE KEY-----"
FIXTURE_CERT = "-----BEGIN CERTIFICATE-----\nfixture-client-cert\n-----END CERTIFICATE-----"
FIXTURE_CA = "-----BEGIN CERTIFICATE-----\nfixture-ca\n-----END CERTIFICATE-----"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _kubeconfig(*, contexts: str, current: str = "kind-dev") -> str:
    return f"""
apiVersion: v1
kind: Config
current-context: {current}
{contexts}
"""


KIND_CONTEXT = f"""
clusters:
  - name: kind-dev
    cluster:
      server: https://127.0.0.1:6443
      certificate-authority-data: {_b64(FIXTURE_CA)}
users:
  - name: kind-dev
    user:
      client-certificate-data: {_b64(FIXTURE_CERT)}
      client-key-data: {_b64(FIXTURE_KEY)}
contexts:
  - name: kind-dev
    context:
      cluster: kind-dev
      user: kind-dev
"""


# A bare `curl -sfL https://get.k3s.io | sh -` install, byte for byte in shape:
# every entry in the file — context, cluster and user — is called `default`, and
# nothing in it names k3s at all. The path it is read from is the only evidence
# there is.
K3S_CONTEXT = f"""
clusters:
  - name: default
    cluster:
      server: https://127.0.0.1:6443
      certificate-authority-data: {_b64(FIXTURE_CA)}
users:
  - name: default
    user:
      client-certificate-data: {_b64(FIXTURE_CERT)}
      client-key-data: {_b64(FIXTURE_KEY)}
contexts:
  - name: default
    context:
      cluster: default
      user: default
"""


MIXED_CONTEXTS = f"""
clusters:
  - name: kind-dev
    cluster:
      server: https://127.0.0.1:6443
      certificate-authority-data: {_b64(FIXTURE_CA)}
  - name: k3d-lab
    cluster:
      server: https://0.0.0.0:35001
      insecure-skip-tls-verify: true
  - name: prod-eks
    cluster:
      server: https://ABC.gr7.eu-west-1.eks.amazonaws.com
contexts:
  - name: kind-dev
    context: {{cluster: kind-dev, user: kind-dev, namespace: shop}}
  - name: k3d-lab
    context: {{cluster: k3d-lab, user: k3d-lab}}
  - name: prod-eks
    context: {{cluster: prod-eks, user: prod-eks}}
users:
  - name: kind-dev
    user:
      client-certificate-data: {_b64(FIXTURE_CERT)}
      client-key-data: {_b64(FIXTURE_KEY)}
  - name: k3d-lab
    user:
      token: k3d-service-account-token
  - name: prod-eks
    user:
      exec:
        apiVersion: client.authentication.k8s.io/v1beta1
        command: aws
        args: [eks, get-token]
"""


@pytest.fixture
def kubeconfig_file(tmp_path, monkeypatch):
    """Write a kubeconfig and point the reader at it.

    Patches ``default_path`` rather than the setting, because ``KUBECONFIG`` in
    the environment wins over the setting and a developer running the suite with
    one exported would otherwise have their own clusters discovered by these
    tests.
    """
    def write(text: str, *, mode: int = 0o600) -> str:
        path = tmp_path / "config"
        path.write_text(text)
        os.chmod(path, mode)
        monkeypatch.setattr(kubeconfig, "default_path", lambda: str(path))
        return str(path)

    return write


@pytest.fixture
def on_the_host(monkeypatch):
    """Report this process as running outside a container.

    `running_in_container` is cached and reads the real filesystem, so on CI —
    which is a container — every loopback candidate would carry a reachability
    concern and none would be adoptable. That is correct behaviour and it is
    tested on its own below; every other test here wants the developer-laptop
    case.
    """
    monkeypatch.setattr(kubeconfig, "running_in_container", lambda: False)


@pytest.fixture
def at_the_k3s_path(monkeypatch):
    """Make a kubeconfig in ``tmp_path`` count as the one k3s writes.

    The suite cannot write `/etc/rancher/k3s/k3s.yaml`, so the *table* is
    extended rather than the filesystem — and the display name is read out of the
    shipped entry rather than spelled again here, so deleting that entry fails
    these tests instead of leaving them passing against a mapping only the tests
    contain. That the real path is in the table is asserted separately, against
    `classify` directly, which never opens the path it is given.
    """
    def register(path: str) -> None:
        # Through `realpath`, because that is what `classify` compares and
        # `tmp_path` lives under a symlinked `/var` on macOS.
        monkeypatch.setitem(
            kubeconfig.LOCAL_DISTRIBUTION_PATHS,
            os.path.realpath(path),
            kubeconfig.LOCAL_DISTRIBUTION_PATHS["/etc/rancher/k3s/k3s.yaml"],
        )

    return register


# --------------------------------------------------------------------------- #
# Reading the file
# --------------------------------------------------------------------------- #

def test_every_context_is_listed_including_the_ones_it_refuses(kubeconfig_file, on_the_host):
    """§0.1 for a kubeconfig: a refusal is a listed row with a sentence on it."""
    kubeconfig_file(_kubeconfig(contexts=MIXED_CONTEXTS))

    found = kubeconfig.discover()
    by_name = {candidate.context: candidate for candidate in found.candidates}

    assert set(by_name) == {"kind-dev", "k3d-lab", "prod-eks"}

    assert by_name["kind-dev"].importable is True
    assert by_name["kind-dev"].distribution == "kind"
    assert by_name["kind-dev"].authentication_type == "client_certificate"
    assert by_name["kind-dev"].namespace == "shop"

    assert by_name["k3d-lab"].importable is True
    assert by_name["k3d-lab"].distribution == "k3d"
    assert by_name["k3d-lab"].authentication_type == "kubeconfig_token"
    assert by_name["k3d-lab"].skip_tls_verify is True

    # The one that matters: present, refused, and the refusal says what to do
    # instead rather than "unsupported".
    eks = by_name["prod-eks"]
    assert eks.importable is False
    assert eks.credential == "exec"
    assert "credential plugin" in eks.reason
    assert "ServiceAccount token" in eks.reason
    assert eks.authentication_type is None


def test_a_remote_context_is_never_local_however_it_is_named(kubeconfig_file, on_the_host):
    """Both halves of the local test, and the half that is easy to drop.

    A context called `kind-prod` pointing at a public endpoint is not a local
    cluster, and treating it as one is how startup adoption would register
    somebody's production API server on its own initiative.
    """
    kubeconfig_file(_kubeconfig(contexts=f"""
clusters:
  - name: kind-prod
    cluster: {{server: https://api.prod.example.com:6443}}
contexts:
  - name: kind-prod
    context: {{cluster: kind-prod, user: kind-prod}}
users:
  - name: kind-prod
    user: {{token: a-real-token}}
""", current="kind-prod"))

    candidate = kubeconfig.discover().candidates[0]
    assert candidate.distribution == "kind"
    assert candidate.is_local is False
    assert kubeconfig.adoptable(kubeconfig.discover()) == []


def test_the_k3s_path_is_the_evidence_and_the_name_never_is():
    """`classify` on its own, against the table this console actually ships.

    Both directions in one place, because they are one decision. `default` is
    the name k3s gives every entry in its file and it is also what a remote
    cluster's context is routinely called, so the name can never be the signal —
    adding it to `LOCAL_DISTRIBUTIONS` is the regression this test exists to
    catch. The path can: `/etc/rancher/k3s/k3s.yaml` was written by k3s by
    construction, and it applies to whatever the context inside was renamed to.

    No file is written: `classify` never opens the path it is given, which is
    why the real path can be asserted here and nowhere else in this suite.
    """
    assert kubeconfig.classify("default", "default", "/etc/rancher/k3s/k3s.yaml") == "k3s"
    assert kubeconfig.classify("renamed", "default", "/etc/rancher/k3s/k3s.yaml") == "k3s"
    assert kubeconfig.classify("default", "default", "/etc/rancher/rke2/rke2.yaml") == "RKE2"

    # The name, from anywhere else — including the merged kubeconfig, where the
    # path evidence is gone and `default` is all that is left.
    assert kubeconfig.classify("default", "default") is None
    assert kubeconfig.classify("default", "default", "/home/operator/.kube/config") is None


def test_a_bare_k3s_install_is_a_local_cluster_and_is_adopted(
    db_engine, kubeconfig_file, at_the_k3s_path, on_the_host
):
    """The install this feature missed: `curl -sfL https://get.k3s.io | sh -`.

    Nothing in the file names k3s — every entry is `default` — so before the
    path was read this was classified remote, shown under "remote", and never
    adopted, though its API server is on `127.0.0.1` and importing it by hand
    worked perfectly.
    """
    at_the_k3s_path(kubeconfig_file(_kubeconfig(contexts=K3S_CONTEXT, current="default")))

    found = kubeconfig.discover()
    candidate = found.candidates[0]
    assert candidate.distribution == "k3s"
    assert candidate.is_local is True
    assert candidate.importable is True
    assert [c.context for c in kubeconfig.adoptable(found)] == ["default"]

    assert adoption.adopt_local_cluster() == "default"

    session = database.SessionLocal()
    try:
        row = session.query(Cluster).one()
        assert row.origin == "autodiscovered"
        assert row.api_server == "https://127.0.0.1:6443"
        assert row.authentication_type == "client_certificate"
    finally:
        session.close()


def test_the_same_file_moved_off_that_path_is_remote_again(
    db_engine, kubeconfig_file, on_the_host
):
    """The limit of the signal, stated as a test.

    `KUBECONFIG=~/.kube/config:/etc/rancher/k3s/k3s.yaml kubectl config view
    --flatten > ~/.kube/config` produces this: identical bytes, no path evidence,
    and a context called `default`. It is remote, and it is imported by hand —
    the alternative is reading the name, which is the thing that must not
    happen.
    """
    kubeconfig_file(_kubeconfig(contexts=K3S_CONTEXT, current="default"))

    candidate = kubeconfig.discover().candidates[0]
    assert candidate.distribution is None
    assert candidate.is_local is False
    # Still listed, still importable by hand — §0.1. Only adoption declines.
    assert candidate.importable is True
    assert adoption.adopt_local_cluster() is None


def test_a_default_context_pointing_somewhere_public_is_never_adopted(
    db_engine, kubeconfig_file, at_the_k3s_path, on_the_host
):
    """The half of `is_local` the new signal must not be allowed to bypass.

    Even from the k3s path, the address decides: a `default` context pointing at
    a public endpoint is remote and is never registered by a console nobody
    asked. The two tests are `and`, and the path changed only which distributions
    the first one recognises.
    """
    at_the_k3s_path(kubeconfig_file(_kubeconfig(contexts=f"""
clusters:
  - name: default
    cluster: {{server: https://api.prod.example.com:6443}}
contexts:
  - name: default
    context: {{cluster: default, user: default}}
users:
  - name: default
    user: {{token: a-real-token}}
""", current="default")))

    candidate = kubeconfig.discover().candidates[0]
    assert candidate.distribution == "k3s"
    assert candidate.is_local is False
    assert kubeconfig.adoptable(kubeconfig.discover()) == []
    assert adoption.adopt_local_cluster() is None


def test_a_missing_kubeconfig_is_reported_not_an_empty_list(tmp_path, monkeypatch):
    missing = str(tmp_path / "absent")
    monkeypatch.setattr(kubeconfig, "default_path", lambda: missing)

    found = kubeconfig.discover()
    assert found.candidates == ()
    assert [entry["reason"] for entry in found.unavailable] == ["not_found"]
    assert missing in found.unavailable[0]["detail"]


def test_an_unreadable_kubeconfig_says_so_rather_than_looking_empty(
    kubeconfig_file, monkeypatch
):
    """The Compose failure README has warned about since before it was detectable.

    A mode-600 kubeconfig owned by the operator, read by a container running as
    uid 10001. Before §34 the console started, reported no clusters, and looked
    exactly like a machine with no kubeconfig on it.

    The refusal is injected rather than produced with ``chmod``, because this
    suite runs as root in CI and root reads a mode-000 file — a test that
    skipped there would be a test of this exact path that never ran on the one
    machine that gates the merge.
    """
    path = kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))
    real_open = builtins.open

    def refuse(file, *args, **kwargs):
        if str(file) == path:
            raise PermissionError(13, "Permission denied")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse)

    found = kubeconfig.discover()
    assert found.candidates == ()
    assert [entry["reason"] for entry in found.unavailable] == ["forbidden"]
    assert "cannot read it" in found.unavailable[0]["detail"]


def test_a_file_that_is_not_a_kubeconfig_is_not_a_crash(kubeconfig_file):
    kubeconfig_file("this: [is, not, a, kubeconfig")

    found = kubeconfig.discover()
    assert found.candidates == ()
    assert found.unavailable[0]["reason"] == "unsupported"


def test_a_loopback_context_inside_a_container_carries_a_concern(kubeconfig_file, monkeypatch):
    """Stated, and stated as a concern rather than as a refusal.

    Registering `https://127.0.0.1:6443` from inside a container produces a
    cluster that lists, looks correct and reaches nothing.
    """
    monkeypatch.setattr(kubeconfig, "running_in_container", lambda: True)
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    found = kubeconfig.discover()
    candidate = found.candidates[0]
    assert candidate.importable is True
    assert "loopback address" in candidate.concern
    # ...and it is what keeps adoption from writing a row that cannot connect.
    assert kubeconfig.adoptable(found) == []


def test_the_unspecified_address_k3d_writes_is_the_same_failure(
    kubeconfig_file, monkeypatch
):
    """`0.0.0.0` means "this host" exactly as `127.0.0.1` does.

    Some k3d versions write it, and a check that only looked for loopback would
    let precisely one of the two tools this feature is named after through with
    a registration that cannot connect.
    """
    monkeypatch.setattr(kubeconfig, "running_in_container", lambda: True)
    kubeconfig_file(_kubeconfig(contexts=MIXED_CONTEXTS))

    found = kubeconfig.discover()
    concerns = {c.context: bool(c.concern) for c in found.candidates}
    assert concerns == {"kind-dev": True, "k3d-lab": True, "prod-eks": False}


# --------------------------------------------------------------------------- #
# The endpoints
# --------------------------------------------------------------------------- #

def test_discovery_returns_the_envelope_and_no_credential(client, kubeconfig_file, on_the_host):
    kubeconfig_file(_kubeconfig(contexts=MIXED_CONTEXTS))

    response = client.get("/api/clusters/discovery")
    assert response.status_code == 200
    body = response.json()

    assert body["partial"] is False
    assert len(body["items"]) == 3
    assert body["source"]["current_context"] == "kind-dev"
    assert body["auto_discovery"]["enabled"] is True
    assert body["auto_discovery"]["candidates"] == ["kind-dev", "k3d-lab"]

    # The whole body, recursively — a key that leaked into a detail string three
    # levels down is the leak a field-name check misses.
    assert FIXTURE_KEY not in response.text
    assert "k3d-service-account-token" not in response.text
    assert FIXTURE_CERT not in response.text


def test_discovery_is_partial_when_the_file_cannot_be_read(client, tmp_path, monkeypatch):
    monkeypatch.setattr(kubeconfig, "default_path", lambda: str(tmp_path / "absent"))

    body = client.get("/api/clusters/discovery").json()
    assert body["items"] == []
    assert body["partial"] is True
    assert body["unavailable"][0]["resource"] == "kubeconfig"


def test_import_copies_the_credential_into_the_registry(client, kubeconfig_file, on_the_host):
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    response = client.post("/api/clusters/import", json={"context": "kind-dev"})
    assert response.status_code == 201
    body = response.json()

    assert body["name"] == "kind-dev"
    assert body["api_server"] == "https://127.0.0.1:6443"
    assert body["authentication_type"] == "client_certificate"
    assert body["has_client_certificate"] is True
    assert body["has_ca_certificate"] is True
    assert body["origin"] == "kubeconfig"
    # Never connected to yet, which is not the same as failed.
    assert body["status"] == "unknown"
    assert FIXTURE_KEY not in response.text

    session = database.SessionLocal()
    try:
        row = session.get(Cluster, body["id"])
        # Encrypted at rest, and decryptable — a column holding the ciphertext of
        # the wrong thing passes a "is not the plaintext" check.
        assert row.client_key_encrypted != FIXTURE_KEY
        assert decrypt(row.client_key_encrypted) == FIXTURE_KEY
        assert row.client_certificate.strip() == FIXTURE_CERT
        assert row.token_encrypted is None
    finally:
        session.close()


def test_import_refuses_a_context_it_cannot_copy(client, kubeconfig_file, on_the_host):
    """Re-checked at import, not trusted from the listing.

    The listing is a separate request; a kubeconfig edited between the two would
    otherwise be imported on the strength of what it used to say.
    """
    kubeconfig_file(_kubeconfig(contexts=MIXED_CONTEXTS))

    response = client.post("/api/clusters/import", json={"context": "prod-eks"})
    assert response.status_code == 422
    body = response.json()
    # Not rbac_denied and not unsupported: no cluster was involved, and this is
    # not a fact about what an API server serves.
    assert body["error"] == "invalid"
    assert "credential plugin" in body["message"]


def test_import_of_an_unknown_context_is_a_404(client, kubeconfig_file):
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    response = client.post("/api/clusters/import", json={"context": "no-such-context"})
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_import_never_overwrites_an_existing_registration(client, kubeconfig_file, on_the_host):
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))
    assert client.post("/api/clusters/import", json={"context": "kind-dev"}).status_code == 201

    again = client.post("/api/clusters/import", json={"context": "kind-dev"})
    assert again.status_code == 409
    assert again.json()["error"] == "conflict"
    assert "de-register" in again.json()["hint"].lower()


# --------------------------------------------------------------------------- #
# Startup adoption
# --------------------------------------------------------------------------- #

def test_adoption_registers_the_one_local_cluster(db_engine, kubeconfig_file, on_the_host):
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    assert adoption.adopt_local_cluster() == "kind-dev"

    session = database.SessionLocal()
    try:
        row = session.query(Cluster).one()
        assert row.origin == "autodiscovered"
        assert row.api_server == "https://127.0.0.1:6443"
    finally:
        session.close()


def test_adoption_never_touches_a_registry_that_is_not_empty(
    db_engine, registered_cluster, kubeconfig_file, on_the_host
):
    """The important refusal. A curated fleet is never added to."""
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    assert adoption.adopt_local_cluster() is None

    session = database.SessionLocal()
    try:
        assert session.query(Cluster).count() == 1
    finally:
        session.close()


def test_adoption_refuses_to_pick_between_two_local_clusters(
    db_engine, kubeconfig_file, on_the_host
):
    """Two is a choice, and making it at boot makes it where nobody can see it."""
    kubeconfig_file(_kubeconfig(contexts=MIXED_CONTEXTS))

    assert adoption.adopt_local_cluster() is None

    session = database.SessionLocal()
    try:
        assert session.query(Cluster).count() == 0
    finally:
        session.close()


def test_adoption_registers_nothing_remote(db_engine, kubeconfig_file, on_the_host):
    kubeconfig_file(_kubeconfig(contexts=f"""
clusters:
  - name: prod
    cluster: {{server: https://api.prod.example.com:6443}}
contexts:
  - name: prod
    context: {{cluster: prod, user: prod}}
users:
  - name: prod
    user: {{token: a-real-token}}
""", current="prod"))

    assert adoption.adopt_local_cluster() is None


def test_adoption_is_off_when_the_switch_is_off(
    db_engine, kubeconfig_file, on_the_host, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "auto_discover_local_cluster", False)
    kubeconfig_file(_kubeconfig(contexts=KIND_CONTEXT))

    assert adoption.adopt_local_cluster() is None


def test_adoption_never_raises(db_engine, monkeypatch):
    """A convenience that could fail a boot is not one."""
    def explode():
        raise RuntimeError("the kubeconfig reader fell over")

    monkeypatch.setattr(kubeconfig, "discover", explode)
    assert adoption.adopt_local_cluster() is None


# --------------------------------------------------------------------------- #
# The transport a client certificate produces
# --------------------------------------------------------------------------- #

def test_a_client_certificate_reaches_the_transport_and_is_cleaned_up():
    """Both halves land on the Configuration, and both files go away again.

    The kubernetes client reads a certificate and key from *paths*, so building
    this transport spills a decrypted private key to disk. The cleanup is not a
    tidiness concern: without it, every cache rebuild — which happens whenever a
    cluster row is edited — leaves another copy of the key in the container's
    writable layer.
    """
    from app.k8s.client import ClusterClients, build_configuration

    configuration, temp_paths = build_configuration(
        api_server="https://127.0.0.1:6443/",
        authentication_type="client_certificate",
        token=None,
        ca_certificate=FIXTURE_CA,
        skip_tls_verify=False,
        client_certificate=FIXTURE_CERT,
        client_key=FIXTURE_KEY,
    )

    assert configuration.host == "https://127.0.0.1:6443"
    assert configuration.verify_ssl is True
    assert FIXTURE_CERT in open(configuration.cert_file).read()
    assert FIXTURE_KEY in open(configuration.key_file).read()
    assert FIXTURE_CA in open(configuration.ssl_ca_cert).read()
    # The key, and only the key, is owner-read-only.
    assert stat.S_IMODE(os.stat(configuration.key_file).st_mode) == 0o600
    assert set(temp_paths) == {
        configuration.cert_file, configuration.key_file, configuration.ssl_ca_cert,
    }
    # No bearer token was invented from nowhere.
    assert not configuration.api_key

    ClusterClients(
        cluster_id=1, platform="kubernetes", cache_key="t",
        api_client=_NullApiClient(), core_v1=None, apps_v1=None, batch_v1=None,
        networking_v1=None, rbac_v1=None, storage_v1=None, authorization_v1=None,
        version_api=None, _temp_paths=temp_paths,
    ).close()

    assert [path for path in temp_paths if os.path.exists(path)] == []


class _NullApiClient:
    """Enough of an ``ApiClient`` for ``close()``. See the fake in conftest.

    Spelled here rather than reached for from conftest because this test is
    about the temp files, and a fake that raises on an unstubbed call would make
    the point of the test harder to see rather than easier.
    """

    def close(self) -> None:
        pass


def test_a_client_certificate_without_its_key_is_refused_at_the_form(client):
    """Half a pair is the failure that looks like success until the handshake."""
    response = client.post("/api/clusters", json={
        "name": "half-configured",
        "api_server": "https://127.0.0.1:6443",
        "authentication_type": "client_certificate",
        "client_certificate": FIXTURE_CERT,
    })

    assert response.status_code == 422
    assert response.json()["error"] == "invalid"
    assert response.json()["context"]["field"] == "client_key"


def test_the_forms_bearer_token_option_is_actually_accepted(client):
    """A console rejecting its own dropdown.

    `Clusters.jsx` has offered "Bearer token" — `bearer_token` — for as long as
    the backend's supported set has not contained it, so choosing it produced
    `422 Unsupported authentication type`. Nothing failed: the form was right,
    the backend was right about its own list, and the two had never been
    compared.
    """
    response = client.post("/api/clusters", json={
        "name": "bearer",
        "api_server": "https://api.example:6443",
        "authentication_type": "bearer_token",
        "token": "a-token",
    })

    assert response.status_code == 201
    assert response.json()["authentication_type"] == "bearer_token"


def test_a_registration_with_no_credential_at_all_is_refused(client):
    """`token` became optional for §34; anonymous did not become a registration."""
    response = client.post("/api/clusters", json={
        "name": "anonymous",
        "api_server": "https://api.example:6443",
        "authentication_type": "service_account_token",
    })

    assert response.status_code == 422
    assert response.json()["context"]["field"] == "token"
