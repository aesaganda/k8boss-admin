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
  `not_registered`, `unsupported`. Callers branch on this; `detail` is for
  humans only and must never be parsed.

`unsupported` is the "this cluster does not have that API" case (no Ingress
controller CRDs, no `metrics.k8s.io`). It is not an error and does not colour a
row red — the UI renders it as "not present on this cluster".

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
| `identity_provider_unavailable` | 502 | LDAP could not answer or its secure transport is misconfigured |
| `mutations_disabled` | 403 | the deployment is running read-only |
| `unsupported` | 501 | the API resource is not served by this cluster |
| `upstream_error` | 502 | any other API server failure |

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

### 1.6 Read-only mode

`ADMIN_ALLOW_MUTATIONS` (env, default **false**) gates every write. When false,
writes return `403 mutations_disabled` **before** touching the cluster, and
`GET /api/health` reports `"mutations": "disabled"` so the UI hides the buttons
instead of offering them and failing.

Dry-run is still permitted in read-only mode — inspecting what *would* change is
a read. This is deliberate: it makes the console useful in an audit posture.

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
  "status": "connected",
  "server_version": "v1.31.4",
  "last_connected": "2026-08-18T09:03:11Z",
  "created_at": "2026-06-02T10:00:00Z",
  "updated_at": "2026-08-01T12:41:09Z"
}
```

No field of this object ever contains credential material. Enforced by a test.

### `POST /api/clusters`

```json
{ "name": "prod-eu", "platform": "kubernetes", "api_server": "https://...",
  "authentication_type": "service_account_token", "token": "eyJ...",
  "ca_certificate": "-----BEGIN CERTIFICATE-----\n...", "skip_tls_verify": false }
```
→ `201` `ClusterPublic`.

### `PUT /api/clusters/{id}` — partial; omitted `token` keeps the stored one.
### `DELETE /api/clusters/{id}` → `204`.
### `POST /api/clusters/{id}/test`

Performs a live connection check. Returns
`{"reachable": true, "server_version": "v1.31.4", "latency_ms": 42, "permissions": [PreflightResult]}`
where `permissions` preflights the console's baseline verb set (see §9) so
registration surfaces a half-permissioned ServiceAccount immediately rather than
at first use.

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
with `changed: true`, so the confirm dialog can show exactly what disappears.

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
  "age_seconds", "ip", "qos_class", "containers": [{"name","image","ready","restarts","state","reason"}],
  "owner": {"kind":"ReplicaSet","name":"checkout-7d9"} }
```

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

## 7. Pod logs and exec

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

---

## 8. Config, storage, access — typed rows

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

### `GET /api/audit`
Query `limit` (default 100, max 1000), `cursor`, `cluster_id`, `actor`,
`outcome`, `since`.
Row:
```json
{ "id": 4471, "ts": "2026-08-18T09:14:03Z", "actor": "erens", "cluster_id": 1, "cluster_name": "prod-eu",
  "verb": "patch", "target": {"group":"apps","version":"v1","resource":"deployments","namespace":"prod","name":"checkout"},
  "dry_run": false, "outcome": "applied",
  "detail": "replicas 3 -> 5", "diff_digest": "sha256:9f2c...", "error": null, "source_ip": "10.4.2.9" }
```
`outcome` ∈ `applied` | `dry_run` | `denied` | `failed` | `conflict`.
Records are append-only; there is no delete endpoint.

With `AUTH_ENABLED=true`, actor is the verified session username and an inbound
`X-K8Boss-User` cannot override it. With auth disabled, actor comes from the
legacy advisory `X-K8Boss-User` header and defaults to `anonymous`.

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

---

## 12. Console authentication and users

Authentication is disabled by default for compatibility with deployments that
already put an authenticating proxy in front. When `AUTH_ENABLED=true`, every
HTTP and WebSocket API except `GET /api/health`, `GET /api/auth/config`, and
`POST /api/auth/login` requires a valid opaque session cookie. Unsafe HTTP
methods also require the session's `X-CSRF-Token`. Session bearer tokens are
HttpOnly cookies and only their SHA-256 digests are stored.

### `GET /api/auth/config`

Public discovery: `{ "enabled":true, "localEnabled":true,
"ldapEnabled":true, "methods":["local","ldap"] }`.

### `POST /api/auth/login`

Body `{ "username":"erens", "password":"...", "source":"auto|local|ldap" }`.
Success sets the session cookie and returns `{enabled, authenticated, user,
csrfToken, expiresAt}`. Every credential rejection is `401 invalid_credentials`;
the response never reveals whether the username exists. An unavailable directory
is `502 identity_provider_unavailable`, not invalid credentials.

### `GET /api/auth/me` / `POST /api/auth/logout`

`me` returns the current session body and CSRF token. `logout` revokes the
server-side session and clears the cookie.

### `/api/auth/users`

Administrator-only. `GET` returns the standard list envelope; `POST` creates a
local user; `PUT /{id}` updates profile, role, state, or a local password;
`DELETE /{id}` deactivates the account and revokes all sessions. Password hashes
never appear in responses. The current user and final active administrator
cannot be deactivated. LDAP passwords and roles are directory-managed and are
refreshed after a successful search-and-bind login. The `admin` role gates this
user-administration surface; both `admin` and `user` identities retain the
console's normal cluster capabilities, still constrained by preflight and the
deployment-wide mutation gate.
