"""
Reading the kubeconfig on this machine — once, at import, and never afterwards.

``client.py`` says it in its first paragraph: clients are built from a
registered cluster's stored endpoint and encrypted credential, *never* from a
kubeconfig on the server. This module does not change that. It reads a
kubeconfig to answer one question — "what could this console be pointed at
without anybody typing anything?" — and hands the answer to §3's registration
path, which copies the credential into the encrypted registry. After an import
the kubeconfig is out of the picture entirely: editing it, moving it or deleting
it changes nothing about a registered cluster, and no request ever reads it
again. That is the boundary, and `docs/adr-0012-kubeconfig-onboarding.md` is
where it is argued.

Three decisions here are load-bearing.

**The YAML is parsed, never executed.** ``kubernetes.config.load_kube_config``
would be fewer lines, and it *runs* the ``exec`` credential plugin of whatever
context it loads — ``aws``, ``gke-gcloud-auth-plugin``, anything else the file
names. Executing an arbitrary binary as a side effect of *listing* what is
available is not a thing a console does, so this module reads the document with
:mod:`app.yaml_dialect` (the backend's one YAML, ADR-0009) and treats an
``exec`` stanza as a fact about a context rather than as an instruction.

**A context this console cannot import is listed with the reason, not
omitted.** §0.1 — empty is never blind. An operator whose only context is an EKS
``exec`` context must be shown that context and the sentence "this console will
not run a credential plugin", because the alternative is an empty discovery
panel that says the same thing as a missing kubeconfig and sends them to look at
the wrong system.

**"Local" is a decision about auto-adoption, not a cosmetic label.** Only a
local candidate is ever registered without somebody asking (see
``app.main``'s startup adoption), because a console that boots and quietly
registers the production context a developer happens to have in their kubeconfig
is a far worse failure than one that asks. Both halves of the test are
deliberately conservative: the distribution has to be one this file recognises —
by a name a local tool wrote, or by the fixed path a local tool writes its
kubeconfig to — *and* the API server has to be a loopback or private address.
"""

from __future__ import annotations

import base64
import binascii
import functools
import ipaddress
import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app import yaml_dialect

logger = logging.getLogger(__name__)


class KubeconfigError(Exception):
    """The kubeconfig could not be read or does not contain what was asked for.

    Carries ``reason`` from the §1.2 vocabulary so a caller can turn it into an
    ``unavailable`` entry without re-deriving which kind of failure it was.
    """

    def __init__(self, message: str, *, reason: str = "unreachable"):
        super().__init__(message)
        self.reason = reason


# --------------------------------------------------------------------------- #
# What counts as a local cluster
# --------------------------------------------------------------------------- #

#: Local-cluster tools, keyed by the prefix or exact name each one writes into
#: the kubeconfig itself. These are not guesses about what an operator might
#: have called something: `kind create cluster --name dev` writes the context
#: `kind-dev`, `k3d cluster create dev` writes `k3d-dev`, and the rest name
#: themselves exactly. A cluster called `kind-prod-eu` by hand is still only
#: adopted if its API server is also a loopback or private address — the two
#: tests below are `and`, never `or`.
LOCAL_DISTRIBUTIONS: tuple[tuple[str, str, bool], ...] = (
    # (token, display name, is a prefix rather than an exact match)
    ("kind-", "kind", True),
    ("k3d-", "k3d", True),
    ("minikube", "minikube", False),
    ("docker-desktop", "Docker Desktop", False),
    ("docker-for-desktop", "Docker Desktop", False),
    ("rancher-desktop", "Rancher Desktop", False),
    ("colima", "Colima", False),
    ("orbstack", "OrbStack", False),
    ("microk8s", "MicroK8s", False),
)

