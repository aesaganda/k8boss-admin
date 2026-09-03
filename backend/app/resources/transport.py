"""
One authenticated round trip to the API server, with its response headers.

Separate from :func:`app.resources.catalog.raw_get` for one reason: this one
asks for the *headers*. The generated client discards them by default, and they
are the only place the API server's ``Warning:`` values live — which §1.5
promises to relay verbatim, and which §18 depends on entirely: a preview of a
Pod Security level comes back carrying admission's own list of the pods that
would violate it, and it arrives as a header or not at all.

**In ``resources/`` rather than ``admin/``, which is where it used to live.**
Nothing here is a write. It is the transport both directions use — the funnel's
POST and PATCH, and :func:`app.resources.reader.read_object`'s GET — and
keeping it under the write module meant a read model that wanted one object had
to import from ``app.admin``, dragging the whole write layer into the import
graph of a page that only reads. Dependencies point one way: ``admin`` may use
``resources``, never the reverse.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.k8s.client import get_api_client

logger = logging.getLogger(__name__)

#: One ``Warning:`` header value: ``299 - "the text"``. The capture group is the
#: quoted text, with escaped quotes and backslashes left for the caller to undo.
_WARNING_RE = re.compile(r'\d{3}\s+\S+\s+"((?:[^"\\]|\\.)*)"')


def parse_warnings(headers: Any) -> list[str]:
    """The API server's ``Warning:`` headers, unquoted, in order.

    Returns ``[]`` when there are none — which here genuinely means "the server
    warned about nothing", not "we could not look": the headers are part of the
    same response as the object, so if we have one we have the other.

    Multiple warnings arrive either as repeated headers or as one comma-joined
    value, depending on the HTTP stack; both are handled, and a value that
    matches no warn-header form at all is returned raw rather than dropped —
    an unparseable warning is still a warning.
    """
    if headers is None:
        return []
    try:
        items = headers.items() if hasattr(headers, "items") else list(headers)
    except Exception:  # noqa: BLE001 - a stand-in with an unusual shape
        logger.debug("Could not iterate response headers of type %s", type(headers).__name__)
        return []

    warnings: list[str] = []
    for key, value in items:
        if str(key).lower() != "warning" or not value:
            continue
        matched = _WARNING_RE.findall(str(value))
        if matched:
            warnings.extend(
                text.replace('\\"', '"').replace("\\\\", "\\") for text in matched
            )
        else:
            warnings.append(str(value))
    return warnings


def request_json(
    method: str,
    path: str,
    *,
    query: list[tuple[str, Any]] | None = None,
    body: Any = None,
    content_type: str = "application/json",
) -> tuple[Any, list[str]]:
    """One authenticated write against the cluster, returning ``(body, warnings)``.

    The write-side counterpart of :func:`app.resources.catalog.raw_get`, and
    separate from it for one reason: this one asks for the response *headers*.
    ``_return_http_data_only=False`` makes ``call_api`` return
    ``(data, status, headers)``, which is the only way to reach the ``Warning:``
    values §1.5 promises to relay.

    ``response_type="object"`` matters as much here as it does on the read side:
    with ``None`` the generated client discards the body, and a create would
    return nothing to diff against — which would render as "this write changes
    nothing".
    """
    response = get_api_client().call_api(
        path,
        method,
        query_params=[(key, value) for key, value in (query or []) if value is not None],
        header_params={"Accept": "application/json", "Content-Type": content_type},
        body=body,
        response_type="object",
        auth_settings=["BearerToken"],
        _return_http_data_only=False,
    )
    # A stand-in (or a future client) that hands back only the body still works:
    # the warnings are then unknown, and an empty list is the honest rendering of
    # "this transport does not surface headers", not a claim that none were sent.
    if isinstance(response, tuple) and len(response) == 3:
        data, _status, headers = response
        return data, parse_warnings(headers)
    return response, []



__all__ = ["parse_warnings", "request_json"]
