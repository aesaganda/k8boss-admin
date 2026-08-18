"""
Pod logs (§7) — the snapshot endpoint and the follow stream.

Three properties are asserted here, and each of them is a failure that would
otherwise be invisible from the outside:

* **A multi-container pod read without a container name is refused, with the
  list.** The tempting implementation picks ``spec.containers[0]`` and returns a
  200. The response is well-formed, the logs are real, and they are about the
  wrong process — an operator reads an idle sidecar's output and concludes the
  service is not receiving traffic. There is no way to tell from the response
  that this happened, which is why it is tested rather than reviewed.

* **The stream always terminates with exactly one terminal frame.** Not "at least
  one": two would tell a viewer the stream failed and then finished normally.
  Not "at most one": zero is indistinguishable from a dropped connection, which
  is the distinction §7 exists to preserve.

* **A denial arrives as an ``error`` frame, not as a closed socket.** A close code
  carries no sentence, and "you lack `get pods/log` in prod" becomes "the log
  viewer is broken" in the bug report that follows.

The fake stream also lets the teardown be asserted directly: after the socket
closes, the urllib3 response must have been closed. That is what makes the
reader thread exit, and a leak there is one thread per closed browser tab —
invisible until the pool is exhausted, and by then it looks like a hang.
"""

from __future__ import annotations

import json
import threading

import pytest
from kubernetes.client.rest import ApiException
from urllib3.exceptions import ProtocolError

from tests.conftest import obj


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _pod(containers=("app",), init=(), annotations=None):
    return obj(
        metadata=obj(
            name="checkout", namespace="prod",
            annotations=annotations if annotations is not None else {},
        ),
        spec=obj(
            containers=[obj(name=name) for name in containers],
            init_containers=[obj(name=name) for name in init],
        ),
    )


def _allow(fake) -> None:
    """Stub the access review to allow. Preflight runs before every log read."""
    fake.authorization_v1.returns(
        "create_self_subject_access_review", obj(status=obj(allowed=True))
    )


def _deny(fake) -> None:
    """Stub a clean denial — allowed false with no evaluation error (§9)."""
    fake.authorization_v1.returns(
        "create_self_subject_access_review",
        obj(status=obj(allowed=False, denied=False, reason=None, evaluation_error=None)),
    )


class FakeLogStream:
    """Stand-in for the urllib3 response ``read_namespaced_pod_log`` returns.

    ``raises`` makes the stream fail *after* delivering some chunks, which is the
    interesting case: the lines already sent must survive and the failure must
    still produce a terminal frame.
    """

    def __init__(self, chunks, raises: BaseException | None = None):
        self._chunks = list(chunks)
        self._raises = raises
        self.closed = threading.Event()

    def stream(self, amt=None, decode_content=True):
        for chunk in self._chunks:
            yield chunk
        if self._raises is not None:
            raise self._raises

    def close(self):
        self.closed.set()


def _drain(ws, limit: int = 50):
    """Every server frame up to the close, plus whether the socket was closed.

    Reads raw ASGI messages rather than :meth:`receive_json` so that the close
    itself is data instead of an exception — "what came after the terminal frame"
    is precisely what these tests are about.
    """
    frames = []
    for _ in range(limit):
        message = ws.receive()
        if message["type"] == "websocket.close":
            return frames, True
        assert message["type"] == "websocket.send", message
        frames.append(json.loads(message["text"]))
    raise AssertionError(f"The stream produced {limit} frames without terminating.")


def _terminal(frames):
    return [f for f in frames if f["type"] in ("end", "error")]


# --------------------------------------------------------------------------- #
# GET /api/pods/{namespace}/{name}/logs
# --------------------------------------------------------------------------- #

