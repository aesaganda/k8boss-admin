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

*Who the actor is.* With `AUTH_ENABLED=true`, the actor is the verified local,
LDAP or single sign-on session username; `X-K8Boss-User` is ignored and cannot
spoof it. With auth disabled, the console remains in legacy proxy mode and the
header is advisory, defaulting to `anonymous`. `main.py` emits the stronger
startup warning when mutations are enabled in that mode because the difference
has to be visible in the logs of the pod where it is happening.

One deliberate exception: on a **refused sign-in**, the actor is the username
that was *submitted*. There is no session yet, so nothing verified it, and §10
states plainly that such a row is a claim by the caller rather than an identity.
It is stored anyway because "somebody made forty attempts on this account" is the
question those rows exist to answer, and forty rows reading `anonymous` cannot
answer it.

### 7.1 Tamper evidence

The append-only hook is a real protection against *this codebase* growing a bug.
It is no protection at all against a `psql` session, a restored backup, or
anyone with write access to the volume — which, for a table whose entire value is
that it can be trusted after an incident, is the population that matters.

So every record is hash-chained: `event_hash` is SHA-256 over the record's
immutable content plus the previous record's `event_hash`. That prevents nothing.
What it does is make editing, deleting, inserting or reordering a committed
record **detectable**, and `GET /api/audit/verify` says where the chain broke.

*The failure it prevents.* An operator scales the payments service to zero during
an incident, then edits the row to name somebody else. The append-only guard
cannot see it — the edit never goes through this application. The trail still
reads perfectly, and the review reaches a confident, wrong conclusion about a
person. With the chain, that row and every row after it stop verifying.

**Three verdicts, and the third is the one that matters.** `intact`, `broken`,
and `partial` — the last meaning no break was found *and* the trail contains
records this mechanism cannot speak for. `partial` is never rendered as a pass,
in the API or in the UI.

**Records written before chaining existed are never back-filled.** Back-filling
would compute a hash over whatever those records say *today* and store it as
proof, converting "we do not know whether this was altered" into "this is
verified". That is strictly worse than no chain: it is the wrong answer delivered
with a cryptographic signature attached. They stay unhashed, they are counted,
and the verdict is withheld. See `docs/adr-0003-audit-hash-chain.md`.

**A record that cannot be chained is still written.** When concurrent writers
exhaust the retry budget, the record is stored *unchained* rather than dropped,
and reports itself that way. Losing the link costs the ability to prove one
record was not altered; losing the record costs the knowledge that the action
happened at all, and nothing says it is missing.

*What this does not claim.* The chain proves that records were not altered
**after** they were written. It says nothing about whether what was written was
true — a compromised console writes truthful-looking records and chains them
correctly. It also does not protect the trail's *availability*: anyone who can
edit the table can also drop it, and a dropped table verifies vacuously. Off-box
export (§10.4) is what addresses that, and it is a different mechanism with
different assumptions.

### 7.2 Console records

The trail also holds sign-ins, sign-outs, console user changes, and audit
exports, tagged `category: console`. Same table, same append-only guarantee, same
chain.

Same table because they answer one question — "who did what to this console and
its clusters" — and a review that has to remember there are two places to look is
one that will eventually look in only one. The `category` column exists so the
two can still be separated by a filter, on a real column rather than by parsing
the JSON `target` (the one filter in this schema that would be engine-divergent
between SQLite and PostgreSQL).

Console records carry no cluster, which is why `GET /api/audit` had to grow an
explicit way to ask for them: a listing silently scoped to the selected cluster
cannot contain a single sign-in, so an operator filtering for "who signed in"
would get an empty table with nothing indicating the question was never asked.

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

## 9. The debug container

`kubectl debug`, for the pod an operator most needs to get inside and the one
`exec` is useless against: an image built from `scratch` or `distroless`, with no
shell to exec. Kubernetes' answer is an **ephemeral container** scheduled into
the running pod, and the console attaches one through the same funnel as every
other write — gate, preflight on `patch pods/ephemeralcontainers`, `dryRun=All`,
the API server's own projected diff, confirmation, audit.

Nothing about it is special-cased, and three details are load-bearing.

