"""
Pod exec (§7) — the privileged stream.

Exec writes nothing through the mutation funnel, so none of the funnel's tests
cover it, and everything it borrows from the write path has to be asserted here:

* **Refused when the console is read-only, before the cluster is touched.** A
  deployment running with ``ADMIN_ALLOW_MUTATIONS=false`` is one the operator
  believes cannot change their cluster. A shell would make that belief false, so
  the gate is asserted — including that the access review is never issued, since
  a refusal that first asks the cluster anything is a refusal that can fail for
  the wrong reason.

* **Audited on open and on close, as two records.** One record at the end would
  vanish with the process, losing exactly the sessions worth knowing about. The
  close record must also say what the command returned, and must say *unknown*
  when it does not know — an audit row claiming exit 0 for a session whose status
  was never read is a fabricated success, and exit 0 on a migration is the
  sentence an incident review takes at face value.

* **A denial is a frame, not a close code.** Same reasoning as the log stream.
"""

from __future__ import annotations

import json
import time

from kubernetes.stream.ws_client import ERROR_CHANNEL, RESIZE_CHANNEL

import app.api.exec_ws as exec_ws
from app.k8s import impersonation
from tests.conftest import obj


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #

def _pod(containers=("app",), init=()):
    return obj(
        metadata=obj(name="checkout", namespace="prod", annotations={}),
        spec=obj(
            containers=[obj(name=name) for name in containers],
            init_containers=[obj(name=name) for name in init],
        ),
    )


def _allow(fake) -> None:
    fake.authorization_v1.returns(
        "create_self_subject_access_review", obj(status=obj(allowed=True))
    )


def _deny(fake) -> None:
    fake.authorization_v1.returns(
        "create_self_subject_access_review",
        obj(status=obj(allowed=False, denied=False, reason=None, evaluation_error=None)),
    )


class FakeExecClient:
    """Stand-in for ``kubernetes.stream``'s ``WSClient``.

    Driven by a script of ``(kind, value)`` steps consumed one per ``update()``,
    which is how the real client behaves — one frame per poll. ``write_stdin``
    appends an echo and an exit to the script, so a test can drive a whole
    session from the client side without sleeping on a real socket.
    """

    def __init__(self, script=()):
        self._script = list(script)
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        self._open = True
        self.channels: dict[int, str] = {}
        self.stdin: list[str] = []
        self.control: list[tuple[int, str]] = []
        self.closed = False

    # -- read side (called on the pump thread) ----------------------------
    def is_open(self) -> bool:
        return self._open

    def update(self, timeout=0) -> None:
        if not self._script:
            # A real poll blocks for `timeout` on a quiet session; sleeping keeps
            # the pump from spinning a core through the whole test.
            time.sleep(min(timeout or 0, 0.01))
            return
        kind, value = self._script.pop(0)
        if kind == "stdout":
            self._stdout.append(value)
        elif kind == "stderr":
            self._stderr.append(value)
        elif kind == "exit":
            self._open = False
            self.channels[ERROR_CHANNEL] = value

    def read_stdout(self, timeout=None) -> str:
        return self._stdout.pop(0) if self._stdout else ""

    def read_stderr(self, timeout=None) -> str:
        return self._stderr.pop(0) if self._stderr else ""

    def read_channel(self, channel, timeout=0) -> str:
        return self.channels.pop(channel, "")

    # -- write side (called from the session coroutine) -------------------
    def write_stdin(self, data: str) -> None:
        self.stdin.append(data)
        self._script.append(("stdout", f"$ {data}"))
        self._script.append(("exit", json.dumps({"status": "Success"})))

    def write_channel(self, channel: int, data: str) -> None:
        self.control.append((channel, data))

    def close(self, **kwargs) -> None:
        self.closed = True
        self._open = False


def _drain(ws, limit: int = 50):
    """Every server frame up to the close, plus whether the socket was closed."""
    frames = []
    for _ in range(limit):
        message = ws.receive()
        if message["type"] == "websocket.close":
            return frames, True
        assert message["type"] == "websocket.send", message
        frames.append(json.loads(message["text"]))
    raise AssertionError(f"The session produced {limit} frames without terminating.")


