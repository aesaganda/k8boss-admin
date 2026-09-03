"""
§18 — set a namespace's Pod Security Admission level, with the dry run naming
the pods that would violate it.

**What this is, and what it deliberately is not.** One merge patch on one
Namespace's labels, through the ordinary funnel: gate, preflight on
``patch namespaces``, ``dryRun=All``, the API server's own diff, a confirmation,
an audit row. §4's YAML editor could write the same six labels today. What it
could not do is answer the question an operator actually has before flipping the
level — *what breaks* — and that answer is the reason this endpoint exists.

**The preview is the feature.** Pod Security admission evaluates the pods
already running in a namespace when the namespace's labels change, and returns
what it finds as ``Warning:`` headers on the response — on a ``dryRun=All``
update exactly as on a real one. So a preview of "enforce restricted" comes back
carrying the API server's own list of the pods in that namespace that do not
meet it, by name and by the field that fails. That is not something this console
computes, and it is deliberately not something this console parses: the
warnings are passed through verbatim, as §1.5 requires of every write, because
a summary of somebody else's admission decision is a summary that can be wrong
about which pods are affected.

**The honest limit, stated in the response and in the dialog.** Raising the
enforce level does not evict, restart or otherwise touch a pod that is already
running. Admission runs when a pod is *created*. A Deployment whose template
violates the new level keeps its existing pods until something replaces them,
and only then stops being able to make more — at which point it sits at 0 of N
with a ``FailedCreate`` event and no pod to look at. An operator who reads
"enforce: restricted" as "this namespace is now restricted" is wrong about the
workloads in front of them, which is exactly the confident-wrong-answer this
project treats as a defect. So it is a consequence they acknowledge by name.

This is §17's neighbour rather than part of it. ADR-0006 refuses to *adopt* an
existing namespace — to create objects into one that is somebody else's — and
that refusal is untouched here: nothing is created, one existing object's labels
are edited, with one diff and one confirmation, the way §4 edits anything else.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.apply import MERGE_PATCH, patch_fn
from app.admin.mutate import FeatureGate, mutate, read_only_switch
from app.errors import Conflict, Invalid
from app.resources import reader
from app.resources.shaping import (
    POD_SECURITY_LEVELS,
    POD_SECURITY_MODES,
    POD_SECURITY_PREFIX,
    get_field,
    pod_security_row,
)

logger = logging.getLogger(__name__)

#: Ordered weakest to strongest. The index is what makes "raised" and "lowered"
#: answerable; a level outside this tuple is neither, and says so.
_LEVEL_ORDER = POD_SECURITY_LEVELS

#: The consequence codes, as constants because the frontend branches on them and
#: a typo in a string literal is a checkbox that never appears.
WARN_DOES_NOT_EVICT = "psa_does_not_evict"
WARN_ENFORCEMENT_REMOVED = "psa_enforcement_removed"
WARN_LOWERED = "psa_lowered"
WARN_NO_WARN_LABEL = "psa_no_warn_label"
WARN_VERSION_PINNED = "psa_version_pinned"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """One switch. Editing a label is not a larger commitment than §4 already is.

    The dry run is not withheld, and withholding it would defeat the point: the
    projection is what carries the admission warnings, and those are the reason
    an operator can decide whether to open the switch at all.
    """
    return FeatureGate(
        feature="setting a Pod Security level",
        message="Setting a Pod Security level is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The preview is still available, and "
                "with it the API server's own warnings about the pods that would "
                "violate the level: reading what would change is a read."
            )),
        ),
        enabled_detail="This deployment permits setting a Pod Security level.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §18's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def _level(raw: Any, *, mode: str) -> str | None:
    """One mode's level: a known level, or ``None`` meaning "remove the label".

    ``None`` and the string ``"privileged"`` are different requests and the
    difference is the whole of §18's honesty problem. ``privileged`` declares
    that this namespace admits everything. ``None`` removes the declaration, and
    what applies then is the cluster's Pod Security default — which lives in the
    API server's ``AdmissionConfiguration`` file, which no API serves. The
    console cannot tell an operator what removing the label will mean, so it
    must not let them express it by accident.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text not in POD_SECURITY_LEVELS:
        raise _invalid(
            f"{text!r} is not a Pod Security level.",
            parameter=f"podSecurity.{mode}",
            hint=f"Use one of {', '.join(POD_SECURITY_LEVELS)}, or null to remove the label.",
        )
    return text


