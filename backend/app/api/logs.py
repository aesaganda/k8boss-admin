"""
Pod logs (§7) — the snapshot read and the live stream.

Two endpoints and one rule that shapes both: **the console never guesses which
container an operator meant.** A pod with three containers whose logs are read
without a container name is answered with ``422 invalid`` listing them, not with
the first container's output. Silently picking one produces logs that are real,
plausible, and about the wrong process — an operator reads them, concludes the
service is idle, and goes looking for the outage somewhere it is not. That is
this project's defect standard pointed at a log viewer.

The websocket adds a second rule: **it always terminates with exactly one
terminal frame** (``end`` or ``error``), enforced in a ``finally`` rather than
trusted to the happy path. Without it, a viewer cannot distinguish "the pod
stopped logging" from "the connection dropped" — the first is information about
the workload, the second is information about us, and a UI that shows the same
spinner-stops-moving for both teaches operators to trust neither.

**Why there are threads in an async module.** ``read_namespaced_pod_log`` with
``_preload_content=False`` hands back a urllib3 response whose ``stream()`` is a
blocking read. Iterating it directly inside the coroutine would block the event
loop for as long as the pod stays quiet — one silent pod would stall every other
websocket in the process. So the read runs on a worker thread that hands lines
back through an ``asyncio.Queue``, and the socket is closed from the coroutine on
teardown, which is what makes the blocking read return and the thread exit. A
viewer that closes its tab must not leave a thread parked on a socket until the
600-second watch deadline fires; that leak is per closed tab, and a dashboard
left cycling through pods will exhaust the pool in an afternoon.

Flow control is a semaphore of credits rather than an unbounded queue or a
lossy one. A viewer slower than the pod is common (a chatty container, a laptop
on hotel wifi); an unbounded queue makes that the console's memory leak, and
dropping lines makes it a wrong answer. Blocking the reader thread pushes the
backpressure into the kernel socket buffer and then to the API server, which is
where kubectl puts it too.

:mod:`app.api.exec_ws` imports :class:`StreamTerminator`, :func:`resolve_container`,
:func:`error_frame` and :func:`acquire_credit` from here. §7's two streams make
the same promises about terminal frames and container selection, and two copies
of those promises is one copy that gets fixed.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from fastapi import APIRouter, Path, Query, WebSocket
from fastapi.responses import PlainTextResponse
from kubernetes.client.rest import ApiException
from starlette.datastructures import QueryParams
from urllib3.exceptions import HTTPError as Urllib3Error
from urllib3.exceptions import ReadTimeoutError

from app.admin import preflight
from app.errors import AdminError, Invalid, from_api_exception
from app.k8s.client import get_core_v1
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["logs"])

#: §7's bounds on ``tailLines``. The ceiling is the contract's; it also keeps a
#: single request from asking the API server to buffer a whole log file into one
#: response, which fails as a read timeout — "the cluster is unreachable", a
#: diagnosis about the wrong system.
DEFAULT_TAIL_LINES = 500
MAX_TAIL_LINES = 10000

#: Read size for the follow stream. Small enough that a trickle of lines is
#: delivered promptly rather than sitting in a half-full buffer, large enough
#: that a chatty container does not cost one syscall per line.
_CHUNK_BYTES = 16 * 1024

#: Lines allowed in flight between the reader thread and the socket. See the
#: module docstring: this is backpressure, not a buffer size to tune for
#: throughput.
_STREAM_CREDITS = 512

#: How long the reader thread waits for a credit before re-checking whether the
#: viewer has gone. Long enough not to spin, short enough that teardown is not
#: noticeably delayed.
_CREDIT_WAIT_SECONDS = 0.5

#: The pod annotation ``kubectl logs`` uses to pick a container without being
#: told. We read it, report it, and still refuse to act on it — see
#: :func:`resolve_container`.
_DEFAULT_CONTAINER_ANNOTATION = "kubectl.kubernetes.io/default-container"

#: Error-frame ``reason`` per §1.3 error code.
#:
#: §7's example uses ``forbidden``, which is the §1.2 *unavailable* vocabulary
#: rather than the §1.3 *error code* vocabulary, so this table starts from §1.2
#: and adds the tokens §1.2 has no honest home for. ``invalid`` is the one that
#: matters: :data:`app.errors._UNAVAILABLE_REASON` maps it to ``unreachable``
#: (correct for a partial read, where an invalid request is not a thing that
#: happens), and reporting a rejected container name as "unreachable" sends the
#: operator to check DNS and firewalls that are provably fine.
_WS_REASONS: dict[str, str] = {
    "rbac_denied": "forbidden",
    "not_found": "not_found",
    "cluster_unreachable": "unreachable",
    "no_cluster_selected": "no_cluster_selected",
    "unsupported": "unsupported",
    "mutations_disabled": "mutations_disabled",
    "conflict": "conflict",
    "invalid": "invalid",
    "upstream_error": "upstream_error",
}


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #

def ws_reason(error: BaseException) -> str:
    """The error-frame ``reason`` token for an exception.

    ``internal_error`` is deliberately its own token rather than folded into
    ``upstream_error``: a ``TypeError`` in this process is not the cluster
    failing, and telling an operator their API server errored sends them to read
    the wrong component's logs. Same reasoning as
    :func:`app.resources.envelope.reason_for_error`, applied to a socket that has
    no envelope to put an ``unavailable`` entry in.
    """
    if isinstance(error, AdminError):
        if error.context.get("cause") == "timeout":
            # app.k8s.client stamps this when the API server accepted the
            # connection and then did not answer. The operator's next move is to
            # narrow the query, not to check the network path.
            return "timeout"
        return _WS_REASONS.get(error.code, "upstream_error")
    if isinstance(error, ReadTimeoutError):
        return "timeout"
    if isinstance(error, (Urllib3Error, ConnectionError, OSError)):
        return "unreachable"
    return "internal_error"


def error_frame(error: BaseException) -> dict[str, Any]:
    """A §7 ``error`` frame. Terminal: nothing follows it on the socket.

    Every key is always present, ``None`` where it does not apply, for the same
    reason as :meth:`app.errors.AdminError.to_envelope` — a viewer reads
    ``frame.hint`` unconditionally, and an absent key is a ``TypeError`` in the
    browser rather than a missing sentence.

    ``hint`` is carried beyond §7's example on purpose. A viewer that can render
    only "forbidden" plus the API server's own sentence cannot tell the operator
    *which* grant is missing, and that sentence is the whole reason
    :mod:`app.admin.preflight` computes one.
    """
    if isinstance(error, ApiException):
        # Mapped rather than reported as a transport failure. An ApiException
        # that reaches here escaped a reader thread, and it can just as easily be
        # a 403 as a broken connection — "unreachable" would send the operator to
        # check a network that is fine while their RBAC is not.
        error = from_api_exception(error, context={})
    if isinstance(error, AdminError):
        return {
            "type": "error",
            "reason": ws_reason(error),
            "message": error.message,
            "detail": error.detail,
            "hint": error.hint,
        }
    return {
        "type": "error",
        "reason": ws_reason(error),
        "message": "The log stream failed.",
        "detail": f"{type(error).__name__}: {error}",
        "hint": None,
    }


class StreamTerminator:
    """Guarantees at most one terminal frame per socket, and that one is tried.

    §7: *the stream always terminates with exactly one ``end`` or ``error``
    frame*. Two failure modes make that hard to hold by convention. The first is
    sending two — an ``error`` from an exception handler and then an ``end`` from
    the cleanup path, which a viewer reads as "it failed, then it finished
    normally". The second is sending none, when the handler is cancelled or
    raises somewhere nobody expected, which is indistinguishable from the
    connection dropping.

    So the terminal frame goes through here: :meth:`send` is a no-op once
    anything terminal has been emitted, and the endpoint's ``finally`` calls it
    with a default ``end`` frame that fires only if nothing else did.

    A send that fails because the viewer already went away is logged at debug and
    counts as terminated — there is no socket left to make a promise to, and
    retrying would only produce a second exception.
    """

    def __init__(self, websocket: WebSocket, label: str) -> None:
        self._websocket = websocket
        self.label = label
        self.sent = False
        self.frame: dict[str, Any] | None = None

    async def send(self, frame: dict[str, Any]) -> bool:
        """Emit ``frame`` if nothing terminal has been sent. True if it went out."""
        if self.sent:
            return False
        self.sent = True
        self.frame = frame
        try:
            await self._websocket.send_json(frame)
            return True
        except Exception:  # noqa: BLE001 - the socket is gone; nothing to salvage
            logger.debug(
                "Could not deliver the terminal %s frame on %s: the viewer had "
                "already disconnected.", frame.get("type"), self.label,
                exc_info=True,
            )
            return False

    def suppress(self, why: str) -> None:
        """Record that no terminal frame can be delivered, without trying.

        Used when the *viewer* closed the socket: there is nobody left to tell,
        and attempting the send would raise inside a cleanup path and mask
        whatever we were cleaning up after.
        """
        if not self.sent:
            self.sent = True
            logger.debug("No terminal frame sent on %s: %s.", self.label, why)


# --------------------------------------------------------------------------- #
# Query parameters (parsed by hand on the websocket routes)
# --------------------------------------------------------------------------- #
# FastAPI can validate websocket query parameters, but it rejects a bad one by
# closing with policy-violation *before* the handshake completes — so the viewer
# gets a bare close code and no sentence explaining what was wrong. Accepting
# first and validating here costs a few lines and turns that into an `error`
# frame with a reason, which is the whole point of §7's frame vocabulary.

def int_param(
    params: QueryParams,
    key: str,
    *,
    default: int | None,
    minimum: int,
    maximum: int,
) -> int | None:
    """One bounded integer query parameter, or ``default`` when absent.

    Out-of-range is rejected rather than clamped. Clamping ``tailLines=999999``
    to 10000 answers a different question from the one asked and says nothing
    about having done so.
    """
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise Invalid(
            f"`{key}` must be an integer.",
            detail=f"received {raw!r}",
            context={"parameter": key, "value": raw},
        ) from e
    if not minimum <= value <= maximum:
        raise Invalid(
            f"`{key}` must be between {minimum} and {maximum}.",
            detail=f"received {value}",
            context={"parameter": key, "value": value,
                     "minimum": minimum, "maximum": maximum},
        )
    return value


def bool_param(params: QueryParams, key: str, *, default: bool) -> bool:
    """One boolean query parameter.

    An unrecognised spelling is an error, not a false. ``?tty=yes`` silently
    meaning "no" is how an operator ends up with a shell whose terminal size is
    never applied and no idea why.
    """
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    lowered = str(raw).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise Invalid(
        f"`{key}` must be true or false.",
        detail=f"received {raw!r}",
        context={"parameter": key, "value": raw},
    )


# --------------------------------------------------------------------------- #
# Container selection
# --------------------------------------------------------------------------- #

def _pod_containers(pod: Any) -> tuple[list[str], list[str], str | None]:
    """``(containers, initContainers, declared default)`` for one pod object."""
    containers = [
        str(get_field(c, "name"))
        for c in (get_field(pod, "spec", "containers", default=[]) or [])
        if get_field(c, "name")
    ]
    init_containers = [
        str(get_field(c, "name"))
        for c in (get_field(pod, "spec", "initContainers", default=[]) or [])
        if get_field(c, "name")
    ]
    annotations = get_field(pod, "metadata", "annotations", default={}) or {}
    default_container = annotations.get(_DEFAULT_CONTAINER_ANNOTATION)
    return containers, init_containers, default_container


def resolve_container(namespace: str, name: str, container: str | None) -> str | None:
    """Decide which container a log or exec request is about. Never guesses.

    Returns the container name to use, or ``None`` when the pod has exactly one
    container and the API server's own default is therefore unambiguous.

    Raises:
        Invalid: the pod has several containers and the request named none, or
            named one the pod does not have. 422 with the list, so the caller can
            fix it in one step instead of discovering the names by trial.
        AdminError: the pod could not be read *and* no container was named — see
            below.

    **Why a request that names a container still reads the pod.** Validating
    locally turns "the API server rejected your request" into a message that
    lists the valid names. When that read fails but a container *was* named, the
    call proceeds anyway and lets the API server judge: refusing would turn a
    missing ``get pods`` grant into a failure of an endpoint the caller is
    otherwise permitted to use.

    When the read fails and no container was named, there is no safe answer. We
    cannot tell whether the pod has one container or five, so we say that — the
    §0 rule that a code path must be able to distinguish "nothing there" from
    "we could not look", applied to the one place where guessing wrong shows an
    operator somebody else's logs.
    """
    context = {
        "verb": "get", "group": "", "resource": "pods",
        "namespace": namespace, "name": name,
    }
    try:
        pod = get_core_v1().read_namespaced_pod(name=name, namespace=namespace)
    except ApiException as e:
        mapped = from_api_exception(e, context=context)
        if container:
            logger.info(
                "Could not read pod %s/%s to validate container %r (%s); "
                "forwarding the request and letting the API server judge.",
                namespace, name, container, mapped.code,
            )
            return container
        raise mapped from e
    except AdminError as e:
        if container:
            logger.info(
                "Could not read pod %s/%s to validate container %r (%s); "
                "forwarding the request and letting the API server judge.",
                namespace, name, container, e.code,
            )
            return container
        raise

    containers, init_containers, default_container = _pod_containers(pod)
    known = containers + init_containers

    if container:
        if known and container not in known:
            raise Invalid(
                f'Pod "{name}" has no container named "{container}".',
                hint=(
                    "Containers: " + (", ".join(containers) or "none")
                    + (f". Init containers: {', '.join(init_containers)}."
                       if init_containers else ".")
                ),
                context={**context, "container": container,
                         "containers": containers, "initContainers": init_containers},
            )
        return container

    if len(containers) == 1:
        # Unambiguous, so naming it adds nothing. Returned as None so the API
        # server applies its own default — one fewer place for this console to
        # disagree with kubectl about what "the container" means.
        return None

    if not containers:
        # A pod with no containers should not exist; a *shape* we did not expect
        # is the realistic cause (a fake in a test, a future API version). Either
        # way we do not know which container was meant, and that is what we say.
        raise Invalid(
            f'Could not determine which container of pod "{name}" to read.',
            detail="The pod object listed no containers.",
            hint="Name one explicitly with ?container=.",
            context={**context, "containers": containers,
                     "initContainers": init_containers},
        )

    # The ambiguous case, and the reason this function exists.
    #
    # `kubectl` would consult the default-container annotation here and use it.
    # We report it and still refuse: the annotation is set by whoever wrote the
    # manifest, for whoever runs `kubectl logs` without arguments, and an
    # operator debugging an outage in a sidecar-heavy pod has no reason to know
    # it exists. The UI can preselect it from `context.defaultContainer` — that
    # is a suggestion the operator sees and confirms, which is a different thing
    # from a choice made for them and never mentioned.
    raise Invalid(
        f'Pod "{name}" has {len(containers)} containers; say which one.',
        detail="Containers: " + ", ".join(containers) + (
            f". Init containers: {', '.join(init_containers)}." if init_containers else "."
        ),
        hint=(
            "Add ?container=<name>. It is not defaulted: reading the wrong "
            "container's logs looks exactly like reading the right one."
        ),
        context={
            **context,
            "containers": containers,
            "initContainers": init_containers,
            "defaultContainer": default_container,
        },
    )


# --------------------------------------------------------------------------- #
# GET /api/pods/{namespace}/{name}/logs
# --------------------------------------------------------------------------- #

@router.get("/pods/{namespace}/{name}/logs", response_class=PlainTextResponse)
def get_pod_logs(
    namespace: str = Path(..., description="Pod namespace."),
    name: str = Path(..., description="Pod name."),
    container: str | None = Query(
        None,
        description=(
            "Which container. Required when the pod has more than one — the "
            "request is refused with 422 rather than defaulted."
        ),
    ),
    tailLines: int = Query(  # noqa: N803 - §7 wire spelling
        DEFAULT_TAIL_LINES, ge=1, le=MAX_TAIL_LINES,
        description="Lines from the end of the log.",
    ),
    previous: bool = Query(
        False,
        description="Read the previous terminated container's log, for a crash loop.",
    ),
    sinceSeconds: int | None = Query(  # noqa: N803 - §7 wire spelling
        None, ge=1, description="Only lines newer than this many seconds.",
    ),
    timestamps: bool = Query(
        False, description="Prefix every line with the API server's RFC 3339 timestamp.",
    ),
) -> PlainTextResponse:
    """A pod container's recent log output as ``text/plain`` (§7).

    Preflighted on ``get core/pods/log`` — the log is its own subresource in RBAC,
    and a ServiceAccount can hold ``get pods`` without holding it. Preflighting
    the parent would report "allowed" and then fail at the API server with a bare
    forbidden naming nothing.

    An empty body means the container has produced no output. It cannot mean
    anything else: every way this endpoint can fail to look — no permission, no
    such pod, an unreachable API server — raises and is rendered as §1.3, so
    there is no path from a swallowed error to a blank page. That is the
    text/plain equivalent of ``partial``.
    """
    preflight.require(
        "get", "", "pods", namespace=namespace, name=name, subresource="log"
    )
    resolved = resolve_container(namespace, name, container)

    context = {
        "verb": "get", "group": "", "resource": "pods", "subresource": "log",
        "namespace": namespace, "name": name, "container": resolved,
    }
    try:
        text = get_core_v1().read_namespaced_pod_log(
            name=name,
            namespace=namespace,
            container=resolved,
            tail_lines=tailLines,
            previous=previous,
            since_seconds=sinceSeconds,
            timestamps=timestamps,
        )
    except ApiException as e:
        if previous and getattr(e, "status", None) == 400:
            # The API server's 400 here means "this container has not restarted",
            # which from_api_exception would render as "the cluster rejected your
            # request as malformed" — true of the HTTP exchange and useless to
            # the operator, who asked a reasonable question about a pod that has
            # simply not crashed yet.
            mapped = from_api_exception(e, context=context)
            raise Invalid(
                f'No previous log for container "{resolved or "the container"}" '
                f'in pod "{name}".',
                detail=mapped.detail,
                hint=(
                    "`previous` reads the log of a container that has already "
                    "terminated. This one has not restarted."
                ),
                context=context,
            ) from e
        raise from_api_exception(e, context=context) from e

    # str(...) rather than trusting the client: with _preload_content left on,
    # the kubernetes client deserialises the body as a string, but a caller that
    # somehow received bytes would otherwise have them repr'd into the response.
    body = text if isinstance(text, str) else (text or "").__str__()
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


# --------------------------------------------------------------------------- #
# The follow stream
# --------------------------------------------------------------------------- #
# Queue item kinds. `line` carries one decoded log line; `end` and `error` are
# terminal; `disconnect` is the viewer leaving, produced by the receive task
# rather than by the reader thread.
_LINE = "line"
_END = "end"
_ERROR = "error"
_DISCONNECT = "disconnect"


def acquire_credit(credit: threading.Semaphore, stop: threading.Event) -> bool:
    """Wait for a send credit, giving up if the stream is being torn down.

    ``False`` means "stop reading" — either the viewer went away or the endpoint
    is shutting the stream down. Waiting with a timeout in a loop, rather than
    blocking forever, is what makes a reader thread notice a closed tab instead
    of parking on the semaphore until the process exits.
    """
    while not stop.is_set():
        if credit.acquire(timeout=_CREDIT_WAIT_SECONDS):
            return True
    return False


def _split_timestamp(line: str) -> tuple[str | None, str]:
    """Split ``"<rfc3339> message"`` into its timestamp and its message.

    The stream is opened with ``timestamps=true`` and the prefix is split off
    here, so the ``ts`` on a §7 ``log`` frame is *the API server's* timestamp for
    that line. The alternative — stamping the frame with our own clock as it is
    relayed — would be a number that looks like a log time, is off by the
    buffering and network delay, and is wrong by minutes for the backlog that
    ``tailLines`` replays before the live tail begins.

    A line whose prefix does not parse gets ``ts: None`` and its text verbatim.
    Null is the honest answer for "this line carried no timestamp"; inventing one
    is not.
    """
    head, separator, rest = line.partition(" ")
    if separator and "T" in head and (head.endswith("Z") or "+" in head[10:]):
        return head, rest
    return None, line


def _open_log_stream(
    namespace: str, name: str, container: str | None,
    tail_lines: int, since_seconds: int | None,
):
    """Open the follow stream. Runs on a worker thread; returns a urllib3 response.

    ``timestamps=True`` unconditionally — see :func:`_split_timestamp`.
    ``_preload_content=False`` is what makes this a stream rather than a call that
    never returns; it is also the flag ``app.k8s.client._is_streaming`` looks for
    when it decides which read deadline to apply, so a follow stream gets the
    600-second watch deadline instead of being torn down after 30 seconds of a
    quiet pod.
    """
    return get_core_v1().read_namespaced_pod_log(
        name=name,
        namespace=namespace,
        container=container,
        follow=True,
        tail_lines=tail_lines,
        since_seconds=since_seconds,
        timestamps=True,
        _preload_content=False,
    )


def _pump_lines(
    response: Any,
    loop: asyncio.AbstractEventLoop,
    queue: asyncio.Queue,
    credit: threading.Semaphore,
    stop: threading.Event,
) -> None:
    """Read the blocking log stream and post lines to ``queue``. Worker thread.

    Nothing here touches request context: contextvars are not carried into a
    bare thread, so the cluster-scoped client is resolved by the caller and only
    the opened response is handed over. A ``get_core_v1()`` call in this function
    would resolve to whatever the *fallback* cluster is and stream the wrong
    cluster's logs — a misattributed answer, which is the failure mode
    :mod:`app.k8s.context` exists to prevent.

    Errors raised after ``stop`` is set are dropped: that is our own
    ``response.close()`` landing in the middle of a read, and reporting it to a
    viewer who has already gone would be an invented failure.
    """
    def offer(item: tuple[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, item)

    buffer = b""
    try:
        for chunk in response.stream(_CHUNK_BYTES, decode_content=True):
            if stop.is_set():
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if not acquire_credit(credit, stop):
                    return
                offer((_LINE, raw))
        # A trailing fragment with no newline is still output the container
        # produced; dropping it would silently lose the last line of every log
        # that ends without one, which is most crash traces.
        if buffer and not stop.is_set() and acquire_credit(credit, stop):
            offer((_LINE, buffer))
        if not stop.is_set():
            offer((_END, "stream_closed"))
    except Exception as e:  # noqa: BLE001 - relayed to the viewer as an error frame
        if stop.is_set():
            return
        offer((_ERROR, e))


async def _watch_for_disconnect(websocket: WebSocket, queue: asyncio.Queue) -> None:
    """Post a ``disconnect`` item when the viewer goes away.

    A websocket send does not reliably fail the moment the peer leaves — the
    close arrives on the *receive* side, so a stream that only ever sends never
    learns it is talking to nobody. Without this task, a closed tab leaves the
    reader thread holding a socket open against the API server until the watch
    deadline expires, once per closed tab.

    Frames the viewer sends on a log socket are ignored: §7 gives the log stream
    no client vocabulary, and treating an unexpected frame as fatal would let a
    keepalive ping from a proxy close somebody's log view.
    """
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            logger.debug("Ignoring unexpected client frame on a log stream: %s",
                         message.get("type"))
    except Exception:  # noqa: BLE001 - a receive failure *is* a disconnect
        logger.debug("Log stream receive loop ended", exc_info=True)
    queue.put_nowait((_DISCONNECT, None))


async def _relay_lines(
    websocket: WebSocket,
    response: Any,
    terminator: StreamTerminator,
    stop: threading.Event,
) -> None:
    """Relay the reader thread's lines to the socket until something ends it."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    credit = threading.Semaphore(_STREAM_CREDITS)

    reader = threading.Thread(
        target=_pump_lines,
        args=(response, loop, queue, credit, stop),
        name="k8boss-admin-logs",
        daemon=True,
    )
    reader.start()
    watcher = asyncio.create_task(_watch_for_disconnect(websocket, queue))

    try:
        while True:
            kind, payload = await queue.get()
            if kind == _LINE:
                text = payload.decode("utf-8", errors="replace")
                ts, line = _split_timestamp(text)
                await websocket.send_json({"type": "log", "line": line, "ts": ts})
                # Released only after the frame is on the wire, so a slow viewer
                # slows the reader rather than filling memory behind it.
                credit.release()
            elif kind == _END:
                await terminator.send({"type": "end", "reason": payload})
                return
            elif kind == _ERROR:
                logger.info("Log stream for %s failed: %r", terminator.label, payload)
                await terminator.send(error_frame(payload))
                return
            else:  # _DISCONNECT
                terminator.suppress("the viewer disconnected")
                return
    finally:
        # Order matters: stop first so the thread stops offering, then close the
        # response so its blocking read returns and the thread actually exits.
        stop.set()
        watcher.cancel()
        try:
            response.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            logger.debug("Closing the log stream response failed", exc_info=True)


