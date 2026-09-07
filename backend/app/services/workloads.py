"""
The unified workload model (§6).

Six controller kinds — Deployment, StatefulSet, DaemonSet, Job, CronJob,
ReplicaSet — are shaped into one row so the console can put them in a single
table. They have almost nothing in common at the API level: a Deployment counts
``status.readyReplicas``, a DaemonSet counts ``status.numberReady``, a Job counts
``status.succeeded``, and a CronJob counts nothing at all. The mapping from each
kind's own vocabulary into the shared row lives here, in one place, because the
alternative — six per-kind row builders — is six places for the same subtle bug
to be fixed five times.

Three things in this module are the whole reason it is careful:

**Status has a real ``Unknown`` branch.** A controller that has not written a
status for the *current* generation of an object has told us nothing about it.
Reporting that as ``Healthy`` is the project's defect standard in miniature: the
absence of a reported problem is not evidence of health, and a green row is a
claim. So ``observedGeneration`` is checked against ``metadata.generation``
before any count is believed, and a stale or missing one yields ``Unknown`` with
a sentence saying which generation the numbers actually describe.

**Zero and unknown are different numbers.** ``status.readyReplicas`` is omitted
from the API response when it is zero (``omitempty`` on the Go type), so a
missing field means "zero" — but only once we know the controller wrote the
status at all. Every count goes through :func:`_count`, which takes that
distinction as an argument instead of guessing. Likewise ``restarts_24h`` is
``None`` when the pod listing failed and ``0`` when it succeeded and found no
restarts; a column that renders "0 restarts" for a workload we could not look at
is the confident wrong answer this console exists to avoid.

**Every optional read is collected, never swallowed.** The row's pod-derived
fields, the detail view's Services, its rollout block and its pods are each read
independently inside ``app.resources.envelope.collect``, so one forbidden verb
degrades one key and says so in ``unavailable[]`` rather than blanking the view
or, worse, quietly returning a smaller truth.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from kubernetes.client.rest import ApiException

from app.errors import Invalid, NotFound, from_api_exception
from app.k8s.client import get_apps_v1, get_batch_v1, get_core_v1
from app.models import rfc3339 as _rfc3339_datetime
from app.resources.envelope import collect, envelope, unavailable_entry
from app.resources.shaping import label_selector_matches, phase_detail, pod_row

logger = logging.getLogger(__name__)

# The window ``restarts_24h`` reports over. Named rather than inlined because the
# field name in the contract carries the number, so changing one without the
# other would produce a column whose label lies about its own contents.
RESTART_WINDOW = timedelta(hours=24)


# --------------------------------------------------------------------------- #
# The six kinds
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class KindSpec:
    """Everything that differs between the six workload kinds, as data.

    Method names are spelled out rather than derived from the kind, because the
    kubernetes client's snake_casing is not mechanical: ``StatefulSet`` becomes
    ``stateful_set`` and ``CronJob`` becomes ``cron_job``, so a generated name
    would be right for Deployment and Job and wrong for the other four — and
    wrong in the form of an ``AttributeError`` inside a request handler, which
    reads as a bug in the handler.

    The capability flags are the answer to "may this button exist for this
    kind", and they live here so the API layer can reject an impossible action
    with a message naming the kind instead of forwarding it to the cluster and
    relaying whatever the API server says about a subresource that is not there.
    """

    kind: str
    plural: str
    group: str
    version: str
    client: Callable[[], Any]
    list_namespaced: str
    list_all: str
    read_namespaced: str
    scalable: bool       # has a /scale subresource
    restartable: bool    # `kubectl rollout restart` applies
    suspendable: bool    # has spec.suspend
    revisioned: bool     # has rollout history and can be rolled back


_SPECS: tuple[KindSpec, ...] = (
    KindSpec(
        "Deployment", "deployments", "apps", "v1", get_apps_v1,
        "list_namespaced_deployment", "list_deployment_for_all_namespaces",
        "read_namespaced_deployment",
        scalable=True, restartable=True, suspendable=False, revisioned=True,
    ),
    KindSpec(
        "StatefulSet", "statefulsets", "apps", "v1", get_apps_v1,
        "list_namespaced_stateful_set", "list_stateful_set_for_all_namespaces",
        "read_namespaced_stateful_set",
        scalable=True, restartable=True, suspendable=False, revisioned=True,
    ),
    KindSpec(
        "DaemonSet", "daemonsets", "apps", "v1", get_apps_v1,
        "list_namespaced_daemon_set", "list_daemon_set_for_all_namespaces",
        "read_namespaced_daemon_set",
        # No /scale subresource: a DaemonSet's size is decided by node selection.
        scalable=False, restartable=True, suspendable=False, revisioned=True,
    ),
    KindSpec(
        "Job", "jobs", "batch", "v1", get_batch_v1,
        "list_namespaced_job", "list_job_for_all_namespaces",
        "read_namespaced_job",
        scalable=False, restartable=False, suspendable=True, revisioned=False,
    ),
    KindSpec(
        "CronJob", "cronjobs", "batch", "v1", get_batch_v1,
        "list_namespaced_cron_job", "list_cron_job_for_all_namespaces",
        "read_namespaced_cron_job",
        scalable=False, restartable=False, suspendable=True, revisioned=False,
    ),
    KindSpec(
        "ReplicaSet", "replicasets", "apps", "v1", get_apps_v1,
        "list_namespaced_replica_set", "list_replica_set_for_all_namespaces",
        "read_namespaced_replica_set",
        # Scalable, and deliberately not restartable: `kubectl rollout restart`
        # does not accept a ReplicaSet, and re-rolling one behind a Deployment
        # would be undone by the Deployment controller within a reconcile loop.
        scalable=True, restartable=False, suspendable=False, revisioned=False,
    ),
)

SPEC_BY_PLURAL: dict[str, KindSpec] = {spec.plural: spec for spec in _SPECS}
SPEC_BY_KIND: dict[str, KindSpec] = {spec.kind: spec for spec in _SPECS}
PLURAL_TO_KIND: dict[str, str] = {spec.plural: spec.kind for spec in _SPECS}
KIND_TO_PLURAL: dict[str, str] = {spec.kind: spec.plural for spec in _SPECS}
WORKLOAD_PLURALS: tuple[str, ...] = tuple(spec.plural for spec in _SPECS)


def resolve_plural(plural: str) -> KindSpec:
    """The spec for a path segment, or ``not_found`` naming what is available.

    404 rather than 501 ``unsupported``: ``unsupported`` means "this cluster does
    not serve that API", which would send an operator to check their cluster's
    API groups. ``/api/workloads/pods/...`` is not a cluster capability problem,
    it is a URL that this endpoint does not have — Pods are reachable through the
    generic resource browser (§4).
    """
    spec = SPEC_BY_PLURAL.get(plural)
    if spec is None:
        raise NotFound(
            f'"{plural}" is not a workload kind.',
            hint=(
                "The workload endpoints cover "
                + ", ".join(WORKLOAD_PLURALS)
                + ". Anything else is reachable through /api/resources/{group}/{version}/{plural}."
            ),
            context={"resource": plural},
        )
    return spec


def resolve_kind_filter(kind: str | None) -> tuple[KindSpec, ...]:
    """Parse ``?kind=`` into the specs to list.

    Accepts the Kind (``Deployment``), the plural (``deployments``) and the
    singular (``deployment``), case-insensitively, because the row carries
    ``kind: "Deployment"`` while the path carries ``deployments`` — a UI that
    round-trips either field would otherwise get an empty list, and an empty list
    here reads as "this cluster has no Deployments".

    An unrecognised value is rejected rather than ignored. Ignoring it returns
    every workload under a filter the caller believes is applied, which is a
    wrong answer; returning nothing claims the cluster is empty, which is a worse
    one.
    """
    if kind is None or not kind.strip():
        return _SPECS

    wanted = kind.strip().lower()
    for spec in _SPECS:
        if wanted in (spec.kind.lower(), spec.plural, spec.plural[:-1]):
            return (spec,)

    raise Invalid(
        f'"{kind}" is not a workload kind.',
        hint="Use one of: " + ", ".join(spec.kind for spec in _SPECS) + ".",
        context={"parameter": "kind", "value": kind},
    )


# --------------------------------------------------------------------------- #
# Field access
# --------------------------------------------------------------------------- #
#
# This module is fed by the typed kubernetes clients, whose models expose
# snake_case attributes. The dynamic client hands back camelCase dicts of the
# same objects, and both spellings turn up in fixtures. Reading one spelling with
# ``getattr`` would return None for the other — silently, producing a row full of
# nulls that looks like a workload the cluster could not describe. So access goes
# through one reader that understands both.

_CAMEL_RE = re.compile(r"_([a-z0-9])")


def _camel(name: str) -> str:
    """``ready_replicas`` -> ``readyReplicas``."""
    return _CAMEL_RE.sub(lambda match: match.group(1).upper(), name)


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """One attribute or key, in either spelling. ``None`` means absent."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        if name in obj:
            value = obj[name]
        else:
            value = obj.get(_camel(name))
    else:
        value = getattr(obj, name, None)
        if value is None:
            value = getattr(obj, _camel(name), None)
    return default if value is None else value