def _audit_rows():
    from app import database
    from app.models import AuditRecord

    session = database.SessionLocal()
    try:
        return list(session.query(AuditRecord).order_by(AuditRecord.id).all())
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_exec_is_refused_when_mutations_are_disabled(client, fake_k8s, registered_cluster):
    """403-equivalent frame, no cluster call, and the attempt is in the trail."""
    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?cluster_id={registered_cluster.id}"
    ) as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "mutations_disabled"
    assert "ADMIN_ALLOW_MUTATIONS" in frames[0]["hint"]
    assert closed
    # Refused before the cluster was asked anything — not even the access review.
    assert fake_k8s.authorization_v1.calls == []
    assert fake_k8s.core_v1.calls == []

    rows = _audit_rows()
    assert len(rows) == 1
    assert (rows[0].verb, rows[0].outcome, rows[0].dry_run) == ("create", "denied", False)
    assert rows[0].target["subresource"] == "exec"
    assert rows[0].target["name"] == "checkout"
    assert rows[0].cluster_name == "prod-eu"
    assert "mutations_disabled" in rows[0].error


def _impersonating(monkeypatch, fake_k8s):
    """Make ``get_clients`` pin an active decision, the way the real one does.

    ``app.k8s.client.get_clients`` is where ADR-0007's ``decide`` runs and where
    ``set_current`` is called, and the fake bundle replaces it — so a test about
    an impersonating request has to put the decision back. Pinned inside the
    replacement rather than in the test body on purpose: ``asyncio.to_thread``
    copies the context, so a decision set out here would be invisible to the
    code under test, which is the whole reason that check lives where it does.
    """
    decision = impersonation.Decision(
        impersonation=impersonation.Impersonation(
            username="ada@example.test", groups=("platform-admins",),
        ),
        subject="ada@example.test",
    )

    def get_clients():
        impersonation.set_current(decision)
        return fake_k8s

    monkeypatch.setattr(exec_ws, "get_clients", get_clients)


def test_exec_is_refused_when_the_cluster_acts_as_the_operator(
    client, fake_k8s, allow_mutations, monkeypatch, registered_cluster,
):
    """ADR-0007 condition 4, on the one path that cannot carry the headers.

    ``kubernetes.stream`` opens the channel through ``create_websocket``, which
    forwards only ``authorization`` and ``sec-websocket-protocol``, so
    ``Impersonate-User`` never reaches the API server. Running the shell anyway
    would hand somebody a root prompt under an identity the cluster never saw,
    after checking a permission the console then does not use.
    """
    _allow(fake_k8s)
    _impersonating(monkeypatch, fake_k8s)

    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?cluster_id={registered_cluster.id}"
    ) as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "impersonation_unavailable"
    assert closed

    rows = _audit_rows()
    assert len(rows) == 1
    assert (rows[0].verb, rows[0].outcome) == ("create", "denied")
    assert rows[0].target["subresource"] == "exec"
    assert "impersonation_unavailable" in rows[0].error


def test_the_impersonation_refusal_comes_before_the_access_review(
    client, fake_k8s, allow_mutations, monkeypatch, registered_cluster,
):
    """Whether we can act as the operator is not a question about permissions.

    Asked in the other order, the console reviews a permission it has already
    decided it cannot use — and an operator who *lacks* exec would be told they
    lack exec, sending them to widen a ClusterRole that would not have helped.
    """
    _allow(fake_k8s)
    _impersonating(monkeypatch, fake_k8s)

    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?cluster_id={registered_cluster.id}"
    ) as ws:
        _drain(ws)

    assert fake_k8s.authorization_v1.calls == []
    assert fake_k8s.core_v1.calls == []


def test_a_cluster_that_does_not_impersonate_still_opens_a_shell(
    client, fake_k8s, allow_mutations, monkeypatch, registered_cluster,
):
    """The refusal is about impersonation, not about exec.

    Worth asserting because the cheap way to write the guard — refusing whenever
    a decision exists at all — would close exec on every cluster, and every other
    test here would still pass by way of the fake never pinning one.
    """
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    monkeypatch.setattr(exec_ws, "k8s_stream",
                        lambda *a, **k: FakeExecClient([("exit", '{"status":"Success"}')]))

    def get_clients():
        impersonation.set_current(
            impersonation.Decision(impersonation=None, subject="system:serviceaccount")
        )
        return fake_k8s

    monkeypatch.setattr(exec_ws, "get_clients", get_clients)

    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?cluster_id={registered_cluster.id}"
    ) as ws:
        frames, _ = _drain(ws)

    assert [f["type"] for f in frames] == ["end"]
    assert [row.outcome for row in _audit_rows()] == ["applied", "applied"]


