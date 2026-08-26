# ADR 0005 — k8boss-admin browses OLM catalogs and can create a Subscription

**Status:** accepted
**Date:** 2026-08-26
**Supersedes nothing. Constrained by:** ADR-0004's boundary, which this ADR
argues it does not cross — and which is the first thing to check if you disagree.

**The uncomfortable half is not incidental to this decision.** A console that can
subscribe to a catalog operator is a console through which third-party software
enters somebody's cluster, under RBAC that Operator Lifecycle Manager grants and
this console can neither see nor report. That is stated in
§"What this costs" below and it is not mitigated away anywhere in this document,
because it cannot be. Whoever revisits this needs the cost as clearly as the
reason.

## The decision

k8boss-admin reads the Operator Lifecycle Manager catalogs a cluster already
runs — PackageManifests, CatalogSources, Subscriptions, ClusterServiceVersions,
InstallPlans, OperatorGroups — and can create **one** object: an OLM
`Subscription`. The write is off by default behind
`ADMIN_PORTAL_INSTALL_ENABLED`, on top of `ADMIN_ALLOW_MUTATIONS`.

Before that write it renders the exact object, checks whether the target
namespace is one OLM can actually install into, and refuses unless the caller
acknowledges every consequence the check found, by name. Everything else in §16
is a live read. `docs/api-contract.md` §16 is the normative shape.

## Why this is not a second shipped bundle

ADR-0004 drew a boundary and meant it: k8boss-admin installs **one pinned bundle
of eight objects, through the ordinary write funnel, and nothing else, ever.**
The obvious reading of §16 is that the list just grew to two. It did not, and the
difference is not a technicality.

| ADR-0004's router | §16's portal |
|---|---|
| Ships manifests, checked into this repo | Ships **nothing**. Every package comes from a `CatalogSource` the cluster already runs |
| Pins an image and recommends a version | Pins nothing, names no image, ranks nothing |
| Owes a version-bump obligation with a clock on it | Owes none. There is no version of anything here to go stale |
| Eight writes, orchestrated, with a partial-install outcome | **One write**, one object, one diff, one audit row |
| Installs a data plane this project chose | Writes a request into an API the cluster already serves |