def _dig(obj: Any, *names: str, default: Any = None) -> Any:
    """``_get`` down a path, stopping at the first absent level."""
    current = obj
    for name in names:
        current = _get(current, name)
        if current is None:
            return default
    return current


def _as_datetime(value: Any) -> datetime | None:
    """A Kubernetes timestamp as an aware UTC datetime, or None.

    The typed client parses timestamps for us; the dynamic client and every JSON
    fixture leave them as RFC 3339 strings. Both arrive here. A value that parses
    as neither returns None rather than raising: a malformed timestamp on one
    pod must not fail the listing of the other four hundred.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("Unparseable Kubernetes timestamp %r", value)
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _timestamp(value: Any) -> str | None:
    """A Kubernetes timestamp as the contract's ``2026-08-18T09:14:00Z`` form."""
    parsed = _as_datetime(value)
    return _rfc3339_datetime(parsed) if parsed is not None else None


def _age_seconds(value: Any, *, now: datetime) -> int | None:
    """Seconds since a creation timestamp.

    ``None``, not ``0``, when there is no timestamp: zero reads as "created this
    second", which is a specific and wrong claim about an object we could not
    date. Clamped at zero because a node with a skewed clock can otherwise
    produce a workload that was created in the future.
    """
    created = _as_datetime(value)
    if created is None:
        return None
    return max(0, int((now - created).total_seconds()))


def _count(value: Any, observed: bool) -> int | None:
    """A replica counter, with 'omitted' resolved against 'was it written at all'.

    Kubernetes omits zero-valued status counters from the wire (``omitempty``),
    so an absent ``readyReplicas`` means zero — *provided* the controller wrote
    the status. If it has not, the same absence means we do not know, and this is
    the one function that keeps those two apart. Passing ``observed`` in rather
    than inferring it here forces every caller to have decided.
    """
    if value is None:
        return 0 if observed else None
    return value


def _condition(obj: Any, condition_type: str) -> Any | None:
    """One entry of ``status.conditions`` by type, or None."""
    for condition in _dig(obj, "status", "conditions", default=[]) or []:
        if _get(condition, "type") == condition_type:
            return condition
    return None


def _condition_sentence(prefix: str, condition: Any) -> str:
    """``prefix`` plus whatever the condition says, without empty punctuation."""
    reason = _get(condition, "reason")
    message = _get(condition, "message")
    tail = " — ".join(part for part in (reason, message) if part)
    return f"{prefix}: {tail}" if tail else f"{prefix}."


# --------------------------------------------------------------------------- #
# Selectors
# --------------------------------------------------------------------------- #

def _selector_terms(selector: Any) -> tuple[dict[str, str], list[Any]]:
    """``(matchLabels, matchExpressions)`` from a LabelSelector, both possibly empty."""
    match_labels = dict(_dig(selector, "match_labels", default={}) or {})
    match_expressions = list(_dig(selector, "match_expressions", default=[]) or [])
    return match_labels, match_expressions


def selector_is_empty(selector: Any) -> bool:
    """True when the selector constrains nothing (and so selects everything)."""
    match_labels, match_expressions = _selector_terms(selector)
    return not match_labels and not match_expressions


def label_selector_string(selector: Any) -> str | None:
    """A LabelSelector rendered for the API server's ``labelSelector`` parameter.

    Server-side selection rather than listing a namespace and filtering here:
    a namespace with ten thousand pods would otherwise be transferred in full to
    build one detail page, and the read deadline would fire before it finished —
    a timeout that looks like an unreachable cluster.
    """
    if selector is None or selector_is_empty(selector):
        return None

    match_labels, match_expressions = _selector_terms(selector)
    terms = [f"{key}={value}" for key, value in sorted(match_labels.items())]
    for expression in match_expressions:
        key = _get(expression, "key")
        operator = _get(expression, "operator")
        values = ",".join(str(v) for v in (_get(expression, "values", default=[]) or []))
        if operator == "In":
            terms.append(f"{key} in ({values})")
        elif operator == "NotIn":
            terms.append(f"{key} notin ({values})")
        elif operator == "Exists":
            terms.append(str(key))
        elif operator == "DoesNotExist":
            terms.append(f"!{key}")
        else:
            # Cannot be expressed; refuse to build a selector that would silently
            # mean something broader than the caller asked for.
            return None
    return ",".join(terms)


