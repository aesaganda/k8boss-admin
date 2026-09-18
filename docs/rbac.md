# RBAC reference

Every Kubernetes permission the console uses, grouped by the feature that needs
it, with **what happens if you withhold it**.

That last column is the point of this document. Least privilege is only
practical when the cost of each grant is known, and the honest cost is rarely
"the console breaks". It is usually one column of one table becoming `—`, with
an `unavailable[]` entry saying `forbidden` and a banner naming it. Read the
degradation, then decide.

The shipped roles are in [`deploy/rbac.yaml`](../deploy/rbac.yaml):
`k8boss-admin-reader` and `k8boss-admin-writer`. **Both bindings are applied in
the manifests as they stand, and the writer role carries a wildcard
`apiGroups: ["*"] / resources: ["*"]` write rule** — opted into deliberately for
this deployment, and enumerated in that file's own header. So the per-feature
grants below describe what the console's *features* need, not what its
ServiceAccount currently holds: on the shipped manifests it holds everything.
Deleting the wildcard rule is what makes this table the operative document, and
that is the deployment it is written for.

**Every permission here belongs to one ServiceAccount per cluster, not to the
person signed in.** The console's own users decide who may reach the console;
they decide nothing about a cluster, so two operators with different console
roles have identical power *through* a cluster's registered credential, and
every preflight in this document answers about the ServiceAccount. That is why
withholding a grant is the control, and why this table is worth reading before
enabling writes.

[`adr-0007-impersonation.md`](adr-0007-impersonation.md) is **accepted and
implemented**, so the paragraph above describes the default rather than the only
behaviour. `Cluster.impersonation_enabled` is per-cluster and off until somebody
sets it; where it is set, `app/k8s/impersonation.py` sends `Impersonate-User`
and `Impersonate-Group` for the signed-in operator, and the API server
authorizes, admits and writes its own audit record as that person. A session
that cannot supply a cluster identity is refused with `impersonation_unavailable`
rather than served as the console — a silent fall-back would show an operator
data their own RBAC forbids. What that grant costs is in the ADR; what it ships
as is commented out, below.

**One thing is not identical between two operators, and it is the credential
itself.** With `AUTH_ENABLED=true`, registering a cluster, editing one and
de-registering one are administrator-only — `POST`, `PUT` and
`DELETE` on `/api/clusters`, §3 of [`api-contract.md`](api-contract.md). The
reason is the `PUT`: it is a partial update and an omitted `token` keeps the
stored one, which is a promise the console has to keep and also the shape of the
attack. An account that can move `api_server` while leaving `token` out has
redirected this console's bearer token at a host it chose, and the next request
hands the token over. Clearing `impersonation_enabled` is the quieter half of
the same edit — every later call to that cluster reverts to the ServiceAccount,
so the caller leaves their own RBAC behind and inherits the console's. In legacy
proxy mode there is no console role to check, so the proxy in front owns that
decision exactly as it owns every other endpoint.

---

## How a missing permission surfaces

Three different shapes, and knowing which to expect saves a lot of guessing:

1. **A refused primary read** — the thing the endpoint exists to return — is
   `403 rbac_denied`. The §1.3 envelope's `context` names the exact
   verb/group/resource/namespace and `hint` names the grant. The page renders an
   error state, not an empty table.
2. **A refused secondary read** — a column, a tally, a related object — becomes
   `null` in the row, `partial: true` on the envelope, and one entry in
   `unavailable[]` with `reason: "forbidden"`. The page renders, with a
   persistent banner naming what is missing. `null` renders as `—`, never as
   `0`.
3. **A refused write** is `403 rbac_denied` at preflight, before the cluster is
   touched. In the UI the button is **disabled with the reason showing** — not
   hidden. An operator must be able to see that an action exists and why they
   cannot use it.

None of the three is ever a shorter list presented as fact. That distinction is
the whole product; see [`safety-model.md`](safety-model.md).

---

## Cross-cutting: the two grants everything depends on

| Permission | Why | Withheld |
|---|---|---|
| `create authorization.k8s.io/selfsubjectaccessreviews` | §0.2 preflight, and §11.4's disabled-with-a-reason buttons | **Degrades the console globally.** Every preflight returns `allowed: false` with a non-null `evaluationError`, which the UI must render as "we could not determine whether you may" — not as a denial. Writes refuse to proceed on a permission that was never established. Withholding it makes the console uninformative, not safer. Kubernetes binds this to `system:basic-user` for every authenticated identity anyway |
| `get` on `/api`, `/api/*`, `/apis`, `/apis/*`, `/version` (non-resource URLs) | §4 catalog discovery; `server_version` on §2 and §3 | The catalog is empty, so **every** typed page fails at `resolve()` before it reaches a resource. This is the one entry in this document whose absence really does break the console |

A `create` verb inside a read-only role looks wrong and is correct: a
`SelfSubjectAccessReview` creates nothing. It asks the API server "may this
identity do X" and returns an answer.

---

## Read: cluster overview and nodes

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch ""/nodes` | §5 node list and detail; §3 overview node counts and capacity | Nodes page is `403`. The overview's `nodes` and `capacity` keys go `null` with an `unavailable` entry; the other five panels are unaffected |
| `get,list,watch ""/pods` | §6 pods, §5 per-node pod lists, §3 pod phases | Pods page is `403`. Per-node `requested` and `pod_count` become **`null`, never `0`** — a node showing `0 pods, 0 cores` reads as idle, and an idle node is the one an operator picks to drain |
| `get,list,watch ""/namespaces` | §5 namespaces; every page's namespace filter | Namespaces page is `403` and the namespace selector is empty, so cluster-scoped browsing still works but per-namespace filtering does not |
| `get,list,watch ""/events` | §5 events | Events page is `403`. Nothing else changes |
| `get,list,watch certificates.k8s.io/certificatesigningrequests` | §25 the Certificate requests tab, and the decoded subject on it | The tab reports the listing was refused. It never reports an empty one: "nothing pending" is the sentence that stops somebody looking, and a node that never joined is often a request nobody saw |

## Read: workloads

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch apps/deployments,statefulsets,daemonsets,replicasets` | §6 workload rows and detail | Those kinds vanish from the unified list — with an `unavailable` entry per refused kind, so the list says which kinds it could not read rather than looking like a cluster with fewer workloads |
| `get,list,watch batch/jobs,cronjobs` | §6 Jobs and CronJobs, including `suspended` / `schedule` / `last_schedule` | Same shape: named in `unavailable`, absent from the rows |
| `get,list,watch apps/controllerrevisions` | §6 rollout history for StatefulSets and DaemonSets | The rollout panel for those two kinds reports that it could not read history. It must **not** be rendered as "this workload has never been rolled out" — that is a different fact. Deployments are unaffected: their history lives in ReplicaSets |
| `get,list,watch ""/services` | §6 workload detail's related Services; §8 Services | The `services` block of a workload detail is empty with an `unavailable` entry |

