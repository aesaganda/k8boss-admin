"""
The §1.5 diff: what the operator is about to change, and nothing else.

Rule 3 of the contract says the UI shows a unified diff before the confirming
call. A diff nobody reads is worse than no diff, and the fastest way to make one
unreadable is to show it honestly: a live Deployment carries a
``managedFields`` block that is routinely two thirds of the object, a
``resourceVersion`` that changes on every write anywhere in the cluster, a
``generation`` the API server increments itself, and a ``status`` block the
controller owns. Diff two of those verbatim and a one-line replica change
arrives inside two hundred lines of server bookkeeping — which trains operators
to scroll to the bottom and click Confirm, which is the same as having no diff.

So both sides are normalised first, and the normalisation is deliberately
conservative: it removes only fields **no operator ever chooses**, and it does it
to both sides, so nothing that is genuinely changing can be hidden by it.

* ``metadata.managedFields`` — server-side-apply bookkeeping. Never authored.
* ``metadata.resourceVersion``, ``metadata.generation`` — assigned by the API
  server on every write. They differ between live and projection on *every*
  mutation, so keeping them would mean every diff is non-empty and
  ``diff.changed`` would never be false.
* ``metadata.uid``, ``metadata.creationTimestamp``, ``metadata.selfLink`` —
  identity and history the API server assigns. A submitted manifest does not
  carry them, so keeping them would render every hand-edited YAML as deleting
  the object's identity.
* the ``kubectl.kubernetes.io/last-applied-configuration`` annotation — a whole
  serialised copy of the object, on one line, inside the object. Kept, it buries
  a two-line change under a 3 KB line that no diff viewer can wrap usefully.
  This one *can* genuinely change on a replace, and dropping it is a deliberate
  trade: the operator loses sight of kubectl's bookkeeping and gains sight of
  their own edit.
* ``status`` — but only when two objects are being compared and one of them
  lacks it. Both sides having a status means both came from the API server and
  any difference between them is real; one side lacking it means the other side
  is a hand-written manifest, and showing "your edit deletes the entire status
  block" would be a claim about an outcome that is not going to happen. When one
  side is ``None`` — a create or a delete — the status stays, because §4 fixes a
  delete's ``diff.before`` as *the live object* and the whole of it is what
  disappears.

What is deliberately *not* normalised: labels, annotations (other than the one
above), spec in any form, and anything under a CRD's own keys. If it is in the
diff, it is because the operator's request put it there.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: kubectl's serialised copy of the object, stored inside the object's own
#: annotations. Same constant as :mod:`app.resources.reader`, restated rather
#: than imported: this module performs no reads and importing the reader for one
#: string would tie the diff to the read path's import graph.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"

#: ``metadata`` keys the API server owns. Removed from both sides unconditionally.
SERVER_METADATA_FIELDS: tuple[str, ...] = (
    "managedFields",
    "resourceVersion",
    "generation",
    "uid",
    "creationTimestamp",
    "selfLink",
)

#: Labels for the two sides of the unified diff. Fixed strings, and the same on
#: a create (where the ``live`` side is empty) and a delete (where the
#: ``proposed`` side is): the UI colours and folds hunks by these headers, so a
#: header that changed with the verb would need special-casing in the viewer for
#: no benefit to the reader.
FROM_LABEL = "live"
TO_LABEL = "proposed"


def _strip_metadata(obj: dict[str, Any]) -> None:
    """Remove the API server's own metadata bookkeeping, in place on a copy."""
    metadata = obj.get("metadata")
    if not isinstance(metadata, dict):
        return
    for field in SERVER_METADATA_FIELDS:
        metadata.pop(field, None)

    annotations = metadata.get("annotations")
    if isinstance(annotations, dict):
        annotations.pop(LAST_APPLIED_ANNOTATION, None)
        if not annotations:
            # Removed entirely rather than left as ``{}``. An object whose only
            # annotation was kubectl's copy would otherwise show an
            # ``annotations: {}`` line on one side and nothing on the other — a
            # phantom hunk describing a change that is not being made.
            metadata.pop("annotations", None)

    if not metadata:
        obj.pop("metadata", None)


def _normalise(
    obj: dict[str, Any] | None, *, drop_status: bool
) -> dict[str, Any] | None:
    """A deep copy of ``obj`` with server bookkeeping removed.

    Deep copy, not in-place: ``before`` is the live object the caller also uses
    to build the response's ``resourceVersion`` and, on a conflict, to re-read
    against. Mutating it here would strip the resourceVersion out of the very
    object the optimistic-concurrency check needs.
    """
    if obj is None:
        return None
    if not isinstance(obj, dict):
        # A non-object body (a Status from a delete, a string from a proxy).
        # Returned as-is so it renders in the diff rather than raising: the diff
        # is a display of what happened, and refusing to display something
        # unexpected hides the one case worth looking at.
        return obj
    clone = copy.deepcopy(obj)
    _strip_metadata(clone)
    if drop_status:
        clone.pop("status", None)
    return clone