#: Local-cluster tools identified by the **path** their kubeconfig is read from,
#: because they write no name that identifies them. k3s names every entry in
#: `/etc/rancher/k3s/k3s.yaml` — context, cluster and user — ``default``, which
#: matches nothing above, so a bare `curl -sfL https://get.k3s.io | sh -` on the
#: console's own machine was classified remote and never adopted.
#:
#: **`default` is not in `LOCAL_DISTRIBUTIONS` and must never be.** It is a name
#: a remote cluster's context routinely carries, and adopting one on the strength
#: of it is precisely the failure the two-part `is_local` test exists to prevent.
#: The path is different in kind: a file at `/etc/rancher/k3s/k3s.yaml` was
#: written by k3s, by construction, and no cluster somewhere else can arrange to
#: be read from it.
#:
#: **The evidence does not survive the file being moved, and nothing weaker
#: replaces it.** A kubeconfig copied to `~/.kube/config`, or merged into one
#: with `KUBECONFIG=~/.kube/config:/etc/rancher/k3s/k3s.yaml kubectl config view
#: --flatten`, has no path left to read and a context called `default` — so it is
#: remote, is listed as remote, and is imported by hand from
#: Clusters -> Discovered. That is the honest answer: there is nothing safe left
#: to go on, and reaching for the name at that point would put back exactly the
#: heuristic the paragraph above refuses.
LOCAL_DISTRIBUTION_PATHS: dict[str, str] = {
    "/etc/rancher/k3s/k3s.yaml": "k3s",
    "/etc/rancher/rke2/rke2.yaml": "RKE2",
}


def classify(context_name: str, cluster_name: str, path: str | None = None) -> str | None:
    """The local tool that wrote this context, or ``None`` if none did.

    Both names are checked because the two tools this exists for spell it
    differently: kind writes the context ``kind-dev`` over a cluster also called
    ``kind-dev``, while k3d writes the context ``k3d-dev`` over a cluster called
    ``k3d-dev`` — but a kubeconfig merged with ``kubectl config rename-context``
    keeps only one of the two intact, and dropping a cluster out of discovery
    because somebody renamed its context is the kind of silent omission §0.1 is
    about.

    ``path`` is the kubeconfig the context was read from, and is the only
    evidence for the tools that name nothing after themselves — see
    :data:`LOCAL_DISTRIBUTION_PATHS`. It identifies the *file*, so it applies to
    every context in it rather than only to one called ``default``: a k3s context
    somebody renamed with ``kubectl config rename-context`` is still the file k3s
    wrote, and dropping it would be the same silent omission as above. The
    converse never holds — ``default`` read from any other path is no
    distribution at all, whatever its address.
    """
    for name in (context_name or "", cluster_name or ""):
        lowered = name.strip().lower()
        if not lowered:
            continue
        for token, display, is_prefix in LOCAL_DISTRIBUTIONS:
            if lowered.startswith(token) if is_prefix else lowered == token:
                return display
    if path:
        # Both sides through `realpath`, so a symlink at `~/.kube/config`
        # pointing into `/etc/rancher` still resolves to the file k3s wrote —
        # and so does the table's own key on a machine where `/etc` is itself a
        # symlink, which is every macOS one. A *copy* resolves to neither, and
        # cannot: a copy is a different file that happens to hold the same
        # bytes, which is the distinction this whole signal rests on.
        resolved = os.path.realpath(path)
        for known, display in LOCAL_DISTRIBUTION_PATHS.items():
            if resolved == os.path.realpath(known):
                return display
    return None


#: Context names that identify nothing in a cluster switcher.
#:
#: Only k3s's `default` is here, and only because §34 adopts k3s now: kind and
#: k3d name a context after the cluster, so what they write is already the right
#: label. This is not a list of names to avoid — it is the list of names that
#: carry no information, which is a much shorter one.
_GENERIC_CONTEXT_NAMES = frozenset({"default"})


def suggested_name(context: str, distribution: str | None) -> str:
    """What to call a cluster adopted or imported from this context.

    The context name, except where the context name says nothing. k3s calls its
    context ``default``, so recognising k3s made ``default`` a cluster name for
    the first time — uninformative on its own, and actively misleading the
    moment a second cluster is registered beside it, because "default" reads as
    *the default one* rather than as the name of a particular cluster. That is
    the cluster switcher, the audit trail's ``cluster_name``, and the sentence
    the startup log prints.

    Falls back to the distribution and never to something invented. A context
    this console could not classify keeps whatever the kubeconfig called it,
    however generic: renaming somebody's context on a guess is worse than a dull
    label, and ``POST /api/clusters/import`` takes an explicit ``name`` that
    always wins.
    """
    context = (context or "").strip()
    if distribution and context.lower() in _GENERIC_CONTEXT_NAMES:
        return distribution
    return context


