"""
One pod, read three ways (§7.5–§7.7) — the detail, its environment, its usage.

The §4 generic reader already returns a pod object, and §6's ``pod_row`` already
shapes the columns a table needs. This module exists for the three questions a
*page* about one pod asks that neither answers, and each of the three is a place
where the obvious implementation would say something untrue:

**The detail.** ``pod_row`` deliberately carries no init containers — adding them
would change what the Logs and Terminal pickers offer — and it carries no
per-container resources, ports or last-termination reason. Those are exactly the
fields somebody looks at when a pod is misbehaving: "it restarted 14 times" is
the symptom, ``lastState.terminated.reason: OOMKilled`` is the answer. So the
detail *enriches* the row rather than rebuilding it: ``ready``, ``restarts``,
``state`` and ``kind`` stay the shaper's, because a second implementation of the
ephemeral-container rules would drift and the drift would show up as one page
disagreeing with another about whether a pod is healthy.

**The environment.** A container's environment is not in the pod spec. Half of it
is a set of *references* — to ConfigMap keys, to Secret keys, to fields the
kubelet substitutes at start — and a viewer that printed only ``env[].value``
would show an operator four variables out of thirty and imply the rest do not
exist. So every variable is reported with where it comes from, and the value is
reported with a state saying whether we have it, cannot have it, or refused to
carry it:

* ``literal`` — written in the pod spec, and already visible in the YAML tab.
* ``resolved`` — read out of a ConfigMap.
* ``withheld`` — it comes from a Secret. **This endpoint never returns Secret
  values.** Key names are not the secret (§4 already relies on that distinction
  for the Secrets table); the values are, and an environment viewer is the last
  place they should turn up, because it is the screen people share.
* ``absent`` — the ConfigMap was read and has no such key. Different from both
  of the above: unless the reference is ``optional``, the kubelet refuses to
  start the container, and reporting it as unreadable would send somebody to
  check RBAC that is already correct.
* ``unreadable`` — the ConfigMap or Secret could not be read. Named in
  ``unavailable[]``, never rendered as an empty value, because "this variable is
  empty" and "we could not look" send an operator to two different places.
* ``runtime`` — a ``fieldRef`` or ``resourceFieldRef``. The kubelet computes it
  when the container starts and the API server never stores the result, so there
  is no value to show and inventing a plausible one would be a guess about what
  is running inside somebody's container.

**The usage.** ``metrics.k8s.io`` is an aggregated API that a great many clusters
do not serve at all, and on the ones that do, a pod that started ten seconds ago
has no sample yet. Both of those are *ordinary facts*, not failures — and both
must produce ``None``, never ``0``. A metrics tab showing a pod at 0 cores and
0 bytes is the same defect as a node showing zero requested cores: it reads as
idle, and idle is what gets something turned off.
"""

from __future__ import annotations

import logging
from typing import Any

from kubernetes.client.rest import ApiException

from app.admin.apply import read_object
from app.errors import NotFound
from app.k8s.client import get_core_v1
from app.resources import catalog, shaping
from app.resources.envelope import collect, envelope
from app.resources.shaping import get_field

logger = logging.getLogger(__name__)

#: The aggregated API metrics-server serves. Absent on a great many clusters,
#: which is an ordinary fact rather than a failure — see :func:`get_pod_metrics`.
_METRICS_GROUP = "metrics.k8s.io"

#: The version to *name* when the cluster serves none of the group. Only ever
#: used to build the diagnosis: the version actually read is whatever discovery
#: reports, so a cluster that ships a future ``v1`` is read rather than refused.
#: See :func:`_metrics_version`.
_METRICS_FALLBACK_VERSION = "v1beta1"


# --------------------------------------------------------------------------- #
# The pod itself
# --------------------------------------------------------------------------- #

