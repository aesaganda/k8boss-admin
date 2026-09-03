"""
Rendering and writing an exposure (§13) — the form half of the Routes screen.

The console models one thing, an **exposure**: a hostname, a path, one or more
target Services, and a decision about TLS. That model is written in OpenShift's
vocabulary because OpenShift's vocabulary is the one that matches how operators
talk about the problem — "terminate at the edge", "pass it through", "send ten
percent to the canary" — and because it is the only one of the three that has a
field for every part of it.

This module compiles that model down to whichever of the three APIs the
operator picked. Two properties of the compilation are the whole point:

**Nothing is dropped silently.** A `passthrough` exposure cannot be written as
an Ingress. The compiler does not quietly emit an ``nginx.ingress.kubernetes.io``
annotation that happens to work on the controller this was developed against,
and it does not quietly emit an edge-terminated Ingress and let the operator
find out when their mTLS client breaks. It returns the document it *can* write
plus a ``lossy[]`` list naming each feature it had to leave out, what that means
for traffic, and what to do instead. The write endpoint then **refuses** unless
the caller acknowledges exactly those features by name — the same shape as
``force`` on a node drain, and for the same reason: "I read this plan and
accept it" has to be a separate act from "go".

**The operator's document survives the form.** :func:`render` takes the document
currently in the editor and patches the fields the form owns into *it*, rather
than generating a fresh object from the model. An operator who hand-wrote
``spec.rules[1]``, a controller annotation, or an ``externalCertificate``
reference keeps all of it when they flip to the form, change the path, and flip
back. What the form does not model, it does not touch — and :func:`render`
reports those paths in ``preserved[]`` so the UI can say out loud which parts of
the document the form is not showing.

That second property is why the YAML is the source of truth here and the form is
a projection of it, rather than the two being independent editors with a "your
changes will be lost" warning between them. A console that discards a field an
operator wrote, at the moment they touch an unrelated control, is a console that
silently changes clusters.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any

from app.admin import apply as apply_service
from app.errors import Invalid, Unsupported
from app.resources import reader
from app.services import routes as routes_service
from app.services.routes import (
    FEATURE_EDGE_TLS,
    FEATURE_GENERATED_HOST,
    FEATURE_INSECURE_ALLOW,
    FEATURE_INSECURE_REDIRECT,
    FEATURE_LABELS,
    FEATURE_PASSTHROUGH_TLS,
    FEATURE_PATH_EXACT,
    FEATURE_REENCRYPT_TLS,
    FEATURE_WEIGHTED_BACKENDS,
    FEATURE_WILDCARD_SUBDOMAIN,
    RouteBackend,
)

logger = logging.getLogger(__name__)

#: Termination modes, in the OpenShift spelling the console's model uses.
TERMINATIONS = ("edge", "passthrough", "reencrypt")

#: ``spec.tls.insecureEdgeTerminationPolicy`` values.
INSECURE_POLICIES = ("None", "Allow", "Redirect")

#: Path match kinds. ``Prefix`` and ``Exact`` are the console's spelling; each
#: backend translates them into its own (Ingress says ``Prefix``, Gateway API
#: says ``PathPrefix``, a Route has no such field at all).
PATH_TYPES = ("Prefix", "Exact")

#: The maximum number of target Services. Three alternates plus the primary is
#: the OpenShift Route limit (``alternateBackends`` is ``MaxItems=3``), and the
#: console holds every backend to it rather than letting an exposure be
#: authored on Gateway API and then be un-portable to a Route.
MAX_TARGETS = 4


# --------------------------------------------------------------------------- #
# The model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Target:
    """One Service an exposure sends traffic to."""

    service: str
    port: int | str | None = None
    weight: int | None = None


@dataclass(frozen=True)
class TLSModel:
    """The TLS half of an exposure.

    ``termination`` of ``None`` means plain HTTP — a real and common choice for
    an internal service behind something else that terminates. It is distinct
    from every other value here and never defaulted to ``edge``: silently
    turning on TLS would produce an exposure that fails to serve rather than one
    that serves insecurely, but it would still be a cluster doing something the
    operator did not ask for.

    **There is no inline certificate or private key here, and that is a
    decision.** OpenShift's own console offers file inputs for both, and a Route
    can carry them in its spec — so this is a deliberate narrowing rather than an
    oversight. A PEM private key in this model would travel through a request
    body, back out in a dry-run projection, into the diff the operator reads, and
    into browser memory, on a path where ``redact_secret`` cannot help: it guards
    Secrets, and this key would be in an object's own spec. Every one of those
    hops is avoidable.

    What replaces it: ``secret_name``, which is a reference. It is the only form
    an Ingress can express at all, and the only form a Gateway listener can, so
    the narrower model is also the portable one. An operator who genuinely wants
    inline PEM writes it in the YAML view, where they typed it themselves, know
    it is there, and the document is written verbatim — parity with OpenShift's
    console is kept, one view across.

    ``destination_ca_certificate`` stays. It is a CA certificate — public
    material by definition — and reencrypt cannot be configured safely without
    it.
    """

    termination: str | None = None
    insecure_policy: str | None = None
    secret_name: str | None = None
    destination_ca_certificate: str | None = None


@dataclass(frozen=True)
class Exposure:
    """What the operator filled in, before it is any particular API's object."""

    name: str
    namespace: str
    targets: tuple[Target, ...]
    host: str | None = None
    subdomain: str | None = None
    path: str | None = None
    path_type: str = "Prefix"
    tls: TLSModel = field(default_factory=TLSModel)
    wildcard_policy: str | None = None
    ingress_class_name: str | None = None
    parent_refs: tuple[dict[str, Any], ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    # -- what the operator asked for, as feature tokens -------------------- #

    def requested_features(self) -> set[str]:
        """The exposure features this model uses, as :mod:`app.services.routes` tokens.

        This is the input to the lossiness check, and it is derived from the
        model rather than passed in so that a form control added later cannot be
        forgotten here — an unmodelled control would produce a document field
        that no backend was asked whether it could express.
        """
        wanted: set[str] = set()
        if self.tls.termination == "edge":
            wanted.add(FEATURE_EDGE_TLS)
        elif self.tls.termination == "passthrough":
            wanted.add(FEATURE_PASSTHROUGH_TLS)
        elif self.tls.termination == "reencrypt":
            wanted.add(FEATURE_REENCRYPT_TLS)

        if self.tls.insecure_policy == "Redirect":
            wanted.add(FEATURE_INSECURE_REDIRECT)
        elif self.tls.insecure_policy == "Allow":
            wanted.add(FEATURE_INSECURE_ALLOW)

        # A split is more than one target, not merely a weight being present:
        # a single target with weight 100 is what the OpenShift console writes
        # for an ordinary exposure, and calling that a traffic split would make
        # every Ingress lossy for a feature nobody used.
        if len(self.targets) > 1:
            wanted.add(FEATURE_WEIGHTED_BACKENDS)

        if self.wildcard_policy == "Subdomain":
            wanted.add(FEATURE_WILDCARD_SUBDOMAIN)
        if not self.host:
            wanted.add(FEATURE_GENERATED_HOST)
        if self.path_type == "Exact":
            wanted.add(FEATURE_PATH_EXACT)
        return wanted

    @property
    def primary(self) -> Target:
        return self.targets[0]


def _string(value: Any) -> str | None:
    """Normalise a wire string: blank and whitespace-only become ``None``.

    A blank hostname from an untouched form field and an absent one mean the
    same thing — "the router picks" — and letting ``""`` through would write
    ``spec.host: ""``, which the API server stores and which then reads as a
    hostname nobody can type.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def build_exposure(payload: dict[str, Any]) -> Exposure:
    """Validate a §13 exposure model off the wire, or 422 naming the field.

    Validation here is *semantic* — the shapes are already checked by the
    Pydantic model in :mod:`app.api.routes`. What is checked is the set of
    things a schema cannot express: that there is at least one target, that a
    reencrypt exposure was given the CA it needs to verify the pod it is about
    to trust, that a path starts with a slash.
    """
    name = _string(payload.get("name"))
    namespace = _string(payload.get("namespace"))
    if not name:
        raise Invalid(
            "An exposure needs a name.",
            hint="This becomes metadata.name on the object that is created.",
            context={"parameter": "name"},
        )
    if not namespace:
        raise Invalid(
            "An exposure needs a namespace.",
            hint="All three route kinds are namespaced, and they must sit in the "
                 "same namespace as the Service they target.",
            context={"parameter": "namespace"},
        )

    raw_targets = payload.get("targets") or []
    if not raw_targets:
        raise Invalid(
            "An exposure needs at least one target Service.",
            hint="Pick the Service that should receive the traffic.",
            context={"parameter": "targets"},
        )
    if len(raw_targets) > MAX_TARGETS:
        raise Invalid(
            f"An exposure can have at most {MAX_TARGETS} target Services "
            f"(got {len(raw_targets)}).",
            detail=(
                "The limit is the OpenShift Route one — a primary plus three "
                "alternateBackends — and it is applied to every backend so an "
                "exposure authored here stays portable between them."
            ),
            context={"parameter": "targets", "value": len(raw_targets)},
        )

    targets: list[Target] = []
    for index, raw in enumerate(raw_targets):
        service = _string(raw.get("service"))
        if not service:
            raise Invalid(
                f"Target {index + 1} has no Service name.",
                context={"parameter": f"targets[{index}].service"},
            )
        weight = raw.get("weight")
        if weight is not None and not 0 <= int(weight) <= 256:
            raise Invalid(
                f"Target {index + 1} has weight {weight}; the range is 0 to 256.",
                detail="256 is the maximum the Route API accepts for a backend weight.",
                context={"parameter": f"targets[{index}].weight", "value": weight},
            )
        targets.append(
            Target(
                service=service,
                port=raw.get("port"),
                weight=None if weight is None else int(weight),
            )
        )

    raw_tls = payload.get("tls") or {}
    termination = _string(raw_tls.get("termination"))
    if termination and termination not in TERMINATIONS:
        raise Invalid(
            f"{termination!r} is not a TLS termination mode.",
            detail=f"Modes: {', '.join(TERMINATIONS)}, or none for plain HTTP.",
            context={"parameter": "tls.termination", "value": termination},
        )
    insecure = _string(raw_tls.get("insecurePolicy"))
    if insecure and insecure not in INSECURE_POLICIES:
        raise Invalid(
            f"{insecure!r} is not an insecure-traffic policy.",
            detail=f"Policies: {', '.join(INSECURE_POLICIES)}.",
            context={"parameter": "tls.insecurePolicy", "value": insecure},
        )
    if insecure and insecure != "None" and not termination:
        raise Invalid(
            "An insecure-traffic policy only means something on a TLS exposure.",
            detail=(
                f"{insecure!r} says what to do with plain HTTP when HTTPS is also "
                "served. With no TLS termination there is nothing to redirect to "
                "or to allow alongside."
            ),
            hint="Choose a TLS termination mode, or clear the insecure-traffic policy.",
            context={"parameter": "tls.insecurePolicy", "value": insecure},
        )
    if insecure == "Allow" and termination == "passthrough":
        raise Invalid(
            "A passthrough exposure cannot also serve plain HTTP.",
            detail=(
                "The router does not terminate TLS on a passthrough exposure, so "
                "it has no plaintext listener to serve the same content on. The "
                "Route API rejects this combination too."
            ),
            hint="Use Redirect to send HTTP clients to HTTPS, or None to refuse them.",
            context={"parameter": "tls.insecurePolicy", "value": insecure},
        )

    # Inline PEM is refused rather than ignored. Silently dropping a key an
    # operator pasted would produce an exposure that is created and then does not
    # serve, with nothing on screen saying why — the field-eating this whole
    # module is built to avoid, on the one field where it is hardest to notice.
    for field_name in ("certificate", "key"):
        if _string(raw_tls.get(field_name)):
            raise Invalid(
                "This form does not take an inline certificate or private key.",
                detail=(
                    "A key sent here would travel through this request, back out "
                    "in the dry-run projection, into the diff, and into browser "
                    "memory. `redact_secret` cannot help: it guards Secrets, and "
                    "this one would sit in the object's own spec."
                ),
                hint=(
                    "Put the certificate in a Secret and reference it with "
                    "`secretName` — the only form an Ingress or a Gateway listener "
                    "can express anyway. To write `spec.tls.certificate` on a Route "
                    "regardless, use the YAML view: that document is written "
                    "verbatim."
                ),
                context={"parameter": f"tls.{field_name}"},
            )
    destination_ca = _string(raw_tls.get("destinationCACertificate"))
    if termination == "reencrypt" and not destination_ca:
        raise Invalid(
            "A reencrypt exposure needs the CA certificate of the pod it re-encrypts to.",
            detail=(
                "Without destinationCACertificate the router has nothing to verify "
                "the backend's certificate against. Some routers then fall back to "
                "not verifying it at all, which is a re-encrypted connection to "
                "whatever answers — the property reencrypt exists to provide, "
                "silently absent."
            ),
            hint="Paste the CA that signed the pod's serving certificate.",
            context={"parameter": "tls.destinationCACertificate"},
        )

    path = _string(payload.get("path"))
    if path and not path.startswith("/"):
        raise Invalid(
            f"A path has to start with a slash (got {path!r}).",
            context={"parameter": "path", "value": path},
        )
    path_type = _string(payload.get("pathType")) or "Prefix"
    if path_type not in PATH_TYPES:
        raise Invalid(
            f"{path_type!r} is not a path match kind.",
            detail=f"Kinds: {', '.join(PATH_TYPES)}.",
            context={"parameter": "pathType", "value": path_type},
        )

    host = _string(payload.get("host"))
    subdomain = _string(payload.get("subdomain"))
    if host and subdomain:
        raise Invalid(
            "Give either a hostname or a subdomain, not both.",
            detail=(
                "spec.host is the whole hostname; spec.subdomain is the left-hand "
                "label the router completes with its own domain. The Route API "
                "ignores subdomain when host is set, so an object with both does "
                "something other than what it appears to say."
            ),
            context={"parameter": "subdomain"},
        )

    wildcard = _string(payload.get("wildcardPolicy"))
    if wildcard and wildcard not in ("None", "Subdomain"):
        raise Invalid(
            f"{wildcard!r} is not a wildcard policy.",
            detail="Policies: None, Subdomain.",
            context={"parameter": "wildcardPolicy", "value": wildcard},
        )

    return Exposure(
        name=name,
        namespace=namespace,
        targets=tuple(targets),
        host=host,
        subdomain=subdomain,
        path=path,
        path_type=path_type,
        tls=TLSModel(
            termination=termination,
            insecure_policy=insecure,
            secret_name=_string(raw_tls.get("secretName")),
            destination_ca_certificate=destination_ca,
        ),
        wildcard_policy=wildcard,
        ingress_class_name=_string(payload.get("ingressClassName")),
        parent_refs=tuple(payload.get("parentRefs") or ()),
        labels=dict(payload.get("labels") or {}),
        annotations=dict(payload.get("annotations") or {}),
    )


# --------------------------------------------------------------------------- #
# Lossiness
# --------------------------------------------------------------------------- #

#: What each feature's absence actually does to traffic, per backend, and what
#: to do instead. Held as data rather than built into the compiler so that the
#: sentence an operator reads is reviewable next to the decision it describes.
#:
#: Every entry answers two questions, in this order: *what will happen to my
#: traffic if I go ahead*, and *what should I do instead*. A warning that
#: answers only the first is a warning people click past.
_CONSEQUENCES: dict[tuple[str, str], tuple[str, str]] = {
    ("ingress", FEATURE_PASSTHROUGH_TLS): (
        "The Ingress API cannot express passthrough. This exposure will be written "
        "with the router terminating TLS instead, so the TLS session ends at the "
        "router and the pod receives a new one — client certificates the pod "
        "expects will not arrive, and anything pinning the pod's own certificate "
        "will fail.",
        "Write this as an OpenShift Route if the cluster serves them, or use a "
        "Gateway API TLSRoute, which this console does not write from here. Some "
        "controllers offer passthrough through an annotation; that annotation is "
        "specific to one controller and this console will not guess which one you "
        "are running.",
    ),
    ("ingress", FEATURE_REENCRYPT_TLS): (
        "The Ingress API cannot express re-encryption to the pod as a portable "
        "field. This exposure will be written with edge termination, so the hop "
        "from the router to the pod is plaintext inside the cluster network.",
        "Write this as an OpenShift Route, or add your controller's own "
        "backend-protocol annotation to the YAML by hand after reviewing the "
        "diff — the console will preserve an annotation you write yourself.",
    ),
    ("ingress", FEATURE_INSECURE_REDIRECT): (
        "The Ingress API has no portable redirect field. Plain HTTP requests will "
        "reach the same backend unencrypted rather than being redirected to HTTPS, "
        "unless your controller redirects by default.",
        "Most controllers offer an ssl-redirect annotation; add it to the YAML "
        "view and it will be preserved. On the router this console installs, "
        "`ssl-redirect: \"true\"` is the annotation.",
    ),
    ("ingress", FEATURE_INSECURE_ALLOW): (
        "The Ingress API has no field for this. Whether plain HTTP is served "
        "alongside HTTPS is decided entirely by your controller's defaults.",
        "Check your controller's ssl-redirect default; there is nothing to write "
        "on the object itself.",
    ),
    ("ingress", FEATURE_WEIGHTED_BACKENDS): (
        "The Ingress API has no weight field. Only the first target will receive "
        "traffic — the others will be written into the document as additional "
        "paths only if you add them by hand, and by default they receive nothing.",
        "Write this as an OpenShift Route or a Gateway API HTTPRoute, both of "
        "which have a real weight field.",
    ),
    ("ingress", FEATURE_WILDCARD_SUBDOMAIN): (
        "The Ingress API has no wildcard policy. The exposure will answer for the "
        "exact hostname only; requests to subdomains of it will not match.",
        "Add a second rule with a `*.` hostname in the YAML view — Ingress "
        "supports a wildcard in the host itself, which covers one label but is "
        "not the same as a Route's Subdomain policy.",
    ),
    ("ingress", FEATURE_GENERATED_HOST): (
        "An Ingress with no hostname matches every hostname that reaches the "
        "controller, which is almost never what 'let the router pick' means. It "
        "will collide with every other host-less Ingress on the same controller.",
        "Give it a hostname. Only an OpenShift Route can be told to generate one, "
        "because only OpenShift has a cluster wildcard domain to generate it from.",
    ),
    ("gateway", FEATURE_EDGE_TLS): (
        "An HTTPRoute has no TLS field. TLS belongs to the listener on its parent "
        "Gateway, so this exposure will be written without any TLS configuration "
        "and will be served however that listener is already configured.",
        "Configure the certificate on the Gateway's listener. If its listener is "
        "plaintext, this exposure will be served over plain HTTP.",
    ),
    ("gateway", FEATURE_PASSTHROUGH_TLS): (
        "An HTTPRoute terminates HTTP; it cannot pass TLS through. This exposure "
        "will be written with no TLS handling at all.",
        "Use a TLSRoute with a passthrough listener on the Gateway. This console "
        "does not write TLSRoutes from here.",
    ),
    ("gateway", FEATURE_REENCRYPT_TLS): (
        "An HTTPRoute cannot express re-encryption to the pod. The hop from the "
        "Gateway to the pod will be plaintext.",
        "Attach a BackendTLSPolicy to the Service. This console does not write "
        "one from here; the Gateway page lists the ones that exist.",
    ),
    ("gateway", FEATURE_INSECURE_ALLOW): (
        "Gateway API expresses this by having a plaintext listener as well as a "
        "TLS one, which is a property of the Gateway rather than of this route.",
        "Add an HTTP listener to the parent Gateway and attach this route to both.",
    ),
    ("gateway", FEATURE_WILDCARD_SUBDOMAIN): (
        "An HTTPRoute has no wildcard policy field.",
        "Put a `*.` hostname in spec.hostnames — Gateway API accepts a wildcard "
        "prefix there, which covers one label.",
    ),
    ("gateway", FEATURE_GENERATED_HOST): (
        "An HTTPRoute with no hostnames inherits every hostname its Gateway "
        "listener serves, rather than being given one of its own.",
        "Give it a hostname, or accept that it answers for the listener's whole "
        "hostname set.",
    ),
    ("openshift", FEATURE_PATH_EXACT): (
        "A Route matches its path as a prefix; there is no exact-match mode. "
        "`/api` will also match `/api/v2/things`.",
        "Write this as an Ingress or an HTTPRoute if exact matching is the point, "
        "or accept prefix matching.",
    ),
}


def _lossy(backend: RouteBackend, wanted: set[str]) -> list[dict[str, Any]]:
    """Every requested feature this backend cannot express, with consequences.

    A feature with no entry in :data:`_CONSEQUENCES` still appears, with a
    generic sentence. That is deliberate: the missing entry is a gap in this
    table, and hiding the feature because the table is incomplete would turn a
    documentation gap into a silent behaviour change.
    """
    entries: list[dict[str, Any]] = []
    for feature in sorted(wanted - backend.features):
        consequence, mitigation = _CONSEQUENCES.get(
            (backend.key, feature),
            (
                f"{backend.kind} objects cannot express this, so it will not be "
                "part of the object that is written.",
                "Choose a different route backend, or drop this from the exposure.",
            ),
        )
        entries.append({
            "feature": feature,
            "label": FEATURE_LABELS[feature],
            "consequence": consequence,
            "mitigation": mitigation,
        })
    return entries


# --------------------------------------------------------------------------- #
# Compilation — one function per backend
# --------------------------------------------------------------------------- #

def _metadata(exposure: Exposure, existing: dict[str, Any]) -> dict[str, Any]:
    """Merge the form's name, namespace, labels and annotations into ``existing``.

    Merged rather than replaced. The form owns the keys it is showing; every
    other label and annotation on the object — the ones a controller wrote, the
    ones a GitOps tool uses to find it, ``kubectl.kubernetes.io/last-applied-
    configuration`` — belongs to somebody else and stays.
    """
    metadata = dict(existing.get("metadata") or {})
    metadata["name"] = exposure.name
    metadata["namespace"] = exposure.namespace
    if exposure.labels:
        metadata["labels"] = {**(metadata.get("labels") or {}), **exposure.labels}
    if exposure.annotations:
        metadata["annotations"] = {
            **(metadata.get("annotations") or {}), **exposure.annotations,
        }
    return metadata


def _port_value(port: int | str | None) -> int | str | None:
    """A port as the API wants it: an int stays an int, a name stays a string.

    ``"8080"`` from a text input becomes ``8080``, because a Service port
    *number* written as a string is a different thing to the API server from a
    port *name*, and an Ingress with ``port: {name: "8080"}`` matches no port at
    all.
    """
    if port is None:
        return None
    if isinstance(port, int):
        return port
    text = str(port).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _compile_route(exposure: Exposure, existing: dict[str, Any], version: str) -> dict[str, Any]:
    """``route.openshift.io`` Route — the lossless one."""
    document = copy.deepcopy(existing)
    document["apiVersion"] = f"route.openshift.io/{version}"
    document["kind"] = "Route"
    document["metadata"] = _metadata(exposure, document)

    spec = dict(document.get("spec") or {})
    primary = exposure.primary

    # host and subdomain are mutually exclusive and the model already refused
    # both. Clearing the other one matters: an operator switching a Route from a
    # fixed hostname to a generated one leaves spec.host behind otherwise, and
    # spec.host wins — so the change appears to do nothing.
    if exposure.host:
        spec["host"] = exposure.host
        spec.pop("subdomain", None)
    elif exposure.subdomain:
        spec["subdomain"] = exposure.subdomain
        spec.pop("host", None)
    else:
        spec.pop("host", None)
        spec.pop("subdomain", None)

    if exposure.path:
        spec["path"] = exposure.path
    else:
        spec.pop("path", None)

    spec["to"] = {
        "kind": "Service",
        "name": primary.service,
        **({"weight": primary.weight} if primary.weight is not None else {}),
    }
    alternates = [
        {
            "kind": "Service",
            "name": target.service,
            **({"weight": target.weight} if target.weight is not None else {}),
        }
        for target in exposure.targets[1:]
    ]
    if alternates:
        spec["alternateBackends"] = alternates
    else:
        spec.pop("alternateBackends", None)

    port = _port_value(primary.port)
    if port is not None:
        spec["port"] = {"targetPort": port}
    else:
        spec.pop("port", None)

    if exposure.tls.termination:
        tls: dict[str, Any] = dict(spec.get("tls") or {})
        tls["termination"] = exposure.tls.termination
        if exposure.tls.insecure_policy:
            tls["insecureEdgeTerminationPolicy"] = exposure.tls.insecure_policy
        else:
            tls.pop("insecureEdgeTerminationPolicy", None)
        if exposure.tls.secret_name:
            tls["externalCertificate"] = {"name": exposure.tls.secret_name}
            # An inline pair already on the object is removed when the operator
            # switches to a reference: the Route API rejects both together, and
            # leaving the key behind would keep it in the spec after the operator
            # believed they had moved it into a Secret.
            tls.pop("certificate", None)
            tls.pop("key", None)
        if exposure.tls.destination_ca_certificate:
            tls["destinationCACertificate"] = exposure.tls.destination_ca_certificate
        elif exposure.tls.termination != "reencrypt":
            # Only meaningful on reencrypt. Leaving a stale one behind on a Route
            # switched from reencrypt to edge is confusing to read and is exactly
            # the kind of field an operator later "fixes" in the wrong direction.
            tls.pop("destinationCACertificate", None)
        spec["tls"] = tls
    else:
        spec.pop("tls", None)

    if exposure.wildcard_policy:
        spec["wildcardPolicy"] = exposure.wildcard_policy
    else:
        spec.pop("wildcardPolicy", None)

    document["spec"] = spec
    return document


def _compile_ingress(exposure: Exposure, existing: dict[str, Any], version: str) -> dict[str, Any]:
    """``networking.k8s.io`` Ingress — the portable, lossy one.

    Only the **first** rule and its first path are rewritten. An Ingress an
    operator built with four rules keeps the other three: the form is showing
    one exposure, and the other rules are not it. ``preserved[]`` names them so
    the UI can say the form is not the whole object.
    """
    document = copy.deepcopy(existing)
    document["apiVersion"] = f"networking.k8s.io/{version}"
    document["kind"] = "Ingress"
    document["metadata"] = _metadata(exposure, document)

    spec = dict(document.get("spec") or {})
    if exposure.ingress_class_name:
        spec["ingressClassName"] = exposure.ingress_class_name

    port = _port_value(exposure.primary.port)
    backend: dict[str, Any] = {
        "service": {
            "name": exposure.primary.service,
            # An Ingress backend port is `{number}` or `{name}`, never both and
            # never bare. A missing port here is not defaultable: the API server
            # rejects the object, which is better than guessing 80 and routing
            # to something that happens to listen there.
            "port": (
                {"number": port} if isinstance(port, int)
                else {"name": port} if port is not None
                else {}
            ),
        }
    }

    rules = list(spec.get("rules") or [])
    path_entry = {
        "path": exposure.path or "/",
        "pathType": "Exact" if exposure.path_type == "Exact" else "Prefix",
        "backend": backend,
    }
    if rules:
        first = dict(rules[0])
        http = dict(first.get("http") or {})
        paths = list(http.get("paths") or [])
        # Merge into the existing first path rather than replacing the list, so
        # a hand-written second path on the same rule survives.
        if paths:
            merged = dict(paths[0])
            merged.update(path_entry)
            paths[0] = merged
        else:
            paths = [path_entry]
        http["paths"] = paths
        first["http"] = http
        if exposure.host:
            first["host"] = exposure.host
        else:
            first.pop("host", None)
        rules[0] = first
    else:
        rules = [
            {
                **({"host": exposure.host} if exposure.host else {}),
                "http": {"paths": [path_entry]},
            }
        ]
    spec["rules"] = rules

    # TLS. Only edge is expressible, and only with a Secret — an Ingress has no
    # field for an inline certificate, so one supplied in the form is reported
    # as lossy rather than dropped into an annotation.
    if exposure.tls.termination and exposure.tls.secret_name:
        hosts = [exposure.host] if exposure.host else []
        spec["tls"] = [
            {
                **({"hosts": hosts} if hosts else {}),
                "secretName": exposure.tls.secret_name,
            }
        ]
    elif exposure.tls.termination and not exposure.tls.secret_name:
        # A TLS block with no secretName tells the controller to serve its own
        # default certificate. That is a real configuration and the honest
        # rendering of "terminate TLS, I have not given you a certificate".
        spec["tls"] = [{"hosts": [exposure.host]} if exposure.host else {}]
    else:
        spec.pop("tls", None)

    document["spec"] = spec
    return document


def _compile_httproute(exposure: Exposure, existing: dict[str, Any], version: str) -> dict[str, Any]:
    """``gateway.networking.k8s.io`` HTTPRoute.

    Weights are real here, and so is the HTTP-to-HTTPS redirect — but the
    redirect is a *filter on a rule*, and a rule cannot both redirect and
    forward. So a redirect produces a **second** rule that redirects, alongside
    the one that forwards. Writing the filter onto the forwarding rule instead
    would produce a route that redirects everything and never reaches the
    Service, which is the failure this shape exists to avoid.
    """
    document = copy.deepcopy(existing)
    document["apiVersion"] = f"gateway.networking.k8s.io/{version}"
    document["kind"] = "HTTPRoute"
    document["metadata"] = _metadata(exposure, document)

    spec = dict(document.get("spec") or {})
    if exposure.parent_refs:
        spec["parentRefs"] = [dict(ref) for ref in exposure.parent_refs]
    if exposure.host:
        spec["hostnames"] = [exposure.host]
    else:
        spec.pop("hostnames", None)

    match = {
        "path": {
            "type": "Exact" if exposure.path_type == "Exact" else "PathPrefix",
            "value": exposure.path or "/",
        }
    }
    backend_refs = []
    for target in exposure.targets:
        ref: dict[str, Any] = {"name": target.service}
        port = _port_value(target.port)
        # Gateway API's backendRef port is an integer only — there is no name
        # form. A named port has to be resolved to a number before it can be
        # written, and this compiler does not read Services, so it is left out
        # and reported rather than written as something the API will reject.
        if isinstance(port, int):
            ref["port"] = port
        if target.weight is not None:
            ref["weight"] = target.weight
        backend_refs.append(ref)

    forward_rule: dict[str, Any] = {"matches": [match], "backendRefs": backend_refs}

    rules = [forward_rule]
    if exposure.tls.insecure_policy == "Redirect":
        rules.append({
            "matches": [match],
            "filters": [
                {
                    "type": "RequestRedirect",
                    "requestRedirect": {"scheme": "https", "statusCode": 301},
                }
            ],
        })
    spec["rules"] = rules

    document["spec"] = spec
    return document


_COMPILERS = {
    "openshift": _compile_route,
    "ingress": _compile_ingress,
    "gateway": _compile_httproute,
}


# --------------------------------------------------------------------------- #
# What the form does not model
# --------------------------------------------------------------------------- #

#: Top-level paths the form owns on each backend. Anything in the document
#: outside these is reported in ``preserved[]``.
#:
#: This is how the UI answers "is the form showing me everything". A form that
#: could not say what it was hiding would leave an operator to discover, at
#: confirm time, that the diff changes a field they never saw a control for.
_FORM_OWNED: dict[str, frozenset[str]] = {
    "openshift": frozenset({
        "host", "subdomain", "path", "to", "alternateBackends", "port", "tls",
        "wildcardPolicy",
    }),
    "ingress": frozenset({"ingressClassName", "rules", "tls"}),
    "gateway": frozenset({"parentRefs", "hostnames", "rules"}),
}


def _preserved(backend: RouteBackend, document: dict[str, Any]) -> list[str]:
    """Spec paths present in the document that the form does not show.

    Reported as dotted paths rather than as a boolean, because "the form is not
    showing you everything" is not actionable and "the form is not showing you
    ``spec.rules[1]``" is.
    """
    owned = _FORM_OWNED[backend.key]
    spec = document.get("spec") or {}
    preserved = [f"spec.{key}" for key in sorted(spec) if key not in owned]

    # Extra rules and paths are the common case and the one worth naming
    # individually: they are how a single Ingress object carries several
    # exposures, and the form is only ever editing the first.
    if backend.key == "ingress":
        rules = spec.get("rules") or []
        for index in range(1, len(rules)):
            preserved.append(f"spec.rules[{index}]")
        if rules:
            paths = ((rules[0] or {}).get("http") or {}).get("paths") or []
            for index in range(1, len(paths)):
                preserved.append(f"spec.rules[0].http.paths[{index}]")
        tls = spec.get("tls") or []
        for index in range(1, len(tls)):
            preserved.append(f"spec.tls[{index}]")
    if backend.key == "gateway":
        rules = spec.get("rules") or []
        # The compiler writes rule 0 (forward) and optionally rule 1 (redirect).
        # Anything beyond that was written by hand.
        for index in range(2, len(rules)):
            preserved.append(f"spec.rules[{index}]")

    annotations = (document.get("metadata") or {}).get("annotations") or {}
    for key in sorted(annotations):
        if key.startswith("kubectl.kubernetes.io/"):
            continue
        preserved.append(f"metadata.annotations[{key}]")

    return preserved


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #

def render(
    backend_key: str,
    payload: dict[str, Any] | None,
    *,
    document: str | None = None,
) -> dict[str, Any]:
    """§13 ``POST /api/routes/render`` — compile an exposure into one object.

    **This writes nothing.** It touches the cluster only to find out which
    version of the backend's API is served, and it returns a document, not a
    mutation response. It is not preflighted and not audited, because there is
    nothing to preflight and nothing happened.

    Two modes, and the second one is the reason the YAML view is honest:

    * ``payload`` given — the form's fields are compiled and patched into
      ``document``. This is the form view.
    * ``payload`` **None** with ``document`` given — the document is taken
      verbatim. Nothing is compiled, so ``lossy`` is empty and ``preserved`` is
      empty: the console dropped nothing because the console decided nothing.
      This is the YAML view, and without it a hand-edited document would have
      the form's fields re-applied over the top of it at write time — silently
      undoing the edit the UI had just locked the form to protect.

    Args:
        backend_key: ``openshift``, ``ingress`` or ``gateway``.
        payload: the §13 exposure model, or ``None`` for the verbatim mode.
        document: the YAML currently in the editor. Required when ``payload`` is
            ``None``; there would otherwise be nothing to write.

    Returns:
        ``{"yaml", "document", "backend", "lossy", "preserved", "requested"}``.
        ``lossy`` is the list the write endpoint requires acknowledgement of.
    """
    backend = routes_service.resolve_backend(backend_key)
    state = routes_service.backend_state(backend)
    if state.state == routes_service.STATE_UNKNOWN:
        # The real discovery failure, not a guess at a version. Rendering
        # against an assumed apiVersion would produce a document that is
        # rejected at apply time with a message about the wrong thing.
        assert state.error is not None
        raise state.error
    if not state.available:
        raise Unsupported(
            f"This cluster does not serve {backend.kind} objects.",
            detail=state.detail,
            hint="GET /api/routes/capabilities lists the backends this cluster serves.",
            context={"backend": backend.key, "group": backend.group},
        )

    version = state.version
    assert version is not None

    existing: dict[str, Any] = {}
    if document and document.strip():
        existing = apply_service.parse_document(document)

    if payload is None:
        if not existing:
            raise Invalid(
                "There is nothing to write: no exposure and no document.",
                hint="Fill in the form, or paste an object into the YAML view.",
                context={"parameter": "document"},
            )
        # Verbatim. No compilation, so nothing was dropped and nothing is
        # hidden — `lossy` and `preserved` are empty as statements of fact, not
        # as defaults nobody filled in.
        return {
            "backend": backend.key,
            "kind": backend.kind,
            "group": backend.group,
            "version": version,
            "plural": backend.plural,
            "document": existing,
            "yaml": reader.to_yaml(existing),
            "lossy": [],
            "preserved": [],
            "requested": [],
            "verbatim": True,
        }

    exposure = build_exposure(payload)
    requested = exposure.requested_features()
    compiled = _COMPILERS[backend.key](exposure, existing, version)

    return {
        "backend": backend.key,
        "kind": backend.kind,
        "group": backend.group,
        "version": version,
        "plural": backend.plural,
        "document": compiled,
        "yaml": reader.to_yaml(compiled),
        "lossy": _lossy(backend, requested),
        "preserved": _preserved(backend, compiled),
        "requested": sorted(requested),
        "verbatim": False,
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #

def _require_acknowledgement(
    lossy: list[dict[str, Any]], acknowledged: list[str] | None,
) -> None:
    """Refuse a lossy write the caller has not explicitly accepted.

    The same shape as ``force`` on a node drain: the console has computed a
    consequence, and confirming has to be a distinct act from requesting. The
    acknowledgement names the features, so a UI that acknowledged everything
    once and then changed the form has to acknowledge the *new* list — a boolean
    would stay true across an edit that added a consequence nobody read.

    Extra tokens are accepted; missing ones are not. A caller who acknowledged a
    consequence that turned out not to apply has not consented to anything that
    is happening.
    """
    if not lossy:
        return
    accepted = set(acknowledged or ())
    missing = [entry for entry in lossy if entry["feature"] not in accepted]
    if not missing:
        return
    raise Invalid(
        "This exposure asks for something the chosen route backend cannot express.",
        detail="; ".join(f"{entry['label']}: {entry['consequence']}" for entry in missing),
        hint=(
            "Re-send with acknowledgeLossy naming each of "
            f"[{', '.join(entry['feature'] for entry in missing)}] to write it "
            "anyway, or choose a backend that supports them."
        ),
        context={
            "parameter": "acknowledgeLossy",
            "unacknowledged": [entry["feature"] for entry in missing],
        },
    )



#: OpenShift gates *setting a hostname on a Route* behind its own RBAC
#: subresource, separately from creating the Route at all.
CUSTOM_HOST_SUBRESOURCE = "custom-host"


def _custom_host_subresources(
    backend: RouteBackend, exposure: Exposure | None, document: dict[str, Any],
) -> tuple[str, ...]:
    """``("custom-host",)`` when this write needs that grant, else empty.

    A **pure decision**, handed to the funnel as ``also_requires`` rather than
    reviewed here. Invariant 2 promises a denial that names the missing
    permission, and this is the one case in §13 where the funnel alone would
    not: OpenShift gates *choosing a hostname* behind ``routes/custom-host``, a
    separate RBAC subresource, so a ServiceAccount can hold ``create routes``,
    pass the funnel's own review, and still have the API server refuse the
    write. The operator is then told they cannot create Routes — a permission
    the review just confirmed they hold — and goes looking in the wrong
    ClusterRole.

    **Deciding here and reviewing there is the point.** This check used to run
    before the write reached :func:`app.admin.mutate.mutate`, which put it ahead
    of the mutations gate: on a read-only console a Route carrying a hostname
    was answered ``403 rbac_denied``, sending somebody to widen a ClusterRole
    when the deployment simply was not permitted to write at all. §1.3 gives
    ``mutations_disabled`` its own code precisely so that cannot happen, and the
    ordering inside the funnel is what keeps the promise. The denial is audited
    there too, against the subresource that was actually refused.

    Only for Routes, and only when a hostname is actually set: a Route with
    ``spec.subdomain``, or one letting the router generate the name entirely, is
    not a custom host and does not need the grant. Asking for it anyway would
    disable a control for a permission the action does not require, which is the
    same defect pointed the other way.
    """
    if backend.key != "openshift":
        return ()
    host = (
        exposure.host if exposure is not None
        else (document.get("spec") or {}).get("host")
    )
    return (CUSTOM_HOST_SUBRESOURCE,) if host else ()


def _audit_detail(exposure: Exposure, backend: RouteBackend, verb: str) -> str:
    """The audit sentence for an exposure write.

    Specific on purpose. "create Route checkout" is what the generic §4 create
    would record, and it does not answer the question this trail is going to be
    asked: *who exposed the payments service to the internet, on what hostname*.
    The sentence below does, in a form that fits in a table.
    """
    where = exposure.host or (
        f"*.{exposure.subdomain}" if exposure.subdomain else "a router-assigned hostname"
    )
    scheme = "https" if exposure.tls.termination else "http"
    targets = ", ".join(
        f"{t.service}{'' if t.port is None else f':{t.port}'}"
        f"{'' if t.weight is None else f' (weight {t.weight})'}"
        for t in exposure.targets
    )
    tls = exposure.tls.termination or "no TLS"
    return (
        f"{verb} {backend.kind} {exposure.name}: expose {targets} at "
        f"{scheme}://{where}{exposure.path or '/'} ({tls})"
    )


def _verbatim_detail(
    rendered: dict[str, Any], verb: str, namespace: str, name: str,
) -> str:
    """The audit sentence for a document written verbatim.

    Deliberately different from the compiled one, and it says so. The compiled
    sentence describes an exposure the console understood — "expose checkout:8080
    at https://…". For a hand-written document the console did not model the
    intent and must not narrate one it inferred; what it can say truthfully is
    which object was written and that the operator authored it.
    """
    return (
        f"{verb} {rendered['kind']} {namespace}/{name} "
        "(written verbatim from the YAML view)"
    )


def create_route(
    backend_key: str,
    payload: dict[str, Any] | None,
    *,
    document: str | None = None,
    acknowledge_lossy: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """§13 ``POST /api/routes`` — create one exposure.

    Delegates the write itself to :func:`app.admin.apply.create_from_yaml`, which
    is what routes it through :func:`app.admin.mutate.mutate`. Building a second
    create path here would be a second place for the mutations gate, the
    preflight, the dry run and the audit row to be got right — and the copy that
    drifted would be indistinguishable from outside.

    The rendered document and its ``lossy`` list ride along in the response so
    the UI shows the operator the same consequences beside the diff they are
    about to confirm.
    """
    rendered = render(backend_key, payload, document=document)
    _require_acknowledgement(rendered["lossy"], acknowledge_lossy)
    backend = routes_service.resolve_backend(backend_key)

    if payload is None:
        metadata = rendered["document"].get("metadata") or {}
        namespace = metadata.get("namespace")
        exposure = None
        detail = _verbatim_detail(
            rendered, "create", namespace or "?", metadata.get("name") or "?",
        )
    else:
        exposure = build_exposure(payload)
        namespace = exposure.namespace
        detail = _audit_detail(exposure, backend, "create")

    result = apply_service.create_from_yaml(
        rendered["group"],
        rendered["version"],
        rendered["plural"],
        namespace,
        rendered["yaml"],
        dry_run,
        detail=detail,
        # Reviewed inside the funnel, after the gate. See
        # `_custom_host_subresources` for why the ordering is load-bearing.
        also_requires=_custom_host_subresources(
            backend, exposure, rendered["document"],
        ),
    )
    result["route"] = {
        "backend": rendered["backend"],
        "kind": rendered["kind"],
        "lossy": rendered["lossy"],
        "preserved": rendered["preserved"],
        "verbatim": rendered["verbatim"],
    }
    return result


def update_route(
    backend_key: str,
    namespace: str,
    name: str,
    payload: dict[str, Any] | None,
    *,
    resource_version: str,
    document: str | None = None,
    acknowledge_lossy: list[str] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """§13 ``PUT /api/routes/{backend}/{namespace}/{name}`` — replace one exposure.

    Delegates to :func:`app.admin.apply.update_from_yaml`, which carries rule 4:
    the caller's ``resourceVersion`` goes into the submitted body, so the API
    server enforces the same check the local one does and the window between our
    read and our write is closed.
    """
    rendered = render(backend_key, payload, document=document)
    _require_acknowledgement(rendered["lossy"], acknowledge_lossy)
    backend = routes_service.resolve_backend(backend_key)

    if payload is None:
        # `update_from_yaml` refuses a document whose metadata.name disagrees
        # with the URL, so the rename guard below is not repeated here — it
        # would be a second implementation of a check that already exists and
        # already has the better message.
        exposure = None
        detail = _verbatim_detail(rendered, "replace", namespace, name)
    else:
        exposure = build_exposure(payload)
        if exposure.name != name:
            raise Invalid(
                f'The URL names "{name}" but the exposure is called "{exposure.name}".',
                hint=(
                    "A replace cannot rename an object. Create the new exposure and "
                    "delete the old one — which is also what has to happen to its "
                    "hostname claim."
                ),
                context={"parameter": "name", "value": exposure.name},
            )
        detail = _audit_detail(exposure, backend, "replace")

    result = apply_service.update_from_yaml(
        rendered["group"],
        rendered["version"],
        rendered["plural"],
        namespace,
        name,
        rendered["yaml"],
        resource_version,
        dry_run,
        detail=detail,
        also_requires=_custom_host_subresources(
            backend, exposure, rendered["document"],
        ),
    )
    result["route"] = {
        "backend": rendered["backend"],
        "kind": rendered["kind"],
        "lossy": rendered["lossy"],
        "preserved": rendered["preserved"],
        "verbatim": rendered["verbatim"],
    }
    return result


def delete_route(
    backend_key: str, namespace: str, name: str, *, dry_run: bool = True,
) -> dict[str, Any]:
    """§13 ``DELETE /api/routes/{backend}/{namespace}/{name}``.

    Deleting an exposure takes a hostname out of service. The diff shows the
    whole object as a removal — including the hostname — which is the disclosure
    that matters here: the operator is confirming that a specific public
    hostname stops answering.
    """
    backend = routes_service.resolve_backend(backend_key)
    state = routes_service.backend_state(backend)
    if state.state == routes_service.STATE_UNKNOWN:
        assert state.error is not None
        raise state.error
    if not state.available:
        raise Unsupported(
            f"This cluster does not serve {backend.kind} objects.",
            detail=state.detail,
            context={"backend": backend.key, "group": backend.group},
        )
    version = state.version
    assert version is not None

    return apply_service.delete_resource(
        backend.group, version, backend.plural, namespace, name, "Background", dry_run,
        detail=f"delete {backend.kind} {namespace}/{name} (an exposure stops answering)",
    )


__all__ = [
    "CUSTOM_HOST_SUBRESOURCE",
    "Exposure",
    "INSECURE_POLICIES",
    "MAX_TARGETS",
    "PATH_TYPES",
    "TERMINATIONS",
    "TLSModel",
    "Target",
    "build_exposure",
    "create_route",
    "delete_route",
    "render",
    "update_route",
]
