# ADR 0004 — k8boss-admin ships and installs a router

**Status:** accepted
**Date:** 2026-08-22
**Supersedes nothing. Amends:** the README's "not a deployment engine".

**Decided against the analysis.** The design review that preceded this ADR
recommended the opposite — write objects for whatever router the cluster
already runs, and ship no data plane — and three independent judge passes ranked
that first on product identity and on operator truth. The product owner read
those costs, stated in §"What this costs" below, and chose to ship the router
anyway. This ADR records the decision *and* the argument against it, because the
argument does not stop being true once the decision is made, and whoever revisits
this needs both halves.

## The decision

k8boss-admin ships a pinned HAProxy ingress-controller bundle as data and can
install it, upgrade it and remove it from the console. The feature is off by
default behind `ADMIN_ROUTER_MANAGE_ENABLED`.

It ships **manifests, not a controller.** There is no reconcile loop in the
console process, no desired state in its database, and no watch. Install,
upgrade and uninstall are each a sequence of ordinary writes through
`app.admin.mutate.mutate()` — eight objects, eight preflights, eight diffs,
eight audit rows. What keeps the router running afterwards is the Kubernetes
control plane. What routes traffic is HAProxy's own in-cluster controller.
`GET /api/router` is a live read like every other page.

## Why

An exposure written into an API is not an exposure. On a bare cluster —
kubeadm, k3s with `--disable=traefik`, a lab, an air-gapped rack — a `POST` of
an Ingress returns 201 and nothing routes. The object sits there, `admitted`
stays `null` forever, and the operator's browser hangs on the hostname.

That is this project's own defect standard pointed at its newest feature: *an
action reported as done is a claim, and a claim about somebody's cluster has to
be true.* A Routes screen that can only write objects reports success for
something that did not happen, on precisely the clusters where the feature was
most needed.

There were two honest resolutions. Refuse to write when nothing will serve the
object — which makes the feature useless where it matters and turns an ordinary
fact into a wall. Or offer the operator a way to make something serve it. This
is the second.

The timing is not incidental. `kubernetes/ingress-nginx`, which served roughly
half of all Kubernetes clusters, was retired and its repository archived
read-only on **2026-03-24**, after a joint Steering Committee and Security
Response Committee statement on 2026-01-29 warning that staying on it leaves
users vulnerable; `InGate`, the project meant to succeed it, was retired before
it shipped, and upstream deliberately blesses no successor. "The cluster already
has an ingress controller" is a materially weaker assumption in 2026 than it was
in 2024.

## Why HAProxy

Because it is what the OpenShift Router is built on, which makes it the honest
answer to "the same thing OpenShift uses" — the framing the feature was
requested in. And because it is one of the few actively released ingress
controllers left: 3.2.13, August 2026.

**What it does not serve, and the console says so before anyone finds out the
hard way:**

- **HTTPRoute — no.** The HAProxy Kubernetes Ingress Controller implements
  Gateway API for **TCPRoute only**. Enabling the console's Gateway API option
  grants the permissions and sets the controller name, and it still will not
  accept an HTTPRoute. Serving those needs Envoy Gateway, Istio or Cilium, and
  this console installs none of them: a Gateway API implementation is often the
  CNI or the mesh, which is a far larger commitment than an add-on proxy.
- **OpenShift Route — no.** An OpenShift cluster already runs its own router.
  Installing a second one contending for the same hostnames is how an outage
  starts.

So the shipped router is a data plane for the **Ingress** backend, which is the
portable one, the one every cluster's API serves, and the one that has had no
default answer since March 2026.

## What this costs, stated rather than argued away

**"Not a deployment engine" becomes false.** The console installs software. The
README says so now. Precisely which claims survive:

| Claim | After this |
|---|---|
| "holds no cluster state" | **True.** Nothing is stored. "Where is the router installed" is read back off its own ClusterRoleBinding's subject, not from a row. |
| "every page is a live read" | **True.** `GET /api/router` reads the cluster every time. |
| "not a GitOps controller" | **True.** No reconcile loop, no drift correction, no watch. |
| "not a deployment engine" | **False, narrowly.** It installs one pinned bundle of eight objects through the ordinary write funnel, and nothing else, ever. That boundary is the point of this ADR. |