**The refusal for an old cluster is `unsupported`, and it comes from
discovery.** The API server answers a request for a subresource it does not
serve with **404**, which `from_api_exception` maps — correctly, for every other
caller — to `not_found`. An operator told "not found" about a pod they are
looking at goes hunting for a deletion that never happened. So the question is
asked of the core group's discovery document, where the answer is about the
cluster rather than about the object, and §1.3 makes `unsupported` an ordinary
fact in the UI rather than a red banner.

**Discovery that could not be read is `null`, never `false`.** "This cluster
cannot do this" and "we could not find out whether it can" send an operator to
two different places, and only one of them is a cluster upgrade. Unknown lets the
request proceed and the API server answer — the same posture `resolve_container`
takes when it cannot read the pod it wanted to validate against. It is §0.1's
rule applied to a boolean.

**The patch is a strategic merge, and that is what makes it an append.**
`spec.ephemeralContainers` has `name` as its merge key. An RFC 7386 merge patch
replaces a list wholesale, so attaching a second debug container would delete the
first from the manifest — an operation the API server then refuses, because an
ephemeral container cannot be removed, with an error about the field rather than
about the console's choice of patch type.

*What §0.4 does not have to do here.* There is no `resourceVersion` on this
write and none is needed. The merge key means two operators attaching at the same
moment produce two containers rather than one silently overwriting the other, so
there is no lost update for optimistic concurrency to detect. This is the one
write in the tree where that is true, and it is a property of the merge key, not
an exemption.

*The fact that goes above the form, not in the diff.* **An ephemeral container
cannot be removed.** The Kubernetes API has no verb for deleting one; it lives
until the pod does, and restarting the workload is what removes it. That, rather
than five added lines of YAML, is what the operator is consenting to — so the
console says it in the dialog before the preview, and offers no detach button
anywhere. A control the API server will always refuse is this document's defect
standard applied to a button.

*The blast radius, stated plainly.* The operator chooses the image, and the
container it becomes shares the pod's network namespace, its volumes and — on
request — the process namespace of an application container. It runs as the
pod's own ServiceAccount. This is close to `pods/exec` in what it grants, which
is why `pods/ephemeralcontainers` is a separate rule in `deploy/rbac.yaml` and
why the audit sentence names the image: the question after an incident is not
that somebody debugged a pod but *what they put inside it*.

---

## 9.1 The node debug pod

The largest grant in this product, and the one whose safety argument cannot be
made with RBAC.

`kubectl debug node/<name>` creates a pod pinned to one machine with its root
filesystem mounted. What that reaches is worse than the manifest makes it sound.
Under `/host/var/lib/kubelet/pods` sit the projected ServiceAccount token and
every mounted Secret of **every pod on that node** — each one a live bearer
credential; `/host/var/lib/kubelet/pki` holds the node's own client certificate,
an identity in `system:nodes`. `hostPID` adds every process's `environ` and
`cmdline`, and `/proc/<pid>/root` reaches into other containers' mount namespaces
— including tmpfs Secret mounts that never touch the host disk. A shell in this
pod is, for practical purposes, root on the machine.

**RBAC cannot express the difference between this pod and any other.**
`SelfSubjectAccessReview` answers on verb, group, resource, namespace, name and
subresource. It has no field-level granularity whatsoever: `create pods` that
succeeds for an nginx pod succeeds identically for this one. There is no verb for
`hostPath`, none for `hostPID`, none for `privileged`. The layer that once gated
this — PodSecurityPolicy's `use` on a `podsecuritypolicies` resource — was
removed in Kubernetes 1.25, and its replacement, Pod Security admission, is
namespace-label-based rather than RBAC-based.

Three consequences follow, and the design is all three:

**The deployment gate is the real control, so there is one.**
`ADMIN_NODE_DEBUG_ENABLED`, on top of `ADMIN_ALLOW_MUTATIONS`. §8 is about
orthogonality rather than a count of switches, and this is orthogonal in exactly
the way that section means: a deployment can want every other write and not this.
Preflight still runs — the operator may lack `create pods` — but nobody should
mistake it for the boundary. Withholding `create pods` from the console's
ServiceAccount is the only RBAC answer, and it also removes the YAML editor's
ability to create anything at all.

**The dry run is refused too, uniquely.** Everywhere else in this document a
projection is a read and is permitted in read-only mode. Here the projection is a
working manifest for a privileged pod, complete with the namespace that admits
it. A deployment that has switched this off has not consented to handing one out.

