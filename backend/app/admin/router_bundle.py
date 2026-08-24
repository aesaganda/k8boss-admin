"""
The router this console ships (§14) — HAProxy, as a list of ordinary objects.

k8boss-admin installs a reverse proxy. That is a deliberate departure from
"holds no cluster state, not a deployment engine", and the shape of the
departure is the whole design: **the console writes manifests, and nothing
else.** There is no controller in this process, no reconcile loop, no desired
state stored anywhere. What is installed is a Deployment, and the thing that
keeps it running is the Kubernetes control plane — which is where a control loop
belongs. What routes traffic is HAProxy's own in-cluster controller, watching
Ingresses. The console's involvement ends the moment the last object is
accepted; every subsequent page is still a live read.

Concretely: :mod:`app.admin.router` turns this bundle into a sequence of calls
to :func:`app.admin.mutate.mutate`, one per object. Each is gated, preflighted,
dry-run, diffed and audited exactly like a scale or a drain. Installing the
router is not a new kind of act — it is eight ordinary writes with a plan in
front of them.

## Why HAProxy, and what it does not do

HAProxy is what the OpenShift Router is built on, which is what makes it the
right answer to "the same thing OpenShift uses". It is also, in August 2026, one
of the few actively released ingress controllers left: ``ingress-nginx``, which
served roughly half of all clusters, was retired in March 2026, and ``InGate``
— the project meant to succeed it — was retired before it shipped.

What this bundle serves, precisely:

* **Ingress** — fully. This is the point of it.
* **Gateway API HTTPRoute** — **no.** The HAProxy Kubernetes Ingress Controller
  implements Gateway API for TCPRoute only; HTTPRoute is not implemented
  upstream. The console does not claim otherwise, and :mod:`app.admin.router`
  reports it as a fact about the installed router rather than leaving an
  operator to discover it when their HTTPRoute is never accepted.
* **OpenShift Route** — no, and it should not: an OpenShift cluster already runs
  its own router, and installing a second one that claims the same hostnames is
  how an outage starts.

## Why the manifests are here and not fetched

Baked in as data, pinned to one version, rather than fetched from an upstream
URL at install time. An install that reaches the internet is an install that
does something different on the day upstream changes a default, on the day the
cluster is air-gapped, and on the day somebody takes over the repository. The
operator sees the exact objects in the diff before they are created, and the
bytes they approved are the bytes that get applied.

The bundle is derived from the upstream ``deploy/haproxy-ingress.yaml`` with
four deliberate differences, each of which is a defect in the upstream file for
this use:

1. **The image is pinned.** Upstream uses ``haproxytech/kubernetes-ingress``
   with no tag, which is ``:latest``. A router that silently changes major
   version when its pod restarts is not something to install on somebody's
   cluster from a console.
2. **An IngressClass is created**, and its ``spec.controller`` carries the class
   name as a suffix. Upstream ships no IngressClass at all, so an Ingress written
   with ``spec.ingressClassName: haproxy`` matches nothing and is never served —
   and the console's Routes screen writes exactly that. The suffix is not
   decoration: because the Deployment passes ``--ingress.class``, the controller
   matches only ``haproxy.org/ingress-controller/<that value>``. The bare string
   is the other half of the *other* valid pairing — the one where the flag is
   absent — and mixing the two halves is what this bundle shipped for its first
   releases, which admitted nothing at all while reporting itself healthy.
   Getting this pair wrong is silent; see :func:`ingress_controller_for`. **If you
   are re-deriving this bundle against a newer upstream release, check this pair
   first.**
3. **``--publish-service`` is set.** Without it the controller never writes
   ``status.loadBalancer`` on the Ingresses it serves, so the console's
   ``address`` column stays empty forever and an operator cannot tell a working
   exposure from an unclaimed one. That column is load-bearing for §13's
   ``admitted`` reporting.
4. **The Service type is a choice, not NodePort.** Upstream defaults to
   NodePort; on a cloud cluster the operator almost always wants LoadBalancer,
   and on a bare cluster NodePort or HostPort is the only thing that works.
   Guessing wrong produces a router with no way in.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

#: Upstream release this bundle was derived from. Bumping it means re-deriving
#: the manifests below against that release's ``deploy/haproxy-ingress.yaml``
#: and re-reading the four differences above — not just editing the string.
ROUTER_VERSION = "3.2.13"

#: The image, fully qualified and pinned. Fully qualified because a bare
#: ``haproxytech/...`` resolves through whatever the node's container runtime
#: has configured as its default registry, which on a mirrored or air-gapped
#: cluster is not Docker Hub.
ROUTER_IMAGE = f"docker.io/haproxytech/kubernetes-ingress:{ROUTER_VERSION}"

#: The IngressClass an Ingress names to be served by this router, and the
#: controller string that binds the class to it. The controller string is
#: HAProxy's own and is not ours to choose.
INGRESS_CLASS_NAME = "haproxy"
INGRESS_CONTROLLER = "haproxy.org/ingress-controller"


def ingress_controller_for(ingress_class_name: str) -> str:
    """The ``spec.controller`` string that binds our IngressClass to our router.

    It is **not** a constant, and the reason is the single most expensive defect
    this bundle has had. The deployment passes ``--ingress.class=<name>``, and
    when that flag is set the controller only accepts an IngressClass whose
    ``spec.controller`` is ``haproxy.org/ingress-controller/<name>`` — the bare
    string is what it matches when the flag is *absent*. Shipping the bare
    string alongside the flag means every Ingress naming this class is dropped
    with ``ignored: no matching``, and the router serves nothing at all: not the
    class it ships, not the annotation forms, not a class-less Ingress.

    Nothing about that is visible from outside. The Deployment is Available, both
    pods are Ready, ``/api/router`` reports the bundle installed and healthy, and
    every exposure the console creates is admitted by the API server. The only
    symptom is a 404 from a hostname that looks correct — §14's "an object that
    routes nothing while looking created", produced by the very thing installed
    to prevent it.

    There are two valid pairings, not one. Dropping ``--ingress.class`` and
    keeping the bare string works exactly as upstream documents it — verified on
    a live cluster, picked up within twelve seconds of the Ingress appearing.
    What does not work is a *mismatched* pair, which is what this bundle shipped.

    The rule has a second half that upstream's ``ingressclass.md`` does not
    state: **the IngressClass's name must equal the flag value as well.** A class
    named ``ic2-class`` carrying a correct ``.../ic2`` controller string, under
    ``--ingress.class=ic2``, is never matched and never logs a reason; renaming
    it to ``ic2`` and changing nothing else is picked up in fifteen seconds. Both
    halves were established one variable at a time against 3.2.13. This bundle
    derives the name and the flag from the same option so they cannot drift, and
    a test pins that, because a coupling nothing can violate is also a coupling
    nobody can see.

    We keep the flag and the suffix rather than dropping both, because the suffix
    makes the pairing exclusive: an IngressClass carrying the bare string belongs
    to any HAProxy controller running without the flag, and on a cluster that
    already has one, a class-less configuration would have the two of them
    contending for the same objects. ``docs/adr-0004-shipped-router.md`` is about
    not doing that.
    """
    return f"{INGRESS_CONTROLLER}/{ingress_class_name}"

#: Everything this bundle creates carries these, so :func:`app.admin.router`
#: can find what it installed without keeping a record of it. That is what makes
#: the console stateless about its own router: "what did I install" is a live
#: label selector against the cluster, not a row in the console's database.
MANAGED_BY = "k8boss-admin"
LABELS: dict[str, str] = {
    "app.kubernetes.io/name": "k8boss-admin-router",
    "app.kubernetes.io/component": "router",
    "app.kubernetes.io/managed-by": MANAGED_BY,
    "app.kubernetes.io/part-of": "k8boss-admin",
}

#: The version actually installed, stamped on every object. Read back by
#: :func:`app.admin.router.status` to answer "is this the version this console
#: ships" — which is how an upgrade is offered rather than guessed at.
VERSION_LABEL = "app.kubernetes.io/version"

#: Service types the console will create. ``ClusterIP`` is included and is not
#: useless: it is the right answer when something else in the cluster — a cloud
#: load balancer created out of band, another proxy — fronts the router.
SERVICE_TYPES = ("LoadBalancer", "NodePort", "ClusterIP")


@dataclass(frozen=True)
class RouterOptions:
    """The choices an operator makes about the router before it is installed.

    Small on purpose. Every field here is one that cannot be defaulted without
    producing a router that is wrong on some clusters — a namespace, a way in, a
    replica count, and whether it claims Ingresses that name no class at all.
    Everything else upstream exposes is left at its default and is editable
    afterwards through the ConfigMap, which the console browses like any other.
    """

    namespace: str = "k8boss-router"
    service_type: str = "LoadBalancer"
    replicas: int = 2
    ingress_class_name: str = INGRESS_CLASS_NAME
    #: Make this the cluster's default IngressClass. Off by default, and it is
    #: the single most consequential field here: turning it on makes this router
    #: claim every Ingress in the cluster that names no class, including ones
    #: another controller is already serving.
    default_class: bool = False
    #: Also grant and enable Gateway API. Off by default because the controller
    #: implements TCPRoute only — see the module docstring. Enabling it does not
    #: make HTTPRoutes work.
    gateway_api: bool = False
    image: str = ROUTER_IMAGE


NAME = "k8boss-admin-router"


def _meta(options: RouterOptions, *, namespaced: bool = True, name: str = NAME) -> dict[str, Any]:
    """Object metadata, with the managed-by labels and the version stamp."""
    metadata: dict[str, Any] = {
        "name": name,
        "labels": {**LABELS, VERSION_LABEL: ROUTER_VERSION},
    }
    if namespaced:
        metadata["namespace"] = options.namespace
    return metadata


def _namespace(options: RouterOptions) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": options.namespace,
            "labels": {
                **LABELS,
                VERSION_LABEL: ROUTER_VERSION,
                # The controller binds :80 and :443 inside its own container as
                # a non-root user with NET_BIND_SERVICE, so `restricted` would
                # reject it — `baseline` is the tightest level that admits this
                # pod. Set explicitly rather than inherited: on a cluster that
                # enforces `restricted` cluster-wide, an unlabelled namespace
                # means the Deployment is created and no pod ever starts, which
                # presents as "the router does nothing" with a healthy-looking
                # Deployment object.
                "pod-security.kubernetes.io/enforce": "baseline",
                "pod-security.kubernetes.io/audit": "baseline",
                "pod-security.kubernetes.io/warn": "baseline",
            },
        },
    }


def _service_account(options: RouterOptions) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": _meta(options),
    }


def _cluster_role(options: RouterOptions) -> dict[str, Any]:
    """The controller's own permissions.

    Transcribed from upstream rather than widened. Two rules are worth reading
    before this is applied, because they are what an operator is actually
    consenting to:

    * ``secrets`` with ``get/list/watch`` **cluster-wide**. An ingress controller
      that terminates TLS must read the Secrets holding the certificates, and
      RBAC cannot scope that to "only the ones referenced by an Ingress". This
      is the same grant the console's own reader role documents as its most
      privileged, and it is the price of TLS termination for every ingress
      controller that exists.
    * ``secrets`` with ``create/patch/update``, which upstream needs to store
      the generated default certificate. Narrower would be better and upstream
      does not offer narrower.

    The console shows this object in the diff before it is created, which is the
    only honest way to hand somebody a grant of this size.
    """
    rules: list[dict[str, Any]] = [
        {
            "apiGroups": [""],
            "resources": [
                "configmaps", "endpoints", "nodes", "pods", "services",
                "namespaces", "events", "serviceaccounts",
            ],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["", "extensions", "networking.k8s.io"],
            "resources": ["ingresses", "ingressclasses"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["extensions", "networking.k8s.io"],
            "resources": ["ingresses/status"],
            "verbs": ["update"],
        },
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "verbs": ["get", "list", "watch", "create", "patch", "update"],
        },
        {
            "apiGroups": ["ingress.v1.haproxy.org", "ingress.v3.haproxy.org"],
            "resources": ["*"],
            "verbs": ["get", "list", "watch", "update"],
        },
        {
            "apiGroups": ["discovery.k8s.io"],
            "resources": ["endpointslices"],
            "verbs": ["get", "list", "watch"],
        },
        {
            "apiGroups": ["apiextensions.k8s.io"],
            "resources": ["customresourcedefinitions"],
            "verbs": ["get", "list", "watch", "update"],
        },
        {
            "apiGroups": ["apps"],
            "resources": ["replicasets", "deployments", "daemonsets"],
            "verbs": ["get", "list"],
        },
    ]
    if options.gateway_api:
        rules.extend([
            {
                "apiGroups": ["gateway.networking.k8s.io"],
                "resources": ["referencegrants", "gateways", "gatewayclasses", "tcproutes"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": ["gateway.networking.k8s.io"],
                "resources": ["gatewayclasses/status", "gateways/status", "tcproutes/status"],
                "verbs": ["update"],
            },
        ])
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": _meta(options, namespaced=False),
        "rules": rules,
    }


def _cluster_role_binding(options: RouterOptions) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": _meta(options, namespaced=False),
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": NAME,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": NAME, "namespace": options.namespace},
        ],
    }


def _config_map(options: RouterOptions) -> dict[str, Any]:
    """Global HAProxy settings.

    ``ssl-redirect`` is **not** set here, and that is the decision it looks like.
    §13 reports "redirect plain HTTP to HTTPS" as a feature an Ingress cannot
    express, and turning it on globally in this ConfigMap would make that report
    false for every exposure on this router — the console would say the redirect
    was dropped while the router quietly performed it. An operator who wants it
    sets it here afterwards, at which point it is their configuration and the
    console is not claiming anything about it.
    """
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": _meta(options),
        "data": {
            "ssl-redirect-port": "443",
        },
    }


def _ingress_class(options: RouterOptions) -> dict[str, Any]:
    """The class an Ingress names to reach this router.

    Not in the upstream bundle, and the reason it is here is §13: the Routes
    screen writes ``spec.ingressClassName``, and without a matching IngressClass
    object the API server accepts the Ingress and no controller ever claims it.
    The exposure then sits there looking created and serving nothing, which is
    exactly the confidently-wrong state the console exists to avoid producing.
    """
    metadata = _meta(options, namespaced=False, name=options.ingress_class_name)
    if options.default_class:
        metadata["annotations"] = {"ingressclass.kubernetes.io/is-default-class": "true"}
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "IngressClass",
        "metadata": metadata,
        "spec": {"controller": ingress_controller_for(options.ingress_class_name)},
    }


def _deployment(options: RouterOptions) -> dict[str, Any]:
    """The controller itself: HAProxy plus the process that configures it."""
    args = [
        f"--configmap={options.namespace}/{NAME}",
        f"--ingress.class={options.ingress_class_name}",
        # Without this the controller does not write status.loadBalancer onto
        # the Ingresses it serves, and §13's `addresses` — the field that
        # distinguishes a claimed exposure from an unclaimed one — is empty for
        # every Ingress on this cluster, forever.
        f"--publish-service={options.namespace}/{NAME}",
    ]
    if options.default_class:
        # Claim Ingresses that name no class at all. Only when the operator
        # asked for the default class: otherwise this router would take over
        # objects another controller is serving.
        args.append("--empty-ingress-class")
    if options.gateway_api:
        args.append("--gateway-controller-name=haproxy.org/gateway-controller")

    selector = {
        "app.kubernetes.io/name": LABELS["app.kubernetes.io/name"],
        "app.kubernetes.io/component": "router",
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(options),
        "spec": {
            "replicas": options.replicas,
            # The selector is immutable after creation, so it deliberately
            # excludes the version label: including it would make every upgrade
            # a delete-and-recreate, which is an outage on the cluster's ingress
            # path. The pod template carries the version; the selector does not.
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": {**LABELS, VERSION_LABEL: ROUTER_VERSION}},
                "spec": {
                    "serviceAccountName": NAME,
                    # Spread replicas across nodes. `ScheduleAnyway` rather than
                    # `DoNotSchedule`: on a single-node cluster the strict form
                    # would leave the second replica Pending forever, and a
                    # router that is half-scheduled on a lab cluster is worse
                    # than one that is merely not spread.
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "ScheduleAnyway",
                            "labelSelector": {"matchLabels": selector},
                        }
                    ],
                    "containers": [
                        {
                            "name": "haproxy-ingress",
                            "image": options.image,
                            "args": args,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "allowPrivilegeEscalation": False,
                                "runAsUser": 1000,
                                "runAsGroup": 1000,
                                "capabilities": {
                                    "drop": ["ALL"],
                                    # The one capability it keeps, and it needs
                                    # it: HAProxy binds 80 and 443 inside the
                                    # container. Dropping this makes the pod
                                    # start and the listener fail.
                                    "add": ["NET_BIND_SERVICE"],
                                },
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "400Mi"},
                                "limits": {"memory": "2Gi"},
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/healthz", "port": 1042},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/healthz", "port": 1042},
                                "initialDelaySeconds": 5,
                                "periodSeconds": 5,
                            },
                            "ports": [
                                {"name": "http", "containerPort": 8080},
                                {"name": "https", "containerPort": 8443},
                                {"name": "stat", "containerPort": 1024},
                            ],
                            "env": [
                                {"name": "TZ", "value": "Etc/UTC"},
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
                                },
                                {
                                    "name": "POD_NAMESPACE",
                                    "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}},
                                },
                                {
                                    "name": "POD_IP",
                                    "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}},
                                },
                            ],
                        }
                    ],
                },
            },
        },
    }


def _service(options: RouterOptions) -> dict[str, Any]:
    """The way in.

    ``stat`` (the HAProxy statistics socket) is deliberately **not** published
    here. Upstream's Service exposes it on port 1024 alongside http and https,
    which on a ``LoadBalancer`` Service means the runtime statistics of the
    cluster's ingress path get a public IP. It stays reachable inside the
    cluster through the pod, which is where anything scraping it should be.
    """
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": _meta(options),
        "spec": {
            "type": options.service_type,
            "selector": {
                "app.kubernetes.io/name": LABELS["app.kubernetes.io/name"],
                "app.kubernetes.io/component": "router",
            },
            "ports": [
                {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 8080},
                {"name": "https", "port": 443, "protocol": "TCP", "targetPort": 8443},
            ],
        },
    }


#: Install order. It is not cosmetic: the Namespace has to exist before anything
#: namespaced, and the ServiceAccount before the Deployment that names it. The
#: RBAC pair can technically be created in any order, but a ClusterRoleBinding
#: that names a not-yet-existing ClusterRole is confusing to read in a diff.
#:
#: Uninstall walks this list backwards — and then **skips the Namespace**. It is
#: tempting to let the Namespace go last and take everything with it, and
#: :func:`app.admin.router.uninstall` deliberately does not: a namespace can hold
#: objects the console never put there, and deleting one is not recoverable. If
#: you are here because that seems like a simplification, read that function's
#: docstring before making it.
_BUILDERS = (
    ("namespaces", "", "v1", _namespace, False),
    ("serviceaccounts", "", "v1", _service_account, True),
    ("clusterroles", "rbac.authorization.k8s.io", "v1", _cluster_role, False),
    ("clusterrolebindings", "rbac.authorization.k8s.io", "v1", _cluster_role_binding, False),
    ("ingressclasses", "networking.k8s.io", "v1", _ingress_class, False),
    ("configmaps", "", "v1", _config_map, True),
    ("deployments", "apps", "v1", _deployment, True),
    ("services", "", "v1", _service, True),
)


@dataclass(frozen=True)
class BundleObject:
    """One object in the bundle, with everything a write needs to address it."""

    group: str
    version: str
    plural: str
    kind: str
    name: str
    namespace: str | None
    body: dict[str, Any]


def build(options: RouterOptions) -> list[BundleObject]:
    """The whole bundle, in install order.

    Pure: no cluster is read and nothing is written. That is what lets the plan
    be shown, diffed and tested without a cluster, and what lets
    :mod:`app.admin.router` hand each object to the funnel one at a time.
    """
    objects: list[BundleObject] = []
    for plural, group, version, builder, namespaced in _BUILDERS:
        body = copy.deepcopy(builder(options))
        objects.append(
            BundleObject(
                group=group,
                version=version,
                plural=plural,
                kind=body["kind"],
                name=body["metadata"]["name"],
                namespace=options.namespace if namespaced else None,
                body=body,
            )
        )
    return objects


def validate_options(payload: dict[str, Any]) -> RouterOptions:
    """Build :class:`RouterOptions` from a request body, or 422 naming the field."""
    from app.errors import Invalid

    namespace = str(payload.get("namespace") or "k8boss-router").strip()
    if not namespace:
        raise Invalid(
            "The router needs a namespace to be installed into.",
            context={"parameter": "namespace"},
        )

    service_type = str(payload.get("serviceType") or "LoadBalancer").strip()
    if service_type not in SERVICE_TYPES:
        raise Invalid(
            f"{service_type!r} is not a Service type this console will create.",
            detail=f"Types: {', '.join(SERVICE_TYPES)}.",
            hint=(
                "LoadBalancer on a cloud cluster, NodePort on a cluster with no "
                "load-balancer provider, ClusterIP when something else already "
                "fronts the router."
            ),
            context={"parameter": "serviceType", "value": service_type},
        )

    replicas = payload.get("replicas")
    replicas = 2 if replicas is None else int(replicas)
    if not 1 <= replicas <= 20:
        raise Invalid(
            f"A router replica count of {replicas} is outside the range 1 to 20.",
            detail=(
                "Zero is excluded deliberately: a router scaled to zero is an "
                "ingress outage that looks like a successful configuration change."
            ),
            context={"parameter": "replicas", "value": replicas},
        )

    ingress_class = str(payload.get("ingressClassName") or INGRESS_CLASS_NAME).strip()
    if not ingress_class:
        raise Invalid(
            "The router needs an IngressClass name.",
            detail="This is the value an Ingress puts in spec.ingressClassName to be served by it.",
            context={"parameter": "ingressClassName"},
        )

    return RouterOptions(
        namespace=namespace,
        service_type=service_type,
        replicas=replicas,
        ingress_class_name=ingress_class,
        default_class=bool(payload.get("defaultClass")),
        gateway_api=bool(payload.get("gatewayApi")),
        image=str(payload.get("image") or ROUTER_IMAGE).strip() or ROUTER_IMAGE,
    )


__all__ = [
    "INGRESS_CLASS_NAME",
    "INGRESS_CONTROLLER",
    "ingress_controller_for",
    "LABELS",
    "MANAGED_BY",
    "NAME",
    "ROUTER_IMAGE",
    "ROUTER_VERSION",
    "SERVICE_TYPES",
    "VERSION_LABEL",
    "BundleObject",
    "RouterOptions",
    "build",
    "validate_options",
]
