"""
Quota advice (§29) — whether the next workload is admitted, and what refuses it.

A ResourceQuota refuses a write at admission, and the refusal arrives as a 403
carrying a sentence somebody has to parse under pressure. Everything needed to
answer the question *first* is already in the namespace: `spec.hard` minus
`status.used` is headroom, and a workload's requests are arithmetic over its own
containers. This module does that arithmetic.

**The trap this exists for is not "the quota is full".** That one is obvious from
§17's page and from the 403. The one that costs an afternoon is this:

> A quota that tracks a compute resource makes that resource **mandatory**. If
> any quota in the namespace bounds `requests.cpu`, then every pod created there
> must state `requests.cpu` — and a pod that omits it is refused with *"must
> specify requests.cpu"* even when the quota is one percent used.

A LimitRange with a `defaultRequest` for `Container` fixes it by injecting the
value at admission. So a namespace with a quota and no LimitRange is a namespace
where an ordinary Deployment cannot be created at all, the message names a field
rather than the missing object, and the fix is a second object nobody mentioned.
That is a *different* refusal from insufficient headroom, and §29 keeps them
apart because they send an operator to two different places.

Three properties are load-bearing.

**Advice, never admission.** The API server admits; §4's dry-run create asks it
and gets the authoritative answer. This module answers from arithmetic, before a
manifest exists, and it says which limit is tight and how much room is left —
which a 403 does not. Nothing here returns a field called `will_be_admitted`;
the verdict field is `verdict` and one of its values is `unknown`.

**`used` we could not read makes the verdict `unknown`, never `admitted`.** The
quota controller writes `status.used` asynchronously, so a fresh quota has none.
An advisor that treated absent usage as zero would report the roomiest possible
answer at exactly the moment it knows least — the confidently wrong answer this
project is built against, aimed at the operator who is about to deploy.

**A scoped quota is not silently included or silently excluded.** `scopes` and
`scopeSelector` decide which pods a quota counts, and for a proposed workload
this console usually cannot tell. Including it invents a refusal from a quota
that does not govern the workload; excluding it hides a real one. So it is
reported as `applies: null` with its own finding, and it makes the namespace
verdict `unknown` rather than `admitted`.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from app.k8s.client import get_core_v1
from app.resources import shaping
from app.resources.envelope import collect
from app.k8s.quantities import parse_quantity

logger = logging.getLogger(__name__)

#: Verdicts. `unknown` is a first-class answer, not an error.
ADMITTED = "admitted"
REFUSED = "refused"
UNKNOWN = "unknown"

#: Findings.
QUOTA_REQUIRES_UNSET_RESOURCE = "quota_requires_unset_resource"
QUOTA_EXHAUSTED = "quota_exhausted"
QUOTA_USAGE_UNKNOWN = "quota_usage_unknown"
QUOTA_SCOPED = "quota_scoped"

#: Quota keys that bound a *compute* resource on pods, and the container field
#: each one is satisfied by. `cpu` and `memory` are the API's own aliases for
#: the `requests.` forms, and a console that treated them as separate keys would
#: report a namespace as tracking two things when it tracks one.
COMPUTE_KEYS: dict[str, tuple[str, str]] = {
    "cpu": ("requests", "cpu"),
    "memory": ("requests", "memory"),
    "requests.cpu": ("requests", "cpu"),
    "requests.memory": ("requests", "memory"),
    "requests.ephemeral-storage": ("requests", "ephemeral-storage"),
    "limits.cpu": ("limits", "cpu"),
    "limits.memory": ("limits", "memory"),
    "limits.ephemeral-storage": ("limits", "ephemeral-storage"),
}

#: The LimitRange item type whose `default`/`defaultRequest` are injected into a
#: container that omits a value. `Pod` items bound the total and default nothing.
CONTAINER_TYPE = "Container"


def _items(listing: Any) -> list[Any]:
    return list(shaping.get_field(listing, "items", default=[]) or [])


def _container_defaults(limit_ranges: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """``(kind, resource) -> quantity`` a LimitRange injects into a container.

    Later ranges do not override earlier ones here, and that is a simplification
    this module states rather than hides: with two LimitRanges setting the same
    default the API server's choice is not something a console can predict, so
    the first is reported and the arithmetic below is advice either way.
    """
    defaults: dict[tuple[str, str], str] = {}
    for limit_range in limit_ranges:
        for item in limit_range.get("limits") or []:
            if item.get("type") != CONTAINER_TYPE:
                continue
            for resource, value in (item.get("defaultRequest") or {}).items():
                defaults.setdefault(("requests", str(resource)), str(value))
            for resource, value in (item.get("default") or {}).items():
                defaults.setdefault(("limits", str(resource)), str(value))
    return defaults


def _effective(container: dict[str, Any], defaults: dict[tuple[str, str], str],
               kind: str, resource: str) -> tuple[Decimal | None, str]:
    """One container's effective value for ``kind``/``resource``, and where from.

    Returns ``(value, source)`` where source is ``declared``, ``defaulted`` or
    ``absent``. ``absent`` is what makes a pod refused by the "must specify"
    rule, and it is deliberately distinct from a declared zero — `requests.cpu:
    "0"` is a value, satisfies the rule, and consumes no headroom.
    """
    declared = ((container.get(kind) or {}) or {}).get(resource)
    if declared is not None and str(declared) != "":
        return parse_quantity(declared), "declared"
    injected = defaults.get((kind, resource))
    if injected is not None:
        return parse_quantity(injected), "defaulted"
    return None, "absent"


def _tracked_compute(quotas: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """``(kind, resource) -> quota names`` for every compute resource bounded.

    This is the mandatory set: any pod created in the namespace must carry a
    value for each of these, from its own spec or from a LimitRange.
    """
    tracked: dict[tuple[str, str], list[str]] = {}
    for quota in quotas:
        for entry in quota.get("resources") or []:
            field = COMPUTE_KEYS.get(str(entry.get("resource")))
            if field is None:
                continue
            tracked.setdefault(field, [])
            name = str(quota.get("name"))
            if name not in tracked[field]:
                tracked[field].append(name)
    return tracked


def _headroom(entry: dict[str, Any]) -> Decimal | None:
    """``hard - used``, or ``None`` when either side is unknown.

    `None` propagates to a verdict of `unknown`. Treating an unwritten `used` as
    zero would report the roomiest possible answer at the moment this console
    knows least about the namespace.
    """
    hard = entry.get("hard_value")
    used = entry.get("used_value")
    if hard is None or used is None:
        return None
    return Decimal(hard) - Decimal(used)


def _quota_applies(quota: dict[str, Any]) -> bool | None:
    """Does this quota count the proposed workload? ``None`` when undecidable.

    A quota with no scopes counts everything, which is decidable and the common
    case. Anything scoped depends on the pod's priority class, its terminating
    state or whether it is BestEffort — facts about an object that does not
    exist yet — so this returns `None` rather than picking a side. Including it
    invents a refusal from a quota that may not govern the workload; excluding it
    hides a real one.
    """
    if quota.get("scoped") or (quota.get("scopes") or []):
        return None
    return True


def _findings(quota: dict[str, Any], applies: bool | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if applies is None:
        out.append({
            "code": QUOTA_SCOPED,
            "label": "This quota only counts some pods",
            "detail": (
                "It carries scopes or a scope selector, so whether it counts a "
                "given workload depends on that workload's priority class, its "
                "terminating state or whether it requests anything at all. This "
                "console cannot decide that for an object that does not exist "
                "yet, so its numbers are shown and its verdict is withheld."
            ),
        })
    unknown = [e for e in quota.get("resources") or [] if _headroom(e) is None]
    if unknown:
        out.append({
            "code": QUOTA_USAGE_UNKNOWN,
            "label": "How much of this quota is used is unknown",
            "detail": (
                "The quota controller writes status.used asynchronously and has "
                "not written it for "
                + ", ".join(sorted(str(e["resource"]) for e in unknown))
                + ". That is not zero, and no headroom can be computed from it."
            ),
        })
    full = [e for e in quota.get("resources") or [] if e.get("exhausted") is True]
    if full:
        out.append({
            "code": QUOTA_EXHAUSTED,
            "label": "This quota has no room left",
            "detail": (
                ", ".join(sorted(str(e["resource"]) for e in full))
                + " is at its hard limit, so anything consuming it is refused "
                "until something in the namespace is deleted or the quota is "
                "raised."
            ),
        })
    return out


def _mandatory_findings(
    tracked: dict[tuple[str, str], list[str]],
    defaults: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    """The trap: a tracked compute resource with no LimitRange default.

    Namespace-level rather than per-quota, because the rule is a property of the
    namespace: *any* quota bounding the resource makes it mandatory for *every*
    pod, and the fix is one LimitRange rather than a change to any quota.
    """
    missing = [
        (field, names) for field, names in sorted(tracked.items())
        if field not in defaults
    ]
    if not missing:
        return []
    return [{
        "code": QUOTA_REQUIRES_UNSET_RESOURCE,
        "label": (
            f"{len(missing)} resource(s) every pod must state, with no default "
            "to supply them"
        ),
        "detail": (
            "A quota that bounds a compute resource makes it mandatory: "
            + ", ".join(f"{kind}.{resource}" for (kind, resource), _ in missing)
            + " must be set on every container, or the pod is refused with "
            "\"must specify …\" — even when the quota is barely used. No "
            "LimitRange in this namespace supplies a default for them, so an "
            "ordinary Deployment that omits them cannot be created at all. The "
            "refusal names the field, not the missing LimitRange."
        ),
        "resources": [f"{kind}.{resource}" for (kind, resource), _ in missing],
        "quotas": sorted({name for _, names in missing for name in names}),
    }]


def _worst(verdicts: list[str]) -> str:
    """Any refusal refuses; otherwise any unknown is unknown."""
    if REFUSED in verdicts:
        return REFUSED
    if UNKNOWN in verdicts:
        return UNKNOWN
    return ADMITTED


def advise(namespace: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """``GET /api/quota/{namespace}`` and its ``POST …/preview`` (§29).

    Both listings are **collected**, and losing either withholds the verdict
    rather than producing one from half the picture: without the quotas there is
    nothing to be refused by, and without the LimitRanges every "must specify"
    conclusion would be wrong in the dangerous direction — reporting a workload
    as refused when a default the console could not read would have supplied the
    value.
    """
    unavailable: list[dict[str, Any]] = []

    quotas: list[dict[str, Any]] | None = None
    with collect(unavailable, "", "resourcequotas", namespace=namespace):
        quotas = [
            shaping.resourcequota_row(obj)
            for obj in _items(get_core_v1().list_namespaced_resource_quota(namespace))
        ]

    limit_ranges: list[dict[str, Any]] | None = None
    with collect(unavailable, "", "limitranges", namespace=namespace):
        limit_ranges = [
            shaping.limitrange_row(obj)
            for obj in _items(get_core_v1().list_namespaced_limit_range(namespace))
        ]

    readable = quotas is not None and limit_ranges is not None
    defaults = _container_defaults(limit_ranges or [])
    tracked = _tracked_compute(quotas or [])

    quota_rows: list[dict[str, Any]] = []
    for quota in quotas or []:
        applies = _quota_applies(quota)
        quota_rows.append({
            **quota,
            "applies": applies,
            "headroom": {
                str(entry["resource"]): (
                    None if _headroom(entry) is None else str(_headroom(entry))
                )
                for entry in quota.get("resources") or []
            },
            "findings": _findings(quota, applies),
        })

    result: dict[str, Any] = {
        "namespace": namespace,
        # `null`, never `[]`: "this namespace has no quota" is the answer that
        # says every workload is admitted, and a refused listing must not
        # produce it.
        "quotas": quota_rows if quotas is not None else None,
        "limitRanges": limit_ranges,
        "containerDefaults": {
            f"{kind}.{resource}": value for (kind, resource), value in sorted(defaults.items())
        },
        "mandatory": [
            f"{kind}.{resource}" for (kind, resource) in sorted(tracked)
        ] if quotas is not None else None,
        "findings": _mandatory_findings(tracked, defaults) if readable else [],
        "unavailable": unavailable,
        "partial": bool(unavailable),
    }

    if request is None:
        return result
    result["preview"] = _preview(
        request, quota_rows, defaults, tracked, readable,
        defaults_known=limit_ranges is not None,
    )
    return result


def _preview(
    request: dict[str, Any],
    quota_rows: list[dict[str, Any]],
    defaults: dict[tuple[str, str], str],
    tracked: dict[tuple[str, str], list[str]],
    readable: bool,
    *,
    defaults_known: bool,
) -> dict[str, Any]:
    """What one proposed workload would need, and which limit refuses it.

    ``defaults_known`` is false when the LimitRange listing did not answer, and
    it suppresses the "must specify" conclusion entirely rather than computing
    it from an empty default map. That conclusion is wrong in the dangerous
    direction: it reports a workload as **refused** because a value is missing,
    when a default this console could not read may well supply it. The verdict
    becomes `unknown` instead, which is what not knowing looks like.
    """
    replicas = int(request.get("replicas") or 1)
    containers = list(request.get("containers") or [])

    # Per-container effective values, and the ones that end up absent — which is
    # a refusal by a different rule from headroom and is reported as one.
    per_pod: dict[tuple[str, str], Decimal] = {}
    unset: list[dict[str, Any]] = []
    for index, container in enumerate(containers):
        for field in sorted(tracked):
            value, source = _effective(container, defaults, field[0], field[1])
            if source == "absent":
                if defaults_known:
                    unset.append({
                        "container": container.get("name") or f"container[{index}]",
                        "resource": f"{field[0]}.{field[1]}",
                    })
                continue
            if value is not None:
                per_pod[field] = per_pod.get(field, Decimal(0)) + value

    needed = {
        f"{kind}.{resource}": str(total * replicas)
        for (kind, resource), total in sorted(per_pod.items())
    }

    checks: list[dict[str, Any]] = []
    for quota in quota_rows:
        for entry in quota.get("resources") or []:
            key = str(entry["resource"])
            field = COMPUTE_KEYS.get(key)
            if field is not None:
                want = per_pod.get(field, Decimal(0)) * replicas
            elif key == "pods":
                want = Decimal(replicas)
            else:
                # An object-count or storage key this preview does not model.
                # Skipped rather than guessed: reporting a verdict for
                # `count/deployments.apps` from a request that does not say how
                # many it creates would be arithmetic over an invented number.
                continue
            room = _headroom(entry)
            if quota.get("applies") is None or room is None:
                verdict = UNKNOWN
            else:
                verdict = ADMITTED if want <= room else REFUSED
            checks.append({
                "quota": quota.get("name"),
                "resource": key,
                "needed": str(want),
                "headroom": None if room is None else str(room),
                "verdict": verdict,
            })

    verdicts = [check["verdict"] for check in checks]
    if unset:
        verdicts.append(REFUSED)
    if not readable:
        verdicts.append(UNKNOWN)

    return {
        "replicas": replicas,
        "needed": needed,
        # Containers missing a resource the namespace makes mandatory. This is
        # the "must specify …" refusal, and it is listed separately from the
        # headroom checks because the fix is a LimitRange rather than more room.
        "unsetMandatory": unset,
        "checks": checks,
        "verdict": _worst(verdicts) if (checks or unset or not readable) else ADMITTED,
    }


__all__ = [
    "ADMITTED",
    "QUOTA_EXHAUSTED",
    "QUOTA_REQUIRES_UNSET_RESOURCE",
    "QUOTA_SCOPED",
    "QUOTA_USAGE_UNKNOWN",
    "REFUSED",
    "UNKNOWN",
    "advise",
]
