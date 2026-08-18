# The safety model

This is the document the product is judged on.

k8boss-admin writes to production clusters. Every other console does too; what
distinguishes this one is not that it is careful in spirit but that the care is
mechanical — one funnel, five ordered steps, and a set of refusals that a
developer would have to work around rather than merely forget.

The standard everything below is measured against:

> **A wrong answer delivered confidently is worse than no answer.** For a
> console that writes, its sibling: *an action reported as done is a claim, and a
> claim about somebody's cluster has to be true.*

The seven controls, in the order a write meets them:

| Control | Refuses | Section |
|---|---|---|
| Read-only mode | Any real write, before the cluster is touched | [1](#1-read-only-mode) |
| Preflight | A write we do not know we are permitted to make | [2](#2-preflight) |
| Dry-run | Confirming a change nobody has seen | [3](#3-dry-run) |
| Diff | A dry-run nobody can read | [4](#4-the-diff) |
| Optimistic concurrency | Overwriting an edit that landed while you were typing | [5](#5-optimistic-concurrency) |
| The drain plan | "Drained" over three pods that did not evict | [6](#6-the-drain-plan) |
| Audit | An action with no record of who attempted it | [7](#7-audit) |

And two gates that sit outside the funnel: [RBAC](#8-two-gates-not-one) and
[the Secret reveal](#9-the-secret-reveal).

---

## 0. One funnel

`backend/app/admin/mutate.py` is the only module in this codebase that calls an
`apply_fn`. Everything that changes a cluster — scale, restart, suspend,
rollback, cordon, drain, create, replace, delete — goes through
`mutate(verb=…, group=…, apply_fn=…)`.

The order inside it is fixed:

```
1. mutations gate   ADMIN_ALLOW_MUTATIONS  →  403 mutations_disabled (audited)
2. preflight        SelfSubjectAccessReview →  403 rbac_denied       (audited)
3. apply            the caller's closure, told dry_run
4. diff             live vs. the API server's own projection
5. audit            every terminal state, including the failures
```

**Why one function rather than a convention.** Four promises followed by twelve
endpoints is four promises followed by nine of them after the next refactor —
and the three that stopped are indistinguishable from outside: same response
shape, same status code, no error, no failing test. The first evidence is a hole
in the audit trail, found by someone trying to establish who scaled the payments
service to zero. Here, an endpoint that skipped a step would have to be written
to bypass this module in a way that shows up in an import list.

**`applied` means the cluster changed.** It is `true` only when `dryRun` was
false *and* the call succeeded. A successful dry run returns a full projected
object, a `resourceVersion` and a diff — everything that looks like success —
and `applied: false`. A UI that treated any of the rest as evidence of a change
would report writes that never happened.

---

## 1. Read-only mode

`ADMIN_ALLOW_MUTATIONS` (env, default **false**) gates every write. When false:

* writes return `403 mutations_disabled` **before the cluster is touched**;
* `GET /api/health` reports `"mutations": "disabled"`, and the UI renders a
  global read-only banner and disables write affordances;
* the attempt is still audited, as `denied`;
* **dry-run stays permitted.**

*The failure it prevents.* A console deployed without anyone deciding what it may
do can write to every cluster registered in it. Defaults get inherited by Helm
values files, copied between environments and forgotten; "safe unless configured
otherwise" is the only default that survives that. Turning writes on is an act
with a line in a file; leaving them off is not an act at all.

*Why it is its own error code and not `rbac_denied`.* The operator's permissions
are irrelevant here — the *deployment* is read-only. Telling someone they lack a
grant they actually hold sends them to fix the wrong system, and they will fix it
by widening a ClusterRole.

*Why dry-run survives it.* Projecting what a write would do is a read. Keeping it
available is what makes the console useful in an audit posture: you can answer
"what would this change" without holding permission to change anything. This is
also the mode most incident reviews want.

> **Dry-run is not a lesser RBAC permission.** The API server requires the same
> verb to project a write as to perform one. A console that only ever previews
> still needs `patch` on `apps/deployments`. See `deploy/rbac.yaml`.

---

## 2. Preflight

Before any write, the backend issues a `SelfSubjectAccessReview` for the exact
`verb` / `group` / `resource` / `namespace` / `name` / `subresource`. A denial is
`403 rbac_denied` whose `context` names the target and whose `hint` names the
grant that would fix it. The same primitive backs `GET|POST /api/access/preflight`,
which the UI uses to decide which buttons to disable **and why**.

*The failure it prevents.* Relaying the API server's bare "forbidden" to an
operator who just clicked Scale. A single console action can touch four
resources; "forbidden" does not say which one, and the operator's next move is to
guess, or to grant something broad enough to stop the guessing.

### The distinction this module exists to preserve

A review answers with three fields, and two of them can both be falsy:

```
allowed=false, evaluationError=null   →  a clean denial. You may not.
allowed=false, evaluationError="..."  →  the review itself failed.
                                         We do not know whether you may.
```

Collapsing the second into the first tells an operator they are missing a
permission they may well hold, and sends them to edit a ClusterRole that is
already correct. So the two stay apart in the result dict (`evaluationError` is a
first-class field), in what `require()` raises (`rbac_denied` versus
`upstream_error`), and therefore in the HTTP status — 403 versus 502. The UI must
render them differently.

*Nothing is cached.* A cached `allowed` outlives the RBAC change that revoked it,
and a console that says "you may" because it asked five minutes ago is answering
a question about the past with the confidence of the present. One review is one
round trip against an endpoint built for exactly this.

*Why preflight also runs for dry runs.* See the box in §1: the API server needs
the same permission either way. Finding out at the confirm step, after the
operator has read a diff and decided, is strictly worse than finding out at the
preview step.

*Why it runs at cluster registration too.* `POST /api/clusters/{id}/test`
preflights a baseline of seventeen checks (§9) so a half-permissioned
ServiceAccount is visible while the operator is still looking at the form —
rather than at 03:00, mid-incident, on the one action they needed.

---

## 3. Dry-run

Every write body accepts `dryRun`, defaulting to **true**. With `dryRun: true`
the backend sends `dryRun=All` to the API server and returns the server's own
projected object plus a unified diff.

Two properties make this worth trusting:

**It is the API server's projection, not ours.** `dryRun=All` runs the whole
admission chain — mutating webhooks, defaulting, validation, quota — and returns
the object that *would* have been persisted. A client-side simulation would miss
every webhook in the cluster, and webhooks are exactly the thing that surprises
people.

**It is the same code path as the real write.** `dryRun=All` is a query parameter
on the same URL, with the same body and content type. There is no separate
preview branch to drift out of sync with the write it is previewing. A dry run
that took a different route through `apply.py` would eventually be previewing
something else.

*The failure it prevents.* Confirming a change nobody has seen. A `kubectl apply`
of an edited manifest can delete a field the editor never looked at; a rollback
can restore a pod template missing a container the current one has. Both are
invisible in the request and obvious in the projection.

---

## 4. The diff

The projection is only useful if it is read, so both sides are normalised before
`difflib` sees them, and the normalisation is deliberately conservative: it
removes only fields **no operator ever authors**, and it removes them from *both*
sides, so nothing that is genuinely changing can be hidden by it.

Dropped: `metadata.managedFields`, `resourceVersion`, `generation`, `uid`,
`creationTimestamp`, `selfLink`, and the
`kubectl.kubernetes.io/last-applied-configuration` annotation. `status` is
dropped only when one side has it and the other does not — that means one side is
a hand-written manifest, and "your edit deletes the entire status block" is a
claim about an outcome that is not going to happen. When one side is `null` (a
create or a delete) `status` stays, because a delete's `diff.before` is the whole
live object and the whole of it is what disappears.

*The failure it prevents.* A live Deployment carries a `managedFields` block that
is routinely two thirds of the object, a `resourceVersion` that changes on every
write anywhere in the cluster, and a `generation` the API server increments
itself. Diff two of those verbatim and a one-line replica change arrives inside
two hundred lines of server bookkeeping. Operators respond to that by scrolling
to the bottom and clicking Confirm — which is the same as having no diff, except
that the console now claims to have shown one.

Keeping `resourceVersion` would also mean every diff is non-empty, so
`diff.changed` would never be `false` and the "nothing would change" case would
be unreachable.

**`diff.changed: false` is a first-class outcome.** The UI offers "nothing would
change" instead of a confirm button. A no-op write that reports success is a
small lie that teaches people the console's success messages are decorative.

---

## 5. Optimistic concurrency

`PUT` carries the `resourceVersion` the user was looking at. A mismatch is
`409 conflict` with `context.currentResourceVersion` and a **fresh diff against
live**, never a blind overwrite.

It is enforced twice, on purpose:

* **Locally**, against the object just read — which is what produces the fresh
  diff, so the editor can show what moved underneath instead of asking the
  operator to retype their edit.
* **By the API server**, because the submitted object carries the *caller's*
  `resourceVersion`, not the one we just read. The window between our read and
  our write is small, and a check that only closes it locally is a check that
  loses the race it exists to detect.

The conflict is raised **inside** the apply closure, so the funnel audits it as
an attempted write with outcome `conflict`. "Someone tried to save a stale edit
to prod" is a fact the trail should hold.

*The failure it prevents.* Two operators on the same Deployment during an
incident. Without this, the second save silently reverts the first — including,
in the case that motivated it, a rollback that undid someone else's emergency
image pin, with no record that two changes had ever been in flight.

A `PUT` also refuses to rename: if the URL names `checkout` and the document
names `checkout-v2`, that is `422 invalid`. A replace cannot rename an object,
and the API server's own answer to the attempt would be a schema error about a
field nobody wrote.

---

## 6. The drain plan

Drain is the only multi-step action in the console, and the one whose failure
mode is somebody's production traffic. It is built around three refusals.

**A plan before an action.** Every pod on the node is classified — `evict`,
`skip` or `blocked` — and the classification is returned whether or not anything
is executed. An operator confirming a drain is confirming a list, not a verb.

**A refusal that explains itself.** `blocked > 0` with `force: false` stops the
drain **before the node is even cordoned**, returning `422 invalid` with the plan
attached. The operator sees exactly which pods to resolve and why, rather than a
drain that half-ran and left them to work out where it stopped.

**Per-pod results, and no aggregate "success".** Evictions are reported
individually. A drain over three pods the API server refused returns
`applied: true` with `failed: 3` and `drained: false` — never a bare success.
"Drained" over three stuck pods is the sentence that gets a machine terminated
with a database still on it.

*What `force` is, and is not.* `force` means "I have read this plan and I accept
it": it lets execution proceed past the blocked entries. It does **not** give the
console power it does not have. A PodDisruptionBudget is enforced by the API
server on the eviction subresource, and a pod it refuses is refused with `force`
too — reported per-pod as a failure. Claiming otherwise would sell the operator a
bigger hammer than exists.

The console evicts (`pods/eviction`); it never deletes a pod to drain a node.
Delete ignores PodDisruptionBudgets. Drain additionally preflights `create` on
`core/pods/eviction` from inside the apply step, because permission to cordon a
node is not permission to evict what is running on it, and finding that out three
pods in is not a discovery anyone wants to make.

---

## 7. Audit

`record()` is called by the funnel on **every terminal state of every write** —
`applied`, `dry_run`, `denied`, `failed`, `conflict` — and directly by the two
privileged *reads* that are treated as writes (revealing a Secret, opening an
exec session; exec is recorded on open and on close).

A row is: actor, source IP, cluster, verb, target, `dry_run`, outcome, a human
`detail` ("replicas 3 -> 5"), the `diff_digest`, and the error when there was one.

Three properties:

**Append-only, enforced in code.** A SQLAlchemy session hook refuses to flush an
update or a delete of an `AuditRecord`. There is no delete endpoint. An audit
trail that can be edited answers a different question from the one operators
believe they are asking it.

**Assembled from context, not from arguments.** Actor, source IP and cluster come
from the request context, not from the caller's parameters. A caller that has to
pass them is a caller that can pass the wrong ones, and a row attributing a write
to the wrong operator is worse than one attributing it to `anonymous`.

**`target` is filtered to an allowlist.** It is caller-supplied, stored verbatim
and echoed back in §10. An allowlist means a future call site cannot put a token,
a request body or a Secret value into a table that is kept for a long time and
read by more people than can read the cluster it describes.

**Failures are audited before they propagate.** The question after an incident is
"who tried", and a trail holding only the writes that worked cannot answer it.
Conversely, a failed INSERT never fails the write: by the time the recorder runs
on a real write, the cluster has already changed, and turning a logging problem
into a 500 would tell the operator their action failed when it did not.

*Who the actor is.* With `AUTH_ENABLED=true`, the actor is the verified local or
LDAP session username; `X-K8Boss-User` is ignored and cannot spoof it. With auth
disabled, the console remains in legacy proxy mode and the header is advisory,
defaulting to `anonymous`. `main.py` emits the stronger startup warning when
mutations are enabled in that mode because the difference has to be visible in
the logs of the pod where it is happening.

---

## 8. Two gates, not one

RBAC and `ADMIN_ALLOW_MUTATIONS` are independent, and a deployment can sit in any
of the four combinations:

| RBAC write grants | `ADMIN_ALLOW_MUTATIONS` | Behaviour |
|---|---|---|
| no | false | Read-only console. Buttons disabled with the reason. **The default install.** |
| no | true | Every confirm returns `403 rbac_denied` naming the exact missing grant |
| yes | false | Preflights and dry-runs succeed; confirming returns `403 mutations_disabled` |
| yes | true | Writes reach the cluster |

The first three are all diagnosable states with a clear message. What is *not*
diagnosable is a console that writes because a default did it quietly — which is
why `deploy/rbac.yaml` ships the writer ClusterRole defined but its
ClusterRoleBinding commented out, and `deploy/deployment.yaml` ships
`ADMIN_ALLOW_MUTATIONS: "false"`. Enabling writes takes two edits in two files by
someone who read both.

Per-permission degradation is catalogued in [`rbac.md`](rbac.md).

---

## 9. The Secret reveal

A Secret's values are returned by exactly one code path, and only when **all** of
the following hold:

1. the request is the single-object read with `?reveal=true`;
2. `SECRET_REVEAL_ENABLED` is on — a gate separate from
   `ADMIN_ALLOW_MUTATIONS`, because reading a Secret is a privileged act with a
   different blast radius from writing a Deployment, and an operator may
   reasonably want one without the other;
3. a preflight on `get secrets` for that exact namespace and name passes;
4. an audit record is written.

List responses **never** contain values: `secret_row` reads key names and byte
lengths and never touches a value. `redact_secret` is what the single-object read
returns when the gate is not satisfied.

*The failure it prevents.* A Secrets page that renders a table by fetching the
listing and hiding the values in CSS. The bytes are then in the browser, in the
network tab, and in any logging proxy between the two — and nobody who looked at
the page believes they read a secret.

---

## 10. What this model does not claim

Being honest about the edges is part of the model:

* **Authentication is opt-in.** With `AUTH_ENABLED=false`, anyone who can reach
  the port has whatever the console can do and the audit actor is advisory. Keep
  the port private or put an authenticating proxy in front. Built-in local/LDAP
  auth removes that limitation, but does not replace Kubernetes preflight or the
  deployment-wide mutation gate.
* **There is no undo.** This is the reason the whole flow is dry-run-first rather
  than optimistic-with-rollback; see [`adr-0001-dry-run-first.md`](adr-0001-dry-run-first.md).
  A deleted StatefulSet's PersistentVolumeClaims are not recreated by any button
  in this console.
* **A dry run is a projection, not a promise.** It runs against admission as it
  exists at that moment. A webhook that changes, a quota that fills, or a node
  that fills between the preview and the confirm can still make the real write
  behave differently. The window is seconds; it is not zero.
* **`force` on a drain does not defeat a PodDisruptionBudget.** See §6.
* **Exec bypasses everything here.** A shell inside a container can do whatever
  that container's own ServiceAccount can, and no diff is possible for a
  keystroke. It is gated on `ADMIN_ALLOW_MUTATIONS` *and* a preflight on
  `create pods/exec`, and the session is audited on open and close — but within
  the session, the console is a terminal and nothing more. Withholding
  `pods/exec` is the only real control.