**It is a minority industry posture, and reviewers are right to say so.**
There is close to a controlled experiment here: SUSE met the ingress-nginx
retirement holding both a distribution and a console, and made opposite choices
— RKE2 bundled Traefik as its default, while Rancher added Gateway API support
and a UI for *selecting* a controller. Portainer detects IngressClasses and
installs nothing. Headlamp — the Kubernetes project's UI since Dashboard was
archived — is a proxy in front of the API with no data plane at all. And
OpenShift, the platform this request was modelled on, ships HAProxy through
`cluster-ingress-operator`: even there, the *console* is a separate component
that only reads and writes Route and Ingress objects. Every comparable product
draws the line short of this, and the distribution-versus-console split is where
they all draw it. The counter-argument is narrower than it sounds:
each of those has a distribution, a platform, or an explicit bring-your-own
contract to fall back on, and a standalone console pointed at a bare cluster has
none — where the alternative is not neutrality, it is writing objects that do
nothing.

**The shipped router cannot serve a weighted exposure at all.** The HAProxy
Kubernetes Ingress Controller has no traffic-weighting or canary annotation —
not one, anywhere in its annotation reference. So §13's traffic-split feature is
reported as lossy on the Ingress backend, which is correct and which the console
already says, and on this router it is lossy in the strongest sense: only the
first target receives traffic and no annotation exists that would change that. A
canary needs a Route or an HTTPRoute, and therefore a different data plane.

**A pinned third-party image is a recurring obligation with a clock on it.**
This project does not fork HAProxy and owes no CVE response for it, but it does
recommend a version, and a stale `ROUTER_VERSION` is a stale proxy on somebody's
ingress path. ingress-nginx is the cautionary tale precisely because a small
team could not sustain security response for a widely deployed data plane. The
mitigations reduce the cost and do not remove it: `versionMatches` computed from
the label, one endpoint for the upgrade, and `deploy/router.yaml` checked in and
test-enforced so every bump is a reviewable diff over real manifests. **This is
a permanent maintenance line item and it is the strongest argument against the
whole decision.**

**The install creates the largest RBAC grant this product has ever asked for.**
Cluster-wide `get,list,watch` on Secrets, because an ingress controller that
terminates TLS must read certificate Secrets and RBAC cannot scope that to "only
the referenced ones", plus `create,patch,update` for the generated default
certificate. Upstream offers nothing narrower. What mitigates it: the object is
in the diff before it is created, the feature has its own gate that is off by
default, and the plan is readable while that gate is off so the decision to open
it is informed.

**Preflight cannot see RBAC escalation prevention.** A `SelfSubjectAccessReview`
on `create clusterroles` answers *yes*; the API server then refuses with
`attempt to grant extra privileges`, because the caller does not itself hold
cluster-wide Secret reads. No review can report this — it is enforced at
admission, not by verb matching. §0.2 promises a denial that names the missing
permission, and for this one action the console cannot keep that promise from
the review alone. What it does instead is narrow and deliberate: the status
mapping stands (`rbac_denied`, by status code, as everywhere), and **only the
hint** is rewritten to name escalation prevention and the `escalate`/`bind`
grants. Matching on the API server's message string is normally forbidden here;
it is confined to the hint so that a miss degrades to the ordinary hint rather
than to a wrong code.

## What was rejected

**Refusing to write when nothing will serve the object.** Considered and
rejected: an Ingress written before its controller is installed is a legitimate
order of operations, and the console cannot tell "no controller" from "a
controller it cannot see".

**Fetching the manifests from upstream at install time.** An install that reaches
the internet does something different on the day upstream changes a default, on
the day the cluster is air-gapped, and on the day somebody takes over the
repository. Baked in, pinned, and shown in the diff before it is applied.

**Adopting objects that already carry the bundle's names.** The console refuses
and names the object. An operator who asked for an install and got a silent
takeover of somebody else's ClusterRole is the worst outcome this feature has.

**Deleting the Namespace on uninstall.** It would be tidier and it is not
recoverable. The namespace is retained and reported.

**Rolling back a partial install.** Deleting what succeeded is more writes the
operator did not approve, against objects that may already be in use. The
response reports per-object outcomes and `installed: false`.

## The boundary

For whoever reads this next and is deciding whether the next thing belongs here:

> k8boss-admin installs **one pinned bundle of eight objects, through the
> ordinary write funnel, and nothing else, ever.** No second bundle, no
> operator, no chart rendering, no reconcile loop, no drift correction. The day
> this list grows is the day the README's remaining three claims start going the
> same way as the fourth.

If the router is ever cut, §13 stands unchanged: everything in
`services/routes.py`, `admin/routes.py` and the §13 half of `api/routes.py` is
independent of it. The router is a clean, removable increment — which is both
the strongest structural argument for trying it and the honest escape hatch if
the identity cost is later judged too high.

## See also

- `docs/api-contract.md` §13 (exposures) and §14 (the router) — normative.
- `docs/safety-model.md` — the two gates, and why a dry run is permitted with
  the feature gate off, unlike §5.5's node debug pods.
- `deploy/router.yaml` — the same bundle, applicable by hand, generated by
  `make router-manifest` and enforced by a test.
