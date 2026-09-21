# API contract — k8boss-admin

**Normative.** Backend modules implement it; the frontend consumes it. Where this
document and code disagree, this document is the defect report.

Base path is `/api`. Every response is JSON unless stated otherwise. All
timestamps are RFC 3339 UTC strings (`2026-08-18T09:14:00Z`) or `null`.

---

## 0. The five rules every endpoint obeys

These come from the k8boss defect standard — *a wrong answer delivered
confidently is worse than no answer* — restated for a console that also
**writes** to clusters.

1. **Empty is never blind.** A list endpoint that could not read something says
   so. `items: []` means "the cluster has none". Not being able to look is
   reported in `unavailable[]` with `partial: true`. No endpoint may silently
   swallow an error into an empty array.
2. **Every mutation is preflighted.** Before any write, the backend issues a
   `SelfSubjectAccessReview` for the exact `verb/group/resource/namespace/name`.
   A denial returns `403 rbac_denied` naming the missing permission — never a
   bare "forbidden" relayed from the API server.
3. **Every mutation can be dry-run, and the destructive ones must be.** Writes
   accept `dryRun`. With `dryRun: true` the backend sends `dryRun=All` to the API
   server and returns the server's own projected object plus a unified diff. The
   UI shows that diff before the confirming call.
4. **Optimistic concurrency is mandatory on update.** `PUT` carries the
   `resourceVersion` the user was looking at. A mismatch is `409 conflict` with
   a fresh diff, never a blind overwrite.
5. **Every write is audited** — actor, cluster, verb, target, dry-run digest,
   outcome — including the writes that failed.

## 1. Cross-cutting shapes

### 1.1 Cluster scoping

Every endpoint below is scoped by `?cluster_id=<int>`, read by
`ClusterContextMiddleware` into a `contextvars` context. Handlers never take
`cluster_id` as an argument. Omitted means "the active cluster"; if none is
registered, endpoints return `409 no_cluster_selected`.

### 1.2 List envelope

Every collection endpoint returns exactly this shape:

```json
{
  "items": [ /* resource rows, shape per section */ ],
  "continue": "eyJ2IjoibWV0YS5rOHMuaW8vdjEi...",
  "remaining": 412,
  "partial": false,
  "unavailable": [
    {
      "group": "",
      "resource": "secrets",
      "namespace": "prod",
      "reason": "forbidden",
      "detail": "secrets is forbidden: User \"system:serviceaccount:k8boss:admin\" cannot list resource \"secrets\""
    }
  ]
}
```

- `continue` / `remaining` — passthrough of the Kubernetes chunked-list cursor.
  `null` when the listing is complete.
- `partial` — **true whenever `unavailable` is non-empty.** Invariant, asserted
  in tests.
- `reason` — one of `forbidden`, `not_found`, `unreachable`, `timeout`,
  `not_registered`, `unsupported`, `unrenderable`. Callers branch on this;
  `detail` is for humans only and must never be parsed.

**Every one of those tokens answers "we could not look, and here is why", and an
error that does not answer that question is never given one.** The map from
§1.3's codes to this vocabulary is exhaustive and has no fallback: a code that
is not in it — `invalid` and `conflict`, and §12's console-authentication codes
— makes the read **propagate** rather than degrade. A 422 from a secondary read
says the cluster answered and the request this console built was wrong;
recording that as `unreachable` hides a defect in this process behind a sentence
about somebody's network, which is where a defect of that shape lives forever.
The same rule `collect()` already applied to a `TypeError` from a shaper.

`unsupported` is the "this cluster does not have that API" case (no Ingress
controller CRDs, no `metrics.k8s.io`). It is not an error and does not colour a
row red — the UI renders it as "not present on this cluster".

`unrenderable` is the read that was **never attempted**, because this console
could not build the query that would answer it. §6's workload detail is where it
arises: a `matchExpressions` operator it cannot express as a `labelSelector`, or
a selector that is empty and therefore matches every pod in the namespace. It is
a limit of the console, not of the cluster — so unlike `unsupported` the section
is genuinely unknown, and `detail` says which of the two it was because they
need different things done about them.

### 1.3 Error envelope

Non-2xx bodies:

```json
{
  "error": "rbac_denied",
  "message": "Cannot patch deployments in namespace \"prod\".",
  "detail": "...verbatim API server message, may be absent...",
  "hint": "Grant the console's ServiceAccount `patch` on `apps/deployments`.",
  "context": { "verb": "patch", "group": "apps", "resource": "deployments", "namespace": "prod", "name": "checkout" }
}
```

Stable `error` codes:

| code | HTTP | meaning |
|---|---|---|
| `no_cluster_selected` | 409 | no `cluster_id` and no registered active cluster |
| `cluster_unreachable` | 502 | DNS/TLS/connection failure reaching the API server |
| `rbac_denied` | 403 | preflight said no, or the API server said no |
| `not_found` | 404 | object or API resource does not exist |
| `conflict` | 409 | `resourceVersion` mismatch, or the API server rejected the write as conflicting |
| `invalid` | 422 | submitted YAML failed schema/admission validation |
| `authentication_required` | 401 | no valid application session was presented |
| `invalid_credentials` | 401 | login was rejected without revealing whether the username exists |
| `permission_denied` | 403 | the authenticated console role does not permit the action |
| `too_many_attempts` | 429 | the sign-in budget for this username is spent; `context.retryAfterSeconds` carries the wait |
| `identity_provider_unavailable` | 502 | LDAP or the OIDC issuer could not answer, or its secure transport is misconfigured |
| `mutations_disabled` | 403 | the deployment is running read-only |
| `unsupported` | 501 | the API resource is not served by this cluster |
| `upstream_error` | 502 | any other API server failure |
| `internal_error` | 500 | the console itself failed and nothing mapped the cause. Distinct from `upstream_error`, which blames the cluster; the message is fixed and carries no exception text, and the detail is in the log beside the request's correlation id |

### 1.4 Group-version-resource in URLs

The core API group's real name is the empty string, which cannot appear in a
path segment. **The wire spelling of the core group is the literal `core`.**
Backend translates `core` → `""` at the edge; nothing downstream sees `core`.

```
/api/resources/core/v1/pods
/api/resources/apps/v1/deployments
/api/resources/networking.k8s.io/v1/networkpolicies
```

Resource segment is always the **plural** name as reported by discovery.

### 1.5 Mutation request/response

Every write body accepts `dryRun` (bool, default **true**). Every write response:

```json
{
  "dryRun": true,
  "applied": false,
  "verb": "patch",
  "target": { "group": "apps", "version": "v1", "resource": "deployments", "namespace": "prod", "name": "checkout" },
  "diff": {
    "before": "apiVersion: apps/v1\n...",
    "after":  "apiVersion: apps/v1\n...",
    "unified": "--- live\n+++ proposed\n@@ -12,7 +12,7 @@\n-  replicas: 3\n+  replicas: 5\n",
    "changed": true
  },
  "resourceVersion": "884213",
  "warnings": ["metadata.annotations[kubectl.kubernetes.io/last-applied-configuration] will be replaced"],
  "auditId": 4471
}
```

- `applied` is `true` only when the write actually reached the cluster
  (`dryRun: false` **and** it succeeded). A dry-run that succeeds is
  `applied: false`, and the UI must not report success from it.
- `diff.changed: false` on a dry-run means the write is a no-op. The UI offers
  "nothing would change" rather than a confirm button.
- `warnings` carries the API server's `Warning:` headers verbatim.
- **A Secret's diff never carries a value.** For `""/secrets`, every `data`
  entry on both sides is rendered as `<redacted, N bytes>` — the key and its
  decoded size — with `, changed` appended on the `proposed` side when the value
  differs from the live one, so `changed` still reports a real edit. A key that
  is added or removed shows as a hunk; one whose value is unchanged produces
  none. `stringData` in a submitted manifest is redacted by plaintext length.
  This holds for a delete's `diff.before`, a replace, and the fresh diff on a
  `409`, and the audit digest is over the redacted text. §8's reveal gate is the
  only path that returns a value.

### 1.6 Read-only mode

`ADMIN_ALLOW_MUTATIONS` (env, default **false**) gates every write. When false,
writes return `403 mutations_disabled` **before** touching the cluster, and
`GET /api/health` reports `"mutations": "disabled"` so the UI hides the buttons
instead of offering them and failing.

Dry-run is still permitted in read-only mode — inspecting what *would* change is
a read. This is deliberate: it makes the console useful in an audit posture.

Four features add a switch of their own on top of this one —
`ADMIN_NODE_DEBUG_ENABLED` (§5.5), `ADMIN_CLI_ENABLED` (§15),
`ADMIN_ROUTER_MANAGE_ENABLED` (§14) and `ADMIN_PORTAL_INSTALL_ENABLED` (§16) —
so a deployment can withhold one action while every other write stays available.
All of them are enforced at the same step, by the same function, and every
refusal is `403 mutations_disabled` audited as `outcome: "denied"` against the
object the write would have touched. A refusal names the **first** closed switch:
`ADMIN_ALLOW_MUTATIONS` when that is off, because it is the one to change first.

**Two of those switches withhold the dry run as well**, and they are the
exception rather than the rule: §5.5's projected pod manifest is a working recipe
for a privileged pod and §15's is the offer of a kubectl terminal, so previewing
either is offering the feature. Everywhere else a closed switch still projects,
because an operator deciding whether to open one has to be able to read what it
would let the console write.

---

## 2. Health and capability

### `GET /api/health`

```json
{
  "status": "ok",
  "version": "0.1.0",
  "mutations": "enabled",
  "clusters": { "registered": 3, "reachable": 2 },
  "degraded": [ { "component": "cluster:2", "reason": "unreachable", "detail": "..." } ]
}
```

`status` is `ok` or `degraded`. Never `error` — the console being up is the
thing this endpoint reports. A cluster being down belongs in `degraded[]`.

---

## 3. Clusters

Registration mirrors k8boss: an API server endpoint plus a bearer token, stored
encrypted, never returned.

### `GET /api/clusters` → `{"items": [ClusterPublic], ...envelope}`

```json
{
  "id": 1,
  "name": "prod-eu",
  "platform": "kubernetes",
  "api_server": "https://api.prod-eu.example:6443",
  "authentication_type": "service_account_token",
  "has_ca_certificate": true,
  "skip_tls_verify": false,
  "impersonation_enabled": false,
  "app_domain": "apps.prod-eu.example.com",
  "status": "connected",
  "server_version": "v1.31.4",
  "last_connected": "2026-08-18T09:03:11Z",
  "created_at": "2026-06-02T10:00:00Z",
  "updated_at": "2026-08-01T12:41:09Z"
}
```

No field of this object ever contains credential material. Enforced by a test.

`app_domain` is the cluster's wildcard DNS domain, and §13.3.1 builds generated
exposure hostnames under it. **`null` is a real value and means the console does
not know of one**, which is not the same as the cluster having none — both
render as "no hostname is generated", because a hostname under a wildcard that
does not exist is an exposure that is created, reports Admitted, and routes
nothing.

Unlike `token` and `ca_certificate` it is not credential material, so it *is*
returned here and a UI can prefill it. Sending `""` on `PUT` clears it; omitting
it keeps the stored value. That asymmetry is deliberate — a domain typed wrongly
has to be removable, not only overwritable.

**The three writes below require the `admin` console role** (§12.6) when
`AUTH_ENABLED=true`; a `user` identity gets `403`. Outside §12.6's own surface
they and §10.4's audit export are the only endpoints that ask about a console
role at all — everything else answers to Kubernetes preflight and the mutation
gate. Under `AUTH_ENABLED=false` there is no console role to check, so they are
as open as everything else in that mode and the authenticating proxy in front
owns the decision — the premise of legacy proxy mode, applied here rather than
excepted here.

### `POST /api/clusters`

```json
{ "name": "prod-eu", "platform": "kubernetes", "api_server": "https://...",
  "authentication_type": "service_account_token", "token": "eyJ...",
  "ca_certificate": "-----BEGIN CERTIFICATE-----\n...", "skip_tls_verify": false,
  "app_domain": "apps.prod-eu.example.com" }
```
→ `201` `ClusterPublic`.

### `PUT /api/clusters/{id}` — partial; omitted `token` keeps the stored one.

That last clause is why this one is an administrator act rather than an edit.
Moving `api_server` while omitting `token` re-points the *stored* credential at
a new host, so a caller who can send this body has the console hand that
cluster's bearer token to an address they chose, on the next request, with
nothing on the wire that looks like a credential being read.

### `DELETE /api/clusters/{id}` → `204`.
### `POST /api/clusters/{id}/test`

Performs a live connection check. Returns
`{"reachable": true, "server_version": "v1.31.4", "latency_ms": 42, "permissions": [PreflightResult], "discovered_app_domain": "apps.ocp.example.com"}`
where `permissions` preflights the console's baseline verb set (see §9) so
registration surfaces a half-permissioned ServiceAccount immediately rather than
at first use.

`discovered_app_domain` is what the cluster publishes about *itself* — OpenShift
at `ingresses.config.openshift.io/cluster`, `null` everywhere else. **Offered,
never applied**: it is a suggestion beside the field, and the stored
`app_domain` always wins. Exposing under a CNAME of the cluster wildcard is
ordinary, and a console that re-corrected it on every connection test would
overwrite a deliberate choice with a discovered default.

### `GET /api/clusters/{id}/overview`

```json
{
  "server_version": "v1.31.4",
  "platform": "kubernetes",
  "nodes": { "total": 12, "ready": 11, "unschedulable": 1 },
  "namespaces": 34,
  "workloads": { "deployments": 210, "statefulsets": 12, "daemonsets": 8, "jobs": 41, "cronjobs": 9 },
  "pods": { "total": 1204, "running": 1180, "pending": 8, "failed": 3, "succeeded": 13 },
  "capacity": { "cpu_cores": 192, "memory_bytes": 824633720832, "pods": 1320 },
  "requested": { "cpu_cores": 121.5, "memory_bytes": 512000000000 },
  "unavailable": []
}
```

Each sub-object is collected independently; a failure degrades **that key only**
(set to `null`) and appends to `unavailable`. The page must never 500 because
one collector failed.