**The gate refusal is audited.** The funnel audits its own gate; this refusal
never reaches the funnel, so it records itself. "Who tried to put a host-mounted
pod on a node while that was switched off" is precisely the question §7 exists
to answer.

### What the cluster still decides

Pod Security admission is the control the *cluster* holds, and it is a real one.
A namespace enforcing `baseline` or `restricted` rejects this pod outright — host
namespaces and hostPath volumes both fail those profiles. That is why the
namespace is configurable (`ADMIN_NODE_DEBUG_NAMESPACE`): a cluster that enforces
`restricted` everywhere needs one deliberately labelled namespace for this to be
possible at all, and pinning the console to it keeps the exception in one
auditable place rather than wherever an operator's namespace selector happened to
be. Admission runs on `dryRun=All`, so the refusal arrives at the *preview* step,
before anything exists.

### Least privilege, where it does not cost the feature

Each of these is a deliberate reduction from what `kubectl debug` produces:

| | `kubectl debug node/` | here |
|---|---|---|
| `/host` mount | read-write | **read-only** unless explicitly asked |
| `automountServiceAccountToken` | unset (token mounted) | **false** |
| `hostIPC` | true | **unset** |
| `securityContext` | none (so not privileged) | none |
| hostPath `type` | unset | `Directory` |

The read-only default is the important one. Almost all node debugging reads, and
a read-only `/host` cannot rewrite a static pod manifest or leave a binary that
runs as root at next boot. Asking for write access flips the confirm button to
danger and requires the node's name to be typed.

*What is deliberately not reduced.* `hostPID` and `hostNetwork` stay on: they are
most of why the feature exists, and an operator who needed them and found them
off would turn them on without reading. They are disclosed instead — the create
is a `create`, so `before` is null and the **entire manifest is the diff**, on
screen before the confirming call.

### The pod outlives the session, and no console can fix that

`kubectl debug` has no `--rm`: it issues no delete on detach, on exit or on
Ctrl-C, and leaked node debug pods are common. A console cannot do better
*automatically* — a closed tab is not a signal a server can act on, and a
restarted console pod drops whatever would have issued the DELETE. Promising
cleanup would be a promise that fails exactly when it matters.

So the console does the honest thing instead: it labels the pods it creates,
lists them per node, states for each whether the host filesystem is writable, and
offers a removal that goes through the funnel like any other delete. Removal
stays available even when creating is gated off — a pod left behind after the
switch was thrown is the one that most needs removing, and deleting it takes
privilege away rather than granting it.

This is the exact opposite of §7.4's ephemeral container, which **cannot** be
removed because the API has no verb for it. Two features that look alike and
differ in the one respect an operator most needs to know; the UI renders them
differently for that reason.

---

## 9.2 The CLI pod, and the control this model does not have

§15 creates a pod carrying `kubectl` and opens §7's shell in it. It is in this
chapter because it looks like the two features above, and it is written out
separately because the thing that bounds it is not the thing that bounds them.

**Everything typed in that shell is outside the funnel.** No preflight naming
the permission, no `dryRun=All`, no diff on screen, no `resourceVersion` check,
and no audit record of what changed. §7 records that a session was opened on the
pod, by whom, for how long and how many bytes crossed it; it cannot record the
`kubectl delete` typed into it. Every other control in this document works by
standing between an intention and a cluster. Here there is nothing to stand
between: the operator is talking to the API server directly.

That is the same bargain §7 strikes for a shell in any pod, and it is stated
again because this is the pod whose entire purpose is running cluster commands.
An operator who has learned that this console shows them a diff first will
otherwise assume this surface does too.

**The bound is the ServiceAccount, and RBAC cannot enforce which one.** kubectl
in the pod authenticates as the account the pod binds, so a shell here can do
exactly what that account can do — not what the console can do, and not what the
signed-in operator can do. Kubernetes has no verb covering which ServiceAccount
a pod may bind: `create pods` in a namespace is enough to bind any account in
it, including one more privileged than the caller, and no ClusterRole narrows it
afterwards.

