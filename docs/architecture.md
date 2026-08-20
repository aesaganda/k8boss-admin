# Architecture

What the console is made of, what happens to a request as it travels through it,
and the two or three decisions that everything else follows from.

`docs/api-contract.md` is normative for the wire; this document is about the
shape of the code behind it. Where they disagree the contract wins and this file
is out of date.

---

## 1. System shape

Three processes and one small database.

```
                    ┌──────────────┐
   operator ───────▶│  nginx (SPA) │  serves the React bundle,
                    │    :8021     │  proxies /api and /api/ws same-origin
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐        ┌────────────────────┐
                    │   FastAPI    │───────▶│ Kubernetes API     │
                    │    :8020     │        │ server (per        │
                    └──────┬───────┘        │ registered cluster)│
                           │                └────────────────────┘
                    ┌──────▼───────┐
                    │ SQLite /     │  registered clusters (tokens encrypted)
                    │ PostgreSQL   │  audit trail (append-only)
                    └──────────────┘
```

**The database holds the console's own state and nothing else.** No resource the
UI shows is cached in it. Every pod, node and Deployment on every page is read
live from the cluster on that request. That is a deliberate limit rather than an
unimplemented feature: a cache is a second source of truth, and a console whose
job is to tell an operator what is true right now cannot afford to render a
stale answer with a confident face. The cost is latency on large listings and a
hard dependency on the API server being reachable — both of which are visible to
the user, which is the trade being made.

The SPA is served by nginx, which also reverse-proxies `/api` and `/api/ws` to
the backend. Same-origin, so CORS is not in the picture in a normal deployment
(`cors_origins` exists for split-origin ones and for `npm run dev` on :5174).

---

## 2. Request lifecycle

The middleware order is set in `app/main.py` and is load-bearing. Starlette
wraps each added middleware *around* what came before, so the last added is
outermost:

```
CORS ─▶ request logging ─▶ authentication ─▶ cluster context ─▶ exception handlers ─▶ route
```

**CORS outermost is the whole point.** Everything the app can produce — a 502
for an unreachable cluster, a 403 naming a missing RBAC grant — passes back out
through it and keeps its `Access-Control-Allow-Origin` header. An error rendered
*outside* CORS (which is what happens to anything that reaches Starlette's
`ServerErrorMiddleware`) is blocked by the browser, `fetch()` rejects with
`TypeError: Failed to fetch`, and the operator sees a network error while the
real reason is never delivered. `register_exception_handlers` exists so nothing
gets that far.

A read then travels like this:

```
  HTTP GET /api/workloads?cluster_id=1&namespace=prod
      │
  1.  AuthenticationMiddleware       resolves the opaque session cookie and
      (app/middleware/auth.py)        enforces CSRF on unsafe HTTP methods
      │
  2.  ClusterContextMiddleware       pins cluster_id, the verified principal
      (app/k8s/context.py)            (or legacy proxy actor), and peer IP
      │
  3.  route handler                   app/api/workloads.py — parses query
      │                               parameters, never cluster_id
      │
    4.  ClusterClientManager            app/k8s/client.py — resolves the pinned
      │                               id to a cached, deadline-carrying client
      │                               bundle built from the stored endpoint and
      │                               the decrypted token
      │
    5.  catalog.resolve()               app/resources/catalog.py — is this
      │                               resource served here, is it namespaced,
      │                               which verbs does it support
      │
    6.  reader / service layer          app/resources/reader.py for generic
      │                               reads; app/services/* for the typed ones
      │
    7.  shaping                         app/resources/shaping.py and
      │                               app/services/workloads.py — objects to
      │                               rows, pure functions, no I/O
      │
    8.  envelope()                      app/resources/envelope.py — items,
      │                               continue, remaining, partial, unavailable
      ▼
  200 {"items": [...], "partial": true, "unavailable": [...]}
```

### Why handlers never take `cluster_id`

Every endpoint is cluster-scoped, and threading an id through roughly two
hundred call signatures has exactly one failure mode: the function that forgets
to pass it answers about a *different cluster*, correctly, confidently, and
without any error. A misattributed answer is worse than no answer, so the id
lives in a `contextvars.ContextVar` set once by the middleware and read by the
client manager at the bottom.

`ClusterContextMiddleware` is pure ASGI rather than `BaseHTTPMiddleware`.
`BaseHTTPMiddleware` runs the downstream app in a separate anyio task, and a
contextvar set in the middleware's own frame does not reliably survive into the
threadpool that executes sync route handlers. The symptom is not an exception —
it is `get_current_cluster_id()` returning `None` inside the handler and the
request silently resolving to the fallback cluster.

### Where a failed read goes

Two destinations, and which one a failure takes is the difference between an
endpoint that failed and an endpoint that is partially blind.

* **The endpoint's primary read** failing means the endpoint failed. It raises
  an `AdminError`, and `app/api/exception_handlers.py` renders the §1.3 envelope
  — stable `error` code, human `message`, verbatim `detail`, actionable `hint`,
  machine-readable `context`.