## Read: one pod — logs, environment, usage

| Permission | Feature | Withheld |
|---|---|---|
| `get ""/pods/log` | §7 `GET /api/pods/{ns}/{name}/logs` and the log WebSocket | The log viewer shows a `forbidden` error frame. **This is a separate RBAC resource from `pods`** — granting `pods` alone gives a console that lists pods and cannot show one line of their output, which is the state most operators notice first |
| `get,list metrics.k8s.io/pods` | §7.7 the pod page's Metrics tab | Every `usage` is **`null`, never `0`**, with a `forbidden` entry in `unavailable[]`. Requests and limits still render — they come from the pod. A pod drawn at zero cores reads as idle, and idle is what gets something turned off. On a cluster with no metrics-server the same tab reports `unsupported` instead, an ordinary fact rendered calmly, regardless of this grant |
| `get ""/configmaps` | §7.6 resolving a ConfigMap-sourced environment variable to its value | The variable's row stays, with `value_state: "unreadable"` and the ConfigMap named in `unavailable[]` — never a blank that reads as "this variable is unset". Already granted by the §8 Configuration rule below; listed here because this is the second feature that depends on it |
| `get ""/secrets` | §7.6 listing the **key names** an `envFrom: secretRef` imports | Those rows collapse to one saying an unnamed set of variables comes from that Secret. **This endpoint never returns Secret values under any grant or setting**, so withholding this costs key names only — a `secretKeyRef` is answered without reading the Secret at all, and its tab renders in full on a console with no `get secrets` |

