# Contributing to k8boss-admin

Thanks for looking at this project. Before writing code, read this file, then
skim [`CLAUDE.md`](CLAUDE.md) — it was written to steer an AI assistant working
in this repo, but it doubles as the most complete map of the codebase's rules
and is worth five minutes for a human contributor too. For anything it points
at, the doc it points to (`docs/api-contract.md`, `docs/safety-model.md`,
`docs/architecture.md`, the ADRs) is the detailed version.

## Before you open a PR

**Read the two boundary ADRs if your change touches what the console installs
or writes on its own.** [`docs/adr-0004-shipped-router.md`](docs/adr-0004-shipped-router.md)
and [`docs/adr-0008-shipped-olm.md`](docs/adr-0008-shipped-olm.md) explain why
there are exactly two bundles this console ships and installs, and why that
number is deliberately hard to grow. If your change would add a third, it
needs to make the case against both of those documents, not just against the
feature it's replacing — a PR proposing a third bundle without addressing them
will be closed, not because the idea is dismissed but because the argument
has to happen there first.

**For anything else architectural — a new write, a new resource type, a new
identity provider — check `docs/api-contract.md` (§0's normative) and
`docs/safety-model.md` first.** The contract is what callers actually bind to;
if your change and the contract disagree, the code is treated as the defect,
not the doc.

## The rules that aren't optional

These are repo-wide invariants (`CLAUDE.md`'s §0, restated briefly). A PR that
violates one of these will be asked to change regardless of what else it does
well:

1. **Empty is never blind.** A read that failed returns an `unavailable[]`
   entry and `partial: true` — never a silently swallowed error returned as
   `[]`. A number that could not be computed is `null`, never `0`; a `0`
   reads as a real zero (an idle node, a namespace with no pods) and someone
   will act on it.
2. **Every mutation is preflighted.** A `SelfSubjectAccessReview` for the
   exact verb/group/resource/namespace/name/subresource, never cached, before
   any write. A denial is `403 rbac_denied` naming what was missing — never a
   bare "forbidden" relayed from the API server.
3. **Every mutation supports dry-run, and destructive ones require showing
   it.** `dryRun=All` is a query parameter on the *same* call path as the
   real write, not a separate one. `applied: true` is true only when
   `dry_run` is false **and** the call actually succeeded — nothing else in
   the response may be read as evidence that a cluster changed.
4. **Optimistic concurrency on every update.** The caller's `resourceVersion`
   travels with the write; a mismatch is `409 conflict` with a fresh diff
   against live, never a blind overwrite.
5. **Every write is audited, including the ones that failed, were denied, or
   conflicted.** The audit table is append-only and hash-chained; there is no
   delete endpoint and no code path that flushes an update to a row.

If you're adding a write, you're writing an `apply_fn` and a call to
`mutate()` in `backend/app/admin/`, plus a `FeatureGate` if it needs its own
switch. If that doesn't fit the shape of what you're building, say so in the
PR description rather than adding a second path around the funnel — a second
path is the exact failure mode this architecture exists to prevent, and it
won't be merged as one.

## House style

- Comments and docstrings explain **why**, specifically the failure mode a
  piece of code prevents — not what the code does (identifiers should already
  say that). "Requested is `None`, not `0`, because a `0` here would read as
  an idle node" is the right shape; "loop over pods and sum requests" is not.
- Python: 3.11+ typing, `from __future__ import annotations` at the top of
  every module.
- Shapers (`backend/app/resources/shaping.py`) are pure — no I/O. A shaper
  that could fail to read something would also need somewhere to record that
  failure, which isn't its job; the caller that owns the read owns the
  `unavailable` entry too.
- No TODOs, no `pass` stubs, no "in a real implementation would…" comments.
  If it's not done, don't open the PR yet — draft PRs are fine, silent stubs
  merged as if finished are not.
- Frontend/backend line endings are LF in the tree (`.gitattributes` enforces
  this on checkout). If your editor or OS rewrites a file as CRLF, `git
  status` will show the whole file as changed — that's a tooling problem to
  fix locally, not something to commit around.

## Running things locally

```bash
cd backend && python -m pytest -q          # backend suite, SQLite
cd frontend && npm run dev                 # dev server on :5174
cd frontend && npx playwright test         # e2e, hermetic, every /api/** mocked
make test                                   # both suites
```

Ports are 8020 (API) and 5174 (frontend dev server) — deliberately not the
framework defaults, since this project sits side by side with a sibling
project that uses those.

`backend/tests/conftest.py`'s fake Kubernetes client **raises** on any method
it wasn't told to expect, rather than returning an empty result. That's
intentional: a fake that quietly returned `[]` for an unstubbed call would
make rule 1 above untestable, since the test would pass whether your code
correctly reported the failure or silently swallowed it. If you hit that
assertion, stub the call your code needs — and if the call surprises you,
that's worth a second look at whether the code should be making it at all.

`conftest.py` also sets `DATABASE_URL`, `ENCRYPTION_KEY`,
`ADMIN_ALLOW_MUTATIONS`, and `SECRET_REVEAL_ENABLED` **before any `app.*`
import** — `app.config` builds its `Settings` object at import time, so
setting these later in a test has no effect and the symptom is a test that
passes for the wrong reason.

## What CI checks

Four required jobs, none needing a live cluster — see
[`.github/workflows/ci.yml`](.github/workflows/ci.yml) for the reasoning
behind each:

- **backend** — `pytest` against SQLite
- **frontend** — `npm ci`, `npm run build`, `npm run lint` (lint is a real
  gate here, not advisory — a rule you think is wrong gets changed in
  `eslint.config.js`, where the change is reviewed, not silenced per line)
- **e2e** — Playwright against a production build, with every `/api/**`
  request intercepted
- **image** — builds `backend/Dockerfile` from the repo root and asserts the
  vendored OLM release (§33) is present, unaltered, and loadable inside the
  built image

All four are required on every PR. If one is red for a reason unrelated to
your change, say so in the PR rather than pushing past it — a flaky-looking
CI job is worth a bug report of its own.

## Postgres divergence

Dev runs SQLite; production runs PostgreSQL; CI only ever exercises SQLite.
Anything that could behave differently between the two — raw SQL, reliance on
SQLite's permissive typing — needs a deliberate check before it's trusted.
`scripts/postgres-check.py` exists for exactly this reason (it caught a
startup deadlock SQLite couldn't reproduce); point it at a scratch database
if your change touches `backend/app/audit/`, `backend/app/database.py`, or
sign-in throttling.

## Commit messages and PR descriptions

Say what changed and, more importantly, why — a one-line "fix bug" tells a
future bisector nothing. If your change closes a gap the safety model
describes (an unaudited path, a swallowed error, a missing preflight), name
the failure mode explicitly; it's the fastest way for a reviewer to see why
the change matters.

## Questions

Open a [discussion or issue](../../issues) if something here is unclear
before you've written code — a five-minute question up front is cheaper than
a PR built on a wrong assumption about the write funnel or the bundle
boundary.