def _host_is_local(api_server: str) -> bool:
    """Does this URL point at the machine the console is running on, or its LAN?

    Loopback and the RFC1918 / RFC4193 ranges, plus the two hostnames Docker
    Desktop and Podman publish for the host. Anything that does not parse as an
    address is treated as *not* local: a DNS name could resolve anywhere, and
    guessing wrong in this direction auto-registers somebody's production
    cluster.
    """
    host = (urlparse(api_server).hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal", "host.containers.internal",
                "kubernetes.docker.internal"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


@functools.cache
def running_in_container() -> bool:
    """Is this process inside a container?

    Used for one thing only: deciding whether a ``https://127.0.0.1:6443`` in
    somebody's kubeconfig is reachable from here. Inside a container that
    address is the container, so a registration built from it connects to
    nothing while looking perfectly well formed — the §14 failure with a URL
    instead of a controller.

    ``/.dockerenv`` covers Docker and Docker Desktop; the cgroup scan covers
    Podman and containerd, which write neither. Cached because it cannot change
    while the process runs and the discovery endpoint would otherwise stat a
    file per candidate.
    """
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return False
    return any(marker in content for marker in ("docker", "kubepods", "containerd", "libpod"))


# --------------------------------------------------------------------------- #
# What one context offers
# --------------------------------------------------------------------------- #

#: What a kubeconfig user stanza carries, and whether this console can store it.
#:
#: The three it refuses are refused for the same reason in different clothes:
#: none of them is a credential that can be *copied*. An ``exec`` stanza is an
#: instruction to run a binary, an ``auth-provider`` is an instruction to refresh
#: a token through a plugin, and a username/password pair authenticates against
#: a mechanism modern API servers do not serve. Storing any of them would
#: produce a registration that looks complete and cannot connect.
CREDENTIAL_CLIENT_CERTIFICATE = "client_certificate"
CREDENTIAL_TOKEN = "token"
CREDENTIAL_EXEC = "exec"
CREDENTIAL_AUTH_PROVIDER = "auth_provider"
CREDENTIAL_BASIC = "basic"
CREDENTIAL_NONE = "none"

_REFUSALS: dict[str, str] = {
    CREDENTIAL_EXEC: (
        "This context authenticates by running a credential plugin "
        "(an `exec` stanza). This console will not run a binary named by a file "
        "on its disk, and a plugin's output expires, so there is nothing here it "
        "could store. Register this cluster with its API address and a "
        "ServiceAccount token instead."
    ),
    CREDENTIAL_AUTH_PROVIDER: (
        "This context uses a legacy `auth-provider` plugin, which refreshes its "
        "own token and has no copyable credential. Register this cluster with "
        "its API address and a ServiceAccount token instead."
    ),
    CREDENTIAL_BASIC: (
        "This context authenticates with a username and password. This console "
        "presents a bearer token or a client certificate and nothing else, and "
        "current API servers do not serve basic authentication at all."
    ),
    CREDENTIAL_NONE: (
        "This context names no credential, so importing it would register an "
        "anonymous connection. An anonymous client against a permissive cluster "
        "half-works, which is the configuration mistake that gets found weeks "
        "later as an intermittent permissions mystery."
    ),
}


#: The ``authentication_type`` each importable credential is stored as. Kept
#: beside the refusals so adding a credential kind means deciding both at once.
_AUTH_TYPE_FOR = {
    CREDENTIAL_TOKEN: "kubeconfig_token",
    CREDENTIAL_CLIENT_CERTIFICATE: "client_certificate",
}


@dataclass(frozen=True)
class Candidate:
    """One kubeconfig context, and whether this console can adopt it.

    Every field is safe to return to a browser. There is no credential here —
    not the token, not the client key, not even the CA — only what *kind* of
    credential the context holds. The bytes are read again, from the file, by
    :func:`credentials_for` at the moment an import is confirmed.
    """

    context: str
    cluster: str
    api_server: str
    namespace: str | None
    distribution: str | None
    is_local: bool
    is_current: bool
    credential: str
    #: The ``authentication_type`` an import would store, or ``None`` when this
    #: context cannot be imported at all.
    authentication_type: str | None
    importable: bool
    #: Why not, when ``importable`` is false. Never a bare "unsupported": the
    #: operator reading it is deciding what to do next.
    reason: str | None
    #: A reachability problem this console can *see* but has not tested — today
    #: only the loopback-address-from-inside-a-container case. Never a reason to
    #: hide the candidate, and never merged into ``reason``: "we cannot import
    #: this" and "we can import this and it probably will not connect from here"
    #: send an operator to two different places.
    concern: str | None
    has_ca_certificate: bool
    skip_tls_verify: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "cluster": self.cluster,
            "api_server": self.api_server,
            "namespace": self.namespace,
            "distribution": self.distribution,
            "is_local": self.is_local,
            "is_current": self.is_current,
            "credential": self.credential,
            "authentication_type": self.authentication_type,
            "importable": self.importable,
            "reason": self.reason,
            "concern": self.concern,
            "has_ca_certificate": self.has_ca_certificate,
            "skip_tls_verify": self.skip_tls_verify,
        }