## Read: config, storage, access

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch ""/configmaps` | §8 ConfigMaps | That tab shows one `forbidden` entry instead of a table |
| `get,list,watch ""/secrets` | §8 Secrets (key names and byte lengths only) | Secrets tab shows `forbidden`. **See the warning below before granting this** |
| `get,list,watch ""/serviceaccounts` | §8 ServiceAccounts | That tab shows `forbidden` |
| `get,list,watch ""/resourcequotas,limitranges` | §17's namespace page and §29's quota advice | §17 renders a failure panel where the quota table would be. §29 reports `quotas: null` — **never `[]`**, which is the answer that says nothing bounds this namespace and every workload is admitted — and without the LimitRanges it withholds the "must specify" conclusion entirely rather than computing it from an empty default map, because that error reports a workload as refused for a value a default it could not read would have supplied |
| `get,list,watch ""/persistentvolumeclaims,persistentvolumes` | §8 storage; §26's delete plan reads each bound volume's reclaim policy | Those tabs show `forbidden`. For §26, a volume that cannot be read is reported as **unknown** — never as safe, and never as destroyed: `Delete` and `Retain` are opposite outcomes and there is no defensible default between them |
| `get,list,watch storage.k8s.io/storageclasses` | §8 StorageClasses, including `is_default` | That tab shows `forbidden`; PVC rows still render, with `storage_class` as the name only |
| `get,list,watch storage.k8s.io/volumeattributesclasses` | §8.1 Storage's Volume Attributes Classes tab | That tab shows `forbidden`. On a cluster old enough not to serve the API at all, this permission is moot — the tab shows `unsupported`, an ordinary state, regardless of the grant |
| `get,list,watch rbac.authorization.k8s.io/roles,rolebindings,clusterroles,clusterrolebindings` | §8 Access page | Access page shows `forbidden`. Reading RBAC is not the same privilege as holding it, but it is a map of the cluster's permissions — a reasonable thing to withhold |
| `get /namespaces`, `list /pods`, `list rbac.authorization.k8s.io/rolebindings`, `list networking.k8s.io/networkpolicies` (per namespace) | §17's project page — the namespace itself, its pod tally, its bindings and its policy summary | The namespace read is primary: withheld, the page is a 403 with a hint. Each of the other three is secondary and its section is `null` with the reason. `get namespaces` also decides whether a project can be created at all — the plan reports `exists: null` when it is refused, and the write refuses to proceed, because "unknown" is not a state a create may start from |
| `get,list,watch networking.k8s.io/ingresses` | §8 Ingresses | Ingresses tab shows `forbidden` |
| `get,list,watch networking.k8s.io/ingressclasses` | The `class` column for an Ingress whose spec names none, and §8.1 Network's own Ingress Classes tab | The Ingress column is blank rather than wrong; the Ingress Classes tab shows `forbidden` |
| `get,list,watch networking.k8s.io/networkpolicies` | §8.3 Network's Network Policies tab, and §8.4 its Pod Isolation view | That tab shows `forbidden`. The isolation view fails outright rather than reporting every pod as unrestricted from a read that did not happen |
| `get,list,watch discovery.k8s.io/endpointslices` | §8 Services' `endpoint_count`, and §8.1 Network's own Endpoint Slices tab | The `endpoint_count` column is **`null`**, with an `unavailable` entry — not `0`, which would claim the Service backs nothing. A Service that genuinely has no EndpointSlice still reports `0`, which is a real zero. The Endpoint Slices tab shows `forbidden` |
| `get,list,watch ""/endpoints` | The Network page's endpoint listing | That table shows `forbidden` |
| `get,list,watch autoscaling/horizontalpodautoscalers` | §8.1 Configuration's HPAs tab | That tab shows `forbidden` |
| `get,list,watch autoscaling.k8s.io/verticalpodautoscalers` | §8.1 Configuration's VPAs tab | That tab shows `forbidden`. VerticalPodAutoscaler is a CRD, not a built-in API — on the large majority of clusters without it installed, the tab shows `unsupported` regardless of this grant |
| `get,list,watch policy/poddisruptionbudgets` | §8.1 Configuration's Pod Disruption Budgets tab, alongside its existing drain-planning use below | That tab shows `forbidden` |
| `get,list,watch ""/resourcequotas,limitranges` | §8.1 Configuration's Resource Quotas and Limit Ranges tabs, and §17's project page | Those tabs show `forbidden`. On the project page the section is **`null`** with the reason — a failure panel, never an empty one, because "nothing bounds this namespace" assembled from a listing that did not happen is how a second quota lands on top of the first |
| `get,list,watch scheduling.k8s.io/priorityclasses` | §8.1 Configuration's Priority Classes tab | That tab shows `forbidden` |
| `get,list,watch node.k8s.io/runtimeclasses` | §8.1 Configuration's Runtime Classes tab | That tab shows `forbidden` |
| `get,list,watch coordination.k8s.io/leases` | §8.1 Configuration's Leases tab | That tab shows `forbidden` |
| `get,list,watch admissionregistration.k8s.io/mutatingwebhookconfigurations,validatingwebhookconfigurations` | §8.1 Configuration's two webhook-configuration tabs | Those tabs show `forbidden` |
| `get,list,watch gateway.networking.k8s.io/gateways,gatewayclasses,httproutes,grpcroutes,referencegrants,backendtlspolicies` | §8.1 the whole Gateway (beta) page | Each tab shows `forbidden`. On a cluster without Gateway API installed at all, every tab shows `unsupported` instead, regardless of this grant — an ordinary state, not a permissions problem |

> ### The Secret rule is the most privileged line in the shipped role
>
> `list secrets` cluster-wide means the console's ServiceAccount can read the
> value of every Secret in the cluster. The console never puts a value in a list
> response, and the single-object read redacts unless three separate conditions
> hold — but **RBAC grants the API access, not the console's discipline about
> it**. Anyone who can exec into the console's pod holds this grant directly.
>
> Deleting the rule costs exactly one tab. It is a reasonable default to remove.

## Read: why a pod is Pending (§31)

Every one of these but the pod itself is secondary: withholding it costs a
section of the answer, names itself in `unavailable[]`, and leaves the rest
standing.

| Verb | Group / resource | Withholding it |
|---|---|---|
| `get` | `pods` | The endpoint has no answer and raises. Already granted for §7.5 |
| `list` | `events` | The scheduler's own verdict is unreadable. It is drawn as **"no explanation is readable"** — never as a blank, which would read as nothing being wrong |
| `list` | `nodes` | `nodes: null`. Which nodes are ruled out is unknown, and the panel says so rather than rendering an empty table, which would report a cluster with no nodes |
| `list` | `pods` (cluster-wide) | Every node's `capacity_checked` is false: what each already holds is unknown, so none is judged on room. No node is reported as having any |
| `list` | `persistentvolumeclaims` | `claims: null`. Whether a volume is holding the pod back is unknown, never that it mounts none |
| `list` | `storage.k8s.io/storageclasses` | Every unbound claim's `blocks_scheduling` is `null`: cause and symptom cannot be told apart without the binding mode |

All six are in the reader role already — §31 adds no grant. It is listed here
because the degradation is worth knowing before somebody trims the role.

## Read: the certificate an exposure serves (§32)

No new grant. All four reads are already in the shipped reader role, and this
records what withholding each costs — worth knowing before somebody trims it.

| Verb | Group / resource | Withholding it |
|---|---|---|
| `list` | `networking.k8s.io/ingresses` | No Ingress certificate is reported. The listing names itself in `unavailable[]`; the report never shows a short list that looks complete |
| `list` | `route.openshift.io/routes` | The same for Routes, including the ones carrying an inline certificate — which need no Secret read at all |
| `list` | `gateway.networking.k8s.io/gateways` | No Gateway listener certificate is reported. On a Gateway-API cluster that is the whole page, so `kinds[]` says which API could not be read rather than rendering an empty table |
| `get` | `secrets` | **Every Secret-backed row is `state: unknown`** with a finding naming the failed read, and every host on it is `covered: null`. It is never reported as an exposure with no certificate. Routes with an inline certificate still report normally. This is the one grant §32 shares with the Secrets tab, and it is `get`, never `list`: §32 opens Secrets it was told the name of |

A caller holding `get secrets` in one namespace and not another gets a report
that is right about both — the readable rows carry certificates, the rest carry
their reason.

## Read: the resource browser

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch apiextensions.k8s.io/customresourcedefinitions` | §4 catalog listing CRDs | Custom resources do not appear in the browser's catalog. Note that reading the *definitions* is not reading the custom *objects*: a CRD the reader role does not name is listed and returns `forbidden` when browsed, which is the honest answer |
| `get,list,watch apiregistration.k8s.io/apiservices` | §4 catalog's aggregated-API health | An aggregated group that is down still lands in `unavailable` with `reason: unreachable`, but without the APIService the console cannot say which aggregation layer it was |
| `get,list,watch policy/poddisruptionbudgets` | §5 drain planning, read half | The drain plan cannot tell whether a PDB blocks an eviction — and it says so in the plan rather than guessing. It never reports "nothing blocks this". Granted in the *reader* role on purpose, so a drain plan can be previewed on a read-only install |

Anything else the resource browser can reach follows the same rule: the catalog
lists what discovery reports, and browsing a resource the role does not cover
returns `403 rbac_denied` naming it. A cluster's CRDs are not enumerable in
advance, so the shipped reader role does not try to; grant what you want
browsable.

§8.2's Custom Resources page reads the same catalog and is governed by the same
two rows above — it introduces no permission of its own. Grouping the catalog
by API group does not change what an operator can see; a CRD this role does
not name still lists in Custom Resources (from `customresourcedefinitions`
alone) and still returns `forbidden` the moment its instances are browsed.

## Read: the operator portal

Six resources across the two API groups Operator Lifecycle Manager serves.
`get,list` only — every §16 read is a one-shot live listing.

A cluster that does not run OLM serves none of them, and that is an ordinary
fact: each source resolves to `unsupported`, the portal renders a calm notice
saying OLM is not installed here, and nothing lands in the partial banner.
**Withholding these grants on a cluster that does serve them is a different
answer and the page keeps them apart** — discovery is refused, the source
resolves to `unknown`, and it goes in `unavailable[]` with `reason: forbidden`.