* **A secondary read** failing costs a column, not the page. It is wrapped in
  `envelope.collect()`, which turns the failure into an `unavailable[]` entry
  and leaves the variable it was assigning at `None`. `envelope()` then
  *computes* `partial` from that list — there is no parameter for it, so no
  caller can claim `partial: false` beside a populated `unavailable`.

`GET /api/namespaces` is the smallest example of both in one handler: the
namespace listing is primary and raises; the per-namespace pod tally is
secondary, so losing it sets every row's `pod_count` to `null` and names the
reason.

---

## 3. Module map

### Backend — `backend/app/`

| Module | Responsibility |
|---|---|
| `main.py` | App construction, middleware order, lifespan. Logs a warning at startup when `ADMIN_ALLOW_MUTATIONS` is on |
| `config.py` | `Settings` from the environment. Every gate defaults to the safe value |
| `errors.py` | One exception class per stable `error` code, and `from_api_exception` mapping the Kubernetes client onto them **by HTTP status, not by `Status.reason`** |
| `crypto.py` | Fernet encryption for stored cluster tokens; generate-once-and-persist key handling |
| `database.py`, `models.py` | Engine and session; `Cluster` and the append-only `AuditRecord` |
| `middleware/logging.py` | One JSON line per request with a correlation id. Bodies never logged, query **values** dropped |
| `k8s/context.py` | The contextvars and `ClusterContextMiddleware` |
| `k8s/auth.py` | `AuthProvider` strategy — adding OIDC or client certs means adding a class here, not editing the client |
| `k8s/client.py` | `ClusterClientManager`: per-cluster client bundles, deadlines, typed transport failures, CA temp-file lifecycle |
| `resources/catalog.py` | Discovery. A broken API group becomes an `unavailable` entry, never a smaller catalog |
| `resources/reader.py` | Generic list/get/YAML, plus trimming (`managedFields` always, `last-applied-configuration` from lists) |
| `resources/shaping.py` | Pure row shapers — `pod_row`, `phase_detail`, the §8 config/storage/access rows |
| `resources/envelope.py` | `envelope()`, `collect()`, `unavailable_entry()` — the mechanism behind "empty is never blind" |
| `services/workloads.py` | Six controller kinds into one row; `Unknown` status, `null`-vs-`0` counts |
| `services/nodes.py` | Quantity parsing (`Decimal`), node rows, per-node requested totals |
| `admin/mutate.py` | **The single write funnel.** Gate → preflight → apply → diff → audit |
| `admin/preflight.py` | `SelfSubjectAccessReview`. Keeps a clean denial distinct from a failed review |
| `admin/diff.py` | Normalise both sides, render a unified diff, digest it for the audit row |
| `admin/apply.py` | Generic create / replace / delete from submitted YAML |
| `admin/scale.py` | Scale (via `/scale`), restart (template annotation), suspend |
| `admin/rollout.py` | Revision history — ReplicaSets for Deployments, ControllerRevisions for the rest — and rollback |
| `admin/nodes.py` | Cordon, and the drain planner: classify every pod, then execute per-pod |
| `audit/recorder.py` | `record()` from the funnel and from the two privileged reads; `record_console_event()` for sign-ins and user changes; `query()`, `stream()` and `verify_chain()` for §10 |
| `audit/integrity.py` | The hash chain. A flush listener that links every new record, and `verify()` — which reports `intact`, `broken` or `partial`, and never claims the third is the first |
| `audit/export.py` | §10.4 serialisers. NDJSON is byte-faithful; CSV is flattened and defangs cells a spreadsheet would execute as a formula |
| `identity/service.py` | Local passwords, opaque sessions, LDAP synchronisation, federated (SSO) provisioning and its two account-takeover refusals |
| `identity/oidc.py` | OpenID Connect: discovery, PKCE, and the ID-token verification each of whose checks blocks a specific attack |
| `identity/handshake.py` | The sealed, short-lived cookie carrying one sign-in across the redirect. `SameSite=Lax`, unlike the session cookie, and the module says why |
| `identity/roles.py` | Group → console role, with the tri-state that keeps "in no groups" apart from "we could not look" |
| `identity/throttle.py` | Sign-in rate limiting. Reserves before the password check, in its own table, so a burst cannot walk through the limit and a failed audit write cannot silently disable it |
| `schema_upgrade.py` | Additive column upgrades for databases older than the current build, and a startup refusal when one could not be added |
| `api/*.py` | Routers. Thin: parse, call, envelope. The logic lives below them |

### Frontend — `frontend/src/`

