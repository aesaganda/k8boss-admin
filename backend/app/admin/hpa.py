"""
§21 — set a HorizontalPodAutoscaler's replica bounds, and say whether it is
scaling at all.

**The action this exists for happens during an incident.** A service is at its
ceiling, the queue is growing, and somebody raises `maxReplicas` from 10 to 30.
§4's YAML editor can already write those two integers. What it cannot do is any
of the three things that decide whether the edit does anything:

* **It cannot tell you the autoscaler is inert.** An HPA whose `ScalingActive`
  condition is false is not scaling — usually because the metric it needs cannot
  be read, which is what happens when `metrics.k8s.io` goes away. It looks
  entirely normal in `kubectl get hpa`: the TARGETS column shows `<unknown>` and
  everything else is populated. Raising the ceiling on one of those changes a
  number in etcd and nothing else, and the operator goes back to watching a
  service that will never scale.
* **It cannot tell you the change takes effect now.** Lowering `maxReplicas`
  below the current replica count does not take effect at the next scale
  decision — the HPA clamps immediately, and pods are terminated. That is a
  different act from raising a ceiling, and the form field looks identical.
* **It cannot say what the current count even is**, so it cannot say which of
  those two an operator is about to do.

**What `applied: true` means here.** The HPA's bounds are what this write
changes, and that is all it attests. The replica count follows when the
controller next runs a scale decision — *if* it can, which is exactly what
`ScalingActive` answers and why a false one is a consequence rather than a note.

Pinned to `autoscaling/v2`, like §8's listing: `v1` carries no `metrics` field at
all, so the current-versus-target reading this module reports would be absent on
every HPA. A cluster serving only `v1` is pre-1.23 and gets §1.2's `unsupported`
from discovery, which renders as "not present on this cluster" rather than red.

The gate is `ADMIN_ALLOW_MUTATIONS` alone, like §17, §18 and §20. Editing two
integers on an object the console already lists is not the larger commitment
§5.5's host-mounted pod is.
"""

from __future__ import annotations

import logging
from typing import Any

from app.admin.apply import MERGE_PATCH, patch_fn
from app.admin.mutate import FeatureGate, audit_conflict, mutate, read_only_switch
from app.errors import Conflict, Invalid
from app.resources import reader
from app.resources.shaping import get_field, hpa_row

logger = logging.getLogger(__name__)

#: `v1` has no `metrics`, so every current-versus-target reading would be blank.
#: See the module docstring on what a `v1`-only cluster gets instead.
GROUP, VERSION, PLURAL = "autoscaling", "v2", "horizontalpodautoscalers"

#: Consequence codes. Constants because the frontend branches on them, and a
#: typo in a string literal is a checkbox that never renders and a confirmation
#: nobody can give.
WARN_NOT_SCALING = "hpa_not_scaling"
WARN_MAX_BELOW_CURRENT = "hpa_max_below_current"
WARN_MIN_ABOVE_CURRENT = "hpa_min_above_current"
WARN_REPLICAS_UNKNOWN = "hpa_replicas_unknown"
WARN_SCALE_TO_ZERO_GATED = "hpa_scale_to_zero_gated"
WARN_CANNOT_REACH_TARGET = "hpa_cannot_reach_target"


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _gate() -> FeatureGate:
    """One switch, and the dry run is not withheld.

    On a read-only console the plan is still the most useful thing here: it
    reports whether the autoscaler is scaling, which is a read, and which is the
    fact an operator is usually looking for before they ask for the write.
    """
    return FeatureGate(
        feature="setting autoscaler bounds",
        message="Setting autoscaler bounds is disabled on this console.",
        hint="Set ADMIN_ALLOW_MUTATIONS=true on the console deployment to allow it.",
        switches=(
            read_only_switch(detail=(
                "This console runs read-only (ADMIN_ALLOW_MUTATIONS is off), so it "
                "writes nothing to a cluster. The plan is still available — the "
                "bounds, the current replica count and whether this autoscaler is "
                "scaling at all are reads."
            )),
        ),
        enabled_detail="This deployment permits setting autoscaler bounds.",
    )


def enabled_state() -> dict[str, Any]:
    """The ``gate`` object §21's plan returns, so the UI disables with the reason."""
    return _gate().state()


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _invalid(message: str, *, parameter: str, hint: str | None = None, **context: Any) -> Invalid:
    return Invalid(message, hint=hint, context={"parameter": parameter, **context})


