# ADR 0008 — k8boss-admin ships Operator Lifecycle Manager and can install it

**Status:** accepted
**Date:** 2026-09-08
**Amends:** ADR-0004's boundary — "one pinned bundle of eight objects, through
the ordinary write funnel, and nothing else, ever." There are now two. This ADR
is the re-reading that ADR-0004 and ADR-0005 both said had to happen before that
sentence changed, rather than the reinterpretation they both warned against.

**Decided with the argument against it intact.** ADR-0005 closed with: *the day
somebody argues that a second shipped bundle is "really just like the portal" is
the day both boundaries need re-reading rather than reinterpreting.* This is that
day, and the argument is not that. §33 is a second shipped bundle in the full
sense ADR-0004 meant — vendored manifests, a pinned version, a maintenance clock,
a data plane this project chose. Nothing below claims otherwise. The product
owner read the costs in §"What this costs" and chose to ship it anyway, on the
same terms ADR-0004 was decided on.

## The decision

k8boss-admin vendors the Operator Lifecycle Manager release manifests, pinned to
one version, and can install them onto a cluster that does not run OLM. Twenty-six
objects in two ordered phases, each an ordinary write through
`app.admin.mutate.mutate()`. Off by default behind `ADMIN_OLM_INSTALL_ENABLED`,
which is separate from `ADMIN_PORTAL_INSTALL_ENABLED`.

It ships **manifests, not a controller.** No reconcile loop, no desired state in
the console's database, no watch, no drift correction. `GET /api/portal/olm` is a
live read like every other page. What keeps OLM running is the Kubernetes control
plane; what installs operators is OLM.

There is one exception to "no loops" and it is worth naming precisely, because it
is the first of its kind in this codebase: between the two phases the install
**waits** for the eight CustomResourceDefinitions to report `Established`, bounded
by `ADMIN_OLM_ESTABLISH_TIMEOUT_SECONDS` and then abandoned. That is one bounded
wait inside one request — what `kubectl wait` does — and not a control loop: it
holds no state, survives nothing and corrects nothing.

## Why

The operator portal's answer on a cluster with no OLM is a calm, correct, and
completely useless page: *none of the APIs this page reads are served here, so
there is no catalog to install from and nothing to list.* Every word of that is
true. §16 is not lying and needs no rescuing — which makes this a materially
weaker case than ADR-0004's, and that is stated here rather than buried.

ADR-0004's router existed because the console was **writing objects that did
nothing**: an Ingress `POST`ed to a cluster with no controller returns 201 and
routes nothing, so the console was reporting success for something that did not
happen. That defect does not exist here. §16 writes nothing on an OLM-less
cluster; it refuses, and it says why.

What is left is the weaker but real argument: a console that can browse every
resource a cluster serves, and that already knows exactly why this page is empty,
answering "then install it" with a link to somebody else's documentation. The
operator's next step is two `kubectl apply` commands with a wait between them,
run outside the console, which means outside the audit trail — the same sentence
ADR-0005 used to justify the portal, one layer down.

**Being honest about the size of that argument:** it is a convenience argument,
not a correctness one. Nobody is misled today. If the maintenance cost below is
later judged too high, this is the ADR to cut, and cutting it takes nothing else
with it.

## Why the manifests are vendored rather than fetched

ADR-0004 rejected fetching at install time and the reasoning transfers unchanged:
an install that reaches the internet does something different on the day upstream
changes a default, on the day the cluster is air-gapped, and on the day somebody
takes over the repository. It is stronger here, not weaker. This console holds
cluster credentials; giving it "download YAML and apply it" is a supply-chain
hole in the component with the most to lose from one.

So `deploy/olm/crds.yaml` and `deploy/olm/olm.yaml` are checked in **byte for
byte** from upstream's release, their SHA-256 digests are pinned in
`app/admin/olm_bundle.py`, and `_read()` refuses to load a file whose digest does
not match — at run time, not only in a test. That is what makes "byte for byte
upstream" a fact anyone can verify rather than a claim in a docstring.

It costs 1.4 MiB of vendored YAML in this repository, about 1 MiB of which is the
ClusterServiceVersion CRD's OpenAPI schema. That is a real and permanent cost and
it is the honest price of the property above.

## What this costs, stated rather than argued away

**ADR-0004's boundary is now false as written, and the count is the thing to
watch.** Precisely which claims survive:

| Claim | After this |
|---|---|
| "holds no cluster state" | **True.** Nothing is stored. "Is OLM installed" is read off the cluster's own objects every time. |
| "every page is a live read" | **True.** `GET /api/portal/olm` reads the cluster on every call. |
| "not a GitOps controller" | **True.** No reconcile loop, no drift correction, no watch. The one bounded wait is inside a single request. |
| "not a deployment engine" | **False, and now twice.** Two pinned bundles through the ordinary write funnel. ADR-0004 said one, forever; this ADR is why that changed and what the new count is. |

**The grant is the largest this product has ever created, and it is larger than
ADR-0004's by a wide margin.** ADR-0004 called cluster-wide `get,list,watch` on
Secrets "the largest RBAC grant this product has ever asked for". OLM's
`system:controller:operator-lifecycle-manager` is `apiGroups: ['*']`,
`resources: ['*']`, with `watch, list, get, create, update, patch, delete,
deletecollection, escalate, bind`, plus every verb on all non-resource URLs. That
is cluster-admin plus the ability to grant cluster-admin, and `escalate` means it
can grant permissions nobody gave it.

It is inherent to OLM rather than chosen here — OLM installs operators that ask
for arbitrary permissions, so it has to hold them — but "upstream requires it" is
not a mitigation, it is an explanation. What mitigates it is narrow and real: the
object is in the diff before it is created, the consequence quotes the rule
verbatim rather than paraphrasing it as "broad permissions", the feature has its
own gate that is off by default, and the plan is readable with the gate off so
the decision to open it is made with the object on screen.

**Preflight cannot see escalation prevention, and here that is the expected case
rather than an edge one.** ADR-0004 documented this: a `SelfSubjectAccessReview`
on `create clusterroles` answers yes, and the API server then refuses at
admission because the caller does not hold what it is trying to grant. For the
router that fires when the console lacks cluster-wide Secret reads. For OLM's
ClusterRole it fires unless the console's identity holds **everything**, so on any
deployment that has narrowed `deploy/rbac.yaml` it fires always. §0.2's promise —
a denial that names the missing permission — cannot be kept from the review here,
and what the code does instead is the same narrow thing §14 does: the status
mapping stands, and **only the hint** is rewritten to name escalation prevention
and the `escalate`/`bind` grants.

**A pinned third-party bundle is a recurring obligation with a clock on it, and
this is the second one.** ADR-0004 called that "the strongest argument against the
whole decision" for eight small objects. This bundle is 1.4 MiB and its failure
mode is worse: a stale router is a stale proxy, and a stale OLM is a stale
operator control plane holding cluster-admin. The mitigations reduce the cost and
do not remove it — the digests make a bump a reviewable diff over real upstream
artifacts, `versionMatches` reports drift from what this console ships, and the
console explicitly does **not** claim to upgrade OLM.

**`installed` and `working` are two different questions, and the second one this
console cannot answer at write time.** Twenty-six accepted objects is not a
running OLM. The Deployments have to schedule, and the `packageserver`
ClusterServiceVersion has to be reconciled by OLM itself before
`packages.operators.coreos.com` exists. Until then the portal reads the same
empty state it read before the install — **and it is right to**. So the install
response reports `installed` for "every object was accepted", sets `ready` to
`null` with the sentence saying where it is answered, and `GET /api/portal/olm`
answers it as a live read. Any other shape here would be this project's defect
standard aimed at the thing the console had just done.

**This installs OLM v0, which is the version with a direction.** ADR-0005 already
recorded that §16 speaks `operators.coreos.com` only, and that OLM v1
(`olm.operatorframework.io` `ClusterExtension`) is not read at all. §33 installs
v0, which means it makes that gap *deeper* on any cluster where it is used: the
console now puts the v0 control plane on clusters that had none. That is the
correct pairing with §16 as it exists today and it is the wrong long-term
direction, and both are true at once.

## What was rejected

**Fetching upstream's release at install time, digest-verified.** The smaller
repository and the easy version bump are real. Rejected on ADR-0004's reasoning,
strengthened: air-gapped clusters are precisely the population with no OLM and no
easy way to get it, and outbound network in a component holding cluster
credentials is a bigger hole than 1.4 MiB of vendored YAML is a burden.