# --------------------------------------------------------------------------- #
# Pod templates, images, containers
# --------------------------------------------------------------------------- #

def pod_template(obj: Any, kind: str) -> Any | None:
    """The PodTemplateSpec a workload creates pods from.

    A CronJob keeps it two levels down, inside its jobTemplate; every other kind
    has it at ``spec.template``.
    """
    if kind == "CronJob":
        return _dig(obj, "spec", "job_template", "spec", "template")
    return _dig(obj, "spec", "template")


def _all_containers(template: Any) -> list[tuple[str, Any]]:
    """Every container in a pod template, tagged with which list it came from.

    Init and ephemeral containers are included deliberately. An init container
    that cannot pull its image is the reason a workload never starts, and a
    console that shows only ``spec.containers`` cannot show the operator the
    image they need to fix — they would have to leave and run ``kubectl``, which
    is the failure this view exists to prevent.
    """
    spec = _get(template, "spec")
    tagged: list[tuple[str, Any]] = []
    for container in _get(spec, "init_containers", default=[]) or []:
        tagged.append(("init", container))
    for container in _get(spec, "containers", default=[]) or []:
        tagged.append(("container", container))
    for container in _get(spec, "ephemeral_containers", default=[]) or []:
        tagged.append(("ephemeral", container))
    return tagged


def container_images(obj: Any, kind: str) -> list[str]:
    """Every distinct image the workload runs, init and ephemeral included.

    Order-preserving de-duplication rather than ``sorted(set(...))``: sidecar
    stacks read in the order they are declared, and a shuffled list makes two
    rows that run the same images look different.
    """
    images: list[str] = []
    for _tag, container in _all_containers(pod_template(obj, kind)):
        image = _get(container, "image")
        if image and image not in images:
            images.append(image)
    return images


def _probes(container: Any) -> dict[str, bool]:
    """Which of the three probes are declared. Booleans, not the probe bodies.

    The detail panel asks "does this container have a readiness probe" far more
    often than it asks what the probe is, and a missing readiness probe is a
    common explanation for a Service that black-holes traffic during a rollout.
    """
    return {
        "liveness": _get(container, "liveness_probe") is not None,
        "readiness": _get(container, "readiness_probe") is not None,
        "startup": _get(container, "startup_probe") is not None,
    }


def _container_ports(container: Any) -> list[dict]:
    return [
        {
            "name": _get(port, "name"),
            "containerPort": _get(port, "container_port"),
            "protocol": _get(port, "protocol") or "TCP",
        }
        for port in (_get(container, "ports", default=[]) or [])
    ]


def _resources(container: Any) -> dict:
    resources = _get(container, "resources")
    return {
        "requests": dict(_get(resources, "requests", default={}) or {}),
        "limits": dict(_get(resources, "limits", default={}) or {}),
    }


def _spec_block(obj: Any, kind: str) -> dict:
    """The §6 detail ``spec`` block: containers plus the pod-level settings.

    ``env_count`` counts ``env`` only. ``envFrom`` pulls in an unknown number of
    variables from a ConfigMap or Secret we would have to read (and be permitted
    to read) to count, and folding an unread envFrom into the same number would
    report a count that is wrong in a direction nobody can see.
    """
    template = pod_template(obj, kind)
    spec = _get(template, "spec")

    containers = []
    for tag, container in _all_containers(template):
        containers.append({
            "name": _get(container, "name"),
            "image": _get(container, "image"),
            "type": tag,
            "ports": _container_ports(container),
            "resources": _resources(container),
            "env_count": len(_get(container, "env", default=[]) or []),
            "probes": _probes(container),
        })

    volumes = []
    for volume in _get(spec, "volumes", default=[]) or []:
        name = _get(volume, "name")
        # The volume's *kind* is whichever source field is set. Reported as a
        # type name rather than the whole source: "this is an emptyDir" is what
        # decides whether draining the node loses data, and it is the only part
        # of the source a table can show.
        source = None
        for candidate in (
            "config_map", "secret", "persistent_volume_claim", "empty_dir",
            "host_path", "projected", "downward_api", "nfs", "csi",
        ):
            if _get(volume, candidate) is not None:
                source = _camel(candidate)
                break
        volumes.append({"name": name, "type": source})

    tolerations = [
        {
            "key": _get(toleration, "key"),
            "operator": _get(toleration, "operator"),
            "value": _get(toleration, "value"),
            "effect": _get(toleration, "effect"),
        }
        for toleration in (_get(spec, "tolerations", default=[]) or [])
    ]

    return {
        "containers": containers,
        # service_account_name, not the deprecated service_account: both are
        # populated by the API server, and reading the deprecated one would show
        # nothing on a cluster that has stopped defaulting it.
        "serviceAccount": _get(spec, "service_account_name") or _get(spec, "service_account"),
        "nodeSelector": dict(_get(spec, "node_selector", default={}) or {}),
        "tolerations": tolerations,
        "volumes": volumes,
    }


# --------------------------------------------------------------------------- #
# Restarts
# --------------------------------------------------------------------------- #

def restarts_in_window(pod: Any, *, now: datetime) -> int:
    """Container restarts attributable to the last 24 hours for one pod.

    ``restartCount`` is cumulative for the life of the pod, so reporting it
    directly would put a three-week-old crash loop on today's dashboard — the
    number would be real and the column heading would be a lie.

    Two cases are exact:

    * the pod itself is younger than the window, so every restart it has ever
      had happened inside the window and the cumulative count *is* the windowed
      count;
    * the container's most recent termination predates the window, so there can
      be no later restart and the answer is zero.

    The third case — an older pod whose last termination is inside the window —
    is a lower bound of one, and cannot be better: the kubelet keeps exactly one
    previous termination, so a container that restarted twenty times yesterday
    and once today is indistinguishable from one that restarted once today.
    Under-counting is the safe direction here because ``status`` and
    ``status_reason`` already report the crash loop from the controller's own
    conditions, so the row does not depend on this number to look wrong.
    """
    cutoff = now - RESTART_WINDOW
    started = _as_datetime(_dig(pod, "status", "start_time")) or _as_datetime(
        _dig(pod, "metadata", "creation_timestamp")
    )

    statuses = list(_dig(pod, "status", "container_statuses", default=[]) or [])
    statuses += list(_dig(pod, "status", "init_container_statuses", default=[]) or [])

    total = 0
    for status in statuses:
        count = _get(status, "restart_count", default=0) or 0
        if count <= 0:
            continue
        if started is not None and started >= cutoff:
            total += count
            continue
        last_exit = _as_datetime(_dig(status, "last_state", "terminated", "finished_at"))
        if last_exit is not None and last_exit >= cutoff:
            total += 1
    return total