def _bound(payload: dict[str, Any], key: str) -> int:
    """One bound, required and an integer.

    **Both bounds are named on every request**, including the one that is not
    changing, for §18's reason: a body whose omitted field could mean "leave it
    alone" or "reset it to the default" is a body that eventually resets
    somebody's `minReplicas` to 1 because a form field was left blank — during
    the incident where they were raising the ceiling.
    """
    if key not in payload:
        raise _invalid(
            f"{key} is required.",
            parameter=key,
            hint=(
                "Both bounds are sent on every request, including the one that is "
                "not changing: a bound left out would be ambiguous between "
                "'leave it' and 'reset it'."
            ),
        )
    value = payload[key]
    # bool is an int in Python and `True` would arrive as 1 replica.
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{key} must be an integer.", parameter=key, value=value)
    return value


def validate_request(payload: dict[str, Any]) -> dict[str, Any]:
    """``{"minReplicas": 2, "maxReplicas": 30, "resourceVersion": "…"}``."""
    if not isinstance(payload, dict):
        raise _invalid("The request body must be an object.", parameter="body")

    minimum = _bound(payload, "minReplicas")
    maximum = _bound(payload, "maxReplicas")

    if maximum < 1:
        raise _invalid(
            "maxReplicas must be at least 1.",
            parameter="maxReplicas",
            hint=(
                "An autoscaler with a ceiling of zero is not a stopped workload, "
                "it is a rejected object. Scale the workload to zero, or delete "
                "the autoscaler, depending on which you mean."
            ),
            value=maximum,
        )
    if minimum < 0:
        raise _invalid(
            "minReplicas cannot be negative.", parameter="minReplicas", value=minimum,
        )
    if minimum > maximum:
        raise _invalid(
            f"minReplicas ({minimum}) cannot exceed maxReplicas ({maximum}).",
            parameter="minReplicas",
            hint="Raise the ceiling first, or lower the floor.",
            minReplicas=minimum, maxReplicas=maximum,
        )

    resource_version = payload.get("resourceVersion")
    if resource_version is not None and not isinstance(resource_version, str):
        raise _invalid("resourceVersion must be a string.", parameter="resourceVersion")

    unknown = set(payload) - {"minReplicas", "maxReplicas", "resourceVersion"}
    if unknown:
        raise _invalid(
            "Unknown fields in the request body: " + ", ".join(sorted(unknown)),
            parameter="body",
        )

    return {
        "minReplicas": minimum,
        "maxReplicas": maximum,
        "resourceVersion": resource_version,
    }


# --------------------------------------------------------------------------- #
# The refusals arithmetic settles
# --------------------------------------------------------------------------- #

def check_changed(current: dict[str, Any], requested: dict[str, Any]) -> None:
    """Refuse a request that changes neither bound, naming what they are.

    A no-op is worth refusing rather than sending: the diff would be empty, the
    audit row would record a write that changed nothing, and an operator who
    mistyped a field would read a green result as the ceiling having been
    raised.
    """
    if (
        current["min_replicas"] == requested["minReplicas"]
        and current["max_replicas"] == requested["maxReplicas"]
    ):
        raise _invalid(
            f"This autoscaler already runs between {requested['minReplicas']} and "
            f"{requested['maxReplicas']} replicas.",
            parameter="maxReplicas",
            hint="Change one of the bounds, or nothing needs to happen.",
            minReplicas=current["min_replicas"], maxReplicas=current["max_replicas"],
        )


# --------------------------------------------------------------------------- #
# Consequences
# --------------------------------------------------------------------------- #

def _reason(current: dict[str, Any], condition: str) -> str:
    """A condition's reason and message, as one clause, or a plain fallback."""
    entry = (current.get("conditions") or {}).get(condition)
    if not entry:
        return "the controller has not said why"
    parts = [part for part in (entry.get("reason"), entry.get("message")) if part]
    return ": ".join(parts) if parts else "the controller has not said why"