def test_multi_container_pod_is_refused_with_the_container_list(client, fake_k8s):
    """422 naming every container, and no log read attempted."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns(
        "read_namespaced_pod",
        _pod(("app", "envoy"), init=("migrate",),
             annotations={"kubectl.kubernetes.io/default-container": "app"}),
    )

    response = client.get("/api/pods/prod/checkout/logs")

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert body["context"]["containers"] == ["app", "envoy"]
    assert body["context"]["initContainers"] == ["migrate"]
    # The declared default is reported so the UI can preselect it, and is still
    # not acted on: a suggestion the operator confirms is not the same thing as a
    # choice made for them silently.
    assert body["context"]["defaultContainer"] == "app"
    assert "app" in body["detail"] and "envoy" in body["detail"]
    # Nothing was read. A 422 that had already streamed the wrong container's
    # logs into a discarded response would be the same defect with a better
    # status code.
    assert fake_k8s.core_v1.called("read_namespaced_pod_log") == []


def test_single_container_pod_needs_no_container_name(client, fake_k8s):
    """One container is unambiguous, so the API server's own default is used."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",), init=("migrate",)))
    fake_k8s.core_v1.returns("read_namespaced_pod_log", "line one\nline two\n")

    response = client.get("/api/pods/prod/checkout/logs?tailLines=5")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "line one\nline two\n"
    _, kwargs = fake_k8s.core_v1.called("read_namespaced_pod_log")[0]
    assert kwargs["container"] is None
    assert kwargs["tail_lines"] == 5


def test_logs_preflight_names_the_log_subresource(client, fake_k8s):
    """RBAC names ``pods/log`` separately; preflighting ``pods`` would pass and then fail."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake_k8s.core_v1.returns("read_namespaced_pod_log", "")

    client.get("/api/pods/prod/checkout/logs")

    (args, _), = fake_k8s.authorization_v1.called("create_self_subject_access_review")
    attributes = args[0].spec.resource_attributes
    assert (attributes.verb, attributes.group, attributes.resource,
            attributes.subresource) == ("get", "", "pods", "log")
    assert attributes.namespace == "prod"


def test_unknown_container_is_refused_before_the_cluster_is_asked(client, fake_k8s):
    """A typo'd container name is answered with the real ones, not with the API server's 400."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app", "envoy")))

    response = client.get("/api/pods/prod/checkout/logs?container=nginx")

    assert response.status_code == 422
    assert response.json()["context"]["containers"] == ["app", "envoy"]
    assert fake_k8s.core_v1.called("read_namespaced_pod_log") == []


def test_a_forbidden_log_read_is_403_naming_the_grant(client, fake_k8s):
    """The snapshot endpoint's denial is §1.3, with the hint preflight computed."""
    _deny(fake_k8s)

    response = client.get("/api/pods/prod/checkout/logs?container=app")

    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "rbac_denied"
    assert "pods/log" in body["hint"]
    assert body["context"]["subresource"] == "log"


def test_previous_log_on_a_container_that_never_restarted_says_so(client, fake_k8s):
    """The API server's 400 here means "it has not crashed", not "malformed request"."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake_k8s.core_v1.raises(
        "read_namespaced_pod_log",
        ApiException(status=400, reason="Bad Request"),
    )

    response = client.get("/api/pods/prod/checkout/logs?previous=true")

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid"
    assert "has not restarted" in body["hint"]


# --------------------------------------------------------------------------- #
# WS /api/ws/pods/{namespace}/{name}/logs
# --------------------------------------------------------------------------- #

def test_stream_ends_with_exactly_one_end_frame(client, fake_k8s):
    """Lines, then one ``end``, then the close — and the response is released."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app", "envoy")))
    stream = FakeLogStream([
        b"2026-08-18T09:14:00Z first\n2026-08-18T09:14:01Z sec",
        b"ond\n",
    ])
    fake_k8s.core_v1.returns("read_namespaced_pod_log", stream)

    with client.websocket_connect(
        "/api/ws/pods/prod/checkout/logs?container=envoy&tailLines=10"
    ) as ws:
        frames, closed = _drain(ws)

    assert [f["type"] for f in frames] == ["log", "log", "end"]
    # The timestamp on a log frame is the API server's own, split off the line —
    # not this process's clock, which would be minutes wrong for the backlog
    # tailLines replays before the live tail starts.
    assert frames[0] == {"type": "log", "line": "first", "ts": "2026-08-18T09:14:00Z"}
    # A line split across two chunks is reassembled, not delivered twice.
    assert frames[1]["line"] == "second"
    assert frames[2]["reason"] == "stream_closed"
    assert len(_terminal(frames)) == 1
    assert closed
    # Closed by the endpoint's teardown; without this the reader thread stays
    # parked on the socket until the 600s watch deadline, once per closed tab.
    assert stream.closed.wait(timeout=5)

    _, kwargs = fake_k8s.core_v1.called("read_namespaced_pod_log")[0]
    assert kwargs["follow"] is True
    assert kwargs["_preload_content"] is False
    assert kwargs["timestamps"] is True
    assert kwargs["container"] == "envoy"


