"""
Pod exec (§7) — a shell inside a running container.

**This is not a write, and it is gated like one.** Nothing here calls
:func:`app.admin.mutate.mutate`: there is no object to diff, no dry run that
means anything, and no resourceVersion to check. But a shell in a production pod
can do everything a write can and more — edit a file the deployment does not
describe, kill a process, read a mounted Secret — while leaving no trace in the
cluster's own audit of *what changed*. So it borrows the two controls that
matter from the write path and skips the two that do not:

* ``ADMIN_ALLOW_MUTATIONS`` must be on. A console deployed read-only is one an
  operator believes cannot alter their cluster, and an exec socket would make
  that belief false. Refused before the cluster is touched (§1.6), and the
  refusal is itself audited — "somebody tried to open a shell on the read-only
  console" is a fact worth keeping.
* A ``SelfSubjectAccessReview`` on ``create core/pods/exec``. RBAC names the
  subresource separately from the pod, so a ServiceAccount can hold ``get pods``
  and not this. Preflighting the parent would report allowed and then fail at the
  API server with a bare forbidden naming nothing.

**Audited on open and on close, as two records.** One record written at the end
would be lost entirely if the process were restarted mid-session — precisely the
sessions worth knowing about. The open record says a shell was opened and by
whom; the close record says how long it lasted, how it ended and how much went
through it. A session that appears in the trail with no close is itself
informative: it did not end cleanly.

The threading model, the credit-based flow control and the exactly-one-terminal-
frame guarantee are the ones :mod:`app.api.logs` documents; this module imports
its primitives rather than restating them. ``kubernetes.stream.stream`` with
``_preload_content=False`` hands back a ``WSClient`` whose reads are blocking
``select`` calls, so they run on a worker thread and the socket is closed from
the coroutine to make them return.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from fastapi import APIRouter, WebSocket
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream as k8s_stream
from kubernetes.stream.ws_client import ERROR_CHANNEL, RESIZE_CHANNEL

from app.admin import preflight
from app.api.logs import (
    StreamTerminator,
    acquire_credit,
    bool_param,
    error_frame,
    resolve_container,
)
from app.audit import recorder
from app.config import settings
from app.errors import AdminError, Invalid, MutationsDisabled, from_api_exception
from app.k8s.client import get_core_v1

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["exec"])

#: §7's default command. A list, so ``?command=/bin/bash&command=-lc&command=...``
#: is passed through argument by argument — joining a string and letting a shell
#: split it would make quoting in this console's URL bar decide what runs as root
#: in somebody's pod.
DEFAULT_COMMAND: tuple[str, ...] = ("/bin/sh",)

#: Upper bound on the argv the caller may specify. Not a security control — the
#: first argument alone is enough to run anything — but a bound on what a
#: malformed client can put in a URL and in the audit record's detail.
_MAX_COMMAND_ARGS = 32

#: Chunks of container output allowed in flight towards a slow viewer, and how
#: long the reader thread blocks on the exec socket per cycle. The poll is short
#: because it also bounds how quickly the thread notices teardown.
_OUTPUT_CREDITS = 256
_POLL_SECONDS = 1.0

#: Terminal geometry bounds. A terminal is not 0 columns wide, and a resize to
#: 100000 columns is a malformed client, not a very large monitor. Rejected
#: rather than clamped, but *not* fatal: see :func:`_apply_resize`.
_MIN_TERM = 1
_MAX_TERM = 1000

# Queue item kinds.
_STDOUT = "stdout"
_STDERR = "stderr"
_CLOSED = "closed"
_ERROR = "error"
_CLIENT = "client"
_DISCONNECT = "disconnect"


def _target(namespace: str, name: str) -> dict[str, Any]:
    """The §10 audit target for an exec session.

    ``subresource: "exec"`` is the field that separates this record from an
    ordinary read of the same pod in the trail. Without it, "who opened a shell
    on checkout-7d9" is a question the audit table cannot answer, which is most
    of the reason these records exist.
    """
    return {
        "group": "", "version": "v1", "resource": "pods",
        "namespace": namespace, "name": name, "subresource": "exec",
    }


def _audit(
    namespace: str, name: str, *, outcome: str, detail: str, error: str | None = None
) -> int | None:
    """One audit row for this session. Never raises — see :mod:`app.audit`.

    A failed INSERT must not take the operator's shell down with it: the session
    is already open, and closing it would neither restore the record nor undo
    anything. The failure is logged loudly by the recorder itself.
    """
    return recorder.record(
        verb="create",
        target=_target(namespace, name),
        dry_run=False,
        outcome=outcome,
        detail=detail,
        error=error,
    )


def _close_quietly(client: Any) -> None:
    """Close the exec channel, from a throwaway thread, without waiting for it.

    Two reasons this is not ``await asyncio.to_thread(client.close)``.

    **It must happen even when the handler is being cancelled.** A viewer that
    closes its tab makes the server cancel this websocket task; every ``await``
    in the cleanup path then raises immediately, and an exec channel closed by an
    ``await`` would simply never be closed — leaving a socket open to the API
    server and a reader thread polling it, once per closed tab.

    **It can block.** ``websocket-client``'s close sends a close frame and waits
    for the peer's, several seconds by default. On the event loop that stalls
    every other stream in the process; the thread it is on here is going away
    either way.
    """
    def run() -> None:
        try:
            client.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            logger.debug("Closing the exec channel failed", exc_info=True)

    threading.Thread(target=run, name="k8boss-admin-exec-close", daemon=True).start()


def _parse_command(values: list[str]) -> list[str]:
    """The argv to exec, from repeated ``?command=`` parameters."""
    command = [value for value in values if value != ""]
    if not command:
        return list(DEFAULT_COMMAND)
    if len(command) > _MAX_COMMAND_ARGS:
        raise Invalid(
            f"Too many `command` arguments: {len(command)} (limit {_MAX_COMMAND_ARGS}).",
            context={"parameter": "command", "count": len(command),
                     "limit": _MAX_COMMAND_ARGS},
        )
    return command


def _open_exec(
    namespace: str, name: str, container: str | None, command: list[str], tty: bool
) -> Any:
    """Open the exec channel. Runs on a worker thread; returns a ``WSClient``.

    ``_preload_content=False`` is what makes this a live channel rather than a
    call that collects the whole output and returns it; it is also the flag
    ``app.k8s.client._is_streaming`` looks for when choosing the read deadline,
    so an idle shell is not torn down after 30 seconds.

    ``stderr`` is requested even with a TTY. The API server merges the two
    streams in TTY mode, so the ``stderr`` frames simply never fire there — which
    is accurate. Not requesting it would mean that a caller who asked for
    ``tty=false`` and got a program that only writes to stderr would see an empty
    session and conclude the command produced nothing.
    """
    return k8s_stream(
        get_core_v1().connect_get_namespaced_pod_exec,
        name,
        namespace,
        container=container,
        command=command,
        stderr=True,
        stdin=True,
        stdout=True,
        tty=tty,
        _preload_content=False,
    )


def _exit_status(client: Any) -> tuple[int | None, str | None]:
    """``(exit code, explanation)`` from the exec error channel.

    The API server closes the session with a JSON ``Status`` on channel 3:
    ``{"status":"Success"}`` for exit 0, or a ``Failure`` whose ``ExitCode``
    cause carries the number. Both are parsed here rather than through
    ``WSClient.returncode``, which assumes the payload is present and
    well-formed and raises ``TypeError`` on the empty string — turning a session
    that ended abruptly into a stack trace instead of an ``end`` frame.

    A ``None`` code means *we do not know what the command returned*, never
    "zero". Reporting an unknown exit as success is the confident wrong answer
    this project is built against, and it is the one an operator would act on:
    exit 0 on a migration job means the migration ran.
    """
    try:
        raw = client.read_channel(ERROR_CHANNEL, timeout=0)
    except Exception as e:  # noqa: BLE001 - the channel is gone; say so, do not guess
        logger.debug("Could not read the exec error channel", exc_info=True)
        return None, f"the exit status could not be read ({type(e).__name__}: {e})"
    if not raw:
        return None, "the API server did not report an exit status"

    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, str(raw)[:500]
    if not isinstance(payload, dict):
        return None, str(raw)[:500]

    if payload.get("status") == "Success":
        return 0, None
    causes = (payload.get("details") or {}).get("causes") or []
    for cause in causes:
        if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message")), payload.get("message")
            except (TypeError, ValueError):
                break
    return None, payload.get("message") or str(raw)[:500]


def _pump_output(
    client: Any,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    credit: threading.Semaphore,
    stop: threading.Event,
) -> None:
    """Relay container output to ``queue``. Worker thread.

    Touches no request context: contextvars do not cross into a bare thread, so
    the cluster-scoped client is resolved by the caller and only the opened
    channel is handed over. Resolving one here would silently address whichever
    cluster the fallback picks.

    An exception raised after ``stop`` is set is dropped: that is our own
    ``client.close()`` landing inside a ``select``, and reporting it would invent
    a failure for a session the operator ended deliberately.
    """
    def offer(item: tuple[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    try:
        while not stop.is_set():
            if not client.is_open():
                break
            # One bounded blocking poll per cycle, then a non-blocking drain of
            # both channels. read_channel(timeout=0) pulls whatever `update`
            # buffered without going back to the socket.
            client.update(timeout=_POLL_SECONDS)
            for kind, read in ((_STDOUT, client.read_stdout), (_STDERR, client.read_stderr)):
                data = read(timeout=0)
                if not data:
                    continue
                if not acquire_credit(credit, stop):
                    return
                offer((kind, data))
        if not stop.is_set():
            offer((_CLOSED, None))
    except Exception as e:  # noqa: BLE001 - relayed to the viewer as an error frame
        if stop.is_set():
            return
        offer((_ERROR, e))


async def _watch_client(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Post client frames, and the disconnect, onto ``queue``.

    Everything the viewer sends goes through the same queue as the container's
    output so that one coroutine applies it in order — writing to stdin from a
    second task would interleave two operators' keystrokes on a shared session
    and produce a command neither of them typed.
    """
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            queue.put_nowait((_CLIENT, message))
    except Exception:  # noqa: BLE001 - a receive failure *is* a disconnect
        logger.debug("Exec receive loop ended", exc_info=True)
    queue.put_nowait((_DISCONNECT, None))


