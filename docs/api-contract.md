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

### `POST /api/clusters`

```json
{ "name": "prod-eu", "platform": "kubernetes", "api_server": "https://...",
  "authentication_type": "service_account_token", "token": "eyJ...",
  "ca_certificate": "-----BEGIN CERTIFICATE-----\n...", "skip_tls_verify": false,
  "app_domain": "apps.prod-eu.example.com" }
```
→ `201` `ClusterPublic`.

### `PUT /api/clusters/{id}` — partial; omitted `token` keeps the stored one.
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
Body `{"yaml": "...", "namespace": "prod", "dryRun": true}` → mutation response (§1.5).

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
panel, nothing shape-specific. This is a UI/nav decision, not a new contract —
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

Query: `limit` (optional). Response:

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

---

## 12. Console authentication and users

Authentication is disabled by default for compatibility with deployments that
already put an authenticating proxy in front. When `AUTH_ENABLED=true`, every
HTTP and WebSocket API except `GET /api/health`, `GET /api/auth/config`,
`POST /api/auth/login`, `GET /api/auth/oidc/start` and
`GET /api/auth/oidc/callback` requires a valid opaque session cookie. Unsafe HTTP
methods also require the session's `X-CSRF-Token`. Session bearer tokens are
HttpOnly cookies and only their SHA-256 digests are stored.

The two OIDC routes are public because single sign-on **is how a session is
obtained** — challenging them for one is a deadlock whose symptom is a sign-in
button that answers 401. Public is not unprotected: `/start` mints a sealed
handshake and redirects, and `/callback` refuses anything that does not match a
handshake this console started, issuing a session only after a
signature-verified assertion.

### 12.1 `GET /api/auth/config`

Public discovery:

```json
{ "enabled": true, "localEnabled": true, "ldapEnabled": true, "oidcEnabled": true,
  "methods": ["local", "ldap", "oidc"],
  "oidc": { "label": "Single sign-on", "startPath": "/api/auth/oidc/start" } }
```

It names *which* methods exist and never how they are wired. This is the one
unauthenticated endpoint in the API, and the login page only needs to know which
buttons to draw; returning the issuer, the client id or the configured groups
would let anyone who can reach the console enumerate its identity provider.

`oidcEnabled` is true only when `AUTH_ENABLED`, `OIDC_ENABLED`, `OIDC_ISSUER` and
`OIDC_CLIENT_ID` are all set. A half-configured deployment shows no SSO button,
because a button that leads to an error reads as a broken console rather than an
unconfigured one.

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

### 12.4 Single sign-on (OpenID Connect)

One issuer per deployment, configured from the environment exactly as LDAP is
(`OIDC_*`, see the README). Multiple concurrent issuers is a real design change —
a table, a CRUD surface, per-row encrypted secrets and a subject-collision story
across issuers — not a config key, and the console says it does not do that
rather than half-doing it.

#### `GET /api/auth/oidc/start?next=<path>`

302 to the issuer's authorization endpoint: Authorization Code flow with PKCE
S256, carrying `state` and `nonce`. Those four values plus the exact
`redirect_uri` are sealed into a short-lived cookie.

`next` must be a same-origin path. An absolute URL, a scheme-relative
`//host`, or anything containing a backslash becomes `/`. A login link carrying
`?next=https://evil.example` would otherwise produce a page on this console's
domain that authenticates the operator and hands them to somebody else's site.

The handshake cookie is `SameSite=Lax`, **not** `Strict` like the session cookie.
The callback is a top-level navigation from the issuer's origin, and browsers do
not send a `Strict` cookie on a cross-site navigation — a `Strict` handshake
cookie is simply absent when the callback runs, and every sign-in fails.

#### `GET /api/auth/oidc/callback`

Verifies the ID token completely before reading a single claim from it:
signature against the issuer's JWKS, an **algorithm allowlist** (asymmetric only
— never the token's own `alg`), `iss`, `aud`, `exp`/`iat` as required claims, and
the `nonce` against this browser's handshake. Then the PKCE verifier is presented
on the code exchange.

Each of those has a specific attack behind it: a token minted by the same issuer
for a different client is valid and correctly signed, so without an audience check
anyone holding one can sign in here; `alg: none` and HS256-with-the-public-key
both work against a verifier that trusts the token's header; and without a nonce
an assertion captured from any other sign-in can be replayed.

Success sets the session cookie and 302s to `next`. Failure 302s to `next`
carrying `?auth_error=<§1.3 code>&auth_reason=<slug>`. It cannot answer with a
§1.3 envelope — the caller is a browser navigation, not `fetch()` — so the code
is drawn from the same §1.3 vocabulary rather than inventing a second one. The
login page words the slug; an unrecognised slug is rendered verbatim rather than
replaced with something generic.

**Nothing is written to the audit trail before the handshake is verified**, and
this route is public, so that rule is load-bearing rather than tidy. Every path
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

A federated identity is bound to the provider's `sub` claim, stored as
`users.external_id`. Two refusals, both `permission_denied`, both refusals rather
than merges:

* **A username already owned by another auth source.** If `alice` is a local
  account with a password, an SSO assertion naming `alice` must not adopt it —
  otherwise anyone who can make an issuer assert a username inherits whatever
  that username already had.
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

`OIDC_ADMIN_GROUP` maps its members to `admin`; every other resolved membership
maps to `user`. `OIDC_ALLOWED_GROUPS`, when set, restricts who may sign in at all.

The groups claim can be **absent** rather than empty, and the two are not the
same fact. Most issuers omit it entirely unless the scope was requested *and* the
client is configured to emit it, so absent is the common state during setup.

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
cannot be deactivated. LDAP and OIDC passwords and roles are provider-managed and
are refreshed at login **when the provider reports group membership** — when it
does not, the stored role is left alone rather than reset (§12.4). The `admin` role gates this
user-administration surface; both `admin` and `user` identities retain the
console's normal cluster capabilities, still constrained by preflight and the
deployment-wide mutation gate.

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
  "targets": [{"service": "shop", "port": 80, "weight": null}],
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
of the same name without it refuses with `409 conflict` naming the object —
before writing anything, on a dry run as much as on a real one, and the refusal
is audited because it happens before the funnel is reached.

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