| Path | Responsibility |
|---|---|
| `api/client.js` | One fetch wrapper. `ApiError.code` carries the §1.3 code; callers branch on the code, never on a message |
| `contexts/` | Cluster (active cluster + switcher), Health (mutations gate, degraded list), Namespace, Notification, Theme, Density (the Comfy/Compact row height the workload and pod tables read) |
| `components/ui/` | The toolbox: `DataTable`, `PageHeader`, `StatusBadge`, `MetricCard`, `PartialBanner`, `ConfirmDialog`, `CodeBlock`, `DescriptionList`, `DensityToggle`, cells, inputs, states. `columnWidths.js` sits behind `DataTable` and owns the resizable columns: the per-table widths, their persistence, and the drag and keyboard lifecycle. `DataTable`'s `density` prop is the other half of that: Compact clamps every row to one line, and does it without changing a single column width |
| `components/MutationDialog.jsx` | The dry-run → diff → confirm spine every write dialog is built on |
| `components/DiffView.jsx`, `YamlEditor.jsx` | Rendering the unified diff; editing a manifest |
| `components/{Scale,Restart,Suspend,Rollback,Delete,Cordon,Drain}Dialog.jsx` | The seven actions, all over `MutationDialog` |
| `components/LogViewer.jsx`, `PodTerminal.jsx` | The two WebSocket surfaces |
| `pages/` | One per route; `_data.js` and `_parts.jsx` hold the shared fetch and row helpers |

---

## 4. Why the mutation funnel is one function

§0 of the contract makes four promises about every write: it is preflighted, it
can be dry-run, it produces a diff, and it is audited — including when it fails.

Those four could be a convention that every write endpoint follows. They are
not, and the reason is what conventions do over time. A convention followed by
twelve endpoints is a convention that nine of them follow after the next
refactor, and the three that stopped look identical from the outside: same
response shape, same status code, no error, no test failure. The first evidence
is an audit trail with a hole in it, discovered while someone is trying to work
out who scaled the payments service to zero.

So `app/admin/mutate.py` is one function with one order, and it is the only
place in the codebase that calls an `apply_fn`:

1. **The mutations gate.** `ADMIN_ALLOW_MUTATIONS=false` refuses a real write
   *before the cluster is touched*, and records the attempt as a denial. Dry-run
   is permitted in read-only mode — previewing a change is a read.
2. **Preflight.** `SelfSubjectAccessReview` for this exact
   verb/group/resource/namespace/name/subresource, so a denial names the missing
   grant rather than relaying a bare "forbidden". It runs for dry runs too,
   because the API server needs the same permission to project a write as to
   perform one, and discovering that at the confirm step rather than the preview
   step is the worse of the two.
3. **Apply.** The caller's closure, told whether this is a dry run. The only
   code that touches the cluster.
4. **Diff.** Live versus the API server's own projection.
5. **Audit.** Every terminal state, not just success.

Endpoints supply *what* the write is; the funnel supplies *how every write
behaves*. An endpoint that skipped a step would have to be written to bypass
this module in a way a reviewer can see in the import list.

`applied` is true only when `dry_run` is false **and** the call succeeded.
Nothing else in the response may be read as evidence that a cluster changed: a
successful dry run returns a full projected object, a `resourceVersion` and a
diff, and a UI that read those as success would report a change that did not
happen.

---

## 5. Two abstractions that stay separate

**Reading and writing.** `app/resources/` and `app/services/` never mutate;
`app/admin/` never shapes a row for display. The write path reads (it needs the
`before` side of a diff) but it does so through `admin/apply.read_object`, which
returns the raw object rather than a shaped row — a diff of shaped rows would be
a diff of the console's opinions.

**The catalog and the reader.** `catalog.resolve()` answers "does this cluster
serve this, is it namespaced, what verbs" from a bounded-TTL cache; `reader`
answers "what is in it" live, every time. Keeping them apart is what lets
`resolve()` refuse to report `unsupported` for a group that is in its own
`unavailable` list — during an aggregated-APIService outage, browsing
`metrics.k8s.io` says the API server could not answer, not that the cluster does
not have metrics. Getting that backwards sends an operator to install something
they already have.

---

## 6. Degradation, concretely

The console's failure rule is that a missing dependency degrades exactly its own
surface and says which question it failed to answer:

| What fails | What the operator sees |
|---|---|
| One secondary read (EndpointSlices, the per-node pod listing) | The column is `—` with a tooltip; one `unavailable[]` entry; `partial: true` banner |
| One collector on the cluster overview | That panel is `null`, the other five are unaffected, the page is still 200 |
| The whole API server (unreachable) | `502 cluster_unreachable`, and *no* page claims a cluster is empty |
| One RBAC grant missing | `403 rbac_denied` with `context` naming the target and `hint` naming the grant; the button is disabled **with the reason**, not hidden |
| An API group not served here | `501 unsupported`, rendered as "not present on this cluster" and not coloured red |
| The console's own database unreadable | `/api/health` reports `registered: null` — not `0`, which would say the operator has no clusters registered |

The bar all of these are measured against: **a wrong answer delivered
confidently is worse than no answer.** When a code path swallows an error and
returns `[]`, `None` or `0`, the caller must be able to tell "nothing happened"
from "we could not look". If it cannot, that is the defect — not the missing
data.