@dataclass(frozen=True)
class Credentials:
    """Everything §3 needs to register one context, credential material included.

    Built only by :func:`credentials_for`, only while an import is being
    confirmed, and never returned by an endpoint. It exists as a type so the
    import handler cannot accidentally pass a token where a client key goes.
    """

    context: str
    api_server: str
    authentication_type: str
    token: str | None
    client_certificate: str | None
    client_key: str | None
    ca_certificate: str | None
    skip_tls_verify: bool
    distribution: str | None
    is_local: bool


@dataclass(frozen=True)
class Discovery:
    """The whole answer: what was found, where, and what could not be read."""

    path: str
    current_context: str | None
    candidates: tuple[Candidate, ...]
    #: §1.2 ``unavailable[]`` entries. A kubeconfig that is absent, unreadable or
    #: malformed lands here rather than raising, because "no kubeconfig on this
    #: machine" is an ordinary state for a console whose clusters are registered
    #: by hand — and an unreadable one is the Compose failure README warns
    #: about, which until now produced no signal at all.
    unavailable: tuple[dict[str, Any], ...]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def default_path() -> str:
    """The kubeconfig this console would read, with ``~`` expanded.

    ``KUBECONFIG`` wins over the configured path the way it does for ``kubectl``,
    and only its first entry is read: this console imports one context at a time
    and merging a path list would mean re-implementing kubectl's precedence
    rules, which is a second reading of a file format — the thing ADR-0009 spent
    a document refusing to have.
    """
    from app.config import settings

    env = os.environ.get("KUBECONFIG", "").strip()
    if env:
        first = env.split(os.pathsep)[0].strip()
        if first:
            return os.path.expanduser(first)
    return os.path.expanduser(settings.kubeconfig_path)


def _as_uid() -> str:
    """`` (running as uid N)``, where the platform reports one.

    Part of the sentence rather than a separate field because the operator
    reading it is looking at a mode-600 kubeconfig and a container running as
    uid 10001, and the number is the whole diagnosis.
    """
    getuid = getattr(os, "getuid", None)
    return f" (running as uid {getuid()})" if getuid else ""