So there is exactly one control, and it is a deployment setting:
`ADMIN_CLI_SERVICE_ACCOUNT`. It defaults to `default`, which holds no
permissions, so kubectl in the pod is refused everything until a cluster admin
deliberately binds a Role. The account is named in the diff the operator
confirms, in the table, and in the audit sentence for the create — because it is
the only fact that decides what this feature can do, and it is not visible from
the pod's name or its image.

**Two gates**, as §8 requires: `ADMIN_ALLOW_MUTATIONS` and `ADMIN_CLI_ENABLED`.
The second exists so a deployment can have every other write in this console
without being usable as a kubectl terminal. Unlike §9.1 the *projection* is
permitted on a read-only console — this manifest is a pod running `sleep`, not a
recipe for a privileged one — while the feature gate refuses the dry run too,
because previewing the feature is offering it.

**What it leaks when it is left behind is a credential.** §9.1's leaked pod
holds a node's filesystem; this one holds a live API token for as long as it
exists. `ADMIN_CLI_MAX_SECONDS` stops the container at a deadline and the pod
object remains, so the bound is on the unattended-shell window and not on the
litter. Removal is a button, for the same reason it is one in §9.1: nothing else
will do it.

---

## 10. The Secret reveal

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

---

## 11. The exposure, and the router that serves it

Two features in one lane, and the safety property is the same in both: **a
change that reaches the outside world is a claim about traffic, not about an
object.**

### 11.1 An object created is not an exposure served

`POST` an Ingress to a cluster with no ingress controller and the API server
answers 201. Nothing routes. The object sits there looking created, `admitted`
stays `null` forever, and the operator's browser hangs on the hostname.

This is the defect standard aimed at §13, so §13 answers it three ways:

* `admitted` is **tri-state**, and `null` — "no router has reported" — is the
  common value rather than an edge case. Every Ingress is `null` permanently:
  the Ingress API has no admission condition at all, and whether a controller
  took the object is visible only through the address it publishes.
* A Route admitted by one shard and refused by another is **admitted with the
  refusal named**. It is being served on one shard; a bare green badge hides the
  half the operator came to find, and a red one sends them to debug a working
  exposure.
* §14 exists so the answer to "nothing is serving this" can be something other
  than a wall.

### 11.2 What a backend cannot express is named, never guessed

An Ingress cannot do passthrough TLS. Two ways to get this wrong, and the
compiler does neither:

* Emit `nginx.ingress.kubernetes.io/ssl-passthrough` and hope it is nginx. It
  works on the controller the author tested against and silently does nothing on
  every other one.
* Quietly write edge termination instead. The operator finds out when their mTLS
  client stops connecting.

Instead the write is refused until the caller acknowledges each dropped feature
**by name** — the same shape as `force` on a drain, and for the same reason:
"I read this and accept it" has to be a separate act from "go". The
acknowledgement is a list rather than a boolean, so editing the form after
acknowledging one consequence requires reading the new one.

The no-vendor-annotation rule is enforced by a test rather than stated as
policy. That is exactly the pressure point where "report it as lossy" gets
reversed into "guess the controller", and a reversal would leave `lossy[]`
claiming a feature was dropped while the object silently carried it. An
annotation the *operator* writes is kept, because they chose it for a controller
they know they are running.

### 11.3 The form cannot eat a field

The YAML document is authoritative and the form is a projection of it. Form
edits patch into the current document; `preserved[]` names every path the form
is not showing; and once the YAML is hand-edited the form locks and the write
sends the document **verbatim, with no form model at all**. Without that last
part the form's fields would be recompiled over the hand-edit at write time —
silently undoing the change the lock existed to protect.

### 11.4 `routes/custom-host` — the denial preflight would otherwise misname

OpenShift gates *choosing a hostname* behind its own RBAC subresource, separate
from creating the Route. The funnel preflights `create routes`, that review
passes, the API server refuses the write — and the operator is told they cannot
create Routes, a permission the review just confirmed they hold. §13 preflights
the subresource explicitly, only for Routes and only when a hostname is actually
set, and audits the denial because it happens outside the funnel.

### 11.5 The router is manifests, not a controller

§14 installs a reverse proxy. What makes that defensible is what it does *not*
do: no reconcile loop in the console, no desired state stored, no watch. Eight
objects, eight passes through `mutate()`, eight audit rows. Status is a live
read.

Four refusals carry the safety of it:

* **Never adopt an object it did not create.** An install that finds a
  ClusterRole of the same name without `managed-by: k8boss-admin` refuses and
  names it — before writing anything, on a dry run as much as on a real one, and
  the refusal is audited. An operator who asked for an install and got a silent
  takeover of another team's object is the worst outcome this feature has.
* **A partial install is reported as partial.** `installed` is false unless all
  eight landed. There is no rollback: deleting what succeeded is more writes
  nobody approved, against objects that may already be in use.
* **Uninstall retains the Namespace.** It can hold objects the console never put
  there, and deleting one is not recoverable.
* **`installed` is tri-state.** `null` during an outage, never `false` — which
  would invite installing a second proxy beside the one already running.

### 11.6 Two gates, and one deliberate departure

`ADMIN_ALLOW_MUTATIONS` and `ADMIN_ROUTER_MANAGE_ENABLED`, both required for a
real write, refusals audited.

**The dry run is permitted with the feature gate off** — unlike §9.1's node
debug pods, where the projection is itself withheld. The difference is what the
projection *is*. A node debug pod's manifest is a working recipe for a
privileged pod on a deployment that switched the feature off. The router's
manifests are a pinned copy of a public upstream bundle, and an operator
deciding whether to open the gate has to be able to read what it would create.
Withholding it would be asking somebody to enable a feature sight unseen.

### 11.7 What §14 does not claim

* **It does not serve HTTPRoutes.** The HAProxy Kubernetes Ingress Controller
  implements Gateway API for TCPRoute only. Enabling the Gateway API option
  grants the permissions and sets the controller name and an HTTPRoute is still
  never accepted. The console says so on the page, before anyone writes one.
* **It does not serve OpenShift Routes**, and should not: that cluster already
  runs its own router, and a second one contending for the same hostnames is how
  an outage starts.
* **`upgradeAvailable` is not "a newer HAProxy exists".** It is "the installed
  version differs from the one this console ships" — the only version claim this
  console can make honestly, since the other would be a statement about a third
  party's release history read out of a string baked into this repo.
* **Preflight cannot see RBAC escalation prevention.** See
  [`adr-0004-shipped-router.md`](adr-0004-shipped-router.md); the console
  rewrites the *hint* on that one failure and leaves the code mapped by status.
* **Nothing here prevents two routers contending.** The install refuses to adopt
  another controller's objects and the default-class option is off, but an
  operator can still run this beside nginx and write an Ingress both could
  claim. `otherClasses[]` is what lets the page say what else is already there —
  and it is `null`, never `[]`, when that listing failed.

---

## 12. The operator portal, and the install this console does not perform

§16 browses the catalogs a cluster already runs and writes **one object**: an OLM
`Subscription`. Everything that happens after that write belongs to Operator
Lifecycle Manager — the cluster's own software, running with its own permissions.
That division is what makes the feature defensible, and §12.5 is where it stops.

### 12.1 `applied: true` is one object, not an operator

A Subscription is a request. `status.installedCSV` is the only evidence anything
was installed, and it stays empty for as long as resolution is pending, an
InstallPlan waits for approval, or the namespace lacks the OperatorGroup OLM
needs. §16 keeps the two apart in every shape it returns: the write response
carries `expectedCSV` — *expected*, never a claim — and the Installed view is
where the question is actually answered. `phase` is `null` rather than `Failed`
whenever the ClusterServiceVersion could not be read, with `phaseDetail` saying
which of "we did not look" and "OLM has not acted yet" it is.

This is §11.1 aimed at somebody else's deployment engine. An exposure written
into a cluster with no controller is an object that routes nothing while looking
created; a Subscription written into a namespace with no OperatorGroup is an
object that installs nothing while looking created.

### 12.2 Three things stop an install, and all three are knowable first

The plan reads them before anything is written:

* **The namespace needs exactly one OperatorGroup.** None gives
  `NoOperatorGroup`, two give `TooManyOperatorGroups`, and either way the CSV
  fails and nothing installs.
* **The OperatorGroup's scope has to be one the operator supports**, or OLM
  answers `UnsupportedOperatorGroup`.
* **`Manual` approval stops the install dead** until somebody approves the
  InstallPlan — which this console does not do.

`target.ready` is a tri-state and `null` is the ordinary third value: an
unreadable OperatorGroup listing, a group that selects its namespaces by a label
selector this console does not evaluate, or a catalog that published no install
modes all leave us unable to say. `true` is returned only when every check
actually ran and passed.