| Permission | Feature | Withheld |
|---|---|---|
| `get,list packages.operators.coreos.com/packagemanifests` | §16 the catalog — every package this cluster's CatalogSources offer | **Not an empty catalog. An unanswerable one, and the page says so.** The `packages` source resolves to `unknown`, one `unavailable` entry lands with `reason: forbidden`, and the partial banner names it. §16 refuses to report a refused read as absence: a portal claiming "this cluster offers no operators" sends somebody to add a CatalogSource that is already there. On a cluster with no OLM at all the source is `unsupported` instead — a calm info notice, never red — regardless of this grant |
| `get,list operators.coreos.com/subscriptions` | §16 the Installed view, and the catalog's `installed` column | The Installed view shows `forbidden`. In the catalog every row's `installed` is **`null`, never `false`** — "not installed" for an operator that is installed is how an operator subscribes twice, and two Subscriptions for one package leave two resolutions competing for the same CRDs. `installations` is `null` for the same reason, not `[]` |
| `get,list operators.coreos.com/clusterserviceversions` | §16 what OLM actually installed for each Subscription | Every row's `phase` is **`null`, with `phaseDetail` naming the read that failed** — never `Failed`, which would report a healthy operator as broken during an outage, and never blank. A Subscription that OLM has not acted on yet is *also* `null`, and `phaseDetail` is what tells the two apart |
| `get,list operators.coreos.com/installplans` | §16 whether an install is waiting for somebody to approve it | `approvalRequired` is **`null`, not `false`**, with `installPlanDetail` saying the listing failed. `false` would report an install as needing nothing while it sits stopped on an unapproved InstallPlan |
| `get,list operators.coreos.com/catalogsources` | §16 which catalogs the packages came from, and whether that registry is answering | `catalogs` is **`null`, not `[]`** — an empty list reads as a cluster with no catalogs, which is a different cluster from one whose catalogs could not be read. The package rows still render; their `catalog` names come off the packages themselves |
| `get,list operators.coreos.com/operatorgroups` | §16 the subscribe plan's namespace check | `target.ready` is `null` and the plan carries an `operator_group_unknown` consequence that must be acknowledged by name before the write. It never degrades to `false`: OLM installs only into a namespace governed by exactly one OperatorGroup, and reporting "this namespace has none" from a refused read would put a blocking consequence in front of a namespace that is perfectly configured |

## Read: cluster status

§19 is five unrelated listings joined into one page, and the whole point of
granting them separately is that **each one degrades only its own section**. A
console that may not list `admissionregistration.k8s.io` still has an honest
answer about version skew; it simply says nothing about admission webhooks
rather than saying there are none.