def _read_pod(namespace: str, name: str) -> dict[str, Any]:
    """The live pod, as a plain dict.

    Through :func:`app.admin.apply.read_object` rather than the generic reader,
    for the reason that function documents: group, version and scope are already
    known here, so a discovery round trip to re-learn "core/v1 pods is
    namespaced" would be paid on every request for nothing. ``read_object`` also
    maps the API server's failure to the §1.3 vocabulary, so a denial names
    ``get pods`` rather than arriving as a bare "forbidden".
    """
    return read_object("", "v1", "pods", name, namespace=namespace)


def _ports(spec: Any) -> list[dict[str, Any]]:
    """``containerPort`` entries of one container spec, named where they are named."""
    return [
        {
            "name": get_field(port, "name"),
            "container_port": get_field(port, "containerPort"),
            "protocol": get_field(port, "protocol") or "TCP",
            "host_port": get_field(port, "hostPort"),
        }
        for port in (get_field(spec, "ports", default=[]) or [])
    ]


def _quantities(spec: Any, key: str) -> dict[str, Any] | None:
    """``resources.requests`` or ``resources.limits`` as the strings the spec holds.

    ``None`` when the container declares none — which is a real and important
    fact about a container, and a different one from "it requests nothing". An
    empty dict rendered as ``cpu: 0`` would describe a BestEffort container as
    one that asked for nothing and got it, when what actually happens is that it
    is first in line to be evicted.
    """
    values = get_field(spec, "resources", key)
    if not values:
        return None
    return {str(k): str(v) for k, v in dict(values).items()}


def _last_terminated(status: Any) -> dict[str, Any] | None:
    """``lastState.terminated`` — why the previous instance of this container died.

    This is the field the Restarts column is a symptom of. ``OOMKilled`` here is
    the whole diagnosis for a pod somebody is about to go read logs for, and it
    is not on §6's row.
    """
    terminated = get_field(status, "lastState", "terminated")
    if terminated is None:
        return None
    return {
        "reason": get_field(terminated, "reason"),
        "exit_code": get_field(terminated, "exitCode"),
        "signal": get_field(terminated, "signal"),
        "started_at": shaping.rfc3339(get_field(terminated, "startedAt")),
        "finished_at": shaping.rfc3339(get_field(terminated, "finishedAt")),
        "message": get_field(terminated, "message"),
    }


def _index_by_name(entries: Any) -> dict[str, Any]:
    """``{name: entry}`` for a list of Kubernetes objects that have names."""
    index: dict[str, Any] = {}
    for entry in entries or []:
        key = get_field(entry, "name")
        if key:
            index[str(key)] = entry
    return index


def _enrich(container: dict[str, Any], spec: Any, status: Any) -> dict[str, Any]:
    """Add the detail fields to one §6 container entry, in place.

    The row's own keys are left exactly as ``pod_row`` computed them. That is the
    point of enriching rather than rebuilding: ``ready``, ``restarts``, ``state``
    and ``kind`` encode rules about ephemeral containers that this page must not
    restate in its own words.
    """
    container["image_id"] = get_field(status, "imageID")
    container["container_id"] = get_field(status, "containerID")
    container["started_at"] = shaping.rfc3339(get_field(status, "state", "running", "startedAt"))
    container["last_terminated"] = _last_terminated(status)
    container["ports"] = _ports(spec)
    container["requests"] = _quantities(spec, "requests")
    container["limits"] = _quantities(spec, "limits")
    container["command"] = list(get_field(spec, "command", default=[]) or []) or None
    container["args"] = list(get_field(spec, "args", default=[]) or []) or None
    return container


def _init_container_rows(pod: Any) -> list[dict[str, Any]]:
    """The init containers, shaped like the §6 container entries.

    Separate from ``containers`` rather than appended to it, because they are a
    different thing: an init container that is ``Terminated`` with exit code 0 is
    a *success*, and the same words on an app container are an outage. Merging
    the two lists would put a red-looking row on every healthy pod that has ever
    run a migration.
    """
    statuses = _index_by_name(get_field(pod, "status", "initContainerStatuses", default=[]))
    rows = []
    for spec in get_field(pod, "spec", "initContainers", default=[]) or []:
        name = get_field(spec, "name")
        status = statuses.get(str(name)) if name else None
        state, reason = shaping.container_state(status)
        row = {
            "name": name,
            "image": get_field(spec, "image"),
            # An init container is never "ready" in the readiness-probe sense;
            # the kubelet reports `ready: true` for one that completed. Passed
            # through as the API states it rather than reinterpreted.
            "ready": bool(get_field(status, "ready")),
            "restarts": int(get_field(status, "restartCount", default=0) or 0),
            "state": state,
            "reason": reason,
            "kind": "init",
        }
        rows.append(_enrich(row, spec, status))
    return rows