What §16 writes is nine lines of YAML into `operators.coreos.com/v1alpha1` —
which §4's YAML editor could already write, today, with no new code and no new
gate. The console added discovery in front of it (so the package, the channel and
the catalog come off the cluster rather than out of somebody's memory) and a
pre-write check behind it (so a Subscription that will install nothing says so
before it is created). Neither of those is a deployment engine. **The deployment
engine in this story is OLM, and OLM is the cluster's.**

Applied to the README's four claims, §16 changes none of them. It holds no
cluster state, every page is a live read, there is no reconcile loop, and the one
narrow "installs software" exception stays exactly as narrow as ADR-0004 left
it — one bundle, still.

That is the argument. Its weakness is worth naming: "we only write one object,
what happens next is somebody else's software" is also a sentence that could
justify a great deal, and the thing that stops it being a licence is the boundary
restated at the end of this document rather than the reasoning above it.

## Why at all

The question the portal answers is *what can I install on this cluster, and what
is already installed* — and for a console whose §4 can already browse every
resource a cluster serves, refusing to answer it is a strange gap rather than a
principled position. An operator who wants Prometheus on a cluster running OLM
today reads a catalog in a different tool, writes a Subscription by hand, and
finds out a day later that the namespace had no OperatorGroup and nothing
installed. Two of those three steps happen outside the console, which means
outside the audit trail.

And the thing §16 is actually good at is not the write. It is the three checks
before it. OLM installs an operator only into a namespace governed by exactly one
OperatorGroup whose scope the operator supports, and a `Manual` approval strategy
holds the install until somebody approves an InstallPlan. All three are knowable
before the write and none of them is visible in `kubectl apply -f
subscription.yaml`, which returns `subscription.operators.coreos.com/prometheus
created` and tells you nothing. A Subscription written into a namespace with no
OperatorGroup is an object that installs nothing while looking created — §14's
failure with a namespace in place of a controller, and this project's defect
standard aimed at somebody else's deployment engine.

## What this costs, stated rather than argued away

**The console becomes the place third-party software enters somebody's cluster,
and §0.2's promise does not extend past the write.** This console preflights
`create operators.coreos.com/subscriptions` on that exact namespace and name.
That is the only permission it can speak about. What installs the operator is
OLM, with OLM's permissions, and what OLM grants the operator is whatever the
bundle asks for — its own ServiceAccount, Roles, and routinely ClusterRoles over
resources this console never named. **That grant is made by OLM, not by the
caller**, so no `SelfSubjectAccessReview` previews it: a review answers about the
caller's own verbs, and the caller does not perform this grant.

§0.2 promises a denial that names the missing permission. For what happens after
this write, the console has nothing to promise. Note the asymmetry with
ADR-0004's escalation-prevention case, which is the closest thing in this repo
and is the *easier* one: there, preflight said yes and the API server then said
no, so the operator at least learns the truth from a refusal. Here the API server
says yes, OLM proceeds, and the grant lands. The most that can honestly be shown
first is what the plan already shows — the custom resources the operator declares
it will own — and that is a description of its API surface, not of its
permissions. `docs/rbac.md` says the same thing on the
`create operators.coreos.com/subscriptions` row, and `docs/safety-model.md` §12.5
is titled "the honest limit" for this reason.

**A catalog is a supply chain, and this console renders somebody else's claims in
its own chrome.** `certified`, `capabilityLevel`, `provider`, `repository`,
`containerImage`, the long-form description: all of it is read verbatim out of
CSV annotations written by whoever published the package. This console cannot
verify any of it and does not try. `certified: true` means the publisher wrote
`certified: "true"` in an annotation — a claim, restated. The mitigations are
real and small: the value is tri-state, so an annotation that could not be parsed
is `null` rather than a guessed `false`; the description is rendered as text
rather than as markdown (see below); and no ranking, badge or ordering of this
console's own is layered on top. What none of that fixes is that a
self-assessment displayed inside a trusted console borrows the console's
credibility, and an operator reading a green "Certified" row is reading a
publisher's sentence in an administrator's typeface.

**The portal cannot uninstall, and the asymmetry is real.** Deleting a
Subscription does not remove an operator: the ClusterServiceVersion stays, and so
does its Deployment, its CRDs and its cluster-wide RBAC. So §16 offers no
removal, explains what removal actually takes, and leaves both deletions to §4.
That is the correct answer to *should there be a Remove button that lies* and it
is not a complete feature. It is measurably easier to add an operator here than
to remove one, and the console tilts that way on purpose, which is a strange
direction for a safety-first product to tilt. Closing it honestly means modelling
a two-object removal with two diffs and an ordering constraint, and that is a
larger feature than the one this ADR is deciding.

**This speaks OLM v0 only.** `operators.coreos.com` Subscriptions and
`packages.operators.coreos.com` PackageManifests. OLM v1 —
`olm.operatorframework.io` `ClusterExtension`, with a different resolution model
and no PackageManifest API — is not read at all. A cluster running only v1
resolves every §16 source to `unsupported` and gets a calm empty portal saying
this cluster does not serve those objects. That answer is *correct*: the cluster
genuinely serves no Subscriptions. It is also, on that cluster, useless, and the
population of such clusters only grows. This is a gap with a direction, not a
steady state.

## What was rejected

**Shipping or curating a catalog of our own.** The single largest thing this ADR
declines, and the one that would turn it into ADR-0004's second bundle. A
console-owned catalog is a list this project would have to maintain, defend and
have opinions about, and every entry in it would be a recommendation made by a
tool that cannot review the software it is recommending. Reading the cluster's
own CatalogSources costs nothing, works with a private mirror with no
configuration, and is the true answer on a cluster with none: an empty portal.

**Rendering the CSV description as markdown.** Catalogs publish long-form
markdown, and rendering it is what every other operator UI does. It is
third-party content displayed inside an authenticated administration console,
which makes any renderer an attack surface reachable by anyone who can get a
package into a catalog the cluster trusts, and formatted text is *more*
persuasive than plain text at exactly the moment persuasion is the risk. Shown as
text.

**A Remove button that deletes only the Subscription.** It looks like an
uninstall, takes one click, and leaves the operator running. Reporting an
uninstall that did not happen is the one thing this project's defect standard
refuses outright.

**Refusing to write when the namespace has no OperatorGroup.** Considered and
rejected on ADR-0004's reasoning, unchanged: a Subscription created before its
OperatorGroup is a legitimate order of operations — a namespace being prepared,
a manifest set applied out of order — and the console cannot tell "there is none"
from "there is one we could not read". So it writes, and it makes the caller
acknowledge `no_operator_group` (or `operator_group_unknown`) by name first. The
"unknown" codes are acknowledgeable on the same terms as the rest, deliberately:
letting a read that did not happen pass silently is the swallowed `[]` this
project exists to refuse.

**Approving InstallPlans.** A `Manual` approval strategy exists so that a person
looks at the resolved set of ClusterServiceVersions before it lands. A one-click
Approve on a row would be that consent given without the diff — and it is
available as an ordinary §4 write, where it gets one. §16 reports that an
InstallPlan is holding an install, and warns about `manual_approval` before the
subscription is created.

**Creating the OperatorGroup for the operator.** An OperatorGroup decides what
every operator in a namespace may act on, including ones already installed. It is
a scope decision about the namespace, not a step in subscribing to one package,
and folding it into a subscribe flow would hide the larger of the two changes
inside the smaller.

## The boundary

For whoever reads this next and is deciding whether the next thing belongs here:

> The operator portal reads catalogs the cluster already runs and writes
> **one `Subscription`, through the ordinary write funnel, and nothing else,
> ever.** No uninstall, no InstallPlan approval, no OperatorGroup creation, no
> CatalogSource management, no catalog of this project's own, and nothing that
> reaches a registry. The console's claim over an operator ends at the object it
> created; everything past that belongs to OLM and is reported as OLM's, never
> as ours.

ADR-0004's boundary stands beside this one and is unchanged: one pinned bundle of
eight objects. §16 does not extend it, and the day somebody argues that a second
shipped bundle is "really just like the portal" is the day both boundaries need
re-reading rather than reinterpreting.

If §16 is ever cut, nothing else moves. `services/portal.py`, `admin/portal.py`
and `api/portal.py` are self-contained, the only shared code is the funnel and
the envelope every other section already uses, and `ADMIN_PORTAL_INSTALL_ENABLED`
defaulting to false means the write does not exist on a deployment that never
opted in. That removability is both the strongest structural argument for trying
it and the honest escape hatch if the supply-chain cost is later judged too high.

## See also

- `docs/api-contract.md` §16 — normative: the four endpoints, the tri-states, the
  consequence codes, and what `applied: true` does and does not mean.
- `docs/safety-model.md` §12 — the acknowledgement handshake, the two gates, why
  the dry run is permitted with the feature gate off, and §12.5's honest limit on
  what OLM grants afterwards.
- `docs/adr-0004-shipped-router.md` — the boundary this ADR argues it does not
  cross, and the escalation-prevention case that is the nearest precedent for
  §0.2's promise not reaching far enough.
- `docs/rbac.md` — the six read grants and what withholding each one degrades,
  and the `create operators.coreos.com/subscriptions` row, whose point is what
  the grant does **not** bound.
