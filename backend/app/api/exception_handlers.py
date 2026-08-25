"""
Global exception handlers.

Registered on the app, which puts them in Starlette's ``ExceptionMiddleware`` —
*inside* ``CORSMiddleware``. That placement is the entire point. An exception
that escapes to ``ServerErrorMiddleware`` is rendered *outside* the CORS layer,
so the 500 carries no ``Access-Control-Allow-Origin`` header, the browser blocks
it, and ``fetch()`` rejects with ``TypeError: Failed to fetch``. The operator
then sees a network error instead of "the cluster is unreachable" or "you are
missing `patch` on `apps/deployments`" — the exact class of confidently useless
answer this project is built against.

The handlers below cover everything a route is *expected* to raise. They do not
cover everything it *can*: a bug raises something nothing maps, and that escapes
to ``ServerErrorMiddleware`` exactly as described above. :func:`unhandled_error_handler`
is the floor under that — see :mod:`app.main` for where it is mounted, which is
the part that matters, because a handler for ``Exception`` registered here would
render outside the CORS layer and change nothing.

Handlers, in the order they are consulted:

* :class:`app.errors.AdminError` and every subclass — rendered as the §1.3
  envelope with the subclass's own status code.
* ``kubernetes.client.rest.ApiException`` — mapped through
  :func:`app.errors.from_api_exception`. A safety net, not the primary path:
  handlers that know what they were attempting should map it themselves so the
  envelope's ``context`` names the target. Here we only know the request.
* ``RequestValidationError`` — FastAPI's own 422, re-rendered as ``invalid`` so
  the frontend has one error shape to parse rather than two.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from kubernetes.client.rest import ApiException
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import AdminError, InternalError, Invalid, from_api_exception
from app.k8s.auth import AuthError

logger = logging.getLogger(__name__)


def _render(error: AdminError) -> JSONResponse:
    return JSONResponse(status_code=error.http_status, content=error.to_envelope())


async def admin_error_handler(request, exc: AdminError) -> JSONResponse:
    """Render any AdminError as its §1.3 envelope.

    Logged at WARNING, not ERROR: an ``rbac_denied`` or a ``conflict`` is the
    system working — the console asked, the cluster said no, and the operator is
    being told exactly what and why. Logging those at ERROR trains people to
    ignore ERROR, which is how the ones that matter get missed.
    """
    logger.warning(
        "%s on %s: %s", exc.code, request.url.path, exc.message,
        extra={"error_code": exc.code, "context": exc.context},
    )
    return _render(exc)


async def api_exception_handler(request, exc: ApiException) -> JSONResponse:
    """Map an unhandled Kubernetes ApiException onto the error vocabulary.

    ``context`` carries only the request path, because that is genuinely all we
    know at this level — inventing a verb/resource here would put a wrong target
    in an ``rbac_denied`` envelope, and the UI disables buttons based on that
    target. Handlers that reach Kubernetes deliberately should catch
    ``ApiException`` themselves and call ``from_api_exception`` with the real
    target; this exists so the ones that miss a path still produce a usable
    answer instead of a CORS-less 500.
    """
    error = from_api_exception(exc, context={"path": request.url.path})
    logger.error(
        "Unmapped Kubernetes ApiException on %s: status=%s -> %s",
        request.url.path, getattr(exc, "status", None), error.code,
    )
    return _render(error)


async def auth_error_handler(request, exc: AuthError) -> JSONResponse:
    """A stored cluster's authentication configuration cannot build a client.

    422 ``invalid``, not 502: nothing was wrong with the cluster and nothing was
    attempted against it. The stored registration is unusable, and the fix is in
    the console's own cluster form.
    """
    logger.error("Authentication configuration error on %s: %s", request.url.path, exc)
    return _render(
        Invalid(
            "The cluster's stored authentication configuration cannot be used.",
            detail=str(exc),
            hint="Edit the cluster and re-enter its authentication type and token.",
            context={"path": request.url.path},
        )
    )


async def validation_error_handler(request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI request validation, re-rendered as the contract's ``invalid``.

    FastAPI's default body is ``{"detail": [...]}``, a second error shape the
    frontend would have to special-case. The per-field errors survive in
    ``context.errors`` — dropping them would replace a precise message with
    "the request was not valid", which is the sort of downgrade that gets
    debugged by guessing.
    """
    errors = []
    for item in exc.errors():
        errors.append(
            {
                "location": ".".join(str(part) for part in item.get("loc", ())),
                "message": item.get("msg"),
                "type": item.get("type"),
            }
        )
    first = errors[0]["message"] if errors else "The request was not valid."
    logger.warning("Request validation failed on %s: %s", request.url.path, errors)
    return _render(
        Invalid(
            f"The request was not valid: {first}",
            hint="Correct the highlighted fields and resubmit.",
            context={"path": request.url.path, "errors": errors},
        )
    )


async def http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
    """Render Starlette's own HTTPException in the §1.3 shape.

    Mostly 404s for unrouted paths and 405s for a wrong method. Without this the
    API would answer some errors as ``{"detail": ...}`` and the rest as
    ``{"error": ...}``, and every frontend call site would need both readers.
    ``error`` is derived from the status so the codes stay inside the contract's
    published table rather than inventing new ones per handler.
    """
    code = {
        403: "rbac_denied", 404: "not_found", 409: "conflict",
        422: "invalid", 501: "unsupported",
    }.get(exc.status_code, "invalid" if exc.status_code < 500 else "upstream_error")
    detail = exc.detail if isinstance(exc.detail, str) else None
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": code,
            "message": detail or f"HTTP {exc.status_code}.",
            "detail": None,
            "hint": None,
            "context": {"path": request.url.path},
        },
        headers=getattr(exc, "headers", None),
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install every handler on the app.

    ``AdminError`` is registered once and catches every subclass: Starlette walks
    the exception's MRO looking for a handler, so a new error class inherits
    correct rendering instead of needing a registration nobody remembers to add.
    """
    app.add_exception_handler(AdminError, admin_error_handler)
    app.add_exception_handler(ApiException, api_exception_handler)
    app.add_exception_handler(AuthError, auth_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)


async def unhandled_error_handler(request, exc: Exception) -> JSONResponse:
    """Render anything nothing else mapped as ``internal_error``, inside CORS.

    Mounted as the handler of a second ``ServerErrorMiddleware`` placed *inside*
    ``CORSMiddleware`` (see :mod:`app.main`). That placement is the whole point
    and it is the reason this is not simply ``add_exception_handler(Exception,
    ...)``: Starlette's own ``ServerErrorMiddleware`` is the outermost layer of
    the stack by construction, so a handler registered on the app renders a 500
    the browser is then not allowed to read.

    ERROR with the traceback, because unlike an ``rbac_denied`` this one is
    always a defect in this application. The response deliberately carries none
    of it: the operator gets the correlation id, and the id is what joins their
    report to the log line that has the detail.
    """
    logger.exception(
        "Unhandled exception on %s", request.url.path,
        extra={"error_code": InternalError.code},
    )
    return _render(InternalError())