def _version(raw: Any, *, mode: str, level: str | None) -> str | None:
    """One mode's version label. ``latest``, or ``v1.NN``, or absent."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text != "latest" and not _looks_like_minor(text):
        raise _invalid(
            f"{text!r} is not a Pod Security version.",
            parameter=f"podSecurity.{mode}Version",
            hint="Use `latest` or a minor version such as `v1.31`.",
        )
    if level is None:
        raise _invalid(
            f"`podSecurity.{mode}Version` is set but `podSecurity.{mode}` is not.",
            parameter=f"podSecurity.{mode}Version",
            hint=(
                "A version label without its level label is ignored by admission, "
                "so this would leave the namespace with a label that does nothing."
            ),
        )
    return text


def _looks_like_minor(text: str) -> bool:
    if not text.startswith("v"):
        return False
    parts = text[1:].split(".")
    return len(parts) == 2 and all(part.isdigit() for part in parts)


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """The §18 request: three modes, their versions, and the version being edited.

    ``podSecurity`` must be present and must name every mode explicitly, absent
    meaning "remove this label". A request that omitted a mode would have to
    mean either "leave it alone" or "remove it", and a body whose meaning
    depends on which one this module picked is a body that eventually removes
    somebody's audit label because a form field was left blank.
    """
    if not isinstance(payload, dict):
        raise _invalid("The request body must be an object.", parameter="body")

    raw = payload.get("podSecurity")
    if raw is None:
        raise _invalid(
            "`podSecurity` is required.",
            parameter="podSecurity",
            hint=(
                "Name every mode you want the namespace to end up with; a mode "
                "you leave out is removed, so send its current value to keep it."
            ),
        )
    if not isinstance(raw, dict):
        raise _invalid("`podSecurity` must be an object.", parameter="podSecurity")

    unknown = sorted(
        key for key in raw
        if key not in POD_SECURITY_MODES
        and key not in {mode + "Version" for mode in POD_SECURITY_MODES}
    )
    if unknown:
        raise _invalid(
            f"`podSecurity` has no mode called {unknown[0]!r}.",
            parameter=f"podSecurity.{unknown[0]}",
            hint=f"The modes are {', '.join(POD_SECURITY_MODES)}.",
        )

    requested: dict[str, str | None] = {}
    for mode in POD_SECURITY_MODES:
        level = _level(raw.get(mode), mode=mode)
        requested[mode] = level
        requested[mode + "Version"] = _version(
            raw.get(mode + "Version"), mode=mode, level=level
        )

    version = payload.get("resourceVersion")
    version = str(version).strip() if version is not None and str(version).strip() else None
    return {"podSecurity": requested, "resourceVersion": version}


# --------------------------------------------------------------------------- #
# The patch
# --------------------------------------------------------------------------- #

def build_patch(requested: dict[str, str | None], *, resource_version: str | None) -> dict[str, Any]:
    """The merge patch: every one of the six labels, set or explicitly removed.

    All six are named on every write, including the ones that are not changing.
    A merge patch that carried only the changed keys would be shorter and would
    make the diff the operator confirms depend on what this module decided was
    different, rather than on what they asked for.

    ``None`` is JSON ``null``, which is how a merge patch **removes** a key.
    That is the only way to take a label off, and it is why the patch content
    type here is a merge patch rather than the strategic merge §6 uses.

    ``metadata.resourceVersion`` rides along when the caller sent one, which is
    rule 4 enforced by the API server: a patch carrying a stale version is
    rejected with a 409 rather than silently overwriting whoever changed the
    labels while this operator was reading them.
    """
    labels: dict[str, str | None] = {}
    for mode in POD_SECURITY_MODES:
        labels[POD_SECURITY_PREFIX + mode] = requested[mode]
        labels[POD_SECURITY_PREFIX + mode + "-version"] = requested[mode + "Version"]

    metadata: dict[str, Any] = {"labels": labels}
    if resource_version:
        metadata["resourceVersion"] = resource_version
    return {"metadata": metadata}


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _rank(level: str | None) -> int | None:
    """Where a level sits, or ``None`` for absent or unrecognised.

    ``None`` for an unrecognised level rather than a guess: a namespace labelled
    with something outside the three has whatever admission makes of it, and
    calling that "weaker" or "stronger" than the level being set would be this
    console inventing an ordering the API server does not use.
    """
    if level is None or level not in _LEVEL_ORDER:
        return None
    return _LEVEL_ORDER.index(level)


def consequences_for(
    current: dict[str, Any], requested: dict[str, str | None]
) -> list[dict[str, Any]]:
    """What changing these labels means, each as a code the caller names back.

    Every entry describes something that goes wrong **silently** otherwise,
    which is the bar for putting a checkbox in front of somebody. Nothing here
    needs the cluster: it is the labels the namespace has beside the labels the
    request asks for.
    """
    out: list[dict[str, Any]] = []
    was, now = current.get("enforce"), requested["enforce"]
    was_rank, now_rank = _rank(was), _rank(now)

    if now is not None and now != was:
        out.append({
            "code": WARN_DOES_NOT_EVICT,
            "label": "Pods already running are not affected by this",
            "consequence": (
                "Pod Security admission runs when a pod is created, so nothing "
                f"in this namespace stops, restarts or is evicted by enforcing "
                f"{now}. A workload whose pods violate it keeps the pods it has "
                "and fails to make new ones — the Deployment stays Available "
                "until something replaces a pod, and then sits below its replica "
                "count with a FailedCreate event and no pod to inspect."
            ),
            "mitigation": (
                "Read the admission warnings on the preview: they name the pods "
                "that violate the level today. Fix or redeploy those workloads, "
                "then set the level."
            ),
        })

    if was is not None and now is None:
        out.append({
            "code": WARN_ENFORCEMENT_REMOVED,
            "label": "The enforce label is removed, not set to privileged",
            "consequence": (
                f"This namespace enforces {was} today. Removing the label does "
                "not mean everything is admitted: it means the cluster's own Pod "
                "Security default applies, and that default is read from a file "
                "on the API server which no API serves. This console cannot tell "
                "you what this namespace will enforce afterwards."
            ),
            "mitigation": (
                "Set privileged explicitly if that is what you mean — it is a "
                "declaration the next person can read, and this is not."
            ),
        })

    if was_rank is not None and now_rank is not None and now_rank < was_rank:
        out.append({
            "code": WARN_LOWERED,
            "label": "Pods this namespace refuses today will be admitted",
            "consequence": (
                f"The enforce level goes from {was} down to {now}. Every pod that "
                f"{was} refuses and {now} allows becomes deployable here by "
                "anyone who can create a pod in this namespace, and nothing "
                "records that the level was ever higher except this console's "
                "audit trail."
            ),
            "mitigation": (
                f"Set audit and warn to {was} so the workloads that would have "
                "been refused are still reported."
            ),
        })

    if now in ("baseline", "restricted") and requested["warn"] != now:
        out.append({
            "code": WARN_NO_WARN_LABEL,
            "label": "Violations will be refused without being reported",
            "consequence": (
                f"With enforce at {now} and warn set to "
                f"{requested['warn'] or 'nothing'}, a workload that violates the "
                "level is refused at pod creation and nobody is told at the "
                "moment they apply it. The warn label is what puts the reason in "
                "front of whoever runs kubectl apply, before the ReplicaSet "
                "starts failing."
            ),
            "mitigation": f"Set warn to {now} as well, which is what `oc` does.",
        })

    pinned = sorted(
        mode for mode in POD_SECURITY_MODES
        if requested[mode] is not None
        and requested[mode + "Version"] not in (None, "latest")
    )
    if pinned:
        out.append({
            "code": WARN_VERSION_PINNED,
            "label": "This policy stops tightening as the cluster is upgraded",
            "consequence": (
                "A pinned version freezes the rules of that level as they were "
                f"in that Kubernetes minor ({', '.join(pinned)}). When the "
                "cluster is upgraded and the level gains a new check, this "
                "namespace does not get it, and nothing reports the gap."
            ),
            "mitigation": (
                "Use `latest` unless you are staging a known-breaking upgrade, "
                "and put a date on removing the pin."
            ),
        })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed here from the request being written, never trusted from the plan:
    a caller that could acknowledge a code the server did not derive could
    acknowledge every code it liked.
    """
    if not consequences:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in consequences if entry["code"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This change has consequences that have not been acknowledged.",
        detail="; ".join(f"{entry['code']}: {entry['label']}" for entry in missing),
        hint=(
            "Re-send with acknowledgeConsequences naming each of "
            + ", ".join(entry["code"] for entry in missing)
            + "."
        ),
        context={
            "parameter": "acknowledgeConsequences",
            "unacknowledged": [entry["code"] for entry in missing],
        },
    )