@dataclass(frozen=True)
class _PodFacts:
    """The three things a workload row needs to know about a pod."""

    namespace: str | None
    labels: dict[str, str]
    owner_uids: tuple[str, ...]
    restarts: int


class PodIndex:
    """Every pod in scope, indexed for attribution to workloads.

    Built from *one* listing. Listing pods per workload instead would be an
    N+1 against the API server that turns a 60-workload namespace into 61 round
    trips, and the read deadline would start firing on the larger ones — which
    surfaces as an unreachable cluster rather than as "this page asks too much".
    """

    def __init__(self, pods: Iterable[Any], *, now: datetime) -> None:
        self.facts: list[_PodFacts] = []
        for pod in pods:
            metadata = _get(pod, "metadata")
            self.facts.append(_PodFacts(
                namespace=_get(metadata, "namespace"),
                labels=dict(_get(metadata, "labels", default={}) or {}),
                owner_uids=tuple(
                    str(_get(ref, "uid"))
                    for ref in (_get(metadata, "owner_references", default=[]) or [])
                    if _get(ref, "uid")
                ),
                restarts=restarts_in_window(pod, now=now),
            ))

    def restarts_for_selector(self, namespace: str | None, selector: Any) -> int | None:
        """Restarts of the pods this selector picks, or ``None`` if it cannot be counted.

        ``None`` for an empty selector: by the LabelSelector rules it matches
        every pod in the namespace, and attributing a whole namespace's restarts
        to one workload is a number that is confidently wrong rather than
        missing.

        ``None`` too when **any** pod's membership could not be decided — a
        selector using a `matchExpressions` operator this console does not model.
        The sum would otherwise be a floor presented as a total, and a
        `restarts_24h` that reads low is exactly the number nobody re-checks. One
        undecidable pod poisons the whole count rather than being silently
        excluded, because there is no way to render "23 restarts, plus however
        many belong to the pods we could not classify".
        """
        if selector is None or selector_is_empty(selector):
            return None
        total = 0
        for fact in self.facts:
            if namespace is not None and fact.namespace != namespace:
                continue
            member = label_selector_matches(selector, fact.labels)
            if member is None:
                return None
            if member:
                total += fact.restarts
        return total

    def restarts_for_owners(self, uids: set[str]) -> int:
        """Restarts of the pods directly owned by any of these objects."""
        return sum(
            fact.restarts
            for fact in self.facts
            if any(uid in uids for uid in fact.owner_uids)
        )


# --------------------------------------------------------------------------- #
# Replica counts and status
# --------------------------------------------------------------------------- #

def controller_observed(kind: str, obj: Any) -> tuple[bool, str | None]:
    """Has this kind's controller reported on the object's *current* spec?

    Returns ``(observed, reason_if_not)``. This is the gate in front of every
    count in the row. Without it, a Deployment created one second ago — spec
    accepted, no ReplicaSet yet, no status written — has zero of everything, and
    "0 desired, 0 ready" satisfies any equality test for health. It would go out
    green.
    """
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet"):
        generation = _dig(obj, "metadata", "generation")
        observed = _dig(obj, "status", "observed_generation")
        if observed is None:
            return False, (
                f"The {kind} controller has not written a status for this object "
                "yet, so its rollout state is unknown rather than healthy."
            )
        if generation is not None and observed < generation:
            return False, (
                f"The {kind} controller has observed generation {observed} while "
                f"the object is at generation {generation}: the replica counts "
                "describe an earlier revision of the spec, not the current one."
            )
        return True, None

    if kind == "Job":
        # JobStatus has no observedGeneration to check, so the evidence that the
        # controller has acted is that it set a start time or wrote a counter.
        #
        # The counters are checked against None and not truthiness on purpose:
        # `active: 0` is the controller stating that no pods are running, which is
        # evidence it looked. Treating 0 as "no signal" would push every finished
        # Job back into Unknown.
        #
        # `conditions`, though, is a list, and an EMPTY list is the absence of
        # conditions rather than a condition. The client returns `[]` for a Job the
        # controller has not touched, and `[] is not None`, so counting it as a
        # signal made an untouched Job report Progressing — a claim that it is
        # running, about an object whose controller has said nothing at all. That is
        # the same "empty is not evidence" error the envelope layer exists to
        # prevent, arriving through a status field instead of a failed read.
        conditions = _dig(obj, "status", "conditions")
        signals = (
            _dig(obj, "status", "start_time"),
            _dig(obj, "status", "active"),
            _dig(obj, "status", "succeeded"),
            _dig(obj, "status", "failed"),
            conditions if conditions else None,
        )
        if all(signal is None for signal in signals):
            return False, (
                "The Job controller has not started this Job yet: it has no start "
                "time and no pod counters, so nothing is known about its progress."
            )
        return True, None

    if kind == "CronJob":
        # A CronJob controller writes status only when it schedules something, so
        # "no status" is the normal state of a correct CronJob between runs and
        # must not be reported as Unknown — doing so would mark most of the
        # cluster's CronJobs unknown and bury the rows that genuinely are. The
        # evidence of a working CronJob is its own spec: a schedule to evaluate.
        if not _dig(obj, "spec", "schedule"):
            return False, (
                "This CronJob has no schedule, so the controller has nothing to "
                "evaluate and will never create a Job from it."
            )
        return True, None

    return False, f"{kind} has no known status vocabulary."


def replica_counts(kind: str, obj: Any, observed: bool) -> dict:
    """The row's ``replicas`` block, translated from each kind's own counters.

    Nulls are meaningful throughout: ``updated`` is null for kinds that have no
    concept of an update (a ReplicaSet's pods are by definition at its own
    template; a Job's pods are never re-rolled), because reporting ``updated ==
    desired`` there would invent agreement with a field the API does not have.
    """
    status = _get(obj, "status")

    if kind in ("Deployment", "StatefulSet"):
        return {
            "desired": _dig(obj, "spec", "replicas"),
            "ready": _count(_get(status, "ready_replicas"), observed),
            "updated": _count(_get(status, "updated_replicas"), observed),
            "available": _count(_get(status, "available_replicas"), observed),
        }

    if kind == "ReplicaSet":
        return {
            "desired": _dig(obj, "spec", "replicas"),
            "ready": _count(_get(status, "ready_replicas"), observed),
            "updated": None,
            "available": _count(_get(status, "available_replicas"), observed),
        }

    if kind == "DaemonSet":
        # A DaemonSet's desired count comes from node matching, not from the
        # spec: there is no replicas field to read.
        return {
            "desired": _count(_get(status, "desired_number_scheduled"), observed),
            "ready": _count(_get(status, "number_ready"), observed),
            "updated": _count(_get(status, "updated_number_scheduled"), observed),
            "available": _count(_get(status, "number_available"), observed),
        }

    if kind == "Job":
        # desired/ready read as completion progress ("3 of 5"), which is the
        # question a Job row answers. `available` is the number of pods running
        # right now, which is the closest true analogue of an available replica.
        completions = _dig(obj, "spec", "completions")
        return {
            "desired": completions if completions is not None else _dig(obj, "spec", "parallelism"),
            "ready": _count(_get(status, "succeeded"), observed),
            "updated": None,
            "available": _count(_get(status, "active"), observed),
        }

    # CronJob. It owns no pods of its own — its Jobs do — so every count is null
    # rather than zero. Reporting 0/0 would render as a workload with nothing
    # running, which is a claim about pods this object never had.
    return {"desired": None, "ready": None, "updated": None, "available": None}