def _conditions(pod: Any) -> list[dict[str, Any]]:
    """``status.conditions``, with ``status`` left as the tri-state string it is.

    ``"True"``/``"False"``/``"Unknown"`` is not a boolean, and coercing it to one
    turns "the kubelet has not said" into "no". A pod whose ``Ready`` condition is
    ``Unknown`` is a pod on a node that stopped reporting — which is the single
    most important thing this list can carry.
    """
    return [
        {
            "type": get_field(condition, "type"),
            "status": get_field(condition, "status"),
            "reason": get_field(condition, "reason"),
            "message": get_field(condition, "message"),
            "last_transition": shaping.rfc3339(get_field(condition, "lastTransitionTime")),
        }
        for condition in (get_field(pod, "status", "conditions", default=[]) or [])
    ]


def _volumes(pod: Any) -> list[dict[str, Any]]:
    """The pod's volumes, each reduced to its source kind and the object it names.

    ``kind`` is the spec key the volume is defined under (``configMap``,
    ``secret``, ``persistentVolumeClaim``, …) and ``source`` is the name of the
    object it points at, where the source names one. Enough to answer "where does
    this pod get its configuration from" without reprinting the manifest.
    """
    rows = []
    for volume in get_field(pod, "spec", "volumes", default=[]) or []:
        # `read_object` hands back the API server's JSON, so a volume is a dict
        # whose single non-`name` key *is* its source kind. Anything else here
        # would be a stand-in from a test, and reporting an unknown kind is
        # better than raising on one.
        fields = volume if isinstance(volume, dict) else {}
        kind = None
        source = None
        for key, value in fields.items():
            if key == "name" or value is None:
                continue
            kind = key
            source = (
                get_field(value, "claimName")
                or get_field(value, "secretName")
                or get_field(value, "name")
            )
            break
        rows.append({"name": get_field(volume, "name"), "kind": kind, "source": source})
    return rows