def _decode_client_frame(message: dict[str, Any]) -> dict[str, Any] | None:
    """One client frame as a dict, or ``None`` if it was not usable.

    A malformed frame is dropped with a log line rather than closing the session.
    The trade is explicit: dropping one frame loses at most a keystroke, and the
    operator sees their shell not echo it. Treating it as fatal would kill a live
    session — possibly mid-command, in production — because a proxy injected a
    keepalive or a client had a bug in one code path.
    """
    raw = message.get("text")
    if raw is None and message.get("bytes") is not None:
        raw = message["bytes"].decode("utf-8", errors="replace")
    if not raw:
        return None
    try:
        frame = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Dropping an exec client frame that was not JSON.")
        return None
    if not isinstance(frame, dict):
        logger.warning("Dropping an exec client frame that was not an object.")
        return None
    return frame


async def _apply_resize(client: Any, frame: dict[str, Any], tty: bool) -> None:
    """Forward a ``resize`` frame on the exec resize channel.

    Non-fatal in every failure mode, which is the point. With ``tty=false`` there
    is no terminal to resize and the frame is logged and ignored; with malformed
    dimensions it is logged and ignored. The alternative is an ``error`` frame,
    and §7 makes those terminal — ending an operator's shell because their
    terminal emulator reported a size we did not like is a far worse outcome than
    a window that stays 80 columns wide.
    """
    if not tty:
        logger.info(
            "Ignoring a resize frame on an exec session opened with tty=false: "
            "there is no terminal to resize."
        )
        return
    try:
        cols = int(frame.get("cols"))
        rows = int(frame.get("rows"))
    except (TypeError, ValueError):
        logger.warning("Ignoring a resize frame with non-numeric dimensions: %r", frame)
        return
    if not (_MIN_TERM <= cols <= _MAX_TERM and _MIN_TERM <= rows <= _MAX_TERM):
        logger.warning("Ignoring an out-of-range resize frame: %sx%s", cols, rows)
        return
    # The wire form the kubelet expects on channel 4 — capitalised keys, and a
    # JSON object rather than the frame we received, which carries our own field
    # names.
    await asyncio.to_thread(
        client.write_channel, RESIZE_CHANNEL,
        json.dumps({"Width": cols, "Height": rows}),
    )