# --------------------------------------------------------------------------- #
# The read the plan and the write share
# --------------------------------------------------------------------------- #

def _read_namespace(name: str) -> dict[str, Any]:
    """The namespace as it is now. Never ``None``: a read that failed propagates.

    Both the plan and the write need the labels the namespace has, to say what
    is changing. A read that did not answer is not "it has no labels" — that
    would render as a diff removing labels this console never saw, and an
    operator confirming it would strip somebody's audit level.
    """
    return reader.get_resource("", "v1", "namespaces", name, namespace=None)


def _live_state(name: str) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    """``(live object, its pod security row, its resourceVersion)``."""
    live = _read_namespace(name)
    labels = get_field(live, "metadata", "labels", default={}) or {}
    version = get_field(live, "metadata", "resourceVersion")
    return live, pod_security_row(labels), (str(version) if version else None)


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/projects/{name}/pod-security/plan`` — what this change means.

    Ungated and unaudited: one namespace read and some arithmetic on labels.
    It does **not** dry-run the patch, because a dry run is a write request the
    caller has not asked for yet and it needs the preflight the funnel does. The
    admission warnings — the part an operator actually waits for — arrive with
    the dry run, which is the next step and goes through :func:`set_level`.
    """
    request = validate_request(payload)
    _live, current, resource_version = _live_state(name)
    requested = request["podSecurity"]
    return {
        "namespace": name,
        "current": current,
        "requested": requested,
        "resourceVersion": resource_version,
        "changed": any(
            current.get(key) != requested[key] for key in requested
        ),
        "consequences": consequences_for(current, requested),
        "gate": enabled_state(),
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def set_level(
    name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/projects/{name}/pod-security`` — one merge patch, through the funnel.

    Order of refusals, each before the cluster is changed: request validation,
    the namespace read — which must answer, because a change described against
    labels we could not read is a diff about nothing — the concurrency check,
    and the acknowledgement check, recomputed against the namespace as it is
    now rather than as the plan saw it. Then :func:`mutate`, which gates,
    preflights ``patch namespaces``, sends the patch with ``dryRun=All`` when
    this is a preview, diffs live against the API server's projection, and
    audits the outcome.

    The §1.5 response gains two keys:

    * ``consequences`` — what was acknowledged, echoed so the record of the
      confirmation is in the response the operator's client kept.
    * ``admissionWarnings`` — the API server's ``Warning:`` headers, verbatim
      and separate from §1.5's ``warnings`` for nobody's benefit but clarity:
      they are the same list. On a preview of a level this namespace's pods do
      not meet, this is where Pod Security admission names them.

    **Nothing here evicts anything**, and the response says so in the
    consequences rather than leaving an operator to infer it from a green
    ``applied: true``.
    """
    request = validate_request(payload)
    requested = request["podSecurity"]

    live, current, live_version = _live_state(name)
    sent_version = request["resourceVersion"]
    if sent_version and live_version and sent_version != live_version:
        raise Conflict(
            f"The namespace {name} changed while you were reading it.",
            detail=(
                f"You are editing version {sent_version}; the cluster has "
                f"{live_version}."
            ),
            hint="Reload the namespace and preview again against what it says now.",
            context={
                "group": "", "version": "v1", "resource": "namespaces",
                "namespace": None, "name": name, "verb": "patch",
                "currentResourceVersion": live_version,
                "currentPodSecurity": current,
            },
        )

    consequences = consequences_for(current, requested)
    _require_acknowledgement(consequences, acknowledge_consequences)

    body = build_patch(requested, resource_version=sent_version or live_version)
    result = mutate(
        verb="patch",
        group="",
        version="v1",
        plural="namespaces",
        namespace=None,
        name=name,
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=patch_fn(
            "", "v1", "namespaces", name, body, content_type=MERGE_PATCH,
        ),
        before=live,
        # The audit sentence names the levels, not the fact that labels changed:
        # the question after an incident is what this namespace was enforcing
        # before somebody deployed into it, and the diff digest alone cannot
        # answer that without the object it was taken over.
        detail=(
            f"pod security {name}: enforce "
            f"{current.get('enforce') or 'unset'} -> {requested['enforce'] or 'unset'}"
        ),
    )
    result["consequences"] = consequences
    result["admissionWarnings"] = list(result.get("warnings") or [])
    result["current"] = current
    result["requested"] = requested
    return result


__all__ = [
    "build_patch",
    "consequences_for",
    "enabled_state",
    "plan",
    "set_level",
    "validate_request",
]