**`requested` is §5's algorithm, not a second one.** It is the same function the
nodes page uses, over every pod in the cluster instead of one node's: regular
containers, plus sidecars (init containers with `restartPolicy: Always`, which
run for the pod's whole life), plus the peak of the ordinary init containers,
plus `spec.overhead`, and terminated pods skipped. Summing `spec.containers`
alone is the tempting simplification and it undercounts every pod in a service
mesh — two pages reporting two different numbers for one quantity, with this one
low, which is the direction that reads as headroom. A quantity that will not
parse makes **its dimension** `null` rather than dropping out of the sum, for
the same reason; the pod phase counts beside it are unaffected.

---

## 4. Generic resource access

The console browses **any** resource the cluster serves, via the dynamic client.
Typed endpoints (§5–§8) exist for the resources that need shaped rows; anything
else is reachable here.

### `GET /api/resources/catalog`

```json
{
  "items": [
    { "group": "apps", "version": "v1", "kind": "Deployment", "resource": "deployments",
      "namespaced": true, "verbs": ["get","list","watch","create","update","patch","delete"],
      "shortNames": ["deploy"], "categories": ["all"] }
  ],
  "partial": false,
  "unavailable": []
}
```

A partially-broken discovery (an aggregated APIService that is down) populates
`unavailable` with `reason: "unreachable"` and keeps the groups that answered.
This is the canonical *"say which question you failed to answer"* case: a missing
group must not look like a cluster with fewer resources.

### `GET /api/resources/{group}/{version}/{plural}`

Query: `namespace`, `labelSelector`, `fieldSelector`, `limit` (default 500),
`continue`.

Items are **trimmed** objects, not raw manifests — `managedFields` is dropped
always, and `metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"]`
is dropped from list responses (kept in the single-object read):

```json
{ "apiVersion": "apps/v1", "kind": "Deployment",
  "metadata": {"name": "checkout", "namespace": "prod", "uid": "...", "resourceVersion": "884213",
               "creationTimestamp": "2026-05-01T08:00:00Z", "labels": {...}, "annotations": {...}},
  "spec": {...}, "status": {...} }
```

### `GET /api/resources/{group}/{version}/{plural}/{name}?namespace=`
→ the full object (still without `managedFields`).

### `GET /api/resources/{group}/{version}/{plural}/{name}/yaml?namespace=`
→ `text/plain` YAML, ready for the editor. Same trimming.

### `POST /api/resources/{group}/{version}/{plural}`
Body `{"yaml": "...", "namespace": "prod", "dryRun": true}` → mutation response
(§1.5). `diff.before` is `null` and the unified diff is the whole projected
object as an addition, because that is what a create is.

**This is the only create path in the console, and every resource listing
reaches it.** Each listing offers `Create <Kind>…` for the kind discovery names
behind it, CRD-backed kinds included (§11.10), and that dialog posts the body
above — the same body as the masthead's blank import. The create dialog has a
form view (§11.9) and it does not change this body: the form edits the parsed
document and the document is re-serialised into `yaml`, so there is no second
mode here and no form model for this endpoint to reassemble. A create screen
that posted a structured spec instead would be a second write path for the same
object, and the first one to stop matching would do it silently.

**`create` is a property of the resource before it is a property of the
caller.** A resource whose catalog `verbs` do not list `create` is
`501 unsupported`, naming the verbs discovery does advertise — never `403`. The
API server would answer 405 and §1.3 would map that to `unsupported` anyway;
refusing here makes the sentence actionable ("bindings can be created, not
deleted") where "method not allowed" is not. A resource advertising **no** verbs
at all is not second-guessed: some aggregated APIs report an empty list and
serve the verb.

**The document's namespace wins; the request's `namespace` is the fallback**,
and `target.namespace` in the response is the one that was used. Namespace is
the one field a create's diff does not make obvious — there is no `before`, so
the whole object renders as an addition and the reader's eye goes to the spec,
not to `metadata.namespace` in a wall of green. A namespaced resource with
neither is `422 invalid`. A cluster-scoped one ignores the request's
`namespace`, which is only ever the scope the browser happened to be filtered
to; a `metadata.namespace` inside the document is `422 invalid` rather than
dropped, because the API server drops it silently and the operator goes on
believing they created something namespaced.

**A document with neither `metadata.name` nor `metadata.generateName` is
`422 invalid`**, naming both, before the cluster is touched.

**One document per request.** A body carrying several YAML documents is
`422 invalid` and is never split into N requests: a create that lands three
objects and fails the fourth, reported as one result, is exactly the
confidently wrong answer §0 exists to rule out.

### `PUT /api/resources/{group}/{version}/{plural}/{name}`
Body `{"yaml": "...", "namespace": "prod", "resourceVersion": "884213", "dryRun": true}`
→ mutation response. A `resourceVersion` that no longer matches is `409 conflict`
with `context.currentResourceVersion` and a fresh `diff` against live.

### `DELETE /api/resources/{group}/{version}/{plural}/{name}`
Query `namespace`, `propagationPolicy` (`Background`|`Foreground`|`Orphan`,
default `Background`), `dryRun` (default **true**).

A delete dry-run returns `diff.before` = the live object and `diff.after` = `null`
with `changed: true`, so the confirm dialog can show exactly what disappears. For
a Secret, "the live object" is the redacted rendering §1.5 describes: the keys
and their sizes, never a value.

---

## 5. Namespaces, nodes, events

### `GET /api/namespaces`
Row: `{ "name", "status", "labels", "annotations", "age_seconds", "pod_count", "creationTimestamp" }`.
`pod_count` is `null` (not `0`) when pods could not be listed.

### `GET /api/nodes`
Row:
```json
{ "name": "ip-10-0-1-4", "ready": true, "unschedulable": false,
  "roles": ["worker"], "kubelet_version": "v1.31.4", "os_image": "...", "container_runtime": "...",
  "internal_ip": "10.0.1.4", "age_seconds": 8123456,
  "capacity": {"cpu_cores": 16, "memory_bytes": 68719476736, "pods": 110},
  "allocatable": {"cpu_cores": 15.8, "memory_bytes": 66571993088, "pods": 110},
  "requested": {"cpu_cores": 9.2, "memory_bytes": 34359738368},
  "pod_count": 62,
  "conditions": [{"type": "MemoryPressure", "status": "False", "reason": "KubeletHasSufficientMemory"}],
  "taints": [{"key": "node-role", "value": "infra", "effect": "NoSchedule"}] }
```
`requested`/`pod_count` derive from listing pods with `spec.nodeName=<node>`. If
that listing fails they are `null` and the envelope reports `unavailable` —
never `0`, which would read as an idle node.

### `GET /api/nodes/{name}` → the row above plus `{"pods": [PodRow]}`.

### `POST /api/nodes/{name}/cordon` — `{"unschedulable": true, "dryRun": true}` → mutation response.

### `POST /api/nodes/{name}/drain`
`{"dryRun": true, "gracePeriodSeconds": 30, "ignoreDaemonSets": true, "deleteEmptyDirData": false, "force": false}`

Drain is the one multi-step action. Response extends the mutation envelope:

```json
{ "dryRun": true, "applied": false,
  "plan": [ {"namespace":"prod","pod":"checkout-7d9-abc","action":"evict"},
            {"namespace":"kube-system","pod":"cilium-x4k2","action":"skip","reason":"daemonset"},
            {"namespace":"data","pod":"pg-0","action":"blocked","reason":"emptyDir volume, deleteEmptyDirData=false"} ],
  "blocked": 1 }
```

`blocked > 0` with `force: false` refuses to execute (`422 invalid`) — the plan
tells the operator precisely what to resolve. A drain that half-completes
returns `207`-style detail: `applied: true` with per-pod `result` fields, because
"drain succeeded" when three pods failed to evict is exactly the confident wrong
answer this project exists to avoid.

Every entry also carries `pdb` — the PodDisruptionBudget that covers this pod, or
`null` — and `pdbUnknown[]`, the budgets whose selector this console could not
evaluate, so whether they cover the pod is **unknown rather than absent** (§28.5).
`pdbUnknown` is never merged into `pdb`, and it is deliberately not a blocker: the
eviction subresource is the enforcer, and refusing a drain over a selector we
merely could not parse is how `force` becomes reflex. The plan states the doubt
and the operator decides. `pdb_checked: false` is the coarser version of the same
admission — the budget listing itself failed, so no entry's `pdb` means anything.

### 5.5 Node debug pods

`kubectl debug node/<name>`: a pod pinned to one machine with its filesystem
mounted, for when the node itself is what needs looking at and there is no way
to SSH to it. **The most privileged object this API creates.**

#### `GET /api/nodes/{name}/debug`
A §1.2 envelope of the debug pods this console created *for this node*, plus:
```json
{ "items": [ {"name","namespace","node","image","phase","state","reason",
              "started_at","created_at","hostFilesystemReadOnly"} ],
  "continue": null, "remaining": null, "partial": false, "unavailable": [],
  "enabled": true,
  "enabledDetail": "Node debug pods are enabled on this deployment.",
  "namespace": "default" }
```
- `enabled` is this deployment's **two** gates answered together, so a client can
  disable the action with the reason rather than offering it and taking a 403.
- `namespace` is where a new pod would be created (`ADMIN_NODE_DEBUG_NAMESPACE`).
  A client must not guess it: it is part of what the operator is confirming.
- `hostFilesystemReadOnly` is `null` for a pod carrying this console's label but
  no host mount it recognises — not `true`, which would be a safety claim about
  an object we do not understand.
- The listing is a **read** and answers even when creating is gated off. That is
  what makes rule 11.4 possible here, and a pod left behind after the gate was
  switched off is the one that most needs finding.

#### `POST /api/nodes/{name}/debug`
`{"image": null, "writableHostFilesystem": false, "dryRun": true}` → the §1.5
mutation response plus `pod`, `namespace`, `node`, `image` and
`writableHostFilesystem`.

The pod is pinned with `spec.nodeName`, tolerates every taint, sets `hostPID` and
`hostNetwork`, and mounts the node's root filesystem at `/host` — **read-only
unless `writableHostFilesystem` is true**, which is a deliberate departure from
`kubectl debug`, which always mounts it writable. It is *not* privileged, sets
`automountServiceAccountToken: false`, and does not set `hostIPC`.

`before` is `null`, so the diff is the whole manifest as an addition. That is the
disclosure mechanism: every privileged field is on screen before the confirming
call.

- **Two gates.** `ADMIN_ALLOW_MUTATIONS` **and** `ADMIN_NODE_DEBUG_ENABLED`.
  Either off is `403 mutations_disabled` whose `hint` names both.
- **The dry run is refused too**, uniquely among the writes in this contract.
  Elsewhere §1.6 permits a projection on a read-only console because inspecting
  what would change is a read; here the projection *is* a working manifest for a
  privileged pod, and a deployment that switched this off has not consented to
  handing one out.
- **The gate refusal is audited** as `outcome: "denied"` by the funnel, against
  the pod the write would have created, and whichever of the two switches
  refused.
- **PodSecurity admission decides whether this is possible at all.** A namespace
  enforcing `baseline` or `restricted` rejects the pod (host namespaces, hostPath
  volume). Admission runs on `dryRun=All`, so the refusal arrives at the preview
  step carrying the plugin's own message — before anything exists:

  ```
  403 rbac_denied
  detail: pods "node-debugger-…" is forbidden: violates PodSecurity
          "baseline:latest": host namespaces (hostNetwork=true, hostPID=true),
          hostPath volumes (volume "host-root")
  ```

  **`403 rbac_denied`, not `422 invalid`.** Pod Security refuses with
  `Forbidden`, and §1.3 maps by status rather than by `Status.reason` — which is
  `Forbidden` here exactly as it is for a real RBAC denial, and is the reason
  that mapping exists. The code is therefore right and the advice it implies is
  wrong: no ClusterRole admits a pod the namespace's enforce label refuses. So
  the **hint** is rewritten to name Pod Security and `ADMIN_NODE_DEBUG_NAMESPACE`
  — `app.errors.podsecurity_hint`, shared with §15, and the same treatment §14.4
  gives RBAC escalation prevention. The code, the status and the API server's own
  detail are left alone.

#### `DELETE /api/nodes/{name}/debug/{pod}?dryRun=`
→ the §1.5 mutation response. The delete diff is §4's: `before=live, after=null`.

Only pods carrying this console's label **and** pinned to this node; anything
else is `404 not_found` from this route rather than a delete, or it would be a
general pod-delete with a node in its path.

> **Nothing removes this pod automatically, and that is not a gap this API can
> close.** `kubectl debug` has no `--rm` either. A closed browser tab is not a
> signal, and a restarted console drops whatever would have issued the DELETE, so
> the honest answer is to label the pods, list them, and offer removal — not to
> promise a cleanup that fails exactly when it matters.

Contrast §7.4, which is the opposite case: an ephemeral container **cannot** be
removed, because the API has no verb for it. A client must render the two
differently.

---

### `GET /api/events`
Query `namespace`, `involvedObjectKind`, `involvedObjectName`, `type`
(`Normal`|`Warning`), `limit` (default 200).
Row: `{ "namespace", "type", "reason", "message", "count", "first_seen", "last_seen",
"involved": {"kind","name","namespace","uid"}, "source": {"component","host"} }`.
Sorted newest `last_seen` first.

---

## 6. Workloads

The unified view. `kind` is one of `Deployment`, `StatefulSet`, `DaemonSet`,
`Job`, `CronJob`, `ReplicaSet` — lower-cased plural in paths
(`deployments`, `statefulsets`, `daemonsets`, `jobs`, `cronjobs`, `replicasets`).

### `GET /api/workloads?namespace=&kind=`

Row:
```json
{ "kind": "Deployment", "name": "checkout", "namespace": "prod",
  "replicas": {"desired": 5, "ready": 4, "updated": 5, "available": 4},
  "images": ["ghcr.io/acme/checkout:1.9.2"],
  "selector": {"app": "checkout"},
  "labels": {...},
  "age_seconds": 1209600,
  "status": "Progressing",
  "status_reason": "1 of 5 replicas not available",
  "restarts_24h": 3,
  "suspended": null,
  "schedule": null,
  "last_schedule": null }
```

- `status` ∈ `Healthy` | `Progressing` | `Degraded` | `Suspended` | `Unknown`.
  **`Unknown` is required** — a workload whose controller status has not been
  observed is not `Healthy`.
- `restarts_24h` is `null` when pod data was unavailable. Zero means zero.
- `suspended`/`schedule`/`last_schedule` are CronJob/Job fields; `null` elsewhere.

### `GET /api/workloads/{plural}/{namespace}/{name}`

```json
{ "workload": WorkloadRow,
  "spec": { "containers": [{"name","image","ports","resources","env_count","probes":{"liveness":true,"readiness":true,"startup":false}}],
            "serviceAccount": "checkout", "nodeSelector": {}, "tolerations": [], "volumes": [] },
  "pods": [PodRow],
  "conditions": [{"type","status","reason","message","lastTransitionTime"}],
  "services": [{"name","type","clusterIP","ports"}],
  "rollout": {"revision": 14, "strategy": "RollingUpdate", "maxSurge": "25%", "maxUnavailable": "25%"},
  "unavailable": [] }
```

PodRow:
```json
{ "name", "namespace", "node", "phase", "ready": "2/2", "restarts": 3,
  "age_seconds", "ip", "qos_class",
  "containers": [{"name","image","ready","restarts","state","reason","kind"}],
  "owner": {"kind":"ReplicaSet","name":"checkout-7d9"} }
```

`containers[].kind` is `container` for the pod's own and `ephemeral` for a §7.4
debug container somebody attached. **Neither `ready` nor `restarts` counts an
ephemeral entry**, because the kubelet does not either: an ephemeral container
has no readiness and is never restarted, and letting one turn `2/2` into `2/3`
would report a healthy pod as degraded for the duration of somebody's shell.

`phase` is the raw Kubernetes phase. The UI derives display state, but the
backend additionally supplies `phase_detail` for the cases where phase lies —
a `Running` pod with a `CrashLoopBackOff` container gets
`phase_detail: "CrashLoopBackOff"`. A pod being deleted gets `"Terminating"`.
Reporting a CrashLooping pod as `Running` is a confident wrong answer.

### `POST /api/workloads/{plural}/{namespace}/{name}/scale`
`{"replicas": 5, "dryRun": true}` → mutation response. Rejects on kinds without
a scale subresource (`422 invalid`, message naming the kind).

### `POST /api/workloads/{plural}/{namespace}/{name}/restart`
`{"dryRun": true}` → patches
`spec.template.metadata.annotations["k8boss-admin/restartedAt"] = <RFC3339>`,
the same mechanism as `kubectl rollout restart`. Response diff shows the
annotation change.

### `POST /api/workloads/{plural}/{namespace}/{name}/suspend`
`{"suspend": true, "dryRun": true}` — CronJobs and Jobs only.

### `GET /api/workloads/{plural}/{namespace}/{name}/rollout`
```json
{ "current": 14,
  "revisions": [ {"revision": 14, "created": "...", "images": ["...:1.9.2"], "change_cause": "..." },
                 {"revision": 13, "created": "...", "images": ["...:1.9.1"], "change_cause": null } ] }
```
Deployments read `ReplicaSet`s by `deployment.kubernetes.io/revision`;
StatefulSets/DaemonSets read `ControllerRevision`s. Kinds with no revision
history return `{"current": null, "revisions": [], "unavailable": [{"reason": "unsupported", ...}]}`.

### `POST /api/workloads/{plural}/{namespace}/{name}/rollback`
`{"revision": 13, "dryRun": true}` → mutation response whose diff is live pod
template vs. the historical one.

---

## 7. One pod — its detail, its logs, its shell, its environment and its usage

### `GET /api/pods/{namespace}/{name}/logs`
Query `container`, `tailLines` (default 500, max 10000), `previous`,
`sinceSeconds`, `timestamps`. Returns `text/plain`.
A missing container name with multiple containers is `422 invalid` listing the
containers — never a silent pick of the first one.

### `WS /api/ws/pods/{namespace}/{name}/logs`
Query `container`, `tailLines`, `cluster_id`. Server→client frames:
```json
{"type": "log",   "line": "...", "ts": "2026-08-18T09:14:00Z"}
{"type": "error", "reason": "forbidden", "detail": "..."}
{"type": "end",   "reason": "stream_closed"}
```
The stream always terminates with exactly one `end` or `error` frame, so a
viewer can tell "the pod stopped logging" from "we lost the connection".

### `WS /api/ws/pods/{namespace}/{name}/exec`
Query `container`, `command` (repeatable, default `/bin/sh`), `cluster_id`.
Requires `ADMIN_ALLOW_MUTATIONS` **and** a preflight on
`create pods/exec`. Client→server frames:
```json
{"type": "stdin",  "data": "ls -la\n"}
{"type": "resize", "cols": 120, "rows": 40}
```
Server→client: `{"type":"stdout","data":"..."}`, `{"type":"stderr","data":"..."}`,
`{"type":"error","reason":"...","detail":"..."}`, `{"type":"end","code":0}`.
Exec sessions are audited on open and on close.

**Refused on a cluster that impersonates.** When `impersonation_enabled` is on
for the cluster and the session qualifies (§27.2, ADR-0007), this endpoint
answers `{"type":"error","reason":"impersonation_unavailable"}` and closes,
*before* the preflight is issued. The channel is a WebSocket and its upgrade
request carries no impersonation headers, so the shell would run as the console's
ServiceAccount while the trail and the preflight named the operator — and
ADR-0007's fourth condition is that a request which cannot build those headers
fails rather than proceeding as the console. The refusal is audited like any
other terminal state. `kubectl exec` with the operator's own credentials keeps
their identity end to end; the other option is to turn impersonation off for that
cluster, which changes attribution for every other call and should be decided as
such rather than as a side effect of wanting a terminal.

`container` may name an **ephemeral** container (§7.4) as well as a regular or
init one. A missing `container` on a pod with more than one entry in
`spec.containers` is refused; ephemeral containers never enter that count,
because the API server's own defaulting rule does not count them either — so
attaching a debug container to a single-container pod must not start refusing
container-less requests that worked before.

### 7.4 Debug containers

`kubectl debug`, for the pod whose image has no shell. An **ephemeral
container** is scheduled into the *running* pod, sharing its network namespace
and volumes and — if asked — the process namespace of one of its containers.

#### `GET /api/pods/{namespace}/{name}/debug`
A §1.2 envelope of the pod's ephemeral containers, plus two additive keys:
```json
{ "items": [ {"name","image","targetContainer","command","tty",
              "state","reason","started_at"} ],
  "continue": null, "remaining": null, "partial": false, "unavailable": [],
  "podContainers": ["app", "envoy"], "initContainers": ["migrate"],
  "supported": true,
  "supportDetail": "The API server serves pods/ephemeralcontainers with verbs: get, patch, update." }
```
- `state` uses the same vocabulary as a PodRow container: `Running` |
  `Waiting` | `Terminated`, or **`null` when the kubelet has not reported on the
  container at all**, which is a different fact from `Waiting`.
- `command: null` means the image's own entrypoint runs. It is a real answer,
  not an unread value, and a client must not render it as §11.2's em dash.
- `started_at` is the container's start time from whichever state carries it —
  the API sets it on both `running` and `terminated`. It is `null` only for a
  container that genuinely has not started (`waiting`, or no status yet).
  Reporting a container that ran and exited as never started conflates two
  faults with opposite fixes: a debug image whose entrypoint returned, and one
  the node could not pull.
- `podContainers` and `initContainers` are the pod's own container names, from
  the same read. They are here so a client can offer the `targetContainer`
  choices and refuse an already-taken name **before** spending a request and an
  audit row — the §6 PodRow deliberately carries no init containers.
- **`supported` is three-valued.** `true`/`false` come from the core group's
  discovery document; **`null` means it could not be read**, so whether the
  cluster serves ephemeral containers is unknown. A client that renders `null`
  as `false` tells an operator their cluster lacks a feature it may well have,
  and sends them to plan an upgrade instead of to look at their API server.
- `items: []` is a real zero: the pod was read. A pod that could **not** be read
  is an error, never an empty list (§0.1).

#### `POST /api/pods/{namespace}/{name}/debug`
```json
{ "image": "busybox:1.36", "container": null, "targetContainer": "app",
  "command": ["sleep","3600"], "tty": true, "dryRun": true }
```
→ the §1.5 mutation response, plus `container`, `image`, `targetContainer`,
`command` and `tty`. `container` is the name that was generated
(`debugger-xxxxx`), so a client can open a terminal on what it created without
parsing the diff.

Every field is optional. `image` omitted uses the console's configured default
(`ADMIN_DEBUG_IMAGE`); `container` omitted is generated; `command` omitted runs
the image's entrypoint.

**A client that previews must replay the `container` it was given.** A generated
name is fresh per request, so a confirming call that omits `container` again
gets a *different* one — and the container that appears in the pod is then not
the one whose diff the operator approved. The response returns `container` for
exactly this reason; carry it into the confirming call the same way `dryRun`'s
echoed `resourceVersion` is carried (§0.4). A client that never previews may
omit it.

- **It is a write and it goes through the funnel** (§0.2–§0.5): a
  `SelfSubjectAccessReview` on **`patch` `pods/ephemeralcontainers`** — RBAC
  names the subresource separately from `pods` — then `dryRun=All` on the same
  call, the API server's projected diff, and an audit row naming the image.
- The patch is a **strategic merge** on `spec.ephemeralContainers`, whose merge
  key is `name`, so a second debug container appends. RFC 7386 would replace the
  list and delete the first one — which the API server then refuses, because an
  ephemeral container cannot be removed.
- A cluster that does not serve the subresource is **`501 unsupported`**,
  decided from discovery. Not `404 not_found`: the API server answers 404 for an
  unserved subresource, and "not found" about a pod the operator is looking at
  sends them hunting for a deletion that never happened. Per §1.3 a client
  renders this as an ordinary fact, not as an error.
- A container name already used by *any* container of the pod — regular, init or
  ephemeral — is `422 invalid` listing them. So is a `targetContainer` that is
  not one of `spec.containers`, and a pod whose phase is `Succeeded` or
  `Failed`, where the kubelet would never start the container.
- **§0.4 does not apply.** There is no `resourceVersion` on this write and none
  is needed: the merge key makes two concurrent attaches produce two containers
  rather than one overwriting the other, so there is no lost update to detect.

> **An ephemeral container cannot be removed.** The Kubernetes API has no verb
> for deleting one; it lives until the pod does. There is no `DELETE` here, and
> a client must not offer one — a control the API server will always refuse is
> the defect standard applied to a button. Restarting the workload is what
> removes it.

### 7.5 `GET /api/pods/{namespace}/{name}`

The §6 `PodRow` for one pod, plus the fields a table has no room for. `unavailable`
is present and empty: this endpoint makes exactly one read, and a pod that could
not be read is a §1.3 error, never an empty shell with a name at the top of it.

Added to the row:

```jsonc
{
  "...": "every §6 PodRow field",
  "init_containers": [ /* the container shape below, kind: "init" */ ],
  "conditions": [{"type","status","reason","message","last_transition"}],
  "volumes":    [{"name","kind","source"}],
  "uid": "...", "resource_version": "884213",
  "created_at": "...", "deleted_at": null, "start_time": "...",
  "labels": {}, "annotations": {},
  "service_account": "checkout", "restart_policy": "Always",
  "priority_class": null, "node_selector": {}, "host_network": false,
  "host_ip": "10.0.1.4",
  "status_reason": null, "status_message": null,
  "unavailable": [], "partial": false
}
```

Every entry of `containers` (and of `init_containers`) additionally carries
`image_id`, `container_id`, `started_at`, `ports:[{name,container_port,protocol,host_port}]`,
`requests`, `limits`, `command`, `args` and:

```jsonc
"last_terminated": {"reason": "OOMKilled", "exit_code": 137, "signal": null,
                    "started_at": "...", "finished_at": "...", "message": null}
```

That field is why this endpoint exists. "Restarted 14 times" is a symptom;
`OOMKilled` is the answer, and §6's row has nowhere to put it.

Three rules the shape encodes:

- **`requests` and `limits` are `null`, never `{}`, for a container that declares
  none.** A BestEffort container is first in line to be evicted; `cpu: 0` would
  describe it as one that asked for nothing and got it.
- **`init_containers` is a separate list.** A `Terminated` init container with
  exit code 0 is a success and the same two words on an app container are an
  outage. Merged, every healthy pod that has ever run a migration gets a red row.
- **`conditions[].status` stays the tri-state string** `"True"`/`"False"`/`"Unknown"`.
  Coerced to a boolean, "the kubelet has not said" becomes "no" — and a pod whose
  `Ready` condition is `Unknown` is a pod on a node that stopped reporting.

The row's `ready` fraction and `restarts` total still exclude ephemeral
containers, exactly as §6 specifies: this endpoint *enriches* the shaper's
entries rather than rebuilding them.

### 7.6 `GET /api/pods/{namespace}/{name}/environment`

The §1.2 envelope, one item per container, with the variables that container
will see in the order the kubelet builds them (`envFrom` first, then `env`).

```jsonc
{
  "items": [{
    "container": "app",
    "kind": "container" | "init" | "ephemeral",
    "variables": [{
      "name": "DB_PASSWORD",
      "value": null,
      "value_state": "withheld",
      "source": {"kind": "secretKeyRef", "name": "checkout-db",
                 "key": "password", "optional": null},
      "all_keys": false,
      "overridden": false
    }]
  }],
  "unavailable": [], "partial": false
}
```

`value_state` is a closed vocabulary and clients **branch on it**, because four
of the six states render as a blank value and they are four different facts:

| `value_state` | What it means |
|---|---|
| `literal` | Written in the pod spec. The blank you see is what the container sees |
| `resolved` | Read out of a ConfigMap |
| `withheld` | It comes from a Secret. **This endpoint never returns Secret values**, under any setting |
| `absent` | The ConfigMap was read and has no such key. Unless the reference is `optional` the kubelet refuses to start the container — a finding, and not the same as either blank below |
| `unreadable` | The ConfigMap or Secret could not be read. Named in `unavailable[]`; the variable is **not** known to be unset |
| `runtime` | A `fieldRef` or `resourceFieldRef`. The kubelet computes it at start and the API server never stores the result |

`source.kind` is the spec spelling — `configMapKeyRef`, `secretKeyRef`,
`configMapRef`, `secretRef`, `fieldRef`, `resourceFieldRef` — so a client can
name the object a variable comes from without parsing prose.

Two further rules:

- **`all_keys: true` with `name: null`** is the one row that stands in for an
  `envFrom` import whose object could not be read. Variables are coming from it
  and we cannot name them; reporting zero of them would be §0.1's failure hidden
  inside a container's environment, where nobody would look for it.
- **`overridden: true`** marks a variable a later entry in the same container
  redefines. The losing row is kept, because "this ConfigMap sets `DATABASE_URL`
  and something else is overriding it" is an answer people spend an afternoon on.

A `secretKeyRef` is answered **without reading the Secret**: the key is already in
the pod spec and the value is never returned, so there is nothing a read would
add — which is also why this endpoint renders in full on a console with no
`get secrets` grant. A `secretRef` in `envFrom` does read the Secret, for its key
names only.

### 7.7 `GET /api/pods/{namespace}/{name}/metrics`

The §1.2 envelope, one item per container, plus the pod totals and the sample
window.

```jsonc
{
  "items": [{
    "container": "app",
    "kind": "container" | "init",
    "usage": {"cpu_cores": 0.12, "memory_bytes": 188743680} | null,
    "sample_expected": true,
    "requests": {"cpu": "250m", "memory": "256Mi"} | null,
    "limits":   {"cpu": "1",    "memory": "512Mi"} | null
  }],
  "pod": {"cpu_cores": null, "memory_bytes": null},
  "window_seconds": 30,
  "timestamp": "2026-08-19T09:04:00Z",
  "unavailable": [], "partial": false
}
```

**The pod is the primary read and the sample is a secondary one.** That is the
whole design. A cluster with no `metrics.k8s.io` is an ordinary fact — §1.2's
`unsupported`, which §1.3 says a client renders as "not present on this cluster"
and deliberately not in red — and a pod that started ten seconds ago has no
sample on a cluster that is working perfectly. Both answer **200** with every
`usage` at `null` and an entry in `unavailable[]` naming the group; `requests`
and `limits` still render, because they come from the pod.

- **A number we did not measure is `null`, never `0`.** A pod drawn at zero cores
  reads as idle, and idle is what gets something turned off. That includes a
  quantity the API server spelled in a way this console cannot parse.
- **`pod.cpu_cores` and `pod.memory_bytes` are `null` when any container we
  expected a sample for is missing from it.** Summing the parts we have
  understates the pod by however much the unmeasured container is using, with
  nothing in the response to say a container is missing from the arithmetic.
- **`sample_expected` is what keeps the two kinds of `usage: null` apart.** It is
  `true` for every app container and for a *running* init container — a
  `restartPolicy: Always` sidecar, which runs for the life of the pod and uses
  real resources. It is `false` for a terminated init container: metrics-server
  does not report one, it holds nothing, and requiring it would make every pod
  that has ever run a migration report unknown usage forever. Only a missing
  `sample_expected: true` container nulls the totals.
- **The served version comes from discovery**, not from a pinned `v1beta1`: a
  cluster that has moved on is read rather than refused. A discovery that could
  not be *enumerated* is `forbidden`/`unreachable` in `unavailable[]` and never
  `unsupported` — "this cluster has no metrics" and "we could not find out" send
  an operator to two different places, and only one of them is an install.
- **A 404 from the metrics API is translated.** The pod was read a moment ago, so
  relaying "not found" beside its own name would read as "this pod is gone". The
  `unavailable` entry says the sample is missing and why.

---

## 8. Config, storage, access, network policy — typed rows

Served through §4's generic endpoint; these are the shapes the UI expects when
it asks for them, produced by the shaping layer.

- **Services** — `{name, namespace, type, clusterIP, externalIPs, ports:[{name,port,targetPort,protocol,nodePort}], selector, age_seconds, endpoint_count}`.
  `endpoint_count` is `null` if EndpointSlices could not be read.
- **Ingresses** — `{name, namespace, class, rules:[{host, paths:[{path, pathType, service, port}]}], tls_hosts, address, age_seconds}`.
- **ConfigMaps** — `{name, namespace, keys:[string], data_bytes, age_seconds}`. Values are returned only by the single-object read.
- **Secrets** — `{name, namespace, type, keys:[string], data_bytes, age_seconds}`.
  **Values are never included in list responses.** The single-object read
  returns values only when `?reveal=true` **and** mutations are enabled **and**
  a preflight on `get secrets` passes — and it writes an audit record. A secret
  read is a privileged act and is treated as one.
- **PVCs** — `{name, namespace, status, volume, capacity_bytes, access_modes, storage_class, age_seconds}`.
- **PVs** — `{name, status, capacity_bytes, access_modes, reclaim_policy, storage_class, claim, age_seconds}`.
- **StorageClasses** — `{name, provisioner, reclaim_policy, volume_binding_mode, is_default, age_seconds}`.
- **ServiceAccounts** — `{name, namespace, secrets_count, automount, age_seconds}`.
- **Roles / ClusterRoles** — `{name, namespace, rule_count, rules:[{apiGroups,resources,verbs,resourceNames}], age_seconds}`.
- **RoleBindings / ClusterRoleBindings** — `{name, namespace, role:{kind,name}, subjects:[{kind,name,namespace}], age_seconds}`.
- **NetworkPolicies** — see §8.3; the row is large enough to need its own
  subsection, and every null in it means something specific.

### 8.1 Browsable, untyped — Storage, Network, Configuration, Gateway (beta)

The Storage, Network and Configuration pages also carry tabs for resources with
no typed row above: the generic §4 endpoint returns the trimmed manifest
(`shape=raw`), and the frontend renders Name/Namespace/Age plus a YAML detail
panel, nothing shape-specific in the table itself. The one shape-specific thing
on these tabs is rule 11.10's `Create <Kind>…` button, and its shape comes from
discovery rather than from a shaper — the kind, the verbs and the namespacing
are read off §4's catalog entry, so a tab gains a correct create button without
anybody writing it one. This is a UI/nav decision, not a new contract —
each is reachable exactly as any resource is through §4, just given a tab in
the console's own navigation rather than only through §4's catalog/explorer:

- **Storage**: Volume Attributes Classes (`storage.k8s.io`, cluster-scoped).
- **Network**: Endpoint Slices (`discovery.k8s.io`, namespaced), Ingress
  Classes (`networking.k8s.io`, cluster-scoped), Network Policies
  (`networking.k8s.io`, namespaced).
- **Configuration**: HorizontalPodAutoscalers (`autoscaling`),
  VerticalPodAutoscalers (`autoscaling.k8s.io`), PodDisruptionBudgets
  (`policy`), ResourceQuotas / LimitRanges (core), PriorityClasses
  (`scheduling.k8s.io`, cluster-scoped), RuntimeClasses (`node.k8s.io`,
  cluster-scoped), Leases (`coordination.k8s.io`), Mutating/ValidatingWebhook
  Configurations (`admissionregistration.k8s.io`, cluster-scoped).
- **Gateway (beta)**: Gateways, Gateway Classes, HTTP Routes, GRPC Routes,
  Reference Grants, Backend TLS Policies — all `gateway.networking.k8s.io`.
  `BackendTrafficPolicy` is intentionally not included: it is not part of the
  upstream Gateway API and its real group varies by implementation (e.g. Envoy
  Gateway's is `gateway.envoyproxy.io`) — adding it requires confirming which
  implementation the target cluster runs, not a guess.

**VerticalPodAutoscalers, VolumeAttributesClasses and the whole Gateway (beta)
category are CRD-backed and commonly absent.** Per §1.2 and rule 7 above, a
cluster without the relevant CRDs installed answers `unsupported` on these
tabs' primary list call, and the frontend renders that as an ordinary "not
present on this cluster" state — ranked with the `metrics.k8s.io` example
already in §1.2, not as a defect.

**Gateway API's served version is resolved from the live catalog, not
hardcoded.** Its kinds move between release channels on real clusters
(`ReferenceGrant` commonly at `v1beta1`, `BackendTLSPolicy` at
`v1alpha2`/`v1alpha3` depending on the installed CRD bundle) and §4's resolver
matches a version exactly, so a tab that pinned one the way every other typed
tab does would `unsupported` on any cluster running a different channel. The
Gateway (beta) tabs instead look their plural up in `GET /api/resources/catalog`
and use whichever version the cluster actually reports, falling back to a
guessed version only until the catalog answers.

### 8.2 Custom Resources

A curated view over the same catalog §4's explorer already exposes (`GET
/api/resources/catalog`), grouped by API group and filtered down to groups that
are not one of Kubernetes' own built-in APIs — everything left is a CRD's
instances, by elimination. There is no `isCRD` field in a catalog item; the
frontend holds a static allowlist of built-in group names rather than the
backend adding one, unless that heuristic is shown to misbehave on a real
cluster. This is a second navigation surface over §4's existing data, not a new
endpoint or row shape.

### 8.3 The NetworkPolicy row

```json
{ "name": "default-deny-ingress", "namespace": "prod",
  "pod_selector": {"matchLabels": {}, "matchExpressions": []},
  "selects_all_pods": true,
  "policy_types": ["Ingress"], "policy_types_source": "declared",
  "ingress": {"governed": true,  "rule_count": 0,    "effect": "deny_all", "rules": []},
  "egress":  {"governed": false, "rule_count": null, "effect": null,       "rules": []},
  "age_seconds": 604800 }
```

**`rule_count` is `null` when the direction is not governed, and `0` only when it
is.** This is §0's null-versus-zero rule on the object where breaking it is most
expensive. `policyTypes: [Ingress]` with no `egress` section does not restrict
egress at all; `policyTypes: [Ingress, Egress]` with no `egress` section denies
*every* packet out of *every* pod the policy selects. The two differ by one word
inside a list, and a `0` in both would render the catastrophic one as the
harmless one.

`effect` states the same thing for a reader, and is `null` exactly when
`governed` is false:

| `effect` | Meaning |
|---|---|
| `deny_all` | Governed, no rules. Nothing is permitted in this direction. |
| `allow_all` | At least one rule restricts neither peer nor port. **Rules are a union of allowances**, so no restrictive neighbour narrows it. |
| `restricted` | Governed, with rules naming peers or ports. |

`rules[]` is `{peers, allows_all_peers, ports, allows_all_ports}`. An absent or
empty `from`/`to` means all peers and an absent or empty `ports` means all ports
— the API's own rule, computed here so no client counts list lengths and gets it
backwards. Each peer is
`{type, podSelector, namespaceSelector, cidr, except}` with `type` one of
`pod` (pods **in the policy's own namespace**), `namespace` (every pod in the
matching namespaces), `namespace_pod` (the intersection, from one list entry
carrying both selectors) or `ipBlock`. A peer carrying none of the three is
returned as `unknown` rather than dropped: a dropped peer renders a rule
*narrower* than the one the cluster holds.

`policy_types_source` is `declared` or `derived`. The API server defaults
`spec.policyTypes` on write, so a read normally declares it; when it does not,
the API's defaulting rule is applied here and labelled, because the derivation is
the console's statement rather than the object's.

`selects_all_pods` is a tri-state: `true` for `podSelector: {}` (which selects
**every** pod in the namespace), `false` for a populated selector, `null` for an
absent one — which the API forbids, so it means an object this console does not
understand.

**Nothing in this row asserts enforcement.** NetworkPolicy is implemented by the
CNI plugin, and no API the console can reach reports whether a given cluster's
plugin implements it. A cluster whose plugin does not will store and serve these
objects while forwarding every packet they describe as denied. The row describes
what the API server holds; the UI says so out loud.

### 8.4 Network policy endpoints

Listing NetworkPolicies is §4 —
`GET /api/resources/networking.k8s.io/v1/networkpolicies` returns the §8.3 row —
and creating, replacing and deleting one is §4 as well, through the single
mutation funnel. These two endpoints exist only for the questions a shaper cannot
answer, because both need a pod listing.

#### `GET /api/network/policies/{namespace}/{name}`
→ the §8.3 row plus `{selected_pods: [PodRow] | null, selected_pod_count: int | null,
unavailable, partial}`.

`selected_pods` is `null`, not `[]`, when the pod listing failed **or** when a
selector could not be evaluated. `[]` means the policy selects nothing and is
inert — the finding that gets a policy deleted as dead, so a failed read must
never look like one. The policy read is primary and raises; the pod listing is
secondary and is collected.

The §8.3 row served by §4 deliberately carries no `selected_pod_count`: a field
that were always `null` there would make the em dash mean "we did not look", and
this kind cannot afford a second meaning for `null`.

#### `GET /api/network/isolation?namespace=`
→ the §1.2 envelope over **pods**, plus `{namespace, policy_count, summary}`.

The rows are pods and the policies are the decoration, because the question is
which pods **nothing** selects — and such a pod appears on no policy's page.
Kubernetes defaults to allow, so an unselected pod accepts traffic from anywhere
in the cluster.

Each row is the §6 PodRow plus:

```json
{ "labels": {"app": "checkout"}, "host_network": false,
  "policies": ["default-deny-ingress"],
  "ingress": {"isolated": true, "effect": "deny_all", "policies": ["default-deny-ingress"]},
  "egress":  {"isolated": false, "effect": null, "policies": []} }
```

`isolated` is a tri-state. `true` means a policy that definitely selects this pod
governs that direction; `false` means none does; `null` means a selector could
not be evaluated, and is **not** rendered as `false` — "nothing protects this
pod" is the sentence an operator acts on. `effect` is the union of the governing
policies' effects and is `null` when any selecting policy could not be evaluated,
because an undecided policy can only widen what is permitted. `summary` counts
`isolated`, `unrestricted` and `unknown` separately per direction, so the three
sum to `pod_count` and none of them is a guess.

**Both reads are primary and both raise.** Degrading a failed *policy* listing to
nulls would render a full table of pods under a headline count of unrestricted
ones, assembled entirely from a read that failed.


---

## 9. Access preflight

### `GET /api/access/preflight`
Query `verb`, `group` (`core` for the core group), `resource`, `namespace`,
`name`, `subresource`.

PreflightResult:
```json
{ "verb": "patch", "group": "apps", "resource": "deployments", "namespace": "prod",
  "allowed": false, "reason": "no RBAC policy matched",
  "evaluationError": null,
  "hint": "Grant `patch` on `apps/deployments` in `prod` to the console ServiceAccount." }
```

`allowed: false` with a non-null `evaluationError` means **the review itself
failed** — we do not know whether the caller may act. The UI must render that
differently from a clean denial; conflating them tells the operator they lack a
permission they may well hold.

### `POST /api/access/preflight` — batch, body `{"checks": [ {verb,group,resource,namespace,subresource}, ... ]}`
→ `{"results": [PreflightResult]}`. Used by cluster registration and by pages
that need to know which buttons to show.

Baseline verb set checked at registration: `list` on `core/pods`,
`core/services`, `core/namespaces`, `core/nodes`, `core/configmaps`,
`core/events`, `apps/deployments`, `apps/statefulsets`, `apps/daemonsets`,
`batch/jobs`, `batch/cronjobs`, `networking.k8s.io/ingresses`,
`rbac.authorization.k8s.io/roles`; plus `patch` on `apps/deployments`,
`create` on `core/pods/exec`, `delete` on `core/pods`, `get` on `core/secrets`.

---

## 10. Audit

The trail holds two kinds of record, distinguished by `category`, in one table:

* `cluster` — an attempted write to a Kubernetes API server.
* `console` — a sign-in, a sign-out, a change to a console user, or an audit
  export.

They share a table because they answer one question — "who did what to this
console and its clusters" — and an incident review that has to remember there
are two places to look is one that will eventually look in only one.

### 10.1 `GET /api/audit`
Query `limit` (default 100, max 1000), `cursor`, `cluster_id`, `actor`,
`outcome`, `since`, `until`, `category`, `verb`, `dry_run`.
Row:
```json
{ "id": 4471, "ts": "2026-08-18T09:14:03Z", "category": "cluster", "actor": "erens",
  "cluster_id": 1, "cluster_name": "prod-eu",
  "verb": "patch", "target": {"group":"apps","version":"v1","resource":"deployments","namespace":"prod","name":"checkout"},
  "dry_run": false, "outcome": "applied",
  "detail": "replicas 3 -> 5", "diff_digest": "sha256:9f2c...", "error": null, "source_ip": "10.4.2.9",
  "prev_hash": "…", "event_hash": "…" }
```
`outcome` ∈ `applied` | `dry_run` | `denied` | `failed` | `conflict`.
`category` ∈ `cluster` | `console`. An unrecognised `outcome`, `category` or
timestamp is **rejected** with `422 invalid`, never ignored — a filter the
caller believes is applied and is not returns the whole trail under a false
label.

`until` earlier than `since` is likewise rejected: a window that selects nothing
would answer "nothing happened" to a question that was never asked.

Records are append-only; there is no delete endpoint.

**`cluster_id` means something different on this endpoint.** Everywhere else,
omitting it means "the active cluster" (§1.1). Here:

| value | selects |
|---|---|
| omitted | every record, of both categories |
| `0` | records that belong to **no** cluster — every console record |
| *n* | records for cluster *n* |

`0` is not a cluster id (the column is a positive autoincrement) and is free to
carry this meaning. It exists because the frontend appends `cluster_id` to every
request and drops empty values, so without it every sign-in record would be
unreachable from a session that has a cluster selected — an empty table under a
filter that looks like it is working.

**Actor attribution.** With `AUTH_ENABLED=true`, actor is the verified session
username and an inbound `X-K8Boss-User` cannot override it. With auth disabled,
actor comes from the legacy advisory `X-K8Boss-User` header and defaults to
`anonymous`.

One exception, and it is deliberate: on a **`console` record with outcome
`denied`** — a refused sign-in — the actor is the username that was *submitted*.
Nothing verified it, and it is a claim by the caller rather than an identity. It
is stored anyway because "somebody made forty attempts on this account" is the
question those rows exist to answer, and forty rows reading `anonymous` cannot
answer it. The UI states the distinction on the row.

### 10.2 Console records

`target.group` is `k8boss-admin.io`, which no cluster serves. It exists so a
console record has the same shape as a cluster write and the audit page needs one
renderer rather than two; consumers must not treat it as an API group.

| `verb` | `target.resource` | when |
|---|---|---|
| `login` | `sessions` | a sign-in: `applied` succeeded, `denied` was refused, `failed` means the identity provider could not answer |
| `logout` | `sessions` | a session was revoked |
| `create` / `patch` / `delete` | `users` | console user administration |
| `export` | `audit` | the trail was extracted (§10.4) |

`cluster_id` and `cluster_name` are null and `dry_run` is always false — there is
no rehearsed sign-in.

`failed` and `denied` are kept apart on sign-ins because they send an operator to
different places and only `denied` counts toward the throttle (§12.5). A
directory outage must not lock every operator out on top of being down.

### 10.3 `GET /api/audit/verify`

Every record is hash-chained: `event_hash` is SHA-256 over the record's immutable
content plus the previous record's `event_hash`. Editing, deleting, inserting or
reordering a committed record breaks the links from that point on.

This is a different guarantee from the append-only ORM guard, and the difference
is the reason it exists. The guard stops *this application* rewriting a record.
It is no protection against a `psql` session, a restored backup, or anyone with
write access to the volume — which, for a table whose whole value is that it can
be trusted after an incident, is the population that matters. The chain does not
prevent any of that either; it makes it **detectable**.

Query: `limit` (optional, `1`–`1000`; over the §10.1 page cap is `422 invalid`).
Bounded for the same reason a page is: the windowed walk reads newest-first and
reverses, so it holds `limit` rows in memory by construction where the unwindowed
walk streams — omitting `limit` is therefore uncapped, and is also the only form
that can return `intact`. Rejected rather than silently clamped: verifying a
thousand records while answering a question asked about a million is this
endpoint's own defect standard, a claim about records nobody read.

Response:

```json
{ "status": "partial", "verified": 4460, "unchained": 12, "total": 4472,
  "anchored": true, "first_break": null,
  "tip": "…", "genesis": "000…", "window": { "requested_limit": null, "oldest_unchained_id": 1 } }
```

| `status` | meaning |
|---|---|
| `intact` | every record is chained, every hash recomputes, and the links run unbroken from the first record to the last |
| `broken` | a record's content no longer matches its hash, or a record cannot be reached from the first one. `first_break` names the id and what it means. This is evidence of modification, deletion, insertion or reordering **after** the record was committed |
| `partial` | no break was found, **and** the trail contains records this mechanism cannot speak for. `unchained` counts them |

**`partial` is not a softer `intact`, and must never be rendered as a pass.**

Records written before hash chaining existed have both hash columns null and are
**never back-filled**. Back-filling would compute a hash over whatever those
records say *today* and store it as proof — converting "we do not know whether
this was altered" into "this is verified", in the one table where that inversion
does the most damage. So they stay null, they are counted, and the verdict is
withheld.

A record the writer could not link — the last-resort path after chain contention
— is stored **unchained rather than dropped**, and reports itself the same way.
Losing the link costs the ability to prove one record was not altered; losing the
record costs the knowledge that the action happened at all.

`limit` verifies only the newest N records. That catches an edit or a deletion
inside the window, but cannot prove the records *before* it still link back to
the first one, so the result is always `partial` with `anchored: false`. Omit
`limit` for the only form that can return `intact`.

Concurrency: a UNIQUE constraint on `prev_hash` makes a forked chain impossible
rather than merely detectable. Two replicas that read the same tip compute the
same `prev_hash`, the second INSERT is refused by the database, and the writer
retries against the new tip. The tip is the record nothing links to, not the
highest id — choosing by id let a renumbered record become the apparent tip and
collide with every subsequent write forever.

**What `intact` does not mean.** It means every record present verifies and the
links run unbroken. It does **not** mean nothing was removed from the end:
deleting the newest N records leaves a shorter chain that is internally perfect,
and no chain can detect that, because nothing in the table says where the end
should be. Deletion from the *middle* is detected — the records after the gap
stop linking back — as is renumbering, which is checked separately because a
record's id does not exist yet when its hash is computed. Guarding the tail needs
an anchor outside the database; §10.4 taken off-box on a schedule is the
available one.

### 10.4 `GET /api/audit/export`

The whole matching trail as one downloadable file. Same filters as `GET
/api/audit`, minus paging. Administrator-only when `AUTH_ENABLED=true`; in legacy
proxy mode there is no console role to check and the proxy owns the decision, as
it does for every other endpoint.

`format` ∈ `ndjson` | `csv`. Anything else is `422 invalid` rather than
defaulted — handing somebody who asked for `xlsx` a CSV lets them treat a
modified spreadsheet as the byte-faithful record.

| format | properties |
|---|---|
| `ndjson` | one JSON object per line, identical in shape to a `GET /api/audit` row, **including the chain columns**. Byte-faithful. This is the format to verify a chain against and to feed a machine |
| `csv` | flattened for a spreadsheet, `target` expanded into five columns. **Not byte-faithful** — see below |

**The CSV is defanged and says so.** `detail` and `error` carry text this console
did not write: Kubernetes error strings, admission-webhook responses, object
names. A cell beginning `=`, `+`, `-`, `@` or a tab is evaluated as a formula by
Excel, LibreOffice and Google Sheets when the file is opened, and formulas can
reach the network. An audit extract is a file that gets mailed around and opened
without thought. Such cells are prefixed with `'`. That is a real modification of
recorded bytes, which is why it is confined to CSV and why `ndjson` exists.

**There is no row cap and no `limit`.** A truncated extract that looked complete
would be unfalsifiable at the far end: the reader cannot tell a short file from a
quiet quarter, and "nobody scaled that deployment" is the conclusion they would
draw. The response streams.

**The export is itself audited** — a `console` record with verb `export`,
recorded before a byte is streamed, carrying the filters that were applied. Bulk
extraction of an audit trail is precisely the kind of act the trail exists to
record, and recording it on completion would leave an aborted download with no
trace.

---

## 11. Frontend consumption rules

1. `partial: true` renders a persistent inline banner naming each `unavailable`
   entry. It is never swallowed into a toast that disappears.
2. A `null` numeric field renders as `—` with a tooltip explaining it could not
   be read. It is never rendered as `0`.
3. Mutation flow is always: build request → call with `dryRun: true` → show the
   returned unified diff → user confirms → call with `dryRun: false`. There is
   no button that writes to a cluster without having shown a diff first.
4. Buttons the caller cannot use are **disabled with the reason**, driven by
   `/api/access/preflight`, not hidden. An operator must be able to see that an
   action exists and why it is unavailable.
5. When `health.mutations === "disabled"`, the app renders a read-only banner
   and disables write affordances globally.
6. A view that re-reads on a timer says **when the data on screen was read**, and
   a refresh that fails keeps what it has and says the refresh failed. It never
   blanks, and it never advances the timestamp on an attempt that did not
   return: an unlabelled view is indistinguishable from a live one, and a stale
   object presented as current is the defect standard applied to time.
7. A **primary** list call answering `unsupported` (§1.2) renders the same calm
   "not present on this cluster" empty state as any other ordinary absence —
   never `ErrorState`'s red panel. This applies to a whole tab/page's own
   listing, not only to a secondary read folded into `unavailable[]`: a tab for
   a CRD-backed resource (Gateway API, VerticalPodAutoscaler,
   VolumeAttributesClass) is routine on a cluster that doesn't have the CRD
   installed, and rendering that red trains operators to stop trusting red.
8. A detail view with tabs **fetches only the visible tab**, and the active tab
   lives in the URL. Mounting all of them at once is one read per tab on
   arrival — most of them for a panel nobody is looking at — and on the pod page
   (§7.5) two of the tabs open a websocket, so a Terminal that connected behind
   an unopened tab would start an audited exec session nobody asked for. The URL
   half is not cosmetic: a panel an operator cannot link to is a panel they have
   to describe over the phone.
9. A screen offering **both a form and a document** — every create dialog (§4,
   and §11.10's button on every listing), the exposure screen (§13.5) — makes
   the **document authoritative and the form a projection of it**. A form edit is patched *into* the current document rather
   than regenerating it, and the form **names, by path, every field it is not
   showing**. Both halves are load-bearing. Without the first, a `spec.affinity`
   somebody hand-wrote disappears the moment they touch an unrelated control, and
   the diff they approve is a correct projection of a manifest nobody wrote.
   Without the second, "some fields may not be represented in this form view" is
   a warning with no way to act on it. Where the patching happens is not fixed:
   §13.5 compiles server-side and returns `preserved[]` because one form field
   there means three different objects; the create dialog computes both in the
   browser, because a create is one object and no round trip is involved — which
   also means nothing on the wire attests to it and only a test can.

   The form is offered on every kind and **modelled** on some of them: where no
   model exists — most CRDs, and every kind nobody has written controls for —
   the view control is **present, disabled and says so** (rule 4), and the
   dialog opens in YAML view. Hiding it instead would make "this kind has no
   form" and "this console has no forms" the same screen, and a form
   regenerated from a kind it does not understand would drop the half of the
   spec it never had a control for.

10. **Every resource listing offers `Create <Kind>…`, and the button answers two
   questions in that order.** First, whether the *API* serves a create for this
   resource — §4's catalog `verbs`, and the answer is a property of the cluster.
   Second, whether *this caller* may use it — §9's preflight, batched once per
   page, in the namespace the masthead has selected. **The two are never
   merged.** "This cluster does not serve a create for these" and "you may not
   create one" send an operator to two different places, and answering the first
   with the second sends them to widen a ClusterRole that was already correct. A
   catalog still in flight is a third answer and stays one: until discovery
   answers, the button reads `Create…`, is disabled, and says it does not know
   yet — not "unsupported", which is a fact about a cluster nobody has finished
   asking. The preflight carries the selected namespace: a check with no
   namespace asks whether the caller may create the resource *anywhere*, and an
   operator holding a grant in one namespace would be told they cannot do what
   they can.

   **The kind comes from discovery, never from the tab's title.** Titles are
   display strings — "Endpoint Slices", "Network Policies", "HPAs" — and
   trimming an `s` off them produces "Endpoint Slice", "Network Policie" and
   "HPA", none of which is a kind. A button offering to create a kind that does
   not exist is the defect standard with a click target on it.

   All four states are rule 4's disabled-with-the-reason. None is a hidden
   button.

11. **A create dialog opened from a listing is seeded with a starter; the
   masthead's `+` opens empty.** A starter is a named, minimal, *valid* manifest
   for that one kind, and several kinds carry more than one because the shapes
   an operator picks between are different objects — a headless Service and a
   LoadBalancer share a kind and almost nothing else. Three properties are
   contract, because each has a failure behind it: a starter carrying a pod
   template **applies under the restricted Pod Security Standard**, since an
   admission rejection on the console's own starter reads as a broken console
   rather than as a cluster policy; a starter **never sets
   `metadata.namespace`**, because the masthead selection is the fallback (§4)
   and a hardcoded namespace goes stale the moment an operator switches scope
   mid-edit; and a starter **carries no comments**, because a form edit
   re-serialises the parsed document and nothing carries a comment across a
   parse — a commented starter would warn the operator it was about to lose
   lines the console itself wrote, on the first click, before they had typed
   anything. What a comment would have said is the starter's own description,
   which is on screen beside it and survives the rewrite.

   **A kind this console ships no starter for gets a skeleton** — `apiVersion`,
   `kind`, `metadata.name` — labelled as exactly that. Inventing a body for
   somebody's CRD means guessing at a schema nobody here has read, and a guess
   presented as a starting point is a wrong answer with a Create button under
   it.

   Switching starters **replaces the document**, so it is confirmed — and only
   once there is something to lose: confirming a swap out of a document nobody
   has typed into is a dialog about nothing.

   **No starter is a claim that the object will be accepted.** The dry run is.

12. **The console has one reading of a manifest, and it is the one it sends.**
   `parse_document` reads through `app/yaml_dialect.py`, whose implicit
   resolvers are YAML 1.1's, and its result leaves as JSON — so `off` is the
   boolean `false`, `0755` is 493, `8:30` is 510 and `y` is `true`. The browser reads and re-serialises the same document the same
   way, against a schema mirroring those resolvers. Everything the console says
   about a manifest locally — the form view's controls, the fields rule 9 names
   as unrepresented, and the document-only checks beside them — is therefore a
   lens onto the object that is going to be written, and not onto a second
   reading of it. ADR-0009 records why the reading that won is that one; the
   short version is that it is what was already being sent, and what `kubectl`
   sends for `0755`.

   **The editor still says where that is not the obvious reading**, because the
   ambiguity is the document's and not the console's. `off` is the text `"off"`
   to YAML 1.2, which is the specification and what the operator's editor
   implements, and each such scalar is reported by path with both readings as a
   **warning that never blocks**: the document is legal YAML either way and
   §1.5's dry run is the authority on whether the cluster wants it.

   The diff was never what this protects. `diff.after` is the API server's own
   projection of what it was actually sent, so it already showed the truth while
   the form beside it did not.

   **Both readings come from a parser, not from a pattern over the source.** A
   pattern cannot tell a plain `off` from one inside a `|` block, from a quoted
   `"off"`, from a continuation line of a multi-line scalar, and a create screen
   that warned about a config file's contents is one whose warnings stop being
   read. What is transcribed across the language boundary is only PyYAML's three
   scalar resolvers, and both sides of that transcription are pinned against the
   real parsers over one shared corpus.

   **`y`, `Y`, `n` and `N` are booleans** (ADR-0010). PyYAML declines the
   single-letter half of YAML 1.1's boolean type and `kubectl` does not, so
   `verbose: y` used to reach a cluster as the text `"y"` from here and as
   `true` from the command line — with nothing saying so, because YAML 1.2 calls
   it a string too and the comparison above found nothing to report. Sending
   what `kubectl` sends is what gives the warning something to find.

   **A scalar JSON cannot carry is refused rather than sent.** `.inf`, `-.Inf`
   and `.nan` are YAML numbers with no JSON spelling; `parse_document` answers
   `422 invalid` naming the path, instead of letting the API server reject a
   body with a syntax error at a character offset that is in neither the
   operator's document nor the object they meant.

13. **A view that draws relationships may only draw the ones it can prove, and
   must say where it could not look.** The topology view (`/topology`) is §6's
   workload listing, §8's Services and §13's exposures joined **in the browser**
   — no topology endpoint, no fourth read model. Three properties are contract,
   because a picture is read as fact in a way a table is not:

   **A drawn connection is a proved connection.** A Service is attached to a
   workload only when its `spec.selector` is a subset of the workload row's
   `selector` (§6's `matchLabels`). That direction is sound in one direction
   only, and that is why it is the one used: the API server requires a
   controller's `matchLabels` to be present on its pod template, so a selector
   contained in them really does select these pods. A Service with no selector
   is never attached — it is backed by hand-managed EndpointSlices and reaches
   nothing here, which is the same judgement §6's detail makes.

   **The join is therefore three-valued, because the converse does not hold.**
   `matchLabels` is a *subset* of the pod template's labels and the row does not
   carry the template, so a Service selecting on a key the row does not carry
   may really select these pods: it is neither attached nor ruled out, and the
   node is marked *unknown* rather than drawn bare. What settles the negative is
   a disagreement — a key the row does carry, with a different value — because
   the template carries `matchLabels` verbatim. Without the third value the
   absence of a marker would be an inference from a listing that cannot support
   it, which is the defect standard in a drawing. The panel is the authority
   either way: it asks the object's own read, and the exposures it lists are
   matched against *its* Services, not the node's.

   The same applies to an exposure's target. §13's row carries `namespace` and
   `kind` beside the backend `service` name, and the HTTPRoute shaper reads
   both off `backendRefs[]`, so the join matches a Gateway API `backendRef`
   exactly: `target.namespace ?? route.namespace` is the namespace the target
   really lives in, and a target whose `kind` is set and is not `Service` (or
   `null`) is not drawn against any workload. A `backendRef` to another
   namespace's Service of the same name is no longer drawn against this
   namespace's workload.

   **An unattributable node is marked, never drawn bare.** A row whose
   `selector` is empty — an expression-only selector, or a CronJob, which has
   none — cannot be matched against any Service from the listing. Neither can
   any node while the Services or Routes listing is unreadable, **has not
   answered yet**, or **stopped at its limit**: a listing cut off by §4's
   `continue` cursor or reported in §13's `truncated[]` is a third way to be
   blind, it is not an `unavailable[]` entry, and a Service past the cursor
   still selects pods. In each case the node carries the *unknown* marker and
   the sentence that explains it, because a node drawn with no exposure is the
   claim that nothing outside the cluster reaches it, and that claim is acted
   on. `unsupported` (§1.2) is excluded: a cluster that does not serve
   `route.openshift.io` has no Routes, and marking every node unknown for it
   would make the marker decoration.

   **The selected node's panel asks the object's own read.** `GET
   /api/workloads/{plural}/{ns}/{name}` reads the pod template and is the
   authority on which Services select it; the canvas is a summary of a listing
   and the panel is the answer. The panel's `Actions` are the same dialogs the
   workload's own page opens, gated by the same §9 batch — one preflight for the
   page, over the kinds actually on the canvas, never one per node. **No write
   is added by this view.** The selection lives in `?selected=`, for rule 8's
   reason.

---

## 12. Console authentication and users

Authentication is disabled by default for compatibility with deployments that
already put an authenticating proxy in front. When `AUTH_ENABLED=true`, every
HTTP and WebSocket API requires a valid opaque session cookie, except:

* `GET /api/health`, `GET /api/auth/config`, `POST /api/auth/login`;
* `GET /api/auth/{provider}/start` and `GET /api/auth/{provider}/callback` for
  each of the four single sign-on providers (§12.4);
* `POST /api/auth/saml/acs` and `GET /api/auth/saml/metadata`.

Unsafe HTTP methods also require the session's `X-CSRF-Token`. Session bearer
tokens are HttpOnly cookies and only their SHA-256 digests are stored.

**A WebSocket without a valid session is accepted and then closed with
application close code `4401`, never refused before the accept.** That ordering
is normative, not an implementation detail: a close sent before the accept is a
failure of the HTTP upgrade itself, and a browser reports that to the page as a
generic connection error with no code attached. The `4401` never arrives,
`onclose` cannot tell "your session expired" from "something dropped us", and
both stream viewers rendered an expired session as *connection lost* — which
sends an operator to debug a network that is fine. Accepting first costs one
frame and is the only way the reason reaches the client. `4401` is the one
application close code this API defines, and both stream viewers branch on it to
offer sign-in rather than a reconnect; every other close is an ordinary end of
stream or a transport failure, and neither is a reason to ask anyone to sign in.

The sign-on routes are public because single sign-on **is how a session is
obtained** — challenging them for one is a deadlock whose symptom is a sign-in
button that answers 401. The list is **exact paths built from the provider
registry**, never a prefix rule: "anything under `/api/auth/`" is one route away
from exempting something that should never have been.

Public is not unprotected. `/start` mints a sealed handshake and redirects; a
callback refuses anything that does not match a handshake this console started
and issues a session only after a verified assertion. `POST /api/auth/saml/acs`
is the one unauthenticated POST in this API, and it is exempt from the CSRF check
for a reason that does not generalise: the request comes from the identity
provider's origin, and demanding a token from a party that has never seen a page
of this console is not a check, it is a guaranteed failure. What stands in its
place is the assertion itself — a signature checked against a configured
certificate, and an `InResponseTo` *inside that signed subtree* which must equal
the request id sealed in the browser's own handshake cookie. **A future POST
route in this list needs its own answer to that question rather than inheriting
this one.**

### 12.1 `GET /api/auth/config`

Public discovery:

```json
{ "enabled": true, "localEnabled": true, "ldapEnabled": true, "oidcEnabled": true,
  "methods": ["local", "ldap", "oidc", "openshift"],
  "ssoProviders": [
    { "name": "oidc", "label": "Single sign-on", "startPath": "/api/auth/oidc/start" },
    { "name": "openshift", "label": "OpenShift", "startPath": "/api/auth/openshift/start" }
  ],
  "oidc": { "label": "Single sign-on", "startPath": "/api/auth/oidc/start" } }
```

It names *which* methods exist and never how they are wired. This is the one
unauthenticated endpoint in the API, and the login page only needs to know which
buttons to draw; returning an issuer, a client id, an API server address or the
configured groups would let anyone who can reach the console enumerate its
identity infrastructure.

`ssoProviders` is the general form: one entry per configured provider, in a
stable order, each with the label its button carries. A deployment offering two
of them has to be able to say which is which — "Single sign-on" twice is a choice
nobody can make.

A provider appears **only when every value its flow needs is present**, and only
when `AUTH_ENABLED` is set. A half-configured deployment shows no button for that
provider, because a button that leads to an error reads as a broken console
rather than an unconfigured one; and a console that is not authenticating
requests has no session to issue, so a handshake would end back at a console that
never asked who the operator was.

`oidcEnabled` and `oidc` are the OpenID Connect entry of `ssoProviders`,
repeated. They are retained rather than removed because a browser holding an
older build of the SPA reads them, and dropping them would take that deployment's
sign-in button away at the moment the backend was upgraded — a console nobody can
log in to, produced by a release that changed no behaviour.

### 12.2 `POST /api/auth/login`

Body `{ "username":"erens", "password":"...", "source":"auto|local|ldap" }`.
Success sets the session cookie and returns `{enabled, authenticated, user,
csrfToken, expiresAt}`. Every credential rejection is `401 invalid_credentials`;
the response never reveals whether the username exists. An unavailable directory
is `502 identity_provider_unavailable`, not invalid credentials. An exhausted
sign-in budget is `429 too_many_attempts` (§12.5), which the UI must render
differently — telling somebody to check their password while the console is
refusing to look at it is how a lockout becomes a support ticket.

**Every terminal state is recorded in §10** as a `console` record: `applied`,
`denied`, or `failed` when the identity provider could not answer. The response
still reveals nothing about whether the username exists; the trail does, because
it is read by the operator rather than by the caller, and a run of attempts
against names that do not exist is the shape of an enumeration sweep.

### 12.3 `GET /api/auth/me` / `POST /api/auth/logout`

`me` returns the current session body and CSRF token. `logout` revokes the
server-side session and clears the cookie. A logout carrying no session revokes
nothing and records nothing — a sign-out row for somebody who was never signed in
is noise in the one table that must not have any.

### 12.4 Single sign-on

**Four providers, one of each per deployment**, configured from the environment
or from the console (§12.8, [ADR-0011](adr-0011-database-backed-identity-providers.md)),
exactly as LDAP is:

| `name` | What it is | Where identity comes from | Settings |
|---|---|---|---|
| `oidc` | An OpenID Connect issuer | A signed ID token, verified completely | `OIDC_*` |
| `oauth` | A plain OAuth 2.0 authorization server (GitHub, GitLab, a non-OIDC Keycloak client) | A userinfo endpoint read with the access token | `OAUTH_*` |
| `openshift` | The cluster's own built-in OAuth server | `users/~`, read from the cluster with the access token | `OPENSHIFT_*` |
| `saml` | A SAML 2.0 identity provider | A signed assertion, read out of the verified subtree | `SAML_*` |

Several concurrent providers **of the same kind** is a real design change — a
table, a CRUD surface, per-row encrypted secrets and a subject-collision story
across issuers — not a config key, and the console says it does not do that
rather than half-doing it. Four different kinds do not collide the same way: each
account row carries the `auth_source` that owns it, so a username two of them
assert is refused rather than merged (see **Account binding** below).

**ADR-0011 narrowed that sentence rather than dropping it.** A provider's
configuration is now editable from the console (§12.8), stored **one row per
kind** — `kind` is unique in `identity_providers`, so a second issuer of the same
kind cannot exist, and the subject collision above therefore cannot arise. Three
of the four costs are paid (a table, a CRUD surface, encrypted secrets); the
fourth does not apply. *N* providers of one kind remains refused on exactly the
terms above.

Where a kind's configuration comes from, in order:

1. a row in `identity_providers` — **including a disabled row**, which is this
   deployment's decision that the kind is off;
2. otherwise the `{KIND}_*` environment variables.

A row therefore wins over an enabled variable, and **deleting a row falls back to
the environment rather than to off**. Both halves are normative. An operator who
switches a provider off in the console must not have it switched back on by a
variable in a Compose file they have never read; and an operator who deletes a
row must get the deployment's own configuration back, because a delete that
silently removed the only way into a console would be the worst button in this
API. Every read reports which of the two answered, and a refusal's `hint` names
whichever one is in effect — telling somebody to set `OIDC_ISSUER` while a stored
row is live sends them to change something that changes nothing.

The routes are generic. There is one `/start`, one callback per binding, and one
place where the throttle, the audit calls, the account provisioning and the
failure redirect are written — because four hand-written route pairs would be
four copies of those, and the first one to lose a copy would be invisible from
outside: same status, same shape, no failing test, and a hole in the audit trail
found later by somebody asking who signed in.

An unknown `{provider}` is a **404**, checked before anything else happens: the
failure redirect interpolates the name, so an unvalidated one would be reflected
into a URL this console sends a browser to. `GET /api/auth/saml/callback` is a
404 for the same reason — SAML's assertion arrives on a POST at `.../acs`, and a
console that also answered on the redirect path would let the URL an
administrator registered and the URL it answers on silently differ.

#### `GET /api/auth/{provider}/start?next=<path>`

302 to the provider, carrying whatever binds the response to this sign-in:

* `oidc` — Authorization Code + PKCE S256, with `state` and `nonce`.
* `oauth` and `openshift` — Authorization Code + PKCE S256, with `state` and
  **no** `nonce`. A nonce binds an ID token; there is none here, and a parameter
  nothing ever checks is what a later reader mistakes for a protection in force.
* `saml` — a DEFLATE-compressed `AuthnRequest` on the HTTP-Redirect binding. Its
  `ID` is the value the assertion's `InResponseTo` must equal, and it is sealed
  in the handshake as `state` for exactly that comparison.

Those values plus the exact callback URL are sealed into a short-lived cookie,
**one per provider** — name and path both carry the provider, and the provider is
written inside the sealed payload and checked on unseal. An operator can click
the wrong button, go back and click another; a shared cookie would let the second
handshake overwrite the first and would let one provider's callback consume a
handshake the other started, which is how a flow with weaker checks completes a
sign-in a stronger one began.

`next` must be a same-origin path. An absolute URL, a scheme-relative
`//host`, or anything containing a backslash becomes `/`. A login link carrying
`?next=https://evil.example` would otherwise produce a page on this console's
domain that authenticates the operator and hands them to somebody else's site.

**The handshake cookie's `SameSite` differs by binding, and neither value is
`Strict`.** The redirect providers use `Lax`: their callback is a top-level
navigation from the provider's origin, and browsers do not send a `Strict` cookie
on a cross-site navigation, so a `Strict` handshake cookie is simply absent when
the callback runs and every sign-in fails. SAML uses `None` and therefore
`Secure`: its assertion arrives on a cross-site *form POST*, which carries no
`Lax` cookie either. Since browsers discard a `SameSite=None` cookie that is not
`Secure`, **SAML is not offered at all unless `AUTH_COOKIE_SECURE` is set** —
the same rule as a missing client id, applied to a prerequisite that is easy to
miss.

#### `GET /api/auth/{provider}/callback` — `oidc`, `oauth`, `openshift`

`state` is compared against the sealed handshake before anything else. Then the
provider's own verification runs:

* **`oidc`** verifies the ID token completely before reading a single claim from
  it: signature against the issuer's JWKS, an **algorithm allowlist**
  (asymmetric only — never the token's own `alg`), `iss`, `aud`, `exp`/`iat` as
  required claims, and the `nonce` against this browser's handshake. Then the
  PKCE verifier is presented on the code exchange. Each has a specific attack
  behind it: a token minted by the same issuer for a different client is valid
  and correctly signed, so without an audience check anyone holding one can sign
  in here; `alg: none` and HS256-with-the-public-key both work against a verifier
  that trusts the token's header; and without a nonce an assertion captured from
  any other sign-in can be replayed.
* **`oauth`** has no signed assertion to verify, and the trust argument is
  different rather than weaker: the console exchanges a PKCE-protected code for
  an access token at the configured token endpoint over a verified TLS
  connection, then spends that token on one read of the configured userinfo
  endpoint. A 200 there is the *provider* resolving that token to a person; a
  token this console was fed rather than issued gets 401. The one specific cost
  is that **the identity is only as good as the TLS verification of that call** —
  with a signed ID token a compromised channel still cannot forge an assertion,
  and here it can. `OAUTH_VERIFY_TLS=false` is therefore not an inconvenience
  switch on this provider.
* **`openshift`** is the same shape with a stronger party: the access token was
  minted by the cluster's own OAuth server and it is the **cluster** that
  resolves it, at `GET /apis/user.openshift.io/v1/users/~`. The token is spent
  once and dropped, so the console stores no cluster credential. The subject is
  `metadata.uid`, not the name.

#### `POST /api/auth/saml/acs`

The Assertion Consumer Service. The body is `application/x-www-form-urlencoded`
per the HTTP-POST binding, and only `SAMLResponse` is read — `RelayState` is
neither read nor honoured, because the identity provider echoes it and an
attacker who can make the IdP POST can choose it. The destination after sign-in
comes from the sealed cookie, where nobody outside this console can have set it.

**Everything the console reads comes out of the subtree the signature check
returned, and nothing is ever read from the document as posted.** That is the
whole defence against XML Signature Wrapping, in which the attacker leaves a
genuinely signed assertion where the verifier looks and puts a forged one where
the reader looks — both halves true at once, so the signature verifies and the
identity is the attacker's. It is a property of the code's shape rather than a
check, because "verify, then re-read the document" is what a check-based
implementation degrades into.

Verified, in this order: the signature (over the `Response` or over the
`Assertion` — both shapes are in the wild) against a configured certificate, of
which several may be listed so a signing-key rotation is a config change rather
than an outage; the `Issuer` against `SAML_IDP_ENTITY_ID`, without which a
certificate this console trusts could sign for any issuer; the `Audience`
against this console's entity ID, which is the SAML spelling of `aud`;
`InResponseTo` against the sealed `AuthnRequest` id; `Recipient` against the ACS
URL, **required rather than checked when present** — the party replaying an
assertion at somebody else's endpoint is also the party who can leave the
attribute out, so a check that only ran when it was there checked nothing in the
one case it exists for, and SAML core makes it mandatory on bearer confirmation
data anyway; the condition and subject-confirmation windows, with
`SAML_CLOCK_SKEW_SECONDS` of leeway and **no expiry treated as a refusal**; and
the presence of an `AuthnStatement`, because an assertion carrying
only attributes is a statement *about* somebody rather than a statement that they
just authenticated.

**The `Response` wrapper's `Destination` is not checked, and that is deliberate
rather than missing.** On the assertion-signed shape every large IdP emits —
Shibboleth, Keycloak, Okta — `Destination` sits outside the signed subtree, so
it is a value whoever posts the document writes. Checking it would read, in this
list, as a second address check standing beside `Recipient`, while catching
nothing `Recipient` does not already catch. `Recipient` is inside the signature,
which is why it is the one that can refuse anything.

Three refusals are named rather than folded into "invalid", because the
administrator's next action differs for each: an **encrypted assertion** (this
console holds no decryption key and will not read the envelope instead), an
**unsolicited IdP-initiated response** (no `InResponseTo`, so nothing binds it to
a browser and anyone could replay it into anyone else's), and a **plain-HTTP
deployment** (`AUTH_COOKIE_SECURE` unset, so the handshake cookie can never come
back).

Replay is bounded by the handshake rather than by an assertion-id store: the
sealed cookie is deleted the moment the ACS runs and is unreadable after ten
minutes regardless. A one-time-use store would be strictly stronger and needs a
table this flow deliberately does not have.

#### `GET /api/auth/saml/metadata`

This console's SP metadata — `entityID`, the ACS URL, the binding — served only
when SAML is enabled, so it can never describe an endpoint that would refuse the
assertion it invites. It advertises no certificate, because the console has
none: it does not sign its `AuthnRequest` and cannot decrypt an assertion, and
advertising a key it does not hold is how an IdP ends up encrypting to nobody.

#### Every callback, after verification

Success sets the session cookie and 302s to `next`. Failure 302s to `next`
carrying `?auth_error=<§1.3 code>&auth_reason=<slug>`. It cannot answer with a
§1.3 envelope — the caller is a browser navigation, not `fetch()` — so the code
is drawn from the same §1.3 vocabulary rather than inventing a second one. The
login page words the slug; an unrecognised slug is rendered verbatim rather than
replaced with something generic.

**Nothing is written to the audit trail before the handshake is verified**, and
these routes are public, so that rule is load-bearing rather than tidy. Every path
reachable before the sealed cookie is checked is reachable by anyone who can
reach the console, and an audit write on one of them is an unauthenticated
INSERT into the one table in this schema with no upper bound on rows — a loop
over `?error=` would fill the operator's database, on a deployment that may never
have enabled SSO at all. A callback matching no handshake this console started
is also not a failed sign-in, and recording it as one puts rows in the trail that
no operator's action produced.

Once the handshake *is* ours, every failure is recorded: a provider refusal, a
state mismatch, a rejected assertion, a refused account.

#### Account binding

A federated identity is bound to the provider's stable subject, stored as
`users.external_id` — `sub` for OIDC, `OAUTH_SUBJECT_FIELD` for OAuth,
`metadata.uid` for OpenShift, the `NameID` for SAML. Two refusals, both
`permission_denied`, both refusals rather than merges:

* **A username already owned by another auth source.** If `alice` is a local
  account with a password, an SSO assertion naming `alice` must not adopt it —
  otherwise anyone who can make an issuer assert a username inherits whatever
  that username already had. This holds *between* providers too: a username
  belonging to an `oidc` account is refused to the `saml` one.
* **A username already bound to a different subject.** The subject is the
  provider's stable identity; the username is a label it can reuse. A mismatch is
  either a recycled name or a second issuer asserting the same one.

A row with a null `external_id` predates the binding and is adopted once, so an
existing deployment can turn SSO on without every account being refused.
Rebinding afterwards is deliberately not a login-time action. A federated
account's stored password is cleared on adoption — a provider-managed account
that kept a usable password hash would be a second way in that nobody is
watching.

#### Group mapping, and the two opposite rules

Each provider's `*_ADMIN_GROUP` maps its members to `admin`; every other resolved
membership maps to `user`. Each `*_ALLOWED_GROUPS`, when set, restricts who may
sign in at all.

Membership can be **absent** rather than empty — a claim not in a token, a field
not in a userinfo document, a `groups` key not in a `User` object, an attribute
not released by a SAML IdP — and the two are not the same fact. Most providers
omit it entirely unless it was asked for, so absent is the common state during
setup.

* **Role mapping fails open**: an absent claim leaves the account's stored role
  unchanged. Writing the default there would demote an administrator every time
  the issuer forgot the claim, and the next good login would silently restore
  them — an intermittent loss of administrators with nothing connecting it to the
  issuer.
* **The allowlist fails closed**: an absent claim refuses the sign-in. Role
  mapping asks "should this person be promoted", where the safe answer under
  uncertainty is to change nothing; an allowlist asks "may this person in at
  all", where the safe answer is no. An allowlist that admitted everyone whenever
  the claim went missing would stop working exactly when the issuer is
  misconfigured.

The same tri-state governs LDAP: `memberOf` absent from an entry leaves the
stored role alone.

#### Which sessions may act as a cluster identity

`oidc` and `openshift` only — see `docs/adr-0007-impersonation.md`. The line is
not "did an external system authenticate them", which four of the six sign-in
methods satisfy. It is: **would the API server have derived this same username
and group list had the operator presented their own credential to it?** A
Kubernetes API server can be pointed at the same OIDC issuer; an OpenShift
cluster *is* the party that produced the name. A bare OAuth 2.0 userinfo field
and a SAML `NameID` are names this console chose the shape of, and putting one in
`Impersonate-User` is the invention ADR-0007 refuses. An operator who needs
impersonation behind a SAML IdP puts an OIDC broker in front of it and configures
that as the issuer for both the console and the API server.

Every provider's own username and groups are still captured on the session,
including the two that may not impersonate: which sources qualify is ADR-0007's
decision to revisit, and it cannot be revisited for a session that never carried
the values.

### 12.5 Sign-in rate limiting

`AUTH_THROTTLE_MAX_ATTEMPTS` failed sign-ins per username per
`AUTH_THROTTLE_WINDOW_SECONDS` (default 10 per 5 minutes). Exceeding it is
`429 too_many_attempts` with `context.retryAfterSeconds`. `0` disables it, which
leaves `POST /api/auth/login` an unmetered password oracle.

Counted from the §10 trail rather than from process memory, so the limit is a
property of the console rather than of the pod that answered: a per-process
counter resets on every restart and gives a three-replica deployment three times
the budget.

Checked **before** the password is verified, so a refused request costs one
indexed `COUNT` rather than a 310,000-round PBKDF2 verification — otherwise the
throttle still lets an attacker consume the console's CPU at the rate they can
send requests.

Keyed on the submitted username alone, not username plus source address. Adding
the address makes the limit trivially evadable from a botnet while doing nothing
about the case that matters. The cost is that one user can lock out their own
sign-ins for the window, which is recoverable by waiting.

**When the count cannot be taken, the sign-in proceeds.** A database failure that
started refusing every sign-in would lock every operator out of the console
during exactly the kind of incident when they need it, and the password check
still stands behind the throttle. The log names the consequence — "brute-force
protection is not in effect" — rather than a generic query error.

### 12.6 `/api/auth/users`

Administrator-only. `GET` returns the standard list envelope; `POST` creates a
local user; `PUT /{id}` updates profile, role, state, or a local password;
`DELETE /{id}` deactivates the account and revokes all sessions. Password hashes
never appear in responses. The current user and final active administrator
cannot be deactivated. Every non-local auth source has provider-managed passwords
and roles, refreshed at login **when the provider reports group membership** — when it
does not, the stored role is left alone rather than reset (§12.4). The `admin` role gates this
user-administration surface, §10.4's audit export, **and §3's three cluster
writes** — `POST /api/clusters`, `PUT /api/clusters/{id}`,
`DELETE /api/clusters/{id}`, because those decide which API server this
console's credential talks to, not what a person may read on it. Nothing else is
gated by role: both `admin` and
`user` identities retain the console's normal cluster capabilities, still
constrained by preflight and the deployment-wide mutation gate.

### 12.7 `/api/auth/sessions`

Administrator-only. `GET` returns the standard list envelope of every session
that can currently act on this console; `DELETE /{token_hash}` revokes one.

```json
{ "id": "3f7c…64 hex chars", "username": "erens", "display_name": "Eren S.",
  "auth_source": "ldap", "role": "admin", "ip_address": "10.4.1.22",
  "user_agent": "Mozilla/5.0 (Macintosh; …)", "created_at": "2026-08-18T08:00:00Z",
  "last_used_at": "2026-08-18T09:30:00Z", "expires_at": "2026-08-18T20:00:00Z",
  "current": true }
```

**`id` is the stored SHA-256 digest of the bearer token, and is not a
credential.** It is what `auth_sessions` is keyed by; the middleware hashes the
cookie it is given and looks the row up by the result, so possessing the digest
authenticates nobody. The raw token is stored nowhere and appears in no response.

**Why this surface exists at all, given §12.6.** An account that is active and a
session that is live are different facts. Deactivating an account revokes its
sessions, but the case this page is for is the opposite one — a live session
belonging to an account nobody wants deactivated, which is what a lost laptop
produces — and that session was reachable from no endpoint.

**Expired sessions are excluded, never listed and never deleted here.** They are
pruned lazily, when `load_session` next sees one, so a browser that was merely
closed leaves its row behind indefinitely. Counting those would answer "how many
people can act on this console right now" with a number that includes people who
cannot. Deleting them would make a `GET` a write.

**`ip_address` is the peer address and never an `X-Forwarded-For` claim.** It is
the same value §10 records. Believing that header requires a configured list of
trusted proxies, and without one it is a string the caller chose — an address an
attacker can set is worse than no address, because an administrator reads it as
evidence. `null` means unknown, which includes every session created before the
column existed, and the UI renders it as unknown rather than as a blank cell
(rule 11.2).

**`last_used_at` is coarse: at most one write per session per minute.** The
value is refreshed while resolving the session cookie, which happens on every
authenticated request, so writing it each time would put a database write in
front of every read the console serves. A UI must not imply second-level
precision from it. `null` means the session has not been seen since the column
existed — which is not "never used", and is a third state the listing keeps
distinct.

`current` marks the caller's own session: the one row whose revocation signs
them out. Revoking it is permitted and needs no special handling — the row is
gone, so the cookie the browser still holds resolves to nothing on the next
request, exactly as an expired session does, and the SPA's existing
`authentication_required` branch offers sign-in.

`DELETE` **records both terminal states** (§10, `console` category, verb
`delete`, resource `sessions`): `applied` naming whose session it was, and
`denied` when there was no such session. "I revoked that session" and "there was
no such session" are different answers, and a trail that renders the second as
the first tells an incident review that access was cut when it was not. A
`token_hash` that is not 64 hex characters is `422 invalid` rather than `404`:
a 404 would claim the shape was right and the row was missing.

### 12.8 `/api/auth/providers`

Administrator-only. `GET` returns the standard list envelope of every sign-in
method this build supports, configured or not; `PUT /{kind}` stores one kind's
configuration; `DELETE /{kind}` removes what is stored. `kind` is one of `ldap`,
`oidc`, `oauth`, `openshift`, `saml` — anything else is `422 invalid`, checked
before any database access. See [ADR-0011](adr-0011-database-backed-identity-providers.md).

```json
{ "name": "ldap", "title": "LDAP / Active Directory",
  "summary": "Search-and-bind against a directory: …", "caveat": null,
  "label": "LDAP / Active Directory",
  "enabled": true, "usable": true, "missing": [],
  "source": "database", "stored": true,
  "endpoint": "ldaps://directory.internal.example:636",
  "admin_group": "cn=platform-admins,ou=groups",
  "settings_prefix": "LDAP_", "editable": true,
  "fields": [ { "name": "url", "label": "Server URL", "type": "str",
                "secret": false, "required": true, "help": "…",
                "placeholder": "ldaps://directory.example.com:636" } ],
  "values": { "url": "ldaps://directory.internal.example:636", "start_tls": false },
  "secrets_stored": ["bind_password"], "secrets_unreadable": false }
```

This is the private counterpart of §12.1. Everything here — an issuer, a
directory URL, an API server address, a group DN — is precisely what the public
discovery endpoint withholds, because that one is unauthenticated and returning
these values there would let anyone who can reach the console enumerate its
identity infrastructure. It answers the question the Users table raises and
cannot: an `ldap` account with the `admin` role says nothing about *which*
directory it came from or which group promoted it.

**`enabled` and `usable` are separate fields and neither may be derived from the
other.** `enabled` is what an operator switched on. `usable` is whether a sign-in
through it can complete, and it is each provider module's own `enabled()` — the
same function the login page consults, so the two can never disagree. A provider
that is `enabled` and not `usable` gets no button (a button that leads to an
error reads as a broken console rather than an unconfigured one), and `missing`
names the required fields that are empty. A UI that rendered only `enabled` would
say "on" while nothing appeared where people sign in, with nothing anywhere
explaining the gap.

**Unconfigured methods are included, with `enabled: false`.** A list of only what
is switched on cannot distinguish "we have not set our SAML provider up" from
"this console cannot do SAML", and those call for different actions.

**No secret value is ever returned**, to anybody, including an administrator.
`secrets_stored` names the secret fields that **have** a stored value;
`secrets_unreadable` is true when a stored blob exists and could not be
decrypted, which means the encryption key changed. That third state is reported
rather than shown as configured, because "the bind password is set" sends an
administrator to debug the directory while the fault is the key.

`fields` is the kind's own schema, and the console's form is rendered from it.
That is normative because of what it prevents: a field the sign-in flow reads
that is missing from the screen that configures it.

#### `PUT /api/auth/providers/{kind}`

Body `{ "enabled": bool, "values": { … } }`. Create-or-replace: the kind *is* the
identity of the row, so there is no id to allocate and no `POST`/`409` pair.

The submitted values are **merged over what is stored before validation**, so a
form that sends four fields is not a request to blank the other ten, and
`required` is checked against the effective configuration rather than against the
fragment that arrived.

**An omitted secret keeps the stored one; an empty string clears it.** The
asymmetry is forced by the rule above it: the listing never returns a secret, so
the form has none to send back, and a plain replace would wipe the bind password
every time somebody corrected a typo in the search base — surfacing later as a
credential rejection from a directory that is fine.

**Validation happens at the write, not at the next sign-in.** Every rule mirrors
a refusal that already exists further in — plain `ldap://` without StartTLS, an
issuer that is not https, an OAuth or OpenShift endpoint that is not https, a
search filter with no `{username}` — moved to the moment somebody can still fix
it. `required` and those rules apply only to a row saved as **enabled**:
half-filling a provider and leaving it off is how a configuration is staged, and
refusing that would push an administrator back into editing a file.

#### `DELETE /api/auth/providers/{kind}`

`204`, or `404 not_found` when nothing is stored for that kind — "the stored
configuration is gone" and "there was nothing stored" are different answers, and
only the first changed how this deployment authenticates. What remains in effect
afterwards is the environment (see §12.4), which the UI must say **before** the
click.

Every state change is recorded in §10 as a `console` record against resource
`identity_providers` — `create`, `patch` or `delete` — **including the
refusals**, as `denied`. "Who tried to point our OIDC at a different issuer" is
the question this table is read for, and a validation error that left nothing
behind cannot answer it. The `detail` names the fields that were submitted and
never their values: the trail is append-only, so a bind password written into it
could not be removed afterwards.

---

## 13. Routes — exposing a Service to the outside world

One operator question — *what is reachable from outside, and where does it go* —
answered across the three unrelated APIs that can answer it. The console models
an **exposure** (a hostname, a path, one or more target Services, a decision
about TLS) in OpenShift's vocabulary, because it is the only one of the three
with a field for every part of it, and compiles that model down to whichever API
the cluster actually serves.

### 13.1 The three backends

| `backend` | Kind | Group | Notes |
|---|---|---|---|
| `openshift` | `Route` | `route.openshift.io` | Lossless. Every feature is one field. |
| `ingress` | `Ingress` | `networking.k8s.io` | Portable, and the lossiest. |
| `gateway` | `HTTPRoute` | `gateway.networking.k8s.io` | Weights and redirects are real fields; TLS belongs to the Gateway. |

Two of the three are CRDs whose served version depends on which release the
cluster installed, so the version is **resolved through discovery** and never
assumed. Pinning `gateway.networking.k8s.io/v1` 404s on a cluster serving
`v1beta1`, and a 404 reads as "this cluster has no HTTPRoutes".

### 13.2 Backend state is three-valued

`available` / `unsupported` / `unknown`, and the third is not a variant of the
second:

- `available` — discovery lists the resource.
- `unsupported` — discovery answered and it is not there. **An ordinary fact.**
- `unknown` — discovery could not answer for that group. **We do not know.**

`resolve()` already refuses to report `unsupported` for a group in its own
`unavailable` list; §13 only has to not undo that. Getting it backwards during
an aggregated-API outage tells an operator their Routes are gone, and the action
that follows is re-creating an exposure on a hostname that is already claimed.

**Only `unknown` makes the listing partial.** `unsupported` does *not* go in
`unavailable[]` — a cluster that does not serve `route.openshift.io` is not a
cluster whose Routes we failed to read, there are none, and raising the §1.2
banner on the Routes page of every non-OpenShift cluster would train operators
to ignore it. Per-backend state is reported in its own `backends[]` field.

### 13.3 The exposure features

Nine stable tokens. The frontend branches on them to disable a control **with
the reason** (§11.4), so they are contract, not an internal enum.

`edge-tls`, `passthrough-tls`, `reencrypt-tls`, `insecure-redirect`,
`insecure-allow`, `weighted-backends`, `wildcard-subdomain`, `generated-host`,
`path-exact`.

`GET /api/routes/capabilities` reports **all nine for every backend**, including
the unsupported ones — an absent key carries no reason, and §11.4 needs one.

### 13.3.1 `appDomain` — the cluster wildcard, and generated hostnames

`GET /api/routes/capabilities` carries one extra top-level key beside the §1.2
envelope:

```json
"appDomain": {
  "value": "apps.prod-eu.example.com",
  "source": "configured",
  "stored": "apps.prod-eu.example.com",
  "discovered": null,
  "pattern": "<name>-<namespace>.<domain>"
}
```

`value` is what a generated hostname is built under and is `stored || discovered`;
`source` is `configured`, `discovered`, or `null`. Both inputs are reported even
when they agree — "what you typed differs from what the cluster says" is the only
place a mistyped domain becomes visible.

**`value: null` means no hostname is generated at all,** and the form asks the
operator to type one. This is §0.1 applied to a hostname: a suffix under a
wildcard that does not resolve produces an exposure that is created, reports
Admitted, and routes nothing — §14's failure with a hostname in place of a
controller. A field left blank asks a question; a generated hostname answers it,
and answering it wrongly is worse than not answering.

**The pattern includes the namespace, and that is load-bearing.** Without it a
Service called `web` in two namespaces generates one hostname twice; most
controllers admit both and route to whichever won, which is an outage invisible
in either object.

**Discovery must not make the envelope partial.** `config.openshift.io` is absent
on every non-OpenShift cluster, so the read raises `unsupported` — which §13.2
already says does not belong in `unavailable[]`. The same sentence applies here
for the same reason: a §1.2 banner lit on every plain cluster is a banner nobody
reads. A discovery read that genuinely *failed* is still recorded, because "not
OpenShift" and "we could not ask" are different answers.

The client may build the hostname itself to fill the field as the operator
types. It is a suggestion only: the value travels as an ordinary `host` and is
compiled and validated server-side like any hostname that was typed, so the
backend remains the authority on what is accepted.

### 13.4 `GET /api/routes?namespace=&backend=&limit=`

The §1.2 envelope, plus `backends[]` (§13.2) and `truncated[]`. `continue` is
deliberately **not** propagated: three independent listings cannot share one
cursor, and a token meaning "page 2 of the Ingresses and page 1 of everything
else" produces a table that skips rows.

Each row:

```json
{
  "id": "ingress/prod/shop", "backend": "ingress", "kind": "Ingress",
  "group": "networking.k8s.io", "version": "v1", "plural": "ingresses",
  "name": "shop", "namespace": "prod",
  "hosts": ["shop.example.com"], "subdomain": null,
  "path": "/", "pathType": "Prefix", "paths": [ ... ],
  "targets": [{"service": "shop", "port": 80, "weight": null,
               "namespace": null, "kind": null}],
  "tls": {"termination": "edge", "insecurePolicy": null,
          "inlineCertificate": false, "secretName": "shop-tls"},
  "wildcardPolicy": null,
  "admitted": null, "admittedDetail": "...",
  "addresses": ["a1b2.elb.eu-west-1.amazonaws.com"],
  "ingressClass": "haproxy", "tlsHosts": ["shop.example.com"], "parents": [],
  "age_seconds": 86400, "resourceVersion": "4021"
}
```

**`admitted` is tri-state and the third value is the common one.**

- `true` — a router admitted it. When another shard *refused* it, that is named
  in `admittedDetail` rather than hidden: a Route admitted by `default` and
  refused by `internal` with `HostAlreadyClaimed` is served on one shard and not
  the other, and a bare green badge hides the half the operator came to find.
- `false` — every router that reported refused it, carrying the reason verbatim.
- `null` — **no router has reported.** Not a rejection. Also, permanently, the
  value for every Ingress: the Ingress API has no admission condition at all,
  and whether a controller took the object is visible only through `addresses`.

**`admitted` is not generation-scoped, and cannot be.** §6 refuses to believe a
workload's counts until `status.observedGeneration` matches `metadata.generation`.
`RouteIngressCondition` has no such field: there is nothing in a Route saying
which generation of the spec a router's verdict is about. An `Admitted: True`
written before the hostname was changed still reads as `True` afterwards. The
console does not synthesise a substitute from `lastTransitionTime` — that moves
when the condition's *status* changes, not when the spec does, so comparing it
would manufacture confidence from a timestamp that means something else. This is
a limitation of the Route API, stated rather than papered over.

`managedBy` reports whether something other than a person owns the exposure:
`controller` from an `ownerReferences` entry with `controller: true`
(definitive — something is reconciling it now), `tool` from a Helm, Argo CD or
Flux marker (advisory — whether an edit survives depends on that tool's drift
mode). All keys present and `null` throughout when nothing owns it.
`kubectl.kubernetes.io/last-applied-configuration` is deliberately **not** a
marker: it means somebody once ran `kubectl apply`, not that anything is
watching, and flagging it would warn on a large fraction of every cluster's
objects. `app.kubernetes.io/instance` is recognised but attributed to no tool,
because Helm, hand-written manifests and half the ecosystem all set it.

An edit to a controller-owned exposure succeeds, reports `applied: true`
truthfully, and is reverted seconds later. Both halves are true at once, which
is why the row carries this and the edit dialog says it before the diff.

`targets[].weight` is `null`, never `100`, on a single-backend exposure. A
weight rendered where no split was configured reads as one that was.

`targets[].namespace` and `targets[].kind` are `null` for a Route and an
Ingress — neither backend's target can name another namespace or a kind other
than Service, so there is nothing to report. For an HTTPRoute, they are read
straight off `backendRefs[]`: `namespace` is the referenced namespace when the
`backendRef` sets one (Gateway API requires a ReferenceGrant in the target
namespace for the write to take effect; this console does not evaluate
whether one exists — it reports the reference as written) and `null` when the
`backendRef` omits it, meaning the route's own namespace; `kind` is the
referenced kind (`Service` unless the `backendRef` names something else) and
`null` only if the object itself omits `kind`, which the Gateway API default
resolves to `Service`. A consumer matching a target against a known Service
should treat `namespace: null` as the route's own namespace and `kind: null`
as `Service`, never as "unknown".

A Route's `tls` reports `inlineCertificate: true|false` and **never the key**.
The key lives in the object's own spec — an OpenShift API design fact, not a
choice this console gets to make — but a list endpoint that echoed it would put
key material in a table.

### 13.5 `POST /api/routes/render` — compile, and write nothing

Body `{backend, spec?, document?}`. Returns
`{yaml, document, backend, kind, group, version, plural, lossy[], preserved[], requested[], verbatim}`.

Not preflighted and not audited, because nothing happens. It reads the cluster
only to resolve the served API version.

Two modes:

- **`spec` given** — the form's fields are compiled and patched **into**
  `document` rather than replacing it. A `spec.rules[1]` somebody hand-wrote, a
  controller annotation, an `externalCertificate` reference: all of it survives
  a trip through the form, and `preserved[]` names each path the form is not
  showing.
- **`spec` omitted, `document` given** — the document is taken **verbatim**.
  `lossy` and `preserved` are empty as statements of fact: the console compiled
  nothing, so it dropped nothing and hides nothing. This is what the YAML view
  sends once the operator has edited it, and without it a hand-edited document
  would have the form's fields recompiled over the top of it at write time.

### 13.6 `lossy[]`, and the acknowledgement that gates a write

An Ingress cannot express passthrough TLS. The compiler does **not** emit a
controller-specific annotation and hope, and it does not quietly downgrade. It
returns the object it can write plus:

```json
{"feature": "passthrough-tls",
 "label": "Pass TLS through to the pod without terminating it",
 "consequence": "The Ingress API cannot express passthrough. This exposure will be written with the router terminating TLS instead, so client certificates the pod expects will not arrive…",
 "mitigation": "Write this as an OpenShift Route if the cluster serves them, or use a Gateway API TLSRoute…"}
```

Every entry answers *what happens to my traffic* and then *what to do instead*,
in that order. A warning that answers only the first is one people click past.

**A write is refused with `422 invalid` unless `acknowledgeLossy` names every
entry.** The same shape as `force` on a node drain, and for the same reason:
consenting to a consequence is a separate act from requesting the change. The
list is per-feature, not a boolean, so a caller that acknowledged one
consequence and then edited the form must read the new one.

No compiler output ever contains a vendor annotation of its own
(`nginx.ingress.kubernetes.io/…`, `haproxy.org/…`, `traefik…`). Asserted by a
test rather than stated as policy — that is the exact pressure point where
"report it as lossy" gets quietly reversed into "guess the controller", and a
reversal would leave `lossy[]` claiming a feature was dropped while the object
silently carried it. An annotation the *operator* supplies is written, because
they chose it for a controller they know they are running.

### 13.7 Writes

- `POST /api/routes` — `{backend, spec?, document?, acknowledgeLossy[], dryRun}`
- `PUT /api/routes/{backend}/{namespace}/{name}` — the above plus a required
  `resourceVersion`
- `DELETE /api/routes/{backend}/{namespace}/{name}?dryRun=` — `dryRun` is a
  query parameter for the same reason §4's delete is

All three return the §1.5 mutation response, with an extra `route` object
carrying `{backend, kind, lossy, preserved, verbatim}`.

They delegate to `app.admin.apply.create_from_yaml` / `update_from_yaml` /
`delete_resource`, which is what routes them through the single funnel. A second
create path here would be a second place for the gate, the preflight, the dry
run and the audit row to be got right. What §13 adds is the audit *sentence*:
`create Route checkout: expose checkout:8080 at https://checkout.example.com/ (edge)`
rather than `create Route checkout`, because the question the trail is asked is
who exposed the payments service to the internet, and the generic sentence does
not answer it. A verbatim write says so instead —
`create Ingress prod/checkout (written verbatim from the YAML view)` — because
the console did not model the intent and must not narrate one it inferred.

**`routes/custom-host` is preflighted separately.** OpenShift gates *choosing a
hostname* behind its own RBAC subresource, distinct from creating the Route. The
funnel preflights `create routes`, that review passes, and the API server then
refuses the write — so without this the operator is told they cannot create
Routes, a permission the review just confirmed they hold. It is checked only for
`openshift` and only when `spec.host` is set (a generated hostname is not a
custom one).

**It is checked inside the funnel, after the gate.** §13 decides *whether* the
grant applies and hands the subresource to `mutate()`, which reviews it in the
same step as the verb on the object itself. Checking it earlier — as this once
did — puts it ahead of §1.6: on a read-only console a Route carrying a hostname
came back `rbac_denied`, sending an operator to widen a ClusterRole when the
deployment was not permitted to write at all, which is exactly the wrong-system
error `mutations_disabled` has its own code to prevent. The denial is audited
against `routes/custom-host` rather than `routes`, because a row naming the
object when the subresource was refused is the same misattribution written into
the trail.

---

## 14. The shipped router

k8boss-admin ships a reverse proxy and can install it. That is a deliberate
departure from *not a deployment engine*, and the shape of the departure is the
whole design: **the console writes manifests, and nothing else.**

There is no controller in the console process, no reconcile loop, and no desired
state stored anywhere. Install, upgrade and uninstall are each a sequence of
ordinary writes — one per object, each through `mutate()`, each gated,
preflighted, dry-run, diffed and audited exactly like a scale. What keeps the
router running is the Kubernetes control plane. What routes traffic is HAProxy's
own in-cluster controller. `GET /api/router` is a live read like every other page
in this console.

### 14.1 What it is, and what it does not serve

HAProxy Kubernetes Ingress Controller, pinned, as eight objects: Namespace,
ServiceAccount, ClusterRole, ClusterRoleBinding, IngressClass, ConfigMap,
Deployment, Service. `deploy/router.yaml` is the same bundle at default options,
generated by `make router-manifest` and enforced by a test, so
`kubectl apply -f` and pressing Install are demonstrably the same install.

`serves[]` reports, as facts about the software rather than about the
installation:

| backend | served | why |
|---|---|---|
| `ingress` | **yes** | It is an Ingress controller. This is the point of it. |
| `gateway` | **no** | The controller implements Gateway API for **TCPRoute only**. Enabling the Gateway API option grants the permissions and the controller name and it still will not accept an HTTPRoute. |
| `openshift` | **no** | Routes are served by OpenShift's own router, which an OpenShift cluster already runs. A second one contending for the same hostnames is how an outage starts. |

### 14.2 `GET /api/router`

A live read, never cached. `installed` is **tri-state**: `true`, `false`, or
`null` when one of the reads failed — reporting `false` during an API outage
invites an operator to install a second router on top of the one already
running. `deployment.present`, `service.present` and `ingressClass.present` are
tri-state for the same reason one level down.

`readyReplicas` is `null`, not `0`, when the Deployment controller has not
reported on the current generation — the §6 staleness rule applied to the one
workload whose health decides whether the cluster is reachable at all.

`deployment.gatewayApi` reports whether the **installed** router carries
`--gateway-controller-name`, read off its own arguments rather than remembered,
and is `null` — never `false` — when the Deployment could not be read. It exists
because §14.4's install options are not otherwise recoverable from the cluster,
and the reinstall form seeds itself from them: offered the bundle's defaults over
a router installed with different ones, an operator taking a version bump turns
their own choices off by confirming. `defaultClass` needs no equivalent — it is
already visible as `ingressClass.default`, on the object that carries it.

`otherClasses[]` lists every IngressClass on the cluster with
`{name, controller, default, managedByUs, retired}`, and is `null` — never `[]` —
when the listing failed: "nothing else is serving Ingresses here" is a real
claim and an unreadable listing does not support it. `retired` is matched on the
**exact** `spec.controller` string: `k8s.io/ingress-nginx` was retired in March
2026, and F5's NGINX Ingress Controller and NGINX Gateway Fabric are different,
supported products. Telling an operator their supported controller is retired is
the confidently-wrong answer aimed at their whole ingress path.

`upgradeAvailable` means **the installed version differs from the one this
console ships** — not that a newer HAProxy exists, which would be a claim about
a third party's release history made from a string baked into this repo, wrong
in both directions. `versionMatches` carries the same fact without the
directional word.

### 14.3 `POST /api/router/plan`

The manifests an install would create. **Pure and ungated**: nothing is written,
nothing is audited, and it renders on a console where router management is
switched off — because deciding whether to enable it requires reading what it
would create.

### 14.4 `POST /api/router` — install or upgrade

One endpoint for both; there is no difference in what happens. Each object is a
create if absent and a replace if it is already ours.

**The console never adopts an object it did not create.** Every bundle object
carries `app.kubernetes.io/managed-by: k8boss-admin`. An install that finds one
of the same name without it refuses with `409 conflict` — before writing
anything, on a dry run as much as on a real one, and the refusal is audited
because it happens before the funnel is reached.

**The refusal names every conflicting object, not the first one.** The ownership
scan reads all eight before it can decide, so the rest are already known and
withholding them is a choice — one that costs a round trip each: delete the
ClusterRole, install again, get refused on the Service, delete that, install
again, and nothing in any of those refusals says how many are left. So
`context.conflicts[]` carries one entry per object —
`{group, version, resource, namespace, name, kind}` — the `message` says how
many there are, and `detail` names each one with the `managed-by` value it
actually carries, which is usually what tells the operator whose it is. One
audit row per object, not one for the batch: the question the trail answers is
"did anyone try to install a router over *my* ClusterRole", asked by the person
who just found it, and a single row carrying a count cannot be found by the
object it was about. §33's OLM install names every conflict too, in its own
`context.conflicts[]` shape.

**A partial install is reported as one.** `installed` is false unless every
object landed; `failed` counts the rest; `objects[]` carries a per-object
outcome with the error and its hint. There is no rollback — deleting what
succeeded would be more writes the operator did not approve.

**A dry run says whose diff each object carries.** The API server's
NamespaceLifecycle admission refuses a create into a namespace that does not
exist, `dryRun=All` included, so a fresh install's dry run cannot project the
four objects inside the router's namespace. Each `objects[]` entry therefore
carries `projection`: `"server"` when the API server projected it (the
Namespace and the other cluster-scoped objects always; every object once the
Namespace exists), `"rendered"` when it is the bundle's own manifest diffed
against nothing because the Namespace does not exist yet, `null` when there is
no diff. A rendered entry also carries `preflight` — §9's `PreflightResult` for
the `create` the real install will use — so a missing grant surfaces before the
confirm rather than after the Namespace was created; it has no audit row, since
no request reached the cluster for it. A client labels the two differently: a
rendered manifest has not been through admission. Same rule as §17.5.

**RBAC escalation prevention is the one denial preflight cannot foresee.** A
`SelfSubjectAccessReview` on `create clusterroles` answers *yes*; the API server
then refuses the ClusterRole with `attempt to grant extra privileges`, because
the caller does not itself hold cluster-wide Secret reads. The status-code
mapping stands (it is `rbac_denied`); only the **hint** is rewritten, to name
escalation prevention and the `escalate`/`bind` grants instead of a verb the
operator demonstrably has.

Optimistic concurrency here is **weaker than §13's, on purpose**: the
`resourceVersion` a replace carries is the one read by the ownership scan
moments earlier, not one an operator was shown. Rule 4 holds — the API server
enforces it and the scan-to-apply window is closed — but there is no editor
here, so there is no "fresh diff against what you were looking at" to offer.

### 14.5 `DELETE /api/router`

Reverse order, and **the Namespace is left standing**, reported in `retained[]`
with the reason. A namespace can hold objects the console never put there and
deleting one is not recoverable. Objects that are not ours are `skipped[]`, not
deleted.

### 14.6 The gates

Two, both required for a real write: `ADMIN_ALLOW_MUTATIONS` and
`ADMIN_ROUTER_MANAGE_ENABLED` (off by default). Refusals are
`403 mutations_disabled` — not `rbac_denied`, because the operator's permissions
are irrelevant — and are audited.

**Dry runs are permitted with the feature gate off**, which is a deliberate
departure from §5.5's node debug pods. The difference is what the projection
*is*: a node debug pod's manifest is a working recipe for a privileged pod on a
deployment that switched the feature off; the router's manifests are a pinned
copy of a public upstream bundle, and reading them is the whole point.

The gate is checked **before the eight objects are read**, which is §1.6's step
one called early rather than a second gate: this endpoint reports every object it
touched, and a refusal that waited for the first write would be eight denial rows
and eight failed objects for one attempt.

---

## 15. The CLI session

A pod carrying `kubectl` (or `oc`), and §7's shell into it. For the one command
this console has no page for — `kubectl auth can-i --list`, `kubectl get --raw
/metrics`, an `oc adm` subcommand — where the alternative is leaving the console
for a laptop with a kubeconfig on it, which is the moment the trail stops.

**This API creates the pod. It does not serve the terminal.** The shell is
§7's `WS /api/ws/pods/{namespace}/{name}/exec`, unchanged, with `container` set
to the `container` this section returns. There is deliberately no CLI-specific
exec route: a second one would be a second place for the mutations gate, the
`create pods/exec` preflight and the open/close audit records to be got right.

### 15.1 What this section cannot promise

Every other write in this contract is preflighted for the exact permission,
projected with `dryRun=All`, shown as a diff and recorded in §10 naming the
object. **A command typed into this shell is none of those.** The trail records
that a session was opened on this pod, by whom, for how long and how many bytes
went through it (§7); it cannot record the `kubectl delete` typed into it.

A client must say so before creating the pod. An operator who has learned to
trust the diff will otherwise reasonably assume this surface has one.

### 15.2 The ServiceAccount is the whole permission story

`kubectl` inside the pod authenticates as the pod's ServiceAccount. What a shell
here can do is therefore that account's permissions — **not** the console's, and
**not** the signed-in operator's.

Kubernetes offers no RBAC verb covering *which* ServiceAccount a pod may bind: a
caller holding `create pods` in a namespace can bind any account in it,
including one far more privileged than themselves, and no ClusterRole can narrow
it afterwards. So `ADMIN_CLI_SERVICE_ACCOUNT` is the only control over this
feature's reach, it is a deployment setting rather than a per-user one, and this
API must never present it as anything else.

The default is `default` — the account every namespace has and which holds no
permissions. Out of the box `kubectl` in this pod is refused by the API server
for everything until a cluster admin deliberately binds a Role. A feature that
arrives useless rather than one that arrives dangerous.

### 15.3 `GET /api/cli`

A §1.2 envelope of the CLI pods this console created, plus:
```json
{ "items": [ {"name","namespace","image","serviceAccount","phase","state",
              "reason","node","started_at","created_at"} ],
  "continue": null, "remaining": null, "partial": false, "unavailable": [],
  "enabled": true,
  "enabledDetail": "CLI pods are enabled on this deployment.",
  "namespace": "default",
  "image": "alpine/k8s:1.34.9",
  "serviceAccount": "default",
  "container": "cli" }
```
- `enabled` is this deployment's **two** gates answered together, so a client can
  disable the action with the reason rather than offering it and taking a 403.
- `namespace`, `image` and `serviceAccount` describe what a **new** pod would be
  made of. A client must not guess any of them: they are what the operator is
  confirming, and the third decides what the shell can reach.
- `container` is the container name to pass to §7's exec socket.
- A row's `serviceAccount` is **`null`** when the pod names none and the API
  server defaulted it. That is not `"default"`: rendering it so would be a claim
  about what a shell in that pod may do, made from a field that was absent.
- The listing is a **read** and answers even when creating is gated off. That is
  what makes rule 11.4 possible here, and a pod left behind after the gate was
  switched off is the one that most needs finding.
- `items: []` is a real zero: the namespace was listed and holds none. A read
  that could **not** happen is an error, never an empty list (§0.1) — a client
  shown "no session" because the namespace was unreadable creates a second pod
  beside the one already running.

### 15.4 `POST /api/cli`

`{"image": null, "dryRun": true}` → the §1.5 mutation response plus `pod`,
`namespace`, `image`, `serviceAccount` and `container`.

There is **no ServiceAccount field**, deliberately: see §15.2. `image` is the
only thing a caller chooses, and everything else is fixed by the deployment.

The pod binds `ADMIN_CLI_SERVICE_ACCOUNT` with `automountServiceAccountToken:
true`, runs a shell loop rather than the image's entrypoint (which for a kubectl
image *is* kubectl and would exit immediately), sets `stdin` and `tty` so §7 has
a terminal to attach to, and drops every capability with
`allowPrivilegeEscalation: false` and the runtime's default seccomp profile. It
sets no host namespace, mounts no host path, and is not privileged — §5.5 is the
feature for that and is gated separately.

`before` is `null`, so the diff is the whole manifest as an addition. That is the
disclosure mechanism: `serviceAccountName` is on screen before the confirming
call.

- **Two gates.** `ADMIN_ALLOW_MUTATIONS` **and** `ADMIN_CLI_ENABLED`. Either off
  is `403 mutations_disabled` whose `hint` names both.
- **The dry run is permitted on a read-only console** and refused by the feature
  gate — the two switches behave differently on purpose, and each says which it
  is. §1.6's ordinary rule applies to the first, because this projection is a pod
  running `sleep` bound to an account named in the deployment's own configuration
  and inspecting it discloses nothing new. The second withholds both, because a
  deployment that switched this off has decided the console is not a kubectl
  terminal, and offering a preview of one is offering the feature.
- **The refusal is audited** as `outcome: "denied"`, once per attempt, by the
  funnel's step one — whichever of the two switches refused. Two rows for one
  attempt would make the count of "who tried" wrong in the one table that exists
  to answer it.
- **This endpoint always creates.** Reusing a Running pod is the client's
  decision, made from §15.3, because a POST that sometimes creates and sometimes
  does not cannot report `applied` honestly.
- **PodSecurity admission decides whether this is possible at all.** A namespace
  enforcing `restricted` wants `runAsNonRoot`, which this pod deliberately does
  not set — setting it would make an image whose user is root fail to start with
  a kubelet error naming a field the operator never chose. Admission runs on
  `dryRun=All`, so the refusal arrives at the preview step carrying the plugin's
  own message, before anything exists:

  ```
  403 rbac_denied
  detail: pods "k8boss-cli-3q27n" is forbidden: violates PodSecurity
          "restricted:latest": runAsNonRoot != true (pod or container "cli"
          must set securityContext.runAsNonRoot=true)
  ```

  **`403 rbac_denied`, not `422 invalid`.** Pod Security refuses with
  `Forbidden`, and §1.3 maps by status rather than by `Status.reason` — which is
  `Forbidden` here exactly as it is for a real RBAC denial, and is the reason
  that mapping exists. The code is therefore right and the advice it implies is
  wrong: nothing the operator can write in a ClusterRole fixes a namespace
  label. So the **hint** is rewritten to name Pod Security and
  `ADMIN_CLI_NAMESPACE` — `app.errors.podsecurity_hint`, shared with §5.5, which
  meets the same refusal for the same reason and used to describe it the same
  wrong way. It is the treatment §14.4 gives RBAC escalation prevention, and for
  the same reason. The code, the status and the API server's own detail are left
  alone.

### 15.5 `DELETE /api/cli/{pod}?dryRun=`

→ the §1.5 mutation response. The delete diff is §4's: `before=live, after=null`.

Only pods carrying this console's label; anything else is `404 not_found` from
this route rather than a delete, or it would be a namespaced pod-delete wearing
a friendlier URL.

> **Nothing removes this pod automatically, and that is not a gap this API can
> close.** A closed browser tab is not a signal, and a restarted console drops
> whatever would have issued the DELETE. `ADMIN_CLI_MAX_SECONDS` bounds how long
> the *container* runs — the kubelet stops it at the deadline and marks the pod
> Failed; the object stays and still has to be removed. So the honest answer is
> the same as §5.5's: label the pods, list them, bound them, and offer removal —
> not promise a cleanup that fails exactly when it matters.

Note what a pod left behind here holds: a live API credential for as long as it
exists. §5.5's leaked pod holds a node's filesystem; this one holds a token.

---

## 16. The operator portal

What this cluster's own catalogs offer, what somebody already subscribed to, and
the one object this console writes to add to that list: a `Subscription`.

**The console ships no catalog.** Every package in §16 comes from a
`CatalogSource` the cluster already runs. There is no bundled index, no curated
list, and no network call to anywhere but the API server. A cluster with no
catalogs has an empty portal, which is the true answer; a cluster with a private
mirror gets its own contents with no configuration here.

**And the console installs nothing.** It creates one `Subscription`. Operator
Lifecycle Manager — the cluster's software, not this console's — resolves it,
creates an `InstallPlan`, and installs a `ClusterServiceVersion`, on its own
schedule and only if the namespace and the approval strategy let it. §14 is a
departure from *not a deployment engine*; §16 is not a second one. It writes one
object into an API the cluster already serves, exactly as §4's YAML editor could,
with discovery and a pre-write check in front of it.
`docs/adr-0005-operator-portal.md` records the argument on both sides, including
what it costs.

### 16.1 What it is, and what it is not

| It does | It does not |
|---|---|
| Read PackageManifests and render what a channel would install | Ship, mirror or curate a catalog |
| Create one `Subscription`, through the funnel | Install an operator — OLM does that |
| Report whether OLM *can* install into the target namespace | Create the OperatorGroup that would let it |
| Report an outstanding InstallPlan and that it is holding the install | Approve one |
| Say what removing an operator actually takes | Uninstall one (§16.9) |

`applied: true` on §16.7 means one object was created. Nothing in that response
may be read as evidence that an operator is running. This is §0.3's rule about
dry runs pointed one API further out: the write succeeded, and the thing the
operator wanted has not happened yet.

### 16.2 The six OLM APIs, and three-valued source state

Two API groups, six resources. Every one is resolved through discovery and never
assumed: these are CRDs (and, for `packagemanifests`, an aggregated APIService),
and which version a cluster serves depends on which release of OLM it installed.

| `api` | Kind | Group | Versions tried | `required` | Without it |
|---|---|---|---|---|---|
| `packages` | `PackageManifest` | `packages.operators.coreos.com` | `v1` | **yes** | There is no catalog to browse |
| `subscriptions` | `Subscription` | `operators.coreos.com` | `v1alpha1` | **yes** | There is nothing to list and nothing to write |
| `clusterserviceversions` | `ClusterServiceVersion` | `operators.coreos.com` | `v1alpha1` | no | Every row's `phase` is `null` (§16.4) |
| `installplans` | `InstallPlan` | `operators.coreos.com` | `v1alpha1` | no | `approvalRequired` is `null` |
| `catalogsources` | `CatalogSource` | `operators.coreos.com` | `v1alpha1` | no | `catalogs` is `null`, never `[]` |
| `operatorgroups` | `OperatorGroup` | `operators.coreos.com` | `v1`, then `v1alpha2` | no | `target.ready` is `null` (§16.5) |

`operatorgroups` is tried at two versions because `v1alpha2` is what pre-0.17 OLM
served and long-lived clusters still do. Pinning `v1` 404s there, and a 404 reads
as "this namespace has no OperatorGroup" — the single most consequential wrong
answer this section can give, because it is the reason a subscription installs
nothing.

`sources[]` is reported on §16.3 and §16.4, one entry per API, **including the
ones that are fine**, because rule 11.4 needs a reason to put on a disabled
control and an absent key carries none:

```json
{"api": "packages", "kind": "PackageManifest",
 "group": "packages.operators.coreos.com", "version": "v1",
 "plural": "packagemanifests", "label": "Catalog contents", "required": true,
 "state": "available",
 "detail": "This cluster serves packages.operators.coreos.com/v1 packagemanifests."}
```

State is three-valued, exactly as §13.2's backends are, and the third is not a
variant of the second:

- `available` — discovery lists the resource. `version` names the one served.
- `unsupported` — discovery answered and it is not there. **OLM is not installed
  here. An ordinary fact**, rendered as a calm empty state and never as an error
  (§11.7).
- `unknown` — discovery could not answer. **We do not know** whether this cluster
  serves it. `version` is `null` rather than the first candidate: naming a version
  nobody confirmed is served lets a caller build a URL out of it.

**Only `unknown` makes the envelope partial**, and only `unknown` produces an
`unavailable[]` entry. A cluster without OLM is not a cluster whose operators we
failed to read; raising the §1.2 banner on every one of them is the banner nobody
reads. The `reason` token on the entry is the failure discovery actually raised —
`forbidden` when the read was refused, `unreachable` when the package server did
not answer — because those two send somebody to two different places.

A single `unknown` outranks any number of `unsupported` misses across the
candidate versions. Failing to read `v1` does not entitle the console to conclude
from the `v1alpha2` miss that the cluster has no OperatorGroups.

### 16.3 `GET /api/portal/catalog?limit=`

Every package this cluster's catalogs offer. The §1.2 envelope plus `sources[]`
(§16.2), `catalogs`, `truncated[]` and the §16.8 gate:

```json
{
  "items": [ PackageRow ],
  "continue": null, "remaining": null, "partial": false, "unavailable": [],
  "sources": [ SourceRow ],
  "catalogs": [ CatalogRow ],
  "truncated": [ {"kind": "PackageManifest", "shown": 500, "remaining": null,
                  "detail": "More packages are in the cluster's catalogs than are shown."} ],
  "enabled": false,
  "enabledDetail": "Subscribing is switched off on this deployment: ADMIN_PORTAL_INSTALL_ENABLED is not set. …"
}
```

Read **cluster-wide**, never per namespace. PackageManifests are namespaced
objects served by an aggregated API server, and listing them in one namespace
returns the global catalogs plus that namespace's local ones — so a
namespace-scoped read would silently omit another team's private catalog from a
page whose whole purpose is *what can this cluster install*.

`continue` is deliberately **not** propagated, for §13.4's reason with a second
one on top: three independent listings cannot share one cursor, and the package
server does not implement continuation at all, so a token handed back here would
be one this endpoint could not honour. What a bound left out is reported in
`truncated[]` instead of behind a paging control that lies.

A `PackageRow`:

```json
{
  "id": "olm/community-operators/prometheus",
  "name": "prometheus",
  "displayName": "Prometheus Operator",
  "provider": "Red Hat", "providerUrl": "https://…",
  "catalog": "community-operators", "catalogNamespace": "olm",
  "catalogDisplayName": "Community Operators",
  "defaultChannel": "beta", "channels": ["beta", "stable"],
  "version": "0.79.0",
  "summary": "Manages Prometheus and Alertmanager…",
  "categories": ["Monitoring", "Logging & Tracing"],
  "capabilityLevel": "Deep Insights",
  "certified": null,
  "installModes": ["OwnNamespace", "SingleNamespace", "AllNamespaces"],
  "hasIcon": true,
  "installed": true,
  "installations": [ SubscriptionRow ]
}
```

- `version`, `summary`, `categories`, `capabilityLevel`, `certified` and
  `installModes` are read off the **default channel's** CSV description. All are
  `null` (or `[]` for `categories`) when the catalog published none for it — a
  real state for a pruned catalog, and not the same as version zero.
- `certified` and `capabilityLevel` are the **publisher's own claims**, carried
  verbatim from CSV annotations because this console has no way to verify either.
  `certified` is tri-state: `null` for absent *and* for an annotation that is
  neither spelling of a boolean. Guessing `false` from a parse failure would
  render "not certified" for an operator whose publisher wrote `True` — a claim
  about somebody else's software made from a string this console failed to read.
- `installModes` here is the *supported* mode names only, and is `null` — never
  `[]` — when the catalog published no `installModes` block. An empty list would
  read as "this operator supports no install mode", which is a claim no
  PackageManifest makes.
- `hasIcon` says whether §16.10 has an icon to serve for this package — it is
  **not** the icon. The bytes stay off this row for the reason `description`
  does: a logo is kilobytes, a catalog is hundreds of packages, and a listing
  that inlined one apiece is a response nobody can use. It is a plain boolean
  rather than a tri-state because nothing acts on it: a package whose catalog
  published no CSV description published no icon either, and the cost of being
  wrong is a placeholder tile. **It is also a promise:** `true` means the media
  type is one §16.10 will actually hand back, so a row never sends a client to
  fetch an image the endpoint then refuses.
- `installations` is every Subscription on the cluster naming this package, keyed
  on `spec.name` alone rather than on the catalog triple: an operator installed
  from a mirror is still installed.

**`installed` is tri-state, and `false` is a claim.**

- `true` — the Subscription listing succeeded and matched.
- `false` — the listing succeeded and matched nothing.
- `null` — **the Subscription listing did not happen**, because OLM's
  Subscription API was `unknown` or the read failed. `installations` is `null`
  with it.

A client must never render `null` as "not installed". This is not a cosmetic
error here: the operator subscribes again, and a second Subscription for the same
package is how two ClusterServiceVersions end up racing to own the same CRDs. The
same reasoning is why a truncated Subscription listing produces a `truncated[]`
entry saying, in as many words, that a package shown as not installed may be
installed by one that was not seen.

`catalogs` is `null` — **never `[]`** — when the CatalogSource listing did not
happen, because "this cluster has no catalogs" is a real claim and an unreadable
listing does not support it. A `CatalogRow`:

```json
{"id": "olm/community-operators", "name": "community-operators",
 "namespace": "olm", "displayName": "Community Operators",
 "publisher": "Red Hat", "sourceType": "grpc", "image": "quay.io/…",
 "state": "READY", "healthy": true,
 "detail": "The catalog's last observed connection state is READY.",
 "age_seconds": 86400}
```

`healthy` is `null` until the catalog operator publishes a connection state. A
freshly created CatalogSource has no status at all, and reporting that as
unhealthy sends somebody to debug a registry that is merely still starting.

### 16.4 `GET /api/portal/subscriptions?namespace=&limit=`

What somebody already installed. The §1.2 envelope plus `sources[]`,
`truncated[]` and the §16.8 gate. Omit `namespace` to read every namespace this
deployment may read.

Each Subscription is joined to the ClusterServiceVersion it names and to its
outstanding InstallPlan. The join is by **`(namespace, name)`**, not by name: OLM
copies a CSV owned by an all-namespaces OperatorGroup into every other namespace,
and a copy in `kube-system` is not the installation a Subscription in
`monitoring` is waiting for.

A `SubscriptionRow`:

```json
{
  "id": "monitoring/prometheus", "name": "prometheus", "namespace": "monitoring",
  "package": "prometheus", "channel": "beta",
  "catalog": "community-operators", "catalogNamespace": "olm",
  "installPlanApproval": "Automatic", "startingCSV": null,
  "installedCSV": "prometheusoperator.0.79.0",
  "currentCSV": "prometheusoperator.0.79.0",
  "state": "AtLatestKnown",
  "phase": "Succeeded", "phaseDetail": "install strategy completed with no errors",
  "approvalRequired": false,
  "installPlanDetail": "InstallPlan install-x7kd2 is Complete.",
  "installPlan": "install-x7kd2",
  "conditions": [{"type": "CatalogSourcesUnhealthy", "status": "True",
                  "reason": "…", "message": "…"}],
  "age_seconds": 86400
}
```

**`spec` and `status` are kept apart on purpose.** `channel`,
`installPlanApproval` and `startingCSV` are what somebody asked for.
`installedCSV` is the only evidence anything was installed. A row that merged
them would report an operator as installed the moment somebody typed its name —
the §1.5 rule that a successful dry run is not a write, one API further out.

**`installedCSV: null` means OLM has installed nothing for this Subscription
yet.** It is not a failed read, and it is not a failed install.

**`phase` is tri-state, and there are two different reasons it is `null`.**
`phaseDetail` always carries the sentence saying which:

| `phase` | When | `phaseDetail` says |
|---|---|---|
| a CSV phase (`Succeeded`, `Installing`, `Failed`, …) | The CSV was read | the CSV's own `status.message` or `reason` |
| `null` | **The CSV listing failed or was truncated** | that the read did not happen, and that this is not a failed install |
| `null` | **OLM has published no `installedCSV`** | that resolution may be pending, an InstallPlan may be waiting for approval, or the namespace may have no OperatorGroup |
| `null` | The Subscription names a CSV that is not in the namespace | which CSV, and which namespace it is missing from |

A client renders `phase` through the tri-state cell with
`reason={row.phaseDetail}`. It must **never** default it to `Failed` or to a
blank: reporting a healthy operator as broken during an API outage is the defect
standard aimed at the one column an operator reads to decide whether to
reinstall.

A truncated CSV or InstallPlan listing is treated as *did not read* for the whole
join, not just for the rows that fell outside the page, and says so in
`truncated[]`. A Subscription whose CSV was on page two reports an unknown phase
rather than a missing installation.

`approvalRequired` is tri-state for the same reason one field over: `null` when
the InstallPlan listing did not happen, or when the Subscription names an
InstallPlan that is not in the listing. `true` means the install is stopped dead
until somebody approves it — which this console does not do (§16.9).

`conditions[]` carries the Subscription conditions that are currently `True`,
verbatim rather than collapsed into a boolean. `ResolutionFailed` and
`CatalogSourcesUnhealthy` are the two that answer *I subscribed and nothing
happened*.

### 16.5 `POST /api/portal/subscriptions/plan`

The Subscription that would be created, and what will stop it.

```json
{"package": "prometheus", "namespace": "monitoring",
 "channel": null, "catalog": null, "catalogNamespace": null,
 "installPlanApproval": "Automatic", "startingCSV": null}
```

`channel` defaults to the package's `status.defaultChannel`. Naming a channel the
package does not publish is `422 invalid` listing the ones it does — rather than
silently subscribing to the default and installing something nobody chose.
`catalog` is optional and required in one case: when two catalogs offer a package
of the same name, the plan refuses to guess. They are different software with
different publishers, and picking the first would subscribe to whichever one the
API server happened to list first.

**Ungated and it writes nothing** — no preflight, no audit row, and it renders on
a console where `ADMIN_PORTAL_INSTALL_ENABLED` is off (§16.8). It is a `POST`
only because its body is a request, not a resource path; a client's method for it
must be retryable the way §13.5's render and §14.3's plan are, or a transient
`503` makes the operator refill the form.

The response:

```json
{
  "package": "prometheus", "namespace": "monitoring", "channel": "beta",
  "catalog": "community-operators", "catalogNamespace": "olm",
  "installPlanApproval": "Automatic",
  "displayName": "Community Operators", "provider": "Red Hat",
  "defaultChannel": "beta",
  "channels": [ ChannelPayload ], "selected": ChannelPayload,
  "target": { … },
  "existing": [ SubscriptionRow ],
  "consequences": [ … ],
  "document": "apiVersion: operators.coreos.com/v1alpha1\nkind: Subscription\n…",
  "partial": false, "unavailable": [],
  "enabled": false, "enabledDetail": "…"
}
```

A `ChannelPayload` is one channel in full —
`{name, currentCSV, version, displayName, summary, description, installModes,
minKubeVersion, ownedCustomResources, containerImage, repository,
capabilityLevel, certified, categories, provider}`. `installModes` here is the
full `[{type, supported}]` list, `null` when the catalog published none.
`ownedCustomResources` is `[{kind, name, version, description}]` — the CRDs the
channel's CSV declares it owns, and the visible half of what installing this
operator does to a cluster, listed before the write rather than discovered
afterwards in the API explorer. `description` is the catalog's long-form text,
carried on the channel rather than on a §16.3 row because it is routinely tens of
kilobytes and a listing of three hundred packages carrying it is a response
nobody can use. It is the publisher's copy and is rendered as text (§16.9).

`document` is the object that would be created, rendered as YAML: named after the
package, with the caller's channel, catalog, approval strategy and optional
`startingCSV`, and **nothing else**. No labels of this console's own, no
annotations, no `config` block. An operator reading the diff sees exactly the
fields they filled in, and this console leaves no fingerprint on an object OLM
will go on to manage. Naming it after the package is what OLM's own tooling does,
so a Subscription created here and one created with `kubectl` collide rather than
quietly coexisting — and a collision is a `409` the operator can read.

**The target namespace verdict.** OLM installs an operator only into a namespace
governed by **exactly one** OperatorGroup whose scope the operator supports.
Getting either wrong produces a Subscription that is created and installs
nothing — §14's failure with a namespace in place of a controller. `target`
reports it before the write:

```json
"target": {
  "namespace": "monitoring",
  "operatorGroups": [ {"name": "monitoring-og", "namespace": "monitoring",
                       "targetNamespaces": ["monitoring"],
                       "publishedNamespaces": ["monitoring"],
                       "selector": false, "allNamespaces": false} ],
  "requiredInstallMode": "OwnNamespace",
  "ready": true,
  "detail": "OperatorGroup monitoring-og requires OwnNamespace, which this channel supports."
}
```

**`ready` is tri-state and `null` is the common third state.** It is `true` only
when every check actually ran and passed:

- `false` — the namespace has no OperatorGroup, or more than one, or the required
  install mode is one this channel does not declare.
- `null` — the OperatorGroup listing could not be read, or the group selects its
  namespaces **by label** (which this console does not evaluate, so
  `requiredInstallMode` is `null` too), or the catalog published no install modes
  to check against.

`operatorGroups` is `null` when the listing did not happen and `[]` when the
namespace genuinely has none. Those are the two answers that must never be
confused: `[]` means OLM will refuse to install, `null` means we do not know, and
reporting the second as the first puts a blocking warning in front of a namespace
that is perfectly configured. An `OperatorGroupRow`'s `allNamespaces` is `null`
for a selector-based group for the same reason — an empty target list reported as
"all namespaces" is a different group.

`existing` is the Subscriptions in the target namespace already naming this
package, or `null` when that listing did not happen.

**The consequence codes.** A closed set — the frontend renders one control per
entry and §16.7 refuses unless every code is acknowledged by name, so a new code
is a contract change rather than a new string:

| `code` | Raised when | What it means for the install |
|---|---|---|
| `no_operator_group` | The namespace has zero OperatorGroups | The Subscription is created; OLM marks the CSV `Failed` with `NoOperatorGroup`. **Nothing installs, and the operator keeps looking subscribed.** |
| `too_many_operator_groups` | The namespace has more than one | `TooManyOperatorGroups`, nothing installs — and **every operator already in that namespace is affected too**, not only this one |
| `operator_group_unknown` | The OperatorGroup listing could not be read | Unknown. The Subscription is created either way; if the namespace has none, it fails as above |
| `install_mode_unsupported` | The group's scope requires a mode this channel does not declare | `UnsupportedOperatorGroup`, nothing installs |
| `install_modes_unknown` | The catalog published no install modes, **or** the group selects namespaces by label | The scope check could not run. If the operator does not support the scope, `UnsupportedOperatorGroup` |
| `already_subscribed` | A Subscription for this package already exists in the namespace | The create is refused with a conflict. Written under another name, two Subscriptions would resolve the same package independently |
| `subscriptions_unknown` | That listing could not be read | Unknown. If one already exists, a second leaves two resolutions competing for the same custom resources |
| `manual_approval` | `installPlanApproval: "Manual"` | OLM creates an InstallPlan and stops. **Nothing installs, and nothing upgrades later,** until it is approved — which this console does not do |

Every entry carries `{code, label, consequence, mitigation}` and answers *what
happens to my cluster* before *what to do instead*, in that order (§13.6). Note
that three of the eight — the `_unknown` codes — report a check that **could not
run** rather than one that failed, and they are acknowledgeable on exactly the
same terms as the rest. Letting a check that did not happen pass silently is the
swallowed `[]` of §0.1 wearing a different shape, and a silent `null` here is a
Subscription that installs nothing.

The plan's own reads fail independently. An unreadable OperatorGroup listing
costs `target.ready` and adds an `unavailable[]` entry; the rendered document is
returned regardless. `partial` and `unavailable` are the §1.2 fields on an object
rather than a collection.

### 16.6 `consequences[]`, and the acknowledgement that gates a write

**§16.7 is refused with `422 invalid` unless `acknowledgeConsequences` names
every code the plan returned.** `context.unacknowledged` lists the missing ones
and the `hint` names them.

The key is `consequences`, not `warnings`, and the name is load-bearing rather
than cosmetic: §1.5 already owns `warnings` for the API server's own `Warning:`
headers on this create. A §16 list written into that key would drop a
deprecation notice about the very object being created, which is the one place
an operator would actually want to read one.

This copies §13.6's `acknowledgeLossy` deliberately, and for the same reason:
consenting to a consequence is a separate act from requesting the change. The
list is per-code, not a boolean, so a caller that acknowledged one set and then
changed the namespace has to read the new one. Extra codes are accepted; missing
ones are not.

**The plan is recomputed inside the write**, never trusted from the caller. A
client that read a plan, edited the namespace and posted the old acknowledgements
would otherwise be consenting to consequences that no longer describe the write.

### 16.7 `POST /api/portal/subscriptions`

The §16.5 body plus `acknowledgeConsequences[]` and `dryRun` (default `true`).
Returns the §1.5 mutation response — `{dryRun, applied, verb, target, diff,
resourceVersion, warnings, auditId}` — with §16's own fields riding along:

```json
{
  "dryRun": false, "applied": true, "verb": "create",
  "diff": {"before": null, "after": "apiVersion: …", "unified": "…", "changed": true},
  "resourceVersion": "40219",
  "warnings": [],
  "auditId": 8817,
  "package": "prometheus", "channel": "beta", "catalog": "community-operators",
  "installPlanApproval": "Automatic",
  "expectedCSV": "prometheusoperator.0.79.0", "expectedVersion": "0.79.0",
  "target": {"group": "operators.coreos.com", "version": "v1alpha1",
             "resource": "subscriptions", "namespace": "monitoring",
             "name": "prometheus"},
  "installTarget": { "namespace": "monitoring", "operatorGroups": [ … ],
                     "requiredInstallMode": "OwnNamespace", "ready": true, "detail": "…" },
  "consequences": [ … ],
  "partial": false, "unavailable": []
}
```

**Nothing here overwrites a §1.5 key, and the two near-misses are named on
purpose.** `target` is §1.5's own — the group-version-resource this write
addressed, and what the audit row is filed under. §16.5's namespace verdict
rides alongside it as `installTarget`, recomputed at write time. `warnings` is
likewise §1.5's own, carrying the API server's `Warning:` headers for this
create; §16's list is `consequences` (§16.6), in §16.5's shape.

Both were briefly the same key during development, and both were wrong for the
same reason: a response that reuses a §1.5 name for a different value is one a
generic mutation client reads without noticing, and the value it silently loses
here would be a deprecation notice about the very object being created.

`diff.before` is `null`, so the diff is the whole Subscription as an addition —
which is the disclosure mechanism: the catalog, the channel and the approval
strategy are on screen before the confirming call.

It delegates to `app.admin.apply.create_from_yaml`, which is what routes it
through the single funnel — one gate, one preflight on `create subscriptions` in
the target namespace, one `dryRun=All` projection, one diff, one audit row. There
is no §16-specific write path. What §16 adds is the audit *sentence*:
`subscribe to prometheus channel beta from catalog community-operators (Automatic
approval)`, because the question the trail is asked is who put third-party
software on this cluster, and `create Subscription prometheus` does not answer
it.

> **`applied: true` means the Subscription object exists. That is all it means.**
>
> It is not a claim that an operator is installed, that a ClusterServiceVersion
> was created, that anything is running, or that anything will be. OLM resolves
> the Subscription afterwards, on its own schedule, and only if the namespace has
> exactly one OperatorGroup of a scope the operator supports, the catalog is
> reachable, the CSV's dependencies resolve, and — under `Manual` approval —
> somebody approves the InstallPlan. `expectedCSV` and `expectedVersion` are what
> OLM is *expected* to install, carried so a client can say what to look for
> next; neither is evidence that it did. §16.4 is where that question is actually
> answered, and a client must send the operator there rather than letting a green
> toast imply an installation.

### 16.8 The gates

Two, both required for a real write: `ADMIN_ALLOW_MUTATIONS` and
`ADMIN_PORTAL_INSTALL_ENABLED` (off by default). Refusals are
`403 mutations_disabled` — never `rbac_denied`, because the operator's
permissions are not what is stopping this and telling them otherwise sends them
to edit a ClusterRole that is already correct — and they are **audited** as
`outcome: "denied"` against the Subscription the write would have created,
because a refusal that left no row is a hole in the trail at exactly the moment
somebody asks who tried.

The gate is checked **before the catalog is read**, the way §14's install gates
before it builds: a caller whose deployment forbids this gets
`mutations_disabled` rather than a `404` about a package they were never going to
be allowed to subscribe to.

Both gates are answered together as `enabled` / `enabledDetail` on §16.3, §16.4
and §16.5, in §14's shape, so a client can disable the Subscribe control **with
the reason** from the first paint (§11.4) rather than offering it and taking a
403.

The second gate exists on its own because of what a Subscription hands over. OLM
grants the operator's ClusterServiceVersion whatever RBAC it asks for — routinely
cluster-wide — and that grant is made by OLM, not by the caller. An operator may
reasonably want every other write in this console without wanting it to be the
place third-party software enters their cluster.

**Dry runs are permitted with the feature gate off**, and so is §16.5's plan.
This is §14.6's departure from §5.5, for §14.6's reason: what the projection *is*
decides it. A node debug pod's projected manifest is a working recipe for a
privileged pod on a deployment that switched that feature off. A Subscription's
projection is the caller's own request rendered as an object, plus the contents
of a catalog the cluster already publishes — nothing privileged, and an operator
deciding whether to set `ADMIN_PORTAL_INSTALL_ENABLED` has to be able to read
what it would let the console create.

### 16.9 What §16 does not do

**No uninstall.** Deleting a Subscription does not remove an operator: the
ClusterServiceVersion it created stays, and so does everything that CSV owns —
the Deployment, the CRDs, the cluster-wide RBAC. A Remove button that deleted
only the Subscription would report an uninstall that did not happen, which is the
one thing this project's defect standard refuses. §16 says what removing actually
takes and leaves both deletions to §4, where each is its own diff and its own
audit row. The asymmetry is real: it is easier to add an operator here than to
remove one.

**No InstallPlan approval.** An outstanding one is reported (§16.4's
`approvalRequired`) and warned about before the write (`manual_approval`), and
approving it is an ordinary §4 write in the API explorer. Approving an InstallPlan
is consenting to a specific resolved set of ClusterServiceVersions, and a
one-click button on a row would be that consent given without the diff.

**No OperatorGroup creation.** §16.5 reports that the namespace has none and what
that costs; it does not create one. An OperatorGroup decides which namespaces
every operator in that namespace may act on, including ones already installed —
it is a scope decision about the namespace, not a step in subscribing to one
package.

**No catalog of its own.** No bundled index, no curation, no upstream fetch, no
ranking. `certified`, `capabilityLevel` and `provider` are the publisher's own
claims rendered verbatim (§16.3), and no badge, score or ordering of this
console's own is layered over them.

**No markdown.** A client renders `ChannelPayload.description` as **text**, never
as markdown or HTML. It is third-party content displayed inside an authenticated
administration console, reachable by anyone who can get a package into a catalog
the cluster trusts — and formatted text is more persuasive than plain text at
exactly the moment persuasion is the risk.

**No OLM v1.** This section speaks OLM v0 — `operators.coreos.com` Subscriptions
and `packages.operators.coreos.com` PackageManifests. OLM v1's
`olm.operatorframework.io` `ClusterExtension` is a different API with a different
model, and a cluster running only it gets `state: "unsupported"` across §16.2 and
a calm empty portal. That is the correct answer today and a gap that will grow.

### 16.10 `GET /api/portal/catalog/icon?package=&catalog=&catalogNamespace=`

One package's icon, as image bytes — `Content-Type` from the catalog's own
`mediatype`, body the decoded `base64data` off the default channel's CSV
description. Not an envelope: the response **is** the image, so a client can
point an `<img>` at it.

A request per icon rather than base64 on every §16.3 row, for the reason that
row carries no `description`. The browser asks for the ones it is painting and
caches them; the listing stays the size it was.

`catalog` and `catalogNamespace` narrow the same way §16.5's do. Two catalogs may
offer one package name and ship different logos for it, and the icon that
appears on a tile has to be the icon of the package that tile subscribes to.

**The bytes are somebody else's**, decoded out of an image the cluster pulled
from a registry this console does not control, and three things bound what
leaves here:

- **The media type is an allowlist**, never the catalog's own string echoed into
  a header: `image/svg+xml`, `image/png`, `image/jpeg`, `image/gif`,
  `image/webp`. A catalog naming `text/html` would otherwise get this console to
  serve third-party markup from its own origin. An icon outside the allowlist is
  a `404` and — because §16.3's `hasIcon` is built from the same allowlist — was
  never advertised in the first place.
- **The decoded size is capped** (1 MiB). A logo is kilobytes; the cap exists
  because the input is untrusted, not because a real icon approaches it.
- **The response refuses to execute.** `X-Content-Type-Options: nosniff` and
  `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline';
  sandbox`. SVG is in the allowlist because operator logos overwhelmingly are
  SVG, and an SVG can carry script — so it is served in a way that cannot run
  any, and a client renders these in an `<img>`, which does not execute script.
  This is the §16.9 "no markdown" rule applied to the other third-party asset on
  the page.

`404 not_found` when the catalog published no icon. **Not an error state for the
operator**: catalogs routinely ship none — operatorhub.io's community catalog
publishes an icon for not one of its packages — and a client draws its own
placeholder rather than a broken image. `Cache-Control` is `private, max-age=300`
rather than immutable: a catalog can be re-pointed at a new image under the same
package name, and a long cache would pin a logo the cluster has stopped serving.

---

## 17. Projects — a namespace read with what governs it, and created with it

OpenShift has a *Project*: a namespace together with the ResourceQuota, the
LimitRange, the security posture and the RoleBindings that make it a place a
team can be handed, and `oc new-project` creates all of them at once from a
project request template. Vanilla Kubernetes has every one of those objects and
neither the page nor the act. §17 is both, built out of things this console
already does: five reads joined into one page, and five ordinary creates through
the funnel.

### 17.1 What it is, and what it is not

**It is a read model and a planned multi-object write.** Nothing is stored,
nothing reconciles, nothing is shipped or pinned: the defaults live in the
request the client sends, and what keeps the objects there afterwards is the
cluster. `docs/adr-0006-projects.md` records why this is five writes and not a
template engine, and why it does not cross ADR-0004's boundary.

**The console never adopts a namespace.** A project is created into a namespace
that does not exist. One that does — whoever created it, whatever it holds — is
refused with `409 conflict` naming it, before anything is written and on a dry
run too, and the refusal is audited. Adding quotas, limits or bindings to an
existing namespace is §4's job, one object and one diff at a time.

**No deletion, no edit, no template.** §17 creates; it does not delete a
namespace (that is `kubectl delete namespace`, unrecoverable, and §4's delete
with its own confirmation), does not edit one, and holds no project template of
its own — the request body *is* the template, and a client that wants
organisation-wide defaults keeps them where its other defaults live.

### 17.2 `GET /api/projects/{name}`

The §5 namespace row, plus:

```json
{
  "name": "payments", "status": "Active", "labels": {}, "annotations": {},
  "age_seconds": 91234, "pod_count": 12, "creationTimestamp": "…",
  "displayName": "Payments", "description": null,
  "podSecurity": {
    "enforce": "restricted", "enforceVersion": null,
    "audit": null, "auditVersion": null,
    "warn": "baseline", "warnVersion": "v1.31",
    "labelled": true
  },
  "quotas": [{
    "name": "project-quota", "namespace": "payments", "scopes": [], "scoped": false,
    "reconciled": true, "age_seconds": 91000,
    "resources": [
      {"resource": "pods", "hard": "20", "used": "20", "hard_value": 20, "used_value": 20, "exhausted": true},
      {"resource": "requests.memory", "hard": "8Gi", "used": null, "hard_value": 8589934592, "used_value": null, "exhausted": null}
    ]
  }],
  "limitRanges": [{
    "name": "project-limits", "namespace": "payments", "age_seconds": 91000,
    "limits": [{"type": "Container", "max": {}, "min": {},
                "default": {"cpu": "500m"}, "defaultRequest": {"cpu": "100m"},
                "maxLimitRequestRatio": {}}]
  }],
  "roleBindings": [ RoleBindingRow ],
  "networkPolicies": {"count": 1, "names": ["allow-same-namespace"],
                      "isolatesAllIngress": true, "isolatesAllEgress": false},
  "partial": false, "unavailable": []
}
```

- **The namespace read is primary** and raises (`404 not_found`, `403
  rbac_denied` with a hint). Everything else is a **secondary** read, collected
  on its own: `pod_count`, `quotas`, `limitRanges`, `roleBindings` and
  `networkPolicies` are each `null` — never `[]`, never `0` — when their listing
  did not happen, with the reason in `unavailable[]` and `partial: true`.
  `quotas: []` is the finding that nothing bounds the namespace; `quotas: null`
  is not that finding, and a client must not render it as one.
- `displayName` and `description` are the `openshift.io/display-name` and
  `openshift.io/description` annotations, which §17.5 writes on vanilla
  clusters deliberately: they are inert there, and a namespace created here
  shows the same name in an OpenShift console or in tooling written for one.
- **`podSecurity` is read off labels and stated as such.** Each mode is the
  `pod-security.kubernetes.io/<mode>` label's value or `null`; `<mode>Version`
  is the `-version` label or `null`; `labelled` is true when any mode label is
  present. `null` means *this namespace declares nothing for this mode* — the
  cluster-wide default from the API server's `AdmissionConfiguration` applies,
  and no API serves that file, so the console cannot say what it is. It is
  **never** rendered as `privileged`. A label carrying a value outside
  `privileged`/`baseline`/`restricted` is returned verbatim, because admission
  will refuse every pod in the namespace over it.
- **`quotas[].resources[].used` is `null` until the quota controller writes
  `status.used`**, which happens asynchronously after the object is created; a
  ResourceQuota seconds old has `spec.hard` and no status. `reconciled` is
  whether `status.hard` exists at all. `hard_value`/`used_value` are the
  quantities parsed (`1500m` and `1.5` are the same quantity; the strings are
  kept beside them because a diff of the strings would say otherwise).
  `exhausted` is `true` when both parsed and used has reached hard, `false` when
  both parsed and it has not, and `null` otherwise. `scoped` is true when a
  `scopeSelector` narrows which pods the quota counts, so a client can say the
  numbers cover a subset.
- **`networkPolicies.isolatesAllIngress`** is `true` when some policy with an
  *empty* `podSelector` declares `Ingress` in its policy types — the "default
  deny" shape and the only namespace-wide statement a listing can make. `false`
  means no such policy exists and says nothing about narrower ones. `null` means
  a selector could not be evaluated. Every one of those is a statement about what
  is *declared*; §8.3's rule that enforcement belongs to the CNI plugin applies
  unchanged.

### 17.3 `POST /api/projects/plan`

Body:

```json
{
  "name": "payments",
  "displayName": "Payments", "description": "…",
  "podSecurity": {"enforce": "restricted", "audit": "restricted", "warn": "restricted",
                  "enforceVersion": null, "auditVersion": null, "warnVersion": null},
  "quota": {"requests.cpu": "4", "requests.memory": "8Gi", "limits.cpu": "8", "limits.memory": "16Gi", "pods": "20"},
  "limits": [{"type": "Container",
              "default": {"cpu": "500m", "memory": "512Mi"},
              "defaultRequest": {"cpu": "100m", "memory": "128Mi"}}],
  "admins": [{"kind": "Group", "name": "payments-team"}],
  "adminRole": "admin",
  "isolateIngress": true
}
```

- `name` is an RFC 1123 DNS label (`422 invalid` naming the rule otherwise).
  `kube-` and `openshift-` prefixes are not refused; they are a consequence.
- `podSecurity.<mode>` is one of `privileged`, `baseline`, `restricted` or
  `null` (no label). `<mode>Version` is `latest` or `v1.<n>`, and is refused
  without its level — a version label without a level is ignored by admission.
- `quota` is `spec.hard` keyed by the quota resource name the API server uses
  (`requests.cpu`, `limits.memory`, `pods`, `count/deployments.apps`,
  `requests.nvidia.com/gpu`…). Every value must parse under the Kubernetes
  quantity grammar and be non-negative. Empty values are fields left blank, not
  zeros, and are dropped. An empty map creates no ResourceQuota.
- `limits` is a list of LimitRange items, `type` one of `Container`, `Pod`,
  `PersistentVolumeClaim`, each map validated as `quota` is. An item setting
  nothing is refused. An empty list creates no LimitRange.
- `admins` are RoleBinding subjects, `kind` one of `User`, `Group`,
  `ServiceAccount`. A ServiceAccount subject's `namespace` defaults to the
  project itself; a User or Group carrying one is refused. `adminRole` names the
  built-in aggregated ClusterRole bound — `admin`, `edit` or `view` — and is the
  RoleBinding's name. No subjects creates no RoleBinding.
- `isolateIngress: true` adds the `allow-same-namespace` NetworkPolicy: every
  pod, `Ingress` only, admitted from pods in this namespace and from nowhere
  else — the shape OpenShift's project template ships under the same name.

Response:

```json
{
  "name": "payments",
  "target": {"exists": false, "phase": null, "detail": "payments does not exist and can be created."},
  "objects": [
    {"kind": "Namespace", "name": "payments", "namespace": null, "group": "", "version": "v1", "resource": "namespaces", "yaml": "…"},
    {"kind": "ResourceQuota", "name": "project-quota", "namespace": "payments", "group": "", "version": "v1", "resource": "resourcequotas", "yaml": "…"},
    {"kind": "LimitRange", "name": "project-limits", "namespace": "payments", "group": "", "version": "v1", "resource": "limitranges", "yaml": "…"},
    {"kind": "RoleBinding", "name": "admin", "namespace": "payments", "group": "rbac.authorization.k8s.io", "version": "v1", "resource": "rolebindings", "yaml": "…"},
    {"kind": "NetworkPolicy", "name": "allow-same-namespace", "namespace": "payments", "group": "networking.k8s.io", "version": "v1", "resource": "networkpolicies", "yaml": "…"}
  ],
  "consequences": [ {"code": "…", "label": "…", "consequence": "…", "mitigation": "…"} ],
  "enabled": true, "enabledDetail": "…",
  "partial": false, "unavailable": []
}
```

**Pure and ungated**: one namespace read, nothing written, nothing audited, and
it renders on a read-only console — `enabled`/`enabledDetail` echo
`ADMIN_ALLOW_MUTATIONS` so a client can disable its button with the reason.
`target.exists` is a tri-state: `null` when the namespace read did not answer
(collected into `unavailable[]`, and the consequence `namespace_unknown` is
added), never `false` on a failed read.

Objects are always in this order, and only the ones the request asks for are
present. Their names are fixed — `project-quota`, `project-limits`, the role's
name, `allow-same-namespace` — so a project created here and one created by hand
from the same recipe collide on the object rather than silently coexisting as two
quotas.

### 17.4 `consequences[]`, and the acknowledgement that gates a write

The same handshake as §13.6 and §16.6: every entry is knowable from the request
and the one namespace read, every one describes something that goes wrong
*silently* otherwise, and the write refuses with `422 invalid` unless every code
present is named in `acknowledgeConsequences`. `context.unacknowledged` lists the
codes that were missing. The list is recomputed at write time, so consent given
for one request cannot be carried onto another.

| Code | When |
|---|---|
| `namespace_unknown` | The plan's namespace read did not answer. The write re-reads and refuses if the namespace exists; the plan cannot say which outcome to expect |
| `reserved_prefix` | `name` begins with `kube-` or `openshift-` |
| `psa_not_enforced` | No `enforce` level: the cluster default applies, which the console cannot read |
| `psa_enforced` | `enforce` is `baseline` or `restricted`: violating pods are refused, and a Deployment whose template violates it is accepted and never gets a pod |
| `no_quota` | No ResourceQuota: nothing bounds what the namespace can consume |
| `quota_needs_defaults` | The quota covers `requests.cpu`, `requests.memory`, `limits.cpu`, `limits.memory`, `cpu` or `memory` and no Container LimitRange item supplies the matching `defaultRequest`/`default` — so the API server refuses every pod that does not state that resource itself (`must specify requests.cpu`) |
| `no_admin` | No subjects: only cluster-wide bindings act in the namespace |
| `ingress_isolated` | `isolateIngress`: traffic from other namespaces is refused, the ingress controller's included, until a second policy admits it — if the CNI enforces policy at all |

### 17.5 `POST /api/projects`

Body: the plan body plus `acknowledgeConsequences` and `dryRun` (default
**true**). Response:

```json
{
  "dryRun": true, "created": false, "failed": 0, "skipped": 0, "name": "payments",
  "objects": [
    {"kind": "Namespace", "name": "payments", "namespace": null, "group": "", "resource": "namespaces",
     "verb": "create", "applied": false, "projection": "server",
     "diff": {"before": null, "after": "…", "unified": "…", "changed": true},
     "preflight": null, "auditId": 4471, "skipped": null, "error": null},
    {"kind": "ResourceQuota", "name": "project-quota", "namespace": "payments", "group": "", "resource": "resourcequotas",
     "verb": "create", "applied": false, "projection": "rendered",
     "diff": {"before": null, "after": "…", "unified": "…", "changed": true},
     "preflight": {"allowed": true, "reason": "", "evaluationError": null, "hint": null},
     "auditId": null, "skipped": null, "error": null}
  ],
  "consequences": [ … ]
}
```

Refusals, each before anything is written and in this order: the gate
(`403 mutations_disabled`, real writes only, audited — §1.6's step one called
before the namespace is read, because five objects are written and each is
reported), request validation, the namespace read — which must answer, because
"unknown" is not a state a create may proceed from, so its error propagates — the
takeover refusal (`409 conflict`, audited, dry run included), and the
acknowledgement check.

**Each object is an ordinary create through `app.admin.apply.create_from_yaml`**
and therefore through the funnel: its own preflight, its own diff, its own audit
row. `applied` and `auditId` are the funnel's own, copied and never derived.

**A dry run projects the Namespace and renders the rest, and says so.** The API
server's NamespaceLifecycle admission refuses a create into a namespace that does
not exist — `dryRun=All` included, with a 404 naming the namespace — so a dry run
cannot project the four objects inside it. They are reported with
`projection: "rendered"`: this console's own manifest diffed against nothing
(`diff.before` is `null`), no audit row, and a `preflight` of the exact verb the
real write will use (§9's `PreflightResult` fields), because the grant is the one
thing about such an object that *can* be checked before its namespace exists and
the one most worth knowing first. `projection: "server"` marks a diff the API
server produced. A client must label the two differently: a rendered manifest
has not been through admission. **§14.4's router install follows the same
rule.**

**`created` is true only when `dryRun` is false and every object landed.**
`failed` counts objects whose write failed, each with `error` in §1.3's shape and
the funnel's hint — rewritten only on RBAC escalation prevention, the way §14
rewrites it for ClusterRoles, to name `bind` on the ClusterRole instead of a
verb the preflight just confirmed. On a real write, a Namespace that failed
stops the sequence: the objects inside it are reported with `skipped` set to the
reason, not attempted, because each would fail with a 404 naming the namespace
and leave four audit rows saying nothing the first one did not. Any other
failure is counted and the sequence continues, so the operator learns about every
missing grant in one round rather than one per attempt. **There is no rollback**:
deleting a namespace the operator just asked for is not a correction anybody
wants made on their behalf.

### 17.6 The gate

`ADMIN_ALLOW_MUTATIONS` alone. Unlike §14 and §16 there is no feature switch of
its own: nothing here is a larger commitment than the §4 YAML editor already
offers, since every object is one the editor could create. The plan and the dry
run are permitted with the gate shut, for §14's reason.

### 17.7 What §17 does not do

**No delete, no edit, no adoption.** See §17.1. **No project template stored
anywhere**: the request body is the template. **No claim about enforcement**: a
Pod Security label is a declaration the admission plugin acts on, a NetworkPolicy
is a declaration the CNI may or may not act on, and a RoleBinding to a built-in
ClusterRole grants whatever that ClusterRole aggregates *on that cluster*, which
the plan does not enumerate. **No per-user identity**: the RoleBinding is for the
subject the operator names, written by the console's own ServiceAccount; the
console's user is not a cluster identity, and §17 does not pretend otherwise.

---

## 18. Pod Security — setting the level, with the pods that would violate it

### 18.1 What it is, and why it is not §4

Six labels on one Namespace, as one merge patch through the funnel. §4's YAML
editor can already write them; what it cannot do is answer the question an
operator has *first* — what does this break — and that answer is the whole of
§18.

**Pod Security admission evaluates the pods already in a namespace when the
namespace's labels change, and returns what it finds as `Warning:` headers on
the response — on a `dryRun=All` update exactly as on a real one.** So a preview
of "enforce restricted" comes back carrying the API server's own list of the
pods in that namespace that do not meet it, by name and by the field that fails.
That list is not computed here and is deliberately **not parsed** here: a summary
of somebody else's admission decision is a summary that can be wrong about which
pods are affected.

This is §17's neighbour, not part of it. §17 creates into a namespace that does
not exist and never adopts one; §18 edits one existing object's labels, with one
diff and one confirmation, the way §4 edits anything else.

### 18.2 `POST /api/projects/{name}/pod-security/plan`

```json
{
  "podSecurity": { "enforce": "restricted", "enforceVersion": "latest",
                   "audit": null, "auditVersion": null,
                   "warn": "restricted", "warnVersion": null }
}
```

**Every mode is named on every request, and absent means *remove that label*.** A
body whose omitted field could mean either "leave it alone" or "remove it" is a
body that eventually strips somebody's audit level because a form field was left
blank. An unknown mode key is `422 invalid` rather than ignored.

`null` and `"privileged"` are different requests, and the difference is §18's
honesty problem. `privileged` declares that this namespace admits everything.
`null` removes the declaration, and what applies then is the cluster's Pod
Security default — which lives in the API server's `AdmissionConfiguration`
file, which no API serves. **This console cannot say what removing the label will
mean**, and the consequence for it says exactly that.

Response:

```json
{
  "namespace": "prod",
  "current":  { "enforce": "baseline", "enforceVersion": null, "audit": null,
                "auditVersion": null, "warn": null, "warnVersion": null,
                "labelled": true },
  "requested": { "enforce": "restricted", "…": null },
  "resourceVersion": "4021",
  "changed": true,
  "consequences": [ … ],
  "gate": { "enabled": true, "detail": "…" }
}
```

Ungated and unaudited: one namespace read and some arithmetic on labels. It is
**not** a dry run — a dry run is a write request, preflighted like any other, and
it is what the caller asks for next. The namespace read must answer: a change
described against labels we could not read is a diff about nothing, and one
rendered from an empty dict would show labels being removed that this console
never saw.

### 18.3 `consequences[]`

Same shape and same handshake as §17.4: `{ code, label, consequence, mitigation }`,
recomputed server-side at write time against the namespace as it is *then*, and
a write naming fewer than all of them is `422 invalid` listing what is missing.

| Code | When |
|---|---|
| `psa_does_not_evict` | `enforce` is being set or changed. **The headline.** Admission runs when a pod is *created*, so nothing running stops, restarts or is evicted; a workload whose pods violate the new level keeps them and fails to make more — `FailedCreate`, below its replica count, with no pod to inspect |
| `psa_enforcement_removed` | `enforce` goes from set to absent. Not the same as `privileged`: the cluster default applies and this console cannot read it |
| `psa_lowered` | `enforce` moves down the `privileged` → `baseline` → `restricted` order. Pods this namespace refuses today become deployable. A level outside those three is **neither** raised nor lowered — inventing an ordering the API server does not use is worse than saying nothing |
| `psa_no_warn_label` | `enforce` is `baseline` or `restricted` and `warn` is not the same. Violations are refused at pod creation and nobody is told when they apply |
| `psa_version_pinned` | A mode's version is a minor rather than `latest`. The policy stops tightening as the cluster is upgraded, and nothing reports the gap |

### 18.4 `PUT /api/projects/{name}/pod-security`

The plan's body plus `acknowledgeConsequences[]` and `dryRun` (default **true**).
Returns the §1.5 mutation response with four keys added: `consequences` (what was
required, echoed), `current` and `requested` (the levels either side), and
**`admissionWarnings`** — the API server's `Warning:` headers, verbatim. They are
the same list §1.5's `warnings` carries; the second name exists so a client can
read them as what they are.

`PUT` rather than `PATCH` because §0.4 applies: the caller sends the
`resourceVersion` they were looking at, it is checked locally — which is what
produces a `409` carrying `context.currentPodSecurity`, the level as it is *now*
— and it rides inside the merge patch, so the API server refuses a stale write
too.

**`applied: true` means the labels changed. It does not mean any running pod was
affected**, and the response's consequences say so rather than leaving an
operator to infer it from a green result.

Refusals, in order, each before the cluster is changed: request validation, the
namespace read (which must answer; `404` if it is not there — this endpoint never
creates), the concurrency check, the acknowledgement check, then the funnel's
gate and its preflight on `patch namespaces`.

### 18.5 The gate

`ADMIN_ALLOW_MUTATIONS` alone, like §17. The dry run is **not** withheld, and
withholding it would defeat the feature: the projection is what carries the
admission warnings, and those are what an operator needs to decide whether to
open the switch at all.

### 18.6 What §18 does not do

**It does not evict, restart or reschedule anything** — see `psa_does_not_evict`.
**It does not read the cluster's Pod Security default**, because no API serves
it. **It does not parse the admission warnings** into a pod list, so there is no
`violatingPods[]` to sort or count against: the count in the UI is the number of
warning lines, which is what the API server sent. **It does not check that the
warnings were read** — the console holds no state between the preview and the
confirm, so the dialog blocks Confirm until an operator ticks the box and says
plainly that this one is the client asking, not the server enforcing.

---

## 19. Cluster status — the control plane's own health, from what vanilla serves

### 19.1 What it is, and what it refuses to be

OpenShift answers "is this cluster healthy" with `ClusterOperator` and
`ClusterVersion`: one object per platform component, each carrying `Available`,
`Degraded` and `Progressing`, rolled up into a single line at the top of its
console. **Vanilla Kubernetes serves no such API.** It serves five unrelated ones
that between them answer most of the same question, and an operator on a vanilla
cluster reaches them with five `kubectl` invocations and a lot of `jq`.

§19 is those five reads, joined. It is **not** a monitoring system: no history,
no configurable thresholds, no alerting, no state. Every number is a live read
taken when the page loaded, like every other page here.

**There is no aggregate verdict, and there will not be one.** No `healthy`
field, no traffic light. A stale `cloud-controller-manager` lease with no
aggregated APIs is a normal Tuesday on one cluster and an outage on another, and
a single boolean would have to pick. The response carries the findings and their
counts; the judging belongs to the person who knows the cluster.

### 19.2 `GET /api/cluster-status`

No parameters. One response, five independently nullable sections, plus §0.1's
`partial` and `unavailable[]`:

```json
{
  "controlPlane": [ … ] | null,
  "apiServices": { "items": [ … ], "local_count": 28, "unavailable_count": 1 } | null,
  "crds":        { "items": [ … ], "total": 74, "unhealthy_count": 1 } | null,
  "webhooks":    { "items": [ … ], "blocking_count": 1 | null, "complete": true } | null,
  "versionSkew": { "server_version": "v1.31.4", "supported_minors_behind": 3,
                   "nodes": [ … ] | null, "out_of_skew_count": 1 | null } | null,
  "partial": false,
  "unavailable": []
}
```

**A section is `null` when its read did not happen**, with the reason in
`unavailable[]` and `partial` true. This is §0.1 applied per section rather than
per response, because the five reads are five different permissions: a console
that may not list `admissionregistration.k8s.io` still has an honest answer about
version skew. A `null` section is **never** rendered as the healthy answer — "we
could not read the admission webhooks" and "this cluster has no admission
webhooks" send an operator to two different places, and only the second is good
news.

### 19.3 The five signals, and why each is honest on vanilla

**Leader-election leases** (`coordination.k8s.io/v1`, `kube-system`).
`kube-controller-manager` and `kube-scheduler` renew a Lease every few seconds.
`stale` is true when `renewTime` is older than the lease's own
`leaseDurationSeconds` — which is what a wedged controller looks like from the
API server's side. It is **tri-state**: `null` when either half is missing, since
a lease nobody has acquired has no `renewTime`, and calling that stale reports a
component as wedged on the strength of a field nobody wrote.

Every lease in the namespace is listed, not a filtered set: a cluster runs
leader-elected components this console has never heard of, and the one that
stopped renewing is exactly as interesting as `kube-scheduler`.

**Aggregated APIServices** (`apiregistration.k8s.io/v1`). An APIService with a
`spec.service` is served by a pod, and its `Available` condition is that pod
answering. This is where `metrics.k8s.io` goes when §7.7 starts returning
`unsupported`, and §19 is the only place the *reason* is written down. Only the
aggregated ones are listed — the locally served ones are `Available` on any
cluster that is answering at all, and thirty rows of guaranteed-green is how a
table stops being read. They are counted in `local_count` so the total adds up.

**CustomResourceDefinitions** (`apiextensions.k8s.io/v1`). A CRD whose
`Established` condition is not true serves nothing; one carrying
`NonStructuralSchema` cannot be pruned or converted. Only the unhealthy ones are
listed, with `total` alongside — three problems out of four hundred and three out
of five are different mornings.

**Admission webhooks** (`admissionregistration.k8s.io/v1`, plus an EndpointSlice
listing). One row per webhook, not per configuration, because `failurePolicy`
and the backing Service are set per webhook. The row this section exists for is
`failurePolicy: Fail` with `endpoint_count: 0`: that webhook is **refusing every
write it intercepts, cluster-wide**, until its backend returns. It is the most
effective way to break a Kubernetes cluster without touching a node, and nothing
else in this console — or in `kubectl` — puts it in front of anyone.

`endpoint_count` is nullable twice over, and the two nulls mean different things
the UI states separately: `null` on a URL-addressed webhook means there is
nothing *in the cluster* to count (it may be answering perfectly well), and
`null` on a Service-addressed one means the EndpointSlice listing did not
answer. Neither may render as `0`, which is the finding.

**"Did not answer" includes "answered, but was not exhausted."** The slice
listing is paged, and a cluster with more than ten pages of 500 leaves the
scan holding a cursor rather than a total. That is reported as an `unavailable[]`
entry with `reason: "timeout"` and the whole tally at `null` — not as the tally
the scan managed to build. A page nobody read and a Service with no backends
produce the same number, and that number is `0`, which is the one value this
section acts on: it sorts the webhook to the top of the table under a sentence
saying it is refusing every write that matches its rules. There is no way to
spell "0, of the slices we looked at" in a count an operator reads as a fact, so
it is withheld. A Service with no EndpointSlice at all is still a real `0` —
that is the finding, and it survives the distinction.

`complete` is false when one of the two configuration listings answered and the
other did not — the rows are real, but they are not all of them.

**`blocking_count` is `null`, not `0`, when the EndpointSlice listing did not
answer.** With every `endpoint_count` at `null` nothing matches the predicate, so
the naive count reports "no webhook is refusing writes" off a read that never
happened — this section's one finding, delivered backwards, during exactly the
outage that produces it. The rows are still returned; only the verdict is
withheld.

**Version skew** (`/version` and the node listing). A kubelet more than
`supported_minors_behind` (3, Kubernetes' documented policy since 1.28) minors
behind the API server is outside support, and a kubelet *ahead* of it is
unsupported at any distance. Per-node `status` is `ok`, `behind`, `ahead` or
**`unknown`** — the last whenever either version string could not be parsed,
because a skew rule applied to a number we do not have is a verdict about a node
nobody checked. Distribution suffixes (`v1.30.6-eks-abc1234`, `v1.31.4+rke2r1`)
parse by prefix rather than failing.

`nodes` and `out_of_skew_count` are **both** `null` when the node listing was
refused, while `server_version` still answers and the section stays — the API
server's version is worth reading on its own, and `0` there would claim no node
is out of skew on the strength of nodes nobody listed. The whole section is
`null` only when neither read answered.

### 19.4 What §19 deliberately does not report

**etcd.** No vanilla API reports etcd health to a client with ordinary RBAC; the
API server's `/readyz` includes an etcd check and is not readable on most
clusters. "etcd: unknown" in a table of things that are known is filler, so the
page says nothing about etcd.

**Reachability.** §19 never claims a webhook, an aggregated API or a controller
is *reachable*. It reports what the API server recorded about them — a condition
it wrote, an endpoint count, a renewal timestamp. Reachability from here would be
a guess about a network this console is not on.

**A missing component.** On EKS, GKE, AKS and every managed control plane, the
scheduler and controller-manager run where the customer cannot see them and
`kube-system` may hold no lease for them at all. §19 lists the leases that
**exist** and says nothing about the ones that do not. "kube-scheduler: MISSING"
on a healthy EKS cluster is exactly the confidently wrong answer §0 rules out.

### 19.5 Permissions

Five listings, each degrading only its own section: `list leases` in
`kube-system`, `list apiservices`, `list customresourcedefinitions`, `list
validatingwebhookconfigurations` and `mutatingwebhookconfigurations`, `list
endpointslices`, and `list nodes`. `docs/rbac.md` carries what withholding each
one costs. Nothing here is a write, so there is no preflight and no audit row —
§19 is a read.

---

## 20. Expanding a PersistentVolumeClaim

### 20.1 What it is, and why it is not §4

A claim that has filled up is one of the most ordinary production incidents
there is, and the fix — ask for more — is one field. §4's YAML editor can
already write that field. What it cannot do is any of the four things that
decide whether the write is a good idea:

* **It cannot tell you the StorageClass forbids expansion.** The edit is
  accepted by the form and rejected by the API server, and the operator reads a
  relayed admission message about a field they did not think they were touching.
* **It cannot stop you shrinking.** Typing `5Gi` where the claim says `50Gi` is
  one keystroke. §20 refuses it by arithmetic, before anything is sent.
* **It cannot name the pods that mount the volume**, which is what decides
  whether the filesystem grows now or after a restart.
* **And it cannot say what `applied: true` means**, which is §20.3.

### 20.2 The two endpoints

`POST /api/storage/claims/{namespace}/{name}/expand/plan` takes
`{"size": "100Gi", "resourceVersion": "…"}` and answers with the claim's state,
its class's expansion support, what mounts it, and the consequences:

```json
{
  "current": { "phase": "Bound", "storage_class": "gp3",
               "requested": "50Gi", "requested_bytes": 53687091200,
               "capacity":  "50Gi", "capacity_bytes":  53687091200,
               "conditions": [ … ] },
  "requested": { "size": "100Gi", "size_bytes": 107374182400 },
  "expansion": { "supported": true | false | null, "storage_class": "gp3",
                 "reason": "allowed", "detail": "…" },
  "mountedBy": ["postgres-0"] | null,
  "blocked": { "message": "…", "hint": "…", "context": { … } } | null,
  "consequences": [ … ],
  "resourceVersion": "7710",
  "gate": { … }, "partial": false, "unavailable": []
}
```

`PUT /api/storage/claims/{namespace}/{name}/size` takes the plan's body plus
`acknowledgeConsequences[]` and `dryRun` (default **true**), and returns the
§1.5 mutation response with `consequences`, `current`, `requested` and
`expansion` added.

**The size is kept as the string that was typed.** It is parsed only for
comparison. Reformatting `100Gi` as `107374182400` would put a number in the
diff the operator has to convert back before they can confirm it, on the one
screen where the number is the whole decision.

`PUT` rather than `PATCH` because §0.4 applies: the caller sends the
`resourceVersion` they were looking at, it is checked here — which is what
produces a `409` carrying `context.currentSize` and `context.currentCapacity` —
and it rides inside the merge patch, so the API server refuses a stale write too.

### 20.3 What `applied: true` attests, and what it does not

Expansion is two acts by two different parties. This console patches
`spec.resources.requests.storage`; that is the entire write, and it is the only
thing `applied: true` attests.

Afterwards the storage provider grows the volume — the claim carries a
`Resizing` condition while it does — and then the *filesystem* has to be grown
too, which on many CSI drivers cannot happen while a pod has it mounted: the
claim sits at `FileSystemResizePending` until every pod using it restarts.
**`status.capacity` is what a workload actually has, and this write does not
change it.** `current.capacity` is returned in the same response, read from
before the write, so the two numbers are side by side rather than one of them
being inferred from the other.

An operator who reads a green result as "the volume is bigger now" is wrong
about the disk their database is filling. So it is a consequence acknowledged by
name — `pvc_capacity_is_not_immediate` — on **every** expansion, including the
ones that go on to work perfectly.

### 20.4 The refusals, and the one thing that is deliberately not one

Three refusals, each a `422 invalid` raised before a request leaves the process,
because the API server's message for these names a field the operator did not
know they were editing:

| Refusal | Why here rather than relayed |
|---|---|
| The claim is not `Bound` | There is no volume to expand. An unbound claim has not been provisioned |
| `size` is smaller than, or equal to, the current request | No Kubernetes API makes a bound claim smaller, and a provider that did would be discarding the data past the new end. Equal is a no-op, named as one |
| `expansion.supported` is **`false`** | The class forbids it, so the write is certain to be rejected. The message names the class and the field an administrator would set |

The current request being **unreadable** is a fourth: without it this console
cannot tell an expansion from a shrink, and it will not send a size it could not
compare.

**`expansion.supported` is tri-state, and only `false` refuses.** `null` means
we could not find out — the class was refused or has been deleted, or the claim
names none and is bound to a statically provisioned volume whose growth no API
here reports on. Reading `null` as `false` would block a write the cluster would
have accepted and send an operator to argue with a StorageClass that is already
correct: the same mistake §0.2 forbids on a failed access review. It becomes a
consequence to acknowledge, and the API server decides.

### 20.5 Why the plan answers `200` for a size it will not write

A blocked size comes back as `blocked` rather than as a `422`. The plan is the
screen where the size is *decided*, and one that answered a too-small number
with an error alone would withhold the claim's current size, its capacity and
its mounts at exactly the moment those three facts are what the operator needs.
It is the same refusal the write raises, caught rather than restated, so there is
one set of messages. **The write still refuses.**

### 20.6 The consequences

Two are on every expansion. That is friction on purpose: they are the two things
people are reliably wrong about, and being wrong about either costs an incident.

| Code | What it says |
|---|---|
| `pvc_capacity_is_not_immediate` | §20.3 — this changes the request, not the space a workload has |
| `pvc_expansion_is_one_way` | No API shrinks a bound claim, and on a cloud provider this is what you are billed for from the moment the volume grows |
| `pvc_expansion_unknown` | `expansion.supported` is `null`; the API server will decide |
| `pvc_in_use_offline_resize` | The pods that have the volume mounted, by name. Many CSI drivers need each of them restarted before the filesystem grows |
| `pvc_mounts_unknown` | The pod listing did not answer. **Not** "nothing has it mounted" |
| `pvc_resize_already_pending` | The claim carries `Resizing` or `FileSystemResizePending` already; a second expansion stacks on an unfinished one |

Acknowledgements are **recomputed against the claim as it is at write time**,
never trusted from the plan: a caller that could acknowledge a code the server
did not derive could acknowledge every code it liked, and one of these is on
every write.

### 20.7 `mountedBy` is `null`, never `[]`

§0.1's corollary, on a read where the two answers point in opposite directions:
an empty list says the filesystem can be grown without touching a workload, and
that is the sentence that starts an offline resize on a volume a database has
open. A refused pod listing is `null`, `partial` is true, the reason is in
`unavailable[]`, and `pvc_mounts_unknown` says so in words.

### 20.8 The §8 row gains two fields

`pvc_row` now carries `requested` and `requested_bytes` beside `capacity_bytes`.
On a settled claim the two agree and the extra column is noise; on a claim
mid-expansion they differ, and **that gap is the only thing in the §8 table that
says an earlier resize has not finished.** Neither ever falls back to the other:
a capacity borrowed from the request would tell an operator a Pending claim has
500 GiB behind it, and a request borrowed from the capacity would make the two
columns agree by construction and stop the row from showing an expansion at all.

### 20.9 The gate, and what §20 does not do

`ADMIN_ALLOW_MUTATIONS` alone, like §17 and §18. The dry run is **not** withheld:
the projection is where the API server's own validation lands — including the
StorageClass check this console cannot make when it could not read the class —
so withholding it would leave an operator with strictly less than `kubectl`
gives them.

**§20 shrinks nothing, restarts nothing and creates nothing.** It does not
restart the pods that need restarting for an offline resize, does not watch the
claim afterwards, and does not report whether the expansion completed — a live
read of the claim does that, and `status.capacity` is where the answer is.

---

## 21. Autoscalers — the replica bounds, and whether the autoscaler is scaling at all

### 21.1 What it is, and the failure it exists for

Two integers on one `HorizontalPodAutoscaler`. §4's YAML editor can already write
them; §21 exists because of what the editor cannot say.

**An HPA whose `ScalingActive` condition is false is not scaling anything.** The
controller cannot compute a desired replica count — almost always because the
metric it needs cannot be read, which is what a missing `metrics.k8s.io` looks
like from here. Such an autoscaler is *completely ordinary* in `kubectl get hpa`:
one column reads `<unknown>` and everything else is populated. The action this
endpoint is reached for happens during an incident — a service is at its ceiling
and somebody raises `maxReplicas` — and on an inert HPA that action stores a
number in etcd and changes nothing at all, while the operator goes back to
watching a service that will never grow.

**The second failure is direction.** Lowering `maxReplicas` below the running
replica count is not a cap on future growth: the HPA clamps at its next scale
decision, seconds away, and pods terminate. The form field is identical to the
one that raises a ceiling.

### 21.2 The typed row

`hpa_row` is registered as the §4 shaper for
`autoscaling/horizontalpodautoscalers`, so the generic listing carries it. Three
of its fields are **tri-state and must not be rendered as two**:

* `scaling_active`, `able_to_scale`, `scaling_limited` — `true`/`false` from the
  controller's conditions, and **`null` when the condition has not been written
  yet**. `null` is a fresh autoscaler, not a broken one; `false` is one the
  controller has observed and cannot use. Rendering either as healthy is the
  confident wrong answer §0 rules out.
* Each entry in `metrics[]` carries `target` and `current`, and **`current` is
  `null` when the controller has published no reading**. Never `0`: a CPU metric
  drawn at 0% reads as an idle workload, and idle is the number that argues for
  scaling *down*.
* `current_replicas` and `desired_replicas` are `null` when unobserved.

`metrics[]` is driven by **`spec.metrics`**, not by `status.currentMetrics` — a
status-driven list would drop exactly the metric worth seeing — and the two are
paired by metric **identity** (kind, name, container, described object) rather
than by list position, because pairing by index hands one metric another's
reading the moment either list is incomplete.

`scaling_limited: true` is **not a fault**: it means the desired count is being
clamped to one of the bounds. During an incident it is the answer to "why is this
not scaling up".

### 21.3 `POST /api/autoscaling/hpas/{namespace}/{name}/bounds/plan`

```json
{ "minReplicas": 2, "maxReplicas": 30, "resourceVersion": "9040" }
```

**Both bounds are named on every request**, including the one that is not
changing, for §18.2's reason: a body whose omitted field could mean "leave it
alone" or "reset it to the default" is a body that eventually resets somebody's
floor to 1 because a form field was blank — during the incident where they were
raising the ceiling.

Ungated and unaudited: one read and arithmetic. Returns `current` (the §21.2
row), `requested`, `resourceVersion`, `consequences`, `gate`, and **`blocked`**.

A request that changes neither bound answers **`200` with `blocked` set**, not
`422`, exactly as §20.3 does. The plan is the screen where the bounds are
*decided*, and one that answered with an error alone would withhold the current
bounds and the replica count at the moment those are the facts needed to pick
different ones. `blocked` and `consequences` are never both populated. The write
refuses.

### 21.4 `PUT /api/autoscaling/hpas/{namespace}/{name}/bounds`

The plan's body plus `acknowledgeConsequences[]` and `dryRun` (default **true**).
One merge patch on `spec.minReplicas` and `spec.maxReplicas` through the funnel,
preflighting `patch horizontalpodautoscalers`.

Consequence codes: `hpa_not_scaling`, `hpa_cannot_reach_target`,
`hpa_max_below_current`, `hpa_min_above_current`, `hpa_replicas_unknown`,
`hpa_scale_to_zero_gated`.

`hpa_replicas_unknown` is the tri-state one: with no `currentReplicas` published,
whether these bounds take effect immediately is arithmetic on a number nobody
has, so the verdict is withheld rather than guessed in either direction.

Acknowledgements are **recomputed at write time**, never trusted from the plan —
the replica count moves on its own, so "this terminates two pods" can be a
different number by the time the write lands, and the caller must have accepted
the one that is true then.

**`applied: true` means the bounds are stored.** It does not mean anything
scaled, and on an inert autoscaler it never will: `current.scaling_active` in the
same response is that answer, read from before the write.

Pinned to `autoscaling/v2`. `v1` carries no `metrics` field, so every
current-versus-target reading would be absent; a cluster serving only `v1` is
pre-1.23 and gets §1.2's `unsupported`, which renders calmly rather than red.

### 21.5 `governedBy` on §6's scale

**A manual scale on an autoscaled workload is reverted seconds later.** The write
succeeds, the API server accepts it, `applied: true` is true — and the HPA's next
scale decision puts the count back. An operator who scaled a service to 10 during
an incident and watched it return to 3 has been told something correct and
misleading, which is the defect standard with a green tick on it.

`POST /api/workloads/{plural}/{namespace}/{name}/scale` therefore carries
`governedBy`, read before the write and returned **on the dry run as well** —
the preview is where the operator decides:

```json
{ "governed": true, "autoscaler": { … §21.2 row … }, "reason": null, "detail": "…" }
```

`governed` is **tri-state**:

* `true` — an HPA targets this workload; `autoscaler` is its row, so the UI can
  also say whether that HPA is itself inert (in which case the manual count will
  hold, which is a reprieve rather than a fix).
* `false` — nothing autoscales it, so the count set here is the one that stays.
  **A cluster that does not serve the HPA API is `false`, not `null`**:
  `unsupported` is knowledge, and reporting it as a gap would warn an operator
  about a controller their cluster cannot run.
* `null` — the autoscaler listing did not answer, with `reason` naming why. This
  must never render as "nothing will undo this", which is the whole sentence an
  operator would act on.

Matching is by `scaleTargetRef` kind and name, plus the API group **only when the
autoscaler carries one** — `apiVersion` is optional in the schema, and requiring
it would report "nothing is autoscaling this" about an autoscaler that is.

### 21.6 What §21 does not do

**It does not edit `spec.metrics` or `spec.behavior`.** Both are structured
enough that a form would be a worse editor than §4's, and neither is the thing
somebody reaches for during an incident. **It does not create or delete
autoscalers** — §4 does that, with the same funnel. **It does not report why a
metric is unreadable** beyond the condition's own reason and message: the answer
is usually an aggregated API, and §19 is the page that lists those.

---

## 22. Volume snapshots — and refusing to call one a backup

### 22.1 What it is, and the belief it exists to correct

An operator is about to do something they might regret — grow a claim (§20), run
a migration, delete a workload — and they want a point they can get back to.
`kubectl` writes the four-line object; §4's editor writes it too. §22 exists
because of what neither can say, and on this API the gap between what the thing
is called and what it is is the widest in Kubernetes.

**A CSI snapshot is not a backup.** For nearly every driver it is a
point-in-time reference held *inside the same storage system* as the volume —
often the same array, the same zone, sometimes the same disk. It protects
against the change about to be made. It does not survive the loss of the storage
holding it, because it was never anywhere else. A console that renders a green
`Ready` beside the word *snapshot* and says nothing else is handing somebody a
reason to skip a real backup.

**And it is crash-consistent, not quiesced.** Nothing here freezes a filesystem
or asks an application to flush. What is captured is what would be on disk after
an abrupt stop.

`snapshot.storage.k8s.io` is CRD-backed, shipped by the external-snapshotter
rather than by Kubernetes, so a cluster without it serves nothing here — §1.2's
`unsupported`, rendered as an ordinary fact rather than red.

### 22.2 The typed rows

`volumesnapshot_row` and `volumesnapshotclass_row` are registered as §4 shapers,
so the generic listing carries them.

**`ready_to_use` is tri-state, and the API declares it `*bool` on purpose:**

* `true` — the snapshot exists and can be restored from.
* `false` — the controller looked and it is not usable; `error` usually says
  why, and a snapshot can sit here for hours.
* **`null` — the controller has not reported yet.** A snapshot of a large volume
  is not instant, and this is what its first seconds or minutes look like.

Collapsing `null` into `false` tells an operator their backup failed while it is
being taken. Collapsing it into `true` is worse: it says a restorable snapshot
exists when none may, and only that direction gets somebody to delete the source
volume. `bound_content`, `creation_time` and `restore_size_bytes` are `null` for
the same window and for the same reason — a restore size of `0` is not a snapshot
of nothing.

`source_claim` and `source_content` are **mutually exclusive** in the API and are
reported separately rather than flattened into one "source": a snapshot taken of
a claim and one adopted from content that already existed in the storage system
are different objects meaning different things.

On the class, **`deletion_policy`** is the field worth putting in front of
somebody. It is not a preference: under `Delete` — the common default — removing
the namespaced VolumeSnapshot destroys the snapshot in the storage system; under
`Retain` it does not. The same click is bookkeeping under one and irreversible
data loss under the other.

### 22.3 `POST /api/storage/claims/{namespace}/{name}/snapshot/plan`

```json
{ "name": "postgres-before-upgrade", "snapshotClass": "csi-ebs" }
```

The name is **required and never generated**: it is how anyone finds the
snapshot again, and a generated one is a string nobody recognises during the
incident it was taken for. It is validated here as an RFC 1123 subdomain, so a
bad name is a `422` naming the rule rather than a relayed admission error about a
regex the operator never saw.

**`snapshotClass` is optional and its absence is meaningful:** omitting it asks
the controller for the cluster's default class, which is a real thing the API
does, and is not the same as a class this console could not read. A class named
but absent is a `422` whose hint lists the real ones — unlike §20's and §21's
plans there is nothing to decide from, the name is simply wrong.

Ungated and unaudited: one claim read and one class listing. Returns `claim`,
`requested`, `snapshotClass`, `consequences`, `gate`, and §0.1's `partial` /
`unavailable[]`.

**`snapshotClass.deletion_policy` is tri-state.** `null` when the class listing
was refused, or when no class was named and the cluster marks no default.
Reporting that as `Delete` warns about data loss that will not happen; reporting
it as `Retain` withholds a warning about loss that will. Neither is acceptable,
so it stays unknown and the operator acknowledges that it is.

The whole class listing is read rather than a single `get`, because the *default*
class is discoverable only by looking at every class's annotations — no endpoint
answers "which one is the default".

### 22.4 `POST /api/storage/claims/{namespace}/{name}/snapshot`

The plan's body plus `acknowledgeConsequences[]` and `dryRun` (default **true**).
One create through the funnel, preflighting `create volumesnapshots`.

Consequence codes: `snapshot_is_not_a_backup`, `snapshot_is_crash_consistent`,
`snapshot_delete_destroys_data`, `snapshot_deletion_policy_unknown`,
`snapshot_claim_not_bound`.

The first two are on **every** snapshot. That is friction on purpose: both are
what the word is routinely believed to mean and does not, and being wrong about
either is discovered during a restore.

`snapshot_claim_not_bound` is a consequence rather than a refusal — a claim can
bind between the plan and the write, so §22 does not refuse, it just declines to
pretend the result will be usable.

`volumeSnapshotClassName` is **omitted entirely** from the created object when
the caller named no class, rather than resolved here and pinned. Omitting it is
what asks the controller for the default, and that resolution is the
controller's to make at write time — pinning the name this console read a moment
ago would quietly make the snapshot depend on which class was default when the
dialog opened.

**`applied: true` means a VolumeSnapshot object exists. It does not mean a
snapshot has been taken.** The controller does that afterwards and reports it by
setting `status.readyToUse`, which starts out `null` and can end at `false` with
an error. Nothing in this response is evidence that there is anything to restore
from — §22.2's row is where that answer lives, and it is a tri-state for exactly
this reason.

### 22.5 What §22 does not do

**It does not restore.** Creating a PVC from a snapshot is a claim create with a
`dataSource`, and it belongs with §20's neighbours rather than here; it is also
the operation where getting the size and the class wrong silently produces an
empty volume, which deserves its own plan.

**It does not delete snapshots.** §4 does, through the same funnel — but note
that the generic delete carries no `deletionPolicy` warning, which is why §22
puts the policy in front of the operator at *creation*, the one moment in this
console's life where it is guaranteed to reach the right person.

**It does not schedule anything.** There is no snapshot on a timer here, and
there will not be: that is a controller, and this console holds no state and runs
no loops.

---

## 23. Subject access review — what may *this* subject do

### 23.1 The question §9 does not ask

§9's preflight asks `SelfSubjectAccessReview`: may **the console's own identity**
do this? That is what every button needs, and it is not the question an
administrator has. "What can alice do", "why can this ServiceAccount delete
namespaces", "did removing that binding actually take her access away" — nothing
here answered those, and both alternatives are worse:

* **Subtracting §8's tables.** Roles, ClusterRoles and their bindings are all
  listable, and it is tempting to compute an answer from them. That answer is
  *derived*, and blind to every authorizer that is not RBAC — the node
  authorizer, any webhook authorizer, ABAC where it survives. On a cluster with
  an authorization webhook it is confidently wrong in both directions, about
  somebody's access.
* **Impersonation.** `docs/adr-0007-impersonation.md` records why every cluster
  call here is made as one ServiceAccount and what per-user impersonation would
  cost. People reach for it largely to answer *this* question, and
  `SubjectAccessReview` answers it without taking a single action as anybody: it
  is a question put to the API server, not an identity borrowed.

`SubjectAccessReview` runs the API server's **whole authorization chain** and
reports what it concluded. That is what makes §23 authoritative where a cleverer
reading of §8 cannot be.

### 23.2 `POST /api/access/subject-review`

```json
{
  "subject": { "kind": "User", "name": "alice", "groups": ["platform-admins"] },
  "checks": [ { "verb": "delete", "group": "core", "resource": "pods", "namespace": "prod" } ]
}
```

`kind` is `User` or `ServiceAccount`. **Not `Group`** — the authorizer takes a
user plus a list of groups, so a group cannot be the subject; ask about a user
who is in it. A ServiceAccount requires `namespace`: its identity is
`system:serviceaccount:<namespace>:<name>`, so the namespace is part of who it
is rather than a filter on the question.

Checks are shaped exactly like §9's, `core` for the core group per §1.4, and are
echoed back in the caller's own spelling so a batch response joins to a batch
request key-for-key. At most **50** per request — each is one round trip, and a
caller asking about two hundred verbs wants a feature this is not (see §23.5).

### 23.3 Groups, and the answer that is wrong without them

**This is the failure §23 is built around.** A subject's access is mostly not
attached to their name; it arrives through their groups, and the API server only
considers the groups *in the review*. A review for `alice` with no groups asks
what a user called alice with no memberships can do — a question nobody has — and
answers "nothing", cleanly, about a cluster administrator.

So the response carries **`subject.groups_complete`**, and it is to be read
before any result:

* **`true`** for a **ServiceAccount**. The API server's group assignment is
  deterministic — `system:authenticated`, `system:serviceaccounts`,
  `system:serviceaccounts:<namespace>` — so the backend supplies them and the
  answer is complete.
* **`false`** for a **User, always**, even when groups were supplied. A person's
  real memberships come from whatever authenticated them — OIDC claims, a
  certificate's organisation, a proxy header — and **no API on this cluster
  reports them**. The honest reading is never "alice cannot do this" but "a user
  named alice, in exactly these groups, cannot do this", and `groups_detail`
  says so in words the UI renders verbatim.

`system:authenticated` is added to every review, because every authenticated
request carries it.

### 23.4 The four outcomes

Per result, three fields come back from the API and all three are kept:

| Fields | Means |
|---|---|
| `allowed: true` | An authorizer said yes |
| `denied: true` | An authorizer said **no**, explicitly |
| `allowed: false`, `denied: false` | Nothing granted it — the ordinary "no", and what almost every RBAC-only cluster produces |
| `evaluationError` non-null | The authorizer **could not decide** |

The last is §0.2's rule, the one §9 already keeps: **an evaluation error is not a
denial.** Reporting it as one sends somebody to grant a permission that is
already there. `undecided` counts them so a caller does not have to scan.

Flattening `denied` into `allowed: false` would hide a deliberate deny behind the
same words as an absent grant — a distinction only a webhook authorizer makes
visible, and exactly the one worth seeing when it appears.

A review that could not be *issued* — the API server refused it, the transport
failed — is reported the same way: `allowed: false` with an `evaluationError`
naming what failed. Never a clean denial, which would answer a question nobody
asked.

### 23.5 The grant, the audit, and what §23 is not

The console needs `create` on `authorization.k8s.io/subjectaccessreviews`. That
grant **is** the control: anyone who can issue these can map the authorization
state of every identity on the cluster, so there is no separate switch — the
permission is the switch, and withholding it disables the feature with the
reason. The preflight is about `subjectaccessreviews`, so a refusal names *that*
permission rather than the resource in the question, which would send an operator
to grant alice something when the missing grant is the console's.

It is nonetheless a **privileged read**, like §4's Secret reveal, and audited the
same way: one row per request naming who asked about whom. Nothing is exposed
that §8's tables do not already imply, but "who asked what the CFO's account
could reach" is a question worth being able to answer afterwards.

**§23 does not answer "who can do X".** That question cannot be put to the
authorizer at all — it can only be derived by reading every RBAC object and
computing, which is precisely the blind, non-authoritative answer §23.1 exists to
replace. Offering both on one page would let the weaker one be read as the
stronger, so it is absent rather than caveated.

**§23 does not enumerate a subject's permissions.** `SelfSubjectRulesReview`
returns a rule list, but only for the caller — there is no such API for another
subject. Asking fifty questions is asking fifty questions, which is why the
batch is bounded rather than open-ended.

---

## 24. Node taints and labels — the two fields that decide what runs here

### 24.1 Why this is not "edit the node YAML"

Both writes are small, and §4's editor can already send either. What the editor
cannot do is answer the question that decides whether an operator has written a
scheduling rule or caused an outage, and there are three of those questions.

**`NoExecute` is not a stronger `NoSchedule`.** `NoSchedule` and
`PreferNoSchedule` are consulted when the scheduler is *placing* a pod: adding
one moves nothing that is already running. `NoExecute` is evaluated against the
pods that are already there, and the ones that do not tolerate it are removed by
the taint manager in kube-controller-manager. In a form the three are three
entries in one dropdown.

**That removal is a delete, not an eviction.** §5's drain goes through the
`pods/eviction` subresource, which is where PodDisruptionBudgets are enforced.
This console says so in the drain dialog, in `docs/safety-model.md`, and in the
sentence telling operators that `force` will not get them past a budget. **The
taint manager does not use that subresource, so a `NoExecute` taint takes an
application below its budget without being refused** — from a dialog two clicks
away from the one that has been teaching the opposite. §24 states that as a
consequence the operator acknowledges by name, not as a note beside a form.

**A label change evicts nothing, and that is the trap.** Node affinity is
`requiredDuringSchedulingIgnoredDuringExecution`; the rule is checked when a pod
is placed and never again. Removing a label a running pod's `nodeSelector`
requires does not disturb that pod at all — it changes where it can be placed
*next*, which is discovered during a rollout by somebody who was not here.

### 24.2 `POST /api/nodes/{name}/taints/plan`

```json
{ "taints": [ { "key": "dedicated", "value": "gpu", "effect": "NoExecute" } ] }
```

**The whole list on every request**, including the taints that are not changing.
`spec.taints` is an atomic list in the Kubernetes API — a patch replaces it
outright — so there is no add-one operation to expose, and inventing one would
mean this console reconstructing the list from a delta and the operator
confirming a diff built from an assumption rather than from what they sent. An
empty list removes every taint.

`effect` is one of `NoSchedule`, `PreferNoSchedule`, `NoExecute`. The uniqueness
rule is the **`(key, effect)` pair**, not the key: `dedicated=a:NoSchedule` and
`dedicated=b:NoExecute` are both legal on one node.

The response:

```json
{
  "name": "ip-10-0-1-4",
  "resourceVersion": "884213",
  "current":  [ … ],
  "requested": [ … ],
  "added":   [ … ],
  "removed": [ … ],
  "changed": [ { "before": { … }, "after": { … } } ],
  "deleting": [
    { "namespace": "prod", "pod": "checkout-7c9",
      "controller": { "kind": "ReplicaSet", "name": "checkout-7c9" },
      "taint": { "key": "dedicated", "value": "gpu", "effect": "NoExecute" },
      "delay_seconds": 0 }
  ],
  "pods_checked": true,
  "unavailable": [],
  "blocked": null,
  "consequences": [ … ],
  "gate": { "enabled": true, "detail": "…" }
}
```

Ungated and unaudited: two reads and set arithmetic. It does **not** dry-run the
patch — a dry run is a write request the caller has not made yet, and it needs
the preflight the funnel does.

`changed` is a category of its own rather than a removal plus an addition,
because it behaves like one: a taint whose *value* moves is still one taint to
the API server, and a pod tolerating the old value with `operator: Equal` stops
tolerating the new one. **A value edit deletes pods exactly as a new taint
does**, which is not what "changed" sounds like.

`deleting` is `null` — never `[]` — when the node's pod listing failed, with the
reason in `unavailable[]` and `pods_checked: false`. §0.1's corollary applies to
the most destructive write in this document: an empty list renders as "this taint
deletes nothing" over a node nobody counted.

`delay_seconds` is a **real zero** when the pod does not tolerate the taint — the
taint manager removes it as soon as the taint is written, and nothing was left
unread to produce that number. A pod whose matching toleration carries a
`tolerationSeconds` gets that value instead and is still on the list: it is
going, later. That third state is why the field is not a boolean. The node looks
entirely healthy for five minutes and then empties.

**An unbounded toleration wins outright.** The taint manager takes the minimum
`tolerationSeconds` over the tolerations that match a taint and treats a matching
toleration with none as infinite, so one unbounded match means the pod stays,
however many bounded ones sit beside it.

A request that changes nothing answers `200` with `blocked` set rather than
`422`, as §20's and §21's plans do: this is the screen where the taints are
*decided*, and an error alone would withhold the current list at the moment it is
the fact needed to pick a different one. `blocked` and `consequences` are never
both populated.

### 24.3 `PUT /api/nodes/{name}/taints`

The plan's body plus `dryRun` (defaulting to true, §0.3) and
`acknowledgeConsequences`. `PUT` because §0.4 applies: the caller sends the
`resourceVersion` they were looking at, it is checked here — which is what
produces a `409 conflict` carrying `context.currentResourceVersion` and
`context.currentTaints` — and it rides inside the merge patch, so the API server
refuses a stale write too.

Order of refusals, each before the cluster is touched: request validation, the
node read (which must answer), the concurrency check, the no-op check recomputed
against the node as it is *now*, and the acknowledgement check over consequences
recomputed the same way. Consequences are **never trusted from the plan**: pods
arrive on a node on their own, so "this deletes two pods" can be a different
number by the time the write lands, and what the operator must have accepted is
the one that is true at write time.

Through `mutate()` like every other write: gate (`ADMIN_ALLOW_MUTATIONS` alone —
drain carries no switch of its own either, and a second gate on the lesser action
would say the reverse of what is true), preflight `patch core/nodes`, `dryRun=All`
on a preview, diff, audit.

The §1.5 response gains `current`, `requested`, `deleting`, `pods_checked`,
`unavailable` and `consequences`.

**`applied: true` means the taint list on the node is what you sent.** It does
not mean any pod has gone. The taint manager acts on its own schedule and a pod
with a `tolerationSeconds` is still running by design, so the node is not empty
and the response does not say it is.

### 24.4 Consequence codes — taints

| Code | When |
|---|---|
| `taint_deletes_pods` | A `NoExecute` taint being added or re-valued removes pods running here. Carries the sentence that PodDisruptionBudgets do not apply |
| `taint_deletes_unmanaged` | Some of them have no controller — deleted and gone, here and everywhere |
| `taint_deletes_daemonset` | Some are DaemonSet-managed. The DaemonSet controller tolerates the node's own *condition* taints and nothing else, so a taint you write removes its pods and it will not place them back while the taint stands. Usually log shipping, the CNI agent or node metrics |
| `taint_delayed_deletion` | Some tolerate it only for a bounded time and go when the timer expires |
| `taint_pods_unknown` | A `NoExecute` taint is being added **and** the pod listing failed. The one case where the console cannot say what the button does |
| `taint_removed` | The node stops excluding work it was reserved against |
| `taint_control_plane_opened` | `node-role.kubernetes.io/control-plane` (or the pre-1.24 `master`) is being removed, so application pods will be scheduled alongside the API server and etcd |

### 24.5 `POST /api/nodes/{name}/labels/plan` and `PUT /api/nodes/{name}/labels`

```json
{ "labels": { "team": "payments", "disktype": "ssd" } }
```

**The complete map**, for the same reason the taint list is complete: a key the
caller leaves out is a key this removes. A patch of only what changed cannot
express a removal without a second field, and a form whose "delete" list is
separate from its "set" map is a form where the two disagree.

Values are strings. A number or a boolean is refused here as `422 invalid`
naming the key, rather than by the API server, which refuses it with a schema
error naming a type.

The plan returns `current`, `requested`, `added`, `changed` (`{before, after}`
per key), `removed`, `dependents`, `pods_checked`, `unavailable`, `blocked`,
`consequences` and `gate`.

`dependents` names the pods **on this node** whose `spec.nodeSelector` or whose
required node affinity mentions a key being removed or re-valued — keys only, no
evaluation. Evaluating the rule would be a second and more confident claim:
nothing is re-checked for a running pod, so the verdict would describe a
placement that already happened. `null`, never `[]`, when the pod listing failed.

The write carries the same refusal order as §24.3, the same `PUT` concurrency
rule (`context.currentLabels` on a `409`), and the same gate. A removed key is
sent to the API server as an explicit `null` inside the merge patch: a merge
patch over a map *merges*, so a key the caller dropped would survive untouched.

**`applied: true` means the labels are stored, and nothing else.** No pod moves
because of this write.

### 24.6 Consequence codes — labels

| Code | When |
|---|---|
| `label_removed` | Keys are being removed. States that nothing running is evicted, and that the effect appears at the next rollout |
| `label_reserved_prefix` | A `kubernetes.io/`-family key is being added, removed or re-valued. The kubelet re-applies some of these when it next registers and never re-applies others, and which is which depends on its flags and on a cloud provider no API here reports — so the console says it cannot tell you. `topology.kubernetes.io/zone` is named specifically: volume topology is matched against it |
| `label_role_changed` | A `node-role.kubernetes.io/*` key is being added, removed or re-valued, changing the ROLES column in `kubectl get nodes` and on §5's node list. Both directions, because a role appearing is as much a change as one disappearing. It grants and removes nothing — a node-role label is a label |
| `label_pods_depend` | Pods here were placed by a rule naming a key that is changing |
| `label_pods_unknown` | Keys are changing **and** the pod listing failed |

### 24.7 What §24 is not

**It does not move a workload.** Removing a label that a Deployment's
`nodeSelector` names leaves every one of its pods exactly where they are. Drain
moves pods; a label change changes where the scheduler will put them next.

**It does not defeat a PodDisruptionBudget on purpose.** A `NoExecute` taint
bypasses budgets because of how Kubernetes implements taint-based removal, not
because this console offers a way around them. There is no flag here that turns
that off and none that turns it on, which is why the fact is disclosed rather
than made configurable.

**It does not report whether a removed label comes back.** The kubelet's
re-registration behaviour is not visible through any API this console reads.

---

## 25. Certificate signing requests — reading one before deciding it

### 25.1 The field no other screen has

`kubectl get csr` shows a name, a signer, a requestor and an age.
`kubectl certificate approve` takes a name. Neither shows what the request asks
to **become**, and that is a different field from who asked:

* `spec.username` is the identity that submitted the request. The API server
  sets it from the submitter's own credentials.
* The **common name** and **organizations** inside `spec.request` — a PKCS#10,
  base64-encoded — are the identity the issued certificate would carry.

A request submitted by an ordinary user whose organization is `system:masters`
is a cluster-admin credential. The API server's authorizer treats that group as
cluster-admin **before RBAC is consulted**, so no Role bounds it and no
RoleBinding takes it back; the only revocation is rotating the CA that signed
it, which invalidates every other certificate that CA issued. In the listing and
in the approve command it looks exactly like a kubelet renewing its certificate.

So §25 decodes the request and puts the subject, the organizations and the SANs
on the screen before the confirming call. The decode is **pure and total**: it
runs inside the row shaper, so a malformed `spec.request` must not take a whole
listing down with it, and a request that could not be decoded comes back with
every derived field `null` and `decode_error` set. **Never an empty subject** —
that renders as a certificate asking for nothing, which is the most reassuring
possible description of one nobody could read.

### 25.2 The typed row

`certificates.k8s.io/v1/certificatesigningrequests` is browsed through §4's
generic path, which returns this row because `certificatesigningrequest_row` is
registered. There is deliberately no listing endpoint of its own — a second
listing would be a second shaping of the same object.

| Field | Meaning |
|---|---|
| `state` | `Pending`, `Approved`, `Issued`, `Denied`, `Failed` — see below |
| `issued` | Whether `status.certificate` is present. A real boolean: the object was read |
| `requestor`, `requestor_groups` | Who asked, from `spec.username`/`spec.groups` |
| `subject` | `{common_name, common_names, organizations, organizational_units}`, or **`null`** when the request could not be decoded |
| `dns_names`, `ip_addresses`, `email_addresses`, `uris` | SANs. `[]` is a real "none requested"; `null` means nobody could read it |
| `key` | `{algorithm, size, curve}`. `algorithm` is `null` for a key type this console does not recognise, because a key it cannot name is one whose strength it cannot judge |
| `signature_valid` | Whether the request verifies against the key it carries. `null` when undecodable |
| `signer_known` | Whether `spec.signerName` is one of the three `kubernetes.io/…` signers |
| `decode_error` | Why the decode failed, or `null` |

**`Approved` and `Issued` are two states, not one.** Approving records a
condition; a **signer** then has to act. `kube-controller-manager` signs only
`kubernetes.io/kube-apiserver-client`,
`kubernetes.io/kube-apiserver-client-kubelet` and `kubernetes.io/kubelet-serving`
— and only if it was started with a signing CA, which no API here reports.
`kubernetes.io/legacy-unknown` is deprecated and is **not** signed by it, so
`signer_known` groups that with the custom signers rather than with the three.
A request for a signerName nothing signs sits `Approved` with no certificate,
indefinitely, looking exactly like a success.

`Failed` outranks `Approved` in the derivation, because a request the signer
refused after approval is over rather than still coming. `Denied` outranks
everything: the API makes it and `Approved` mutually exclusive.

### 25.3 `POST /api/certificates/signing-requests/{name}/plan`

```json
{ "decision": "Approved" }
```

`Approved` or `Denied` — the **condition types** the API uses, not verbs, because
that is what lands in the object and what `kubectl describe csr` prints. A body
naming an action and an object naming a state is one translation layer where a
typo becomes an approval.

Ungated and unaudited: one read and a decode. It does not dry-run the update.
The response carries `request` (the row above), `resourceVersion`, `decision`,
`blocked`, `consequences` and `gate`.

A request that has **already been decided** answers `200` with `blocked` set
rather than `422`, as §20's, §21's and §24's plans do: this is the screen where
somebody works out what happened, and an error alone would withhold the decoded
subject at that moment.

### 25.4 `PUT /api/certificates/signing-requests/{name}`

The plan's body plus `dryRun` (defaulting to true), `resourceVersion` and
`acknowledgeConsequences`.

`PUT` because §0.4 applies **and** because the approval subresource takes the
whole object: the caller sends the version they were looking at, it is checked
here — producing a `409 conflict` carrying `context.currentResourceVersion` and
`context.currentState` — and it rides inside the object, so the API server
refuses a stale write too. The object PUT back keeps its `managedFields`: an
update that dropped them hands the API server a different object than the one it
stored, over a decision that cannot be taken back.

**Two permissions, and the second is the one people miss.** The funnel preflights
`update certificatesigningrequests/approval`. That is not enough: the API
server's `CertificateApproval` admission plugin separately requires the
**`approve` verb on `certificates.k8s.io/signers`, with the request's
signerName as the resource name**. A console that preflighted only the first
would enable the button, pass its own check, and be refused by the API server —
§13's `routes/custom-host` failure exactly. The signer check is made from inside
the apply step, after the gate, so a read-only console answers
`mutations_disabled` rather than sending somebody to edit a ClusterRole.

**A decision cannot be changed.** The API server refuses any update that rewrites
an existing `Approved` or `Denied` condition. There is no un-approve, here or in
`kubectl`, and §25 refuses one locally with a message naming which decision was
made and when — the API server's own refusal names a field path.

**`applied: true` means the condition is recorded**, and nothing more. Whether a
certificate exists is `state: Issued`, in a later read. The response carries
`request` as it was read so the two statements stay separate in the same payload.

The gate is `ADMIN_ALLOW_MUTATIONS` alone, for §23's reason rather than §5.5's:
the real control here is already a permission, and a finer one than a flag could
be. `approve` on `signers` is granted **per signerName**, so a cluster can permit
this console to approve kubelet-serving certificates and nothing else.

### 25.5 Consequence codes

Deliberately **empty for an ordinary request** — a kubelet renewing a certificate
it already holds, decoded cleanly, for a signer this cluster runs. That is nearly
every CSR on a running cluster, and a checkbox that appears on all of them is one
nobody reads on the day a request is not a renewal.

| Code | When |
|---|---|
| `csr_grants_cluster_admin` | Approving, and the organizations include `system:masters`. Cluster-admin ahead of RBAC, revocable only by rotating the CA |
| `csr_grants_node_identity` | Approving, the organizations include `system:nodes`, **and the subject is not the requestor**. A node identity being handed to something that is not already that node — ordinary for a bootstrap token, and not ordinary otherwise |
| `csr_subject_is_not_requestor` | Approving, and the common name differs from `spec.username`. Issuing one identity a credential for another |
| `csr_no_known_signer` | Approving, and `spec.signerName` is not one of the three built-ins (`legacy-unknown` included). Approval may leave it Approved with no certificate forever |
| `csr_request_undecodable` | Either decision, and the PKCS#10 could not be read. The subject is unknown rather than empty |
| `csr_signature_invalid` | Either decision, and the request does not verify against the key it carries |
| `csr_deny_blocks_node` | Denying, and the subject is a `system:node:…` identity. That node will not become Ready, or will lose API access when its current certificate expires |

Two facts are stated as a **permanent banner rather than a checkbox**, because
they are true of every decision: that it cannot be changed, and that approving is
not issuing. A tick that always appears is a tick nobody reads.

### 25.6 What §25 is not

**It does not issue certificates.** No signer runs here, and nothing in this
console signs anything. `Issued` is a state reported from a later read, never a
claim this console makes about its own write.

**It does not delete requests.** Kubernetes' own cleanup controller removes
finished requests on a timer. A console delete would be §4's generic one.

**It does not auto-approve.** There is no rule engine, no allowlist of subjects
and no batch approve. Every decision is one request, previewed and confirmed by a
person — which is the point, given what the preview says.

---

## 26. Deleting a namespace — the blast radius

### 26.1 The failure §0.1 already names

§0.1's motivating story ends *"an operator reading 'this namespace has no pods'
acts on it — during a cleanup, that means deleting the namespace."* Every read in
this console is built so that sentence cannot be produced. The delete itself was
not: §4's delete previews `before=live, after=null`, which is honest and useless
here — what disappears is not in the namespace object. Four of those things are
not obvious from it, and one of them is not reversible:

**The volumes.** A PersistentVolumeClaim goes with the namespace. What happens to
the *volume* behind it is decided by the PersistentVolume's
`persistentVolumeReclaimPolicy`, on a cluster-scoped object nobody is looking at.
`Delete` means the storage provider destroys the disk — the data is gone, not
unbound. `Retain` means the volume survives as `Released` and needs clearing by
hand. Those are opposite outcomes behind one button.

**The addresses.** A `LoadBalancer` Service takes its cloud load balancer and its
external IP with it. Recreating the Service later gets a different address, and
whatever points at the old one — DNS, a firewall rule, a partner's allowlist —
keeps pointing at nothing.

**The admission webhooks.** A ValidatingWebhookConfiguration whose backing
Service lives in the namespace is cluster-scoped: it survives the delete and
stops having anything to talk to. At `failurePolicy: Fail` — the **default** in
`admissionregistration.k8s.io/v1` — it then refuses every write it intercepts,
cluster-wide. That is §19's finding arriving one step earlier: before the
namespace goes rather than after.

**The finalizers.** A namespace that will not finish deleting is the most common
complaint about this operation, and the cause is always a finalizer whose
controller is not running. §26 lists them *before* the delete — and, because the
plan is a read, the same plan run against a namespace already in `Terminating` is
the diagnosis of why it is stuck.

### 26.2 `GET /api/projects/{name}/delete-plan`

Ungated and unaudited: reads only, like §17's, §18's and §25's plans. It is
**not** a dry run — a dry run is a write request the caller has not made yet, and
§4's projection for a delete is the one view that says nothing about any of the
above.

```json
{
  "name": "prod",
  "phase": "Active",
  "resourceVersion": "4210",
  "deletionTimestamp": null,
  "namespaceFinalizers": [],
  "inventory": { "kinds": [
    { "group": "", "version": "v1", "resource": "pods", "kind": "Pod",
      "count": 14, "truncated": false },
    { "group": "", "version": "v1", "resource": "secrets", "kind": "Secret",
      "count": null, "truncated": null }
  ] },
  "volumes": [
    { "claim": "postgres-data", "volume": "pv-9c2f", "phase": "Bound",
      "capacity": "200Gi", "storage_class": "gp3",
      "reclaim_policy": "Delete", "reason": null }
  ],
  "load_balancers": [ { "name": "edge", "addresses": ["203.0.113.9"] } ],
  "webhooks": [
    { "configuration": "prod-policy", "kind": "ValidatingWebhookConfiguration",
      "webhook": "policy.example.com", "service": "admission",
      "failure_policy": "Fail" }
  ],
  "finalizers": [
    { "resource": "/persistentvolumeclaims", "name": "postgres-data",
      "finalizers": ["kubernetes.io/pvc-protection"] }
  ],
  "propagationPolicy": "Background",
  "blocked": null,
  "consequences": [],
  "unavailable": [], "partial": false,
  "gate": { "enabled": true, "detail": "…" }
}
```

**The inventory is driven by discovery, never by a curated kind list.** A curated
list is a *completeness claim* that goes stale the first time somebody installs a
CRD, and the namespace whose contents matter is the one full of an operator's
custom resources. Namespaced kinds advertising `list`, at their **preferred**
version only, deduplicated by `(group, resource)` — a CRD serving `v1beta1` and
`v2` side by side holds one set of objects, and listing both would count them
twice and double every finding scanned out of them.

**There is no grand total.** `events` is served by both the core group and
`events.k8s.io` over the same underlying objects, so any sum across kinds
double-counts them. A single headline number is the wrong headline anyway: what
matters is which volumes are destroyed, not that the namespace holds 1,247
things.

**Three fields are tri-state, and each is a §0.1 corollary with a cluster behind
it.**

| Field | `null` means | Never |
|---|---|---|
| `inventory.kinds[].count` | The listing was refused, or was truncated and the API server sent no `remainingItemCount` | `0`, which reads as "this namespace holds no claims", and `200`, which reads as "there are exactly two hundred" |
| `volumes[].reclaim_policy` | The volume is unbound (with a `reason` saying so), or could not be read (with a `reason` saying *that*) | Either real answer. `Delete` and `Retain` point in opposite directions, so a guess is a claim about somebody's database made by a console that did not look |
| `webhooks` | **Neither** configuration listing answered | `[]`, which says "no webhook points here" — the most reassuring possible description of "we could not look" |

A `webhooks` list is returned whenever *one* listing answered: a partial answer
is still an answer about what it saw, and discarding it would hide a real
finding.

A namespace already in `Terminating` answers `200` with `blocked` set, as §25's
plan does for a decided request — the finalizer list below it is the point.

### 26.3 `DELETE /api/projects/{name}`

Body: `dryRun` (defaulting to true) and `acknowledgeConsequences`. **The body is
optional**: a `DELETE` body is legal and occasionally stripped by an
intermediary, and losing it must fail in the safe direction — no body means the
default body, which is a projection with nothing acknowledged and cannot delete
anything.

The write is §4's `delete_resource` unchanged: the same `apply_fn`, the same
`mutate()` call, the same preflight of `delete core/namespaces`, the same
`before=live, after=null` diff and the same audit row. §26 adds the plan, the
handshake and an audit sentence naming the volumes — not a second path to the
cluster.

Refusals, in order and all before the cluster is touched: the plan is recomputed
against the namespace **as it is now**, an already-terminating namespace is `422`,
and the acknowledgement check runs over those recomputed consequences. Nothing is
trusted from the plan the caller read: a volume can be provisioned, a
LoadBalancer can come up and a webhook can be installed between the two calls.

`propagationPolicy` is always `Background`. `Foreground` makes the call block
until the whole cascade finishes — a request that hangs for as long as the
slowest finalizer — and `Orphan` sounds like it saves the contents and does not:
the namespace controller deletes everything *in* the namespace regardless of
ownerReferences, so orphaning changes which objects outlive their owner, not
which objects survive.

**`applied: true` means the namespace has a `deletionTimestamp`**, and the
namespace controller has started. It does **not** mean the namespace is gone: it
can sit in `Terminating` indefinitely behind an unsatisfied finalizer. The
response's summary says exactly that rather than reporting a deletion that has
not finished.

### 26.4 Consequence codes

| Code | When |
|---|---|
| `namespace_destroys_volume_data` | A bound claim's volume has `persistentVolumeReclaimPolicy: Delete`. The disk is destroyed and nothing brings it back |
| `namespace_volume_fate_unknown` | A bound claim's volume could not be read. Whether its data survives is unknown — and this is explicitly *not* a report that it is safe |
| `namespace_releases_volumes` | A bound claim's volume is `Retain`. The data survives, as a `Released` volume no new claim can bind until its `claimRef` is cleared by hand |
| `namespace_drops_load_balancer` | A `LoadBalancer` Service is deleted with the namespace, releasing its address |
| `namespace_breaks_admission_webhook` | A webhook configuration is backed by a Service here — or the configurations could not be read, which is the same code with the "unknown" wording |
| `namespace_finalizers_may_hang` | An object in the namespace holds a finalizer, which is what leaves a namespace stuck in `Terminating` |
| `namespace_inventory_incomplete` | A kind's listing was refused, or held more objects than the 200 per kind this console reads. Every finding above is then a floor, not a total |

A namespace holding none of these produces **no consequences at all** and needs
no checkbox. The friction that is always present is §11.3's typed confirmation —
the operator types the namespace name — because the act is irreversible whether
or not it takes anything interesting with it.

### 26.5 What §26 is not

**It is not a general cascade previewer.** Computing what deleting an arbitrary
object takes with it means walking `ownerReferences` across every kind the
cluster serves — the same cost as the inventory above, for a far weaker payoff: a
Deployment taking its ReplicaSets and Pods is understood, and a namespace taking
a database's volume is not. §4's delete is unchanged and is still the way to
delete anything else.

**It does not clear finalizers.** Editing `spec.finalizers` to unstick a
`Terminating` namespace skips the cleanup those finalizers exist to guarantee,
which is usually an external resource nobody will now delete. §26 names the
finalizer and its holder; removing one is §4's YAML editor, deliberately.

**It does not snapshot anything first.** The mitigation on the destroyed-volume
consequence points at §22, which is a separate, acknowledged act. A delete that
quietly snapshotted would be a second write nobody asked for, and one that
silently failed to would be worse.

---

## 27. Acting as the operator (ADR-0007)

### 27.1 What changes, and what does not

By default every call this console makes to a cluster is made as that cluster's
one registered credential, and three consequences follow that §0.2, §10 and §17
already document: the preflight answers about the console, the audit trail's
actor is a name only this application can vouch for, and §17 asks who to bind
because it does not know who is asking.

A cluster registered with `impersonation_enabled: true` sends `Impersonate-User`
and `Impersonate-Group` on every call instead. The API server authenticates the
ServiceAccount, checks it holds `impersonate` on the requested subject, and then
evaluates **the whole request** — authorization, admission, and its own audit
record — as the operator.

**Off by default, per-cluster, and never a global switch.** A deployment with one
cluster whose `--oidc-issuer-url` matches the console's and a second with no OIDC
at all must be able to have this on for the first and off for the second.

`docs/adr-0007-impersonation.md` is the argument, including what the grant costs
and the two questions the implementation had to settle.

### 27.2 The flag

`ClusterPublic` carries `impersonation_enabled: boolean`, beside
`skip_tls_verify` and for the same reason: a setting that changes who the cluster
thinks is asking can never be silently in effect. `POST /api/clusters` and
`PUT /api/clusters/{id}` both accept it.

Setting it on a console that does not authenticate operators with OpenID Connect
is `422 invalid` naming the field. Accepted, it would produce a cluster that
refuses every operator at the request rather than at the setting — accurate, and
arriving on a page belonging to somebody who did not change it and cannot see it.

The flag is **nullable** in the schema, because it was added to a table that
already had rows and `schema_upgrade` only adds nullable columns. `null` means
"registered before this setting existed", which is off. It is the one nullable
boolean in this schema that is not a tri-state, and that is safe only because the
absent answer and the false answer call for the same behaviour.

### 27.3 `impersonation_unavailable` — 403

A new code in §1.3's vocabulary. The cluster asked to act as the operator and
this session cannot supply a cluster identity.

**Not `rbac_denied`**, for `mutations_disabled`'s reason inverted: there the
deployment is read-only and the operator's permissions are irrelevant, and here
the operator's permissions are exactly what could not be *established*. Reporting
either as a denial sends somebody to widen a ClusterRole that was already
correct. `context.reason` says which of four:

| `reason` | When |
|---|---|
| `auth_disabled` | The console runs in legacy proxy mode. `X-K8Boss-User` is advisory and caller-controlled, so it cannot name a cluster identity |
| `no_session` | No verified session on the request |
| `auth_source` | A local or LDAP session. If this console's password table could cause a request to arrive as `alice`, that table has become an identity provider the cluster trusts |
| `no_idp_username` | A session created before ADR-0007, carrying no issuer-stated username. The console will not derive one from its own normalised account name |
| `groups_absent` | The issuer sent no groups claim. **Absent is not empty** — see §27.5 |

**There is no fallback.** A request that cannot build impersonation headers for a
cluster that wants them fails; it does not proceed as the console. A read that
quietly reverted would show an operator data their own RBAC forbids, which is
§0.1's defect standard with a security consequence attached.

### 27.4 The identity that is sent

`Impersonate-User` is the username **the identity provider stated**, kept
verbatim on the session rather than derived from the console's own account row:
the console casefolds usernames for its own key, and a cluster whose
`--oidc-username-claim` yields `Alice@example.com` does not know anybody called
`alice@example.com`.

`Impersonate-Group` is **repeated, once per group** — never comma-joined. Go's
`http.Header` does not split a comma-joined value, so `Impersonate-Group: a,b`
reaches the authorizer as a single group of that literal name. The request
succeeds, is evaluated with none of the operator's group permissions, and
produces a page of denials that look like a cluster problem.

The groups are the issuer's, plus `system:authenticated`. That is the one group
sent that the issuer did not state, and §27.6 says why it is not the exception
ADR-0007 rejects.

`Impersonate-Uid` and `Impersonate-Extra-*` are **not sent**. This console has no
issuer-stated value for either, and inventing one is what the ADR rejects when it
refuses to send the console's own opinion about somebody's groups to an API
server.

### 27.5 Absent groups refuse; empty groups do not

`app/identity/oidc.py` keeps an **absent** groups claim apart from an **empty**
one, on the grounds that an issuer which did not tell us must not demote anybody.
Under impersonation that distinction stops being about console roles and becomes
load-bearing on the cluster.

Impersonating with an empty group list when the claim was merely omitted strips
every group-derived permission the operator actually holds, and produces a page
full of correctly-reported denials for permissions they have. So absent refuses,
naming the claim to configure. Empty is a real answer and impersonates fine.

The same rule applies at the storage boundary: a stored groups value the console
cannot parse degrades to **absent**, not to empty. Both readings are wrong and
only one of them is dangerous.

### 27.6 The calls that stay as the ServiceAccount

ADR-0007 requires these be a **list** rather than an emergent property of
whoever wrote the last endpoint. There are three, and a test enumerates them:

* **Discovery.** `discover()` memoises its catalog per cluster and hands the same
  one to everybody, so impersonating it would let whichever operator warmed the
  cache decide what every other operator sees the cluster as serving. It does not
  vary by person anyway: discovery reports what the **API** serves, not what the
  caller may do with it. Every question that is about the caller goes through
  §9's preflight, which *is* impersonated.
* **The connection test** (`POST /api/clusters/{id}/test`). It answers "can this
  console reach and authenticate to this cluster, and what may it do there",
  which is a question about the stored credential. Run as the operator it reports
  the operator's permissions instead, and a cluster whose ServiceAccount token
  had expired would still test clean for anyone whose own RBAC was fine.
* **The local kubeconfig fallback.** There is no `Cluster` row, so there is
  nothing carrying the opt-in and nothing that could have been opted in.

The last two are structural rather than conditional: they build transports that
may never impersonate at all, so no contextvar can make them.

### 27.7 `subject` on every `PreflightResult`

§9's results gain `subject`: the identity the `SelfSubjectAccessReview` answered
for. A review asks "may **this credential** do this", which is the question that
decides whether the write succeeds — so the answer was always correct, and always
correct about the wrong subject.

`403 rbac_denied` now names it too: `context.subject`, and the message reads
"`alice@example.com` cannot patch deployments in namespace …" rather than
"Cannot patch deployments". §11.4's disabled button can finally say *whose*
permission is missing, which decides which ClusterRole somebody goes and edits.

On a cluster that does not impersonate, `subject` is `the console's
ServiceAccount` — deliberately a phrase no one could mistake for a username. That
was silently true before and is now written down.

### 27.8 `impersonated_user` on every audit row

§10 rows gain `impersonated_user`, and the export gains a column. `null` means
the console acted as itself — a fact, not a gap, and never defaulted to `actor`.

Both are recorded because they answer different questions. `actor` is who used
this console and is a name only this application can vouch for.
`impersonated_user` is the name the **API server** saw, evaluated authorization
against, and wrote into its own audit log — so an incident review can join this
trail to the cluster's by a value neither side invented. That is a materially
stronger claim than ADR-0003 can make about a table this application owns.

Like `actor`, it is read from request context and is not a parameter of
`record()`: a caller that can pass the identity a write was made as can pass the
wrong one.

**It is hashed only when it is set.** Appending it to `HASHED_FIELDS` would
change the hash computation for every row ever written, including every row
written before the column existed — and the whole table would verify as `broken`.
That is a false tamper alarm delivered by a schema change. It lives in
`OPTIONAL_HASHED_FIELDS` instead: a key in the payload when set, no key when
null. Every mutation of the field still crosses that boundary and still breaks
the chain; see ADR-0007's *What the implementation decided*.

### 27.9 What §27 is not

**Not a way to give somebody permissions.** Impersonation narrows what the
console may do to what the operator may do. It never widens: an operator whose
own RBAC forbids a write gets a denial where the console alone would have
succeeded, which is the point.

**Not a replacement for the mutations gate.** §8's two gates stay independent.
`ADMIN_ALLOW_MUTATIONS` is a property of the deployment and is unaffected by who
is signed in; impersonation narrows the RBAC gate, not that one.

**Not a check that the issuers match.** The console cannot read a cluster's
`--oidc-issuer-url` and will not pretend to. Where the two differ, the console
asserts an identity the cluster has never heard of and every request is refused
as that person. That alignment is the operator's to get right, and the
registration form says so at the checkbox.

---

## 28. Disruption budgets — what they cover, and what they block

### 28.1 The object whose failure mode is silence

A PodDisruptionBudget is the only object in this contract that can be completely
broken while reading as correct. Three ways, and none of them appears in
`kubectl describe`:

**It may cover nothing.** A selector one label key away from the workload it was
written for matches no pod. The YAML is indistinguishable from a budget guarding
a production database. A team reads `minAvailable: 2`, believes it has
availability protection, and does not have it. This is §8.4's "a NetworkPolicy
that selects nothing is inert", pointed at availability.

**It may never allow an eviction.** `maxUnavailable: 0`, or `minAvailable` at or
above the pod count, means no voluntary eviction can ever succeed. That is
arithmetic — knowable the moment the budget is written — and today it is
discovered partway through a node drain that will not finish.

**Two budgets may cover one pod.** Kubernetes does not support this, and *how*
it does not is the point: the eviction API **refuses that pod outright**,
whatever either budget's `disruptionsAllowed` says. Both objects can read
`disruptionsAllowed: 5` and the pod is un-evictable by anyone. Nothing on either
budget says so, and the symptom is a drain that fails on one pod with a message
about a condition nobody set.

### 28.2 The typed row

`policy/v1/poddisruptionbudgets` is browsed through §4's generic path, which
returns this row because `poddisruptionbudget_row` is registered in the shaper
registry. The shaper is **pure**, so it carries only the findings derivable from
the object in front of it; everything that needs a pod listing is §28.3.

```json
{
  "name": "api", "namespace": "prod",
  "min_available": 1, "max_unavailable": null,
  "selector": { "matchLabels": { "app": "api" } },
  "unhealthy_pod_eviction_policy": "IfHealthyBudget",
  "disruptions_allowed": 1, "current_healthy": 2,
  "desired_healthy": 1, "expected_pods": 2,
  "disrupted_pods": [], "status_stale": false,
  "findings": [], "age_seconds": 86400
}
```

**Every status number is nullable and none may be rendered as `0`.** The
disruption controller writes them asynchronously, so a freshly created budget has
none. `disruptions_allowed: 0` means "no eviction is permitted right now" and is
the most consequential value on the row; `null` means the controller has not
spoken, and drawing that as `0` reports a budget as blocking a drain it may be
about to permit.

`unhealthy_pod_eviction_policy` reports `IfHealthyBudget` when the field is
absent, rather than `null`. That is the API's default and it is the **stricter**
behaviour — the reverse of what the word "policy" suggests — and `null` would
read as unknown for a field whose absence has one defined meaning.

`status_stale` is tri-state. `true` means `observedGeneration` is behind
`metadata.generation`, so every count describes the *previous* spec. `null` means
the controller has written no `observedGeneration` at all, and reporting that as
up-to-date would vouch for numbers that do not exist.

### 28.3 `GET /api/disruption/budgets`

Query: `namespace`, optional. Omitted means the whole cluster, which is the view
that finds the budget nobody has looked at since its workload was renamed.

The **budget listing is primary and raises**: an empty table for a cluster whose
budgets could not be read would say "nothing constrains eviction here", which is
the finding rather than the failure. The **pod listing is collected**: losing it
costs every pod-derived field and leaves each budget's own declared numbers on
screen, because those came from an object that answered.

Rows are §28.2's, plus:

| Field | Meaning |
|---|---|
| `selected_pods` | Pods this budget covers, counted from a live listing. **`0` is the finding**; `null` is a listing that did not answer, or a selector that could not be evaluated |
| `undecidable_pods` | How many pods could not be evaluated against this selector, or `null` |
| `findings[]` | `{code, label, detail}`, the object-derived ones plus the pod-derived ones below |

And one top-level key beside `items`:

`overlappingPods` — `[{pod, budgets[]}]`, the inverse index. This is the answer
no single row can carry. `null` means the pod listing failed; **never `[]`**,
which would say the console checked and found none.

### 28.4 Finding codes

| Code | Derived from | When |
|---|---|---|
| `pdb_never_allows_disruption` | the object | `maxUnavailable: 0` or `0%`, or `minAvailable` at/above `expectedPods`, or `minAvailable: 100%`. No voluntary eviction can succeed. The `minAvailable` cases say "at this pod count", because scaling up changes them; `maxUnavailable: 0` says "at any replica count", because it does not |
| `pdb_blocking_now` | the object | `disruptionsAllowed` is 0 and the arithmetic does not make it permanent. Usually clears on its own. **Never emitted alongside the previous code** — both are true, one is actionable, and two rows would make the operator choose |
| `pdb_no_constraint` | the object | Neither `minAvailable` nor `maxUnavailable` is set. The API server accepts it and the controller permits every eviction |
| `pdb_status_stale` | the object | `observedGeneration` behind `generation` |
| `pdb_selects_nothing` | the pod listing | `selected_pods` is exactly 0 |
| `pdb_selection_unknown` | the pod listing | A `matchExpressions` operator this console does not model. The count is unknown, and this is explicitly **not** a report that the budget covers nothing |
| `pdb_overlaps` | the pod listing | A covered pod is also covered by another budget, named in the detail |

### 28.5 The selector matcher is tri-state, and the callers own the third state

There is **one** label-selector matcher in this tree,
`shaping.label_selector_matches`, and it returns `True`, `False` or `None`.
`None` means a `matchExpressions` operator outside the four Kubernetes defines:
*we could not decide whether this object is selected*. It is not a match and it
is not a non-match.

There was a second matcher, `services.workloads.selector_matches`, that resolved
an unmodelled operator to `False`. That was defensible where it was written —
attributing other workloads' pods to a row is worse than attributing none — and
it was reused by §5's drain plan, where the same `False` read as "no
PodDisruptionBudget covers this pod" in a plan an operator studies before
draining a node. It has been removed rather than given a third state, because a
matcher that resolves the ambiguity for its author hands that resolution to
every later caller with nothing at the call site to show it happened.

The three callers each spend the `None` where they know what it costs:

| §  | Caller | On `None` |
|---|---|---|
| 28 | `services/disruption.py` | `selected_pods: null`, an `undecidable_pods` count, and `pdb_selection_unknown`, whose detail says in words that this is not a report of zero. `False` would manufacture `pdb_selects_nothing` and the operator deletes a working budget |
| 5  | `admin/nodes.py` drain plan | the budget is named in the pod's `pdbUnknown[]` — coverage unknown, not absent — and is **not** a blocker. The eviction subresource is the enforcer, and blocking a drain over a selector we could not parse is how `force` becomes reflex |
| 6  | `services/workloads.py` | `restarts_24h: null` for the whole workload as soon as one pod is undecidable. Summing the rest is a floor rendered as a total, and §0.1's rule applies: a number we could not derive is `null`, never a partial count |
| 11 | `services/network.py` | `null` coverage on the policy, per §8.3 |

### 28.6 What §28 is not

**It does not report what is enforced.** The eviction subresource is the
enforcer, and `disruptionsAllowed` is a number a controller wrote at some past
moment. So no field is called `safe`, `protected` or `will_block`.

**It does not write.** Creating, editing and deleting a budget is §4's
`POST`/`PUT`/`DELETE` through the single mutation funnel. A write here would skip
the gate, the preflight, the dry-run diff and the audit row — and a budget edited
without a diff shown first is exactly the change that turns a routine drain into
a stalled upgrade.

**It does not say which workloads *should* have a budget.** A multi-replica
Deployment with none is evicted freely, which is very often correct. Flagging it
would put the console's opinion about somebody's availability requirements on a
page, and a finding that fires on most rows is one nobody reads on the day it
matters.

---

## 29. Quota advice — will the next workload be admitted, and what refuses it

### 29.1 Two refusals a 403 makes look alike

A ResourceQuota refuses at admission, and the refusal is a sentence somebody
parses under pressure. Everything needed to answer first is already in the
namespace: `spec.hard` minus `status.used` is headroom, and a workload's demand
is arithmetic over its own containers.

The obvious refusal is **no room**, and it is fixed by deleting something or
raising the limit. The one that costs an afternoon is different:

> A quota that bounds a compute resource makes that resource **compulsory**. If
> any quota in the namespace bounds `requests.cpu`, every container created
> there must state it — and one that does not is refused with *"must specify
> requests.cpu"* **even when the quota is one percent used**.

A LimitRange with a `defaultRequest` for `Container` discharges that by
injecting the value at admission. So a namespace with a quota and no LimitRange
is a namespace where an ordinary Deployment cannot be created at all, the
message names a *field* rather than the missing object, and the fix is a second
object nobody mentioned.

§29 keeps the two apart, because they send an operator to two different places.

### 29.2 `GET /api/quota/{namespace}`

What bounds the namespace, and what every pod in it must declare.

```json
{
  "namespace": "prod",
  "quotas": [ { "name": "team", "applies": true,
                "resources": [ … ], "headroom": { "requests.cpu": "1" },
                "findings": [] } ],
  "limitRanges": [ … ],
  "containerDefaults": { "requests.memory": "256Mi" },
  "mandatory": ["requests.cpu", "requests.memory"],
  "findings": [ … ],
  "unavailable": [], "partial": false
}
```

`quotas: null` is a refused listing — **never `[]`**, which is the answer that
says nothing bounds this namespace and every workload is admitted. `mandatory`
is `null` for the same reason.

`applies` is tri-state. `true` for an unscoped quota, which counts everything.
**`null`** for one carrying `scopes` or a `scopeSelector`: which pods it counts
depends on the priority class, terminating state or best-effort-ness of an
object that does not exist yet, and this console will not pick a side. Including
it invents a refusal from a quota that may not govern the workload; excluding it
hides a real one.

`cpu` and `memory` are the API's own aliases for the `requests.` forms and are
normalised to them. Treating them as separate keys would report a namespace as
bounding two things when it bounds one.

### 29.3 `POST /api/quota/{namespace}/preview`

```json
{ "replicas": 3, "containers": [ { "name": "app",
    "requests": { "cpu": "500m", "memory": "1Gi" } } ] }
```

A `POST` that writes nothing, like §17's and §18's plans: replicas and a
container list do not belong in a query string. The response is §29.2 plus:

```json
{ "preview": {
    "replicas": 3,
    "needed": { "requests.cpu": "1.500" },
    "unsetMandatory": [ { "container": "app", "resource": "requests.cpu" } ],
    "checks": [ { "quota": "team", "resource": "requests.cpu",
                  "needed": "1.500", "headroom": "1", "verdict": "refused" } ],
    "verdict": "refused" } }
```

**`verdict` is `admitted`, `refused` or `unknown`**, and the third is a real
answer rather than a failure. Any refusal refuses; otherwise any unknown is
unknown. It is `unknown` whenever the arithmetic cannot be completed honestly:

* a `status.used` the quota controller has not written yet — **absence is not
  zero**, and reading it as zero gives the roomiest possible answer at the
  moment this console knows least, to the person about to deploy;
* a scoped quota, per §29.2;
* a LimitRange listing that did not answer, which additionally **suppresses the
  `unsetMandatory` check entirely** rather than computing it from an empty
  default map. That conclusion is wrong in the dangerous direction: it reports a
  workload as refused for a missing value a default the console could not read
  may well supply.

`unsetMandatory[]` is the §29.1 refusal and is listed separately from `checks`
because raising a limit does not fix it.

A **declared `"0"`** is a value: it satisfies the compulsory rule and consumes no
headroom. Collapsing it into "unset" would report a refusal the API server would
not make.

An object-count bound the request does not describe — `count/deployments.apps`
from a body that does not say how many it creates — is **skipped**, not
verdicted. Arithmetic over an invented number is not advice.

### 29.4 Finding codes

| Code | Scope | When |
|---|---|---|
| `quota_requires_unset_resource` | namespace | Some quota bounds a compute resource and no LimitRange defaults it. Every pod omitting it is refused regardless of usage |
| `quota_usage_unknown` | quota | `status.used` has no entry for a bounded resource. No headroom can be computed from it |
| `quota_exhausted` | quota | A bound is at its hard limit |
| `quota_scoped` | quota | Scopes or a scope selector, so its verdict is withheld |

`quota_requires_unset_resource` is namespace-level rather than per-quota because
the rule is a property of the namespace: *any* quota bounding the resource makes
it compulsory for *every* pod, and the fix is one LimitRange rather than a change
to any quota.

### 29.5 What §29 is not

**Not admission.** The API server admits. §4's dry-run create asks it and gets
the authoritative answer for a manifest that exists; this answers from arithmetic
for one that does not, and says the two things a 403 does not — how much room is
left, and which of a quota's bounds is the tight one. No field is called
`will_be_admitted`.

**Not a write.** Editing a quota or a LimitRange is §4's `PUT` through the single
mutation funnel.

**Not a scheduler.** Fitting inside a quota is not fitting on a node. A workload
this endpoint admits can still sit `Pending` because no node has the room, which
is §5's question and a different one.

**Not a complete model of admission.** LimitRange `max`/`min`/`maxLimitRequestRatio`
bounds, Pod-scoped LimitRange items and priority-class scope selectors are read
and reported but not evaluated into the verdict. Where any of them could change
the answer, the verdict is `unknown` rather than confidently wrong.

---

## 30. Granting and revoking a role — what the binding actually confers

### 30.1 A `roleRef` is a name, not a capability

`subjects: [alice]` under `roleRef: ClusterRole/admin` is three words, and §4's
editor can already write them. Its diff is honest and useless: a name appearing
in a list. What that name can now *do* is in a different object, and the operator
who confirmed the grant did not open it.

Two ordinary examples, both of them the reason the grant was made:

* `admin` in a namespace includes `create rolebindings`. The grantee can now
  grant themselves, and anybody else, every other role bindable here — and
  revoking this one binding later does not undo what they granted in between.
* `edit` includes `create pods/exec`. A shell in a pod reads every Secret
  mounted into it and every value in its environment, so the role hands over the
  namespace's credentials **while having no rule about Secrets at all**. A
  console that looked only for a `secrets` rule would confirm this grant as
  touching none.

§30 resolves the role and puts what it confers on the screen where the decision
is made. Three further facts about RoleBindings, none of them visible in one:

**A binding to a role that does not exist is accepted.** The API server does not
validate `roleRef` against anything. The binding grants nothing — until somebody
creates a role by that name, at which point it starts granting whatever that role
holds, with nobody making a second decision. Anyone holding `create roles` in the
namespace can be that somebody.

**A role that could not be read grants *unknown*, never nothing.** §0.1's
corollary aimed at a security control: "this role grants nothing" is the one
answer that must not come out of a read that did not happen, and it would be
produced on the screen where somebody decides to bind it.

**Removing a subject from a binding is not revoking their access.** They may be
named in another binding here, or in a ClusterRoleBinding — which grants
cluster-wide and therefore also here. `applied: true` is a claim about a subject
list, not about a permission.

### 30.2 `POST /api/access/namespaces/{namespace}/grants/plan`

`{"operation": "grant"|"revoke", "role": {"kind": "Role"|"ClusterRole", "name": "…"},
"subject": {"kind": "User"|"Group"|"ServiceAccount", "name": "…", "namespace": "…"}}`

Reads only — ungated and unaudited. It does not dry-run the write; a dry run is a
write request the caller has not made yet, and it needs the preflight the funnel
does.

```json
{ "namespace": "prod", "operation": "grant",
  "role": {"kind": "ClusterRole", "name": "edit"},
  "subject": {"kind": "User", "name": "alice", "apiGroup": "rbac.authorization.k8s.io"},
  "binding": {"name": "edit", "namespace": "prod", "role": {"kind": "ClusterRole", "name": "edit"}},
  "createName": null, "resourceVersion": "7781",
  "capability": {"state": "present", "rules": [ … ], "rule_count": 12,
                 "aggregates": false,
                 "powers": [{"code": "grant_pod_exec", "detail": "…"}]},
  "currentSubjects": [ … ], "requestedSubjects": [ … ],
  "residual": null, "blocked": null,
  "consequences": [ … ], "unavailable": [], "partial": false, "gate": { … } }
```

`capability.state` is `present`, `absent` or `unreadable`. **`rule_count` is
`null` for the last two and for a present ClusterRole whose `aggregationRule` the
controller has not filled in yet** — never `0`. An explicit `rules: []` is a real
zero and reads as one; `aggregates` says which case a null is.

`subject.namespace` defaults to the namespace being granted in for a
ServiceAccount. It is filled rather than omitted because a ServiceAccount subject
with no namespace matches nobody, and a binding that matches nobody is a grant
reported as made that applies to no identity.

A request that cannot proceed comes back `blocked` with a **200**, not a 422 —
this is the screen where the grant is decided, and the residual list below is
what answers "then where does their access come from".

### 30.3 The five powers

`capability.powers` is deliberately not every verb the role holds. A finding that
fires on most roles is one nobody reads on the day it matters (§28.6's rule).
Each of these is either invisible from the role's name or reaches further than
the name suggests:

| Code | When |
|---|---|
| `grant_full_control` | A rule with `*` verbs on `*` resources in `*` groups |
| `grant_privilege_escalation` | `create`/`update`/`patch` on `rolebindings`, or the `escalate` or `bind` verbs — the two the API server checks when refusing to let somebody hand out more than they hold. **Fires on the stock `admin` ClusterRole**, which is correct: binding `admin` delegates the namespace's RBAC with it |
| `grant_impersonate` | The `impersonate` verb over users, groups or ServiceAccounts. Managing ServiceAccounts is not impersonating them, and does not fire |
| `grant_secret_read` | `get`/`list`/`watch` on `secrets` |
| `grant_pod_exec` | `create` on `pods/exec` or `pods/attach`. `resources: [pods]` is **not** this — RBAC names the subresource separately |

### 30.4 Consequence codes

| Code | When |
|---|---|
| `grant_confers_full_control` | The wildcard power |
| `grant_confers_privilege_escalation` | The escalation power |
| `grant_confers_impersonation` | The impersonation power |
| `grant_confers_secret_access` | Secret reads **or** exec. The detail says which, because exec reaching them without a Secrets rule is the surprising half |
| `grant_role_unreadable` | `state: unreadable`. The binding is still made and still grants whatever the role holds |
| `grant_role_absent` | `state: absent`, and when it starts granting |
| `grant_rules_not_aggregated` | A present aggregate whose rules are unwritten |
| `revoke_access_remains` | Another binding still names the subject |
| `revoke_residual_unknown` | The ClusterRoleBinding listing was refused, or answered with more pages behind it |

**The narrower three are suppressed under `grant_confers_full_control`.** Both
are true; one is actionable, and asking somebody to tick "this also reads
Secrets" underneath "this grants everything" lengthens the list without improving
the decision — §28.4's rule about `pdb_never_allows_disruption` and
`pdb_blocking_now`, at the handshake layer. `powers` still carries all of them,
because that list is a description rather than a question.

### 30.5 `residual` — what a revoke does not take away

Present on a revoke plan and on its write response, `null` on a grant:

```json
{"namespace_bindings": [{"name": "edit", "namespace": "prod", "role": {…}}],
 "cluster_bindings": null, "cluster_truncated": false}
```

`cluster_bindings: null` means the cluster-wide listing was **refused**, and the
read names itself in `unavailable[]`. `[]` means the cluster was searched and
nothing else grants this — which is the sentence somebody closes a ticket on, so
it is never produced by a read that did not happen.

`cluster_truncated: true` is the same statement one page weaker: the listing
answered and there are more pages behind it, so an empty `cluster_bindings`
means *the first page did not name them*. What was read is kept — it is strictly
more useful than discarding it — and `revoke_residual_unknown` fires either way.

**A truncated listing of the namespace's own bindings is a refusal, not a
warning.** Which binding to write, whether the subject is already named and what
else grants them access are all read off that one listing; each answered from a
first page is a confident answer about bindings nobody looked at. The plan comes
back `blocked` and the write is a `422`.

The mitigation on both revoke codes points at §23: this endpoint subtracts
objects, and only `SubjectAccessReview` asks the API server's whole authorization
chain. §30 never claims to have computed effective access.

### 30.6 `PUT /api/access/namespaces/{namespace}/grants`

The plan's body plus `dryRun`, `resourceVersion` and `acknowledgeConsequences`.
One funnel call, with the verb it is actually about to use:

* A grant with no binding to extend is a **`create`** of a whole RoleBinding.
* Everything else is a **`patch`** of `subjects`, carrying the plan's
  `resourceVersion` (§0.4) — the array is replaced wholesale, so without it a
  concurrent grant is silently dropped rather than answered with a `409`.

The empty subject list is sent as `[]` and not as `null`. Both are the same grant
— none — but only `[]` shows in the diff that the binding survives with nobody in
it, which is the fact §30 refuses to hide.

**`applied: true` means the binding's subject list is what you sent.** On a
revoke it does not mean the subject can no longer act here; `residual` travels
with the response so that reading cannot be made.

### 30.7 What §30 is not

**Not a second path to the cluster.** §4 already writes RoleBindings. This
endpoint exists to attach a preview and a handshake to an action whose
consequences are not in its own diff, for the reason §20, §24, §25 and §26 do.

**Not a multi-binding editor.** If two RoleBindings in the namespace share a
`roleRef`, §30 refuses and names them: "remove alice from `view`" has two
meanings there, and silently picking the first would report a revoke that left
her bound through the second.

**Not a delete.** Revoking the last subject leaves the binding with an empty
`subjects` list, which grants nothing. Deleting it instead would be a second verb
whose blast radius — every *other* subject in it — this plan would then also have
to explain, for the sake of tidiness. §4 deletes it.

**Not a role editor.** `roleRef` is immutable, so "move alice from `view` to
`edit`" is a revoke and a grant: two writes, two diffs, two audit rows. §30 does
not hide that behind one button. It also never invents a binding name — a
collision with a binding of another `roleRef` is refused rather than renamed to
`view-1`, because a console that picks a name the operator never saw has made a
decision about an object they will go looking for.

**Not cluster-scoped.** §30 writes RoleBindings only. A ClusterRoleBinding is not
a namespace administrator's action with a namespace's blast radius, and offering
it on a namespace page is how one gets made by somebody who meant the namespace.
§4 creates them.

**Not an effective-access answer.** That is §23, and §30's own mitigations say so.

---

## 31. Why is this pod Pending

### 31.1 The question this console answered with one word

`Pending` is a phase, not an answer. The answer exists — the scheduler computed
it over its whole predicate chain and wrote it into an event — and until §31 it
was three clicks away in a list nobody filters.

Four things stand between that event and a correct reading of it, and each one
is a place a better console still gets it wrong:

**Half of Pending is not about the scheduler.** A pod with `spec.nodeName` set
has been *placed*. What is holding it is on that machine — an image that will
not pull, a volume that will not mount, an init container that has not finished
— and cluster capacity has nothing to do with it. One phase in the API, two
entirely different investigations, so `waiting_on` is the first field in the
response.

**The message is a snapshot, not a status.** It carries what the scheduler saw
at the instant of one failed attempt. A node added since, a pod deleted since, a
taint removed since — none of them rewrite it. An operator reading a
forty-minute-old `0/5 nodes are available` as the present goes looking for
capacity that arrived half an hour ago, so the age travels with it.

**Its absence is not innocence.** Events age out of etcd — one hour by default —
so a pod pending since this morning has an explanation that expired. `scheduler:
null` means *no `FailedScheduling` event is readable right now*, and never that
the scheduler is content with the pod.

**A re-derivation cannot say a node fits.** §31 checks what it can check and
rules nodes out. It never rules one in.

### 31.2 `GET /api/pods/{namespace}/{name}/scheduling`

Reads only — no gate, no preflight, no audit row. The pod is the primary read
and raises; everything else is secondary and names itself in `unavailable[]`.

```json
{ "namespace": "prod", "pod": "checkout-7d9-abc",
  "phase": "Pending", "waiting_on": "scheduler", "node": null,
  "pending_seconds": 1320,
  "scheduled_condition": {"status": "False", "reason": "Unschedulable",
                          "message": "…", "last_transition": "…"},
  "scheduler": {"reason": "FailedScheduling",
                "message": "0/3 nodes are available: 2 Insufficient cpu, …",
                "count": 14, "last_seen": "…", "first_seen": "…",
                "age_seconds": 2400},
  "requests": {"cpu_cores": 3, "memory_bytes": 2147483648},
  "claims": [ … ],
  "nodes": [ … ],
  "unavailable": [], "partial": false }
```

`waiting_on` is `scheduler`, `kubelet` or `nothing`. Only the first is a
scheduling problem; the second sends the reader to that node's events and
container statuses; the third is a settled pod, answered rather than refused
because "why was this put *there*" is asked about running pods too.

`scheduled_condition` is `null` when the API server has written no conditions
yet. That is a real state — nothing has evaluated the pod — and synthesising a
"not scheduled" from it would attribute a verdict to a scheduler that has not
looked.

`pending_seconds` is `null` on a settled pod. Reporting one would be the pod's
age dressed up as a complaint.

### 31.3 `nodes[]` — rule-outs, and the verdict that does not exist

```json
{"name": "ip-10-0-1-4", "verdict": "ruled_out", "capacity_checked": true,
 "reasons": [{"code": "node_insufficient_cpu", "detail": "Needs 3 cores; …"}]}
```

`verdict` is `ruled_out` or `no_reason_found`. **There is no `fits`, and there
must not become one.** The scheduler also weighs inter-pod affinity and
anti-affinity, topology spread constraints, volume node affinity and zone,
extended and scalar resources, host port conflicts, pod overhead against a
RuntimeClass, and every scheduling plugin the cluster runs. None of that is
evaluated here, so `no_reason_found` means exactly what it says — this console
checked what it can check and found nothing — and promoting it would send an
operator to argue with the scheduler about a node it already rejected for a
reason they cannot see.

| Code | When |
|---|---|
| `node_cordoned` | `spec.unschedulable` |
| `node_not_ready` | The Ready condition is **`False`**. An absent one is unobserved, not unhealthy, and does not rule the node out |
| `node_untolerated_taint` | A `NoSchedule` or `NoExecute` taint the pod does not tolerate. `PreferNoSchedule` is **not** here: it lowers the node's score and does not exclude it |
| `node_selector_mismatch` | A `nodeSelector` key the node does not carry. `nodeAffinity` is deliberately not evaluated — a partial implementation of its operators and weights would rule nodes out for terms it misread |
| `node_insufficient_cpu` | The request exceeds **allocatable** minus what is already requested there. Not capacity: allocatable is what the kubelet offers the scheduler, and it is the only number the scheduler compares against |
| `node_insufficient_memory` | The same, for memory |
| `node_pod_slots_full` | The node is at its pod limit, whatever the request's size |

`capacity_checked: false` marks a node that was **not fully compared** — the pod
listing failed, a neighbour's request would not parse, or the node publishes no
pod-slot count. Its `no_reason_found` is weaker still, and the response says so
rather than letting a half-examined node look like a cleared one.

`nodes: null` is an unreadable listing. Never `[]`, which would say the cluster
has no nodes on the screen where somebody is working out why nothing will take
their pod.

### 31.4 `claims[]` — the cause, or the symptom

An unbound claim usually blocks scheduling. A claim whose StorageClass uses
`WaitForFirstConsumer` is unbound **because** the pod is unscheduled: the
provisioner is waiting for the scheduler to pick a node so it can create the
volume in the right zone. Reporting that as the blocker sends an operator to fix
storage while storage waits on them to fix scheduling.

So `blocks_scheduling` is tri-state: `true` for an unbound `Immediate` claim or
one that does not exist, `false` for a bound claim or an unbound
`WaitForFirstConsumer` one, and `null` when the binding mode could not be
established — which is either the cause or the symptom, and guessing picks one
of two systems with even odds.

`claims: null` is an unreadable listing; `[]` is a pod that mounts none.

### 31.5 What §31 is not

**Not a scheduler.** It does not simulate placement and does not predict where
the pod will land. Every statement it makes about a node is a reason that node
is *out*.

**Not a fix.** It writes nothing. The actions it points at — cordoning, taints,
quota, scaling — are §5's, §24's, §29's and §6's, each through the funnel.

**Not live.** The scheduler's message is a record of one past attempt, and this
endpoint is a read at a moment. Neither is a watch.

**Not `kubectl describe pod`.** That prints the same event and every other one
beside it. §31's contribution is the separation — which of the two questions
this is, how stale the answer is, and which nodes are out for reasons an
operator can act on.

---

## 32. The certificate an exposure serves

### 32.1 The fact no Kubernetes API reports

`kubectl get ingress` prints `TLS: 1 secret`. `oc get route` prints `edge`.
Neither prints a date and neither prints a name, and those are the two facts
that decide whether the site is up tomorrow. Both are inside the certificate, in
the clear — every client that completes a handshake with that server is handed
them — and reaching them means base64-decoding a Secret and running
`openssl x509 -text`, which is why nobody does it until the site is down.

Two questions, and the second is the one that surprises people.

**When does it expire** is the famous one, and the easy one: it is a date in the
object.

**Is it even for this hostname** is the other, and a certificate can be current,
correctly issued and completely useless. An Ingress moved to a new host, a
wildcard that covers `*.example.com` and not `example.com`, a Secret two
Ingresses share where only one of them was renamed — each produces a green row
in every Kubernetes tool there is and a browser that refuses to connect.

### 32.2 `GET /api/routes/certificates`

Reads only — no gate, no preflight, no audit row. Query: `namespace` (omit for
every namespace) and `limit` (bounds each of the three *listings*, not how many
Secrets are opened).

```json
{ "items": [
    { "id": "ingress/prod/checkout#0",
      "kind": "Ingress", "group": "networking.k8s.io",
      "name": "checkout", "namespace": "prod", "slot": "0",
      "source": "secret", "termination": "edge",
      "hosts": ["shop.example.com"],
      "secret": {"namespace": "prod", "name": "shop-tls"},
      "sourceDetail": "spec.tls[0].secretName names the Secret shop-tls.",
      "certificate": {
        "subject_common_name": "shop.example.com",
        "issuer_common_name": "Example CA R3",
        "serial": "03:ab:5f",
        "not_before": "2026-07-01T00:00:00Z",
        "not_after": "2026-12-01T00:00:00Z",
        "dns_names": ["shop.example.com"], "ip_addresses": [],
        "email_addresses": [], "uris": [],
        "key": {"algorithm": "ECDSA", "size": 256, "curve": "secp256r1"},
        "signature_algorithm": "sha256",
        "self_signed": false, "chain_length": 2, "error": null },
      "state": "valid",
      "expires_in_seconds": 7344000,
      "hostsCovered": [{"host": "shop.example.com", "covered": true}],
      "findings": [] } ],
  "continue": null, "remaining": null,
  "partial": false, "unavailable": [],
  "kinds": [ {"kind": "Route", "group": "route.openshift.io",
              "state": "available", "version": "v1", "detail": "…"} ],
  "truncated": [],
  "expiringWindowSeconds": 2592000,
  "maxCertificateReads": 100 }
```

`kinds[]` carries all three regardless of what the cluster serves, with the
§13 `available` / `unsupported` / `unknown` classification — from the same
function §13 uses, so the two can never disagree. Without it a Gateway
API cluster with no Ingresses would render an empty page that reads as "no
certificates here", which is this section's own failure applied to itself.
`unsupported` is an ordinary fact and does **not** make the report partial;
`unknown` does. Its `unavailable[]` entry is built by §13's own
`unknown_entry`, which **raises** rather than inventing a reason token for an
error that is not a statement about whether we could look — because the token
anybody inventing one reaches for first is `unreachable`, and that sends an
operator to check a network that answered fine.

### 32.3 `state`, and the null that is not a zero

| `state` | Meaning |
|---|---|
| `valid` | `notAfter` is further away than `expiringWindowSeconds` |
| `expiring` | `notAfter` is inside that window |
| `expired` | `notAfter` is in the past |
| `not_yet_valid` | `notBefore` is in the future — outranks the expiry buckets |
| `unknown` | no certificate was read |

`expires_in_seconds` is **negative** for an expired certificate and `null` when
none was read. It is never clamped and never zero-for-unknown: `0` is a real
value here and it means *expires this second*, which is the one number an
operator acts on without reading the rest of the row.

`not_yet_valid` is not exotic. A freshly-issued certificate on a cluster whose
clock is behind the issuer's is exactly this, and reading it as "valid, 89 days
left" sends the operator to look at the router.

### 32.4 Where a certificate lives, and the two places it legitimately is not

`source` is the first field to read, the way §31's `waiting_on` is:

| `source` | Where the certificate is |
|---|---|
| `secret` | A Secret — an Ingress `spec.tls[]` block, a Route's `externalCertificate`, or a Gateway listener's `certificateRefs` |
| `inline` | A Route's own `spec.tls.certificate` |
| `backend` | A `passthrough` exposure: the pod holds it and the router never sees it |
| `router_default` | TLS terminates and nothing names a certificate, so the router serves its own — which lives in the router's namespace under a name this console is not told |

The last two are **not** missing certificates. Drawing either as one puts a red
row on an exposure working exactly as designed, and sends somebody to create a
Secret nothing was asking for.

**One entry per certificate, not per object.** An Ingress with two `spec.tls[]`
blocks has two certificates covering two host sets, so it produces two rows;
§13's row reports the first Secret because it is a summary of one exposure, and
using it here would mean saying `shop.example.com` is covered by a certificate
that has nothing to do with it. A Gateway listener naming three
`certificateRefs` produces three. `slot` is what makes `id` unique per
certificate.

A Gateway `certificateRefs` entry may point into another namespace. The Secret
is read where the reference actually points — looking in the Gateway's own
namespace would report a certificate that exists and works as missing. Whether
the reference is *permitted* is the Gateway controller's decision, visible in
the listener's conditions, and no ReferenceGrant is read here.

### 32.5 `hostsCovered` — tri-state, matched the way a browser matches

Coverage is computed against **subject alternative names only**. Every browser
shipping today ignores the Common Name for host verification, so a console that
matched on CN would call a certificate correct that no client will accept. A
certificate carrying no SAN at all gets `no_subject_alt_names` and authenticates
no hostname, however correct its subject looks.

Wildcards follow RFC 6125 §6.4.3: one label, leftmost only, and the whole label.
`*.a.com` covers `b.a.com`, and covers neither `a.com` nor `c.b.a.com`;
`w*.a.com` is not a wildcard. Relaxing any of those would report a host as
covered that Chrome will refuse — a green row in front of an outage.

A host that is a literal IP is matched against the certificate's IP SANs and
never its DNS SANs, because that is how a client matches it.

`covered` is `true`, `false` or **`null`**. `false` is a claim about a
certificate; with none read there is nothing to make it with, so every host on
an unread row is `null`.

A Route with `wildcardPolicy: Subdomain` serves every sibling of its host, so
`*.<parent>` is added to `hosts` and checked. A certificate naming only
`foo.apps.example.com` covers the route's own name and none of the traffic the
policy invites, and no field anywhere says so.

### 32.6 `findings[]`

| `code` | What it says |
|---|---|
| `certificate_expired` | `notAfter` has passed, with how long ago |
| `certificate_expiring` | Inside `expiringWindowSeconds`, with how long is left |
| `certificate_not_yet_valid` | `notBefore` is in the future |
| `host_not_covered` | A host this exposure serves is not among the SANs, with both lists |
| `no_subject_alt_names` | The certificate names nothing, so it authenticates nothing |
| `chain_leaf_only` | The bundle carries the leaf and no intermediate |
| `self_signed` | Issuer and subject are the same name — reported as a fact, not a fault |
| `certificate_unreadable` | The Secret, or the bytes in it, could not be read |
| `certificate_not_read` | The report's read budget was spent before this row |
| `certificate_not_named` | `source: router_default` |
| `certificate_in_backend` | `source: backend` |

`chain_leaf_only` is a statement about the **bundle**, not about any client: it
says no intermediate is present, which is structural and checkable. It is not a
chain verification, and §32 performs none.

### 32.7 The read budget

One report opens at most `maxCertificateReads` **distinct** Secrets, cached by
`(namespace, name)` for its lifetime. Forty Ingresses behind one wildcard Secret
therefore cost one read — and, more usefully, all forty rows agree about it.

A source past the budget comes back `certificate: null`, `state: unknown` and
`certificate_not_read`, which says *we stopped looking*. It never looks
examined, and the cap is never applied to rows instead of reads: that would hide
thirty-nine rows to save nothing.

### 32.8 Only `tls.crt`, and only from a `kubernetes.io/tls` Secret

The certificate is public material. The private key is not, and this section
never touches it: exactly one key of the Secret is read, by that name, and
`tls.key` is not read, decoded, counted or named. **No PEM appears in the
response at all** — the report is dates and names, and shipping certificate
bodies to a browser buys nothing.

A Secret of any other type is refused **by type** rather than searched for
something certificate-shaped. `Opaque` Secrets hold arbitrary application data,
and a console that went rummaging through one would be reading passwords on a
page about expiry dates.

This is deliberately **not** behind `SECRET_REVEAL_ENABLED`. That gate exists
for the *values* of a Secret; what §32 returns is derived, public metadata about
one key of one Secret type — the same bytes that server hands every client that
connects to it. Gating it there would make the section useless on every
deployment that leaves the gate off, which is all of them, while protecting
nothing. RBAC still applies: a caller who cannot read the Secret gets an
`unavailable[]` entry and `state: unknown`, never a row that looks examined.

### 32.9 What §32 is not

**Not a verifier.** No trust store is consulted, no chain is built, no signature
is checked and no revocation is looked up. A certificate this section calls
unexpired and name-matching can still be rejected by every browser on earth. The
UI says so above the table rather than under it, because a green column read as
"this works" is a claim the console cannot make.

**Not what the router serves.** The Secret is what the object *points at*. A
router started with `--default-ssl-certificate`, a controller-specific
annotation overriding the Secret, a cert-manager renewal that has landed in the
Secret but not in a router that has not reloaded — in each of those the bytes on
the wire differ from the bytes here. Only a handshake settles it, and this
console makes none.

**Not a monitor.** It is a read at a moment. There is no watch, no alert and no
schedule; `expiringWindowSeconds` is a rendering threshold, not a notification.

**Not a certificate manager.** It writes nothing. Renewing one is cert-manager's
job or the CA's, and replacing one is §4's YAML editor through the funnel.

---

## 33. Installing Operator Lifecycle Manager

§16's portal reads what OLM offers. §33 puts OLM there when it is not.

**This is the second bundle this console ships, and it is the last one.**
ADR-0004 drew a boundary at one — "one pinned bundle of eight objects, through
the ordinary write funnel, and nothing else, ever" — and ADR-0005 said the day
somebody argued a second bundle was really just like the portal was the day both
boundaries needed re-reading. `docs/adr-0008-shipped-olm.md` is that re-reading:
it records the decision, the argument against it, and the fact that "and nothing
else, ever" has now been wrong once. Read it before adding a third.

**It ships manifests, not a controller.** `deploy/olm/crds.yaml` and
`deploy/olm/olm.yaml` are upstream's release artifacts, vendored byte for byte,
their SHA-256 digests pinned in `app/admin/olm_bundle.py` and checked on every
load. Installing them is twenty-six ordinary writes through the funnel — twenty-six
preflights, twenty-six diffs, twenty-six audit rows. Nothing watches OLM
afterwards. `GET /api/portal/olm` is a live read.

Off by default behind `ADMIN_OLM_INSTALL_ENABLED`, on top of
`ADMIN_ALLOW_MUTATIONS`. Separate from `ADMIN_PORTAL_INSTALL_ENABLED`, because
they are different sizes of decision: that one writes one object into an API the
cluster already serves, and this one creates the API.

### 33.1 What it is, and what it is not

| It does | It does not |
|---|---|
| Install a pinned OLM onto a cluster that has none | Upgrade an OLM that is already there |
| Report which version is running and whether it matches the shipped one | Claim a newer OLM exists |
| Refuse to write over an OLM it did not install, naming every conflict | Adopt objects that already carry these names |
| Offer upstream's community `CatalogSource` as an acknowledged option | Install it by default, or ship a catalog of its own |
| Report that twenty-six objects were accepted | Claim OLM is running (§33.6) |
| Say what removing OLM actually takes | Uninstall it (§33.8) |

### 33.2 `GET /api/portal/olm`

A live read. Nothing is cached and nothing is remembered between calls.

```jsonc
{
  "enabled": false,
  "enabledDetail": "Installing Operator Lifecycle Manager is switched off …",
  "installed": false,          // tri-state — see 33.5
  "ready": false,              // tri-state, and NOT the same question
  "shippedVersion": "0.35.0",
  "upstream": "https://github.com/operator-framework/…/download/v0.35.0",
  "managedByUs": null,         // null when nothing is present to ask about
  "crds": { "expected": 8, "present": 0, "established": 0, "missing": [...], "detail": null },
  "deployments": [ { "name": "olm-operator", "present": false, … } ],
  "packageServer": { "csvPresent": false, "phase": null, "apiAvailable": false, … },
  "namespaces": ["olm", "operators"],
  "notes": [ … ],
  "partial": false,
  "unavailable": []
}
```

### 33.3 `POST /api/portal/olm/plan`

Ungated, like §14's and §16's plans. Reads no cluster, writes nothing, records no
audit row. Returns every object's rendered YAML, the two phases, the digests, and
the consequences.

It is ungated **because** of what it contains. Deciding whether to set
`ADMIN_OLM_INSTALL_ENABLED` means deciding whether this console may create a
ClusterRole granting every verb on every resource, and that decision has to be
made with the object on screen. A gate that hid the plan would withhold exactly
the thing the gate exists to make someone think about.

Body: `{ "communityCatalog": false }` — the only choice §33 offers. Everything
else in upstream's manifests is not configurable, and that is upstream's decision
rather than this console's: OLM's Deployments, its ClusterRoleBinding subject and
the `packageserver` CSV all name the `olm` namespace literally.

### 33.4 `POST /api/portal/olm`

The write. Body is the plan's plus `dryRun` (default **true**) and
`acknowledgeConsequences`.

Twenty-six writes in two ordered phases:

1. **`crds`** — eight CustomResourceDefinitions.
2. *(the wait)* — each must report `Established` before phase two, bounded by
   `ADMIN_OLM_ESTABLISH_TIMEOUT_SECONDS`. On success the console's discovery
   cache is invalidated, because phase two addresses five `operators.coreos.com`
   kinds that were not in it when the request started.
3. **`core`** — the eighteen objects that are OLM itself, in upstream's order.

The order inside each phase is upstream's, preserved: `olm-operators` precedes
the `packageserver` CSV because OLM refuses to install a CSV into a namespace
with no OperatorGroup, and the ClusterRole precedes the binding that references
it.

**Phase two never runs into APIs that do not exist.** If a CRD write fails, or
the wait expires, phase two's objects are reported with a `skipped` sentence and
no `error` — because they have no error of their own, and eighteen 404s that all
describe the first problem is not eighteen answers.

### 33.5 The two tri-states, and why a UI must not collapse them

`installed` says OLM's objects are on the cluster. `ready` says the package
server is answering, which is what makes §16 show anything. `null` in either
means a read failed and the question is open — never `false`, which during an API
outage would invite an operator to install over an OLM that is already running.

`installed: true, ready: false` is the ordinary state for a minute or two after
an install, and the permanent state on a cluster where OLM cannot schedule.

### 33.6 `installed: true` does not mean OLM works

The strongest thing the install response can honestly say is that twenty-six
objects were accepted by the API server. Two things happen afterwards that belong
to OLM:

* the two Deployments have to schedule and become Ready, and
* the `packageserver` ClusterServiceVersion has to be reconciled by OLM, which
  registers `v1.packages.operators.coreos.com` as an aggregated APIService.

Until that second one succeeds, §16 reads exactly the empty state it read before
the install — *and it is right to*. So the install response carries `ready: null`
and a `readyDetail` naming `GET /api/portal/olm` as the endpoint that answers it.
Nothing in the install response may be rendered as "Operator Lifecycle Manager is
installed and working".

### 33.7 Consequences

Acknowledged by code before a real write, like §16's and §13's. Two are
unconditional, which §16's are not: they are what installing OLM *is*, rather
than findings about a particular cluster.

| Code | When |
|---|---|
| `cluster_admin_grant` | Always. `system:controller:operator-lifecycle-manager` grants `apiGroups: ['*']`, `resources: ['*']` with every verb including `escalate` and `bind`. Quoted rather than paraphrased |
| `crd_ownership` | Always. Eight CRDs join the cluster's API, and deleting one later deletes every custom resource made from it |
| `community_catalog` | `communityCatalog: true`. The cluster pulls `quay.io/operatorhubio/catalog:latest`, hourly |

`notes[]` is separate and **not** acknowledgeable: `installed_is_not_running`,
`preflight_cannot_see_escalation`, `no_uninstall_and_no_upgrade`,
`network_policies`. None is an outcome anyone can accept or decline, and mixing
them in would train people to tick four boxes to get past the two that mattered.

### 33.8 What §33 is not

**Not an uninstall.** Removing OLM means deleting its CRDs, and deleting a CRD
deletes every custom resource made from it across every namespace, with no second
confirmation — every operator's Subscription and ClusterServiceVersion on the
cluster. There is no endpoint and no delete verb granted in `deploy/rbac.yaml`.
This is §16's asymmetry one layer down and larger, and it is not closed.

**Not an upgrade.** A later OLM is installed with `kubectl` from upstream's
release. This console reports which version is running and whether it matches the
one it ships, and stops. `versionMatches` is that report; it is never a claim
that a newer OLM exists, which would be a statement about a third party's release
history made from a string baked into this repository.

**Not a catalog.** §33 installs the software that reads catalogs. Which catalogs
a cluster trusts stays the operator's decision — ADR-0005's largest rejection,
preserved.

**Not OLM v1.** This installs `operators.coreos.com` v0, which is what §16 reads.
`olm.operatorframework.io` `ClusterExtension` is neither read nor installed.

**Not a reconcile loop.** The one wait between the phases is bounded, lives
inside a single request, holds no state and corrects no drift — what `kubectl
wait` does. There is no watch and no drift correction anywhere in this section.

---

## 34. Onboarding — the cluster that is already on this machine

§3 registers a cluster from an API server address and a bearer token. That is
the right shape for a remote cluster and the wrong amount of work for a local
one: somebody with `kind` running on the same laptop has to create a
ServiceAccount, bind it, mint a token, read the API address out of their
kubeconfig and paste both into a form — for a cluster whose credentials are
already on disk, three lines from where the console is running.

§34 reads that file. **It does not change where a client comes from.** Discovery
lists what is there, an import *copies* the credential into §3's registry, and
every transport this console builds still comes from a `Cluster` row and from
nothing else. A kubeconfig edited, rotated or deleted after an import changes
nothing about the cluster registered from it — re-importing is how you pick up a
new credential. `docs/adr-0012-kubeconfig-onboarding.md` argues that boundary and
what it costs.

### 34.1 What it does, and what it does not

| It does | It does not |
|---|---|
| List every context in the kubeconfig, with what each one holds | Hide the ones it cannot import |
| Copy a token, or an X.509 pair, into the encrypted registry | Read the kubeconfig again afterwards, ever |
| Register the one local cluster at startup when nothing is registered | Add to a registry that is not empty, or adopt anything remote |
| Say a loopback address is unreachable from inside a container | Rewrite it to something it guesses would work |
| Report a kubeconfig it may not read, naming the uid | Report that as a Kubernetes permission |
| Say a candidate *could* be registered | Say it answers — §3's `/test` is still what establishes that |
| Run an `exec` credential plugin | — it refuses, and says why |

### 34.2 `GET /api/clusters/discovery`

Administrator-only (§12.6), like §3's three writes: it reports on a file on the
console's own filesystem — its path, the contexts in it, the addresses they point
at. Reads no cluster and opens no connection.

```jsonc
{
  "items": [
    {
      "context": "kind-dev",
      "cluster": "kind-dev",
      "api_server": "https://127.0.0.1:6443",
      "namespace": null,
      "distribution": "kind",        // null when no local tool wrote it; see 34.9
      "is_local": true,              // distribution AND a loopback/private address
      "is_current": true,            // the file's own current-context
      "credential": "client_certificate",
      "authentication_type": "client_certificate",  // null when not importable
      "importable": true,
      "reason": null,                // the sentence, when importable is false
      "concern": null,               // see 34.4
      "has_ca_certificate": true,
      "skip_tls_verify": false
    }
  ],
  "continue": null, "remaining": null, "partial": false, "unavailable": [],
  "source": {
    "path": "/home/operator/.kube/config",
    "current_context": "kind-dev",
    "in_container": false
  },
  "auto_discovery": { "enabled": true, "candidates": ["kind-dev"] }
}
```

**No field of this object ever contains credential material** — not the token,
not the client key, not the CA. Only *what kind* of credential the context holds.
The bytes are read again, from the file, by the import that is being confirmed.
Enforced by a test that scans the whole body.

`credential` is one of `client_certificate`, `token`, `exec`, `auth_provider`,
`basic`, `none`. The last four are never importable, and each carries a `reason`
saying what to do instead:

* **`exec`** — the context runs a credential plugin (`aws`, `gke-gcloud-auth-plugin`).
  This console will not execute a binary named by a file on its disk, and a
  plugin's output expires, so there is nothing to store. The kubeconfig is parsed,
  never loaded by `kubernetes.config`, precisely so that *listing* what is
  available cannot run anything.
* **`auth_provider`** — a legacy refreshing plugin. Same absence of a copyable credential.
* **`basic`** — a username and password. This console presents a bearer token or
  a client certificate; current API servers serve neither basic auth.
* **`none`** — no credential at all. Importing it would register an anonymous
  connection, which half-works against a permissive cluster and becomes an
  intermittent permissions mystery weeks later.

**A refused context is listed, not dropped.** §0.1 applied to a file: an operator
whose only context is an EKS one must be able to tell "this console will not run
your credential plugin" from "you have no kubeconfig", and an omission says
neither.

`unavailable[]` carries `resource: "kubeconfig"` with `reason` of `not_found`
(no such file — the ordinary state of a console whose clusters are registered by
hand), `forbidden` (**a file permission on this machine, not a Kubernetes one** —
a mode-600 kubeconfig against a container running as another uid, which is the
most common way this feature silently does nothing) or `unsupported` (not a
kubeconfig). Never a 500, and never an empty list standing in for any of them.

### 34.3 `POST /api/clusters/import`

Administrator-only. Body: `{ "context": "kind-dev", "name": null, "app_domain": null }`
→ `201` `ClusterPublic`, with `origin: "kubeconfig"` and `status: "unknown"`.

`name` defaults to the context name, except where that name identifies
nothing: a context called `default` on a recognised distribution is named after
the distribution instead (`k3s`). An explicit `name` always wins, and a context
this console could not classify keeps whatever the kubeconfig called it —
renaming somebody's context on a guess is worse than a dull label.

The body names a *context*, never a credential — a body carrying the key would
mean discovery had to return one.

The credential is re-read and re-checked here rather than trusted from the
listing: they are separate requests, and a kubeconfig edited between the two
would otherwise be imported on the strength of what it used to say.

Errors, and why each is the code it is:

| Case | Code | Why not the other one |
|---|---|---|
| No such context, or no such file | `404 not_found` | — |
| An `exec`/`auth-provider`/basic/anonymous context | `422 invalid` | **Not `unsupported`.** That means the *cluster* does not serve something and renders grey as an ordinary fact. This is this console refusing to copy a credential, and it has a sentence to read |
| The file is unreadable by this process | `422 invalid` | **Not `rbac_denied`.** No cluster is involved; sending someone to edit a ClusterRole over a `chmod` is a confidently wrong answer pointed at the wrong system |
| A cluster of that name exists | `409 conflict` | Importing never overwrites — the stored credential may be the one somebody is relying on right now |

`status` is `unknown`, which is not `disconnected`: nothing has been connected
to. Running §3's `/test` is the next step and the UI says so.

### 34.4 The one reachability problem visible without connecting

`kind`, `k3d` and k3s write `https://127.0.0.1:<port>` into the kubeconfig. Read
from inside the backend container, that address is *the container*, so a
registration built from it is created, looks correct and reaches nothing — §14's
failure with a URL instead of a controller.

So a loopback address discovered from inside a container carries a `concern`.
It is:

* **not a `reason`** — the two are separate fields because "this cannot be
  imported" and "this can be imported and probably will not connect from here"
  send an operator to two different places;
* **not a refusal** — a container sharing the host's network namespace reaches it
  fine, and this console cannot tell which it is in;
* **not rewritten** — substituting `host.docker.internal` produces a URL whose
  hostname the cluster's certificate does not cover, so it fails verification
  instead of connecting, and the only way to make it work is to turn verification
  off on the operator's behalf.

It is what startup adoption declines on, and `/test` is what settles it.

### 34.5 Startup adoption

With nothing registered, the console registers the one local cluster in the
kubeconfig. `ADMIN_AUTO_DISCOVER_LOCAL` (default **true**) switches it off.

Every condition is a refusal as much as a condition, and all five must hold:

1. the switch is on;
2. **the registry is empty** — a curated fleet is never added to;
3. the console recognises the local cluster tool that wrote the context **and**
   its API server is a loopback or private address (both, never either: a
   context called `kind-prod` pointing at a public endpoint is remote). See
   §34.9 for what "recognises" reads;
4. it carries no §34.4 concern;
5. **exactly one** context qualifies — two is a choice, and making it at boot
   makes it where nobody can see it happen.

Anything else logs the reason and registers nothing. It never raises: a
convenience that could fail a boot is not one.

The row is written with `origin: "autodiscovered"` and `status: "unknown"`, and
the startup log says what was adopted and how to turn it off. `origin` exists
because this is the only row in the `clusters` table nobody asked for, and the
first question about a cluster somebody does not remember registering is whether
they registered it.

### 34.6 `ClusterPublic` gains two fields

`has_client_certificate` (boolean, like `has_ca_certificate`) and `origin`
(`manual` | `kubeconfig` | `autodiscovered`). `manual` covers a POSTed
registration and a row that predates the column — every one of those was typed,
so there is no third state.

### 34.7 Client-certificate registration

`authentication_type: "client_certificate"` is what §34 needed and §3 now
accepts on its own: `kind`, `k3d`, minikube and Docker Desktop mint no token at
all, so the clusters this console most wants to adopt with no setup were the
ones it could not represent.

`POST`/`PUT` take `client_certificate` and `client_key` as PEM, write-only in
exactly the way `token` is. The certificate is stored in the clear — it is
presented on every handshake and is not the secret half — and the key is
encrypted, listed in `_CLUSTER_SECRET_COLUMNS`, and refused by
`to_public_dict`. Half a pair is `422`: it stores, it lists, and it dies in the
TLS handshake with an error naming neither field.

`token` is therefore optional on `POST`. It did not become optional in the sense
of anonymous: a body carrying neither credential is `422 invalid` naming the
field it lacks.

### 34.8 What §34 is not

**Not a live credential source.** The kubeconfig is read at import and never
again. There is no watch, no refresh and no fallback to it at request time —
`app/k8s/client.py`'s development fallback is a separate, older thing that only
runs when *nothing* is registered.

**Not a cloud onboarding.** An `exec` context is refused, not worked around.
Making EKS, GKE or AKS one-click means executing a credential plugin or embedding
three cloud SDKs, and both are larger decisions than this section.

**Not a way to copy a kubeconfig into the console.** One context at a time, by
name, as an administrator act. There is no bulk import and no "adopt everything".

**Not a connection.** Discovery opens no socket and an import opens no socket.
Everything either of them says is a fact about a file.

### 34.9 What "a local cluster tool wrote this" is read from

Two signals, and the difference between them is the whole of this section.

**A name in the file.** `kind create cluster --name dev` writes the context
`kind-dev`; `k3d cluster create dev` writes `k3d-dev`; minikube, Docker Desktop,
Rancher Desktop, Colima, OrbStack and MicroK8s each name themselves exactly.
Both the context name and the cluster name are checked, because
`kubectl config rename-context` leaves only one of the two intact.

**The path the kubeconfig was read from.** k3s names *every* entry in
`/etc/rancher/k3s/k3s.yaml` — context, cluster and user — `default`, and RKE2 does
the same in `/etc/rancher/rke2/rke2.yaml`. Nothing inside either file identifies
the tool, so the path is what identifies it: a file read from there was written
by k3s, by construction. Symlinks are resolved; the path is matched as a file,
not as a string.

**`default` is not, and must never become, a recognised name.** A remote
cluster's context is routinely called `default`, and adopting one on the strength
of that name is exactly the failure the two-part `is_local` test exists to
prevent — it would be a regression in safety wearing a feature's clothes. The
path is a different kind of evidence: no cluster elsewhere can arrange to be read
from `/etc/rancher/k3s/k3s.yaml`.

**The limit, which is the honest part.** The path evidence does not survive the
file being moved. A k3s kubeconfig copied to `~/.kube/config`, or merged into one
with `KUBECONFIG=~/.kube/config:/etc/rancher/k3s/k3s.yaml kubectl config view
--flatten`, has no path left to read and a context called `default`. It is
classified remote, listed as remote, and imported by hand — there is nothing safe
left to go on, and a weaker heuristic on the name is the thing the paragraph
above refuses. The path is read the other way round, though: it identifies the
*file*, so a k3s context somebody renamed is still recognised.

Being wrong either way costs an adoption that does not happen and a row that says
`remote`. It never produces a registration that lies, which is the bar.
