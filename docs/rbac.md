# RBAC reference

Every Kubernetes permission the console uses, grouped by the feature that needs
it, with **what happens if you withhold it**.

That last column is the point of this document. Least privilege is only
practical when the cost of each grant is known, and the honest cost is rarely
"the console breaks". It is usually one column of one table becoming `—`, with
an `unavailable[]` entry saying `forbidden` and a banner naming it. Read the
degradation, then decide.

The shipped roles are in [`deploy/rbac.yaml`](../deploy/rbac.yaml):
`k8boss-admin-reader` (bound by default) and `k8boss-admin-writer` (defined, and
its binding commented out).

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

## Read: workloads

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch apps/deployments,statefulsets,daemonsets,replicasets` | §6 workload rows and detail | Those kinds vanish from the unified list — with an `unavailable` entry per refused kind, so the list says which kinds it could not read rather than looking like a cluster with fewer workloads |
| `get,list,watch batch/jobs,cronjobs` | §6 Jobs and CronJobs, including `suspended` / `schedule` / `last_schedule` | Same shape: named in `unavailable`, absent from the rows |
| `get,list,watch apps/controllerrevisions` | §6 rollout history for StatefulSets and DaemonSets | The rollout panel for those two kinds reports that it could not read history. It must **not** be rendered as "this workload has never been rolled out" — that is a different fact. Deployments are unaffected: their history lives in ReplicaSets |
| `get,list,watch ""/services` | §6 workload detail's related Services; §8 Services | The `services` block of a workload detail is empty with an `unavailable` entry |

## Read: pod logs

| Permission | Feature | Withheld |
|---|---|---|
| `get ""/pods/log` | §7 `GET /api/pods/{ns}/{name}/logs` and the log WebSocket | The log viewer shows a `forbidden` error frame. **This is a separate RBAC resource from `pods`** — granting `pods` alone gives a console that lists pods and cannot show one line of their output, which is the state most operators notice first |

## Read: config, storage, access

| Permission | Feature | Withheld |
|---|---|---|
| `get,list,watch ""/configmaps` | §8 ConfigMaps | That tab shows one `forbidden` entry instead of a table |
| `get,list,watch ""/secrets` | §8 Secrets (key names and byte lengths only) | Secrets tab shows `forbidden`. **See the warning below before granting this** |
| `get,list,watch ""/serviceaccounts` | §8 ServiceAccounts | That tab shows `forbidden` |
| `get,list,watch ""/persistentvolumeclaims,persistentvolumes` | §8 storage | Those tabs show `forbidden` |
| `get,list,watch storage.k8s.io/storageclasses` | §8 StorageClasses, including `is_default` | That tab shows `forbidden`; PVC rows still render, with `storage_class` as the name only |
| `get,list,watch rbac.authorization.k8s.io/roles,rolebindings,clusterroles,clusterrolebindings` | §8 Access page | Access page shows `forbidden`. Reading RBAC is not the same privilege as holding it, but it is a map of the cluster's permissions — a reasonable thing to withhold |
| `get,list,watch networking.k8s.io/ingresses` | §8 Ingresses | Ingresses tab shows `forbidden` |
| `get,list,watch networking.k8s.io/ingressclasses` | The `class` column for an Ingress whose spec names none | The column is blank rather than wrong |
| `get,list,watch discovery.k8s.io/endpointslices` | §8 Services' `endpoint_count` | The column is **`null`**, with an `unavailable` entry — not `0`, which would claim the Service backs nothing. A Service that genuinely has no EndpointSlice still reports `0`, which is a real zero |
| `get,list,watch ""/endpoints` | The Network page's endpoint listing | That table shows `forbidden` |

> ### The Secret rule is the most privileged line in the shipped role
>
> `list secrets` cluster-wide means the console's ServiceAccount can read the
> value of every Secret in the cluster. The console never puts a value in a list
> response, and the single-object read redacts unless three separate conditions
> hold — but **RBAC grants the API access, not the console's discipline about
> it**. Anyone who can exec into the console's pod holds this grant directly.
>
> Deleting the rule costs exactly one tab. It is a reasonable default to remove.

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

---

## Write

Everything below is in `k8boss-admin-writer`, whose binding is commented out in
`deploy/rbac.yaml`. Withholding **all** of it gives a fully functional read-only
console — which is the default install, not a degraded one.

Remember that **dry-run needs the same verb as the real write**: the API server
requires `patch` to project a patch. A console that only previews still needs
these grants.

| Permission | Feature | Withheld |
|---|---|---|
| `get,patch,update apps/deployments/scale`, `statefulsets/scale`, `replicasets/scale` | §6 scale | The Scale button is disabled with the reason. **A separate RBAC resource from the workload itself** — which is the whole reason the console patches the subresource: a ServiceAccount can be allowed to resize a workload without being allowed to edit it. Grant this pair alone for a resize-only console |
| `patch apps/deployments,statefulsets,daemonsets` | §6 restart (a pod-template annotation, the `kubectl rollout restart` mechanism) and §6 rollback (an RFC 6902 replace of `/spec/template`) | Restart and Rollback disabled with the reason |
| `patch batch/jobs,cronjobs` | §6 suspend | Suspend disabled with the reason. Only Jobs and CronJobs have `spec.suspend`; other kinds are refused at `422 invalid` naming the kind, before RBAC is consulted at all |
| `create,update,patch,delete` on the §4/§8 resources | The YAML editor and the delete button on those pages | Those actions are disabled with the reason; the editor still opens read-only and the diff still renders |
| `patch,update ""/nodes` | §5 cordon and drain | Cordon and Drain disabled with the reason. Note this grants cordon of **any** node, control-plane included |
| `create ""/pods/eviction` | §5 drain, pod half | The drain refuses at its own preflight, from inside the apply step — because permission to cordon a node is not permission to evict what runs on it, and finding that out three pods into a drain is not a discovery anyone wants. The console never falls back to deleting pods: delete ignores PodDisruptionBudgets |
| `delete ""/pods` | The resource browser's pod delete | That one button is disabled. Distinct from eviction: a different action with a different confirm dialog |
| `create ""/pods/exec` | §7 terminal | The terminal button is disabled with the reason. **This is the most dangerous grant in the file after a wildcard**: a shell in a pod can do whatever that pod's own ServiceAccount can, no diff is possible for a keystroke, and it bypasses every other control here. Withholding it is the only real control over it. The console additionally requires `ADMIN_ALLOW_MUTATIONS` for exec and audits the session on open and close |
| `get,patch ""/pods/ephemeralcontainers` | §7.4 debug containers — `kubectl debug`'s ephemeral container | The Attach button is disabled with the reason; existing debug containers are still *listed*, because that listing reads the pod and needs only `get pods`. **A separate RBAC resource from `pods`**, and a separate grant from `pods/exec`: attaching a container and typing in one are different acts. It is nonetheless close to `pods/exec` in blast radius — the operator chooses the image, and a debug container shares the pod's network namespace, its volumes and (on request) the process namespace of an application container. `get` is needed as well as `patch` because the console reads the subresource to build the diff it shows before writing. Withhold it for a console that can exec into what is there but cannot add to it |

### The generic write is deliberately not granted

§4 lets the resource browser *display* everything the cluster serves. The shipped
writer role covers write only on the resources the console has typed pages for.
Editing anything outside that set returns `403 rbac_denied` naming the exact
verb/group/resource — a sentence an operator can act on.

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