### 12.3 The acknowledgement handshake

Each of those becomes a `consequences[]` entry — `code`, `label`,
`consequence`, `mitigation` — and the write is refused `422 invalid` unless
`acknowledgeConsequences` names **every** code the plan returned;
`context.unacknowledged` lists the missing ones.

The list is *not* called `warnings`, and that is deliberate rather than
cosmetic: §1.5's mutation response already owns a `warnings` key carrying the
API server's own `Warning:` headers for the object being created. Overwriting it
would drop a deprecation notice about the very Subscription being written, which
is the one place an operator would most want to read one.

Named codes rather than a boolean, the same shape as §11.2's lossy exposure and
`force` on a drain, for the same reason: **consenting to a consequence is a
separate act from requesting the change.** A boolean is satisfied by a tick that
predates the consequence — a caller who acknowledged one namespace's consequences,
changed the namespace and posted the old flag would have consented to nothing.
The plan is recomputed inside the write rather than trusted from the caller, so
what is checked is the consequence list that describes *this* write.

The two "unknown" codes are acknowledgeable on the same terms as the rest, on
purpose. `operator_group_unknown` and `subscriptions_unknown` say that a read did
not happen, and letting a failed read pass silently is the swallowed `[]` this
whole document exists to refuse.

### 12.4 Two gates, and the same departure §14 made

`ADMIN_ALLOW_MUTATIONS` and `ADMIN_PORTAL_INSTALL_ENABLED`, both required for a
real write. The refusal is `mutations_disabled`, never `rbac_denied` — the
operator's permissions are not what stopped it, and saying otherwise sends them
to edit a ClusterRole that is already correct — and it is audited by
`app/admin/portal.py` directly, because the funnel that audits everything else is
never reached.

**The dry run is permitted with the feature gate off**, as in §11.6 and unlike
§9.1's node debug pods. The difference is what the projection *is*. A node debug
pod's manifest is a working recipe for a privileged pod on a deployment that
deliberately switched that feature off. The §16 plan is the caller's own request
rendered as a Subscription plus the contents of a catalog the cluster already
publishes to anyone who can read it. Nothing in it is privileged, and an operator
deciding whether to open the gate has to be able to read what it would let the
console create. Withholding it would be asking somebody to enable a feature sight
unseen.

### 12.5 The honest limit: what OLM grants afterwards

This console preflights the write it makes — `create
operators.coreos.com/subscriptions` on that exact namespace and name, like every
other write in this document. **That is the only permission it can speak about.**

What installs the operator is OLM, using OLM's permissions, and what OLM grants
the operator is whatever its bundle asks for: its own ServiceAccount, Roles, and
routinely ClusterRoles over resources this console never named. No
`SelfSubjectAccessReview` can preview that — a review answers about *this*
identity's verb on a resource, and the identity doing the granting is not this
one. Nothing in `deploy/rbac.yaml` bounds it either: withholding every other rule
in that file does not narrow what an installed operator ends up holding.

So the plan shows what can honestly be shown — the channel, the CSV it resolves
to, the install modes it declares and the custom resources it says it owns, which
is the visible half of what installing it does to a cluster — and claims nothing
about the rest. A page that implied it had checked the RBAC would be the defect
standard with a signature attached.
[`adr-0005-operator-portal.md`](adr-0005-operator-portal.md) records the argument
on both sides.

---

## 13. What this model does not claim

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
* **Nothing here bounds an operator OLM installs.** §16 preflights the
  Subscription it writes and can say nothing about the RBAC OLM then grants the
  operator; see §12.5. Nor does §16 uninstall anything — deleting a Subscription
  leaves the ClusterServiceVersion and everything it owns in place, so there is
  no Remove button that would report an uninstall that did not happen. Both
  deletions are ordinary §4 writes, each with its own diff and audit row.
* **Exec bypasses everything here.** A shell inside a container can do whatever
  that container's own ServiceAccount can, and no diff is possible for a
  keystroke. It is gated on `ADMIN_ALLOW_MUTATIONS` *and* a preflight on
  `create pods/exec`, and the session is audited on open and close — but within
  the session, the console is a terminal and nothing more. Withholding
  `pods/exec` is the only real control.
