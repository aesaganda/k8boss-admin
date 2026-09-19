"""
Turning a discovered kubeconfig context into a registered cluster (§34).

:mod:`app.k8s.kubeconfig` reads; this writes. The split is not ceremony — the
reader is called by a listing endpoint that renders in a browser and must never
hold credential material, and this module is the only thing that ever sees a
decrypted client key on the way *in*. Keeping them in separate files is what
makes "the discovery response cannot contain a private key" a property of the
call graph rather than of somebody's care when adding a field.

Two callers, one writer: the ``POST /api/clusters/import`` endpoint and the
startup adoption below. They differ in ``origin`` and in nothing else, which is
deliberate — an adopted row and an imported row are the same kind of thing, and
the field that says which is the field an operator reads when they want to know
whether they registered it themselves.

**Why a console may adopt a cluster nobody asked it to.** Because the narrow
case it adopts in is one where the alternative is worse. A developer with a kind
cluster on the same machine, starting this console for the first time, currently
sees an empty cluster list and a form asking for an API server URL and a bearer
token they have to go and mint. Everything needed to fill that form in is
already on disk. So adoption runs when, and only when, all of the following
hold, and each one is a refusal as much as a condition:

* ``ADMIN_AUTO_DISCOVER_LOCAL`` is on (default) — one switch, off-able.
* **the registry is empty.** Adoption never adds to a fleet somebody curated. A
  console with one registered cluster is a console whose owner has made a
  decision about what it points at.
* the context was written by a local cluster tool *and* its API server is a
  loopback or private address. Both, never either — see
  :func:`app.k8s.kubeconfig.discover`.
* this console can plausibly reach it: a loopback address read from inside a
  container is skipped, because a registration that cannot connect is worse than
  no registration at all — it is the §14 failure with a URL instead of a
  controller.
* **exactly one** such context qualifies. Two local clusters is a choice, and a
  console making it at boot makes it where nobody can see it happen.

A failure here is logged and swallowed. Adoption is a convenience; a console
that refused to start because somebody's kubeconfig was malformed would have
turned the nicety into an outage.
"""

from __future__ import annotations

import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.crypto import encrypt
from app.errors import Conflict
from app.k8s import kubeconfig as kubeconfig_reader
from app.models import Cluster

logger = logging.getLogger(__name__)

#: ``Cluster.origin`` values written here. ``manual`` is written by §3's POST
#: and is the meaning of a NULL on a row that predates the column.
ORIGIN_IMPORTED = "kubeconfig"
ORIGIN_ADOPTED = "autodiscovered"


def register_context(
    db: Session,
    credentials: kubeconfig_reader.Credentials,
    *,
    name: str,
    origin: str,
    app_domain: str | None = None,
) -> Cluster:
    """Write one :class:`~app.k8s.kubeconfig.Credentials` into the registry, encrypted.

    ``impersonation_enabled`` is not a parameter and is never set here. ADR-0007's
    opt-in is per cluster and turned on deliberately, and a kubeconfig says
    nothing whatsoever about whether this console and that API server believe the
    same issuer.

    Raises:
        Conflict: a cluster of that name is already registered. Importing never
            overwrites: the stored credential may be the one somebody is relying
            on right now, and "import" is not a word anybody reads as "replace".
    """
    cluster = Cluster(
        name=name,
        platform="kubernetes",
        api_server=credentials.api_server,
        authentication_type=credentials.authentication_type,
        token_encrypted=encrypt(credentials.token) if credentials.token else None,
        client_certificate=credentials.client_certificate,
        client_key_encrypted=(
            encrypt(credentials.client_key) if credentials.client_key else None
        ),
        ca_certificate=credentials.ca_certificate,
        # Carried across rather than reset. A kubeconfig that says
        # `insecure-skip-tls-verify` describes a cluster whose certificate does
        # not verify; importing it with verification *on* produces a registration
        # that cannot connect, and importing it silently off would hide a
        # decision its owner already made. It is carried, and then surfaced in
        # every ClusterPublic response — which is where `skip_tls_verify` has
        # always been visible.
        skip_tls_verify=credentials.skip_tls_verify,
        app_domain=app_domain,
        origin=origin,
        # Never tested yet, which is a distinct state from "failed" — including
        # for an adopted cluster, which has had no connection attempted at all.
        status="unknown",
    )
    db.add(cluster)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise Conflict(
            f"A cluster named {name!r} is already registered.",
            hint=(
                "Rename it, or de-register the existing one first. Importing "
                "never overwrites a registration: the stored credential may be "
                "the one somebody is relying on right now."
            ),
            context={"resource": "clusters", "name": name},
        ) from e
    db.refresh(cluster)
    return cluster