Every row below is already in `k8boss-admin-reader` for another feature — §19
adds no permission of its own, it joins reads the console already makes. What is
new is what each one now costs when withheld.

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch coordination.k8s.io/leases` (namespaced to `kube-system` is enough for §19) | §19 control plane — whether the leader-elected components are still renewing | `controlPlane` is **`null`, not `[]`**. An empty list reads as a control plane with nothing running in it, which on a managed cluster is also what a *correct* answer looks like — so the two must not be spelled the same |
| `get,list,watch apiregistration.k8s.io/apiservices` | §19 aggregated APIs — which extension APIs are answering, and why `metrics.k8s.io` went away | `apiServices` is `null`. The rest of the console keeps reporting a missing aggregated API as `unsupported` (§1.2), an ordinary fact; §19 is the only page that can say *why*, and without this grant it says it cannot |
| `get,list,watch apiextensions.k8s.io/customresourcedefinitions` | §19 CRDs — the ones that never became Established, or carry a non-structural schema | `crds` is `null`. Note this section lists only the *unhealthy* ones, so `{"items": [], "total": 74}` is the good answer and `null` is the absent one; collapsing them would report a healthy cluster from a refused read |
| `get,list,watch policy/poddisruptionbudgets` | §5's drain plan (which pod an eviction would be refused for) and §28's Disruption page (what each budget actually covers) | The drain plan reports `budgets: null` and says the eviction API is the enforcer; §28's page reports the listing was refused rather than an empty table, because "nothing constrains eviction on this cluster" is a finding and must never be produced by a failed read. §28 also needs `list pods` — without it every `selected_pods` is `null`, never `0`, and the overlap index is withheld rather than reported as empty |
| `get,list,watch admissionregistration.k8s.io/{validating,mutating}webhookconfigurations` | §19 admission webhooks — what intercepts writes to this cluster; §26 finds the ones backed from inside a namespace about to be deleted | Both refused: `webhooks` is `null`. **One refused:** the rows that answered are still shown with `complete: false`, and the page says out loud that they are not all of them. Never an empty list, which would say nothing is intercepting writes while something is refusing all of them |
| `get,list,watch discovery.k8s.io/endpointslices` | §19 webhook backends — whether a `failurePolicy: Fail` webhook has anything behind its Service | Every `endpoint_count` is **`null`, not `0`**, and no row is flagged as blocking. This is the finding §19 exists for and the one place a zero from a refused read would be actively harmful: it would name a healthy webhook as the thing breaking the cluster, during an incident, and send somebody to delete it |
| `get,list,watch snapshot.storage.k8s.io/{volumesnapshots,volumesnapshotclasses}` | §22 the snapshots that exist, whether each is usable, and what deleting one would do | Both tabs render §1.2's `unsupported` — the same calm "not present on this cluster" a cluster without the external-snapshotter gets, which is why withholding the grant and not installing the CRDs look alike here and the difference is in `unavailable[]`. Without the *classes* listing specifically, a snapshot's `deletion_policy` is **`null` — never `Delete` and never `Retain`**, and §22 makes the operator acknowledge that it is unknown rather than guessing: one guess warns about data loss that will not happen, the other withholds a warning about loss that will |
| `create authorization.k8s.io/subjectaccessreviews` | §23 asking what another user or ServiceAccount may do | The Access review tab reports that the console may not ask, naming **this** permission rather than the resource in the question — a refusal that named the latter would send an operator to grant alice something when the missing grant is the console's. **This is the most revealing grant in the reader role**: anyone who can issue these can map the authorization state of every identity on the cluster. Nothing §8's listings do not already imply, but far more precisely and including authorizers that are not RBAC — so there is no separate feature switch, the grant is the switch. Every review is audited either way |
| `get,list,watch core/nodes` | §19 version skew — every kubelet's version against the API server's | `versionSkew.nodes` is `null` while `server_version` still answers. Per-node `status` is `unknown` whenever either version is missing — never `ok`, which is a verdict about a node nobody checked |

The API server's own `/version` needs no RBAC; it is served to any authenticated
client. When it fails anyway, `server_version` is `null`, every node's `status`
is `unknown`, and the section stays rather than disappearing — the kubelet
versions are still worth reading, and the page does not pretend to know how far
from supported they are.

**No write, no preflight, no audit row.** §19 is a read, and the funnel is not
involved.

---

---

## Write

Everything below is in `k8boss-admin-writer`. Withholding **all** of it gives a
fully functional read-only console — a supported state, not a degraded one.

The manifests in this repository bind that role and add a wildcard write rule
beside it, so on an unmodified `kubectl apply -f rbac.yaml` the console holds
everything below *and everything else*. This section is what the features
actually need; it becomes the operative list once the wildcard rule is
deleted.

Remember that **dry-run needs the same verb as the real write**: the API server
requires `patch` to project a patch. A console that only previews still needs
these grants.

| Permission | Feature | Withheld |
|---|---|---|
| `get,patch,update apps/deployments/scale`, `statefulsets/scale`, `replicasets/scale` | §6 scale | The Scale button is disabled with the reason. **A separate RBAC resource from the workload itself** — which is the whole reason the console patches the subresource: a ServiceAccount can be allowed to resize a workload without being allowed to edit it. Grant this pair alone for a resize-only console |
| `patch apps/deployments,statefulsets,daemonsets` | §6 restart (a pod-template annotation, the `kubectl rollout restart` mechanism) and §6 rollback (an RFC 6902 replace of `/spec/template`) | Restart and Rollback disabled with the reason |
| `patch batch/jobs,cronjobs` | §6 suspend | Suspend disabled with the reason. Only Jobs and CronJobs have `spec.suspend`; other kinds are refused at `422 invalid` naming the kind, before RBAC is consulted at all |
| `create,update,patch,delete` on the §4/§8 resources | The YAML editor, the delete button, **and the `Create <Kind>…` button on those listings** | All three are disabled with the reason; the editor still opens read-only and the diff still renders. The create half is now the visible half — a button on the listing rather than a `403` after somebody pasted a manifest |
| `create,update,patch,delete networking.k8s.io/networkpolicies` | §8.3 Create NetworkPolicy, Edit YAML and Delete on the Network Policies tab | Those three are disabled with the reason and the tab stays readable. Withholding this while keeping the read grant is a sensible posture: the isolation view is the reason to open the tab, and it needs no write verb |
| `patch,update ""/nodes` | §5 cordon and drain; §24 taints and labels | All four disabled with the reason. Note this grants cordon of **any** node, control-plane included — and that §24 needs no grant of its own, which is worth reading twice: the same verb that cordons a node writes a `NoExecute` taint, and that taint **deletes** the pods that do not tolerate it. Unlike drain it does not use `pods/eviction`, so no PodDisruptionBudget refuses it and `create ""/pods/eviction` below is no part of the permission. An account trusted to cordon is, on this API, already trusted to empty the machine |
| `update certificates.k8s.io/certificatesigningrequests/approval` | §25 approving and denying | Both actions disabled with the reason, and the tab stays readable — which is the half worth keeping: what a request asks to become is a read |
| `approve certificates.k8s.io/signers`, **named for the signerName** | §25, and the rule people miss | Without it the console's own preflight on the rule above **passes** and the API server refuses the write anyway: its `CertificateApproval` admission plugin checks this separately. §25 preflights it by name, so the denial says which signer rather than telling an operator they cannot approve certificates at all. `resourceNames` makes it per-signer, which is why §25 has no deployment switch of its own — a boolean would be a coarser copy of a control RBAC already expresses exactly. **The shipped role grants the two node-lifecycle signers and not `kubernetes.io/kube-apiserver-client`**: that is the signer a `system:masters` certificate comes through, and adding it is the most consequential line in this file |
| `delete ""/namespaces` | §26 deleting a namespace | The Delete namespace button is disabled with the reason — **and the delete plan still renders**, which is the half that matters: an operator deciding whether to grant this verb can see exactly what granting it would destroy. This is the single most destructive verb in the file. The namespace controller deletes everything inside regardless of ownerReferences, and for a claim bound to a `Delete`-policy volume that means the storage provider destroys the disk. §26 puts the reclaim policy of every bound volume, the LoadBalancer addresses released and the admission webhooks left without a backend on screen before the button, and refuses the write until each is acknowledged by code — none of which is a substitute for withholding the verb. The §14 router block below grants it too, among much else; the shipped role states it separately so a deployment that wants neither can see that removing this one line is enough |
| `impersonate ""/users` and `""/groups`, **with `resourceNames`** | ADR-0007 — acting as the signed-in operator on a cluster registered for it | That cluster answers `impersonation_unavailable` for the operators not named, and **never falls back to the ServiceAccount** — a read served that way would show somebody data their own RBAC forbids. Clusters without the opt-in are unaffected. **Shipped commented out**, because the unrestricted form (`impersonate users` with no `resourceNames`) is cluster-admin by proxy: an account that can impersonate any user can impersonate the most powerful user on the cluster, and the console's blast radius stops being "whatever this file grants". Include `system:authenticated` in the groups list: the API server adds it to every request it authenticates itself and does *not* add it to an impersonated one, so omitting it refuses operators permissions they hold. **The other half is not RBAC** — the console sends the username *its* issuer states, so this cluster's `--oidc-username-claim` has to resolve to the same string; where the issuers differ, every request is refused as a person the cluster has never heard of, and nothing checks that for you |
| `create ""/pods/eviction` | §5 drain, pod half | The drain refuses at its own preflight, from inside the apply step — because permission to cordon a node is not permission to evict what runs on it, and finding that out three pods into a drain is not a discovery anyone wants. The console never falls back to deleting pods: delete ignores PodDisruptionBudgets |
| `delete ""/pods` | The resource browser's pod delete | That one button is disabled. Distinct from eviction: a different action with a different confirm dialog |
| `create ""/pods/exec` | §7 terminal | The terminal button is disabled with the reason. **This is the most dangerous grant in the file after a wildcard**: a shell in a pod can do whatever that pod's own ServiceAccount can, no diff is possible for a keystroke, and it bypasses every other control here. Withholding it is the only real control over it. The console additionally requires `ADMIN_ALLOW_MUTATIONS` for exec and audits the session on open and close. §15's CLI session runs on this same grant — it adds no exec route of its own — so withholding it disables the masthead terminal too, while leaving the CLI pod creatable |
| `get,patch ""/pods/ephemeralcontainers` | §7.4 debug containers — `kubectl debug`'s ephemeral container | The Attach button is disabled with the reason; existing debug containers are still *listed*, because that listing reads the pod and needs only `get pods`. **A separate RBAC resource from `pods`**, and a separate grant from `pods/exec`: attaching a container and typing in one are different acts. It is nonetheless close to `pods/exec` in blast radius — the operator chooses the image, and a debug container shares the pod's network namespace, its volumes and (on request) the process namespace of an application container. `get` is needed as well as `patch` because the console reads the subresource to build the diff it shows before writing. Withhold it for a console that can exec into what is there but cannot add to it |
| `create,update,patch,delete networking.k8s.io/ingresses` | §13 exposures written as an Ingress | The Ingress backend's create button is disabled with the reason, and the other two backends still work. Three separate rules for the three route kinds, because they are three separate RBAC resources and an account allowed to write Ingresses is very often not allowed to write Routes |
| `create,update,patch,delete route.openshift.io/routes` **and `routes/custom-host`** | §13 exposures written as an OpenShift Route | Without the first: the Route backend's create button is disabled with the reason. Without the second — and this is the one people miss — the button is *enabled*, the preflight on `create routes` passes, and the API server refuses the write. **OpenShift gates choosing a hostname behind its own subresource**, separately from creating the Route at all, so a console without it tells the operator they cannot create Routes: a permission the review just confirmed they hold. §13 preflights `routes/custom-host` explicitly, but only when `spec.host` is set — a router-generated hostname is not a custom one, and asking for a grant the action does not need would disable a control for a reason that is not true |
| `create,update,patch,delete gateway.networking.k8s.io/httproutes` | §13 exposures written as a Gateway API HTTPRoute | That backend's create button is disabled with the reason. Absent on most clusters anyway, where the backend resolves to `unsupported` and the console says "not present on this cluster" |
| `create,update,patch,delete` on `""/namespaces,serviceaccounts`, `networking.k8s.io/ingressclasses`, `rbac.authorization.k8s.io/clusterroles,clusterrolebindings` | §14 installing the shipped router | The router panel reports the install as denied with the reason. **The plan stays readable** — it is a pure render that touches no cluster — and every §13 exposure keeps working against whatever controller the cluster already runs. These are the verbs needed to *create* the router's objects; the router's own ClusterRole, which they let the console create, is what can then read every Secret in the cluster |
| `escalate` on `rbac.authorization.k8s.io/clusterroles`, `bind` on `clusterroles,clusterrolebindings` | §14, and **the grant whose absence produces the most confusing failure in this file** | Object 3 of 8 fails with `403 attempt to grant extra privileges` and the install is half-finished. These are not ordinary verbs: Kubernetes refuses to let an identity create a ClusterRole granting permissions it does not itself hold, enforced at admission rather than by verb matching — so it is **invisible to a SelfSubjectAccessReview**. The preflight answers "yes, you may create clusterroles" and the API server then says no. §14 rewrites the *hint* on that specific failure to name escalation prevention instead of a verb the operator already has; the code and the status stay mapped by status, as everywhere else. `escalate` is itself a privilege-escalation primitive — it lets this ServiceAccount write a role granting anything. Grant it only alongside the wildcard rule, and delete it whenever that goes |
| `create /namespaces`, `create /resourcequotas,limitranges`, `create rbac.authorization.k8s.io/rolebindings`, `create networking.k8s.io/networkpolicies` | §17 creating a project — five objects, each preflighted on its own | The New project button is gated on `create namespaces` alone and disabled with the reason; the other four are preflighted per object by the dialog's own dry run, where a denial names the object it belongs to and blocks Confirm before the Namespace is created. `create namespaces` and `create networkpolicies` are the grants §14 and §8.3 already carry; the quota, limit range and role binding verbs are new with §17 |
| `patch ""/namespaces` | §18 setting a namespace's Pod Security level — one merge patch on its labels | The Set level button on the namespace page is disabled with the reason (§11.4); the page keeps reporting the level the namespace declares, because reading it is a `get`. This is the grant that lets the console *lower* an enforce level, which makes pods the namespace refuses today deployable by anyone who can create a pod in it — every such change is dry-run first with the API server's own admission warnings on screen, acknowledged by code and audited, but the grant is the control. `patch namespaces` is also part of §14's router block, so a deployment that installs the router already has it |
| `patch autoscaling/horizontalpodautoscalers` | §21 setting an autoscaler's replica bounds — one merge patch on `spec.minReplicas` and `spec.maxReplicas` | The Set bounds action on the Configuration page's HPAs tab is disabled with the reason (§11.4). **Everything that makes that tab worth reading keeps working**, because it is all `get`/`list`: the bounds, each metric's current reading against its target, and — the one that matters — whether the autoscaler is scaling at all. This is the grant that lets the console lower a ceiling *below the running replica count*, which is not a cap on future growth but a scale-down applied at the controller's next decision, seconds later; §21 names how many pods move as a consequence acknowledged by code, dry-runs the patch and audits both bounds either side, but the grant is the control |
| `create snapshot.storage.k8s.io/volumesnapshots` | §22 taking a volume snapshot of a claim | The Snapshot action on the claims table is disabled with the reason (§11.4); the Volume Snapshots tab keeps reporting what exists and whether each one is actually usable, because those are `list`. **Creating is deliberately not deleting**: under a class with `deletionPolicy: Delete`, removing a VolumeSnapshot destroys the snapshot in the storage system, and that verb lives with §4's generic delete rather than here — so an install can let operators take snapshots without letting them remove one somebody is relying on |
| `bind` on `rbac.authorization.k8s.io/clusterroles` | §17's RoleBinding to `admin`, `edit` or `view` | Without it, the API server refuses the binding with RBAC escalation prevention whenever the console's ServiceAccount does not itself hold everything the aggregated ClusterRole grants — a refusal a `SelfSubjectAccessReview` on `create rolebindings` cannot foresee, so the preflight passes and the write fails. §17 rewrites the *hint* on that failure to name `bind`, the way §14 names `escalate`; the code stays `rbac_denied`. Already granted in the shipped writer role for §14, alongside the wildcard it lives beside |
| `create operators.coreos.com/subscriptions` | §16 subscribing to a catalog operator — **the one write the portal makes** | The Subscribe button is disabled with the reason. The plan stays readable — it is a pure read — and the catalog and Installed views keep working. **What this grant does not bound is the point of the row.** The console creates one Subscription; OLM then resolves it and installs the operator, and **OLM — not this ServiceAccount — grants that operator whatever its bundle asks for**, using its own permissions. No rule in `deploy/rbac.yaml` narrows that and no `SelfSubjectAccessReview` can preview it; the plan names the custom resources the operator will own, which is the most that can honestly be shown before the write. There is no `update` or `delete` here on purpose: deleting a Subscription does not uninstall an operator — the ClusterServiceVersion and everything it owns stay — so a Remove button would report an uninstall that did not happen. This grant also needs `ADMIN_PORTAL_INSTALL_ENABLED`, a deployment gate RBAC cannot express |
| `create,update,patch apiextensions.k8s.io/customresourcedefinitions` | §33 installing Operator Lifecycle Manager — phase one of two | The portal's "OLM is not installed" panel reports the install as denied with the reason. The plan and its manifests stay readable — a pure render, no cluster touched — and §16 keeps working exactly as before on every cluster that already runs OLM. **No `delete`, deliberately.** Deleting a CRD deletes every custom resource made from it across every namespace with no second confirmation, so granting it here would put an "Uninstall OLM" button one refactor away from taking out every operator's Subscription and ClusterServiceVersion on the cluster. §33 installs and does not uninstall |
| `create,update,patch operators.coreos.com/{olmconfigs,operatorgroups,catalogsources,clusterserviceversions}` | §33 phase two — OLM's own objects, which are instances of the CRDs above | The same degradation, and it lands mid-install rather than up front: phase one succeeds and phase two reports eighteen denials. These four are written **only** by §33; §16 reads them and writes none of them |
| **(what makes §33 work at all)** `escalate` and `bind` from the §14 block | §33 creating OLM's ClusterRole, which grants `apiGroups: ['*']`, `resources: ['*']` with every verb including `escalate` and `bind` | **This is the row to read before enabling §33.** Without `escalate`, the preflight on `create clusterroles` answers *yes* and the API server then refuses at admission — Kubernetes will not let an identity create a role granting more than it holds, and no `SelfSubjectAccessReview` can see that coming. §33 rewrites the hint to name escalation prevention rather than sending somebody to grant a verb the review just confirmed. What the grant does not bound is the same point as the Subscription row above, one layer larger: what this creates is cluster-admin plus the ability to grant cluster-admin, held by OLM. It is inherent to OLM rather than chosen here, and that is an explanation, not a mitigation. Needs `ADMIN_OLM_INSTALL_ENABLED`, a deployment gate RBAC cannot express |
| `create ""/pods` | §5.5 node debug pods, §15 CLI pods, and §4's create from the YAML editor | All three, together — RBAC cannot separate them. **This is the grant to think hardest about, and the one RBAC is worst at describing.** A `SelfSubjectAccessReview` has no field-level granularity: there is no verb for `hostPath`, `hostPID` or `privileged`, so `create pods` for an nginx pod is the same permission as `create pods` for one that mounts the node's root filesystem. PodSecurityPolicy used to gate that and was removed in 1.25; its replacement, Pod Security admission, is namespace-label-based. The controls that really apply are `ADMIN_NODE_DEBUG_ENABLED` (off by default) and the `pod-security.kubernetes.io/enforce` label on `ADMIN_NODE_DEBUG_NAMESPACE`. Withholding this rule disables node debug pods, CLI pods *and* object creation from the editor and from the `Create Pod…` button on the Pods listing. §15 adds a second thing RBAC cannot express here: there is no verb covering which **ServiceAccount** a pod may bind, so this grant lets the console create a pod bound to an account more privileged than the console itself — and a shell in that pod then holds that account's permissions. `ADMIN_CLI_ENABLED` and `ADMIN_CLI_SERVICE_ACCOUNT` are the only controls over it, and neither lives in RBAC |

### §20 — expanding a persistent volume claim

| Permission | Feature | Withheld |
|---|---|---|
| `patch persistentvolumeclaims` | §20 the expansion itself | The `Expand…` action is disabled with the reason, and the preflight refuses before anything is sent. `ADMIN_ALLOW_MUTATIONS` off does the same thing for a different reason, and §1.3 keeps the two apart: `mutations_disabled` says the deployment is read-only, `rbac_denied` says this service account is not permitted |
| `get storageclasses` | §20's `allowVolumeExpansion` check | **`expansion.supported` is `null`, not `false`.** Withholding this does not block the write: reading a refused class as "expansion forbidden" would refuse what the cluster would have accepted and send an operator to fix a StorageClass that is already correct. It becomes `pvc_expansion_unknown`, acknowledged by name, and the API server decides. Already granted by the reader role for §8's StorageClass tab |
| `list pods` in the claim's namespace | §20's `mountedBy` | **`null`, never `[]`.** "Nothing has this volume mounted" is the sentence that starts an offline resize on a volume a database has open, so a refused listing says so — `partial: true`, an `unavailable[]` entry, and `pvc_mounts_unknown` in the consequences. Already granted by the reader role for §6 |

**§20 adds no permission to the shipped roles.** All three are already there:
`patch persistentvolumeclaims` comes with the §4/§8 config-and-storage write
rule, and both reads belong to the reader role for §8 and §6. A console that can
edit a claim's YAML today can already grow it — §20 is the path that checks
first and says what a green result means.

### §30 — granting and revoking a role

| Verb | Group / resource | Withholding it |
|---|---|---|
| `list` | `rbac.authorization.k8s.io/rolebindings` | The plan cannot run at all. Every branch — which binding to write, whether the subject is already named, what else still grants access — is computed from this listing, so it raises rather than producing a plan built on an empty one |
| `get` | `rbac.authorization.k8s.io/rolebindings` | The one binding being written cannot be re-read, so the write loses its `before` and its fresh `resourceVersion` |
| `get` | `rbac.authorization.k8s.io/roles`, `…/clusterroles` | The role reads as **`unreadable`**: `rule_count` is `null`, no capability is reported, and the grant carries its own acknowledgement saying what it confers was not established. It is never reported as granting nothing |
| `list` | `rbac.authorization.k8s.io/clusterrolebindings` | A revoke's `residual.cluster_bindings` is `null` and the endpoint is `partial`. The plan says whether the subject keeps access cluster-wide is **unknown** — never that nothing else grants it |
| `create` | `rbac.authorization.k8s.io/rolebindings` | A first grant is refused. Adding a subject to a binding that already exists still works: they are different permissions and §30 preflights the one it is about to use |
| `patch` | `rbac.authorization.k8s.io/rolebindings` | Adding to or removing from an existing binding is refused. Creating a new one still works |

The API server additionally requires that the caller **hold the permissions being
granted**, or hold `escalate`/`bind` on the role. That check is the API server's
and is not preflighted: it is per-role and per-caller, and the refusal it
produces arrives as `403 rbac_denied` naming the write.

### §4 — creating an object from a listing

Every listing in the console offers `Create <Kind>…` (rule 11.10), so the
`create` verb is now asked about on **every resource the cluster serves**, not
only the ones with a typed page. Nothing new is granted by that: the button is
one preflight and one `POST` through the same funnel, and the shipped roles are
unchanged. What changes is where a withheld grant becomes visible — a disabled
button on the listing rather than a `403` after somebody pasted a manifest.

| Permission | Feature | Withheld |
|---|---|---|
| `create ""/configmaps,secrets,serviceaccounts,services,persistentvolumeclaims,resourcequotas,limitranges` | The Create button on Configuration's, Storage's and Access's core tabs | Each button is disabled with the reason (§11.4) and its tab stays readable. Granted per resource, not per page: a console that may create ConfigMaps and not Secrets shows exactly that, one button at a time |
| `create networking.k8s.io/ingresses,ingressclasses` | Network's Ingress and Ingress Class buttons | Disabled with the reason. §13's exposures already need the Ingress verb, which is the row above this section |
| `create rbac.authorization.k8s.io/roles,rolebindings,clusterroles,clusterrolebindings` | The Access page's four Create buttons | Disabled with the reason. **Read this one before granting it**: `create rolebindings` lets whoever holds it bind anything this ServiceAccount holds, which is why §30 names the powers a binding confers on screen. The API server additionally refuses a role granting more than the caller holds — enforced at admission, invisible to a `SelfSubjectAccessReview`, and the same escalation-prevention refusal §14 rewrites the hint for |
| `create apps/deployments,statefulsets,daemonsets,replicasets`, `create batch/jobs,cronjobs` | `Create Deployment…` and its five siblings on the Workloads page | Disabled with the reason; every read on that page is unaffected. These are **not** in the shipped writer role: it carries `patch` for scale, restart and rollback and no `create`, so a deployment that has never created a workload from the console keeps working exactly as before and the buttons say why |
| `create` on any other group/resource, **including every CRD** | The Create button on Custom Resources, on the API explorer, and on any tab for a kind with no typed page | Disabled with the reason naming the exact group/resource. This is the expected steady state on the shipped roles — see the next section, which is now the section that explains most disabled Create buttons in the console |

**Two refusals that are not this table.** A resource whose discovery `verbs` do
not include `create` disables the button as `unsupported` — a fact about the API
rather than about a grant, and widening a role does not change it. A console
with `ADMIN_ALLOW_MUTATIONS=false` disables every Create button as
`mutations_disabled`. Neither is `rbac_denied`, and printing this table's
degradation for either sends an operator to edit a role that was already
correct.

### The generic write is deliberately not granted

§4 lets the resource browser *display* everything the cluster serves, and every
one of those listings now offers to create into it. The shipped writer role
covers write only on the resources the console has typed pages for, so on an
unmodified deployment **most Create buttons in the console are disabled, each
naming the exact verb/group/resource it would need** — a sentence an operator
can act on, and the honest picture of what this role grants. Creating or editing
anything outside that set returns `403 rbac_denied` with the same sentence.

That is the design, not a gap: a create affordance on every listing is a
navigation decision and grants nothing. The permission boundary is this file.

`deploy/rbac.yaml` carries a commented `apiGroups: ["*"] / resources: ["*"]`
block for people who need the editor to reach everything. It is cluster-admin
wearing a different name: it grants write over ValidatingWebhookConfigurations,
over RBAC itself, and over every CRD any operator has installed — and it makes
the console's ServiceAccount a privilege-escalation path for anyone who can reach
its port. Uncomment it knowing that.

---

## Baseline check at registration

`POST /api/clusters/{id}/test` preflights these seventeen, so a
half-permissioned ServiceAccount shows up on the registration form rather than at
03:00 during an incident:

```
list   core/pods            core/services      core/namespaces
       core/nodes           core/configmaps    core/events
       apps/deployments     apps/statefulsets  apps/daemonsets
       batch/jobs           batch/cronjobs
       networking.k8s.io/ingresses
       rbac.authorization.k8s.io/roles