def derive_status(kind: str, obj: Any) -> tuple[str, str | None]:
    """``(status, status_reason)`` for one workload.

    ``status`` is one of ``Healthy``, ``Progressing``, ``Degraded``,
    ``Suspended``, ``Unknown``. ``status_reason`` is a sentence for every state
    except ``Healthy``, which needs no explanation and gets ``None`` so the UI
    can render nothing rather than "everything is fine".

    The buckets, so that they stay stable as kinds are added:

    * ``Suspended`` — not expected to be running, and that is a fact about the
      spec rather than a failure: a suspended CronJob or Job, anything scaled to
      zero, a DaemonSet whose node selector matches no node.
    * ``Degraded`` — the controller reported a failure, or nothing is available.
    * ``Progressing`` — moving towards the spec: mid-rollout, pods still
      starting, a Job with pods running.
    * ``Healthy`` — the controller has observed the current spec and the counts
      it reports satisfy it.
    * ``Unknown`` — everything else, which is to say every case where we have no
      evidence either way. It is a real branch, not a fallback nobody reaches.
    """
    observed, unknown_reason = controller_observed(kind, obj)
    counts = replica_counts(kind, obj, observed)

    # Suspension is checked before observation: a suspended Job never starts, so
    # its controller writes no counters, and the observation gate would report
    # the most deliberate state a workload can be in as "unknown".
    if kind in ("Job", "CronJob") and _dig(obj, "spec", "suspend"):
        return "Suspended", (
            "Suspended: the controller will not create pods for it until it is resumed."
        )

    if not observed:
        return "Unknown", unknown_reason

    if kind == "Deployment":
        return _deployment_status(obj, counts)
    if kind == "StatefulSet":
        return _statefulset_status(obj, counts)
    if kind == "DaemonSet":
        return _daemonset_status(obj, counts)
    if kind == "ReplicaSet":
        return _replicaset_status(counts)
    if kind == "Job":
        return _job_status(obj, counts)
    return _cronjob_status(obj)


_ZERO_REPLICAS_REASON = (
    "Scaled to zero replicas: no pods are expected to be running, so this is a "
    "deliberate state and not a failure."
)


def _deployment_status(obj: Any, counts: dict) -> tuple[str, str | None]:
    desired = counts["desired"]
    ready = counts["ready"]
    updated = counts["updated"]
    available = counts["available"]

    if desired is None:
        return "Unknown", (
            "This Deployment has no replica count in its spec, so there is nothing "
            "to compare its running pods against."
        )
    if desired == 0:
        return "Suspended", _ZERO_REPLICAS_REASON

    replica_failure = _condition(obj, "ReplicaFailure")
    if replica_failure is not None and _get(replica_failure, "status") == "True":
        # Quota exhausted, a missing ServiceAccount, an admission webhook
        # refusing the pod. The controller cannot create pods at all, and the
        # replica counts alone would show this as a rollout that is merely slow.
        return "Degraded", _condition_sentence(
            "The controller cannot create pods for this Deployment", replica_failure
        )

    progressing = _condition(obj, "Progressing")
    if progressing is not None and _get(progressing, "status") == "False":
        # Progressing=False means the controller gave up (progressDeadlineSeconds
        # elapsed). Reporting that as Progressing would tell the operator to wait
        # for something that has already stopped happening.
        return "Degraded", _condition_sentence(
            "The rollout stopped making progress", progressing
        )

    available_condition = _condition(obj, "Available")
    if available_condition is not None and _get(available_condition, "status") == "False":
        return "Degraded", _condition_sentence(
            "No replicas are available to serve traffic", available_condition
        )

    if ready == desired and updated == desired and available == desired:
        return "Healthy", None

    if updated is not None and updated < desired:
        return "Progressing", (
            f"Rollout in progress: {updated} of {desired} replicas have been updated."
        )
    shortfall = desired - (available if available is not None else 0)
    return "Progressing", (
        f"{shortfall} of {desired} replicas are not available yet."
    )


def _statefulset_status(obj: Any, counts: dict) -> tuple[str, str | None]:
    desired = counts["desired"]
    ready = counts["ready"]
    updated = counts["updated"]

    if desired is None:
        return "Unknown", (
            "This StatefulSet has no replica count in its spec, so there is nothing "
            "to compare its running pods against."
        )
    if desired == 0:
        return "Suspended", _ZERO_REPLICAS_REASON

    # Judged on ready/updated, not on availableReplicas: that field only became
    # generally available in Kubernetes 1.22, and a cluster that omits it would
    # otherwise show every healthy StatefulSet as permanently Progressing.
    if ready == desired and updated == desired:
        return "Healthy", None
    if ready == 0:
        return "Degraded", (
            f"None of the {desired} replicas are ready."
        )
    if updated is not None and updated < desired:
        return "Progressing", (
            f"Rollout in progress: {updated} of {desired} replicas have been updated."
        )
    return "Progressing", f"{ready} of {desired} replicas are ready."


def _daemonset_status(obj: Any, counts: dict) -> tuple[str, str | None]:
    desired = counts["desired"]
    ready = counts["ready"]
    updated = counts["updated"]
    available = counts["available"]

    if desired == 0:
        # Not Healthy: "0 of 0 ready" satisfies every equality test while the
        # DaemonSet runs nowhere, and a nodeSelector that matches no node is one
        # of the most common ways to deploy something that silently does nothing.
        return "Suspended", (
            "No nodes match this DaemonSet, so it is not scheduled anywhere. Check "
            "its nodeSelector, affinity and tolerations against the cluster's nodes."
        )
    if ready == desired and (updated is None or updated == desired):
        return "Healthy", None
    if ready == 0:
        return "Degraded", f"None of the {desired} scheduled pods are ready."
    if updated is not None and updated < desired:
        return "Progressing", (
            f"Rollout in progress: {updated} of {desired} pods have been updated."
        )
    unavailable = desired - (available if available is not None else 0)
    return "Progressing", f"{unavailable} of {desired} pods are not available yet."