@router.websocket("/ws/pods/{namespace}/{name}/logs")
async def stream_pod_logs(websocket: WebSocket, namespace: str, name: str) -> None:
    """Follow a container's log (§7).

    Server frames: ``{"type":"log","line","ts"}`` for output,
    ``{"type":"end","reason"}`` when the stream finished, ``{"type":"error",
    "reason","message","detail","hint"}`` when it could not. Exactly one of the
    last two is sent, always — enforced by :class:`StreamTerminator` and the
    ``finally`` below rather than by every ``return`` in this function
    remembering to.

    The connection is accepted before anything is validated, so that a refusal
    arrives as an ``error`` frame the viewer can render. Closing during the
    handshake instead would give the browser a numeric close code and nothing
    else, which is how "you lack `get pods/log` in this namespace" becomes "the
    log viewer is broken" in a bug report.
    """
    await websocket.accept()
    label = f"{namespace}/{name}"
    terminator = StreamTerminator(websocket, label)
    response: Any = None
    stop = threading.Event()

    try:
        params = websocket.query_params
        container = params.get("container") or None
        tail_lines = int_param(
            params, "tailLines",
            default=DEFAULT_TAIL_LINES, minimum=1, maximum=MAX_TAIL_LINES,
        )
        since_seconds = int_param(
            params, "sinceSeconds", default=None, minimum=1, maximum=31536000,
        )

        # Both of these are blocking Kubernetes calls. `to_thread` copies the
        # context, so the cluster pinned by ClusterContextMiddleware from
        # ?cluster_id= follows them onto the worker.
        await asyncio.to_thread(
            preflight.require,
            "get", "", "pods", namespace=namespace, name=name, subresource="log",
        )
        resolved = await asyncio.to_thread(
            resolve_container, namespace, name, container
        )
        response = await asyncio.to_thread(
            _open_log_stream, namespace, name, resolved, tail_lines, since_seconds,
        )

        await _relay_lines(websocket, response, terminator, stop)
    except ApiException as e:
        await terminator.send(error_frame(from_api_exception(
            e,
            context={"verb": "get", "group": "", "resource": "pods",
                     "subresource": "log", "namespace": namespace, "name": name},
        )))
    except AdminError as e:
        await terminator.send(error_frame(e))
    except Exception as e:  # noqa: BLE001 - a bug here must still terminate the stream
        logger.exception("Unhandled failure on the log stream for %s", label)
        await terminator.send(error_frame(e))
    finally:
        stop.set()
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                logger.debug("Closing the log stream response failed", exc_info=True)
        # The guarantee. If every path above already sent something terminal this
        # is a no-op; if one did not — a cancellation, an exception in an
        # exception handler — the viewer still learns the stream is over instead
        # of watching a socket that stopped moving.
        await terminator.send({"type": "end", "reason": "stream_closed"})
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed is the common case
            logger.debug("Closing the log websocket for %s failed", label, exc_info=True)


__all__ = [
    "DEFAULT_TAIL_LINES",
    "MAX_TAIL_LINES",
    "StreamTerminator",
    "acquire_credit",
    "bool_param",
    "error_frame",
    "get_pod_logs",
    "int_param",
    "resolve_container",
    "router",
    "stream_pod_logs",
    "ws_reason",
]
