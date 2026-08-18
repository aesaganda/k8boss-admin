"""
Serialising the audit trail for something that is not this console (§10.3).

The paged UI answers "what happened around the time of the incident". It cannot
answer "hand the quarter's administrative actions to an auditor" or "load this
into the SIEM", because both of those want the whole matching set in one file.

Two formats, and the difference between them is deliberate:

``ndjson``
    One JSON object per line, each identical in shape to a ``GET /api/audit``
    row, **including ``prev_hash`` and ``event_hash``**. Byte-faithful: nothing
    is escaped, reordered or defanged. This is the format to verify a chain
    against, and the format to feed a machine.

``csv``
    The same records flattened for a spreadsheet, and **not byte-faithful** —
    see :func:`_defang` for the one transformation applied and why an export
    that skipped it would be a vulnerability rather than a convenience.

Both stream. There is no row cap and no ``limit`` parameter, because a truncated
extract that looks complete is the export-shaped version of an empty list that
means "we could not look": the person reading the file has no way to notice, and
"nobody scaled that deployment" is exactly the conclusion they would draw.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any, Iterable, Iterator

from app.errors import Invalid
from app.models import AuditRecord

logger = logging.getLogger(__name__)

#: The supported ``?format=`` values.
FORMATS: frozenset[str] = frozenset({"csv", "ndjson"})

#: CSV column order. Flat, because a spreadsheet has no nested cells: the five
#: ``target`` keys become five columns rather than one cell of JSON that an
#: auditor would have to parse by eye.
CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "ts",
    "category",
    "actor",
    "source_ip",
    "cluster_id",
    "cluster_name",
    "verb",
    "target_group",
    "target_version",
    "target_resource",
    "target_namespace",
    "target_name",
    "target_subresource",
    "dry_run",
    "outcome",
    "detail",
    "diff_digest",
    "error",
    "prev_hash",
    "event_hash",
)

#: Leading characters that make a spreadsheet treat a cell as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _defang(value: Any) -> Any:
    """Neutralise a CSV cell a spreadsheet would execute as a formula.

    ``detail`` and ``error`` carry text this console did not write: Kubernetes
    error strings, admission-webhook responses, object names. A cell beginning
    ``=``, ``+``, ``-`` or ``@`` is evaluated as a formula by Excel, LibreOffice
    and Google Sheets when the file is opened, and formulas can reach the network
    and the filesystem. An audit extract is a file that gets mailed to somebody
    and opened without thought, which makes it close to the worst possible
    carrier for one.

    So a leading formula character gets a ``'`` in front of it. That *is* a
    modification of the recorded bytes, which is why it is confined to the CSV
    path, stated here, stated in §10.3, and why ``ndjson`` exists and is the
    format to verify against.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def validate_format(fmt: str | None) -> str:
    """The requested format, or a rejection naming the ones that exist.

    Rejected rather than defaulted. Defaulting would hand somebody who asked for
    ``xlsx`` a CSV named ``.xlsx`` and let them find out when it failed to open —
    or worse, not notice, and treat a defanged spreadsheet as the byte-faithful
    record.
    """
    chosen = (fmt or "ndjson").strip().lower()
    if chosen not in FORMATS:
        raise Invalid(
            f"{fmt!r} is not an audit export format.",
            hint="Use `ndjson` for a byte-faithful record (this is the one to "
                 "verify a hash chain against) or `csv` for a spreadsheet.",
            context={"parameter": "format", "value": str(fmt)},
        )
    return chosen


def _flat_row(row: AuditRecord) -> dict[str, Any]:
    """One record as flat CSV cells, defanged."""
    data = row.to_row_dict()
    target = data.get("target") or {}
    flat = {
        "id": data["id"],
        "ts": data["ts"],
        "category": data["category"],
        "actor": data["actor"],
        "source_ip": data["source_ip"],
        "cluster_id": data["cluster_id"],
        "cluster_name": data["cluster_name"],
        "verb": data["verb"],
        "target_group": target.get("group"),
        "target_version": target.get("version"),
        "target_resource": target.get("resource"),
        "target_namespace": target.get("namespace"),
        "target_name": target.get("name"),
        "target_subresource": target.get("subresource"),
        "dry_run": data["dry_run"],
        "outcome": data["outcome"],
        "detail": data["detail"],
        "diff_digest": data["diff_digest"],
        "error": data["error"],
        "prev_hash": data["prev_hash"],
        "event_hash": data["event_hash"],
    }
    return {key: _defang(value) for key, value in flat.items()}


def to_csv(rows: Iterable[AuditRecord]) -> Iterator[str]:
    """Stream the records as CSV text, header first.

    Written through :mod:`csv` into a reused ``StringIO`` rather than by joining
    strings: quoting, embedded newlines and embedded commas are exactly what a
    Kubernetes error message contains, and hand-rolled CSV gets them wrong in a
    way that shifts every later column by one — a corrupted extract that still
    opens.
    """
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="raise")
    writer.writeheader()
    yield _drain(buffer)
    for row in rows:
        writer.writerow(_flat_row(row))
        yield _drain(buffer)


def _drain(buffer: io.StringIO) -> str:
    text = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return text


def to_ndjson(rows: Iterable[AuditRecord]) -> Iterator[str]:
    """Stream the records as newline-delimited JSON, one row per line.

    The same object ``GET /api/audit`` returns in ``items[]``, so a consumer
    writes one parser for the API and the export. ``sort_keys`` because a diff
    between two extracts of an append-only table should show the appended rows
    and nothing else.
    """
    for row in rows:
        yield json.dumps(
            row.to_row_dict(), sort_keys=True, separators=(",", ":"), default=str
        ) + "\n"


#: ``format`` -> (media type, filename extension). The media types matter: a
#: browser handed ``application/json`` for NDJSON may try to render it, and
#: ``text/csv`` is what makes a spreadsheet the default handler.
MEDIA_TYPES: dict[str, tuple[str, str]] = {
    "csv": ("text/csv; charset=utf-8", "csv"),
    "ndjson": ("application/x-ndjson; charset=utf-8", "ndjson"),
}


def render(fmt: str, rows: Iterable[AuditRecord]) -> Iterator[str]:
    """Dispatch to the serialiser for a validated format."""
    if fmt == "csv":
        return to_csv(rows)
    return to_ndjson(rows)


__all__ = [
    "CSV_COLUMNS",
    "FORMATS",
    "MEDIA_TYPES",
    "render",
    "to_csv",
    "to_ndjson",
    "validate_format",
]
