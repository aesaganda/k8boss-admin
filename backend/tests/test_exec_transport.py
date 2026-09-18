"""
§15's exec channel is opened on a transport nothing else shares.

``kubernetes.stream.stream`` does not take a socket — it opens the channel by
assigning a websocket function over ``ApiClient.request`` and restoring it in a
``finally``. ``ApiClient.__call_api`` reaches the network only through that
attribute, so on a shared transport the assignment is process-wide for the
length of the handshake, and every other thread in that window is handed the
websocket function instead of its own request. It comes back as
``ApiException(status=0)``, which ``from_api_exception`` reads as
``cluster_unreachable``: the console tells an operator to check a CA
certificate that is fine, about a cluster that is up.

And the restore writes back a *captured* value rather than deleting the
attribute, so two overlapping handshakes leave the websocket function installed
permanently — the second captures the first's function as the thing to restore.
From then on every call on that cluster's cached bundle goes into it and the
console reports a cluster it can no longer read as unreachable, until the
process restarts.

Neither half can be reproduced against ``tests/conftest.py``'s fake: it has no
transport, and every exec test monkeypatches ``k8s_stream`` so the real one
never runs. What *is* testable without a cluster is the invariant that makes
both halves impossible — that the client handed to ``stream()`` is not the
cached bundle's — and that is what is asserted here, against a real
``ClusterClients`` built from plaintext parameters, which opens no sockets.

**The durable half is deliberately not reproduced as its own test.** Nesting one
stream inside another does not do it: strict last-in-first-out restores
correctly, and the corruption needs the second handshake to *finish* after the
first, which takes two threads and a pair of events to force. A test shaped that
way would be asserting what ``kubernetes.stream`` does with a shared client
rather than what this console hands it — and it would pass with or without the
fix on the day somebody changes the library. Both tests below fail if
``stream_core_v1`` goes back to returning the bundle's own client, which is the
only way either half can happen here.
"""

from __future__ import annotations

import pytest
from kubernetes.stream import stream as k8s_stream
from kubernetes.stream import ws_client

from app.k8s.auth import AUTH_SERVICE_ACCOUNT_TOKEN
from app.k8s.client import manager


@pytest.fixture
def bundle():
    """A real client bundle, built without touching the network.

    ``build_transient`` is the one builder that needs no ``Cluster`` row: no CA
    certificate means no temp file, and ``ApiClient.__init__`` opens no sockets,
    so this is a genuine transport with nothing behind it.
    """
    built = manager.build_transient(
        api_server="https://cluster.invalid",
        authentication_type=AUTH_SERVICE_ACCOUNT_TOKEN,
        token="unused-in-this-test",
        ca_certificate=None,
        skip_tls_verify=True,
    )
    yield built
    built.close()


class _FakeSocket:
    """Stands in for the websocket ``create_websocket`` would return."""

    connected = True

    def settimeout(self, _timeout):
        """``WSClient.__init__`` sets one on the socket it was handed."""


def _open_a_stream(core_v1, monkeypatch, *, on_handshake=None):
    """Run the real ``kubernetes.stream`` with only the socket stubbed out.

    Everything that matters here happens in ``stream()`` itself — the assignment
    over ``ApiClient.request`` and the restore — so the only thing worth faking
    is the connection at the bottom. ``on_handshake`` runs at the moment the
    swap is in force, which is the moment under test.
    """
    def create_websocket(_configuration, _url, headers=None, **_kwargs):
        if on_handshake is not None:
            on_handshake()
        return _FakeSocket()

    monkeypatch.setattr(ws_client, "create_websocket", create_websocket)
    return k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        "checkout-7d9f8b6c4-hk2xv",
        "prod",
        command=["/bin/sh"],
        stderr=True,
        stdin=True,
        stdout=True,
        tty=True,
        _preload_content=False,
    )


def test_the_shared_transport_is_not_swapped_while_a_stream_is_opening(
    bundle, monkeypatch
):
    """The assertion this module exists for.

    Sampled *during* the handshake, not after: ``stream()`` restores in a
    ``finally``, so a check that only looked at the end state would pass against
    the very bug it is meant to catch.
    """
    before = bundle.api_client.request
    seen: dict[str, object] = {}

    _open_a_stream(
        bundle.stream_core_v1(),
        monkeypatch,
        on_handshake=lambda: seen.update(during=bundle.api_client.request),
    )

    assert seen["during"] == before
    assert bundle.api_client.request == before


def test_each_stream_gets_a_transport_of_its_own(bundle):
    """Not cached, and not the bundle's.

    A cached one would put two concurrent exec sessions back on a single
    transport, which is the whole defect with extra steps.
    """
    first = bundle.stream_core_v1()
    second = bundle.stream_core_v1()

    assert first.api_client is not bundle.api_client
    assert second.api_client is not first.api_client
    assert first.api_client.configuration is bundle.api_client.configuration
