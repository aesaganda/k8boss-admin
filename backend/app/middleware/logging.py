"""
Structured request logging.

Every request produces one JSON line with a correlation id, method, path,
status and duration. Request and response *bodies* are never logged, so there is
nothing to redact in them — and query-string **values** are dropped for the same
reason, keeping only the parameter names.

That last point is not theoretical: the obvious implementation logs the raw
query string, and this console's URLs carry ``?container=``, ``?fieldSelector=``
and — on any future OAuth callback — ``?code=``, a single-use bearer-equivalent
credential. Names-only rather than a redact-these-keys list, because a list has
to be extended for every new parameter and fails *silently* when someone forgets.
Names alone still answer "which parameters did this request carry".
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid

from pythonjsonlogger import json as json_log
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings

# Correlation id of the request being handled, stamped onto every log record by
# RequestIdFilter so a line emitted deep in a Kubernetes helper can be tied back
# to the request that caused it.
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# An inbound X-Request-ID is caller-controlled, so it is accepted only as a short
# opaque token — otherwise it is a log-injection and unbounded-field vector.
# fullmatch, not match: Python's "$" also matches before a trailing newline, so
# "^...$" would accept "abc\n". Unreachable over real HTTP (h11 rejects a bare LF
# in a header) and the JSON formatter escapes it anyway, but a header is the
# wrong place to depend on two downstream layers being careful.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestIdFilter(logging.Filter):
    """Stamps the current request id onto every record passing through."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


def _query_param_names(query: str) -> str | None:
    """Parameter names from a query string, values dropped. See the module docstring."""
    if not query:
        return None
    names = [part.split("=", 1)[0] for part in query.split("&") if part]
    return ",".join(name for name in names if name) or None


def setup_logging() -> None:
    """Configure root JSON logging. Idempotent — replaces existing handlers."""
    handler = logging.StreamHandler()
    handler.setFormatter(
        json_log.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(request_id)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(RequestIdFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))

    # urllib3 logs a WARNING per request when TLS verification is off, and
    # kubernetes logs its own request lines. Both would double every entry here.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("kubernetes").setLevel(logging.WARNING)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs each HTTP request with timing, status and a correlation id.

    ``BaseHTTPMiddleware`` is fine *here* — unlike ``ClusterContextMiddleware``,
    nothing downstream reads this contextvar from the sync threadpool; it is read
    by the logging filter, which runs in whichever frame emits the record, and
    the id is also echoed in the response header.
    """

    async def dispatch(self, request: Request, call_next):
        inbound = request.headers.get("x-request-id", "")
        request_id = inbound if _REQUEST_ID_RE.fullmatch(inbound) else uuid.uuid4().hex[:12]
        start = time.perf_counter()

        # Set before call_next: the downstream task copies the context at spawn
        # time, so a later set would not reach it.
        request.state.request_id = request_id
        token = request_id_ctx.set(request_id)
        logger = logging.getLogger("http")

        try:
            try:
                response: Response = await call_next(request)
            except Exception as e:
                # Logged and re-raised. The exception handlers turn known errors
                # into proper envelopes; this line exists so an *unknown* one
                # still leaves a record naming the endpoint, which is the only
                # evidence a 500 rendered by ServerErrorMiddleware produces.
                logger.error(
                    "Request failed",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                        "error": type(e).__name__,
                    },
                )
                raise

            logger.info(
                "Request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "query_params": _query_param_names(request.url.query),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                },
            )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_ctx.reset(token)
