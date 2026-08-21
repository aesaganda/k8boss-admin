# k8boss-admin

A Kubernetes administration console. It browses every resource a cluster serves
and changes them through a path that preflights the permission, dry-runs the
write, shows the operator a diff, waits for a confirmation, and records what
happened — including what failed.

**What it is not.** Not a monitoring system, not a deployment engine, not a
GitOps controller, and not a security-posture product. It holds no cluster state:
every page is a live read. Optional local, LDAP or OpenID Connect authentication
protects the console; legacy proxy mode remains available when it is disabled. It was split out
of [K8Boss](https://github.com/aesaganda/k8boss) and shares no code with it —
`docs/adr-0002-lineage.md` says what came across and what deliberately did not.

## Subsystem map

| Directory | What lives there | Rules |
|---|---|---|
| `backend/app/api/` | FastAPI routers. Thin: parse, call, envelope | Logic belongs below this layer |
| `backend/app/k8s/` | Per-request cluster context, auth strategy, client manager | `docs/architecture.md` §2 |
| `backend/app/resources/` | Catalog (discovery), reader (generic list/get/YAML), shaping (rows), envelope | `docs/api-contract.md` §1.2, §4, §8 |
| `backend/app/services/` | Typed read models: the unified workload row, node rows | `docs/api-contract.md` §5, §6 |
| `backend/app/admin/` | **Every write.** The funnel, preflight, diff, apply, scale, rollout, node drain, debug containers | `docs/safety-model.md` |
| `backend/app/audit/` | Append-only, hash-chained trail: `record()`, `query()`, `verify()`, export | `docs/api-contract.md` §10 |
| `backend/app/identity/` | Local password hashing, opaque sessions, LDAP search-and-bind, OIDC single sign-on, sign-in throttling | `docs/api-contract.md` §12 |
| `backend/tests/` | pytest on SQLite. The fake Kubernetes client **raises** on an unstubbed call | — |
| `frontend/src/` | React 19 / Vite / PatternFly 6 SPA | `docs/api-contract.md` §11 |
| `deploy/` | Namespace, RBAC, Deployments, Services, Ingress, kustomization | `docs/rbac.md` |
| `docs/` | Design docs and ADRs. **`api-contract.md` is normative** — where it and the code disagree, the code is the defect report | — |

> **The contract is the spec, and the code is the truth about today.** When you
> are wiring a new module to an existing one, bind to the **real signatures on
> disk**, and if they differ from `docs/api-contract.md`, say so rather than
> quietly implementing something a caller does not call. The contract is
> idealised; `app/api/resources.py` and `app/api/workloads.py` are what actually
> imports your module.

---

## Repo-wide invariants

These are §0 of `docs/api-contract.md`, restated with the failure each one
prevents. Breaking one is a defect regardless of which layer it happens in.

### 1. Empty is never blind

`items: []` means the cluster has none of the thing. A read that could not
happen goes in `unavailable[]` with `partial: true`. **No endpoint may swallow an
error into an empty array.**

*The failure.* The obvious implementation of a resilient handler is
`except ApiException: return []`, and the page it produces is confident, empty
and wrong. An operator reading "this namespace has no pods" acts on it — during a
cleanup, that means deleting the namespace.

*What makes it mechanical rather than editorial.* `envelope()` **computes**
`partial` from `unavailable`; there is no parameter for it, so no caller can set
`partial: false` beside a populated list and none can forget to set it true.
`collect()` is a context manager that turns the two Kubernetes failure types into
an `unavailable` entry and leaves the variable it was assigning at `None`, so
"could not look" falls out of the control flow instead of needing to be
remembered.

**The corollary that catches people: a number we could not derive is `None`,
never `0`.** A node showing `0` requested cores and `0` pods reads as idle, and an
idle node is the one an operator picks to drain. `pod_count`, `restarts_24h`,
`endpoint_count`, every `requested` total — `null` when the read failed, `0` only
when it is a real zero.

### 2. Every mutation is preflighted

Before any write, a `SelfSubjectAccessReview` for the exact
`verb`/`group`/`resource`/`namespace`/`name`/`subresource`. A denial is
`403 rbac_denied` naming the missing permission — never a bare "forbidden"
relayed from the API server.

*The failure.* One console action can touch four resources. "Forbidden" does not
say which one, so the operator guesses, and the guess that ends the guessing is a
ClusterRole wider than they needed.

**Keep a clean denial distinct from a failed review.** `allowed: false` with a
non-null `evaluationError` means *we do not know* whether the caller may act.
Collapsing it into a denial tells an operator they lack a permission they may well
hold, and sends them to edit a ClusterRole that is already correct.

**Nothing is cached.** A cached `allowed` outlives the RBAC change that revoked
it.

### 3. Every mutation can be dry-run, and the destructive ones must be

`dryRun` defaults to **true** on every write body. `dryRun: true` sends
`dryRun=All` and returns the API server's own projected object plus a unified
diff. The UI shows that diff before the confirming call. There is no button that
writes to a cluster without having shown a diff first.

*The failure.* An edited manifest deletes a field the editor never looked at; a
rollback restores a pod template missing a container the live one has. Both are
invisible in the request and obvious in the projection.

Two properties are load-bearing and easy to break in a refactor:

* **`dryRun=All` is a query parameter on the same call**, not a separate code
  path. A preview that took a different route would eventually preview something
  else.
* **`applied` is true only when `dry_run` is false *and* the call succeeded.** A
  successful dry run returns a full object, a `resourceVersion` and a diff —
  everything that looks like success. Nothing else in the response may be read as
  evidence that a cluster changed.

**Dry-run needs the same RBAC verb as the real write.** The API server requires
`patch` to project a patch. Do not describe dry-run as a lesser permission.

### 4. Optimistic concurrency is mandatory on update

`PUT` carries the `resourceVersion` the user was looking at. A mismatch is
`409 conflict` with `context.currentResourceVersion` and a **fresh diff against
live**, never a blind overwrite.

*The failure.* Two operators on the same Deployment during an incident: the
second save silently reverts the first, including the emergency image pin, with
nothing recording that two changes were ever in flight.

Enforced twice on purpose — locally (which is what produces the fresh diff) and
by the API server (because the submitted object carries the *caller's*
`resourceVersion`, closing the window a local-only check loses the race in).

### 5. Every write is audited

Actor, cluster, verb, target, dry-run flag, diff digest, outcome — on every
terminal state, **including the writes that failed, were denied, or conflicted**.

*The failure.* The question after an incident is "who tried". A trail holding
only the writes that worked cannot answer it.

`AuditRecord` is append-only, enforced by a session hook that refuses to flush an
update or a delete. There is no delete endpoint. The row is assembled from
request context rather than from arguments, because a caller that can pass the
actor can pass the wrong one. And a failed INSERT never fails the write: by then
the cluster has already changed, and turning a logging fault into a 500 would
tell the operator their action failed when it did not.

That hook protects the table against *this application*, and against nothing
else. Records are therefore also **hash-chained**, which prevents nothing and
makes a `psql`-level edit **detectable** — the strongest honest promise an
application can make about storage it does not own. Sign-ins are recorded the
same way, because "who tried" is asked about the console as often as about a
cluster.

**Records that predate the chain are never back-filled.** Hashing them now would
attest whatever they say today, turning "we do not know" into "verified" — the
defect standard with a signature attached. They are counted, and the verdict for
the trail is withheld as `partial`, which is never rendered as a pass. See
`docs/adr-0003-audit-hash-chain.md`.

---

## The single write funnel

`backend/app/admin/mutate.py` is the only module that calls an `apply_fn`.
Everything that changes a cluster goes through it, in this order:

```
1. mutations gate   2. preflight   3. apply   4. diff   5. audit
```

The four promises above could be a convention every write endpoint follows. They
are not, because a convention followed by twelve endpoints is a convention nine
of them follow after the next refactor — and the three that stopped are
indistinguishable from outside: same response shape, same status, no failing
test. The first evidence is a hole in the audit trail, found by someone trying to
establish who scaled the payments service to zero.

**If you are adding a write, you are writing an `apply_fn` and a call to
`mutate()`.** If that feels like it does not fit, the answer is almost never a
second path — say so in your report instead.

---

## The error vocabulary

One exception class per stable `error` code, in `backend/app/errors.py`, because
the frontend **branches on the code**. It disables a button on `rbac_denied`,
offers refresh-and-retry on `conflict`, renders "not present on this cluster" on
`unsupported`, and shows a global banner on `mutations_disabled`. A free-text
detail cannot carry that and a 500 carries nothing.

| Code | HTTP | When |
|---|---|---|
| `no_cluster_selected` | 409 | No `cluster_id` and nothing registered |
| `cluster_unreachable` | 502 | DNS/TCP/TLS/deadline — **we never got an answer**, so nothing about the cluster's contents can be inferred |
| `rbac_denied` | 403 | Preflight said no, or the API server did |
| `not_found` | 404 | Object or API resource does not exist |
| `conflict` | 409 | `resourceVersion` mismatch, or the API server called it conflicting |
| `invalid` | 422 | Schema, admission or request validation |
| `mutations_disabled` | 403 | The *deployment* is read-only. **Not** `rbac_denied` — the operator's permissions are irrelevant, and telling them otherwise sends them to fix the wrong system |
| `unsupported` | 501 | The cluster does not serve that API. **Not an error in the UI** — no Ingress CRDs and no `metrics.k8s.io` are ordinary facts, and rendering them red trains people to ignore red |
| `upstream_error` | 502 | Anything else, including 401 (the cluster answered — it refused us) |

`from_api_exception` maps by **HTTP status, not `Status.reason`**: the reason
string is not stable across API server versions or admission webhooks; the status
code is part of the Kubernetes API contract.

Handlers are registered on the app so they render **inside** `CORSMiddleware`.
Anything escaping to `ServerErrorMiddleware` is rendered outside it, the browser
blocks the response, and `fetch()` rejects with `TypeError: Failed to fetch` —
the operator sees a network error and the real reason is never delivered.

---

## Degradation is isolated, and it says which question it failed to answer

A failing dependency degrades exactly its own surface:

* One secondary read failing costs a **column**, names itself in `unavailable[]`,
  and leaves the endpoint at 200.
* One overview collector failing sets **its own key** to `null`; the other five
  are unaffected.
* A broken API group in discovery becomes an `unavailable` entry and the rest of
  the catalog is still returned. `resolve()` refuses to report `unsupported` for a
  group that is in its own `unavailable` list — during an aggregation outage,
  browsing `metrics.k8s.io` says the API server could not answer, not that the
  cluster has no metrics. Backwards, that sends someone to install what they
  already have.
* The console's own database being unreadable makes `/api/health` report
  `registered: null` — not `0`, which would say the operator has no clusters.

### The defect standard

> **A wrong answer delivered confidently is worse than no answer.** For a console
> that also writes: **an action reported as done is a claim, and a claim about
> somebody's cluster has to be true.**

Canonical instances in this tree:

* `Unknown` is a required workload status. A controller that has not written a
  status for the current `generation` has told us nothing; reporting that as
  `Healthy` makes a green row a lie.
* `phase_detail` exists because phase lies. A `Running` pod with a
  `CrashLoopBackOff` container is not Running.
* A drain over three pods the API server refused returns `applied: true` with
  `failed: 3` and `drained: false` — never a bare success. "Drained" over three
  stuck pods is the sentence that gets a machine terminated with a database on it.
* `force` on a drain means "I read this plan and accept it". It does **not**
  defeat a PodDisruptionBudget — the API server enforces that on the eviction
  subresource — and claiming otherwise sells a bigger hammer than exists.

Practically: when a code path swallows an error and returns `[]`, `None` or `0`,
the caller must be able to tell "nothing happened" from "we could not look". If it
cannot, that is the defect — not the missing data.

---

## House style

* Comments and docstrings record **why**, and the failure mode that motivated the
  code. Not "loop over pods" but "requested is None, not 0, when the pod listing
  failed: 0 would read as an idle node and send someone to drain the wrong box."
* Python 3.11+ typing, `from __future__ import annotations` at the top of every
  module.
* Shapers are pure. Nothing in `resources/shaping.py` performs I/O — a shaper
  that could read from a cluster could also fail to, and then the place that
  decides whether a `null` is "absent" or "unreadable" would have no way to
  record the difference. Values a shaper cannot derive from the object in front
  of it are passed in by the caller, who owns the read and its `unavailable`
  entry.
* Real working code. No TODOs, no `pass` stubs, no "in a real implementation".

---

## Running the tests

```bash
cd backend && python -m pytest -q          # the whole backend suite, SQLite
cd frontend && npm run dev                 # :5174
cd frontend && npx playwright test         # hermetic, route-mocked
make test                                  # both
```

**The fake Kubernetes client in `tests/conftest.py` raises on an unstubbed
method.** That sharp edge is deliberate: a fake that returned an empty list for
anything it was not asked about would make "empty is never blind" untestable —
the test would pass whether the code reported the failure or swallowed it, which
is the exact bug class this project cares most about. If you hit the assertion,
stub the call, or notice that the code is making one it should not.

`conftest.py` sets `DATABASE_URL`, `ENCRYPTION_KEY`, `ADMIN_ALLOW_MUTATIONS` and
`SECRET_REVEAL_ENABLED` **before any `app.*` import**, because `app.config` builds
its `Settings` at import time and `app.database` builds the engine from those
settings at import time too. Setting them anywhere later has no effect and the
symptom is a test that passes for the wrong reason.

Ports are 8020 (API) and 5174 (dev server), not 8000/5173: K8Boss owns 8010/5173
and the two get run side by side.

The Playwright suite intercepts every `/api/**` request, so it needs no backend
and no cluster. It runs against a **production build** via `vite preview`, not the
dev server — the lazy page chunks only exist as separate files in a build, so a
chunk-splitting mistake is invisible in dev and caught there.

---

## What CI gates

`.github/workflows/ci.yml`, three jobs, all required, none needing a cluster:

* **backend** — `pytest` on SQLite.
* **frontend** — `npm ci`, `npm run build`, `npm run lint`.
* **e2e** — Playwright against the production build, every `/api/**` mocked.

**Lint is NOT `continue-on-error`, and that is a decision.** K8Boss runs its
frontend lint advisory, and real errors accumulated behind a green check because
nobody reads the output of a job that passed — until someone ran ESLint by hand
and found a backlog too large to fix in the PR that noticed it. An advisory lint
is worse than no lint, because it also occupies the slot a real one would go in.
If a rule is wrong, change `eslint.config.js`, where the change gets reviewed.

**Dev runs SQLite, production runs PostgreSQL, and CI only exercises SQLite.**
Anything engine-divergent — a raw SQL fragment, a reliance on SQLite's permissive
typing — needs a deliberate Postgres check before it is believed.
`scripts/postgres-check.py` is that check for the audit schema and the sign-in
throttle; point it at a scratch database and run it. It exists because the first
version of `schema_upgrade.py` deadlocked PostgreSQL at startup — forever, before
serving a request — and SQLite could not reproduce it.

---

## Where the truth lives

| Doc | Subject |
|---|---|
| `docs/api-contract.md` | **Normative.** Paths, field names, error codes, envelopes, the five rules |
| `docs/safety-model.md` | Preflight, dry-run, diff, optimistic concurrency, drain, audit, read-only — each with the failure it prevents. **The document this product is judged on** |
| `docs/architecture.md` | System shape, request lifecycle, module map, why the mutation funnel is one function |
| `docs/rbac.md` | Every permission by feature, with the degradation from withholding it |
| `docs/adr-0001-dry-run-first.md` | Why dry-run-then-confirm rather than optimistic-with-undo |
| `docs/adr-0002-lineage.md` | What came from K8Boss, what did not, why the two stay separate |
| `docs/adr-0003-audit-hash-chain.md` | Tamper *evidence* vs tamper prevention, and why pre-chain records are never back-filled |
| `deploy/rbac.yaml` | The shipped roles. Each rule is annotated with the contract section it serves |
| `README.md` | The front door: quickstart, feature list, every environment variable |