def consequences_for(current: dict[str, Any], requested: dict[str, Any]) -> list[dict[str, Any]]:
    """What changing these bounds means, each as a code the caller names back.

    Nothing here needs a second read: it is the HPA's own status beside the two
    integers being requested.
    """
    out: list[dict[str, Any]] = []
    running = current["current_replicas"]
    minimum, maximum = requested["minReplicas"], requested["maxReplicas"]

    if current["scaling_active"] is False:
        out.append({
            "code": WARN_NOT_SCALING,
            "label": "This autoscaler is not scaling anything right now",
            "consequence": (
                "Its ScalingActive condition is false, so the controller cannot "
                f"compute a desired replica count ({_reason(current, 'ScalingActive')}). "
                "Changing the bounds is still a real write and the new numbers will "
                "be stored — but nothing scales until the controller can read its "
                "metrics again, and the workload stays at whatever count it has."
            ),
            "mitigation": (
                "Check that the metrics API this autoscaler needs is answering — "
                "the cluster status page lists aggregated APIServices and says "
                "which are unavailable. Scale the workload directly if it needs "
                "capacity before that is fixed."
            ),
        })

    if current["able_to_scale"] is False:
        out.append({
            "code": WARN_CANNOT_REACH_TARGET,
            "label": "This autoscaler cannot reach the workload it targets",
            "consequence": (
                "Its AbleToScale condition is false "
                f"({_reason(current, 'AbleToScale')}), which usually means the "
                "object named in scaleTargetRef does not exist or has no /scale "
                "subresource. New bounds do not fix that."
            ),
            "mitigation": (
                "Check that the target still exists under the name and kind the "
                "autoscaler names."
            ),
        })

    if running is None:
        out.append({
            "code": WARN_REPLICAS_UNKNOWN,
            "label": "Whether this takes effect immediately is unknown",
            "consequence": (
                "This autoscaler's status carries no current replica count, so "
                "this console cannot tell you whether the bounds you are setting "
                "are above or below what is running. A ceiling below the current "
                "count is applied at once, and that is a number nobody here has."
            ),
            "mitigation": (
                "Read the workload's own replica count before confirming if the "
                "new ceiling might be below it."
            ),
        })
    else:
        if maximum < running:
            out.append({
                "code": WARN_MAX_BELOW_CURRENT,
                "label": f"{running - maximum} pod(s) are terminated as soon as this is written",
                "consequence": (
                    f"{running} replicas are running and the new ceiling is "
                    f"{maximum}. An HPA clamps to its bounds at its next scale "
                    "decision, which is seconds away — this is not a limit that "
                    "applies to future growth, it is a scale-down now."
                ),
                "mitigation": (
                    "If you mean to cap future growth without shedding what is "
                    f"running, set maxReplicas to {running} or above."
                ),
            })
        if minimum > running:
            out.append({
                "code": WARN_MIN_ABOVE_CURRENT,
                "label": f"{minimum - running} pod(s) are started as soon as this is written",
                "consequence": (
                    f"{running} replicas are running and the new floor is "
                    f"{minimum}. The HPA scales up to the floor regardless of what "
                    "the metrics say, so this is capacity claimed now and held "
                    "until the floor is lowered again."
                ),
                "mitigation": (
                    "Check the cluster has room for them — a floor above what can "
                    "be scheduled leaves pods Pending rather than scaling."
                ),
            })

    if minimum == 0:
        out.append({
            "code": WARN_SCALE_TO_ZERO_GATED,
            "label": "A floor of zero needs a feature gate this console cannot read",
            "consequence": (
                "minReplicas: 0 is only accepted when the API server runs with the "
                "HPAScaleToZero feature gate enabled. No API reports whether it is, "
                "so this console cannot tell you in advance — the API server will "
                "either accept the write or refuse it as invalid."
            ),
            "mitigation": (
                "Preview first. A dry run goes through the same validation as the "
                "real write, so a refusal shows up there without changing anything."
            ),
        })

    return out