def _replicaset_status(counts: dict) -> tuple[str, str | None]:
    desired = counts["desired"]
    ready = counts["ready"]

    if desired is None:
        return "Unknown", (
            "This ReplicaSet has no replica count in its spec, so there is nothing "
            "to compare its running pods against."
        )
    if desired == 0:
        # The normal resting state of every superseded revision behind a
        # Deployment. Flagging those as failures would make the ReplicaSet list
        # mostly red on a healthy cluster.
        return "Suspended", _ZERO_REPLICAS_REASON
    if ready == desired:
        return "Healthy", None
    if ready == 0:
        return "Degraded", f"None of the {desired} pods are ready."
    return "Progressing", f"{ready} of {desired} pods are ready."


def _job_status(obj: Any, counts: dict) -> tuple[str, str | None]:
    desired = counts["desired"]
    succeeded = counts["ready"]
    active = counts["available"]
    failed = _dig(obj, "status", "failed") or 0

    failed_condition = _condition(obj, "Failed")
    if failed_condition is not None and _get(failed_condition, "status") == "True":
        return "Degraded", _condition_sentence("The Job failed", failed_condition)

    complete_condition = _condition(obj, "Complete")
    if complete_condition is not None and _get(complete_condition, "status") == "True":
        return "Healthy", None

    if desired is not None and succeeded is not None and desired > 0 and succeeded >= desired:
        return "Healthy", None

    if active:
        target = desired if desired is not None else "?"
        return "Progressing", (
            f"{succeeded or 0} of {target} completions finished; {active} pod(s) running."
        )
    if failed:
        # Retries left — the Job has not been marked Failed by its controller, so
        # calling it Degraded here would contradict the condition it will write
        # if the backoff limit is actually reached.
        return "Progressing", (
            f"{failed} pod(s) have failed; the Job is retrying within its backoff limit."
        )
    return "Progressing", "No pods are running yet; the Job controller has not started one."


def _cronjob_status(obj: Any) -> tuple[str, str | None]:
    active = _dig(obj, "status", "active", default=[]) or []
    last_schedule = _dig(obj, "status", "last_schedule_time")
    last_success = _dig(obj, "status", "last_successful_time")

    if active:
        return "Progressing", f"{len(active)} Job(s) from this CronJob are running."
    if last_schedule is not None and last_success is None:
        # Positive evidence, not an inference from silence: the controller says
        # it scheduled a Job and never recorded a success.
        return "Degraded", (
            "A Job has been scheduled from this CronJob but none has ever completed "
            "successfully. Look at the Jobs it created."
        )
    return "Healthy", None


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

