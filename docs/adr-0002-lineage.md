# ADR-0002: Lineage — what came from K8Boss, what did not, and why the two stay apart

**Status:** accepted
**Date:** 2026-07
**Related:** `scripts/publish-standalone.sh`, [`architecture.md`](architecture.md)

---

## Context

k8boss-admin was written inside the [K8Boss](https://github.com/aesaganda/k8boss)
monorepo, by the same author, in the same house style, and it ships as its own
repository. This is the second such split — the K8Boss operator went the same way
so it could be published on OperatorHub without opening a tree that carries a
commercial licensing module.

Two questions follow from that, and both get asked by everyone who sees the two
repositories: *what did you reuse?* and *why is this not a K8Boss feature?*

## Decision

**k8boss-admin is a separate product with no code dependency on K8Boss.**
Patterns were ported by reading and rewriting; nothing is imported, vendored or
shared. The two repositories can be developed, versioned and licensed
independently, and neither can break the other by refactoring.

`scripts/publish-standalone.sh` performs the split: it copies the tree into a
scratch directory, makes a git repository whose root is `k8boss-admin/`, and
pushes it. **History is deliberately not carried over.** The monorepo history is
K8Boss's; it describes subsystems this repository does not contain, and grafting
it onto a new product would make `git log` a description of something else.

---

## What was carried over

These are patterns, re-implemented. The reasoning came across; the code did not.

**The defect standard itself.** *A wrong answer delivered confidently is worse
than no answer.* K8Boss learned it from a flow-only graph reporting workloads as
unprotected when they had simply never been observed. Here it becomes §0 of the
API contract, `unavailable[]`, `null`-never-`0`, and the `Unknown` workload
status.

**"Say which question you failed to answer."** K8Boss keeps `no_flow_observed`
and `no_flow_provider_available` deliberately separate — one is a statement about
application traffic, the other about our own ability to look. The same split runs
through this console: `unsupported` versus `forbidden` versus `unreachable` in
`unavailable[].reason`; a clean RBAC denial versus a failed access review; a pod
that logged nothing versus a log stream that dropped.

**Isolated degradation.** K8Boss's rule that a failing Mission Control collector
degrades one widget and never blanks the page is `envelope.collect()` here: a
secondary read that fails costs a column, names itself, and leaves the endpoint
at 200.

**The client and error layers.** Per-cluster client construction with encrypted
stored tokens, deadlines on every call, a kubeconfig fallback only when nothing
is registered; and the `AdminError` hierarchy rendered *inside* CORSMiddleware.
Both exist because of the same K8Boss incident: an unreachable cluster raised
through the Kubernetes client, Starlette's `ServerErrorMiddleware` rendered a 500
outside the CORS layer, the browser blocked it, and the operator saw
`TypeError: Failed to fetch` while the real reason was never delivered.

**Generate-once-and-persist for the credential encryption key.** Regenerating per
boot is easy, looks harmless in dev, and presents as every registered cluster
failing to connect after an unrelated restart — which reads as "the console broke
my clusters".

**The frontend shell.** PatternFly 6, React 19, code-split pages, one `fetch`
wrapper whose errors carry a stable code that callers branch on, and a UI
component set (`DataTable`, `PageHeader`, `StatusBadge`, `PartialBanner`) that
was rebuilt to the same shapes.

**CI shape, with one correction.** Tiered jobs, a hermetic route-mocked Playwright
suite, pytest on SQLite. The correction is lint: K8Boss runs its frontend lint
`continue-on-error: true`, and a growing pile of real errors accumulated behind a
green check because nobody reads the output of a job that passed. Here lint
fails the build, and `.github/workflows/ci.yml` says why in a comment.

---

## What was deliberately not carried over

Not "not yet". These are out of scope for what this product is.

| Left in K8Boss | Why it is not here |
|---|---|
| The **Knowledge Graph** — evidence-bearing edges, `valid_from`/`valid_to`, projections, reconciliation | It exists to answer questions *across* time and *across* sources. This console answers "what is true in this cluster right now", reads it live, and caches nothing. Adding a graph would add a second source of truth to a product whose whole value is having one |
| **Network policy and mesh posture** — coverage, attack paths, Cilium/Calico/Istio | A different question about a cluster. This console administers; it does not assess |
| **Runtime process intelligence** (Tetragon), flow providers, detection rules, MITRE ATT&CK coverage | Requires an in-cluster agent. This console requires a kubeconfig-equivalent and nothing else, and that constraint is a feature |
| **GitOps / continuous delivery** — Argo CD verification, change requests, stop levers | K8Boss verifies that other people's delivery tools did what they claimed. This console is the thing an operator uses when they are going around delivery on purpose — and everything it does is dry-run-first and audited for exactly that reason |
| **The commercial licensing module**, entitlement states, the operator | The reason the monorepo cannot simply be opened. Its absence is what lets this repository be Apache-2.0 |
| The **agent** (Go node collectors and coordinator) | Nothing here needs data from inside a node |
| **K8Boss's JWT/session implementation** | Deliberately not copied. k8boss-admin now has its own narrower, opt-in local/LDAP authentication boundary with opaque revocable sessions; legacy proxy mode still uses advisory `X-K8Boss-User`. No K8Boss authentication code or token format crossed the lineage boundary |

There is also one inherited disagreement that this repository does **not** carry:
K8Boss has an unresolved conflict between two confidence orderings
(`engine._CONFIDENCE_ORDER` versus `scoring._CONFIDENCE_WEAKNESS`). It belongs to
the Knowledge Graph, which is not here, so this console has no confidence
vocabulary at all — a fact worth stating so nobody ports one in "for
consistency".

---

## Why they stay separate

**Different questions.** K8Boss answers *is this cluster's posture what we think
it is* — a correlation and evidence problem, inherently historical. k8boss-admin
answers *what is in this cluster and change it safely* — a live-read and
controlled-write problem. A tool that did both would have to cache for the first
and refuse to cache for the second.

**Different blast radius.** K8Boss is predominantly read-only over security data.
This console holds credentials for every cluster it administers and, when
enabled, can delete things. Those two want different deployment stories,
different RBAC, different network placement, and different arguments in a
security review. Merging them would mean K8Boss's read-only installs inherit an
argument about write permissions they do not use.

**Different licensing.** K8Boss carries a commercial module. This console has no
reason to be closed, and Apache-2.0 is only possible because the split is real.

**Different release cadence.** A console gains a resource page; a posture product
gains a provider. Coupling them means every release argues about the other's
risk.

---

## Consequences

* **Duplication is expected and accepted.** Both repositories have a
  `k8s/client.py` that does a similar job. They will drift, and that is the point
  of a split: a fix here does not need K8Boss's release, and a K8Boss refactor
  cannot break this console. A bug fixed in one is worth *reading across*, not
  importing across.
* **No shared library, deliberately.** Extracting one would recreate the coupling
  in a package with two consumers, one release cadence and two licences.
* **Cross-referencing stays at the level of prose.** This repository's docs cite
  K8Boss incidents as the reason a control exists (that is why the comments say
  what failure motivated them); it does not cite K8Boss code.
* **Contributors need only this tree.** No monorepo checkout, no second backend,
  no agent. `docker compose up` and a cluster.