def _require_acknowledgement(
    consequences: list[dict[str, Any]], acknowledged: list[str] | None
) -> None:
    """Refuse a write whose consequences the caller has not accepted by name.

    Recomputed from the autoscaler as it is now, never trusted from the plan: the
    replica count moves on its own between the two, so the plan's "this
    terminates two pods" can be a different number by the time the write lands —
    and the caller must have accepted the one that is true at write time.
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

def _live(namespace: str, name: str) -> tuple[Any, dict[str, Any], str | None]:
    """``(live HPA, its §21 row, its resourceVersion)``. A failed read propagates.

    Never a default: bounds described against an autoscaler we could not read
    would put the numbers the operator typed on both sides of the diff.
    """
    live = reader.get_resource(GROUP, VERSION, PLURAL, name, namespace=namespace)
    version = get_field(live, "metadata", "resourceVersion")
    return live, hpa_row(live), (str(version) if version else None)


def build_patch(requested: dict[str, Any], *, resource_version: str | None) -> dict[str, Any]:
    """The merge patch: both bounds, plus rule 4's version when the caller sent one.

    Both are written on every patch, including the unchanged one, so the diff the
    operator confirms depends on what they asked for rather than on what this
    module decided was different.
    """
    patch: dict[str, Any] = {
        "spec": {
            "minReplicas": requested["minReplicas"],
            "maxReplicas": requested["maxReplicas"],
        },
    }
    if resource_version:
        patch["metadata"] = {"resourceVersion": resource_version}
    return patch


# --------------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------------- #

def plan(namespace: str, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``POST /api/autoscaling/hpas/{namespace}/{name}/bounds/plan``.

    Ungated and unaudited: one read and arithmetic on two integers. It does not
    dry-run the patch — a dry run is a write request the caller has not asked for
    yet, and it needs the preflight the funnel does.

    A request that changes nothing comes back as ``blocked`` rather than as a
    `422`, the way §20's does: the plan is the screen where the bounds are
    *decided*, and one that answered "you changed nothing" with an error alone
    would withhold the current bounds and the replica count at the moment those
    are the facts needed to pick different ones.
    """
    request = validate_request(payload)
    _live_obj, current, resource_version = _live(namespace, name)

    blocked: dict[str, Any] | None = None
    consequences: list[dict[str, Any]] = []
    try:
        check_changed(current, request)
    except Invalid as refusal:
        blocked = {
            "message": refusal.message,
            "hint": refusal.hint,
            "context": refusal.context,
        }
    else:
        consequences = consequences_for(current, request)

    return {
        "namespace": namespace,
        "name": name,
        "current": current,
        "requested": {
            "minReplicas": request["minReplicas"],
            "maxReplicas": request["maxReplicas"],
        },
        "resourceVersion": resource_version,
        # Never both: a blocked plan has no consequences to accept, and one that
        # is not blocked has nothing standing in the way.
        "blocked": blocked,
        "consequences": consequences,
        "gate": enabled_state(),
    }


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def set_bounds(
    namespace: str,
    name: str,
    payload: dict[str, Any],
    *,
    dry_run: bool = True,
    acknowledge_consequences: list[str] | None = None,
) -> dict[str, Any]:
    """``PUT /api/autoscaling/hpas/{namespace}/{name}/bounds`` — through the funnel.

    Order of refusals, each before the cluster is changed: request validation,
    the autoscaler read — which must answer — the concurrency check, the no-op
    check recomputed against the object as it is *now*, and the acknowledgement
    check. Then :func:`mutate`, which gates, preflights
    ``patch horizontalpodautoscalers``, sends the patch with ``dryRun=All`` when
    this is a preview, diffs live against the API server's projection, and audits
    the outcome.

    The §1.5 response gains three keys: ``consequences`` (echoed, so the record
    of the confirmation is in the response the operator's client kept),
    ``current`` and ``requested``.

    **`applied: true` means the bounds are stored.** Whether anything scales
    afterwards is the controller's decision and depends on it being able to read
    its metrics at all — ``current.scaling_active`` in this same response is that
    answer, read from before the write.
    """
    request = validate_request(payload)
    live, current, live_version = _live(namespace, name)

    sent_version = request["resourceVersion"]
    if sent_version and live_version and sent_version != live_version:
        conflict = Conflict(
            f"The autoscaler {namespace}/{name} changed while you were reading it.",
            detail=(
                f"You are editing version {sent_version}; the cluster has "
                f"{live_version}."
            ),
            hint="Reload the autoscaler and preview again against what it says now.",
            context={
                "group": GROUP, "version": VERSION, "resource": PLURAL,
                "namespace": namespace, "name": name, "verb": "patch",
                "currentResourceVersion": live_version,
                "currentMinReplicas": current["min_replicas"],
                "currentMaxReplicas": current["max_replicas"],
            },
        )
        # Rule 5 applies to a conflict too, and this one fires before the first
        # `mutate()` — so without this the trail held nothing to say two people
        # were editing the same autoscaler at once, which is the whole question
        # rule 4 exists to make answerable.
        audit_conflict(
            verb="patch", group=GROUP, version=VERSION, plural=PLURAL,
            namespace=namespace, name=name, dry_run=dry_run, error=conflict,
            detail=(
                f"hpa bounds {namespace}/{name}: refused, editing "
                f"{sent_version} and the cluster has {live_version}"
            ),
        )
        raise conflict

    check_changed(current, request)
    consequences = consequences_for(current, request)
    _require_acknowledgement(consequences, acknowledge_consequences)

    result = mutate(
        verb="patch",
        group=GROUP,
        version=VERSION,
        plural=PLURAL,
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        gate=_gate(),
        apply_fn=patch_fn(
            GROUP, VERSION, PLURAL, name,
            build_patch(request, resource_version=sent_version or live_version),
            namespace=namespace,
            content_type=MERGE_PATCH,
        ),
        before=live,
        # Both bounds, both sides. The question after an incident is what the
        # ceiling was before somebody raised it, and the diff digest alone cannot
        # answer that without the object it was taken over.
        detail=(
            f"hpa bounds {namespace}/{name}: "
            f"{current['min_replicas']}-{current['max_replicas']} -> "
            f"{request['minReplicas']}-{request['maxReplicas']}"
        ),
    )
    result["consequences"] = consequences
    result["current"] = current
    result["requested"] = {
        "minReplicas": request["minReplicas"],
        "maxReplicas": request["maxReplicas"],
    }
    return result


__all__ = [
    "build_patch",
    "check_changed",
    "consequences_for",
    "enabled_state",
    "plan",
    "set_bounds",
    "validate_request",
]
