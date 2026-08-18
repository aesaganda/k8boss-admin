# k8boss-admin

A Kubernetes administration console. It browses every resource a cluster serves,
and it changes them through a path that checks the permission first, dry-runs the
write, shows you the diff, waits for you to confirm, and records what happened.

It is a web UI for the work you would otherwise do with `kubectl` across several
clusters, built on the assumption that the dangerous part is not reading.

## What it is not

* **Not a monitoring or observability tool.** No metrics storage, no dashboards
  over time, no alerting. It shows you the cluster's current state, live.
* **Not a deployment or CI system.** It does not build, does not template, does
  not reconcile from Git. If you have GitOps, this console is the thing you use
  when you are deliberately going around it — which is why every write is
  dry-run-first and audited.
* **Not a security posture product.** No network policy analysis, no service mesh
  awareness, no runtime process intelligence, no attack paths. That is
  [K8Boss](https://github.com/aesaganda/k8boss), which this console was split out
  of; see [`docs/adr-0002-lineage.md`](docs/adr-0002-lineage.md) for what was
  carried over and what deliberately was not.
* **Not an identity authority.** Built-in authentication is optional and supports
  local accounts plus LDAP. When it is disabled, the legacy authenticating-proxy
  mode remains available and `X-K8Boss-User` is advisory.
* **Not a cache.** No cluster contents are stored. Every page is a live read, so
  it cannot show you a stale answer with a confident face — and it cannot show
  you anything when the API server is unreachable, which it will say plainly.

---

## Sixty-second quickstart

```bash
git clone https://github.com/aesaganda/k8boss-admin
cd k8boss-admin
docker compose up --build
```

Open **http://localhost:8021**. The console starts **read-only**: it can read
every cluster you register and write to none of them. That is deliberate — see
[Turning on writes](#turning-on-writes).

If you have a kubeconfig at `~/.kube/config`, compose mounts it read-only and the
console falls back to your current context. That fallback is for development
only, and is used **only while no cluster is registered** — register a cluster
and it is ignored entirely.

> The backend runs as uid 10001, and a kubeconfig is usually mode `600` owned by
> you, so the container often cannot read it. `chmod 644 ~/.kube/config` fixes it
> on a development machine and is a bad idea anywhere else; registering a cluster
> properly is the better answer. The symptom is the console starting fine and
> reporting no clusters — which is accurate, not a failure to look.

### Enable sign-in and user administration

Authentication is opt-in so existing proxy deployments keep working. For a
first local administrator, place these values in a mode-`600` Compose env file
(do not put the password in shell history):

```dotenv
AUTH_ENABLED=true
AUTH_BOOTSTRAP_USERNAME=admin
AUTH_BOOTSTRAP_PASSWORD=replace-with-a-long-random-password
AUTH_COOKIE_SECURE=false
```

Start with `docker compose --env-file .env.auth up --build`, sign in, then open
**Administration -> Users**. The bootstrap credentials are used only while the
users table is empty and never overwrite an existing account. Set
`AUTH_COOKIE_SECURE=true` behind HTTPS.

LDAP is search-and-bind: the service account finds one user DN, then the console
binds as that DN with the submitted password. Plain-text LDAP is refused; use
`ldaps://` or StartTLS.

```dotenv
AUTH_ENABLED=true
LDAP_ENABLED=true
LDAP_URL=ldaps://ldap.example.com:636
LDAP_BIND_DN=cn=k8boss-admin,ou=services,dc=example,dc=com
LDAP_BIND_PASSWORD=replace-with-the-directory-service-password
LDAP_USER_SEARCH_BASE=ou=people,dc=example,dc=com
LDAP_USER_SEARCH_FILTER=(uid={username})
LDAP_USERNAME_ATTRIBUTE=uid
LDAP_DISPLAY_NAME_ATTRIBUTE=cn
LDAP_EMAIL_ATTRIBUTE=mail
LDAP_ADMIN_GROUP_DN=cn=k8boss-admins,ou=groups,dc=example,dc=com
LDAP_TLS_VALIDATE=true
```

Directory users are synchronized after their first successful login. Membership
in `LDAP_ADMIN_GROUP_DN` maps to the console `admin` role; all other directory
users map to `user`. LDAP passwords are never stored. Local accounts with the
same normalized username always take precedence and cannot be taken over by LDAP.

### Register a cluster

Registration is an API server endpoint plus a bearer token. Create a
ServiceAccount in the target cluster and bind it to the console's read-only
ClusterRole:

```bash
kubectl apply -f deploy/namespace.yaml
kubectl apply -f deploy/rbac.yaml          # reader role + binding; writer role
                                           # is defined but NOT bound
TOKEN=$(kubectl -n k8boss-admin create token k8boss-admin --duration=8760h)
APISERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify --raw \
      -o jsonpath='{.clusters[0].cluster.certificate-authority-data}' | base64 -d)
```

Then either paste those into **Clusters → Register** in the UI, or:

```bash
curl -X POST localhost:8020/api/clusters \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg s "$APISERVER" --arg t "$TOKEN" --arg ca "$CA" \
        '{name:"prod-eu", platform:"kubernetes", api_server:$s,
          authentication_type:"service_account_token", token:$t,
          ca_certificate:$ca, skip_tls_verify:false}')"
```

The token is encrypted at rest and is never returned by any endpoint.

### Check what it can actually do there

```bash
curl -X POST localhost:8020/api/clusters/1/test
```

This connects **and** preflights seventeen baseline permissions, so a
half-permissioned ServiceAccount shows up now rather than at 03:00 on the one
action you needed. A cluster that can list everything and cannot patch a
Deployment is a valid read-only registration; the point is that it says so.

### Turning on writes

Two independent gates, and both must be opened by someone who read this:

```bash
# 1. Let the API server allow it: uncomment the writer ClusterRoleBinding
#    at the bottom of deploy/rbac.yaml, then re-apply.
kubectl apply -f deploy/rbac.yaml

# 2. Let the console try: compose
ADMIN_ALLOW_MUTATIONS=true docker compose up
```

With only the first, every confirm returns a `403` naming the missing grant. With
only the second, the console preflights and dry-runs and refuses to confirm. Both
are diagnosable states. What is not diagnosable is a console that writes because
a default did it quietly.

---

## The safety model, in brief

Every write in this console — scale, restart, suspend, rollback, cordon, drain,
create, replace, delete — goes through one function, in this order:

```
   ADMIN_ALLOW_MUTATIONS ──▶ preflight ──▶ dry-run ──▶ diff ──▶ confirm ──▶ audit
     read-only refuses        may I?       what would    is that      write     who did
     before the cluster       names the    the API       really       actually  what, when,
     is touched               grant        server do?    my change?   happens   including
                                                                                the failures
```

* **Preflight.** A `SelfSubjectAccessReview` for the exact
  verb/group/resource/namespace/name — so a refusal names the missing permission
  instead of relaying a bare "forbidden". A review that *fails* is kept distinct
  from a clean denial: telling you that you lack a permission you actually hold
  sends you to fix the wrong system.
* **Dry-run.** `dryRun=All` on the same URL with the same body, so the projection
  comes from the API server's own admission chain — webhooks, defaulting, quota —
  not from a simulation we wrote.
* **Diff.** A unified diff with server bookkeeping normalised out of both sides,
  so a replica change is two lines rather than two lines inside two hundred. If
  nothing would change, the UI says so instead of offering a confirm button.
* **Confirm.** `applied: true` appears only when the write actually reached the
  cluster. A successful dry run returns a full projected object and
  `applied: false`.
* **Audit.** Append-only, enforced in code. Every terminal state, including the
  denials, the conflicts and the failures.

Two more, because they are the ones that matter under pressure:

* **`PUT` carries the `resourceVersion` you were looking at.** A mismatch is a
  `409` with a *fresh* diff against live, never a blind overwrite.
* **Drain plans before it acts.** Every pod is classified `evict` / `skip` /
  `blocked` and the plan is returned whether or not anything runs. Blocked pods
  stop the drain before the node is cordoned. Evictions are reported per pod —
  "drained" over three pods that did not evict is the sentence that gets a
  machine terminated with a database on it.

The full treatment, with the concrete failure each control prevents, is
[`docs/safety-model.md`](docs/safety-model.md). The reasoning behind
dry-run-first rather than optimistic-with-undo is
[`docs/adr-0001-dry-run-first.md`](docs/adr-0001-dry-run-first.md) — the short
version being that there is no undo for a deleted StatefulSet.

---

## Features

**Read**

* **Overview** — server version, node readiness, namespace and workload counts,
  pod phases, aggregate capacity versus requests. Six independent collectors: one
  failing sets its own panel to `null` and names itself, and the page still
  renders.
* **Nodes** — capacity, allocatable, actually-requested, pod count, conditions,
  taints, roles. Per-node totals are `null` (not `0`) when the pod listing fails,
  because an idle-looking node is the one someone drains.
* **Workloads** — Deployments, StatefulSets, DaemonSets, Jobs, CronJobs and
  ReplicaSets in one table, with a real `Unknown` status for controllers that
  have not reported on the current generation.
* **Pods** — with `phase_detail` for the cases where the phase lies: a `Running`
  pod whose container is in `CrashLoopBackOff` is not reported as Running.
* **Namespaces, Events, Network, Config, Storage, Access** — Services, Ingresses,
  ConfigMaps, Secrets (key names and byte lengths only), PVCs, PVs,
  StorageClasses, ServiceAccounts, Roles and Bindings.
* **Resource explorer** — anything the cluster serves, including CRDs, through
  the dynamic client, with the raw object and a YAML view.
* **Logs and exec** — pod logs over HTTP or a WebSocket that always terminates
  with exactly one `end` or `error` frame, so you can tell "the pod stopped
  logging" from "we lost the connection"; and a terminal, gated on both write
  gates and audited on open and close.

**Write** (all dry-run-first, all audited)

* Scale, restart (the `kubectl rollout restart` mechanism), suspend/resume
  Jobs and CronJobs.
* Rollout history and rollback — ReplicaSets for Deployments, ControllerRevisions
  for StatefulSets and DaemonSets. A kind with no revision concept says so rather
  than returning an empty list.
* Cordon, uncordon and drain.
* Create, replace and delete any resource from YAML, with optimistic concurrency.

**Operational**

* Multiple clusters, tokens encrypted at rest, per-cluster connection testing
  with a permission report.
* An append-only audit trail with a queryable API.
* Read-only mode as the default posture, reported on `/api/health` so the UI
  disables write affordances rather than offering them and failing.

---

## Architecture

```mermaid
flowchart TB
    operator([operator])

    subgraph console["k8boss-admin"]
        direction TB
        nginx["nginx :8021<br/>SPA + same-origin proxy for /api and /api/ws"]

        subgraph api["FastAPI :8020"]
            direction TB
            mw["middleware<br/>CORS → logging → cluster context → error handlers"]

            subgraph read["read path"]
                direction LR
                catalog["catalog<br/>what this cluster serves"]
                reader["reader<br/>list / get / YAML"]
                shaping["shaping<br/>objects → rows"]
                envelope["envelope<br/>items + unavailable + partial"]
                catalog --> reader --> shaping --> envelope
            end

            subgraph write["write path — one funnel"]
                direction TB
                gate["1 · ADMIN_ALLOW_MUTATIONS"]
                pre["2 · preflight<br/>SelfSubjectAccessReview"]
                apply["3 · apply<br/>dryRun=All or for real"]
                diff["4 · diff<br/>live vs projection"]
                audit["5 · audit<br/>every outcome"]
                gate --> pre --> apply --> diff --> audit
            end

            clients["ClusterClientManager<br/>per-cluster clients, deadlines, decrypted tokens"]
            mw --> catalog
            mw --> gate
            reader --> clients
            pre --> clients
            apply --> clients
        end

        db[("SQLite / PostgreSQL<br/>registered clusters — tokens encrypted<br/>audit trail — append-only")]
    end

    k1["Kubernetes API server<br/>cluster 1"]
    k2["Kubernetes API server<br/>cluster N"]

    operator --> nginx --> mw
    clients --> k1
    clients --> k2
    audit --> db
    clients -.reads registrations.-> db
```

No cluster contents pass through that database. Registered clusters and the audit
trail are the only things it holds. Details, including the request lifecycle and
the module map, are in [`docs/architecture.md`](docs/architecture.md).

---

## Configuration

All backend settings are environment variables with safe defaults, where "safe"
means read-only.

### Backend

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_ALLOW_MUTATIONS` | `false` | **The write gate.** False makes every write return `403 mutations_disabled` before the cluster is touched, and `/api/health` report `mutations: disabled` so the UI disables the buttons. Dry-run stays available: previewing is a read |
| `SECRET_REVEAL_ENABLED` | `false` | Lets the single-object Secret read return values when asked with `?reveal=true`. Separate gate, separate blast radius; every reveal is audited either way |
| `AUTH_ENABLED` | `false` | Requires a managed local or LDAP session for every API and WebSocket request except health and login |
| `AUTH_SESSION_TTL_HOURS` | `12` | Lifetime of the revocable HttpOnly session cookie, from 1 to 168 hours |
| `AUTH_COOKIE_NAME` | `k8boss_admin_session` | Session cookie name |
| `AUTH_COOKIE_SECURE` | `false` | Adds the cookie `Secure` flag. Set true for every HTTPS deployment |
| `AUTH_BOOTSTRAP_USERNAME` / `AUTH_BOOTSTRAP_PASSWORD` | *(empty)* | Creates the first local administrator only when no users exist |
| `LDAP_ENABLED` | `false` | Enables LDAP as an alternate login provider |
| `LDAP_URL` / `LDAP_START_TLS` | *(empty)* / `false` | Directory endpoint. Either `ldaps://` or StartTLS is mandatory |
| `LDAP_TLS_VALIDATE` / `LDAP_CA_CERTIFICATE_FILE` | `true` / *(empty)* | Validate the directory certificate, optionally with a private CA bundle |
| `LDAP_BIND_DN` / `LDAP_BIND_PASSWORD` | *(empty)* | Search identity; empty uses an anonymous search bind |
| `LDAP_USER_SEARCH_BASE` / `LDAP_USER_SEARCH_FILTER` | *(empty)* / `(uid={username})` | User search scope and escaped filter template |
| `LDAP_USERNAME_ATTRIBUTE` / `LDAP_DISPLAY_NAME_ATTRIBUTE` / `LDAP_EMAIL_ATTRIBUTE` | `uid` / `cn` / `mail` | Profile attributes synchronized at login |
| `LDAP_ADMIN_GROUP_DN` | *(empty)* | Exact `memberOf` DN whose members become console administrators |
| `LDAP_CONNECT_TIMEOUT_SECONDS` | `5` | LDAP connect and response deadline |
| `DATABASE_URL` | `sqlite:///./k8boss_admin.db` | SQLAlchemy URL. SQLite for dev, PostgreSQL in cluster. Holds the console's own state only |
| `ENCRYPTION_KEY` | *(empty)* | Secret used to derive the Fernet key protecting stored cluster tokens. Empty means a key is generated **once** and persisted beside the database. Never let this be regenerated per boot: every stored token becomes undecryptable and the symptom is every cluster failing to connect after an unrelated restart |
| `ENCRYPTION_KEY_FILE` | *(empty)* | Override for that generated key's path. Empty means "next to the SQLite database", or `./k8boss_admin.key` when the database is not SQLite |
| `K8S_CONNECT_TIMEOUT_SECONDS` | `5` | TCP connect deadline for Kubernetes calls |
| `K8S_READ_TIMEOUT_SECONDS` | `30` | Read deadline for non-streaming Kubernetes calls. Without deadlines, a black-holed connection pins a threadpool worker forever while the liveness probe keeps answering healthy |
| `K8S_WATCH_READ_TIMEOUT_SECONDS` | `600` | Read deadline for watches and log/exec streams, which are legitimately idle between events. A pod that logs nothing for five minutes is normal |
| `KUBECONFIG_PATH` | `~/.kube/config` | Kubeconfig used **only when no cluster is registered**. Ignored in-cluster |
| `KUBE_CONTEXT` | *(unset)* | Context name within that kubeconfig. Unset uses its current-context |
| `IN_CLUSTER_MODE` | `false` | Authenticate with the pod's own ServiceAccount when no cluster is registered. Set to `true` by `deploy/deployment.yaml` |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000,http://localhost:8080` | Comma-separated allowed origins. Only relevant for split-origin deployments and the dev server — nginx proxies same-origin |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. Request bodies are never logged and query-string **values** are dropped at every level |
| `APP_VERSION` | `0.1.0` | Reported by `/api/health` |

### Frontend

| Variable | Where | Default | What it does |
|---|---|---|---|
| `BACKEND_URL` | frontend container, runtime | `http://backend:8020` | Where nginx proxies `/api` and `/api/ws`. Rendered into the config at container start, so one image serves compose and Kubernetes |
| `VITE_BACKEND_URL` | `npm run dev`, build-time | `http://localhost:8020` | Where the Vite dev server proxies `/api` |
| `VITE_API_BASE` | build-time | `/api` | Absolute API base for split-origin deployments. Leave it alone for same-origin, which is what removes CORS from the picture |

### docker-compose

| Variable | Default | What it does |
|---|---|---|
| `UI_PORT` | `8021` | Published port for the console |
| `API_PORT` | `8020` | Published port for the API |
| `API_BIND` | `127.0.0.1` | Interface the API is published on. Keep loopback unless application auth or a trusted proxy protects it |
| `KUBECONFIG` | `~/.kube/config` | Host kubeconfig mounted read-only for the no-clusters-registered fallback |

---

## RBAC

The console needs a ServiceAccount in each cluster it administers.
[`deploy/rbac.yaml`](deploy/rbac.yaml) ships two ClusterRoles so that read-only is
an install and not a configuration:

* **`k8boss-admin-reader`** — bound by default. Enough for every read in the API
  contract: nodes, pods, namespaces, events, workloads across `apps` and `batch`,
  ControllerRevisions, Services and EndpointSlices, ConfigMaps, ServiceAccounts,
  PVCs/PVs/StorageClasses, Ingresses, RBAC objects, PodDisruptionBudgets, CRD
  definitions, plus API discovery and `create selfsubjectaccessreviews` (which
  creates nothing — it is how preflight asks).
* **`k8boss-admin-writer`** — defined, **binding commented out**. Adds the write
  verbs: `*/scale`, `patch` on workload kinds, `patch nodes`, `create
  pods/eviction`, `delete pods`, `create pods/exec`, and create/update/delete on
  the config, storage and networking resources.

Two rules are worth knowing before you apply anything:

* **`list secrets` is the most privileged line in the reader role.** The console
  never puts a Secret value in a list response — but RBAC grants API access, not
  the console's discipline about it. Deleting that rule costs exactly one tab.
* **`create pods/exec` bypasses every other control here.** A shell in a pod can
  do whatever that pod's own ServiceAccount can, and there is no diff for a
  keystroke. Withholding it is the only real control over it.

Every permission, grouped by feature, with the exact degradation you get from
withholding it, is in [`docs/rbac.md`](docs/rbac.md).

---

## Development

```bash
make dev-backend       # uvicorn on :8020, reload, SQLite, mutations off
make dev-frontend      # Vite on :5174, proxying /api and /api/ws to :8020
make test              # backend pytest + hermetic Playwright suite
make lint              # ESLint; fails the build, and so does CI
make build             # production bundle into frontend/dist
make docker-build      # both images
```

Ports are 8020/5174 rather than 8000/5173 because K8Boss owns 8010/5173 and the
two are routinely run side by side.

The Playwright suite intercepts every `/api/**` request, so it needs no backend
and no cluster — a smoke suite that needs a live cluster is a smoke suite nobody
runs. It asserts on the two frontend consumption rules the whole contract leans
on: a `partial: true` response renders a persistent banner, and a `null` numeric
renders as an em dash rather than as `0`.

Dev runs SQLite and production runs PostgreSQL, and CI only exercises SQLite.
Anything engine-divergent needs a deliberate Postgres check.

## Deployment

```bash
kubectl apply -k deploy/
kubectl -n k8boss-admin port-forward svc/k8boss-admin-frontend 8021:8021
```

That gets a console that can read the cluster it runs in (via `IN_CLUSTER_MODE`)
and write to nothing. Built-in auth is still disabled in the base manifest.
Before enabling the optional Ingress, either enable local/LDAP auth with the
`k8boss-admin-auth` Secret or put a trusted authenticating proxy in front.

---

## Documentation

| Document | Subject |
|---|---|
| [`docs/api-contract.md`](docs/api-contract.md) | **Normative.** Paths, shapes, error codes, envelopes, the five rules |
| [`docs/safety-model.md`](docs/safety-model.md) | Preflight, dry-run, diff, concurrency, drain, audit, read-only — and the failure each prevents |
| [`docs/architecture.md`](docs/architecture.md) | System shape, request lifecycle, module map, why the mutation funnel is one function |
| [`docs/rbac.md`](docs/rbac.md) | Every permission, by feature, with its degradation |
| [`docs/adr-0001-dry-run-first.md`](docs/adr-0001-dry-run-first.md) | Why dry-run-then-confirm rather than optimistic-with-undo |
| [`docs/adr-0002-lineage.md`](docs/adr-0002-lineage.md) | What came from K8Boss, what did not, why they stay separate |
| [`CLAUDE.md`](CLAUDE.md) | Working agreements for anyone (or anything) changing this repository |

## License

Apache-2.0. Copyright 2026 A. Eren Saganda. See [LICENSE](LICENSE).