def _to_yaml(obj: Any) -> str:
    """Render one side of the diff.

    ``None`` renders as the empty string, never as ``null``: the ``after`` side
    of a delete is ``None`` (§4), and a diff against the literal text ``null``
    would show one line being *added* where the object is being removed.

    ``sort_keys=False`` keeps the API server's field order — alphabetical order
    puts ``status`` above ``spec`` and buries ``kind`` in the middle, which makes
    a diff materially harder to read. ``width`` is very wide so a long image
    reference is not wrapped: wrapped YAML still parses, but the wrap point moves
    when an unrelated field changes length, and the diff then shows lines that
    nobody edited.
    """
    if obj is None:
        return ""
    if not isinstance(obj, (dict, list)):
        return str(obj)
    return yaml.safe_dump(
        obj, sort_keys=False, default_flow_style=False, allow_unicode=True, width=4096,
    )


def build_diff(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """The §1.5 ``diff`` block: ``{before, after, unified, changed}``.

    ``before`` is the live object (``None`` on a create); ``after`` is the API
    server's own projection of the write (``None`` on a delete). Both are
    rendered as YAML after normalisation, so ``diff.before`` and ``diff.after``
    are exactly the text the ``unified`` hunks refer to — a UI that shows the two
    sides side by side and the unified diff below them cannot show three
    inconsistent things.

    ``changed`` is computed from the normalised text, which is what makes
    ``changed: false`` meaningful: it says *this write is a no-op*, so the UI
    offers "nothing would change" instead of a confirm button (§1.5). Comparing
    the raw objects instead would make ``changed`` true on every mutation,
    because the API server bumps ``resourceVersion`` on all of them.

    A delete — ``after=None`` with a live ``before`` — is always ``changed:
    true``, which §4 requires: the confirm dialog has to show what disappears.
    """
    # Status is dropped only when two objects are being compared and one of them
    # lacks it. See the module docstring: an asymmetric status means one side is
    # a hand-written manifest, and diffing it against a live controller's status
    # would show a deletion that is not going to happen. A create or a delete has
    # no second side to be asymmetric with, and §4 fixes a delete's `before` as
    # the live object entire.
    before_has_status = isinstance(before, dict) and "status" in before
    after_has_status = isinstance(after, dict) and "status" in after
    comparing_two = before is not None and after is not None
    drop_status = comparing_two and not (before_has_status and after_has_status)

    before_text = _to_yaml(_normalise(before, drop_status=drop_status))
    after_text = _to_yaml(_normalise(after, drop_status=drop_status))

    unified = "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=FROM_LABEL,
            tofile=TO_LABEL,
            # No timestamps in the headers: they would make the digest of an
            # otherwise identical diff differ between the dry run and the
            # confirming call, and that digest is the whole proof that what was
            # confirmed is what was applied.
            lineterm="\n",
        )
    )
    # unified_diff omits the trailing newline marker that `diff` emits; a final
    # line without one renders as if it ran into the next hunk. Normalised here
    # so the digest is stable regardless of how the API server terminated its
    # YAML.
    if unified and not unified.endswith("\n"):
        unified += "\n"

    return {
        "before": before_text,
        "after": after_text,
        "unified": unified,
        "changed": before_text != after_text,
    }


def digest(diff: dict[str, Any]) -> str:
    """``sha256:...`` over the unified diff, for the audit record (§10).

    The diff itself is deliberately never stored: it can contain Secret data and
    ConfigMap payloads, and the audit table is a much less carefully guarded
    place than the cluster those values came from. The digest keeps the one
    property that matters after the fact — that the change an operator confirmed
    in the dry run is byte-for-byte the change that was applied — without keeping
    the bytes.

    Computed over ``unified`` rather than over the two sides, so a diff of an
    unchanged object has a stable, recognisable digest (the digest of the empty
    string) rather than one that varies with the object it did not change.
    """
    unified = diff.get("unified") if isinstance(diff, dict) else None
    payload = (unified or "").encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "FROM_LABEL",
    "LAST_APPLIED_ANNOTATION",
    "SERVER_METADATA_FIELDS",
    "TO_LABEL",
    "build_diff",
    "digest",
]