patch  apps/deployments
create core/pods/exec
patch core/pods/ephemeralcontainers
create core/pods
delete core/pods
get    core/secrets
```

A cluster that lists everything and cannot patch a Deployment is a perfectly
valid read-only registration. The point of checking is to say so at
registration, not to refuse it.

---

## Verifying by hand

```bash
# As the console's ServiceAccount, ask the same question preflight asks.
kubectl auth can-i list pods --all-namespaces \
  --as=system:serviceaccount:k8boss-admin:k8boss-admin

kubectl auth can-i patch deployments.apps -n prod \
  --as=system:serviceaccount:k8boss-admin:k8boss-admin

# Subresources use a slash, and they are separate resources:
kubectl auth can-i create pods/exec -n prod \
  --as=system:serviceaccount:k8boss-admin:k8boss-admin
kubectl auth can-i patch deployments.apps/scale -n prod \
  --as=system:serviceaccount:k8boss-admin:k8boss-admin
```

Or ask the console, which asks the cluster the same way:

```bash
curl -s 'localhost:8020/api/access/preflight?verb=patch&group=apps&resource=deployments&namespace=prod'
```

Note the `group=core` spelling for the core API group: its real name is the empty
string, which cannot appear in a URL path segment, so `core` is the wire spelling
and the backend translates it at the edge.