def test_the_websocket_scope_carries_the_cluster_context(client, fake_k8s,
                                                         registered_cluster):
    """``?cluster_id=`` is read on websocket scopes, not only on HTTP ones.

    Asserted with an id that is *not* registered, because the fallback
    (lowest-id registered cluster) would otherwise produce the same row and hide
    a middleware that ignored the parameter. An audit record attributing a write
    to the wrong cluster is the misattribution :mod:`app.k8s.context` exists to
    prevent, and a websocket that quietly fell back would produce exactly that.
    """
    other = registered_cluster.id + 1000

    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?cluster_id={other}"
    ) as ws:
        _drain(ws)

    row = _audit_rows()[0]
    assert row.cluster_id == other
    assert row.cluster_name is None


def test_exec_denied_by_rbac_is_audited_and_named(client, fake_k8s, allow_mutations):
    """A clean denial is `denied` in the trail and `forbidden` on the wire."""
    _deny(fake_k8s)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "forbidden"
    assert "pods/exec" in frames[0]["hint"]
    assert closed

    rows = _audit_rows()
    assert len(rows) == 1
    assert rows[0].outcome == "denied"
    assert rows[0].target["subresource"] == "exec"
    # The shell never opened, so there is no close record to pair with it.
    assert "preflight" in rows[0].detail


def test_exec_preflight_names_the_exec_subresource(client, fake_k8s, allow_mutations,
                                                   monkeypatch):
    """``create core/pods/exec`` — RBAC names it separately from the pod."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    monkeypatch.setattr(exec_ws, "k8s_stream",
                        lambda *a, **k: FakeExecClient([("exit", '{"status":"Success"}')]))

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        _drain(ws)

    (args, _), = fake_k8s.authorization_v1.called("create_self_subject_access_review")
    attributes = args[0].spec.resource_attributes
    assert (attributes.verb, attributes.group, attributes.resource,
            attributes.subresource) == ("create", "", "pods", "exec")
    assert attributes.namespace == "prod"


def test_exec_open_failure_unfolds_a_handshake_status_into_the_real_code(
    client, fake_k8s, allow_mutations, monkeypatch,
):
    """A 403 the API server answered in full is `rbac_denied`, not `unreachable`.

    ``kubernetes.stream.ws_client.websocket_call`` catches every exception the
    websocket upgrade can raise — including a clean, fully-answered 403 from the
    API server — and re-raises ``ApiException(status=0, reason=str(e))``. Left
    unhandled, that status-0 reaches ``from_api_exception`` and is reported as
    `cluster_unreachable`: "no HTTP response was received", sending the operator
    to check TLS certificates for a permission they lack.
    """
    from kubernetes.client.rest import ApiException

    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))

    body = (
        '{"kind":"Status","apiVersion":"v1","status":"Failure",'
        '"message":"pods \\"checkout\\" is forbidden: User '
        '\\"system:serviceaccount:k8boss-admin:k8boss-admin\\" cannot get '
        'resource \\"pods/exec\\" in API group \\"\\" in the namespace '
        '\\"prod\\"","reason":"Forbidden","code":403}'
    )
    reason = f"Handshake status 403 Forbidden -+-+- {{}} -+-+- {body}"

    def raise_folded(*_a, **_k):
        raise ApiException(status=0, reason=reason)

    monkeypatch.setattr(exec_ws, "k8s_stream", raise_folded)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "forbidden"
    assert "checkout" in frames[0]["detail"]
    assert closed

    rows = _audit_rows()
    assert rows[-1].outcome == "failed"
    assert "rbac_denied" in rows[-1].error


# --------------------------------------------------------------------------- #
# The session
# --------------------------------------------------------------------------- #

def test_exec_is_audited_on_open_and_on_close(client, fake_k8s, allow_mutations,
                                              monkeypatch, registered_cluster):
    """Two records: one when the shell opens, one saying how it ended."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake = FakeExecClient()
    monkeypatch.setattr(exec_ws, "k8s_stream", lambda *a, **k: fake)

    with client.websocket_connect(
        f"/api/ws/pods/prod/checkout/exec?command=/bin/bash&cluster_id={registered_cluster.id}"
    ) as ws:
        ws.send_json({"type": "resize", "cols": 120, "rows": 40})
        ws.send_json({"type": "stdin", "data": "whoami\n"})
        frames, closed = _drain(ws)

    assert [f["type"] for f in frames] == ["stdout", "end"]
    assert frames[0]["data"] == "$ whoami\n"
    assert frames[1]["code"] == 0
    assert closed
    assert fake.stdin == ["whoami\n"]
    assert fake.closed

    channel, payload = fake.control[0]
    assert channel == RESIZE_CHANNEL
    assert json.loads(payload) == {"Width": 120, "Height": 40}

    rows = _audit_rows()
    assert len(rows) == 2
    assert [r.outcome for r in rows] == ["applied", "applied"]
    assert all(r.verb == "create" for r in rows)
    assert all(r.target["subresource"] == "exec" for r in rows)
    assert all(r.cluster_name == "prod-eu" for r in rows)
    assert "opened" in rows[0].detail and "/bin/bash" in rows[0].detail
    assert "closed" in rows[1].detail
    assert "exit 0" in rows[1].detail
    assert "7 B stdin" in rows[1].detail