def get_pod(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/pods/{namespace}/{name}`` (§7.5) — the §6 row plus the detail.

    The pod is the primary read, so a failure raises rather than degrading: a
    detail page for a pod we could not read has nothing honest to render, and an
    empty shell with a name at the top of it looks like a pod that exists and is
    empty.

    ``unavailable`` is present and empty for the same reason every other endpoint
    carries it — the frontend reads it unconditionally — and it stays empty
    because this endpoint makes exactly one read.
    """
    pod = _read_pod(namespace, name)

    row = shaping.pod_row(pod)
    spec_index = _index_by_name(get_field(pod, "spec", "containers", default=[]))
    spec_index.update(_index_by_name(get_field(pod, "spec", "ephemeralContainers", default=[])))
    status_index = _index_by_name(get_field(pod, "status", "containerStatuses", default=[]))
    status_index.update(
        _index_by_name(get_field(pod, "status", "ephemeralContainerStatuses", default=[]))
    )
    for container in row["containers"]:
        key = str(container.get("name"))
        _enrich(container, spec_index.get(key), status_index.get(key))

    row["init_containers"] = _init_container_rows(pod)
    row["conditions"] = _conditions(pod)
    row["volumes"] = _volumes(pod)
    row["uid"] = get_field(pod, "metadata", "uid")
    row["resource_version"] = get_field(pod, "metadata", "resourceVersion")
    row["created_at"] = shaping.rfc3339(get_field(pod, "metadata", "creationTimestamp"))
    row["deleted_at"] = shaping.rfc3339(get_field(pod, "metadata", "deletionTimestamp"))
    row["labels"] = dict(get_field(pod, "metadata", "labels", default={}) or {})
    row["annotations"] = dict(get_field(pod, "metadata", "annotations", default={}) or {})
    row["service_account"] = (
        get_field(pod, "spec", "serviceAccountName") or get_field(pod, "spec", "serviceAccount")
    )
    row["restart_policy"] = get_field(pod, "spec", "restartPolicy")
    row["priority_class"] = get_field(pod, "spec", "priorityClassName")
    row["node_selector"] = dict(get_field(pod, "spec", "nodeSelector", default={}) or {})
    row["host_network"] = bool(get_field(pod, "spec", "hostNetwork"))
    row["host_ip"] = get_field(pod, "status", "hostIP")
    row["start_time"] = shaping.rfc3339(get_field(pod, "status", "startTime"))
    row["status_reason"] = get_field(pod, "status", "reason")
    row["status_message"] = get_field(pod, "status", "message")
    row["unavailable"] = []
    row["partial"] = False
    return row


# --------------------------------------------------------------------------- #
# The environment
# --------------------------------------------------------------------------- #

#: ``value_state`` vocabulary. Closed, and the frontend branches on it — see the
#: module docstring for what each one promises and, more importantly, what each
#: one refuses to promise.
LITERAL = "literal"
RESOLVED = "resolved"
WITHHELD = "withheld"
ABSENT = "absent"
UNREADABLE = "unreadable"
RUNTIME = "runtime"


class _SourceCache:
    """Reads each referenced ConfigMap and Secret once, and records the failures.

    One pod can name the same ConfigMap in eight containers. Reading it eight
    times would be eight chances to be denied and eight identical lines in the
    banner; reading it once and remembering the *failure* as well as the success
    is what keeps ``unavailable[]`` to one entry per object that could not be
    read.

    **Secrets are read for their key names and never for their values.** The
    value never leaves this class — ``secret_keys`` returns names only, and there
    is no method that returns anything else.
    """

    def __init__(self, namespace: str, unavailable: list[dict[str, Any]]):
        self._namespace = namespace
        self._unavailable = unavailable
        self._configmaps: dict[str, dict[str, str] | None] = {}
        self._secret_keys: dict[str, list[str] | None] = {}

    def configmap(self, name: str) -> dict[str, str] | None:
        """``{key: value}`` of a ConfigMap, or ``None`` if it could not be read."""
        if name in self._configmaps:
            return self._configmaps[name]
        data: dict[str, str] | None = None
        with collect(self._unavailable, "", "configmaps", namespace=self._namespace):
            obj = get_core_v1().read_namespaced_config_map(name, self._namespace)
            merged = dict(get_field(obj, "data", default={}) or {})
            # `binaryData` values are base64 and are not environment material —
            # the kubelet refuses to build an env var from one. The keys are
            # listed so the viewer does not imply the ConfigMap has fewer keys
            # than it does, with no value, which is the truth about them here.
            for key in get_field(obj, "binaryData", default={}) or {}:
                merged.setdefault(str(key), "")
            data = {str(k): "" if v is None else str(v) for k, v in merged.items()}
        self._configmaps[name] = data
        return data

    def secret_keys(self, name: str) -> list[str] | None:
        """Key names of a Secret, or ``None`` if it could not be read.

        Names only, deliberately and permanently. The values are read off the
        wire by the client library and dropped here without ever being copied
        into a return value — see the class docstring.
        """
        if name in self._secret_keys:
            return self._secret_keys[name]
        keys: list[str] | None = None
        with collect(self._unavailable, "", "secrets", namespace=self._namespace):
            obj = get_core_v1().read_namespaced_secret(name, self._namespace)
            names = set(get_field(obj, "data", default={}) or {})
            names.update(get_field(obj, "stringData", default={}) or {})
            keys = sorted(str(key) for key in names)
        self._secret_keys[name] = keys
        return keys


def _variable(
    *,
    name: str | None,
    value: str | None,
    state: str,
    source: dict[str, Any] | None,
    all_keys: bool = False,
) -> dict[str, Any]:
    """One row of the environment table."""
    return {
        "name": name,
        "value": value,
        "value_state": state,
        "source": source,
        # True only for the one row that stands in for an `envFrom` import whose
        # object could not be read: we know a set of variables is coming from it
        # and we cannot name them. Rendering that as zero variables would be the
        # empty-is-never-blind failure with the blank left inside a container's
        # environment, where nobody would look for it.
        "all_keys": all_keys,
        "overridden": False,
    }


def _source_ref(kind: str, ref: Any, key: str | None = None) -> dict[str, Any]:
    """The ``source`` block for a reference-valued variable."""
    return {
        "kind": kind,
        "name": get_field(ref, "name"),
        "key": key,
        "optional": get_field(ref, "optional"),
    }


def _from_env_from(spec: Any, cache: _SourceCache) -> list[dict[str, Any]]:
    """Every variable a container imports wholesale through ``envFrom``.

    Emitted before the explicit ``env`` entries because that is the order the
    kubelet applies them in, and an explicit ``env`` of the same name wins. The
    losing row is kept and marked ``overridden`` rather than dropped: "this
    ConfigMap defines DATABASE_URL and something else is overriding it" is the
    answer to a question people spend an afternoon on.
    """
    rows: list[dict[str, Any]] = []
    for entry in get_field(spec, "envFrom", default=[]) or []:
        prefix = str(get_field(entry, "prefix") or "")
        config_ref = get_field(entry, "configMapRef")
        secret_ref = get_field(entry, "secretRef")

        if config_ref is not None:
            ref_name = str(get_field(config_ref, "name") or "")
            data = cache.configmap(ref_name) if ref_name else None
            if data is None:
                rows.append(_variable(
                    name=None, value=None, state=UNREADABLE,
                    source=_source_ref("configMapRef", config_ref), all_keys=True,
                ))
                continue
            for key in sorted(data):
                rows.append(_variable(
                    name=f"{prefix}{key}", value=data[key], state=RESOLVED,
                    source=_source_ref("configMapRef", config_ref, key),
                ))

        if secret_ref is not None:
            ref_name = str(get_field(secret_ref, "name") or "")
            keys = cache.secret_keys(ref_name) if ref_name else None
            if keys is None:
                rows.append(_variable(
                    name=None, value=None, state=UNREADABLE,
                    source=_source_ref("secretRef", secret_ref), all_keys=True,
                ))
                continue
            for key in keys:
                rows.append(_variable(
                    name=f"{prefix}{key}", value=None, state=WITHHELD,
                    source=_source_ref("secretRef", secret_ref, key),
                ))
    return rows


def _from_env(spec: Any, cache: _SourceCache) -> list[dict[str, Any]]:
    """Every variable a container declares explicitly in ``env``."""
    rows: list[dict[str, Any]] = []
    for entry in get_field(spec, "env", default=[]) or []:
        name = get_field(entry, "name")
        value_from = get_field(entry, "valueFrom")

        if value_from is None:
            # A literal. `value` may legitimately be absent, which Kubernetes
            # reads as the empty string — reported as "" rather than null, since
            # the container really will see an empty variable.
            raw = get_field(entry, "value")
            rows.append(_variable(
                name=name, value="" if raw is None else str(raw), state=LITERAL, source=None,
            ))
            continue

        config_key = get_field(value_from, "configMapKeyRef")
        if config_key is not None:
            key = str(get_field(config_key, "key") or "")
            ref_name = str(get_field(config_key, "name") or "")
            data = cache.configmap(ref_name) if ref_name else None
            if data is None:
                state = UNREADABLE
            elif key in data:
                state = RESOLVED
            else:
                # The ConfigMap was read and does not have this key. That is a
                # fact about the cluster, not a failure of ours, and it is worth
                # its own state: unless the reference is `optional`, the kubelet
                # refuses to start this container. Reporting it as `unreadable`
                # would send the operator to check RBAC that is already correct.
                state = ABSENT
            rows.append(_variable(
                name=name,
                value=data.get(key) if state == RESOLVED else None,
                state=state,
                source=_source_ref("configMapKeyRef", config_key, key),
            ))
            continue

        secret_key = get_field(value_from, "secretKeyRef")
        if secret_key is not None:
            # No read at all: the key is already named in the spec, and the only
            # thing a read would add is the value — which this endpoint does not
            # return. Not reading it is also the reason a console with no `get
            # secrets` grant still renders this tab completely.
            rows.append(_variable(
                name=name, value=None, state=WITHHELD,
                source=_source_ref("secretKeyRef", secret_key, str(get_field(secret_key, "key") or "")),
            ))
            continue

        field_ref = get_field(value_from, "fieldRef")
        if field_ref is not None:
            rows.append(_variable(
                name=name, value=None, state=RUNTIME,
                source={
                    "kind": "fieldRef",
                    "name": get_field(field_ref, "fieldPath"),
                    "key": None,
                    "optional": None,
                },
            ))
            continue

        resource_ref = get_field(value_from, "resourceFieldRef")
        if resource_ref is not None:
            rows.append(_variable(
                name=name, value=None, state=RUNTIME,
                source={
                    "kind": "resourceFieldRef",
                    "name": get_field(resource_ref, "resource"),
                    "key": get_field(resource_ref, "containerName"),
                    "optional": None,
                },
            ))
            continue

        # A `valueFrom` spelling this console has not seen. Reported as a
        # variable with an unknown source rather than dropped: a row missing from
        # this table is a variable an operator concludes is not set.
        rows.append(_variable(
            name=name, value=None, state=RUNTIME,
            source={"kind": "unknown", "name": None, "key": None, "optional": None},
        ))
    return rows


def _mark_overrides(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag every row whose name is defined again later in the same container."""
    last_index: dict[str, int] = {}
    for index, row in enumerate(rows):
        if row["name"]:
            last_index[row["name"]] = index
    for index, row in enumerate(rows):
        if row["name"] and last_index[row["name"]] != index:
            row["overridden"] = True
    return rows


def get_pod_environment(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/pods/{namespace}/{name}/environment`` (§7.6).

    One entry per container — the pod's own, its init containers and any §7.4
    debug container — each with the variables that container will see, in the
    order the kubelet builds them.

    ``items: []`` means the pod has no containers, which cannot happen; a
    container with no variables is an entry with an empty ``variables`` list, and
    that *is* a real zero. A source object that could not be read never produces
    a shorter list quietly: it produces a row saying so, and an entry in
    ``unavailable[]``.
    """
    pod = _read_pod(namespace, name)
    unavailable: list[dict[str, Any]] = []
    cache = _SourceCache(namespace, unavailable)

    items = []
    groups = (
        ("init", get_field(pod, "spec", "initContainers", default=[]) or []),
        ("container", get_field(pod, "spec", "containers", default=[]) or []),
        ("ephemeral", get_field(pod, "spec", "ephemeralContainers", default=[]) or []),
    )
    for kind, specs in groups:
        for spec in specs:
            rows = _from_env_from(spec, cache) + _from_env(spec, cache)
            items.append({
                "container": get_field(spec, "name"),
                "kind": kind,
                "variables": _mark_overrides(rows),
            })

    return envelope(items, unavailable=unavailable)


# --------------------------------------------------------------------------- #
# The usage
# --------------------------------------------------------------------------- #

def _metrics_version() -> str:
    """The ``metrics.k8s.io`` version this cluster serves for ``pods``.

    Discovery decides, rather than a constant, because the group is an aggregated
    API served by something the cluster operator installed: pinning ``v1beta1``
    would refuse a cluster that has moved on, while claiming the pin is the
    contract.

    Raises whatever :func:`app.resources.catalog.resolve` raises when the group
    is not there — ``Unsupported`` for a cluster with no metrics-server,
    ``ClusterUnreachable`` or ``RBACDenied`` for a discovery we could not read.
    That distinction is the whole reason this goes through the catalog: "this
    cluster has no metrics" and "we could not find out" send an operator to two
    different places, and only one of them is an install.
    """
    items, _unavailable = catalog.discover()
    candidates = [
        item for item in items
        if item["group"] == _METRICS_GROUP and item["resource"] == "pods"
    ]
    preferred = next((item for item in candidates if item.get("preferred")), None)
    chosen = preferred or (candidates[0] if candidates else None)
    if chosen is not None:
        return str(chosen["version"])
    # Not served, or discovery could not say. `resolve` owns the diagnosis for
    # both, including the blind-spot rule that refuses to call a group absent
    # when it is a group we failed to enumerate.
    catalog.resolve(_METRICS_GROUP, _METRICS_FALLBACK_VERSION, "pods")
    raise AssertionError("catalog.resolve returned for a resource discover() does not list")


def _sample(namespace: str, name: str) -> dict[str, Any]:
    """The ``PodMetrics`` object for one pod, or an :class:`AdminError` saying why not.

    A 404 here is translated deliberately. The API server means "no sample", and
    the pod is demonstrably there — this endpoint read it a moment ago. Left
    alone it would surface as "not found" next to the pod's own name and read as
    "this pod is gone", sending somebody to look for a pod that is running fine.
    """
    version = _metrics_version()
    path = (
        f"/apis/{_METRICS_GROUP}/{version}/namespaces/"
        f"{catalog.quote_segment(namespace)}/pods/{catalog.quote_segment(name)}"
    )
    try:
        return catalog.raw_get(path)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise NotFound(
                "The metrics API has no sample for this pod yet.",
                detail=(
                    "metrics.k8s.io is served by this cluster but has no sample for this pod "
                    "yet — it answered 404. That usually means the pod started within the last "
                    "collection interval, or the kubelet on its node is not reporting."
                ),
                hint="Metrics appear once metrics-server has scraped the node, typically within a minute.",
                context={"group": _METRICS_GROUP, "resource": "pods", "namespace": namespace},
            ) from e
        raise


def _usage(container: Any) -> dict[str, Any]:
    """``{cpu_cores, memory_bytes}`` of one sampled container.

    ``None`` for a quantity that did not parse, never ``0``. A metrics API that
    returned a spelling this console does not understand has told us nothing
    about the container, and zero would say it is idle.
    """
    return {
        "cpu_cores": shaping.parse_cpu_cores(get_field(container, "usage", "cpu")),
        "memory_bytes": shaping.parse_bytes(get_field(container, "usage", "memory")),
    }


def _is_running(status: Any) -> bool:
    """Whether the kubelet reports this container as currently running."""
    return get_field(status, "state", "running") is not None


def _expects_sample(kind: str, status: Any) -> bool:
    """Whether a missing measurement for this container is a *gap*.

    An app container is always expected in the sample: if it is not there, the
    pod totals below cannot be completed.

    An init container is expected only while it is **running**. An ordinary init
    container has terminated by the time anybody opens this page and holds
    nothing, so metrics-server does not report it and requiring it would make
    every pod that has ever run a migration report unknown usage forever. A
    *sidecar* init container (``restartPolicy: Always``, Kubernetes 1.29+) is
    running for the life of the pod and is using real resources, so leaving it
    out of the total would understate the pod by however much the sidecar is
    using — which is exactly the arithmetic this function exists to protect.
    """
    return kind == "container" or _is_running(status)


def _total(items: list[dict[str, Any]], key: str) -> float | int | None:
    """Sum one usage field across the containers, or ``None`` if any is missing.

    Summing the parts we have and presenting that as the pod total would
    understate it by however much the unmeasured container is using, with
    nothing in the response to say a container is missing from the arithmetic.
    An unknown part makes the total unknown; the per-container rows still show
    what was measured.

    A container we do not *expect* a sample for — a terminated init container —
    is skipped rather than treated as missing. See :func:`_expects_sample`.
    """
    values: list[float | int] = []
    for item in items:
        usage = item["usage"]
        if usage is None:
            if item["sample_expected"]:
                return None
            continue
        value = usage[key]
        if value is None:
            return None
        values.append(value)
    return sum(values) if values else None


def get_pod_metrics(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/pods/{namespace}/{name}/metrics`` (§7.7) — live usage per container.

    The **pod** is the primary read: it names the containers and carries the
    requests and limits usage is only meaningful against, and a metrics page for
    a pod we could not read has nothing to say.

    The **sample** is a secondary read, and that is the design decision worth
    stating. A cluster with no metrics-server is the common case, not an error
    (§1.2 has ``unsupported`` for exactly this), and a pod that started seconds
    ago has no sample on a cluster that is working perfectly. Both land in
    ``unavailable[]`` with every ``usage`` left at ``None`` — so the tab still
    shows what each container asked for, says in words why there is no
    measurement, and never draws a pod at zero.

    Each item carries ``sample_expected``, which is what keeps the two kinds of
    ``usage: null`` apart: a container we could not measure, and a terminated
    init container there is nothing to measure. The pod totals are ``None``
    whenever a container of the first kind is missing.
    """
    pod = _read_pod(namespace, name)
    unavailable: list[dict[str, Any]] = []

    sample: dict[str, Any] | None = None
    with collect(unavailable, _METRICS_GROUP, "pods", namespace=namespace):
        sample = _sample(namespace, name)

    measured = _index_by_name(get_field(sample, "containers", default=[]))
    statuses = {
        "init": _index_by_name(get_field(pod, "status", "initContainerStatuses", default=[])),
        "container": _index_by_name(get_field(pod, "status", "containerStatuses", default=[])),
    }

    items = []
    for kind, path in (("init", "initContainers"), ("container", "containers")):
        for spec in get_field(pod, "spec", path, default=[]) or []:
            container_name = str(get_field(spec, "name") or "")
            measurement = measured.get(container_name) if container_name else None
            items.append({
                "container": get_field(spec, "name"),
                "kind": kind,
                # None, not a zeroed pair, when this container has no sample. A
                # container reading "0 cores" is indistinguishable from one that
                # is running and idle, and idle is what gets things turned off.
                "usage": _usage(measurement) if measurement is not None else None,
                # Whether that `None` is a gap or an ordinary absence. A
                # terminated init container holds nothing and metrics-server
                # does not report it; saying so is the difference between "we
                # could not measure this" and "there is nothing here to
                # measure".
                "sample_expected": _expects_sample(kind, statuses[kind].get(container_name)),
                "requests": _quantities(spec, "requests"),
                "limits": _quantities(spec, "limits"),
            })

    result = envelope(items, unavailable=unavailable)
    result["pod"] = {
        # `None` — never a partial sum — when any container we expected a sample
        # for is missing from it. See `_total`.
        "cpu_cores": _total(items, "cpu_cores"),
        "memory_bytes": _total(items, "memory_bytes"),
    }
    result["window_seconds"] = _window_seconds(sample)
    result["timestamp"] = shaping.rfc3339(get_field(sample, "timestamp"))
    return result


def _window_seconds(sample: Any) -> int | None:
    """The sample window, as seconds, from the ISO 8601 duration the API returns.

    ``metrics.k8s.io`` reports ``window`` as a Go duration string (``30s``,
    ``1m0s``). It is worth carrying because a number with no window is a number
    with no meaning — "0.4 cores" over one second and over five minutes are
    different claims — and ``None`` when it cannot be parsed says so rather than
    implying a window we made up.
    """
    text = get_field(sample, "window")
    if not text:
        return None
    total = 0.0
    number = ""
    for character in str(text):
        if character.isdigit() or character == ".":
            number += character
            continue
        factor = {"h": 3600.0, "m": 60.0, "s": 1.0}.get(character)
        if factor is None or not number:
            return None
        total += float(number) * factor
        number = ""
    if number:
        return None
    return int(total) if total > 0 else None


__all__ = [
    "ABSENT",
    "LITERAL",
    "RESOLVED",
    "RUNTIME",
    "UNREADABLE",
    "WITHHELD",
    "get_pod",
    "get_pod_environment",
    "get_pod_metrics",
]