def workload_row(obj: Any, kind: str, *, restarts_24h: int | None, now: datetime | None = None) -> dict:
    """One §6 row.

    ``restarts_24h`` is passed in rather than computed here because it comes from
    a *different* API read (pods) that can fail on its own. Keeping it a
    parameter means the caller that failed to read pods has to decide what to
    pass, and the only honest thing to pass is ``None``.
    """
    now = now or _now()
    metadata = _get(obj, "metadata")
    selector = _dig(obj, "spec", "selector")
    counts_observed, _ = controller_observed(kind, obj)
    status, reason = derive_status(kind, obj)

    match_labels, _expressions = _selector_terms(selector)

    return {
        "kind": kind,
        "name": _get(metadata, "name"),
        "namespace": _get(metadata, "namespace"),
        "replicas": replica_counts(kind, obj, counts_observed),
        "images": container_images(obj, kind),
        # matchLabels only: the row's selector is what the UI puts in a "find the
        # pods" link, and a matchExpressions term has no equivalent in that shape.
        # An expression-only selector therefore yields {} — which is why the
        # selector itself, not this field, drives pod attribution.
        "selector": match_labels,
        "labels": dict(_get(metadata, "labels", default={}) or {}),
        "age_seconds": _age_seconds(_get(metadata, "creation_timestamp"), now=now),
        "status": status,
        "status_reason": reason,
        "restarts_24h": restarts_24h,
        "suspended": bool(_dig(obj, "spec", "suspend")) if kind in ("Job", "CronJob") else None,
        "schedule": _dig(obj, "spec", "schedule") if kind == "CronJob" else None,
        "last_schedule": (
            _timestamp(_dig(obj, "status", "last_schedule_time")) if kind == "CronJob" else None
        ),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Cluster reads
# --------------------------------------------------------------------------- #

class _api_errors:
    """Turn an ``ApiException`` raised inside the block into a typed AdminError.

    A context manager rather than a try/except at every call site: the point is
    that the resulting error carries the *target* (verb, group, resource,
    namespace, name), and a call site that has to retype that context is a call
    site that will eventually raise an ``rbac_denied`` naming nothing. Nested
    inside ``collect``, this is also what gives the ``unavailable`` entry its
    reason — ``forbidden`` rather than a generic ``unreachable``.
    """

    def __init__(self, **context: Any) -> None:
        self.context = {key: value for key, value in context.items() if value is not None}

    def __enter__(self) -> "_api_errors":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if isinstance(exc, ApiException):
            raise from_api_exception(exc, context=self.context) from exc
        return False


def _list_objects(spec: KindSpec, namespace: str | None) -> list[Any]:
    api = spec.client()
    if namespace:
        result = getattr(api, spec.list_namespaced)(namespace)
    else:
        result = getattr(api, spec.list_all)()
    return list(_get(result, "items", default=[]) or [])


def _list_pods(namespace: str | None, *, label_selector: str | None = None) -> list[Any]:
    core = get_core_v1()
    if namespace:
        if label_selector:
            result = core.list_namespaced_pod(namespace, label_selector=label_selector)
        else:
            result = core.list_namespaced_pod(namespace)
    elif label_selector:
        result = core.list_pod_for_all_namespaces(label_selector=label_selector)
    else:
        result = core.list_pod_for_all_namespaces()
    return list(_get(result, "items", default=[]) or [])


def _build_pod_index(namespace: str | None, unavailable: list[dict], *, now: datetime) -> PodIndex | None:
    """One pod listing, or None if we could not do it.

    None is the whole point of the return type. Every row's ``restarts_24h``
    hangs off this, and the difference between "no restarts" and "we were not
    allowed to look at pods" is the difference between a clean dashboard and a
    dashboard that is lying by omission. The ``unavailable`` entry recorded by
    ``collect`` is what makes the resulting nulls explicable in the UI.
    """
    pods: list[Any] | None = None
    with collect(unavailable, "", "pods", namespace=namespace), _api_errors(
        verb="list", group="", resource="pods", namespace=namespace
    ):
        pods = _list_pods(namespace)
    if pods is None:
        return None
    return PodIndex(pods, now=now)


def _cronjob_job_owners(
    namespace: str | None,
    unavailable: list[dict],
    *,
    known_jobs: list[Any] | None,
) -> dict[str, set[str]] | None:
    """Map CronJob uid -> the uids of the Jobs it owns, or None if unreadable.

    A CronJob has no selector, so its pods can only be reached through the Jobs
    it created. When the caller already listed Jobs (the unfiltered workload
    listing does), those objects are reused instead of listing them a second
    time — the same read, billed twice, is also two chances to disagree.
    """
    jobs = known_jobs
    if jobs is None:
        with collect(unavailable, "batch", "jobs", namespace=namespace), _api_errors(
            verb="list", group="batch", resource="jobs", namespace=namespace
        ):
            jobs = _list_objects(SPEC_BY_KIND["Job"], namespace)
    if jobs is None:
        return None

    owners: dict[str, set[str]] = {}
    for job in jobs:
        metadata = _get(job, "metadata")
        job_uid = _get(metadata, "uid")
        if not job_uid:
            continue
        for ref in _get(metadata, "owner_references", default=[]) or []:
            if _get(ref, "kind") == "CronJob" and _get(ref, "uid"):
                owners.setdefault(str(_get(ref, "uid")), set()).add(str(job_uid))
    return owners


def _restarts_for(
    obj: Any,
    kind: str,
    *,
    index: PodIndex | None,
    cronjob_owners: dict[str, set[str]] | None,
) -> int | None:
    """``restarts_24h`` for one workload, or None when it could not be determined."""
    if index is None:
        return None

    if kind == "CronJob":
        if cronjob_owners is None:
            return None
        uid = _dig(obj, "metadata", "uid")
        if not uid:
            return None
        return index.restarts_for_owners(cronjob_owners.get(str(uid), set()))

    return index.restarts_for_selector(
        _dig(obj, "metadata", "namespace"), _dig(obj, "spec", "selector")
    )


def list_workloads(*, namespace: str | None = None, kind: str | None = None) -> dict:
    """``GET /api/workloads`` (§6): every workload of every kind, as one table.

    Six independent listings, each collected on its own: a ServiceAccount with no
    ``list`` on ``batch/cronjobs`` still gets its Deployments, and the row it
    cannot see is named in ``unavailable[]`` instead of being absent without
    explanation.

    The envelope's ``continue``/``remaining`` are always null here, and that is
    deliberate rather than unimplemented. A Kubernetes list cursor belongs to one
    listing of one resource; six listings cannot share one, and returning any one
    of them as *the* cursor would silently truncate the other five on the next
    page. Narrowing is done with ``namespace`` and ``kind``, which are cursors an
    operator can reason about.
    """
    specs = resolve_kind_filter(kind)
    now = _now()
    unavailable: list[dict] = []

    index = _build_pod_index(namespace, unavailable, now=now)

    listed: dict[str, list[Any] | None] = {}
    for spec in specs:
        objects: list[Any] | None = None
        with collect(unavailable, spec.group, spec.plural, namespace=namespace), _api_errors(
            verb="list", group=spec.group, resource=spec.plural, namespace=namespace
        ):
            objects = _list_objects(spec, namespace)
        listed[spec.plural] = objects

    cronjob_owners: dict[str, set[str]] | None = None
    if any(spec.kind == "CronJob" for spec in specs) and index is not None:
        cronjob_owners = _cronjob_job_owners(
            namespace, unavailable, known_jobs=listed.get("jobs"),
        )

    rows: list[dict] = []
    for spec in specs:
        for obj in listed.get(spec.plural) or []:
            rows.append(workload_row(
                obj,
                spec.kind,
                restarts_24h=_restarts_for(
                    obj, spec.kind, index=index, cronjob_owners=cronjob_owners,
                ),
                now=now,
            ))

    # Stable order across calls. The six listings are concatenated, so the
    # API server's own ordering only holds within a kind; without a total order
    # the table reshuffles under the operator's cursor on every refresh.
    rows.sort(key=lambda row: (row["namespace"] or "", row["kind"], row["name"] or ""))

    return envelope(rows, unavailable=unavailable)


# --------------------------------------------------------------------------- #
# Detail
# --------------------------------------------------------------------------- #

def _conditions_block(obj: Any) -> list[dict]:
    return [
        {
            "type": _get(condition, "type"),
            "status": _get(condition, "status"),
            "reason": _get(condition, "reason"),
            "message": _get(condition, "message"),
            "lastTransitionTime": _timestamp(_get(condition, "last_transition_time")),
        }
        for condition in (_dig(obj, "status", "conditions", default=[]) or [])
    ]


def _rollout_block(obj: Any, kind: str) -> dict | None:
    """The §6 detail ``rollout`` block, or None for kinds that do not roll out.

    ``revision`` is an integer or null and never a string. A Deployment's current
    revision is the ``deployment.kubernetes.io/revision`` annotation, which is a
    number; a StatefulSet's and a DaemonSet's is a ControllerRevision *name* (a
    hash), which is not. Putting the hash in an int-shaped field would make the
    two kinds' revisions compare and sort against each other in the UI as if they
    were the same thing. The numeric history for those kinds is behind
    ``GET /api/workloads/{plural}/{ns}/{name}/rollout``, which reads the
    ControllerRevisions and their real ``revision`` numbers.
    """
    if kind == "Deployment":
        strategy = _dig(obj, "spec", "strategy")
        rolling = _get(strategy, "rolling_update")
        raw_revision = (_dig(obj, "metadata", "annotations", default={}) or {}).get(
            "deployment.kubernetes.io/revision"
        )
        revision: int | None
        try:
            revision = int(raw_revision) if raw_revision is not None else None
        except (TypeError, ValueError):
            revision = None
        return {
            "revision": revision,
            "strategy": _get(strategy, "type"),
            "maxSurge": _get(rolling, "max_surge"),
            "maxUnavailable": _get(rolling, "max_unavailable"),
        }

    if kind in ("StatefulSet", "DaemonSet"):
        strategy = _dig(obj, "spec", "update_strategy")
        rolling = _get(strategy, "rolling_update")
        return {
            "revision": None,
            "strategy": _get(strategy, "type"),
            "maxSurge": _get(rolling, "max_surge"),
            "maxUnavailable": _get(rolling, "max_unavailable"),
        }

    return None


def _service_block(service: Any) -> dict:
    return {
        "name": _dig(service, "metadata", "name"),
        "type": _dig(service, "spec", "type"),
        "clusterIP": _dig(service, "spec", "cluster_ip"),
        "ports": [
            {
                "name": _get(port, "name"),
                "port": _get(port, "port"),
                "targetPort": _get(port, "target_port"),
                "protocol": _get(port, "protocol") or "TCP",
                "nodePort": _get(port, "node_port"),
            }
            for port in (_dig(service, "spec", "ports", default=[]) or [])
        ],
    }


def _matching_services(services: Iterable[Any], template_labels: dict[str, str]) -> list[dict]:
    """Services whose selector picks this workload's pods.

    A Service with no selector is excluded: it is backed by manually managed
    EndpointSlices, so it does not select these pods — and listing it here would
    tell an operator that traffic reaches a workload it does not reach.
    """
    matched = []
    for service in services:
        selector = _dig(service, "spec", "selector", default={}) or {}
        if not selector:
            continue
        if all(template_labels.get(key) == value for key, value in selector.items()):
            matched.append(_service_block(service))
    return matched


def _pod_rows(pods: Iterable[Any]) -> list[dict]:
    """Shared PodRow shaping, with ``phase_detail`` guaranteed present.

    ``setdefault`` rather than assignment: ``pod_row`` owns the row shape and may
    already carry the field. This only guarantees that a pod whose phase says
    ``Running`` while its container is in CrashLoopBackOff cannot reach the UI
    described only as Running.
    """
    rows = []
    for pod in pods:
        row = pod_row(pod)
        if isinstance(row, dict) and "phase_detail" not in row:
            row["phase_detail"] = phase_detail(pod)
        rows.append(row)
    return rows


def get_workload_detail(plural: str, namespace: str, name: str) -> dict:
    """``GET /api/workloads/{plural}/{namespace}/{name}`` (§6).

    The workload itself is read *outside* ``collect``: if that read fails there
    is no detail page to degrade, and the operator needs the real error (404,
    403 naming the verb) rather than an empty shell with a footnote.

    Everything else — pods, Services, the CronJob's Jobs — is read independently
    inside ``collect``, so a namespace where the console may read Deployments but
    not Services still renders the workload, with one line saying which question
    went unanswered.
    """
    spec = resolve_plural(plural)
    now = _now()
    unavailable: list[dict] = []

    api = spec.client()
    with _api_errors(
        verb="get", group=spec.group, resource=spec.plural, namespace=namespace, name=name,
    ):
        obj = getattr(api, spec.read_namespaced)(name, namespace)

    template = pod_template(obj, spec.kind)
    template_labels = dict(_dig(template, "metadata", "labels", default={}) or {})
    selector = _dig(obj, "spec", "selector")

    # -- pods -------------------------------------------------------------
    pods: list[Any] | None = None
    if spec.kind == "CronJob":
        jobs: list[Any] | None = None
        with collect(unavailable, "batch", "jobs", namespace=namespace), _api_errors(
            verb="list", group="batch", resource="jobs", namespace=namespace,
        ):
            jobs = _list_objects(SPEC_BY_KIND["Job"], namespace)
        if jobs is not None:
            uid = str(_dig(obj, "metadata", "uid") or "")
            job_uids = {
                str(_dig(job, "metadata", "uid"))
                for job in jobs
                if any(
                    _get(ref, "kind") == "CronJob" and str(_get(ref, "uid")) == uid
                    for ref in (_dig(job, "metadata", "owner_references", default=[]) or [])
                )
            }
            with collect(unavailable, "", "pods", namespace=namespace), _api_errors(
                verb="list", group="", resource="pods", namespace=namespace,
            ):
                # No label selector exists for a CronJob's pods; ownership is the
                # only true link, and it is only visible on the pod objects.
                pods = [
                    pod for pod in _list_pods(namespace)
                    if any(
                        str(_get(ref, "uid")) in job_uids
                        for ref in (_dig(pod, "metadata", "owner_references", default=[]) or [])
                    )
                ]
    else:
        label_selector = label_selector_string(selector)
        if label_selector is not None:
            with collect(unavailable, "", "pods", namespace=namespace), _api_errors(
                verb="list", group="", resource="pods", namespace=namespace,
            ):
                pods = _list_pods(namespace, label_selector=label_selector)
        else:
            # No query was built, so no listing happened — and `pods` must stay
            # `None` and say so. Rendering the empty list here is the §0.1 defect
            # in its purest form: the page prints "this workload has no pods"
            # over a workload that may have hundreds, and an operator reading it
            # during an incident concludes the ReplicaSet is making none.
            #
            # Two ways to get here, and the sentence distinguishes them because
            # they need different things done about them.
            unavailable.append(unavailable_entry(
                "", "pods", "unrenderable",
                namespace=namespace,
                detail=(
                    "This workload's selector matches every pod in the "
                    "namespace, so the pods listed under it would not be its "
                    "own. Kubernetes treats an empty LabelSelector as selecting "
                    "everything."
                    if selector is not None and selector_is_empty(selector)
                    else "This workload's selector uses a matchExpressions "
                    "operator this console cannot render as a labelSelector "
                    "query, so its pods were not listed. Every pod in the "
                    "namespace is on the Pods page."
                ),
            ))

    restarts = None if pods is None else sum(restarts_in_window(pod, now=now) for pod in pods)

    # -- services ---------------------------------------------------------
    services: list[Any] | None = None
    with collect(unavailable, "", "services", namespace=namespace), _api_errors(
        verb="list", group="", resource="services", namespace=namespace,
    ):
        services = list(_get(get_core_v1().list_namespaced_service(namespace), "items", default=[]) or [])

    return {
        "workload": workload_row(obj, spec.kind, restarts_24h=restarts, now=now),
        "spec": _spec_block(obj, spec.kind),
        # `[]` even when nothing was listed, paired with `partial` and the
        # `unavailable` entry that names the pod read — the convention this
        # payload already follows, and what §1.1 requires an empty list to be
        # accompanied by. The banner is the signal, not the list's type.
        "pods": _pod_rows(pods or []),
        "conditions": _conditions_block(obj),
        "services": _matching_services(services or [], template_labels),
        "rollout": _rollout_block(obj, spec.kind),
        # `partial` alongside `unavailable` for the same reason §1.2 carries it:
        # the frontend renders its persistent banner off this one boolean, and a
        # detail page that degraded silently is exactly what the banner exists
        # to prevent.
        "partial": bool(unavailable),
        "unavailable": unavailable,
    }


__all__ = [
    "KIND_TO_PLURAL",
    "KindSpec",
    "PLURAL_TO_KIND",
    "PodIndex",
    "RESTART_WINDOW",
    "SPEC_BY_KIND",
    "SPEC_BY_PLURAL",
    "WORKLOAD_PLURALS",
    "container_images",
    "controller_observed",
    "derive_status",
    "get_workload_detail",
    "label_selector_string",
    "list_workloads",
    "pod_template",
    "replica_counts",
    "resolve_kind_filter",
    "resolve_plural",
    "restarts_in_window",
    "workload_row",
]