def _load_document(path: str) -> dict[str, Any]:
    """The kubeconfig as a mapping, or a :class:`KubeconfigError` saying why not.

    The three failures are kept apart because they send an operator to three
    different places: no file at all (register a cluster by hand, or point
    ``KUBECONFIG_PATH`` somewhere), no permission to read it (the mode-600 file
    versus the uid-10001 container, which is the single most common way this
    feature does nothing on a Compose deployment), and a file that is not a
    kubeconfig.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError as e:
        raise KubeconfigError(
            f"No kubeconfig at {path}.", reason="not_found",
        ) from e
    except PermissionError as e:
        raise KubeconfigError(
            f"{path} exists but this process cannot read it{_as_uid()}.",
            reason="forbidden",
        ) from e
    except OSError as e:
        raise KubeconfigError(f"{path} could not be read: {e}.") from e

    try:
        documents = [doc for doc in yaml_dialect.load_all(text) if doc is not None]
    except Exception as e:  # noqa: BLE001 - every parse failure is one answer
        raise KubeconfigError(
            f"{path} is not valid YAML ({type(e).__name__}).", reason="unsupported",
        ) from e

    if not documents or not isinstance(documents[0], dict):
        raise KubeconfigError(
            f"{path} does not contain a kubeconfig document.", reason="unsupported",
        )
    return documents[0]


#: The key each kubeconfig list entry keeps its body under. Spelled out rather
#: than derived by trimming an "s": `contexts` does not follow the rule, and a
#: derivation with one exception in it is a derivation somebody extends wrongly.
_SINGULAR = {"clusters": "cluster", "users": "user", "contexts": "context"}


def _index(document: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    """``clusters``/``users``/``contexts`` as a name → body mapping.

    Names are forced through ``str`` because ADR-0010 resolves ``y``, ``n`` and
    their capitals as booleans, and this console has exactly one YAML reading
    (ADR-0009) — so a context genuinely called ``n`` arrives here as ``False``.
    That is a two-character context name nobody has, and silently dropping it
    would still be a silent drop.
    """
    result: dict[str, dict[str, Any]] = {}
    for entry in document.get(key) or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if name is None:
            continue
        body = entry.get(_SINGULAR[key])
        result[str(name)] = body if isinstance(body, dict) else {}
    return result


def _credential_kind(user: dict[str, Any]) -> str:
    """What the user stanza actually holds. Order matters.

    ``exec`` and ``auth-provider`` are checked first because a context can carry
    a stale ``token`` *beside* them — kubectl ignores it and refreshes through
    the plugin, and a console that stored the stale one would register a
    credential that authenticated yesterday.
    """
    if isinstance(user.get("exec"), dict):
        return CREDENTIAL_EXEC
    if isinstance(user.get("auth-provider"), dict):
        return CREDENTIAL_AUTH_PROVIDER
    if user.get("token") or user.get("tokenFile"):
        return CREDENTIAL_TOKEN
    if user.get("client-certificate-data") or user.get("client-certificate"):
        return CREDENTIAL_CLIENT_CERTIFICATE
    if user.get("username") or user.get("password"):
        return CREDENTIAL_BASIC
    return CREDENTIAL_NONE


def _reachability_concern(api_server: str) -> str | None:
    """The one reachability problem visible without connecting.

    A kind or k3d kubeconfig names ``https://127.0.0.1:<port>``, which is the
    host's loopback. Read from inside the backend container that address is the
    *container's* own loopback, so the registration is created, looks correct,
    and reaches nothing. Saying so here — beside the candidate, before the
    import — is the difference between a two-line explanation and an afternoon
    with ``curl``.

    Stated as a concern rather than as a refusal because it is not always true:
    a Compose file with ``network_mode: host``, or a console run straight on the
    host with ``uvicorn``, reaches it fine, and this console has no way to know
    which of those it is in. The connection test is what settles it.
    """
    if not running_in_container():
        return None
    host = (urlparse(api_server).hostname or "").strip().lower()
    if host == "localhost":
        here = True
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        # `is_unspecified` catches `0.0.0.0`, which some k3d versions write and
        # which means "this host" exactly as `127.0.0.1` does — so it fails in
        # exactly the same way and must not be the one that slips through.
        here = address.is_loopback or address.is_unspecified
    if not here:
        return None
    return (
        f"{api_server} is a loopback address and this console is running in a "
        "container, where that address is the container itself rather than the "
        "machine your cluster is on. Run the console directly on the host "
        "(`make dev`) to adopt this cluster, or give it an address reachable "
        "from inside the container. The connection test is what settles it."
    )


def discover(path: str | None = None) -> Discovery:
    """Every context in the kubeconfig, each with what this console could do with it.

    Never raises for a missing, unreadable or malformed file: that is one of the
    ordinary states of a console whose clusters are registered by hand, and it
    is reported as an ``unavailable`` entry so the page can say *which* of the
    three it is. It raises only for a bug in this module.
    """
    resolved = path or default_path()
    try:
        document = _load_document(resolved)
    except KubeconfigError as e:
        logger.info("Kubeconfig discovery found nothing usable: %s", e)
        return Discovery(
            path=resolved,
            current_context=None,
            candidates=(),
            unavailable=(
                {
                    "group": "",
                    "resource": "kubeconfig",
                    "namespace": None,
                    "reason": e.reason,
                    "detail": str(e),
                },
            ),
        )

    clusters = _index(document, "clusters")
    users = _index(document, "users")
    contexts = _index(document, "contexts")
    current = document.get("current-context")
    current = str(current) if current is not None else None

    candidates: list[Candidate] = []
    for name in sorted(contexts):
        body = contexts[name]
        cluster_name = str(body.get("cluster") or "")
        cluster = clusters.get(cluster_name, {})
        user = users.get(str(body.get("user") or ""), {})

        api_server = str(cluster.get("server") or "").strip()
        credential = _credential_kind(user)
        distribution = classify(name, cluster_name, resolved)
        importable = credential in (CREDENTIAL_TOKEN, CREDENTIAL_CLIENT_CERTIFICATE)

        reason: str | None = None
        if not api_server:
            importable, reason = False, (
                f"The kubeconfig names no API server for cluster {cluster_name!r}, "
                "so there is nothing to connect to."
            )
        elif not importable:
            reason = _REFUSALS[credential]

        candidates.append(
            Candidate(
                context=name,
                cluster=cluster_name,
                api_server=api_server,
                namespace=str(body["namespace"]) if body.get("namespace") else None,
                distribution=distribution,
                # Both halves, never either half. See the module docstring.
                is_local=bool(distribution) and _host_is_local(api_server),
                is_current=name == current,
                credential=credential,
                authentication_type=(
                    _AUTH_TYPE_FOR[credential] if importable else None
                ),
                importable=importable,
                reason=reason,
                concern=_reachability_concern(api_server) if api_server else None,
                has_ca_certificate=bool(
                    cluster.get("certificate-authority-data")
                    or cluster.get("certificate-authority")
                ),
                skip_tls_verify=bool(cluster.get("insecure-skip-tls-verify")),
            )
        )

    return Discovery(
        path=resolved,
        current_context=current,
        candidates=tuple(candidates),
        unavailable=(),
    )


def adoptable(discovery: Discovery) -> list[Candidate]:
    """The candidates startup adoption may register, best first.

    "Best" is the kubeconfig's own ``current-context`` if it qualifies, then
    everything else in discovery order. The caller decides what to do with a
    list of more than one; this function deliberately does not pick, because
    picking between two local clusters on behalf of somebody who did not ask is
    the same class of guess as :func:`_infer_default_cluster_id`'s, made at boot
    where nobody sees it.
    """
    usable = [c for c in discovery.candidates if c.importable and c.is_local and not c.concern]
    usable.sort(key=lambda c: (not c.is_current, c.context))
    return usable


# --------------------------------------------------------------------------- #
# Materialising one context
# --------------------------------------------------------------------------- #

def _decode(value: Any, *, what: str) -> str:
    """One ``*-data`` field, base64-decoded.

    Raises rather than returning the raw text: a client key that failed to
    decode and was stored anyway produces a registration that cannot connect and
    a TLS error that says nothing about base64.
    """
    try:
        # Whitespace stripped before validating: `validate=True` refuses any
        # character outside the alphabet, and a kubeconfig whose long `*-data`
        # scalar was folded across lines arrives here with spaces in it. Keeping
        # the strict validation is the point — it is what turns a truncated key
        # into a refusal at import rather than a TLS error days later.
        compact = "".join(str(value).split())
        return base64.b64decode(compact, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as e:
        raise KubeconfigError(
            f"The {what} in this context is not valid base64-encoded PEM.",
            reason="unsupported",
        ) from e


def _material(
    holder: dict[str, Any],
    *,
    file_key: str,
    base_dir: str,
    what: str,
    data_key: str | None = None,
) -> str | None:
    """PEM from either the inline ``*-data`` field or the file it points at.

    Relative paths resolve against the kubeconfig's own directory, which is what
    ``kubectl`` does; resolving them against the process's working directory
    instead would make discovery's answer depend on where somebody started
    uvicorn from.
    """
    inline = holder.get(data_key) if data_key else None
    if inline:
        return _decode(inline, what=what)
    reference = holder.get(file_key)
    if not reference:
        return None
    candidate = os.path.expanduser(str(reference))
    if not os.path.isabs(candidate):
        candidate = os.path.join(base_dir, candidate)
    try:
        with open(candidate, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as e:
        raise KubeconfigError(
            f"This context's {what} lives at {candidate}, which this process "
            f"could not read ({type(e).__name__}).",
            reason="forbidden" if isinstance(e, PermissionError) else "not_found",
        ) from e


def credentials_for(context: str, path: str | None = None) -> Credentials:
    """Read one context's credential material, for an import that is being confirmed.

    Separate from :func:`discover` on purpose. Discovery is a listing an
    operator may load repeatedly and its result is rendered in a browser, so it
    must never hold a private key; this reads the key and is called once, by the
    handler that is about to encrypt it. Keeping them apart is what makes "no
    credential is in the discovery response" a property of the type rather than
    of somebody's care when adding a field.

    Raises:
        KubeconfigError: the file, the context, or the credential cannot be
            read — each with the sentence that says which.
    """
    resolved = path or default_path()
    document = _load_document(resolved)
    base_dir = os.path.dirname(os.path.abspath(resolved))

    contexts = _index(document, "contexts")
    if context not in contexts:
        raise KubeconfigError(
            f"{resolved} has no context named {context!r}.", reason="not_found",
        )
    body = contexts[context]
    cluster_name = str(body.get("cluster") or "")
    cluster = _index(document, "clusters").get(cluster_name, {})
    user = _index(document, "users").get(str(body.get("user") or ""), {})

    api_server = str(cluster.get("server") or "").strip()
    if not api_server:
        raise KubeconfigError(
            f"Context {context!r} names no API server.", reason="unsupported",
        )

    credential = _credential_kind(user)
    if credential not in _AUTH_TYPE_FOR:
        # The same sentence the listing showed. Re-checked here because the
        # listing is a separate request: a kubeconfig edited between the two
        # would otherwise be imported on the strength of what it used to say.
        raise KubeconfigError(_REFUSALS[credential], reason="unsupported")

    token: str | None = None
    client_certificate: str | None = None
    client_key: str | None = None

    if credential == CREDENTIAL_TOKEN:
        token = user.get("token") or _material(
            user, file_key="tokenFile", base_dir=base_dir, what="token file",
        )
        token = str(token).strip() if token else None
        if not token:
            raise KubeconfigError(
                f"Context {context!r} names a token that is empty.",
                reason="unsupported",
            )
    else:
        client_certificate = _material(
            user, data_key="client-certificate-data", file_key="client-certificate",
            base_dir=base_dir, what="client certificate",
        )
        client_key = _material(
            user, data_key="client-key-data", file_key="client-key",
            base_dir=base_dir, what="client key",
        )
        if not client_certificate or not client_key:
            # A certificate without its key is the half-configured case that
            # would otherwise be stored and fail at the TLS handshake with a
            # message about the cluster.
            raise KubeconfigError(
                f"Context {context!r} has a client certificate but no readable "
                "client key, so it cannot authenticate.",
                reason="unsupported",
            )

    ca_certificate = _material(
        cluster, data_key="certificate-authority-data",
        file_key="certificate-authority", base_dir=base_dir,
        what="certificate authority",
    )

    # Classified once, from the same path the document was read from, so an
    # imported row records the same distribution the listing showed it under.
    distribution = classify(context, cluster_name, resolved)

    return Credentials(
        context=context,
        api_server=api_server,
        authentication_type=_AUTH_TYPE_FOR[credential],
        token=token,
        client_certificate=client_certificate,
        client_key=client_key,
        ca_certificate=ca_certificate,
        skip_tls_verify=bool(cluster.get("insecure-skip-tls-verify")),
        distribution=distribution,
        is_local=bool(distribution) and _host_is_local(api_server),
    )


__all__ = [
    "LOCAL_DISTRIBUTIONS",
    "LOCAL_DISTRIBUTION_PATHS",
    "Candidate",
    "Credentials",
    "Discovery",
    "KubeconfigError",
    "adoptable",
    "classify",
    "credentials_for",
    "suggested_name",
    "default_path",
    "discover",
    "running_in_container",
]
