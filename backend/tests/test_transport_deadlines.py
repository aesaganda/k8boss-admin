"""
The deadline every cluster call is supposed to carry, and the reason it did not.

`_cluster_api_client`'s docstring states the failure it exists to prevent: the
kubernetes client is constructed with no request timeout, so a black-holed
connection — an API server behind a firewall that drops rather than resets, a
NAT that lost the conntrack entry — blocks the socket forever. Sync route
handlers run in the shared threadpool, so those threads never come back and the
console stops serving while its own liveness probe, which touches no cluster,
keeps answering healthy.

The deadline was injected with `if "_request_timeout" not in kwargs`, described
in the code as setdefault semantics: *a caller that already passed a deadline
wins*. It reads correctly and it never fired. `RESTClientObject.GET` — and
`POST`, `PUT`, `PATCH`, `DELETE`, every one of them — passes `_request_timeout`
**explicitly**, as `None`, on every call:

    def GET(self, url, headers=None, query_params=None, _preload_content=True,
            _request_timeout=None):
        return self.request("GET", url, ..., _request_timeout=_request_timeout, ...)

So the key was always present, the default was never applied, and every typed
call this console made ran with no deadline at all. It was found by registering
a cluster at an unroutable address and running the connection test: **135
seconds** on one connect, against a configured `K8S_CONNECT_TIMEOUT_SECONDS` of
5 — the kernel's SYN retry budget, which is what "no deadline" actually means in
practice.

Nothing failed while it was wrong. The timeouts were configured, documented and
passed through `docker-compose.yml`; the code that read them ran on every
request; and the only observable difference was how long a broken cluster took
to give up. That is why the tests below assert on the value that reaches the
transport rather than on the code path that computes it.
"""

from __future__ import annotations

import pytest
from kubernetes.client.rest import RESTClientObject

from app.config import settings
from app.k8s.client import _cluster_api_client


@pytest.fixture
def spy(monkeypatch):
    """Capture what reaches the real ``RESTClientObject.request``.

    Patched on the class, before the client is built: `_cluster_api_client`
    captures ``api_client.rest_client.request`` at wrap time, so the spy has to
    be in place first to end up as the ``inner`` its wrapper calls.
    """
    calls: list[dict] = []

    def record(self, *args, **kwargs):
        calls.append(kwargs)
        return "unused"

    monkeypatch.setattr(RESTClientObject, "request", record)
    return calls


def test_a_call_that_names_no_deadline_gets_one(spy):
    """The bug, stated as the property it broke.

    `GET` passes `_request_timeout=None`, which is what "I did not ask for a
    deadline" looks like on the wire — not an absent key.
    """
    api = _cluster_api_client()

    api.rest_client.GET("https://api.example:6443/version")

    assert spy[0]["_request_timeout"] == (
        settings.k8s_connect_timeout_seconds,
        settings.k8s_read_timeout_seconds,
    )


def test_a_caller_that_names_a_deadline_still_wins(spy):
    """The behaviour the original condition was reaching for, preserved."""
    api = _cluster_api_client()

    api.rest_client.GET("https://api.example:6443/version", _request_timeout=(1, 2))

    assert spy[0]["_request_timeout"] == (1, 2)


def test_a_stream_gets_the_long_deadline_not_the_short_one(spy):
    """A log follow is *supposed* to sit idle.

    The 30-second read deadline on a quiet pod's log stream tears down every
    viewer and reports it as an error, so streams get their own. This is the
    assertion that keeps the fix above from applying one deadline to everything.
    """
    api = _cluster_api_client()

    api.rest_client.GET(
        "https://api.example:6443/api/v1/namespaces/shop/pods/web/log",
        query_params=[("follow", "true")],
        _preload_content=False,
    )

    assert spy[0]["_request_timeout"] == (
        settings.k8s_connect_timeout_seconds,
        settings.k8s_watch_read_timeout_seconds,
    )


@pytest.mark.parametrize("verb", ["POST", "PUT", "PATCH", "DELETE"])
def test_every_write_verb_carries_a_deadline_too(spy, verb):
    """Not only reads. A write to a black-holed API server pins the same thread,
    and it is the one an operator is watching a spinner for."""
    api = _cluster_api_client()

    getattr(api.rest_client, verb)("https://api.example:6443/apis/apps/v1/x")

    assert spy[0]["_request_timeout"] == (
        settings.k8s_connect_timeout_seconds,
        settings.k8s_read_timeout_seconds,
    )