def adopt_local_cluster() -> str | None:
    """Register the one local cluster on this machine, if the conditions hold.

    Returns the name of the cluster it registered, or ``None`` — which covers
    every refusal, each of which is logged at INFO with the reason. It is
    ``None`` far more often than not, and that is the intended shape: this runs
    on every boot of every deployment, and the overwhelming majority of them
    have a registry that is not empty.

    Never raises. See the module docstring.
    """
    from app.config import settings
    from app.database import SessionLocal

    if not settings.auto_discover_local_cluster:
        return None

    db = SessionLocal()
    try:
        if db.query(Cluster.id).first() is not None:
            # The common case, and the important refusal: a curated registry is
            # never added to. Logged at DEBUG rather than INFO because on a real
            # deployment this line would be in every restart's logs forever.
            logger.debug("Startup adoption skipped: clusters are already registered.")
            return None

        found = kubeconfig_reader.discover()
        if found.unavailable:
            # Not silent. This is the mode-600-kubeconfig-versus-uid-10001 case,
            # which until §34 produced no signal at all — the console started
            # fine, reported no clusters, and looked exactly like a machine with
            # no kubeconfig on it.
            logger.info(
                "Startup adoption found no usable kubeconfig: %s",
                "; ".join(entry["detail"] for entry in found.unavailable),
            )
            return None

        candidates = kubeconfig_reader.adoptable(found)
        if not candidates:
            logger.info(
                "Startup adoption registered nothing: none of the %d context(s) in "
                "%s is a local cluster this console can reach. Register a cluster "
                "with its API address and a ServiceAccount token, or import a "
                "context from Clusters -> Discovered on this machine.",
                len(found.candidates), found.path,
            )
            return None
        if len(candidates) > 1:
            logger.info(
                "Startup adoption registered nothing: %s each qualify, and picking "
                "between them at boot is a choice made where nobody can see it. "
                "Import the one you want from Clusters -> Discovered on this machine.",
                ", ".join(candidate.context for candidate in candidates),
            )
            return None

        chosen = candidates[0]
        credentials = kubeconfig_reader.credentials_for(chosen.context, found.path)
        cluster = register_context(
            db, credentials, name=chosen.context, origin=ORIGIN_ADOPTED,
        )
        logger.warning(
            "Adopted %s from %s as cluster id=%s — no cluster was registered and "
            "this is the only local %s cluster in that file. It has not been "
            "connected to yet; run the connection test. Set "
            "ADMIN_AUTO_DISCOVER_LOCAL=false to register nothing automatically.",
            chosen.context, found.path, cluster.id,
            chosen.distribution or "kubeconfig",
        )
        return cluster.name
    except Exception as e:  # noqa: BLE001 - a convenience must not fail a boot
        logger.warning(
            "Startup adoption failed and was skipped (%s: %s). The console is "
            "running; register a cluster from the Clusters page.",
            type(e).__name__, e,
        )
        return None
    finally:
        db.close()


__all__ = ["ORIGIN_ADOPTED", "ORIGIN_IMPORTED", "adopt_local_cluster", "register_context"]
