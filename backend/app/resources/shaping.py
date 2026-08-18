"""
Typed row shapers — the §8 config/storage/access rows, plus ``pod_row`` and
``phase_detail`` from §6.

Pure functions over objects. Nothing here performs I/O, which is a deliberate
constraint rather than an accident of the current call sites: a shaper that could
read from the cluster could also fail to read from it, and then the place that
decides whether a value is ``null`` because it is genuinely absent or ``null``
because a call failed would be a function with no way to record the difference.
Every value a shaper cannot derive from the object in front of it is passed in by
the caller, which owns the read and its ``unavailable`` entry.

**Both object shapes are accepted.** The generic §4 reader returns parsed JSON
dicts with camelCase keys; the typed §5/§6 endpoints hold ``V1Pod``-style client
models with snake_case attributes. ``pod_row`` is imported by the workloads lane,
which uses the typed clients, and is called by the resource browser, which does
not. Rather than making one of them convert, :func:`get_field` reads either —
using the client models' own ``attribute_map`` where one exists, so the mapping is
the generated one and not a guess about how the SDK spells ``clusterIP``.

**The two rules this file exists to enforce:**

* *Secret values never appear in a list row.* :func:`secret_row` reads key names
  and byte lengths and never touches a value. :func:`redact_secret` is what the
  single-object read uses when the reveal gate is not satisfied.
* *A number we could not derive is ``None``, never ``0``.* An unbound PVC has no
  capacity; a ServiceAccount with no ``automountServiceAccountToken`` field is
  not the same as one that sets it to ``false``; an aggregated ClusterRole whose
  ``rules`` the controller has not populated has an unknown rule count, not zero.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: kubectl's serialised copy of the object, stored inside the object's own
#: annotations. It lives here rather than in :mod:`app.resources.reader` because
#: this module imports nothing else from the package and is therefore the bottom
#: of the dependency graph — and because :func:`redact_secret` has to strip it. A
#: Secret applied with ``kubectl apply`` carries every one of its values, base64
#: and all, inside this annotation, so a redaction that only nulled ``data``
#: would hand back the entire Secret in its own metadata. ``reader`` imports the
#: name from here so there is exactly one spelling of it.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"

# --------------------------------------------------------------------------- #
# Field access across dicts and generated client models
# --------------------------------------------------------------------------- #

_SNAKE_RE = re.compile(r"(?<=[a-z0-9])([A-Z])")

# type -> {jsonName: python_attr}. The generated models carry attribute_map the
# other way round; reversing it once per class is cheap and exact, where a
# hand-rolled camel-to-snake conversion is not: `clusterIP` is `cluster_ip` and
# `podIP` is `pod_ip`, but a naive "underscore before every capital" gives
# `cluster_i_p`, which silently reads as None and renders a Service with no
# cluster IP.
_ATTR_CACHE: dict[type, dict[str, str]] = {}


def _snake(key: str) -> str:
    """camelCase -> snake_case, for objects with no ``attribute_map``.

    Only reached for stand-ins (test fakes, ``SimpleNamespace``); the real client
    models take the exact path above.
    """
    return _SNAKE_RE.sub(r"_\1", key).lower()


def _json_to_attr(obj: Any) -> dict[str, str] | None:
    attr_map = getattr(type(obj), "attribute_map", None)
    if not isinstance(attr_map, dict):
        return None
    cached = _ATTR_CACHE.get(type(obj))
    if cached is None:
        cached = {json_name: attr for attr, json_name in attr_map.items()}
        _ATTR_CACHE[type(obj)] = cached
    return cached


def get_field(obj: Any, *path: str, default: Any = None) -> Any:
    """Read a nested field by its **JSON** (camelCase) name, from a dict or a model.

    ``get_field(pod, "status", "containerStatuses", default=[])`` works whether
    ``pod`` came from the generic reader or from ``CoreV1Api``.

    Returns ``default`` as soon as any step of the path is missing or ``None``.
    A value that is present but falsy — ``0`` restarts, ``False`` for
    ``automount``, an empty ``{}`` container state — is returned as itself, which
    is the whole reason this is not a chain of ``or`` expressions: ``0 or None``
    is ``None``, and a pod with zero restarts is not a pod whose restart count is
    unknown.
    """
    current = obj
    for key in path:
        if current is None:
            return default
        if isinstance(current, dict):
            if key in current:
                current = current[key]
            else:
                snake = _snake(key)
                current = current[snake] if snake in current else None
            continue
        reverse = _json_to_attr(current)
        attr = reverse.get(key) if reverse else None
        if attr is not None:
            current = getattr(current, attr, None)
            continue
        # No attribute_map: a test fake or a SimpleNamespace. Try the snake_case
        # spelling the SDK would have used, then the JSON key verbatim, so a
        # stand-in built either way is read correctly.
        snake = _snake(key)
        value = getattr(current, snake, None)
        if value is None and snake != key:
            value = getattr(current, key, None)
        current = value
    return default if current is None else current


# --------------------------------------------------------------------------- #
# Timestamps and quantities
# --------------------------------------------------------------------------- #

def _as_datetime(value: Any) -> datetime | None:
    """Parse an API timestamp, whichever form the caller's client produced."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.debug("Unparseable Kubernetes timestamp %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rfc3339(value: Any) -> str | None:
    """Render a timestamp as the RFC 3339 UTC string every response uses."""
    parsed = _as_datetime(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def age_seconds(value: Any) -> int | None:
    """Seconds since ``value``, or ``None`` if there is no usable timestamp.

    Clamped at zero. The API server's clock and this pod's clock are not the
    same clock, and an object created a moment ago can arrive with a
    creationTimestamp a second or two in the future; a negative age renders as
    "created in -2 seconds", which looks like a bug in the console rather than
    like the sub-second clock skew it is.
    """
    parsed = _as_datetime(value)
    if parsed is None:
        return None
    delta = (datetime.now(timezone.utc) - parsed).total_seconds()
    return int(delta) if delta > 0 else 0


_QUANTITY_RE = re.compile(r"^(?P<number>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?P<suffix>[a-zA-Z]*)$")

# Binary suffixes are checked as whole tokens; decimal ones are single
# characters. "Mi" and "M" differ by 4.8%, which on a 1 TiB volume is 50 GB —
# large enough that guessing wrong shows up as a capacity report nobody trusts.
_BINARY_SUFFIXES = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3,
    "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6,
}
_DECIMAL_SUFFIXES = {
    "n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}


def parse_quantity(value: Any) -> float | None:
    """Parse a Kubernetes resource quantity (``5Gi``, ``100m``, ``1e3``, ``500M``).

    Returns ``None`` for anything unrecognised rather than raising or guessing a
    magnitude. A capacity the console cannot parse is a capacity it does not
    know, and the row will show an em dash — which is honest, where a zero would
    describe a 2 TiB volume as empty.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    match = _QUANTITY_RE.match(text)
    if match is None:
        logger.debug("Unparseable Kubernetes quantity %r", value)
        return None
    number = float(match.group("number"))
    suffix = match.group("suffix")
    if suffix in _BINARY_SUFFIXES:
        return number * _BINARY_SUFFIXES[suffix]
    if suffix in _DECIMAL_SUFFIXES:
        return number * _DECIMAL_SUFFIXES[suffix]
    logger.debug("Unknown quantity suffix %r in %r", suffix, value)
    return None


def parse_bytes(value: Any) -> int | None:
    """A quantity read as a byte count."""
    parsed = parse_quantity(value)
    return None if parsed is None else int(parsed)


def parse_cpu_cores(value: Any) -> float | None:
    """A quantity read as CPU cores. ``100m`` is 0.1, not 100."""
    return parse_quantity(value)


def _base64_decoded_size(value: Any) -> int:
    """Decoded byte length of a base64 string, without decoding it.

    Deliberately arithmetic rather than ``base64.b64decode``: this is called on
    Secret values, and decoding one would put the plaintext in a local variable
    on a code path whose entire purpose is to never hold it. Exact for the
    canonical, unwrapped base64 the API server emits.
    """
    text = str(value or "")
    return max((len(text) * 3) // 4 - text.count("="), 0)


# --------------------------------------------------------------------------- #
# §6 — pods
# --------------------------------------------------------------------------- #

# Waiting reasons that do not contradict the phase. A Pending pod whose
# containers are being created is exactly what "Pending" means, so surfacing
# ContainerCreating as a phase_detail would put a second status on every pod
# during every rollout and train operators to ignore the field.
_BENIGN_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})


def _container_state(status: Any) -> tuple[str | None, str | None]:
    """``(state, reason)`` for one container status.

    ``state`` is ``Running``/``Waiting``/``Terminated``; ``reason`` is the API's
    own reason where it has one. A terminated container with no reason gets one
    derived from its exit code, because "Terminated" with a blank reason is the
    row that makes an operator open a shell to find out whether the job worked.
    """
    state = get_field(status, "state")
    if state is None:
        return None, None
    for key, label in (("running", "Running"), ("waiting", "Waiting"), ("terminated", "Terminated")):
        part = get_field(state, key)
        if part is None:
            continue
        reason = get_field(part, "reason")
        if key == "terminated" and not reason:
            exit_code = get_field(part, "exitCode")
            if exit_code == 0:
                reason = "Completed"
            elif exit_code is not None:
                reason = f"ExitCode {exit_code}"
        return label, reason
    return None, None


def phase_detail(pod: Any) -> str | None:
    """The truth when ``status.phase`` is not it, or ``None`` when phase suffices.

    ``phase`` is a coarse five-value field and it is wrong often enough to
    matter: a pod whose only container has been crash-looping for a day is
    ``Running``, and a pod that has been stuck terminating since last Tuesday is
    also ``Running``. §6 is explicit that reporting a CrashLooping pod as
    Running is a confident wrong answer, so the backend computes the correction
    rather than leaving each caller to reinvent it.

    Order matters:

    1. ``deletionTimestamp`` wins — a pod being deleted is ``Terminating``
       whatever its phase says, and it is the case an operator is most often
       looking for.
    2. Init containers before app containers, prefixed ``Init:`` the way kubectl
       spells it. A pod blocked on an init container reports the init container's
       reason, not the app container's uninformative ``PodInitializing``.
    3. A non-benign waiting reason: ``CrashLoopBackOff``, ``ImagePullBackOff``,
       ``CreateContainerConfigError`` and friends.
    4. A container that terminated non-zero while the pod still says ``Running``.
    5. ``status.reason`` for Failed and Pending pods, which is where
       ``Evicted`` and ``Unschedulable`` live.

    Not included: partial readiness. A ``1/2`` pod is already reported as ``1/2``
    by :func:`pod_row`'s ``ready`` field, and adding a second "NotReady" signal
    would fire during every healthy rollout.
    """
    if get_field(pod, "metadata", "deletionTimestamp"):
        return "Terminating"

    phase = get_field(pod, "status", "phase")
    for prefix, key in (("Init:", "initContainerStatuses"), ("", "containerStatuses")):
        for status in get_field(pod, "status", key, default=[]) or []:
            state = get_field(status, "state")
            if state is None:
                continue
            waiting = get_field(state, "waiting")
            if waiting is not None:
                reason = get_field(waiting, "reason")
                if reason and reason not in _BENIGN_WAITING_REASONS:
                    return f"{prefix}{reason}"
            terminated = get_field(state, "terminated")
            if terminated is not None and phase == "Running":
                exit_code = get_field(terminated, "exitCode")
                if exit_code not in (None, 0):
                    return f"{prefix}{get_field(terminated, 'reason') or 'Error'}"

    if phase in ("Failed", "Pending"):
        reason = get_field(pod, "status", "reason")
        if reason:
            return str(reason)
    return None


def pod_row(pod: Any) -> dict[str, Any]:
    """The §6 PodRow, plus the ``phase_detail`` correction.

    ``ready`` counts ready container statuses over the number of containers. The
    denominator falls back to ``spec.containers`` when statuses are absent, which
    is the case for a pod that has not been scheduled yet: ``0/2`` is right
    there, and ``0/0`` would say the pod has no containers.

    ``restarts`` is a genuine zero when there are no container statuses — nothing
    has run, so nothing has restarted. That is a different null from §6's
    ``restarts_24h``, which is ``None`` when the pod *listing* failed; the
    difference is whether we are describing a pod we read or a pod we could not.
    """
    statuses = get_field(pod, "status", "containerStatuses", default=[]) or []
    spec_containers = get_field(pod, "spec", "containers", default=[]) or []
    total = len(statuses) or len(spec_containers)
    ready_count = sum(1 for status in statuses if get_field(status, "ready") is True)

    containers = []
    for status in statuses:
        state, reason = _container_state(status)
        containers.append({
            "name": get_field(status, "name"),
            "image": get_field(status, "image"),
            "ready": bool(get_field(status, "ready")),
            "restarts": int(get_field(status, "restartCount", default=0) or 0),
            "state": state,
            "reason": reason,
        })
    if not statuses:
        # A pod with no statuses still has a spec, and showing its containers
        # with unknown state beats showing an empty list that reads as "this pod
        # has no containers".
        for container in spec_containers:
            containers.append({
                "name": get_field(container, "name"),
                "image": get_field(container, "image"),
                "ready": False,
                "restarts": 0,
                "state": None,
                "reason": None,
            })

    owner_refs = get_field(pod, "metadata", "ownerReferences", default=[]) or []
    controller = next(
        (ref for ref in owner_refs if get_field(ref, "controller") is True),
        owner_refs[0] if owner_refs else None,
    )

    return {
        "name": get_field(pod, "metadata", "name"),
        "namespace": get_field(pod, "metadata", "namespace"),
        "node": get_field(pod, "spec", "nodeName"),
        "phase": get_field(pod, "status", "phase"),
        "phase_detail": phase_detail(pod),
        "ready": f"{ready_count}/{total}",
        "restarts": sum(int(get_field(s, "restartCount", default=0) or 0) for s in statuses),
        "age_seconds": age_seconds(get_field(pod, "metadata", "creationTimestamp")),
        "ip": get_field(pod, "status", "podIP"),
        "qos_class": get_field(pod, "status", "qosClass"),
        "containers": containers,
        "owner": (
            {"kind": get_field(controller, "kind"), "name": get_field(controller, "name")}
            if controller is not None else None
        ),
    }


# --------------------------------------------------------------------------- #
# §8 — services and ingresses
# --------------------------------------------------------------------------- #

def _load_balancer_addresses(obj: Any) -> list[str]:
    """IPs and hostnames a load balancer has published for this object."""
    addresses: list[str] = []
    for entry in get_field(obj, "status", "loadBalancer", "ingress", default=[]) or []:
        value = get_field(entry, "ip") or get_field(entry, "hostname")
        if value and value not in addresses:
            addresses.append(str(value))
    return addresses


def service_row(obj: Any, *, endpoint_count: int | None = None) -> dict[str, Any]:
    """§8 Service row.

    ``endpoint_count`` is supplied by the caller, which owns the EndpointSlice
    read and its ``unavailable`` entry. The default is ``None`` — "not counted" —
    and never ``0``, because a Service with no backends and a Service whose
    backends we failed to look up are the two states an operator most needs to
    tell apart when something is returning 503.

    ``externalIPs`` merges ``spec.externalIPs`` with the addresses a
    LoadBalancer has published, which is what the column means to an operator
    and what ``kubectl get svc`` shows. Keeping them separate would leave the
    field empty for every LoadBalancer Service.
    """
    ports = []
    for port in get_field(obj, "spec", "ports", default=[]) or []:
        ports.append({
            "name": get_field(port, "name"),
            "port": get_field(port, "port"),
            "targetPort": get_field(port, "targetPort"),
            "protocol": get_field(port, "protocol"),
            "nodePort": get_field(port, "nodePort"),
        })

    external = [str(ip) for ip in (get_field(obj, "spec", "externalIPs", default=[]) or [])]
    for address in _load_balancer_addresses(obj):
        if address not in external:
            external.append(address)

    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "type": get_field(obj, "spec", "type"),
        "clusterIP": get_field(obj, "spec", "clusterIP"),
        "externalIPs": external,
        "ports": ports,
        "selector": dict(get_field(obj, "spec", "selector", default={}) or {}),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
        "endpoint_count": endpoint_count,
    }


# Pre-1.18 clusters, and a surprising amount of still-deployed Helm, select the
# ingress controller with this annotation instead of spec.ingressClassName.
_INGRESS_CLASS_ANNOTATION = "kubernetes.io/ingress.class"


def ingress_row(obj: Any) -> dict[str, Any]:
    """§8 Ingress row.

    ``class`` falls back to the legacy annotation when ``spec.ingressClassName``
    is unset. Reporting ``null`` for an Ingress that is in fact bound to nginx by
    annotation would have an operator debugging why their rule is not being
    served by a controller that is serving it.
    """
    rules = []
    for rule in get_field(obj, "spec", "rules", default=[]) or []:
        paths = []
        for path in get_field(rule, "http", "paths", default=[]) or []:
            backend = get_field(path, "backend")
            service = get_field(backend, "service")
            port_spec = get_field(service, "port")
            paths.append({
                "path": get_field(path, "path"),
                "pathType": get_field(path, "pathType"),
                # A backend can be a Service or, rarely, a resource reference to
                # a CRD-backed object store. Naming whichever one is present
                # beats a null that reads as "this path routes nowhere".
                "service": (
                    get_field(service, "name")
                    if service is not None
                    else get_field(backend, "resource", "name")
                ),
                "port": (
                    get_field(port_spec, "number") or get_field(port_spec, "name")
                    if port_spec is not None else None
                ),
            })
        rules.append({"host": get_field(rule, "host"), "paths": paths})

    tls_hosts: list[str] = []
    for tls in get_field(obj, "spec", "tls", default=[]) or []:
        for host in get_field(tls, "hosts", default=[]) or []:
            if host not in tls_hosts:
                tls_hosts.append(str(host))

    addresses = _load_balancer_addresses(obj)
    annotations = get_field(obj, "metadata", "annotations", default={}) or {}
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "class": (
            get_field(obj, "spec", "ingressClassName")
            or annotations.get(_INGRESS_CLASS_ANNOTATION)
        ),
        "rules": rules,
        "tls_hosts": tls_hosts,
        "address": ", ".join(addresses) if addresses else None,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


# --------------------------------------------------------------------------- #
# §8 — config
# --------------------------------------------------------------------------- #

def configmap_row(obj: Any) -> dict[str, Any]:
    """§8 ConfigMap row: key names and total size, never values.

    ConfigMaps are not secret, but a list of two hundred of them carrying
    embedded certificates and dashboards is megabytes of payload nothing renders.
    Values come back from the single-object read.
    """
    data = get_field(obj, "data", default={}) or {}
    binary = get_field(obj, "binaryData", default={}) or {}
    size = sum(len(str(value).encode("utf-8")) for value in data.values())
    size += sum(_base64_decoded_size(value) for value in binary.values())
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "keys": sorted([*data.keys(), *binary.keys()]),
        "data_bytes": size,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def secret_row(obj: Any) -> dict[str, Any]:
    """§8 Secret row. **Values never appear here.**

    The only two things this function does with ``data`` are read its key names
    and measure the encoded length of its values arithmetically. No value is
    decoded, assigned to a local, logged or returned, so there is no code path
    from a Secret's contents into a list response — which is a stronger guarantee
    than "we remembered to delete the field", and is asserted by a test.
    """
    data = get_field(obj, "data", default={}) or {}
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "type": get_field(obj, "type"),
        "keys": sorted(data.keys()),
        "data_bytes": sum(_base64_decoded_size(value) for value in data.values()),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def redact_secret(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    """A Secret with its values withheld, for the single-object read (§8).

    Keys are kept and each value becomes ``null`` rather than the key being
    removed or the value being masked with asterisks. All three were considered:

    * Removing ``data`` entirely would render a Secret that appears to have no
      contents, which is a confidently wrong answer about the one object type
      where being wrong matters most.
    * A ``"********"`` placeholder is a *value*: it survives copy-paste, it can
      be applied back to the cluster, and it silently replaces a password with
      eight asterisks.
    * ``null`` says exactly what happened — the key exists, the value was
      withheld — and YAML that carries it fails validation if anyone tries to
      apply it, which is the correct outcome for an edit made without being
      allowed to see what is being edited.

    ``stringData`` is a write-only field and never comes back from a read; it is
    dropped anyway so a caller round-tripping an object it constructed cannot
    smuggle plaintext through.

    **``last-applied-configuration`` is dropped too, and that is not tidiness.**
    ``kubectl apply -f secret.yaml`` stores a complete serialised copy of the
    object — ``data`` included, base64 and all — in that annotation, inside the
    object it annotates. §4 keeps the annotation on single-object reads because
    an operator editing YAML has a legitimate reason to see what kubectl last
    applied; on a Secret that reason is outranked by the fact that it *is* the
    Secret. Nulling ``data`` while returning its verbatim copy one key away would
    be a reveal gate that gates nothing, and the request that bypassed it would
    look, in the audit trail, like a read that was never granted.
    """
    if not isinstance(obj, dict):
        return obj
    redacted = dict(obj)
    data = redacted.get("data")
    if isinstance(data, dict):
        redacted["data"] = {key: None for key in data}
    redacted.pop("stringData", None)

    metadata = redacted.get("metadata")
    if isinstance(metadata, dict):
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict) and LAST_APPLIED_ANNOTATION in annotations:
            # Copied on write, like `trim`: the caller may still hold the
            # unredacted object for a diff, and mutating its metadata in place
            # would corrupt that from the outside.
            metadata = dict(metadata)
            annotations = dict(annotations)
            annotations.pop(LAST_APPLIED_ANNOTATION, None)
            metadata["annotations"] = annotations
            redacted["metadata"] = metadata
    return redacted


# --------------------------------------------------------------------------- #
# §8 — storage
# --------------------------------------------------------------------------- #

# Pre-1.6 clusters set the storage class through an annotation. Still emitted by
# some CSI drivers' documentation, so it is checked after the spec field.
_STORAGE_CLASS_ANNOTATION = "volume.beta.kubernetes.io/storage-class"
_DEFAULT_CLASS_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.beta.kubernetes.io/is-default-class",
)


def pvc_row(obj: Any) -> dict[str, Any]:
    """§8 PersistentVolumeClaim row.

    ``capacity_bytes`` comes from ``status.capacity`` — the size actually
    provisioned — and is ``None`` for a Pending claim. It deliberately does not
    fall back to ``spec.resources.requests``: that is what was *asked for*, and
    reporting a request as a capacity would tell an operator a claim stuck in
    Pending has 500 GiB of storage.

    ``access_modes`` prefers ``status`` over ``spec`` for the same reason — what
    the volume grants, not what the claim requested.
    """
    status_modes = get_field(obj, "status", "accessModes", default=None)
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "status": get_field(obj, "status", "phase"),
        "volume": get_field(obj, "spec", "volumeName"),
        "capacity_bytes": parse_bytes(get_field(obj, "status", "capacity", "storage")),
        "access_modes": list(
            status_modes if status_modes is not None
            else (get_field(obj, "spec", "accessModes", default=[]) or [])
        ),
        "storage_class": (
            get_field(obj, "spec", "storageClassName")
            or (get_field(obj, "metadata", "annotations", default={}) or {}).get(
                _STORAGE_CLASS_ANNOTATION
            )
        ),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def pv_row(obj: Any) -> dict[str, Any]:
    """§8 PersistentVolume row. ``claim`` is ``namespace/name`` or ``None``."""
    claim_ref = get_field(obj, "spec", "claimRef")
    claim = None
    if claim_ref is not None:
        claim_namespace = get_field(claim_ref, "namespace")
        claim_name = get_field(claim_ref, "name")
        if claim_name:
            claim = f"{claim_namespace}/{claim_name}" if claim_namespace else str(claim_name)
    return {
        "name": get_field(obj, "metadata", "name"),
        "status": get_field(obj, "status", "phase"),
        "capacity_bytes": parse_bytes(get_field(obj, "spec", "capacity", "storage")),
        "access_modes": list(get_field(obj, "spec", "accessModes", default=[]) or []),
        "reclaim_policy": get_field(obj, "spec", "persistentVolumeReclaimPolicy"),
        "storage_class": get_field(obj, "spec", "storageClassName"),
        "claim": claim,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def storageclass_row(obj: Any) -> dict[str, Any]:
    """§8 StorageClass row.

    ``is_default`` reads both the GA and the beta annotation. A cluster upgraded
    from 1.5 keeps the beta spelling indefinitely, and reading only the GA one
    reports every class as non-default — which makes a PVC with no
    ``storageClassName`` look like it will never bind, when it binds fine.
    """
    annotations = get_field(obj, "metadata", "annotations", default={}) or {}
    is_default = any(
        str(annotations.get(key, "")).lower() == "true" for key in _DEFAULT_CLASS_ANNOTATIONS
    )
    return {
        "name": get_field(obj, "metadata", "name"),
        "provisioner": get_field(obj, "provisioner"),
        "reclaim_policy": get_field(obj, "reclaimPolicy"),
        "volume_binding_mode": get_field(obj, "volumeBindingMode"),
        "is_default": is_default,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


# --------------------------------------------------------------------------- #
# §8 — access
# --------------------------------------------------------------------------- #

def serviceaccount_row(obj: Any) -> dict[str, Any]:
    """§8 ServiceAccount row.

    ``automount`` is a **tri-state** and is kept as one. ``true`` and ``false``
    are explicit decisions on the ServiceAccount; ``None`` means the field is
    unset and the decision is made by the pod spec, or defaults to mounting.
    Collapsing ``None`` to ``false`` would tell an operator auditing token
    exposure that a ServiceAccount does not mount its token when it does — which
    is the exact shape of wrong answer §0 is about, on a security control.
    """
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "secrets_count": len(get_field(obj, "secrets", default=[]) or []),
        "automount": get_field(obj, "automountServiceAccountToken"),
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def _policy_rules(obj: Any) -> tuple[list[dict[str, Any]], int | None]:
    """``(rules, rule_count)`` for a Role or ClusterRole.

    ``rule_count`` is ``None`` when ``rules`` is absent rather than empty. An
    aggregated ClusterRole (one with an ``aggregationRule``) has its ``rules``
    filled in by a controller; before that happens the field is null, and
    reporting ``0`` would describe an aggregate that grants cluster-admin as
    granting nothing. An explicitly empty ``rules: []`` is a real zero and is
    reported as one.
    """
    raw = get_field(obj, "rules", default=None)
    if raw is None:
        return [], None
    rules = []
    for rule in raw:
        rules.append({
            "apiGroups": list(get_field(rule, "apiGroups", default=[]) or []),
            "resources": list(get_field(rule, "resources", default=[]) or []),
            "verbs": list(get_field(rule, "verbs", default=[]) or []),
            "resourceNames": list(get_field(rule, "resourceNames", default=[]) or []),
        })
    return rules, len(rules)


def role_row(obj: Any) -> dict[str, Any]:
    """§8 Role row."""
    rules, count = _policy_rules(obj)
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "rule_count": count,
        "rules": rules,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def clusterrole_row(obj: Any) -> dict[str, Any]:
    """§8 ClusterRole row. ``namespace`` is always ``None`` — it is cluster-scoped.

    The key is present rather than omitted so one table component can render both
    Roles and ClusterRoles without branching on which keys exist.
    """
    row = role_row(obj)
    row["namespace"] = None
    return row


def _binding_row(obj: Any) -> dict[str, Any]:
    role_ref = get_field(obj, "roleRef")
    subjects = []
    for subject in get_field(obj, "subjects", default=[]) or []:
        subjects.append({
            "kind": get_field(subject, "kind"),
            "name": get_field(subject, "name"),
            # ServiceAccount subjects are namespaced; User and Group subjects are
            # not, and carry None here. Defaulting to the binding's own namespace
            # would invent an attribution the object does not make.
            "namespace": get_field(subject, "namespace"),
        })
    return {
        "name": get_field(obj, "metadata", "name"),
        "namespace": get_field(obj, "metadata", "namespace"),
        "role": (
            {"kind": get_field(role_ref, "kind"), "name": get_field(role_ref, "name")}
            if role_ref is not None else None
        ),
        "subjects": subjects,
        "age_seconds": age_seconds(get_field(obj, "metadata", "creationTimestamp")),
    }


def rolebinding_row(obj: Any) -> dict[str, Any]:
    """§8 RoleBinding row."""
    return _binding_row(obj)


def clusterrolebinding_row(obj: Any) -> dict[str, Any]:
    """§8 ClusterRoleBinding row. Cluster-scoped, so ``namespace`` is ``None``."""
    row = _binding_row(obj)
    row["namespace"] = None
    return row


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

#: ``(group, plural) -> shaper``. Keyed on the *real* group name, so the core
#: group is the empty string; §1.4's ``core`` spelling is translated away before
#: anything reaches here.
ROW_SHAPERS: dict[tuple[str, str], Callable[[Any], dict[str, Any]]] = {
    ("", "pods"): pod_row,
    ("", "services"): service_row,
    ("", "configmaps"): configmap_row,
    ("", "secrets"): secret_row,
    ("", "persistentvolumeclaims"): pvc_row,
    ("", "persistentvolumes"): pv_row,
    ("", "serviceaccounts"): serviceaccount_row,
    ("networking.k8s.io", "ingresses"): ingress_row,
    ("storage.k8s.io", "storageclasses"): storageclass_row,
    ("rbac.authorization.k8s.io", "roles"): role_row,
    ("rbac.authorization.k8s.io", "clusterroles"): clusterrole_row,
    ("rbac.authorization.k8s.io", "rolebindings"): rolebinding_row,
    ("rbac.authorization.k8s.io", "clusterrolebindings"): clusterrolebinding_row,
}


def shaper_for(group: str, plural: str) -> Callable[[Any], dict[str, Any]] | None:
    """The row shaper for a resource, or ``None`` if it has no typed row.

    Version-independent on purpose: ``rbac.authorization.k8s.io/v1`` and a future
    ``v2`` describe the same Role, and pinning the registry to a version would
    silently drop the typed row on a cluster that serves the newer one — the row
    would become a raw manifest and the table would render blank columns.
    """
    return ROW_SHAPERS.get((group, plural))


__all__ = [
    "LAST_APPLIED_ANNOTATION",
    "ROW_SHAPERS",
    "age_seconds",
    "clusterrole_row",
    "clusterrolebinding_row",
    "configmap_row",
    "get_field",
    "ingress_row",
    "parse_bytes",
    "parse_cpu_cores",
    "parse_quantity",
    "phase_detail",
    "pod_row",
    "pv_row",
    "pvc_row",
    "redact_secret",
    "rfc3339",
    "role_row",
    "rolebinding_row",
    "secret_row",
    "service_row",
    "serviceaccount_row",
    "shaper_for",
    "storageclass_row",
]
