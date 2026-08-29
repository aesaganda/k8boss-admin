"""
Debug containers (§7.4) — ``kubectl debug``, through the write funnel.

A pod whose image is a distroless binary with no shell is the pod an operator
most needs a shell in, and the one exec cannot help with: there is nothing to
exec. Kubernetes' answer is an **ephemeral container** — a second container
scheduled into the *running* pod, sharing its network namespace and (when asked)
its process namespace, carrying whatever tools the debug image has. That is what
``kubectl debug`` attaches and what this module attaches.

**It is a write, so it goes through :func:`app.admin.mutate.mutate` like every
other write.** Nothing here is special-cased: the mutations gate, the preflight
on the subresource RBAC actually names, the dry run, the diff and the audit row
all come from the funnel. Exec is the exception in this codebase (there is no
object to diff and no resourceVersion to check); this is not — there is a real
object, the API server will project the change on request, and the projection is
worth reading before confirming.

Four properties this module is responsible for.

**The subresource is what is addressed, patched and preflighted.**
``pods/ephemeralcontainers`` is a separate RBAC resource from ``pods``, so a
ServiceAccount can hold ``patch pods`` and not this, and preflighting the parent
would report allowed and then fail at the API server with a bare forbidden
naming nothing. It is also a separate *API surface*: writing
``spec.ephemeralContainers`` on the pod itself is rejected by the API server, and
an operator reading that rejection would conclude the field is immutable rather
than that it has its own endpoint.

**A cluster that does not serve it is ``unsupported``, decided from discovery
rather than from a 404.** Ephemeral containers are beta-behind-a-gate before
1.23 and absent before 1.16, and the API server answers a request for a
subresource it does not serve with **404** — which
:func:`app.errors.from_api_exception` maps, correctly for every other caller, to
``not_found``. An operator told "not found" about a pod they are looking at goes
hunting for a deleted pod. So the question is asked of discovery, where the
answer is about the *cluster*, and §1.3 makes ``unsupported`` not an error in the
UI: no ephemeral containers on a 1.15 cluster is an ordinary fact.

**When discovery cannot be read, support is ``None``, never ``False``.** "This
cluster has no ephemeral containers" and "we could not find out whether it does"
send an operator to two different places, and only one of them is a cluster
upgrade. Unknown proceeds and lets the API server judge — the same posture
:func:`app.api.logs.resolve_container` takes when it cannot read the pod it
wanted to validate against.

**An ephemeral container cannot be removed.** The API forbids deleting one; it
lives until the pod does. That is the single most important sentence in the
confirm dialog, and it is why this action is dry-run-and-confirm rather than a
button: what the operator is consenting to is permanent for the life of the pod.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from kubernetes.client.rest import ApiException

from app.admin.apply import STRATEGIC_MERGE_PATCH, patch_fn, read_object
from app.admin.images import validate_image_reference
from app.admin.mutate import mutate
from app.admin.names import random_suffix
from app.config import settings
from app.errors import AdminError, Invalid, Unsupported, from_api_exception
from app.resources import catalog
from app.resources.envelope import envelope
from app.resources.shaping import container_state, get_field

logger = logging.getLogger(__name__)

#: The subresource, spelled once. It is the RBAC resource suffix, the URL segment
#: and the discovery name, and they have to agree.
SUBRESOURCE = "ephemeralcontainers"

#: Discovery's name for it within the core group's resource list.
_DISCOVERY_NAME = f"pods/{SUBRESOURCE}"

#: Generated names look like ``debugger-x4k2p``, which is ``kubectl debug``'s own
#: shape. Deliberately recognisable: an operator reading ``kubectl get pod -o
#: yaml`` six months later should be able to tell at a glance that the extra
#: container was somebody debugging and not part of the workload.
NAME_PREFIX = "debugger-"

#: How many times to re-roll a generated name that collides with a container the
#: pod already has. With 27^5 suffixes a collision is a fluke; a bounded loop
#: means a pod that somehow holds them all fails loudly instead of spinning.
_NAME_ATTEMPTS = 8

#: The same bound :mod:`app.api.exec_ws` puts on an exec argv, for the same
#: reason: not a security control — the first argument alone runs anything — but
#: a bound on what a malformed client can put in a request body and in an audit
#: row's detail.
MAX_COMMAND_ARGS = 32

#: RFC 1123 label, which is what the API server validates a container name
#: against. Checked here so a typo is answered with the rule rather than with an
#: admission error quoting a regex.
_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAME_LENGTH = 63

# --------------------------------------------------------------------------- #
# Does this cluster serve ephemeral containers?
# --------------------------------------------------------------------------- #

def support() -> dict[str, Any]:
    """Whether ``pods/ephemeralcontainers`` is served, from discovery.

    Returns ``{"supported": bool | None, "verbs": [...] | None, "detail": str}``.

    ``supported`` is **three-valued and the third value is the point**. ``None``
    means the core group's discovery document could not be read, so we do not
    know — and "we could not find out" is not "this cluster cannot do it". The
    first sends an operator to look at their API server; the second sends them to
    plan an upgrade they may not need.

    ``verbs`` is what discovery advertises for the subresource, so a cluster that
    serves it read-only is reported as such rather than being discovered at the
    confirm step.
    """
    try:
        payload = catalog.raw_get("/api/v1")
    except (AdminError, ApiException) as e:
        # Both, because `raw_get` raises the API server's own ``ApiException``
        # untranslated and :mod:`app.k8s.client` raises ``AdminError`` for the
        # transport failures that never reach it. Catching only one of them
        # would let a 403 on discovery — the likeliest failure of all, on a
        # console whose ServiceAccount was scoped narrowly — escape as a 502 from
        # an endpoint the caller is otherwise permitted to use.
        #
        # Not re-raised. This is a *capability question* asked in service of a
        # real request; failing the request because the capability lookup failed
        # would turn a degraded discovery endpoint into "you cannot debug this
        # pod", which is a claim about the cluster we have no evidence for.
        code = (
            e.code if isinstance(e, AdminError)
            else from_api_exception(e, context={"path": "/api/v1"}).code
        )
        logger.info(
            "Could not read core API discovery to check for %s (%s); "
            "treating support as unknown.", _DISCOVERY_NAME, code,
        )
        return {
            "supported": None,
            "verbs": None,
            "detail": (
                "The core API group's discovery document could not be read "
                f"({code}), so whether this cluster serves ephemeral containers "
                "is unknown."
            ),
        }

    resources = payload.get("resources")
    if not isinstance(resources, list):
        logger.info(
            "Core API discovery returned no resource list; treating %s support as unknown.",
            _DISCOVERY_NAME,
        )
        return {
            "supported": None,
            "verbs": None,
            "detail": (
                "The core API group's discovery document carried no resource list, "
                "so whether this cluster serves ephemeral containers is unknown."
            ),
        }

    for entry in resources:
        if isinstance(entry, dict) and entry.get("name") == _DISCOVERY_NAME:
            verbs = [str(verb) for verb in (entry.get("verbs") or [])]
            return {
                "supported": True,
                "verbs": verbs,
                "detail": (
                    f"The API server serves {_DISCOVERY_NAME}"
                    + (f" with verbs: {', '.join(sorted(verbs))}." if verbs else ".")
                ),
            }

    # A complete answer that does not contain it. This is the one branch entitled
    # to say False: discovery was read, and the subresource is not in it.
    return {
        "supported": False,
        "verbs": None,
        "detail": (
            f"This cluster's core API group does not serve {_DISCOVERY_NAME}. "
            "Ephemeral containers need Kubernetes 1.16 or later, and the "
            "EphemeralContainers feature gate enabled before 1.23."
        ),
    }


def _require_support(namespace: str, name: str) -> None:
    """Refuse before touching the pod when the cluster provably cannot do this.

    ``supported is None`` deliberately falls through: see :func:`support`. The
    request then proceeds and the API server answers, which is the correct
    outcome when the only alternative is to guess.
    """
    context = {
        "group": "", "version": "v1", "resource": "pods",
        "subresource": SUBRESOURCE, "namespace": namespace, "name": name,
    }
    state = support()

    if state["supported"] is False:
        raise Unsupported(
            "This cluster does not serve ephemeral containers.",
            detail=state["detail"],
            hint=(
                "Debug containers need the pods/ephemeralcontainers subresource. "
                "Read the pod's logs, or exec into a container that has a shell."
            ),
            context=context,
        )

    verbs = state["verbs"]
    if state["supported"] and verbs and "patch" not in verbs:
        # Discovery answered and said this subresource cannot be patched. Same
        # reasoning as `app.resources.reader._require_verb`: the API server would
        # answer 405 and that maps to `unsupported` anyway, but "it advertises
        # get and update, not patch" is actionable where "method not allowed" is
        # not.
        raise Unsupported(
            f"This cluster serves {_DISCOVERY_NAME} but does not allow patching it.",
            detail=f"Verbs advertised by discovery: {', '.join(sorted(verbs))}.",
            context={**context, "verb": "patch"},
        )


# --------------------------------------------------------------------------- #
# Reading what is already there
# --------------------------------------------------------------------------- #

def _read_pod(namespace: str, name: str) -> dict[str, Any]:
    """The live pod, for its container names and its ephemeral containers.

    Read through :func:`app.admin.apply.read_object` rather than the generic
    reader for the reason that function documents: group, version and scope are
    already known here, so a discovery round trip to re-learn "core/v1 pods is
    namespaced" would be paid on every request for nothing.
    """
    return read_object("", "v1", "pods", name, namespace=namespace)


def _names(pod: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """``(containers, initContainers, ephemeralContainers)`` names of one pod."""

    def names_of(*path: str) -> list[str]:
        return [
            str(get_field(entry, "name"))
            for entry in (get_field(pod, *path, default=[]) or [])
            if get_field(entry, "name")
        ]

    return (
        names_of("spec", "containers"),
        names_of("spec", "initContainers"),
        names_of("spec", "ephemeralContainers"),
    )


def _status_index(pod: dict[str, Any]) -> dict[str, Any]:
    """``status.ephemeralContainerStatuses`` keyed by container name.

    Ephemeral container statuses live in their own array, not in
    ``containerStatuses``. A caller that looked in the usual place would find
    nothing and report a running debug container as never started.
    """
    index: dict[str, Any] = {}
    for status in get_field(pod, "status", "ephemeralContainerStatuses", default=[]) or []:
        key = get_field(status, "name")
        if key:
            index[str(key)] = status
    return index


def _row(spec: Any, status: Any) -> dict[str, Any]:
    """One debug container, as the UI reads it.

    ``state`` comes from :func:`app.resources.shaping.container_state`, the same
    shaper the pod rows use, so a debug container reads ``Running`` in this list
    and ``Running`` in the pod's container table rather than the same fact in two
    spellings. ``(None, None)`` from it means the kubelet has not reported on
    this container yet — genuinely different from ``Waiting``, and the difference
    is whether the operator should keep waiting or go and look at the node.
    """
    state, reason = container_state(status)
    # Read from whichever half of the state carries it. The API sets `startedAt`
    # on *both* `running` and `terminated`, and only `waiting` genuinely lacks
    # one — so reading the running path alone reports `null` for a container the
    # kubelet demonstrably started and then watched exit.
    #
    # That null is not harmless: §7.4 fixes it as "has not started", and the
    # Debug tab renders it as the words "Not started" in the row whose State
    # column says "Terminated". The console would be contradicting itself about
    # one container. Worse, the two cases it conflates are diagnosed in opposite
    # directions — a debug image whose entrypoint ran and exited (a bad command,
    # a non-shell image) versus one the node never started (ImagePullBackOff) —
    # and the operator who reads the second story about the first attaches
    # another debug container, which is an irreversible change to a live pod.
    started_at = (
        get_field(status, "state", "running", "startedAt")
        or get_field(status, "state", "terminated", "startedAt")
    )
    return {
        "name": get_field(spec, "name"),
        "image": get_field(spec, "image"),
        "targetContainer": get_field(spec, "targetContainerName"),
        "command": list(get_field(spec, "command", default=[]) or []) or None,
        "tty": bool(get_field(spec, "tty", default=False)),
        "state": state,
        "reason": reason,
        "started_at": started_at,
        # `restarts` is absent rather than 0: an ephemeral container is never
        # restarted by the kubelet, so a restart count would be a field that is
        # always zero and reads as though it could be otherwise.
    }


def list_debug_containers(namespace: str, name: str) -> dict[str, Any]:
    """``GET /api/pods/{namespace}/{name}/debug`` (§7.4).

    The §1.2 envelope plus ``supported``. ``items: []`` here is a real zero — the
    pod was read and it has no ephemeral containers — which is exactly what §0.1
    requires the empty list to mean. A pod that could *not* be read raises
    instead: it is the primary read of this endpoint, not a secondary column, and
    an empty list for it would say "this pod has no debug containers" about a pod
    we never saw.
    """
    pod = _read_pod(namespace, name)
    containers, init_containers, _ephemeral = _names(pod)
    statuses = _status_index(pod)
    items = [
        _row(spec, statuses.get(str(get_field(spec, "name"))))
        for spec in (get_field(pod, "spec", "ephemeralContainers", default=[]) or [])
    ]

    state = support()
    result = envelope(items)
    # Additive to §1.2, and three-valued for the reason `support` documents. The
    # UI renders False as "this cluster cannot do this" and None as "we could not
    # find out" — collapsing them would tell an operator their cluster lacks a
    # feature it may well have.
    result["supported"] = state["supported"]
    result["supportDetail"] = state["detail"]
    # The other two lists of names in this pod, which the caller needs for two
    # things it otherwise cannot do: offer the process-namespace targets, and
    # refuse a container name that is already taken *before* spending a request
    # and an audit row to be told so.
    #
    # They are here rather than on the §6 PodRow because that row deliberately
    # carries no init containers, and adding them would change what the Logs and
    # Terminal pickers offer. They are real values from a pod that was read —
    # this endpoint raises rather than answering with an empty envelope when it
    # could not read the pod — so an empty list here is a genuine zero.
    result["podContainers"] = containers
    result["initContainers"] = init_containers
    return result


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _check_image(image: str) -> str:
    """The debug image, or 422 naming what is wrong with it."""
    return validate_image_reference(
        image,
        missing_message="A debug container needs an image.",
        missing_hint=(
            "Name an image that has the tools you need — a shell, `ps`, `curl`. "
            f"This console's configured default is {settings.debug_image!r}."
        ),
    )


def _check_command(command: list[str] | None) -> list[str] | None:
    """The argv to run in the debug container, or ``None`` for the image's own.

    ``None`` and ``[]`` are the same answer here — run whatever the image's
    entrypoint is — and both are returned as ``None`` so the patch body omits the
    field entirely. Sending ``command: []`` would *clear* the entrypoint, and a
    debug container with no command exits immediately.
    """
    if not command:
        return None
    argv = [arg for arg in command if arg != ""]
    if not argv:
        return None
    if len(argv) > MAX_COMMAND_ARGS:
        raise Invalid(
            f"Too many command arguments: {len(argv)} (limit {MAX_COMMAND_ARGS}).",
            context={"parameter": "command", "count": len(argv),
                     "limit": MAX_COMMAND_ARGS},
        )
    return argv


def _check_name(
    container: str | None,
    *,
    taken: list[str],
    namespace: str,
    name: str,
) -> str:
    """The debug container's name: the caller's, validated, or a generated one.

    A name already in use is refused here rather than forwarded. The API server
    refuses it too, with a message about ``spec.ephemeralContainers[1].name``
    being a duplicate — which does not say *which* existing container it
    duplicates, and the answer matters: colliding with the app's own container
    name is a typo, and colliding with an earlier debug container means somebody
    else is already in here.
    """
    context = {
        "parameter": "container", "namespace": namespace, "name": name,
        "existingContainers": taken,
    }

    if container:
        candidate = container.strip()
        if len(candidate) > _MAX_NAME_LENGTH or not _DNS_LABEL.match(candidate):
            raise Invalid(
                f'"{candidate}" is not a valid container name.',
                detail=(
                    "A container name is a DNS-1123 label: lower-case letters, "
                    f"digits and dashes, starting and ending with an alphanumeric, "
                    f"at most {_MAX_NAME_LENGTH} characters."
                ),
                context={**context, "value": candidate},
            )
        if candidate in taken:
            raise Invalid(
                f'Pod "{name}" already has a container named "{candidate}".',
                detail="Containers, init containers and ephemeral containers share "
                       "one namespace of names: " + ", ".join(taken) + ".",
                hint=(
                    "Leave the name blank to have one generated, or pick another. "
                    "An existing debug container can be exec'd into directly — it "
                    "does not need to be attached again."
                ),
                context={**context, "value": candidate},
            )
        return candidate

    for _ in range(_NAME_ATTEMPTS):
        suffix = random_suffix()
        candidate = f"{NAME_PREFIX}{suffix}"
        if candidate not in taken:
            return candidate

    raise Invalid(
        f'Could not generate an unused container name for pod "{name}".',
        detail=f"{_NAME_ATTEMPTS} generated names all collided with existing containers.",
        hint="Name the debug container explicitly.",
        context=context,
    )


def _check_target(
    target: str | None,
    *,
    containers: list[str],
    namespace: str,
    name: str,
) -> str | None:
    """The container whose process namespace the debug container should share.

    Only a real container of the pod. Init and ephemeral containers are refused:
    an init container has exited, and targeting another ephemeral container is
    not something the API supports — both would be accepted into the manifest and
    then quietly do nothing, which is the worst of the three outcomes.

    ``None`` means "do not set ``targetContainerName``", which is a debug
    container that shares the pod's network and volumes but not its process
    namespace. That is the *safe* default and also the widely supported one: not
    every container runtime implements process namespace targeting, and where it
    is unimplemented the field is ignored rather than refused — so a console that
    always set it would sometimes promise a view of the app's processes and
    silently not deliver it.
    """
    if not target:
        return None
    candidate = target.strip()
    if not candidate:
        return None
    if candidate not in containers:
        raise Invalid(
            f'Pod "{name}" has no container named "{candidate}" to target.',
            detail="Containers: " + (", ".join(containers) or "none") + ".",
            hint=(
                "A debug container can only share the process namespace of one of "
                "the pod's own containers. Leave it unset to share only the "
                "network namespace and volumes."
            ),
            context={"parameter": "targetContainer", "value": candidate,
                     "namespace": namespace, "name": name, "containers": containers},
        )
    return candidate


# --------------------------------------------------------------------------- #
# The write
# --------------------------------------------------------------------------- #

def _container_spec(
    *,
    container: str,
    image: str,
    command: list[str] | None,
    target: str | None,
    tty: bool,
) -> dict[str, Any]:
    """The ``EphemeralContainer`` to add.

    ``stdin`` is always true and ``tty`` follows the caller, because the point of
    this container is that somebody is about to type in it. A debug container
    with ``stdin: false`` running a shell reads EOF immediately and terminates,
    and the operator watches a container they just created go straight to
    ``Completed`` with no explanation.

    Deliberately absent: ``resources``, ``ports``, ``lifecycle`` and the three
    probes. The API server *rejects* an ephemeral container carrying any of them,
    so there is nothing to expose and no request that could set them.
    """
    spec: dict[str, Any] = {
        "name": container,
        "image": image,
        # `IfNotPresent` rather than `Always`: the pod may be on a node that
        # cannot reach the registry, and the whole reason to debug it may be that
        # its network is broken. A cached image that starts beats a fresh pull
        # that hangs in ImagePullBackOff.
        "imagePullPolicy": "IfNotPresent",
        "stdin": True,
        "tty": bool(tty),
        "terminationMessagePolicy": "File",
    }
    if command:
        spec["command"] = list(command)
    if target:
        spec["targetContainerName"] = target
    return spec


def attach_debug_container(
    namespace: str,
    name: str,
    *,
    image: str | None = None,
    container: str | None = None,
    target_container: str | None = None,
    command: list[str] | None = None,
    tty: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    """``POST /api/pods/{namespace}/{name}/debug`` (§7.4).

    Adds one ephemeral container to a running pod and returns the §1.5 mutation
    response, plus the container that was (or would be) created so the caller can
    open a terminal on it without guessing the generated name.

    The patch is a **strategic merge** on ``spec.ephemeralContainers``, whose
    merge key is ``name``. That is what makes this an *append*: a plain merge
    patch (RFC 7386) replaces a list wholesale, so attaching a second debug
    container would silently delete the first one from the manifest — an
    operation the API server would then reject, because ephemeral containers
    cannot be removed, with an error about the field rather than about the
    console's choice of patch type. ``kubectl debug`` sends the same patch to the
    same subresource.

    ``before`` is the subresource's own representation, read from the very path
    the patch is sent to. Both sides of the diff therefore have the same shape by
    construction: if a future API version returned something narrower from the
    subresource than from the pod, a diff assembled from the two would show the
    whole spec being deleted.
    """
    resolved_image = _check_image(image if image is not None else settings.debug_image)
    argv = _check_command(command)

    # Ordered before the pod read on purpose: on a cluster that cannot do this at
    # all, "your cluster does not serve ephemeral containers" is the answer, and
    # it should not be preceded by a pod read that might fail for its own reasons
    # and produce a different, less useful error.
    _require_support(namespace, name)

    pod = _read_pod(namespace, name)
    containers, init_containers, ephemeral = _names(pod)
    chosen = _check_name(
        container,
        taken=[*containers, *init_containers, *ephemeral],
        namespace=namespace,
        name=name,
    )
    target = _check_target(
        target_container, containers=containers, namespace=namespace, name=name,
    )

    # A pod that has finished cannot host a debug container: the kubelet will
    # never start it, and the API server accepts the write regardless. Refused
    # here so the operator is told why nothing happens, rather than watching a
    # container sit in `waiting` forever on a pod that exited last Tuesday.
    phase = get_field(pod, "status", "phase")
    if phase in ("Succeeded", "Failed"):
        raise Invalid(
            f'Pod "{name}" has already finished (phase {phase}), so a debug '
            "container would never start.",
            detail=(
                "The kubelet starts an ephemeral container only in a pod that is "
                "still running."
            ),
            hint=(
                "Read this pod's logs instead — including the previous "
                "container's, for a crash loop."
            ),
            context={"namespace": namespace, "name": name, "phase": phase},
        )

    before = read_object("", "v1", "pods", name, namespace=namespace, subresource=SUBRESOURCE)
    body = {
        "spec": {
            "ephemeralContainers": [
                _container_spec(
                    container=chosen, image=resolved_image, command=argv,
                    target=target, tty=tty,
                )
            ]
        }
    }

    result = mutate(
        verb="patch",
        group="",
        version="v1",
        plural="pods",
        namespace=namespace,
        name=name,
        dry_run=dry_run,
        subresource=SUBRESOURCE,
        apply_fn=patch_fn(
            "", "v1", "pods", name, body,
            namespace=namespace,
            subresource=SUBRESOURCE,
            content_type=STRATEGIC_MERGE_PATCH,
        ),
        before=before,
        # The audit sentence names the image, because the question an incident
        # review asks about a debug container is not that one was attached but
        # *what was put inside somebody's production pod*.
        detail=(
            f"debug container {chosen} ({resolved_image})"
            + (f" targeting {target}" if target else "")
            + (f" running {' '.join(argv)}" if argv else "")
        ),
    )

    # Additive to §1.5, and the reason this function does not just return the
    # funnel's dict: the generated name is chosen here, and a UI that had to
    # parse it back out of the diff would be reading a rendered document to
    # recover a value the server already knows.
    result["container"] = chosen
    result["image"] = resolved_image
    result["targetContainer"] = target
    result["command"] = argv
    result["tty"] = bool(tty)
    return result


__all__ = [
    "MAX_COMMAND_ARGS",
    "NAME_PREFIX",
    "SUBRESOURCE",
    "attach_debug_container",
    "list_debug_containers",
    "support",
]