@router.websocket("/ws/pods/{namespace}/{name}/exec")
async def exec_in_pod(websocket: WebSocket, namespace: str, name: str) -> None:
    """Interactive exec into a container (§7).

    Client frames: ``{"type":"stdin","data":"..."}`` and
    ``{"type":"resize","cols":N,"rows":N}``. Server frames:
    ``{"type":"stdout","data"}``, ``{"type":"stderr","data"}``,
    ``{"type":"error","reason",...}`` and ``{"type":"end","code","detail"}``,
    with exactly one of the last two ending the session.

    The connection is accepted before the gate and the preflight run, so a
    refusal reaches the viewer as an ``error`` frame naming the reason rather
    than as a numeric close code. "mutations are disabled on this deployment"
    and "you lack create pods/exec in prod" are different problems with different
    fixes, and a viewer that cannot tell them apart sends the operator to the
    wrong one.
    """
    await websocket.accept()
    label = f"{namespace}/{name}"
    terminator = StreamTerminator(websocket, f"exec {label}")
    client: Any = None
    stop = threading.Event()
    opened_at = time.monotonic()
    session_open = False
    stdin_bytes = 0
    output_bytes = 0
    exit_code: int | None = None
    exit_detail: str | None = None
    end_reason = "the session ended"

    try:
        params = websocket.query_params
        container = params.get("container") or None
        command = _parse_command(params.getlist("command"))
        tty = bool_param(params, "tty", default=True)

        # 1. The mutations gate, before the cluster is touched (§1.6). Dry run is
        #    meaningless for a shell, so unlike a write there is no permitted
        #    read-only path through here.
        if not settings.admin_allow_mutations:
            error = MutationsDisabled(
                "Opening a shell in a pod is disabled on this console.",
                hint=(
                    "Set ADMIN_ALLOW_MUTATIONS=true to allow it. Exec is gated with "
                    "the writes because a shell can do everything a write can."
                ),
                context={**_target(namespace, name), "verb": "create"},
            )
            await asyncio.to_thread(
                _audit, namespace, name, outcome="denied",
                detail=f"exec refused (read-only console): {' '.join(command)}",
                error=f"{error.code}: {error.message}",
            )
            logger.warning(
                "Refused an exec into %s: ADMIN_ALLOW_MUTATIONS is false.", label
            )
            await terminator.send(error_frame(error))
            return

        # 2. Preflight the subresource RBAC actually names (§0.2).
        try:
            await asyncio.to_thread(
                preflight.require,
                "create", "", "pods",
                namespace=namespace, name=name, subresource="exec",
            )
        except AdminError as e:
            await asyncio.to_thread(
                _audit, namespace, name,
                # A clean denial is `denied`; a review that could not be
                # evaluated is `failed`. Recording the second as a denial would
                # put "this operator was refused" in the trail when what happened
                # is that we could not find out.
                outcome="denied" if e.code == "rbac_denied" else "failed",
                detail=f"exec preflight refused: {' '.join(command)}",
                error=f"{e.code}: {e.message}",
            )
            raise

        resolved = await asyncio.to_thread(resolve_container, namespace, name, container)

        try:
            client = await asyncio.to_thread(
                _open_exec, namespace, name, resolved, command, tty
            )
        except ApiException as e:
            mapped = from_api_exception(
                e, context={**_target(namespace, name), "verb": "create"}
            )
            await asyncio.to_thread(
                _audit, namespace, name, outcome="failed",
                detail=f"exec failed to open: {' '.join(command)}",
                error=f"{mapped.code}: {mapped.message}",
            )
            raise mapped from e

        # 3. Audited on open, not only on close. A session that never gets a
        #    close record — the pod was killed, this process restarted — is
        #    exactly the one an incident review needs to see.
        session_open = True
        await asyncio.to_thread(
            _audit, namespace, name, outcome="applied",
            detail=(
                f"exec session opened: {' '.join(command)}"
                + (f" in container {resolved}" if resolved else "")
                + f" (tty={'on' if tty else 'off'})"
            ),
        )

        # A mutable tally handed down rather than returned: the close audit
        # record has to state how much went through the session even when the
        # session ended by an exception, and a return value is not delivered on
        # that path.
        counters = {"stdin": 0, "output": 0}
        try:
            exit_code, exit_detail, end_reason = await _run_session(
                websocket, client, terminator, stop, tty, counters,
            )
        finally:
            stdin_bytes = counters["stdin"]
            output_bytes = counters["output"]
    except ApiException as e:
        await terminator.send(error_frame(from_api_exception(
            e, context={**_target(namespace, name), "verb": "create"},
        )))
        end_reason = "the exec channel failed"
    except AdminError as e:
        await terminator.send(error_frame(e))
        end_reason = f"refused: {e.code}"
    except asyncio.CancelledError:
        # The ASGI server cancels this task when the viewer's socket goes away,
        # and that races _run_session's own _DISCONNECT branch. When cancellation
        # wins, `end_reason` is still its initial generic value and the close
        # record says "the session ended" about a session somebody walked away
        # from — the same fact with the useful half missing, in the one row whose
        # job is to say what happened.
        #
        # CancelledError is a BaseException, so the handler below does not catch
        # it and this clause is not redundant. Re-raised rather than swallowed:
        # suppressing cancellation leaves the task running after the server asked
        # it to stop. The `finally` block still writes the close record, because
        # every statement there up to the first `await` runs during cancellation
        # — which is exactly why it was written that way.
        end_reason = "the operator disconnected"
        raise
    except Exception as e:  # noqa: BLE001 - a bug here must still end the session
        logger.exception("Unhandled failure on the exec session for %s", label)
        await terminator.send(error_frame(e))
        end_reason = "the console failed"
    finally:
        # Every statement up to the first `await` runs even when this task is
        # being cancelled, which is what a viewer closing its tab does. The two
        # things that must not be lost — the channel and the close record — are
        # therefore both on this side of it.
        stop.set()
        if client is not None:
            _close_quietly(client)
        if session_open:
            # 4. Audited on close: how it ended, how long it lasted, how much
            #    went through it. `exit code unknown` is written as such — an
            #    audit row claiming exit 0 for a session whose status we never
            #    read would be a fabricated success.
            #
            #    Written inline rather than on a worker thread: `await
            #    asyncio.to_thread(...)` here would raise instead of running
            #    whenever the operator disconnected, which is the *normal* way an
            #    exec session ends. A trail that records every session that
            #    finished tidily and none of the ones somebody walked away from
            #    is worse than no trail, and it costs one INSERT on a socket that
            #    is already closing.
            duration = time.monotonic() - opened_at
            _audit(
                namespace, name, outcome="applied",
                detail=(
                    f"exec session closed after {duration:.1f}s "
                    f"(exit {'unknown' if exit_code is None else exit_code}"
                    + (f": {exit_detail}" if exit_detail else "")
                    + f"); {stdin_bytes} B stdin, {output_bytes} B output; {end_reason}"
                ),
            )
        # The §7 guarantee, enforced here rather than on every return path above.
        await terminator.send({
            "type": "end",
            "code": exit_code,
            "detail": exit_detail or end_reason,
        })
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed is the common case
            logger.debug("Closing the exec websocket for %s failed", label, exc_info=True)