def test_an_unreported_exit_status_is_null_not_zero(client, fake_k8s, allow_mutations,
                                                    monkeypatch):
    """We never fabricate a successful exit for a session we did not see finish."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake = FakeExecClient([("stdout", "partial output\n"), ("exit", "")])
    monkeypatch.setattr(exec_ws, "k8s_stream", lambda *a, **k: fake)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        frames, closed = _drain(ws)

    assert [f["type"] for f in frames] == ["stdout", "end"]
    assert frames[1]["code"] is None
    assert "exit status" in frames[1]["detail"]
    assert closed

    rows = _audit_rows()
    assert "exit unknown" in rows[1].detail


def test_a_nonzero_exit_is_reported_with_its_code(client, fake_k8s, allow_mutations,
                                                  monkeypatch):
    """The ExitCode cause carries the number; it is not flattened to "failed"."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    status = json.dumps({
        "status": "Failure",
        "message": "command terminated with non-zero exit code",
        "reason": "NonZeroExitCode",
        "details": {"causes": [{"reason": "ExitCode", "message": "127"}]},
    })
    fake = FakeExecClient([("stderr", "sh: nope: not found\n"), ("exit", status)])
    monkeypatch.setattr(exec_ws, "k8s_stream", lambda *a, **k: fake)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        frames, closed = _drain(ws)

    assert frames[0] == {"type": "stderr", "data": "sh: nope: not found\n"}
    assert frames[1]["type"] == "end"
    assert frames[1]["code"] == 127
    assert closed
    assert "exit 127" in _audit_rows()[1].detail


def test_a_multi_container_pod_needs_a_container_and_opens_nothing(
    client, fake_k8s, allow_mutations, monkeypatch
):
    """Exec picks no container for you either — a shell in the wrong one is worse."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app", "envoy")))
    opened = []
    monkeypatch.setattr(exec_ws, "k8s_stream",
                        lambda *a, **k: opened.append(k) or FakeExecClient())

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        frames, closed = _drain(ws)

    assert len(frames) == 1
    assert frames[0]["type"] == "error"
    assert frames[0]["reason"] == "invalid"
    assert "envoy" in frames[0]["detail"]
    assert closed
    assert opened == []
    # No shell existed, so there is nothing to audit: the trail records attempts
    # on the cluster, not requests this console rejected as malformed.
    assert _audit_rows() == []


def test_a_malformed_client_frame_does_not_kill_the_session(client, fake_k8s,
                                                            allow_mutations, monkeypatch):
    """Dropping a frame loses a keystroke; ending the session loses the shell."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake = FakeExecClient()
    monkeypatch.setattr(exec_ws, "k8s_stream", lambda *a, **k: fake)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec") as ws:
        ws.send_text("not json at all")
        ws.send_json({"type": "wat"})
        ws.send_json({"type": "stdin", "data": "id\n"})
        frames, closed = _drain(ws)

    assert [f["type"] for f in frames] == ["stdout", "end"]
    assert frames[1]["code"] == 0
    assert fake.stdin == ["id\n"]
    assert closed


def test_a_disconnected_operator_closes_the_channel(client, fake_k8s, allow_mutations,
                                                    monkeypatch):
    """Closing the tab closes the exec channel and still writes the close record."""
    _allow(fake_k8s)
    fake_k8s.core_v1.returns("read_namespaced_pod", _pod(("app",)))
    fake = FakeExecClient()  # never exits on its own
    monkeypatch.setattr(exec_ws, "k8s_stream", lambda *a, **k: fake)

    with client.websocket_connect("/api/ws/pods/prod/checkout/exec"):
        # Give the session time to open and be audited before leaving.
        for _ in range(200):
            if len(_audit_rows()) == 1:
                break
            time.sleep(0.01)

    for _ in range(200):
        if fake.closed:
            break
        time.sleep(0.01)
    assert fake.closed

    rows = _audit_rows()
    assert len(rows) == 2
    # The exit code was never reported, so the record says so rather than
    # claiming the operator's last command succeeded.
    assert "exit unknown" in rows[1].detail
    assert "disconnected" in rows[1].detail