def test_a_line_without_a_timestamp_gets_a_null_ts(client, fake_k8s):
    """``ts: null`` is the honest answer for an unparseable prefix; inventing one is not."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake_k8s.core_v1.returns("read_namespaced_pod_log", FakeLogStream([b"no timestamp here\n"]))

    with client.websocket_connect("/api/ws/pods/prod/checkout/logs") as ws:
        frames, _ = _drain(ws)

    assert frames[0] == {"type": "log", "line": "no timestamp here", "ts": None}


def test_forbidden_surfaces_as_an_error_frame_not_a_silent_close(client, fake_k8s):
    """One ``error`` frame with ``reason: forbidden`` and the grant that fixes it."""
    _deny(fake_k8s)

    with client.websocket_connect(
        "/api/ws/pods/prod/checkout/logs?container=app"
    ) as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "forbidden"
    assert "pods/log" in frames[0]["hint"]
    assert len(_terminal(frames)) == 1
    assert closed
    # The pod was never read and the stream never opened: preflight refused
    # before either.
    assert fake_k8s.core_v1.calls == []


def test_an_ambiguous_container_is_invalid_not_unreachable(client, fake_k8s):
    """The frame reason distinguishes "your request was wrong" from "the cluster is down"."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app", "envoy")))

    with client.websocket_connect("/api/ws/pods/prod/checkout/logs") as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    # §1.2's vocabulary would map `invalid` onto `unreachable`, which would send
    # the operator to check a network that is provably fine.
    assert frames[0]["reason"] == "invalid"
    assert "envoy" in frames[0]["detail"]
    assert closed


def test_a_stream_that_breaks_midway_keeps_its_lines_and_still_terminates(client, fake_k8s):
    """Delivered lines survive; the failure is one terminal ``error`` frame."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    stream = FakeLogStream(
        [b"2026-08-18T09:14:00Z before the break\n"],
        raises=ProtocolError("Connection broken: IncompleteRead"),
    )
    fake_k8s.core_v1.returns("read_namespaced_pod_log", stream)

    with client.websocket_connect("/api/ws/pods/prod/checkout/logs") as ws:
        frames, closed = _drain(ws)

    assert frames[0] == {"type": "log", "line": "before the break",
                         "ts": "2026-08-18T09:14:00Z"}
    assert len(_terminal(frames)) == 1
    assert frames[-1]["type"] == "error"
    assert frames[-1]["reason"] == "unreachable"
    assert closed


def test_a_viewer_that_disconnects_tears_the_reader_down(client, fake_k8s):
    """Closing the tab closes the response, which is what makes the thread exit."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))

    # A stream that never ends on its own: the only thing that can stop it is the
    # teardown path this test is about.
    forever = threading.Event()

    class BlockingStream(FakeLogStream):
        def stream(self, amt=None, decode_content=True):
            yield b"2026-08-18T09:14:00Z one\n"
            forever.wait(timeout=10)
            return

    stream = BlockingStream([])
    fake_k8s.core_v1.returns("read_namespaced_pod_log", stream)

    with client.websocket_connect("/api/ws/pods/prod/checkout/logs") as ws:
        first = ws.receive()
        assert json.loads(first["text"])["line"] == "one"
    forever.set()

    assert stream.closed.wait(timeout=5)


@pytest.mark.parametrize("query", ["tailLines=abc", "tailLines=0", "tailLines=99999"])
def test_a_bad_query_parameter_is_an_error_frame_not_a_handshake_failure(
    client, fake_k8s, query
):
    """Accept first, then complain in a frame the viewer can render."""
    with client.websocket_connect(f"/api/ws/pods/prod/checkout/logs?{query}") as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "invalid"
    assert closed