async def _run_session(
    websocket: WebSocket,
    client: Any,
    terminator: StreamTerminator,
    stop: threading.Event,
    tty: bool,
    counters: dict[str, int],
) -> tuple[int | None, str | None, str]:
    """Pump the session until something ends it.

    Returns ``(exit code, exit detail, human reason)``. The exit code is ``None``
    whenever the session did not end with a status we could read — a viewer that
    disconnected, a channel that failed — because the alternative is telling an
    operator their command succeeded when nobody watched it finish.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    credit = threading.Semaphore(_OUTPUT_CREDITS)

    reader = threading.Thread(
        target=_pump_output,
        args=(client, loop, queue, credit, stop),
        name="k8boss-admin-exec",
        daemon=True,
    )
    reader.start()
    watcher = asyncio.create_task(_watch_client(websocket, queue))

    try:
        while True:
            kind, payload = await queue.get()

            if kind in (_STDOUT, _STDERR):
                counters["output"] += len(payload)
                await websocket.send_json({"type": kind, "data": payload})
                # Released after the frame is on the wire: a slow viewer slows
                # the reader instead of filling this process's memory with a
                # `yes` loop's output.
                credit.release()

            elif kind == _CLIENT:
                frame = _decode_client_frame(payload)
                if frame is None:
                    continue
                frame_type = frame.get("type")
                if frame_type == "stdin":
                    data = frame.get("data")
                    if not isinstance(data, str) or not data:
                        continue
                    counters["stdin"] += len(data)
                    await asyncio.to_thread(client.write_stdin, data)
                elif frame_type == "resize":
                    await _apply_resize(client, frame, tty)
                else:
                    logger.warning(
                        "Ignoring an exec client frame of unknown type %r.", frame_type
                    )

            elif kind == _CLOSED:
                code, detail = await asyncio.to_thread(_exit_status, client)
                return code, detail, "the command exited"

            elif kind == _ERROR:
                logger.info("Exec session %s failed: %r", terminator.label, payload)
                await terminator.send(error_frame(payload))
                return None, None, "the exec channel failed"

            else:  # _DISCONNECT
                terminator.suppress("the operator disconnected")
                return None, None, "the operator disconnected"
    finally:
        # Stop first so the thread stops offering, then close the channel so its
        # blocking select returns and the thread exits rather than living until
        # the watch deadline — one leaked thread per closed browser tab.
        stop.set()
        watcher.cancel()


__all__ = ["DEFAULT_COMMAND", "exec_in_pod", "router"]