**Installing upstream's `operatorhubio-catalog` CatalogSource by default.**
Upstream's `olm.yaml` ends with one, pulling `quay.io/operatorhubio/catalog:latest`
and re-polling hourly. Installing it by default would make this console the thing
that decided a cluster trusts operatorhub.io, and would do it at an unpinned tag —
ADR-0004's first deliberate difference from its own upstream, violated. It is
opt-in, by an acknowledged consequence naming the image. An OLM with no
CatalogSource is a working OLM with an empty catalog, and the portal renders that
honestly: zero packages because the cluster subscribes to no catalog, not because
a read failed.

Note the boundary this preserves. ADR-0005's single largest rejection was
*shipping or curating a catalog of our own*. §33 still ships none — it ships the
software that reads catalogs, and leaves choosing them to the operator.

**Uninstalling OLM.** Removing OLM means deleting its CRDs, and deleting a CRD
deletes every custom resource made from it across every namespace with no second
confirmation — every operator's Subscription and ClusterServiceVersion on the
cluster. There is no delete verb granted for it in `deploy/rbac.yaml` and no
endpoint. This is the same asymmetry ADR-0005 admitted for the portal, one layer
down and larger, and it is not closed: it is measurably easier to install a
cluster's operator control plane here than to remove one.

**Upgrading OLM.** A later OLM is installed with `kubectl` from upstream's
release, the same way this one could have been. The console reports which version
is running and whether it matches the one shipped here, and stops. An in-console
upgrade of a component holding cluster-admin, across a CRD schema migration, is a
larger feature than this ADR is deciding.

**Adopting objects that already carry the bundle's names.** The console refuses
and names every conflicting object rather than the first. A cluster already
running OLM collides on most of the twenty-six at once, and writing 0.35.0's
Deployments over a running 0.30 would restart the cluster's entire operator
control plane and every operator it manages. This is ADR-0004's rule, made
stricter because the blast radius is larger.

**Rolling back a partial install.** Deleting what succeeded is more writes the
operator did not approve, against objects that may already be in use — and here
the objects are CRDs, whose deletion is not recoverable. The response reports
per-object outcomes and `installed: false`.

**Applying both files in one pass.** `kubectl apply -f olm.yaml` on a fresh
cluster fails with `no matches for kind "OLMConfig"`, because eleven of the
nineteen objects are instances of the CRDs in the other file. Upstream's own
installer applies, waits and then applies; so does this.

## The boundary

For whoever reads this next and is deciding whether the next thing belongs here:

> k8boss-admin installs **two pinned bundles — §14's router and §33's OLM — through
> the ordinary write funnel, and nothing else.** No third bundle, no operator of
> its own, no chart rendering, no reconcile loop, no drift correction, no
> uninstall, no upgrade of somebody else's control plane, and nothing that
> reaches a registry at install time.
>
> ADR-0004 said one, forever, and one became two. That is the fact to weigh
> against the next proposal, and the honest thing to say about it is that the
> sentence "and nothing else, ever" has now been wrong once. A third bundle is
> not a third instance of a pattern; it is the point at which the boundary
> stops being one.

If §33 is ever cut, nothing else moves. `admin/olm_bundle.py`, `admin/olm.py`,
`deploy/olm/` and the three endpoints in `api/portal.py` are self-contained; the
only shared code is the funnel and the envelope every other section already uses;
§16 does not import any of it and works exactly as before on every cluster that
already runs OLM. `ADMIN_OLM_INSTALL_ENABLED` defaulting to false means the write
does not exist on a deployment that never opted in. That removability is both the
strongest structural argument for trying it and the honest escape hatch if the
maintenance cost is later judged too high.

## See also

- `docs/api-contract.md` §33 — normative: the three endpoints, the two phases,
  the tri-states, the consequence codes, and what `installed: true` does and does
  not mean.
- `docs/safety-model.md` §24 — the acknowledgement handshake, the two gates, the
  adoption refusal, and why the establishment wait is not a control loop.
- `docs/adr-0004-shipped-router.md` — the boundary this ADR amends, and the
  escalation-prevention case that is the nearest precedent.
- `docs/adr-0005-operator-portal.md` — why creating a Subscription was *not* a
  second bundle, which is the argument §33 deliberately does not make for itself.
- `docs/rbac.md` — the two write rules §33 adds, and what deleting them degrades.
- `deploy/olm/` — the vendored release artifacts, applicable by hand with
  `kubectl apply -f`, and their provenance.
