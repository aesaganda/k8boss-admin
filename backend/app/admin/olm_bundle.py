"""
The Operator Lifecycle Manager bundle this console ships (§33), as vendored data.

This is the **second** bundle k8boss-admin installs, and ADR-0004 said there
would never be one. `docs/adr-0008-shipped-olm.md` is where that boundary is
re-opened, with the argument on both sides; read it before adding a third. What
follows is the mechanics.

## Vendored, not authored

:mod:`app.admin.router_bundle` *constructs* its objects from Python, because the
console chose that router's shape — the image, the class name, the four
deliberate departures from upstream. Nothing here is chosen. OLM's manifests are
upstream's release artifacts, checked in **byte for byte** under ``deploy/olm/``
and parsed at run time:

* ``deploy/olm/crds.yaml`` — eight CustomResourceDefinitions, 1.1 MiB of them.
* ``deploy/olm/olm.yaml`` — the nineteen objects that make up OLM itself.

Re-typing a 1 MiB CRD schema into Python would be a transcription with no
reviewer, and the first typo in it would be a cluster's operator API silently
missing a field. So the files are the source, :data:`FILE_DIGESTS` pins their
SHA-256, and ``tests/test_olm.py`` fails the build when a vendored file and its
digest disagree. That check is what makes "byte for byte upstream" a fact rather
than a claim in a docstring: a local edit to a vendored manifest — however well
meant — stops the build.

## The three deliberate differences from `kubectl apply -f`

Everything else is upstream's bytes. These three are ours, and each exists
because applying upstream's file unchanged would be worse:

1. **Every object is labelled.** ``app.kubernetes.io/managed-by: k8boss-admin``
   is added to ``metadata.labels``, alongside whatever upstream already put
   there and never replacing it. This is not bookkeeping — it is the whole
   safety property. It is how :func:`app.admin.olm.install` can tell a cluster
   with no OLM from a cluster that already runs one, and refuse the second case
   instead of writing OLM 0.35.0 over somebody's OLM 0.30 control plane. A
   console that overwrote a running OLM because the object names matched would
   take out every operator on the cluster, and the operators' workloads with
   them.

2. **The community CatalogSource is not installed unless asked for.** Upstream's
   ``olm.yaml`` ends with a ``CatalogSource`` pulling
   ``quay.io/operatorhubio/catalog:latest``, re-polled hourly. Installing it by
   default would make this console the thing that decided a cluster trusts
   operatorhub.io, and would do it with an unpinned tag — ADR-0004's first
   deliberate difference from the router's upstream, which is there because a
   data plane that changes major version on a pod restart is not something to
   install from a console. A catalog that changes its *contents* on an hourly
   poll is the same defect aimed at the list of software an operator installs
   from. So it is opt-in, by name, with the consequence stated; see
   :data:`COMMUNITY_CATALOG_NAME` and §33's ``community_catalog`` consequence.

   An OLM with no CatalogSource is a working OLM with an empty catalog. That is
   an honest state and the portal renders it as one: zero packages, because the
   cluster subscribes to no catalog, not because a read failed.

3. **The install is two ordered phases with a wait between them**, which
   ``kubectl apply -f olm.yaml`` cannot be. See :data:`PHASE_CRDS` below.

## Why two phases

Eleven of the nineteen objects in ``olm.yaml`` are ``operators.coreos.com``
kinds — the ``OLMConfig``, two ``OperatorGroup``s, the ``packageserver``
``ClusterServiceVersion``, and the optional ``CatalogSource``. Those APIs do not
exist until the CRDs in phase one are **Established**, which is a condition the
apiextensions controller writes some time after the create returns 201. Applying
both files in one pass is how ``kubectl apply -f olm.yaml`` fails on a fresh
cluster with ``no matches for kind "OLMConfig"``, and it is why upstream's own
installer applies ``crds.yaml``, waits, and only then applies ``olm.yaml``.

The wait belongs to :mod:`app.admin.olm`, not here — this module is pure and
reads no cluster — but the phase each object belongs to is a property of the
object, so it is recorded on :class:`BundleObject` where the ordering cannot
drift from the data.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import pathlib
from dataclasses import dataclass
from typing import Any

from app import yaml_dialect

#: Upstream release these manifests were taken from. Bumping it means replacing
#: **both** files under ``deploy/olm/`` with that release's artifacts and
#: updating :data:`FILE_DIGESTS` — not just editing this string. The digests are
#: what make that impossible to get half-right.
OLM_VERSION = "0.35.0"

#: Where the release artifacts came from, recorded so the vendored copies can be
#: checked against upstream by anyone, at any time, without trusting this repo.
UPSTREAM_RELEASE = (
    "https://github.com/operator-framework/operator-lifecycle-manager/releases/"
    f"download/v{OLM_VERSION}"
)

_MANIFEST_DIR = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "olm"

#: SHA-256 of each vendored file, as downloaded. Pinned here rather than only in
#: a test fixture so that the constant a reviewer reads and the constant the
#: build enforces are the same one.
FILE_DIGESTS: dict[str, str] = {
    "crds.yaml": "0b66ca9d94298f04ec0704887adf663003447f50ca906187aa0ed14d701a9bd7",
    "olm.yaml": "5756646581f5a13fab43a20e7c548492c2494ed5899d8ad2d18c0c3032d2a590",
}

#: The two phases, in order. An object's phase is not a preference — phase two's
#: ``operators.coreos.com`` kinds have no API to be written into until phase
#: one's CRDs are Established. See the module docstring.
PHASE_CRDS = "crds"
PHASE_CORE = "core"
PHASES = (PHASE_CRDS, PHASE_CORE)

#: The CatalogSource upstream ships and this console does not install by default.
#: Matched by name *and* kind, so a rename upstream turns into a test failure
#: rather than into silently shipping it.
COMMUNITY_CATALOG_NAME = "operatorhubio-catalog"
COMMUNITY_CATALOG_KIND = "CatalogSource"

#: The image the community CatalogSource pulls, quoted here so the consequence
#: the operator acknowledges names the actual thing rather than "a catalog".
COMMUNITY_CATALOG_IMAGE = "quay.io/operatorhubio/catalog:latest"

#: Added to every object's ``metadata.labels``, never replacing what upstream
#: put there. See difference 1 in the module docstring: this label is the
#: adoption check, and the adoption check is what stops an install from
#: overwriting a running OLM.
MANAGED_BY = "k8boss-admin"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
VERSION_LABEL = "app.kubernetes.io/version"
PART_OF_LABEL = "app.kubernetes.io/part-of"

LABELS: dict[str, str] = {
    MANAGED_BY_LABEL: MANAGED_BY,
    PART_OF_LABEL: "operator-lifecycle-manager",
    VERSION_LABEL: OLM_VERSION,
}

#: The namespaces OLM's own manifests create. Not configurable, and that is
#: upstream's decision rather than this console's: OLM's Deployments, its
#: ClusterRoleBinding subject and the ``packageserver`` CSV all name ``olm``
#: literally, and the global OperatorGroup names ``operators``. Offering an
#: operator a namespace field here would be offering a choice that produces a
#: broken install.
OLM_NAMESPACE = "olm"
OPERATORS_NAMESPACE = "operators"
NAMESPACES = (OLM_NAMESPACE, OPERATORS_NAMESPACE)

#: The two Deployments that *are* OLM. Read back by :func:`app.admin.olm.status`
#: to answer "is it installed", and the reason that answer is not the same
#: question as "is it working".
OLM_DEPLOYMENTS = ("olm-operator", "catalog-operator")

#: The CSV OLM must reconcile before ``packages.operators.coreos.com`` exists.
#: Creating this object is not installing the package server; it is asking OLM
#: to. §33's ``installed`` never means this has succeeded.
PACKAGESERVER_CSV = "packageserver"

#: Plural names for the kinds in these two files. The console addresses every
#: write by ``group/version/plural``, and discovery cannot supply the plural for
#: a kind whose CRD does not exist yet — which is every ``operators.coreos.com``
#: kind at the moment phase two is planned. Hard-coded from the CRDs in
#: ``crds.yaml``, and pinned by a test that reads their ``spec.names.plural``
#: rather than trusting this table.
_PLURALS: dict[tuple[str, str], str] = {
    ("", "Namespace"): "namespaces",
    ("", "ServiceAccount"): "serviceaccounts",
    ("apps", "Deployment"): "deployments",
    ("networking.k8s.io", "NetworkPolicy"): "networkpolicies",
    ("rbac.authorization.k8s.io", "ClusterRole"): "clusterroles",
    ("rbac.authorization.k8s.io", "ClusterRoleBinding"): "clusterrolebindings",
    ("apiextensions.k8s.io", "CustomResourceDefinition"): "customresourcedefinitions",
    ("operators.coreos.com", "OLMConfig"): "olmconfigs",
    ("operators.coreos.com", "OperatorGroup"): "operatorgroups",
    ("operators.coreos.com", "CatalogSource"): "catalogsources",
    ("operators.coreos.com", "ClusterServiceVersion"): "clusterserviceversions",
}


@dataclass(frozen=True)
class OLMOptions:
    """The one choice an operator makes about this install.

    One field, and it is deliberately the only one. Everything else upstream's
    manifests decide — the namespaces, the images, the replica counts, the
    NetworkPolicies — is not ours to offer, because every value other than
    upstream's produces an OLM that does not work (see :data:`OLM_NAMESPACE`).
    A form with five fields that must all be left alone is worse than no form.
    """

    #: Also install upstream's ``operatorhubio-catalog`` CatalogSource. Off by
    #: default; see difference 2 in the module docstring. Turning it on is the
    #: decision to have the cluster pull and trust a community catalog at
    #: ``:latest``, hourly, and §33 makes it an acknowledged consequence rather
    #: than a checkbox.
    community_catalog: bool = False


@dataclass(frozen=True)
class BundleObject:
    """One object in the bundle, with everything a write needs to address it."""

    group: str
    version: str
    plural: str
    kind: str
    name: str
    namespace: str | None
    phase: str
    body: dict[str, Any]

    @property
    def where(self) -> str:
        """``namespace/name`` for a namespaced object, ``name`` otherwise."""
        return f"{self.namespace}/{self.name}" if self.namespace else self.name


def _read(filename: str) -> str:
    """One vendored manifest's text, checked against its pinned digest.

    Checked on **every** load rather than only in a test, and it raises rather
    than warning. A vendored manifest that has been edited is a manifest nobody
    reviewed against upstream, and the objects in these two files are a cluster's
    entire operator control plane and a ClusterRole granting ``*`` on ``*``.
    Refusing to install is the only safe answer to "these bytes are not the bytes
    that were vendored".
    """
    path = _MANIFEST_DIR / filename
    text = path.read_text(encoding="utf-8")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    expected = FILE_DIGESTS[filename]
    if actual != expected:
        raise RuntimeError(
            f"{path} does not match the digest pinned in app.admin.olm_bundle "
            f"(expected {expected}, got {actual}). These files are vendored "
            f"byte-for-byte from {UPSTREAM_RELEASE}/{filename} and are not "
            "edited by hand: re-download the release artifact, or update "
            "FILE_DIGESTS in the same commit that replaces the file."
        )
    return text


@functools.lru_cache(maxsize=1)
def _documents() -> tuple[tuple[str, dict[str, Any]], ...]:
    """Every vendored document, paired with its phase, parsed once per process.

    Cached because ``crds.yaml`` is 1.1 MiB of YAML and the plan, the dry run
    and the install each want the whole bundle. The cache holds the *parsed
    upstream* documents; :func:`build` deep-copies out of it before labelling,
    so no caller can mutate what the next one reads.
    """
    documents: list[tuple[str, dict[str, Any]]] = []
    for phase, filename in ((PHASE_CRDS, "crds.yaml"), (PHASE_CORE, "olm.yaml")):
        # The console's one reading (ADR-0009, ADR-0010), not a second loader
        # for the bundle: these documents are written into a cluster through the
        # same funnel as a pasted manifest, and `kubectl apply -f` on the same
        # bytes would read them this way too. The SHA-256 pin above is what
        # guards the bytes; this decides what they mean.
        for doc in yaml_dialect.load_all(_read(filename), fast=True):
            if doc:
                documents.append((phase, doc))
    return tuple(documents)


def _split_api_version(api_version: str) -> tuple[str, str]:
    """``apps/v1`` -> ``("apps", "v1")``; ``v1`` -> ``("", "v1")``."""
    if "/" in api_version:
        group, _, version = api_version.partition("/")
        return group, version
    return "", api_version


def _labelled(body: dict[str, Any]) -> dict[str, Any]:
    """A copy carrying the console's labels **on top of** upstream's.

    Order matters and is the opposite of the obvious one: upstream's labels are
    laid down first and ours are merged over them, so a collision resolves our
    way — but there are none, and a test asserts that. What must never happen is
    the reverse of this function: replacing ``metadata.labels`` wholesale would
    drop ``rbac.authorization.k8s.io/aggregate-to-edit`` from the two aggregated
    ClusterRoles, and the aggregation controller would then never fold OLM's
    verbs into the cluster's ``edit`` and ``view`` roles. Nothing about that
    failure is visible in the install: every object is created, and ordinary
    users simply cannot see Subscriptions.
    """
    obj = copy.deepcopy(body)
    metadata = obj.setdefault("metadata", {})
    metadata["labels"] = {**(metadata.get("labels") or {}), **LABELS}
    return obj


def build(options: OLMOptions | None = None) -> list[BundleObject]:
    """The whole bundle, in install order, phase one first.

    Pure: no cluster is read and nothing is written, which is what lets the plan
    be rendered, diffed and tested with no cluster at all — and what lets
    :mod:`app.admin.olm` hand each object to the funnel one at a time.

    Order is upstream's own, preserved exactly. It is not incidental: inside
    ``olm.yaml`` the ``olm-operators`` OperatorGroup precedes the
    ``packageserver`` CSV because OLM refuses to install a CSV into a namespace
    that has no OperatorGroup, and the ClusterRole precedes the
    ClusterRoleBinding that references it. Sorting this list for tidiness would
    break an install in a way no test that only counted objects would catch.
    """
    options = options or OLMOptions()
    objects: list[BundleObject] = []
    for phase, doc in _documents():
        kind = doc["kind"]
        name = doc["metadata"]["name"]
        if (
            kind == COMMUNITY_CATALOG_KIND
            and name == COMMUNITY_CATALOG_NAME
            and not options.community_catalog
        ):
            continue
        group, version = _split_api_version(doc["apiVersion"])
        objects.append(
            BundleObject(
                group=group,
                version=version,
                plural=_PLURALS[(group, kind)],
                kind=kind,
                name=name,
                namespace=doc["metadata"].get("namespace"),
                phase=phase,
                body=_labelled(doc),
            )
        )
    return objects


def crd_names() -> list[str]:
    """The eight CRDs phase one creates, by name.

    What :func:`app.admin.olm.install` waits on between the phases, and what
    :func:`app.admin.olm.status` counts as established. Derived from the bundle
    rather than listed again here, so the wait covers whatever ``crds.yaml``
    actually contains — including one added by a future release, which a
    hand-written list would silently not wait for.
    """
    return [item.name for item in build() if item.phase == PHASE_CRDS]


def validate_options(payload: dict[str, Any]) -> OLMOptions:
    """Build :class:`OLMOptions` from a request body.

    Nothing can be invalid here — there is one boolean — so this raises no
    ``Invalid``. It exists anyway, because §33's endpoints must not pass a raw
    request dict into :func:`build`: a body carrying ``{"namespace": "mine"}``
    should be ignored rather than silently accepted as configuration this
    bundle does not have.
    """
    return OLMOptions(community_catalog=bool(payload.get("communityCatalog")))


__all__ = [
    "COMMUNITY_CATALOG_IMAGE",
    "COMMUNITY_CATALOG_KIND",
    "COMMUNITY_CATALOG_NAME",
    "FILE_DIGESTS",
    "LABELS",
    "MANAGED_BY",
    "MANAGED_BY_LABEL",
    "NAMESPACES",
    "OLM_DEPLOYMENTS",
    "OLM_NAMESPACE",
    "OLM_VERSION",
    "OPERATORS_NAMESPACE",
    "PACKAGESERVER_CSV",
    "PART_OF_LABEL",
    "PHASES",
    "PHASE_CORE",
    "PHASE_CRDS",
    "UPSTREAM_RELEASE",
    "VERSION_LABEL",
    "BundleObject",
    "OLMOptions",
    "build",
    "crd_names",
    "validate_options",
]
