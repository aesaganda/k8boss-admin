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

And one gate that sits outside the funnel: [the Secret
reveal](#9-the-secret-reveal), which guards a read. [RBAC](#8-two-gates-not-one)
is the other axis, and the per-feature switches are inside the funnel with the
console-wide one.

---

## 0. One funnel

`backend/app/admin/mutate.py` is the only module in this codebase that calls an
`apply_fn`. Everything that changes a cluster — scale, restart, suspend,
rollback, cordon, drain, create, replace, delete — goes through
`mutate(verb=…, group=…, apply_fn=…)`.

The order inside it is fixed:

```
1. gate             ADMIN_ALLOW_MUTATIONS  →  403 mutations_disabled (audited)
                    + the feature's own switch, passed in as a FeatureGate
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
preflights a baseline of nineteen checks (§9) so a half-permissioned
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

### 4.1 One reading of the document, and it is the one that gets sent

The console used to parse every submitted manifest **twice, with two parsers
that did not agree**. The browser used js-yaml, which is YAML 1.2.
`parse_document` uses PyYAML, which is YAML 1.1. For a handful of unquoted
scalars those differ, and the two readings went to two different places:

```
  enabled: off      YAML 1.2: the text "off"     sent: the boolean false
  mode: 0755        YAML 1.2: the number 755     sent: the number 493
  port: 8:30        YAML 1.2: the text "8:30"    sent: the number 510
  limit: 1_000      YAML 1.2: the text "1_000"   sent: the number 1000
```

**The diff was never what this endangered, and saying otherwise would overstate
it.** `diff.after` is the API server's own projection of what it was actually
sent, so the operator confirming a diff was confirming the truth. What was built
on the *browser's* reading is everything local: the form view's controls are
lenses onto it, §11.9's list of fields the form is not showing is computed from
it, and so is every document-only check beside them — including the one written
for precisely this class of mistake, *"a label value that parsed as a number or
a boolean is the one mistake YAML makes on the operator's behalf"*, which could
not see `version: yes` because to js-yaml that is an ordinary string.

**ADR-0009 made it one reading, and the one that survived is PyYAML's** — not
because YAML 1.1 is better, but because it is what was already on the wire, so
adopting it changed what no manifest means. Adopting the other would have turned
`defaultMode: 0755` from 493 into 755 on every manifest this console had ever
accepted, silently, and away from what `kubectl` sends for the same line. The
browser now reads *and re-serialises* through a schema mirroring PyYAML's scalar
resolvers, so a form control, a list of unrepresented fields and a local check
all describe the object that is about to be written.

**The warning stayed, and it is now about the document.** `off` is `false` to
this console, to `kubectl` and to YAML 1.1, and the text `"off"` to the YAML 1.2
specification and to the operator's editor. Which of those is right is not this
console's to settle; which one it will send is, and that is what the editor and
the form view report, by path, as a warning that **never blocks**. The document
is legal YAML whichever version reads it, and §3's dry run remains the authority
on whether the cluster wants it. What an operator gets is the chance to quote a
scalar while quoting it is still free.

*Why a parse rather than a pattern over the text.* A pattern cannot tell a plain
`off` from an `off` inside a `|` block, from a quoted `"off"`, from a
continuation line of a multi-line scalar — and a console that warned about the
contents of a ConfigMap holding an nginx config would be teaching operators to
dismiss the warning on the day it was right. Both readings therefore come from a
parser. The only thing transcribed across the language boundary is PyYAML's
three scalar resolvers, and both halves of that transcription are held to the
real parsers, from both sides, over one shared corpus —
`backend/tests/data/yaml_scalar_corpus.json`. A PyYAML release that moves one of
those resolvers fails a test rather than quietly restoring the two-reading bug.

**What is still not fixed is stated rather than left to be found.** Seven scalars
in that corpus reach a cluster differently from this console than from `kubectl` —
`08`, `09`, `0o10`, `8:30`, `1:2:3`, `60:30.5` and `1.0e3` — and the warning
covers every one of them, because YAML 1.2 disagrees with us about each. ADR-0009
lists them with the measurements behind them.

It was nine, and two of them — `y` and `n` — were the ones nothing could see:
both parsers here called them text, so there was nothing to compare, while
`kubectl` sent `true` and `false`. ADR-0010 closed those two by *sending* what
`kubectl` sends, which is the one change of reading this console has made on
purpose: PyYAML registers its boolean resolver under `y`, `Y`, `n` and `N` and
then excludes them from the pattern, so the gap was a type half-implemented
rather than a dialect chosen. **There is no longer a difference this console
cannot see** — which matters more than the count.

**One reading can still produce a value that cannot be sent.** `.inf`, `-.Inf`
and `.nan` are YAML numbers with no JSON spelling, and `parse_document` refuses
them by path rather than letting the API server reject the body with a syntax
error at a character offset.

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

**And the audit row says the same thing.** A drain is one funnel call performing
many writes: the cordon patch, which the funnel preflights and diffs, and then
one eviction per pod inside its `apply_fn`. So the row's outcome is `applied` the
moment the cordon lands, whatever the evictions did — the response was honest and
for a while the record that outlives it was not. The sentence is now resolved
when the row is written rather than before the call, and carries the counts:
`drain node-5: evicted 12, 3 refused, node NOT drained`. A refusal before the
evictions ran — the gate, the preflight, the blocked-pods `422` — says only what
was attempted, because "evicted 0" over a drain that never started would be a
worse claim than the fixed sentence it replaced.

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

*And there is one path through this console that is not an eviction.* A
`NoExecute` taint written by §24 removes pods through the taint manager, which
**deletes** them — no eviction subresource, so no budget. It is two clicks from
this dialog and it is the exception to everything in the paragraph above, so it
is stated there as an acknowledgement the operator ticks rather than as a note
beside a form. See "Taints and labels" below.

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
diagnosable is a console that writes because a default did it quietly. Enabling
writes is therefore two edits in two files — the writer `ClusterRoleBinding` in
`deploy/rbac.yaml` and `ADMIN_ALLOW_MUTATIONS` in `deploy/deployment.yaml` — and
`ADMIN_ALLOW_MUTATIONS=false` alone stops every write immediately without
touching RBAC.

**The manifests in this repository have both edits made.** They ship the writer
binding applied, `ADMIN_ALLOW_MUTATIONS: "true"`, and — deliberately, on request
— a wildcard `apiGroups: ["*"] / resources: ["*"]` write rule in the writer role.
`kubectl apply -f rbac.yaml` therefore produces a console that can write to every
cluster it is registered against, unrestricted. Both files say so in their own
headers, in those words: it is cluster-admin wearing a different name, it
includes write over RBAC itself, and it makes the console's ServiceAccount a
privilege-escalation path for anyone who can reach its port. That is what makes
`AUTH_ENABLED` load-bearing rather than optional on this deployment, and it is
why the header enumerates exactly what is live — a comment that misdescribes its
own YAML sends an operator hunting for a binding that was never there. A
deployment that wants the bounded console deletes the wildcard rule and keeps the
typed verbs the features below actually need; `rbac.md` is what each of those
costs.

Per-permission degradation is catalogued in [`rbac.md`](rbac.md).

**Four features add a third switch, and it is the funnel that enforces it.**
`ADMIN_NODE_DEBUG_ENABLED` (§9.1), `ADMIN_CLI_ENABLED` (§10),
`ADMIN_ROUTER_MANAGE_ENABLED` (§11.6) and `ADMIN_PORTAL_INSTALL_ENABLED` (§12)
each let a deployment withhold one action while every other write stays
available. Each is passed to `mutate()` as part of a `FeatureGate`: an ordered
list of switches, console-wide one first, each carrying the sentence it produces
and whether it withholds the dry run as well as the write. A refusal names the
**first** closed switch, because "writes are off" is a different conversation
from "writes are on and this one thing is not", and the two send an operator to
two different lines of the same file.

That it is the funnel rather than the feature is the point. Each of the four,
and §17's project, once carried its own copy of step one — build the error, write
the denial row, log, raise — and the copies had already drifted in wording, in
what they recorded and in what they withheld. A copy that stopped writing its row
would look identical from outside: same code, same status, no failing test. The
first evidence would have been a hole in the trail, found by somebody asking who
tried to install a router on a console where that was switched off. There is one
constructor of `mutations_disabled` in `app/admin/` now, and a test asserts
that.

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
ServiceAccount is the only RBAC answer, and it removes the console's ability to
create a pod anywhere — the YAML editor, the masthead's `+`, and the
`Create Pod…` button on the Pods listing alike.

**The dry run is refused too, uniquely.** Everywhere else in this document a
projection is a read and is permitted in read-only mode. Here the projection is a
working manifest for a privileged pod, complete with the namespace that admits
it. A deployment that has switched this off has not consented to handing one out.

**The gate refusal is audited**, by the funnel, against the pod the write would
have created. "Who tried to put a host-mounted pod on a node while that was
switched off" is precisely the question §7 exists to answer, and it is answered
by the same step that records every other refusal rather than by a copy of it
this feature maintains.

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

**Two gates**, as §8 requires: `ADMIN_ALLOW_MUTATIONS` and `ADMIN_CLI_ENABLED`,
handed to the funnel together. The second exists so a deployment can have every
other write in this console without being usable as a kubectl terminal. The two
differ on the dry run and each says so on itself: unlike §9.1 the *projection* is
permitted on a read-only console — this manifest is a pod running `sleep`, not a
recipe for a privileged one — while the feature switch withholds the dry run too,
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

**The mutation diff is the other channel a value could leave by, and it never
carries one.** Every write of a Secret passes the live object through the funnel
as the left side of its diff — a delete, a replace, the fresh diff a 409 carries
— and the live object holds every value. Until this was closed, a *dry-run*
delete of a Secret returned the whole object in `diff.before`: permitted in
read-only mode, preflighted on `delete` rather than `get`, audited as an
ordinary dry run, and gated by nothing above. So `build_diff` redacts a Secret's
`data` on both sides before it diffs, into `<redacted, N bytes>` placeholders
that keep the key and the decoded size and carry `changed` on the proposed side
when the value differs from the live one. The marker is what keeps `changed`
honest — a same-length password rotation would otherwise redact to identical
text on both sides and be reported as a write that changes nothing, which is a
confirm dialog with no Confirm. `stringData` in a submitted manifest is redacted
by plaintext length, the last-applied annotation is dropped as it is on every
diff, and the audit digest is computed over the redacted text, so the trail is
not a place a value can be recovered from either. Keyed on the object's kind
inside `build_diff` rather than on a flag a caller passes, so no future write
path has to remember it.

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

**The create dialog's form view holds the same invariant, and holds it with
nothing on the wire to prove it.** §11.9 of the contract states it generally, and
the create dialog is the second implementation: every control is a lens into the
parsed object, a form edit is one path write that copies the rest of the document
through, and the list of paths the form does not show is *computed* from the
document and the model rather than written down — so it cannot go stale, and a
control removed from a model puts its path straight back on screen.

One input to that list is not derived: a container's coverage is a table in
`objectFormModel.js` describing a renderer in `ObjectForm.jsx`, and a control deleted
without its pattern would move that field silently into the set the form hides
while saying it hides nothing — the one way this list can lie. The two are
checked against each other by a test, which is the same answer this document
gives above for the vendor-annotation rule: enforced rather than asserted.

Two differences from the Route form matter to anyone reviewing it. There is **no
render endpoint**: an exposure is compiled server-side because one form field
there can mean three different objects, whereas a create is one object whose
fields are already in front of us, so the patching and the "what I am not
showing" list are both computed in the browser. Nothing in the mutation response
attests to either, which means **only a test can** —
`frontend/tests/e2e/create-form-view.spec.js` is where "a field the form does not
show survives an edit made in the form" is actually established, the same way the
no-vendor-annotation rule above is enforced by a test rather than stated as
policy. And there is **no hand-edit lock**, because there is nothing to lock: the
two views edit one string of text, the form is rebuilt from it every time it is
opened, and the write sends that text whichever view produced it.

What survives a form edit is the *object*, and three things in a manifest live in
the text rather than in the object: comments, which nothing carries across a
parse; and anchors and merge keys, which the parser resolves, so a rewrite spells
out what they stood for. The first is a loss and the other two are an expansion,
and the dialog says which is which, counted, before the first form edit — the
only moment any of it is still actionable. Calling an expanded anchor "dropped"
would be its own wrong answer.

### 11.4 `routes/custom-host` — the denial preflight would otherwise misname

OpenShift gates *choosing a hostname* behind its own RBAC subresource, separate
from creating the Route. The funnel preflights `create routes`, that review
passes, the API server refuses the write — and the operator is told they cannot
create Routes, a permission the review just confirmed they hold. §13 names the
subresource explicitly, only for Routes and only when a hostname is actually set.

**The funnel reviews it, and the ordering is the point.** §13 decides whether
the grant applies; `mutate()` checks it, in step 2, after step 1. This once ran
before the funnel, which put it ahead of the mutations gate: on a read-only
console a Route with a hostname was refused `rbac_denied`, sending somebody to
edit a ClusterRole when the deployment could not write at all. §1 exists to keep
those two answers apart, and a check that runs before it undoes that for one
endpoint. The denial is audited against the subresource that was refused, not
the object — the same misattribution, in the trail, is still a
misattribution.

### 11.5 The router is manifests, not a controller

§14 installs a reverse proxy. What makes that defensible is what it does *not*
do: no reconcile loop in the console, no desired state stored, no watch. Eight
objects, eight passes through `mutate()`, eight audit rows. Status is a live
read.

Four refusals carry the safety of it:

* **Never adopt an object it did not create.** An install that finds a
  ClusterRole of the same name without `managed-by: k8boss-admin` refuses —
  before writing anything, on a dry run as much as on a real one, and the
  refusal is audited. An operator who asked for an install and got a silent
  takeover of another team's object is the worst outcome this feature has. The
  refusal names **every** conflicting object in `context.conflicts[]`, not the
  first one: the scan has already read all eight by the time it can decide, so
  stopping at one costs a round trip per conflict — delete, install, get refused
  on the next, delete, install — where each refusal reads as a fresh failure
  rather than as the second of three, and nothing says how many remain. One
  audit row per object too, because the row is looked for by the object it was
  about.
* **A partial install is reported as partial.** `installed` is false unless all
  eight landed. There is no rollback: deleting what succeeded is more writes
  nobody approved, against objects that may already be in use.
* **Uninstall retains the Namespace.** It can hold objects the console never put
  there, and deleting one is not recoverable.
* **`installed` is tri-state.** `null` during an outage, never `false` — which
  would invite installing a second proxy beside the one already running.

**Its dry run says whose diff each object carries.** The API server refuses a
create into a namespace that does not exist, `dryRun=All` included, so a fresh
install's dry run cannot project the four objects inside the router's namespace
— and the first version of this install asked it to anyway, and reported four
`not_found` failures on every real cluster while the fake in the tests answered
happily. Now the cluster-scoped objects are projected by the API server, the
four inside the namespace are reported as the bundle's own manifest with
`projection: "rendered"` and a preflight of the `create` the real install will
need, and the panel shows every object's diff labelled with whose it is —
something it did not do at all before: the funnel produced eight diffs and the
panel showed them to nobody. A preflight denial on a rendered object blocks
Confirm with the grant, because creating the Namespace and then failing inside
it is a half-install for a refusal that was knowable up front. §13.3 is the same
rule for a project.

### 11.6 Two gates, and one deliberate departure

`ADMIN_ALLOW_MUTATIONS` and `ADMIN_ROUTER_MANAGE_ENABLED`, both required for a
real write, refusals audited.

**The dry run is permitted with the feature gate off** — unlike §9.1's node
debug pods, where the projection is itself withheld. The difference is what the
projection *is*. A node debug pod's manifest is a working recipe for a
privileged pod on a deployment that switched the feature off. The router's
manifests are a pinned copy of a public upstream bundle, and an operator
deciding whether to open the gate has to be able to read what it would create.
Neither of this feature's switches is marked as withholding the preview, which
is where that decision lives now — §8's list rather than this paragraph is what
a reviewer compares against the other four.

**The refusal happens before the eight objects are read**, which is the funnel's
step one called early rather than a second gate: install reports every object it
touched, so a refusal that waited for the first `mutate()` would be eight denial
rows and eight failed objects for one attempt. The row it writes and the error it
raises are the ones the funnel would have produced.
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
to edit a ClusterRole that is already correct — and it is audited by the funnel's
step one, called before the catalog is read so that a caller whose deployment
forbids this gets `mutations_disabled` rather than a 404 about a package they
were never going to be allowed to subscribe to.

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

## 13. The project, and the five objects it is

`oc new-project` on OpenShift does not create a namespace. It instantiates a
project request template — a Namespace, a ResourceQuota, a LimitRange, a
RoleBinding for the requester and, on most production clusters, a NetworkPolicy
— so that a team never receives a bare namespace with the quota arriving a week
later, after the incident. Vanilla Kubernetes has all five objects and no act
that produces them together. §17 of the contract is that act, and every
property of this model applies to it five times over rather than once.

### 13.1 Five ordinary writes, not a template engine

Each object goes through `app.admin.apply.create_from_yaml`, and so through the
funnel: its own preflight, its own dry run, its own diff, its own audit row.
There is no template stored in the console, no reconcile loop, and no label
claiming the namespace is managed afterwards — the defaults live in the request,
and what keeps the objects there is the cluster. That is what keeps this on the
right side of [`adr-0004-shipped-router.md`](adr-0004-shipped-router.md)'s
boundary: nothing is shipped, nothing is pinned, and every object is one the §4
editor could already have written. [`adr-0006-projects.md`](adr-0006-projects.md)
records the argument.

### 13.2 A namespace is never adopted

A project is created into a namespace that does not exist. One that does is
refused with a 409 naming it — before anything is written, on a dry run as much
as on a real one, and audited, because the funnel is never reached and "did
anyone try to create a project over `kube-system`" is a question the trail
exists to answer. This is stricter than §11.5's managed-by check on purpose: a
namespace is somebody's, and there is no label that makes taking one over
acceptable. A namespace read that did not answer stops a real write outright
rather than becoming "it is not there"; on the plan it becomes `exists: null`
and a consequence, never `false`.

### 13.3 The dry run says whose diff each object carries

The API server's NamespaceLifecycle admission refuses a create into a namespace
that does not exist, `dryRun=All` included. So a project's dry run can project
the Namespace and nothing inside it. §3 promises the API server's own projection,
and §17 keeps that promise where it can and says plainly where it cannot: the
Namespace comes back `projection: "server"`, and the four objects inside it come
back `projection: "rendered"` — the console's manifest diffed against nothing,
with a preflight of the exact verb the real write will use. The preflight is the
one thing about a namespaced object that can be checked before its namespace
exists, and it is the thing most worth knowing first: a denial found *after* the
Namespace was created leaves a half-project behind for a refusal that was
knowable up front, so the dialog blocks Confirm on it with the grant it needs.

The UI labels the two projections differently. A rendered manifest has not been
through admission, and a client that showed it under the sentence "this is the
difference the API server projected" would be making §3's promise on the
console's behalf. §14's router install has exactly this shape on a fresh
namespace and, since the review that produced §17, follows the same rule — see
§11.5.

### 13.4 Consequences are acknowledged by name

The same handshake as §11.3 and §12.3, and the reason is the same: consenting to
a consequence is a separate act from requesting the change. What is different is
how *quiet* the failures are. A ResourceQuota over `requests.cpu` with no
LimitRange supplying a default makes the API server refuse every pod that does
not state its own request — `must specify requests.cpu` — and the operator
finds out from a Deployment that is accepted and never gets a pod. An enforced
Pod Security level does the same for a violating template: the Deployment is
created, the ReplicaSet fails, and the only evidence is an event. A namespace
with no quota is what vanilla Kubernetes gives by default and is exactly the
thing this feature exists to avoid. Each is a code the write refuses without,
recomputed at write time so consent for one request cannot be carried onto
another.

### 13.5 A partial project is reported as partial

`created` is true only when every object landed on a real write. A Namespace
that failed stops the sequence and the objects inside it are reported as
skipped with the reason, because each would fail with a 404 naming the
namespace and add four audit rows saying nothing the first one did not. Any
other failure is counted, named with the grant it needed, and the sequence
continues so the operator learns about every missing grant in one round. There
is no rollback: deleting a namespace the operator just asked for is not a
correction anybody wants made on their behalf, and the response says which
objects exist so they can add the rest through §4 where each is its own diff.

### 13.6 What the read model refuses to guess

The project page is five reads joined, and each one that did not answer is
`null` with the reason rather than an empty section — `quotas: null` is not
"nothing bounds this namespace", which is the sentence that gets a second quota
added over the one nobody could see. A quota's `used` is `null` until the
controller has written status, never `0`, which is what somebody about to scale
wants to hear. And a namespace declaring no Pod Security label is reported as
declaring nothing, never as `privileged`: the cluster-wide default lives in the
API server's `AdmissionConfiguration` file, which no API serves, so the console
cannot know what applies and says so instead of picking the reassuring answer.

### 13.7 Setting the level, with the pods that would violate it

§18 is the one write in this document whose *preview* is the feature. Six labels
on one namespace, one merge patch, the ordinary funnel — and the reason it exists
rather than being left to §4's YAML editor is what the API server says back.

**Pod Security admission evaluates the pods already in the namespace when the
labels change, and reports what it finds as `Warning:` headers — on `dryRun=All`
exactly as on a real write.** So the preview of "enforce restricted" carries the
API server's own list of the workloads in that namespace that do not meet it, by
name and by the field that fails, before anything is confirmed. This console does
not compute that list, and does not parse it: it is rendered verbatim, one line
per header, because a summary of somebody else's admission decision is a summary
that can be wrong about which pods are affected.

**The thing that must be said out loud is that nothing is evicted.** Admission
runs when a pod is *created*. Raising the enforce level does not stop, restart or
reschedule anything already running: a workload whose pods violate the new level
keeps the pods it has and fails to make more, so it stays Available until
something replaces a pod and then sits below its replica count with a
`FailedCreate` event and no pod to inspect. An operator who reads
`enforce: restricted` as "the workloads in front of me are now restricted" is
wrong about their own cluster, which is this document's defect standard exactly.
So it is a consequence acknowledged by name (`psa_does_not_evict`), not a
paragraph in a doc.

**Removing a label is not setting `privileged`, and the request can say which.**
`null` removes the declaration and hands the namespace back to the cluster's own
Pod Security default — which lives in a file on the API server that no API
serves. The console cannot say what will apply afterwards, and
`psa_enforcement_removed` says that rather than implying everything is admitted.

**One acknowledgement here is the client's, and the response says so.** The
admission warnings only exist on a response, and this console holds no state
between the preview and the confirm, so it cannot refuse a write on the grounds
that nobody read them. The dialog blocks Confirm until an operator ticks a box,
and states plainly that this one is the dialog asking rather than the backend
enforcing. Every *other* acknowledgement on this write is recomputed server-side
against the namespace as it is at write time, and refused if unnamed.

## 14. What this model does not claim

Being honest about the edges is part of the model:

* **Authentication is opt-in.** With `AUTH_ENABLED=false`, anyone who can reach
  the port has whatever the console can do and the audit actor is advisory. Keep
  the port private or put an authenticating proxy in front. Built-in local/LDAP
  auth removes that limitation, but does not replace Kubernetes preflight or the
  deployment-wide mutation gate.
* **The console's users are not cluster identities — unless a cluster has opted
  in.** By default every call to a cluster is made as that cluster's one
  registered credential, so §2's preflight answers about the console rather than
  about the operator reading it — correct about whether the write will succeed,
  correct about the wrong subject — and the cluster's own audit log records the
  ServiceAccount for every write this console makes. Two operators with
  different console roles have identical power *through* that credential.
  [`adr-0007-impersonation.md`](adr-0007-impersonation.md) is **accepted and
  implemented**: §18 is what a cluster with `impersonation_enabled` does
  instead, including why the grant it needs is cluster-admin by proxy and why it
  therefore ships commented out. This bullet describes every cluster that has
  not set the flag, which is the default and, until somebody changes it, all of
  them.
* **Identical power through the credential is not identical power over the
  credential.** With `AUTH_ENABLED=true`, registering a cluster, editing one and
  de-registering one are administrator-only. That is not paperwork: the edit is
  a partial update where an omitted `token` keeps the stored one, so an account
  that can move `api_server` without sending a `token` has pointed this
  console's bearer token at a host it chose and is handed the token on the next
  request. Clearing `impersonation_enabled` is the quiet version — every later
  call to that cluster reverts to the ServiceAccount, and the caller leaves
  their own RBAC behind for the console's. With `AUTH_ENABLED=false` there is no
  console role to check and the proxy in front decides, as it does for every
  other endpoint.
* **There is no undo.** This is the reason the whole flow is dry-run-first rather
  than optimistic-with-rollback; see [`adr-0001-dry-run-first.md`](adr-0001-dry-run-first.md).
  A deleted StatefulSet's PersistentVolumeClaims are not recreated by any button
  in this console.
* **A dry run is a projection, not a promise.** It runs against admission as it
  exists at that moment. A webhook that changes, a quota that fills, or a node
  that fills between the preview and the confirm can still make the real write
  behave differently. The window is seconds; it is not zero.
* **`force` on a drain does not defeat a PodDisruptionBudget.** See §6.
* **A `NoExecute` taint does not honour one.** Not a hole in this console — it
  is how Kubernetes implements taint-based removal, and there is no flag here
  that turns it on or off. See §24.
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
  `pods/exec` is the only real control. On a cluster that impersonates, exec is
  **refused** rather than opened: the WebSocket upgrade carries no impersonation
  headers, so the shell would run as the ServiceAccount while the preflight and
  the trail named the operator, and ADR-0007's fourth condition makes that a
  refusal rather than a fallback.
* **Setting a Pod Security level changes what is admitted next, not what is
  running.** §18 patches six labels; no pod is evicted, restarted or
  rescheduled, and `applied: true` there means the labels changed and nothing
  more. The pods the API server named in its admission warnings keep running
  under a level their spec does not meet until something replaces them.
* **A project is created, not managed.** §17 writes five objects and stops.
  Nothing watches them, nothing restores a quota somebody edits away, and the
  console's user is not a cluster identity — the RoleBinding is for whichever
  subject the operator names, written by the console's own ServiceAccount. What
  a `ClusterRole/admin` binding grants is whatever that aggregated role holds on
  that cluster, which the plan does not enumerate.
* **A NetworkPolicy shown here is a declaration, not an enforcement.**
  NetworkPolicy objects are implemented by the cluster's CNI plugin, and no API
  this console can reach reports whether a given cluster's plugin implements
  them: a cluster running a plugin without NetworkPolicy support accepts, stores
  and serves these objects while forwarding every packet they describe as
  denied. So the console reports what the API server holds and never uses a word
  like *enforced*, *blocked* or *protected*. `ingress.isolated` on the pod
  isolation view means "a policy selecting this pod declares an ingress
  section" — the strongest true statement available, and a much weaker one than
  it looks. Both NetworkPolicy tabs carry that sentence above the table rather
  than in a tooltip.

  The same caution runs through the row itself. A direction nobody governs is
  `rule_count: null` and a direction that denies everything is `rule_count: 0`,
  because the two are one word apart in YAML and opposite in effect; a policy
  whose `podSelector` matches nothing reports `selected_pod_count: 0` and is
  flagged as inert, while a pod listing we could not make reports `null`. See
  §8.3 of [`api-contract.md`](api-contract.md).

---

## 14. Expanding a claim — where "done" is somebody else's word

§20 is the one write in this document whose **success is not the end of the
change**, and everything careful about it follows from that.

One field on one PersistentVolumeClaim, one merge patch, the ordinary funnel.
§4's YAML editor could write the same field. The reason §20 exists is that a
claim that filled up is a live incident, the fix is one number, and every part
of getting that number right is invisible in a text editor.

**`applied: true` means the claim requests the new size, and nothing else.**
Expansion is two acts by two parties. This console patches
`spec.resources.requests.storage`. Then the storage provider grows the volume —
the claim carries `Resizing` while it does — and then the *filesystem* has to be
grown, which on many CSI drivers cannot happen while a pod has the volume
mounted: the claim sits at `FileSystemResizePending` until every pod using it
restarts. `status.capacity` is what a workload actually has, and no write from
this console changes it directly.

That gap is the whole risk. A console that reported a green result and closed
the dialog would leave an operator believing a database has room it does not
have, during the incident where that belief is most expensive. So the response
carries `current.capacity` — read from *before* the write — beside
`requested.size`, the dialog renders both after the write under the heading
"What changed, and what has not", and `pvc_capacity_is_not_immediate` is
acknowledged by name on **every** expansion, including the ones that go on to
work perfectly. This document's defect standard is that a claim about somebody's
cluster has to be true; "the volume is bigger" is not one this console is in a
position to make.

**Expansion is one way, and that is a second acknowledgement rather than the
same one.** No Kubernetes API makes a bound claim smaller, and on a cloud
provider the new size is what you are billed for from the moment the volume
grows. The first consequence is about *when* the change takes effect; this one
is about whether it can be taken back. They are different mistakes, so they are
different checkboxes.

**Three things are refused here rather than relayed from admission.** A claim
that is not `Bound` has no volume to expand. A size smaller than the current
request is a shrink — refused by arithmetic, before anything is sent, because
the API server's own message for it names a field the operator did not think
they were editing, and because a provider that honoured it would be discarding
the data past the new end. And a StorageClass that does not set
`allowVolumeExpansion` refuses the write with the class named, since the
alternative is a rejection the operator has to decode.

**But `allowVolumeExpansion` is tri-state, and only `false` refuses.** A class
that was refused, a class that has been deleted since the claim bound, a claim
that names no class at all — each of those is `null`, meaning *we could not find
out*. Reading `null` as `false` would block a write the cluster would have
accepted and send an operator to argue with a StorageClass that is already
correct: §2's rule about a failed access review, applied to a different read.
`null` becomes a consequence to acknowledge and the API server decides.

**And the pods that mount the volume are `null`, never `[]`, when the listing
failed.** The two answers point in opposite directions. An empty list says the
filesystem can grow without touching a workload; that is the sentence that
starts an offline resize on a volume a database has open. So a refused pod
listing is `null`, `partial` is true, and `pvc_mounts_unknown` says in words
that this console is not claiming nothing is using it.

**The plan answers `200` for a size it will not write.** A blocked size comes
back as `blocked` rather than a `422`, because the plan is the screen where the
size is decided and one that answered a too-small number with an error alone
would withhold the claim's current size, its capacity and its mounts at exactly
the moment those are the three facts needed. It is the same refusal the write
raises, caught rather than restated. The write still refuses.

**What §20 does not do.** It does not restart the pods that an offline resize
needs restarted, does not watch the claim afterwards, and does not report
whether the expansion completed. A live read of the claim answers that, and
`status.capacity` is where the answer is — which is the point of showing it in
the first place.

---

## 15. Taints and labels (§24) — the write that deletes without evicting

Two fields on a node, both writable from §4's YAML editor already. §24 exists
because between them they hold the one path in this console that removes running
pods without going anywhere near the guardrail the rest of the document promises,
and the one path that changes nothing today and breaks a rollout next week.

### 15.1 `NoExecute` deletes, and a budget does not stop it

`NoSchedule` and `PreferNoSchedule` are consulted when the scheduler is
*placing* a pod. Adding one moves nothing. `NoExecute` is evaluated against the
pods already on the node, and the ones that do not tolerate it are removed by the
taint manager in kube-controller-manager. In a form the three are three entries
in one dropdown, and the difference between the first two and the third is a node
that keeps serving versus a node that empties.

The removal is a **delete**. It does not go through `pods/eviction`, which is
where PodDisruptionBudgets are enforced — so a `NoExecute` taint takes an
application below its budget and nothing refuses it.

That is a problem this console partly created. §6 spends three paragraphs
teaching the operator that eviction honours budgets and that `force` will not get
them past one. Both sentences are true, and they are what the same operator has
just read, two clicks away. Offering the taint write beside them without saying
so would have handed them a bigger hammer while the console described a smaller
one. So the plan names every pod the taint removes, and the fact that budgets do
not apply is `taint_deletes_pods` — a consequence acknowledged by name, refused
until it is.

The mitigation the console offers is the honest one: **drain the node instead**
if budgets matter, or use `NoSchedule`, which stops new work arriving and leaves
what is running alone.

### 15.2 The pod that is neither staying nor going

A pod can tolerate a `NoExecute` taint for a bounded time — `tolerationSeconds`
on the matching toleration. It is not in the set that goes now and not in the set
that stays: the node looks entirely healthy for as long as the timer runs and
then empties on its own, after whoever wrote the taint has stopped watching.

So `delay_seconds` is a number rather than a flag: `0` for a pod that goes at
once, the timer for a pod that goes later, and absent from the list entirely for
one that stays. An unbounded toleration wins over any number of bounded ones,
which is what the taint manager does, and getting that backwards would put pods
on a deletion list they are not on.

### 15.3 An unread pod listing is the one case the console cannot answer

Which pods do not tolerate a taint is computed from the node's own pod listing.
When that listing fails, `deleting` is `null` and `pods_checked` is `false` —
never an empty list, which renders as "this taint deletes nothing" over a node
nobody counted. §0.1's corollary, pointed at the most destructive write in this
document.

It is also the only consequence in the codebase that says the console does not
know what the button does, and the operator has to accept that by name before the
write proceeds.

### 15.4 A label change evicts nothing, and that is the trap

Node affinity is `requiredDuringSchedulingIgnoredDuringExecution`. The second
half of that name is load-bearing: the rule is checked when a pod is placed and
never again. Removing a label a running pod's `nodeSelector` requires does not
disturb the pod at all.

An operator who removed a label to move a workload has not moved it. What they
changed is where the scheduler will put it *next*, which is discovered during a
rollout, by somebody else. So the plan names the pods on this node whose
placement rules mention a key that is leaving — keys only, never a verdict, since
evaluating a rule that is not re-evaluated would be a more confident claim than
the API supports — and the consequence says in those words that nothing moves
today.

### 15.5 What the console will not guess about a label

The kubelet re-applies some `kubernetes.io/`-family labels when it next
registers and never re-applies others. Which is which depends on its flags and on
a cloud provider that no API this console reads reports. So `label_reserved_prefix`
says it cannot tell you, rather than picking the reassuring half of that. It
names `topology.kubernetes.io/zone` specifically, because volume topology is
matched against it and a node that has lost it stops being chosen for zonal
volumes.

`node-role.kubernetes.io/*` gets its own consequence in both directions, added
and removed alike: it decides the ROLES column in `kubectl get nodes` and on §5's
node list, which is what every operator reading either believes the machine is
for. It grants nothing and removes nothing — a node-role label is a label — and
saying so is the point, because the name suggests otherwise.

---

## 16. Certificate requests (§25) — the approve button with the least on screen

Approving a CertificateSigningRequest is the fastest way to create an identity on
a Kubernetes cluster, and it is the action whose consequences are least visible
at the moment of taking it. `kubectl get csr` shows who *asked*.
`kubectl certificate approve` takes a name. Neither shows what the request asks
to **become**.

### 16.1 The two fields, and why only one of them is ever on screen

`spec.username` is the submitter. The common name and organizations inside
`spec.request` are the identity the certificate would carry. They are different
fields and they routinely differ for good reasons — a bootstrap token asking for
a kubelet's first certificate is exactly that shape.

They also differ for the bad reason. A request from an ordinary user whose
organization is `system:masters` is a cluster-admin credential: the API server's
authorizer honours that group **before RBAC is consulted**, so no Role bounds it
and no RoleBinding revokes it. The only way back is rotating the signing CA,
which invalidates every certificate it ever issued.

So §25 decodes the PKCS#10 and puts the subject on the screen. That is the whole
feature; the write is two lines.

### 16.2 The decode is total, and a failure is not an empty subject

`spec.request` is bytes somebody else submitted. The decode runs inside a row
shaper, so it must not raise: one malformed request would take down the listing
that shows the other forty.

It must also not succeed quietly. A decode that failed returns every derived
field `null` with `decode_error` set — never an empty subject, which renders as
a certificate that asks for nothing and is the most reassuring possible
description of one nobody could read. On the decision screen that becomes a
consequence the operator accepts by name: *approving means signing a certificate
whose identity nobody here has seen.*

### 16.3 Approved is not Issued, and the console will not merge them

Approving records a condition. A **signer** then has to act, and
`kube-controller-manager` signs only the three `kubernetes.io/…` signerNames —
if it was started with a signing CA, which no API here reports.
`kubernetes.io/legacy-unknown` is deprecated and is not signed by it at all.

A request for any other signerName needs a controller somebody installed. Without
one it sits `Approved` with no certificate, indefinitely, and every screen that
collapsed the two states would report that as a success. So they are two states,
the write says so in its own summary, and a signerName outside the built-in three
is a consequence before the decision rather than a surprise after it.

### 16.4 A decision cannot be taken back

The API server refuses any update that rewrites an existing `Approved` or
`Denied` condition. There is no un-approve — not here, not in `kubectl`.

The console handles that in three places rather than one. A decided request is
`blocked` on the plan, with the decision and its timestamp. It is offered no
action in the listing, because a menu entry that always fails is worse than an
absent one. And the dialog says the decision is final in a banner that is
permanent rather than a checkbox: it is true of every decision, and a tick that
always appears is a tick nobody reads on the one that matters.

### 16.5 Two permissions, and the second is the one people miss

Writing the decision needs `update certificatesigningrequests/approval`. It is
not sufficient. The API server's `CertificateApproval` admission plugin
separately requires the **`approve` verb on `certificates.k8s.io/signers`, named
for this request's signerName**.

A console that preflighted only the first would enable the button, pass its own
check, and be refused by the API server — which is §13's `routes/custom-host`
failure, in a place where the operator's conclusion would be that they cannot
approve certificates at all. Both are preflighted. The signer check happens
inside the apply step, after the gate, so a read-only console answers
`mutations_disabled` instead of sending somebody to edit a ClusterRole that was
never the obstacle.

That permission is also why §25 has no deployment switch of its own. `approve` on
`signers` is granted per signerName, so a cluster can let this console approve
kubelet-serving certificates and nothing else. A boolean on the deployment would
be a coarser copy of a control RBAC already expresses precisely — and the coarser
copy is the one that ends up on. The shipped writer role reflects that: it grants
the two node-lifecycle signers and deliberately not
`kubernetes.io/kube-apiserver-client`, which is the signer a `system:masters`
certificate comes through.

### 16.6 The consequence list is empty for the ordinary case, on purpose

Nearly every CSR on a running cluster is a kubelet renewing its own certificate:
decoded cleanly, for a signer the cluster runs, subject equal to requestor. That
raises nothing, and the operator previews and confirms.

That is a deliberate decision rather than an omission. A checkbox on every
request is a checkbox nobody reads on the one asking for `system:masters` — which
is the request this whole section exists for.

---

## 17. Deleting a namespace (§26) — the diff that shows the wrong thing

Every other section here is about making a preview *honest*. This one is about a
preview that is perfectly honest and shows the wrong object.

§4's delete previews `before=live, after=null`. For a namespace that is a
metadata block disappearing: a name, a couple of labels, a `spec.finalizers` list
with `kubernetes` in it. Nothing on that screen is the database, the address, or
the admission webhook that goes with it. An operator who reads it carefully and
approves it has done everything this model asks of them and is still about to
lose a disk.

`kubectl` is no better. `kubectl delete namespace prod` prints
`namespace "prod" deleted` — a sentence that is also not true at the moment it is
printed, since what has happened is a `deletionTimestamp`.

### 17.1 The four things, and why they are on screen rather than in a warning

A generic "this deletes everything in the namespace" banner is true and does not
help: an operator already knows that, and the question they cannot answer from
their own memory is *which* everything. So §26 reads it.

**What is in it.** One listing per namespaced kind the cluster serves, from
discovery. Not a curated list of the fifteen kinds that usually matter: a curated
list is a completeness claim, and it goes stale the first time somebody installs
an operator. The namespace whose contents an operator cannot recite is exactly
the one full of custom resources.

**What happens to the data.** For each PersistentVolumeClaim, the bound volume's
`persistentVolumeReclaimPolicy`. This is the finding the section exists for,
because the policy lives on a cluster-scoped object the operator is not looking
at, `Delete` and `Retain` are opposite outcomes, and only one of them is
recoverable.

**What stops answering.** LoadBalancer Services, with their current addresses —
because the address is what other people's systems point at, and it does not come
back. And webhook configurations backed from inside, which is §19's finding moved
one step earlier: §19 tells you a webhook has no backend *after* somebody deleted
its namespace, and at `failurePolicy: Fail` that is a cluster refusing writes.

**Why it might not finish.** Every object holding a finalizer. This is the one
finding that is equally useful after the fact, which is why the plan is a read
and is offered on a namespace already `Terminating`: the same screen that would
have warned you is the screen that tells you what is holding it.

### 17.2 The three nulls, and what each one prevents

§0.1's corollary — *a number we could not derive is `null`, never `0`* — is
stated once in the invariants and lands hardest here.

`count: null` on a kind whose listing was refused. The alternative renders as
"this namespace holds no PersistentVolumeClaims", which is §0.1's motivating
sentence appearing at the exact moment it costs the most. It also covers the
truncation case: a listing with a `continue` token and no `remainingItemCount`
is `null`, not the page size, because "200" reads as a total.

`reclaim_policy: null` on a claim whose volume could not be read. The two real
answers point in opposite directions, so there is no safe default — `Retain`
would say the data is fine and `Delete` would say it is doomed, and both are
claims about somebody's database made by a console that did not look. The UI
draws it as an em dash with the reason, and it is a consequence the operator
acknowledges by name.

`webhooks: null` when neither configuration listing answered. `[]` there says "no
webhook points at this namespace", which is the most reassuring possible
description of "we could not look" — and the gap between them is a cluster that
stops accepting writes. One listing answering is enough to report what it saw:
losing the mutating listing must not turn a real validating finding into silence.

### 17.3 Recomputed at the write, never trusted from the plan

The plan can be minutes old — this is the most expensive read in the console, and
it is the screen people stare at. In between, a volume can be provisioned, a
LoadBalancer can come up, an operator can install a webhook. So the write
recomputes the whole plan and checks the acknowledgement against **that**, which
is the same rule §18, §20, §21, §22, §24 and §25 follow and matters more here:
the tick the operator gave was for a namespace that no longer exists in that
shape.

### 17.4 The typed confirmation is not the handshake

§26 has both, and they answer different questions. The checklist covers the
consequences the plan *found* — this volume, that address, that webhook — and an
empty namespace produces none of them. Typing the namespace name covers the act
itself, which is irreversible whether or not it takes anything interesting with
it, and is there on every deletion including the empty one.

Neither is theatre and neither substitutes for the other. A checklist without the
typed field would let an empty namespace be deleted by one misplaced click; a
typed field without the checklist would make an operator spell out `prod` while
telling them nothing about the disk.

### 17.5 `applied: true` is a deletionTimestamp

This is the defect standard applied to the one write in the console whose
completion is genuinely not observable at the moment it returns. The namespace
controller starts; how long it takes is a function of finalizers this console
does not run. So the summary says the deletion *started*, names `Terminating` as
the state it may sit in, and points back at the finalizer table rather than
reporting a namespace that is gone.

"Deleted" over a namespace stuck behind an uninstalled operator's finalizer is
the same sentence as §6's "drained" over three pods the API server refused.

### 17.6 What withholding the permission does

`delete namespaces` is the single most destructive verb this console holds, and
the shipped writer role states it as its own rule with its own paragraph so that
removing it is a one-line decision. Withheld, the plan still reads — an operator
deciding whether to grant it can see exactly what granting it would allow — and
the button is disabled with the reason, per rule 11.4. Nothing about §26 is a
substitute for not holding the verb where nobody should.

---

## 18. Acting as the operator (ADR-0007) — what the preflight is finally about

Every section above this one describes a control that answers a question about
**the console**. §2's preflight asks whether the console may act; §7's trail
records who used the console; §8's gates decide whether the console will try.
That was accurate and it was a gap, documented as one in ADR-0007 for three days
before it was closed.

A cluster registered with "act as the signed-in operator" changes what those
controls are about, and only for that cluster.

### 18.1 The preflight becomes a statement about a person

`SelfSubjectAccessReview` asks "may **this credential** do this". That is the
question that decides whether a write succeeds, so the answer was never wrong —
it was correct about the wrong subject. An operator with no `patch deployments`
of their own saw an enabled Scale button, clicked it, and it worked, because the
console's ServiceAccount could.

Under impersonation the review is issued as the operator, so §11.4's disabled
button is a statement about the person reading it. And because there are now two
possible subjects, every result says which one it answered for — a denial that
cannot name its subject sends somebody to edit a ClusterRole that was never
consulted.

### 18.2 The audit trail gains a name this application did not invent

ADR-0003's honest limit is that a hash chain makes tampering *detectable* in a
table this application owns. The strongest claim it can make is "these records
have not been altered since we wrote them", and the actor in them is a name only
this console can vouch for.

The API server's own audit log records both parties on an impersonated request.
So `impersonated_user` on our row is the join key to a log written by something
that does not trust this console at all — and "who scaled the payments service to
zero" becomes answerable without taking this application's word for anything.

`null` there means the console acted as itself. It is never defaulted to `actor`:
a trail naming a subject the cluster never saw reads as attribution and is a
claim this application cannot support, which is exactly what ADR-0003 refuses
when it declines to back-fill pre-chain records.

### 18.3 It refuses rather than falls back, and that is the whole design

There is no path that returns "no headers" for a cluster that asked for
impersonation. A session that cannot supply a cluster identity gets
`impersonation_unavailable` and the request never reaches the API server.

The alternative — fall back to the ServiceAccount — is worse than not having the
feature. A read served that way shows an operator data their own RBAC forbids,
and nothing on the page says so. That is this document's defect standard with a
security consequence attached: a wrong answer delivered confidently, where the
wrongness is an authorization boundary.

Three things make the refusal structural rather than disciplined:

* `decide()` raises **inside** `get_clients`, before a bundle is returned, so
  there is no transport a caller could have used as the console.
* Whether a transport may impersonate at all is a property of the transport,
  fixed when it is built. The connection test's unsaved credentials and the local
  kubeconfig cannot assert somebody's cluster identity no matter what any
  contextvar says.
* The headers are merged in the **one** wrapper every cluster call passes
  through, so a new endpoint cannot forget and a new typed client cannot bypass.

### 18.4 The exemptions are a list, not a habit

Three calls stay as the ServiceAccount, and ADR-0007 requires they be enumerable
rather than emergent. Discovery (a per-cluster cache shared between operators),
the connection test (a question about the stored credential), and the local
kubeconfig (no cluster row to opt in). A test greps the tree and fails on a
fourth, so adding one is a reviewed act with a written reason.

### 18.5 The two refusals that keep it honest

**A session whose name the cluster could not have derived itself cannot become a
cluster identity.** If a local account in this console's own table could cause a
request to arrive at the API server as `alice`, then the sign-in throttle and the
password policy stop protecting a console and start protecting the cluster's
authorization model — and this application's user table has quietly become an
identity provider the cluster trusts. Nothing in the API server checks that the
name in `Impersonate-User` belongs to anyone real; it checks only that the
impersonator may say it. This is the condition most likely to be argued away by
somebody who wants the feature on a cluster with no OIDC, and the answer there is
to give the cluster an issuer, not to give the console one.

The console offers six ways in, and only two of them clear that bar: **OIDC**,
because an API server can be pointed at the same issuer, and **the cluster's own
OpenShift OAuth server**, because the username and groups *are* the cluster's own
record of them. A plain OAuth 2.0 provider and a SAML IdP both genuinely
authenticate somebody — in a vocabulary no API server consumes, so the string the
console would send is one it chose the shape of, which is the same invention as
sending its own opinion about somebody's groups. `IMPERSONATION_SOURCES` in
`app/k8s/impersonation.py` is that set, with a test asserting its exact contents;
`docs/adr-0007-impersonation.md` records why each of the six lands where it
does.

**An absent groups claim refuses.** Empty is a real answer. Absent means the
issuer did not tell us, and impersonating on that reading strips every
group-derived permission the operator holds — then reports the result as
permissions they lack. The console would be manufacturing accurate-looking
denials for a person who is correctly configured, and the reliable end of that is
a ClusterRole widened to fix a problem that was never RBAC.

### 18.6 What withholding the grant does, and why it ships withheld

`impersonate` on `users` with no `resourceNames` is cluster-admin by proxy: an
account that can impersonate any user can impersonate the most powerful user on
the cluster. So `deploy/rbac.yaml` ships the rule **written out and commented
out**, with `resourceNames` on both halves.

The narrow form costs something and the file says so: it is a list somebody
maintains, and an operator missing from it cannot use that cluster at all. The
console reports that as `impersonation_unavailable` naming the reason rather than
as a permission denial, so it looks like the missing grant it is — but somebody
still has to add the name.

Note what this does *not* fix. On a deployment that kept the wildcard writer rule,
the console's ServiceAccount can already write RBAC and therefore grant itself
anything; adding `impersonate` there buys attribution, not containment. The
narrow grant is written for the deployment that deleted the wildcard and has a
genuinely bounded ServiceAccount — the one an unrestricted grant would unbound in
a single line.

---

## 19. Disruption budgets (§28) — the object that fails silently

Every other section here is about a control this console operates. This one is
about a control the *cluster* operates, which this console can only read — and
which is uniquely able to be completely broken while reading as correct.

That matters to a safety model because §5's drain is built on it. The drain plan
tells an operator which pods an eviction would be refused for, and it is right.
What it cannot tell them is that the budget doing the refusing was never going to
allow anything, or that it covers a workload nobody has run since a rename, or
that a second budget makes the pod un-evictable regardless. Those are questions
about the budget, asked before the drain rather than during it.

### 19.1 Three silent failures, and why each is a defect standard case

**A budget that covers nothing** is the §0.1 corollary pointed at availability.
`selected_pods: 0` is the finding — this object constrains no eviction. The
danger is the *other* zero: a pod listing that did not answer, rendered as `0`,
tells an operator their budget is dead and gets a working one deleted. So the
count is `null` when it could not be derived, drawn as an em dash, and the
distinction survives all the way to the table cell.

**A budget that can never allow an eviction** is a config bug that presents as a
stalled upgrade. It is separated from `disruptionsAllowed: 0` — which is the
controller's last written count and usually clears on its own — because
collapsing the two either panics somebody about a healthy budget or buries a
permanent one among transient ones. The permanent finding also says whether it is
*unconditional* (`maxUnavailable: 0`, true at any replica count) or *conditional*
on the current pod count, because scaling up fixes the second and never the
first.

**A pod covered by two budgets** is the one worth reading twice. Kubernetes does
not support overlapping budgets, and the way it does not is that the eviction API
refuses that pod **outright** — not by the numbers, by rule. Both objects report
themselves healthy. Nothing on either mentions the other. A drain fails on one
pod with a message about a condition nobody set. This is the finding no single
row can carry, so it is an index beside the rows, and `null` there means the pod
listing failed rather than that nothing was found.

### 19.2 The third answer to the selector question

A label selector this console cannot evaluate — a `matchExpressions` operator
outside the four Kubernetes defines — has three answers available, and only one
of them is honest: *matches*, *does not match*, and *we could not tell*.
`shaping.label_selector_matches` is **tri-state** and returns the third.

There was for a while a second matcher, `workloads.selector_matches`, that
resolved an unmodelled operator to `False`. That is defensible on its own terms —
over-matching would attribute other workloads' pods to a row — and it did not
stay on its own terms: §5's drain plan imported it, and the answer it produced
there was "no PodDisruptionBudget covers this pod", stated in a plan an operator
reads before draining a node. The budget was never consulted again; the API
server refused the eviction mid-drain instead. A matcher that resolves one
ambiguity is reused for a question where that resolution is a lie, and nothing
about the call site shows it.

So there is one matcher, and the three questions are answered by the three
callers, each with the third state in front of it:

* **§28** reports `selected_pods: null`, an `undecidable_pods` count, and a
  finding that says in words this is *not* a report of zero. `False` here
  manufactures the "this budget protects nothing" sentence and the operator
  deletes a budget that works.
* **§5's drain plan** lists the budget under `pdbUnknown` on the pod: coverage
  unknown, not absent. It is deliberately not a blocker — the eviction
  subresource is the enforcer, and refusing a drain over a selector we merely
  could not parse is how `force` becomes reflex (§5.4).
* **§6's workload row** returns `restarts_24h: null` for the whole workload as
  soon as one pod is undecidable. Summing the rest would be a floor rendered as
  a total, and a restart count that reads low is the one nobody re-checks.

Same objects, three questions, three answers — but the divergence now lives at
the three call sites, where the cost of each choice is visible, instead of inside
a shared helper that hands out one of them to whoever imports it.

### 19.3 What it does not claim

It never says a budget is **enforced**. The eviction subresource is the enforcer,
and `disruptionsAllowed` is a number a controller wrote at some past moment. So
there is no field called `safe`, `protected` or `will_block` — the same rule §11
applies to NetworkPolicy and the CNI plugin.

It does not say which workloads *should* have a budget. A multi-replica
Deployment with none is evicted freely, and that is very often correct. Putting
the console's opinion about somebody's availability requirements on a page would
produce a finding that fires on most rows, and a finding that fires on most rows
is one nobody reads on the day it matters.

And it writes nothing. Editing a budget is §4's YAML editor, through the funnel —
because a budget changed without a diff shown first is exactly the change that
turns a routine drain into a stalled upgrade.

---

## 20. Quota advice (§29) — the refusal that is not about headroom

§29 is the only section in this document about a refusal the console cannot
prevent, cannot preflight and does not audit. A ResourceQuota is enforced by the
API server's admission chain, which is exactly where it should be. What §29 adds
is that the answer arrives *before* the write instead of as a 403 afterwards —
and that the two refusals a 403 makes look alike are kept apart.

### 20.1 Why "must specify" is the one that costs an afternoon

A quota bounding a compute resource makes that resource compulsory on every
container in the namespace. Omit it and the pod is refused with *"must specify
requests.cpu"* **at any level of quota usage** — one percent used, refused. The
fix is a LimitRange with a `defaultRequest`, and the message names a field
rather than the missing object.

That is a different problem from "the quota is full", and the two send an
operator to two different places: one to trim the request or raise the limit,
the other to create an object the refusal never mentions. A console that listed
them together would send somebody to raise a limit that is fine, which wastes
the afternoon the message already cost them. So they are separate findings, on
separate rows, with the headroom check for the same resource visibly reading
`admitted` beside the "must specify" refusal.

### 20.2 `unknown` is a verdict, not a failure

Three situations make the arithmetic unfinishable, and all three answer
`unknown` rather than resolving toward the reassuring side:

**An unwritten `status.used`.** The quota controller writes usage
asynchronously, so a fresh quota has none. Reading absence as zero would report
the roomiest possible headroom at the moment this console knows least — to the
person about to deploy. That is the defect standard aimed squarely at the
question being asked.

**A scoped quota.** `scopes` and `scopeSelector` decide which pods a quota
counts, and for a workload that does not exist yet the answer depends on its
priority class, its terminating state or whether it requests anything at all.
Including it invents a refusal from a quota that may not govern the workload;
excluding it hides a real one. So the quota's numbers are shown, its verdict is
withheld, and the namespace verdict is `unknown`.

**A LimitRange listing that did not answer.** This one additionally suppresses
the "must specify" check rather than computing it from an empty default map.
Computing it there would be wrong in the *dangerous* direction: a workload
reported as refused for a missing value that a default this console could not
read would have supplied. Under uncertainty the rule is not "assume the worst" —
it is "say you do not know", because a false refusal trains people to ignore the
true ones.

### 20.3 The boundary with §4's dry run

§4 can already ask the API server, and the API server is the authority. §29 does
not compete with that and says so: no field is named `will_be_admitted`.

The two answer different questions at different times. §4's dry-run create needs
a manifest and `create` permission, and returns *refused* — one bit. §29 answers
from a form before a manifest exists, and returns the two things the 403 does
not carry: how much room is left, and which of a quota's eight bounds is the
tight one. During an incident that difference is the difference between "it will
not deploy" and "trim 200 millicores off the sidecar".

### 20.4 What it deliberately does not model

LimitRange `max`, `min` and `maxLimitRequestRatio`, Pod-scoped LimitRange items,
and priority-class scope selectors are read and reported but not evaluated into
the verdict. Where any of them could change the answer the verdict is `unknown`.

And fitting inside a quota is not fitting on a node. A workload §29 admits can
still sit `Pending` because nothing has room for it, which is the scheduler's
answer and a different question — §5's, not this one's.

---

## 21. Granting a role (§30) — the diff that is three words long

A RoleBinding's diff is a name appearing in a list. §4 can already produce it,
and what it shows is true and useless: the object that says what the name can now
*do* is somewhere else, and the operator who confirmed the grant did not open it.

This is §17's problem — the namespace delete whose diff is a metadata block —
pointed at a security control instead of at storage. The write is the same one
§4 makes. The screen in front of it is the feature.

### 21.1 The two grants that reach further than their names

**`admin` in a namespace includes `create rolebindings`.** The grantee can grant
themselves and anybody else every other role bindable there. That makes the
revoke asymmetric in a way nothing on the screen would otherwise say: taking the
binding back does not take back what they granted while they had it.

**`edit` includes `create pods/exec`.** A shell in a pod reads every Secret
mounted into it and every environment value it was started with — so the role
hands over the namespace's credentials **while having no rule about Secrets at
all**. A console that scanned for a `secrets` rule would confirm this grant as
touching none, which is a confidently wrong answer about who can read a database
password.

Five capabilities are called out and no more, because a finding that fires on
most roles is one nobody reads on the day it matters: a wildcard, the two above,
`impersonate`, and direct Secret reads. Everything else the role grants is still
granted; this list is the subset worth a sentence of its own, and the response
says so rather than implying the rest is nothing.

### 21.2 Three states for one role, because they send you three places

`present` is a role that was read. `absent` is a role that is not there — and the
API server **accepts a binding to it**, which grants nothing today and starts
granting whatever appears under that name later, with nobody deciding a second
time. `unreadable` is a read that failed.

`rule_count` is `null` for the last two and for an aggregated ClusterRole whose
controller has not written its rules yet. Never `0`. This is §0.1's corollary at
its sharpest: "this role grants nothing" is the answer that must not be produced
by a read that did not happen, and the screen it would be produced on is the one
where somebody decides to bind it.

### 21.3 "Revoked" is a claim, and usually a false one

Removing a subject from a binding changes a subject list. It does not remove
their access if another binding here names them, or if any ClusterRoleBinding
does — a cluster-wide grant applies in every namespace, including this one.

So the revoke plan lists what else names them, and the write carries that list
into its response, so that `applied: true` cannot be read as "they can no longer
act here". The summary on screen says the same thing in words rather than leaving
it to a table.

**And the residual list is itself tri-state.** The namespace's bindings are in
hand; the cluster-wide listing is a second read that can be refused. When it is,
`cluster_bindings` is `null` and gets its own acknowledgement. `[]` there would
mean *the cluster was searched and nothing else grants this* — the sentence
somebody closes a ticket on, and the one this model exists to stop being produced
by a read that never ran.

Even the clean case does not claim the permission is gone. §30 subtracts objects;
§23 asks the API server's whole authorization chain. Both revoke consequences
point at §23 for the authoritative answer, and no field here is named
`has_access`.

### 21.4 What it refuses rather than guesses

Two RoleBindings sharing a `roleRef` make "remove alice from `view`" ambiguous.
§30 refuses and names them instead of picking the first, because picking would
report a revoke that left her bound through the second — the same failure the
drain's per-pod results exist to prevent, one object earlier.

A binding-name collision is refused rather than renamed. `view-1` is a decision
about an object the operator will later go looking for, made by a console they
never saw make it.

And the last subject out of a binding leaves the binding, empty. Deleting it
would be a second verb whose blast radius — every other subject in it — this plan
would then have to explain as well, in exchange for tidiness. The empty
`subjects: []` is sent rather than `null` for the same reason the plan exists at
all: only one of them shows, in the diff, that the binding survived with nobody
in it.

---

## 22. Why is this pod Pending (§31) — the answer that already existed

Every other section here is about a write. This one is about a read, and it
belongs beside them because it is the same failure in a different place: an
answer delivered confidently that is not true.

The scheduler computed the answer. It is in an event. The console showed the
word `Pending` and left the operator to find it.

### 22.1 Two questions wearing one phase

A `Pending` pod that already carries `spec.nodeName` has been **placed**. What
is holding it is on that machine — an image, a volume, an init container — and
cluster capacity has nothing to do with it. A console that answers both cases
with a table of node capacity sends half its users to audit a fleet over a
failed image pull on one box.

So `waiting_on` is the first field, and the panel branches on it before it shows
anything else. Only `scheduler` gets the nodes.

### 22.2 A snapshot rendered as a status

`0/5 nodes are available: 3 Insufficient cpu` is what the scheduler saw at one
instant. It is not maintained. A node added since, a pod deleted since, a taint
removed since — none of them rewrite it, and the event can sit there for its
whole TTL describing a cluster that no longer exists.

The age is therefore never optional. It is beside the message, in words, saying
what the number means: *this is what it saw then*.

### 22.3 The null that must not read as a clean bill

Events age out of etcd, an hour by default. A pod pending since this morning has
an explanation that expired.

`scheduler: null` is drawn as a **warning**, not as blank space, and it says so
in the sentence: no explanation is readable, and that is not a report that the
scheduler is content. A blank there is the reassuring reading of a missing fact,
on the one screen where something is demonstrably wrong.

### 22.4 The verdict this feature refuses to offer

The per-node table is this console's own re-derivation. It checks cordoning,
readiness, untolerated taints, `nodeSelector`, and requests against allocatable
minus what is already requested. Every one of those is a reason to **rule a node
out**.

There is no `fits`, and adding one would be the defect. The scheduler also
weighs inter-pod affinity and anti-affinity, topology spread, volume node
affinity and zone, extended resources, host ports, RuntimeClass overhead and
every plugin the cluster runs. None of that is evaluated. `no_reason_found`
means *this console found nothing against it*, which is a statement about this
console — and promoting it to "this node has room" would send somebody to argue
with a scheduler that had already rejected the node for a reason they cannot
see.

`capacity_checked` is the same discipline one level finer. A node whose free
capacity was never compared — the pod listing failed, a neighbour's request
would not parse, the node publishes no pod-slot count — reached
`no_reason_found` by a shorter path, and the row says which. Two shrugs of
different strength must not be drawn as one.

### 22.5 The claim that is the symptom

An unbound PersistentVolumeClaim usually blocks scheduling. One whose
StorageClass uses `WaitForFirstConsumer` is unbound *because* the pod is
unscheduled — the provisioner is waiting for a node to be chosen so it can
create the volume in the right zone.

Reporting that as the blocker sends an operator to fix storage while storage
waits on them to fix scheduling. So the field is tri-state, and the `null` — an
unbound claim whose binding mode could not be read — is the honest answer to a
question with two possible causes and even odds.

### 22.6 What it does not claim

It does not predict where the pod will land, it writes nothing, and it is a read
at a moment rather than a watch. Its whole contribution is the separation: which
of the two questions this is, how old the answer is, and which nodes are out for
reasons an operator can act on.

---

## 23. The certificate an exposure serves (§32) — the green row in front of an outage

Every other section here is about a write. This one is a read, and it belongs
beside them because it is the same failure somewhere else: **an answer delivered
confidently that is not true.**

`kubectl get ingress` prints `TLS: 1 secret`. `oc get route` prints `edge`.
Neither prints a date and neither prints a name. Both facts are inside the
certificate, in the clear — every client that completes a handshake with that
server is handed them — and reaching them means base64-decoding a Secret and
running `openssl x509 -text`. Nobody does that until the site is down, which is
why the certificate expiry outage is the one that was scheduled months in
advance and still surprised everybody.

### The second question, which is the one that catches people

*When does it expire* is the famous half, and the easy one: it is a date.

*Is it even for this hostname* is the other, and a certificate can be current,
correctly issued and completely useless. An Ingress moved to a new host, a
wildcard covering `*.example.com` and not `example.com`, a Secret two Ingresses
share where only one was renamed — each of those produces a **green row in every
Kubernetes tool there is** and a browser that refuses to connect.

So coverage is checked, against **subject alternative names only**. Every
browser shipping today ignores the Common Name for host verification; matching
on it would let this console call a certificate correct that no client will
accept, which is the defect standard with a padlock drawn on it. Wildcards
follow RFC 6125 — one label, leftmost, whole label — because relaxing any of
those reports a host as covered that Chrome refuses.

### Four things §32 refuses to say

**That a client will accept it.** No trust store is consulted, no chain is
built, no signature is verified, no revocation is checked. A certificate this
section calls unexpired and name-matching can still be rejected by every browser
on earth. The UI puts that sentence **above** the table, not under it: a green
column read as "this works" is a claim the console cannot make, and a caveat
below the fold is a caveat nobody reads.

**That this is what the router serves.** The Secret is what the object *points
at*. A router started with `--default-ssl-certificate`, an annotation overriding
the Secret, a cert-manager renewal that landed in the Secret but not in a router
that has not reloaded — in each of those the bytes on the wire differ from the
bytes here. Only a handshake settles it, and this console makes none.

**That an unreadable certificate is a missing one.** A Secret the caller cannot
read leaves `certificate: null`, `state: unknown` and a finding naming the failed
read. It never renders as an exposure with no certificate — the reading that
sends somebody to create a Secret that already exists and is fine — and every
host on that row is `covered: null`, because `false` is a claim about a
certificate and there is none here to make it with.

**That an exposure with no certificate of its own is broken.** A `passthrough`
exposure keeps its certificate in the pod; one that terminates TLS and names
nothing is served by the router's default. Both are ordinary, both are drawn
neutral, and both say where the certificate actually is. Painting either red
puts a fault on an exposure working exactly as designed.

### The null that is not a zero, again

`expires_in_seconds` is `null` when nothing was read and **negative** — never
clamped — when the certificate has already lapsed. `0` is a real value here and
it means *expires this second*: it is the one number on the page an operator
acts on without reading the rest of the row, so using it for "we could not look"
would buy an outage a deadline it has already missed.

### One key, by name

The certificate is public and the private key is not. §32 reads exactly one key
of a `kubernetes.io/tls` Secret — `tls.crt` — and `tls.key` is not read,
decoded, counted or named. **No PEM reaches the response at all**: the report is
dates and names, and shipping certificate bodies to a browser buys nothing that
could be lost. A Secret of any other type is refused *by type* rather than
searched for something certificate-shaped, because rummaging through an `Opaque`
Secret would be reading application passwords on a page about expiry dates.

It is deliberately not behind `SECRET_REVEAL_ENABLED`: that gate guards the
*values* of a Secret, and what comes back here is derived, public metadata about
the one key of the one Secret type whose contents that server hands every client
that connects to it. Gating it there would make the section useless on every
deployment that leaves the gate off — which is all of them — while protecting
nothing. RBAC still decides: a caller who cannot read the Secret gets an
`unavailable[]` entry and `state: unknown`.

---

## 24. Installing OLM (§33) — the second bundle, and the wait

§14 put a proxy on somebody's cluster. §33 puts the cluster's **operator control
plane** there, and the difference in size is the whole reason it has its own gate
rather than riding on §16's.

`docs/adr-0008-shipped-olm.md` records the decision and the argument against it.
This section is what the safety machinery does with it.

### 24.1 The thing to read before anything else

Upstream's `olm.yaml` contains this, and the console creates it:

```yaml
kind: ClusterRole
metadata:
  name: system:controller:operator-lifecycle-manager
rules:
  - apiGroups: ['*']
    resources: ['*']
    verbs: [watch, list, get, create, update, patch, delete, deletecollection, escalate, bind]
```

That is cluster-admin plus the ability to grant cluster-admin. ADR-0004 called
cluster-wide Secret reads "the largest RBAC grant this product has ever asked
for"; this is larger by a wide margin, and `escalate` means OLM can grant
permissions nobody gave it.

It is inherent to OLM rather than chosen here — OLM installs operators that ask
for arbitrary permissions, so it has to hold them. That is an explanation, not a
mitigation. What actually mitigates it is small and worth listing because it is
all there is: the object is in the diff before it is created; the consequence
quotes the rule verbatim rather than calling it "broad permissions"; the feature
is off by default; and the plan is readable with the gate off, so the decision to
open the gate is made with the object on screen.

### 24.2 Two gates, and neither withholds the dry run

`ADMIN_ALLOW_MUTATIONS` and `ADMIN_OLM_INSTALL_ENABLED`, handed to `mutate()` as
a `FeatureGate` and not checked by the feature. Neither withholds the preview,
which is the same call §14 and §16 make and the opposite of §5.5's node debug
pods.

The reason is sharper here than anywhere else in this model. §5.5 withholds its
projection because the projection *is* the sensitive thing — a working recipe for
a privileged pod. §33's projection is a pinned copy of a public upstream release
that anyone can download, and it contains the ClusterRole above. Withholding it
would mean the decision to grant OLM `*` on `*` gets made without the object in
front of the person making it, which is the exact inversion of what the gate is
for.

### 24.3 The adoption refusal, made stricter than §14's

Every object carries `app.kubernetes.io/managed-by: k8boss-admin`. An install
that finds any of the twenty-six already present without it refuses — before
writing anything, on a dry run as much as on a real one.

Two differences from §14, both because the blast radius is larger:

* **It names every conflict, not the first.** A cluster already running OLM
  collides on most of the twenty-six at once, and reporting them one per attempt
  would have an operator re-run the install a dozen times to learn a fact that
  was knowable on the first read.
* **The failure it prevents is not "a takeover", it is an outage.** Writing
  0.35.0's Deployments over a running 0.30 restarts the cluster's entire operator
  control plane, and every operator OLM manages with it.

The refusal is audited by hand, because it fires before the first `mutate()` and
the funnel — which records everything else — is never reached.

### 24.4 The wait, and why it is not a control loop

Between the phases the install waits for the eight CRDs to report `Established`.
It is the only place in this codebase that blocks on a cluster, so it is worth
being exact: **one bounded wait inside one request**, what `kubectl wait` does,
bounded by `ADMIN_OLM_ESTABLISH_TIMEOUT_SECONDS` and then abandoned. It holds no
state, survives nothing, retries nothing and corrects no drift.

Three properties are load-bearing:

* **A CRD read that failed is `None`, not "not established yet".** Counting it as
  pending would spend the whole timeout on a cluster answering perfectly well
  about everything else; counting it as established would send phase two at an
  API that is not ready.
* **The timeout stops the install.** Phase two is reported as skipped with the
  timeout's own sentence, and the CRDs that really were created are reported as
  applied. "Re-run the install" is safe and the message says so: the objects that
  exist are this console's and are re-applied, not refused.
* **Discovery is invalidated only on success.** The console caches what a cluster
  serves for up to a minute, and phase two addresses five kinds that were not in
  that cache when the request started. Dropping the cache after a *timeout* would
  advertise APIs that are not established — the same wrong answer one layer down.

### 24.5 `installed` is not `working`, and the response says which it means

Twenty-six accepted objects is not a running OLM. The Deployments have to
schedule, and the `packageserver` ClusterServiceVersion has to be reconciled by
OLM before `packages.operators.coreos.com` exists — until then §16's portal reads
the same empty state it read before the install, **and it is right to**.

So `installed` means "every object was accepted", `ready` is `null` on the
install response with a sentence naming the live read that answers it, and
`GET /api/portal/olm` reports the two separately as tri-states. A green banner
over a package server that has not registered would be this project's defect
standard aimed at the thing the console had just done — the §14 lesson
(`installed: true` over a router admitting nothing) with a cluster's operator
control plane in place of a proxy.

### 24.6 The honest limit: preflight cannot see this one coming, and here it never can

§14 documented escalation prevention as a failure preflight cannot predict: a
`SelfSubjectAccessReview` on `create clusterroles` answers yes, and the API server
then refuses at admission because the caller does not hold what it is granting.

For §14 that fires when the console lacks cluster-wide Secret reads. For §33's
ClusterRole it fires unless the console's identity holds **everything**, so on any
deployment that has narrowed `deploy/rbac.yaml` it fires always. §0.2's promise —
a denial naming the missing permission — cannot be kept from the review here.

What the code does instead is deliberately narrow and identical to §14's: the
status mapping stands (`rbac_denied`, by status code), and **only the hint** is
rewritten, to name escalation prevention and the `escalate`/`bind` grants rather
than sending the operator to grant `create clusterroles` — the one verb the
review has just confirmed they hold. Matching on the API server's message string
is forbidden everywhere else in this codebase; it is confined to the hint so that
a miss degrades to the ordinary hint rather than to a wrong code.

### 24.7 What this section does not claim

It does not claim the community catalog is safe, which is why installing it is
opt-in and acknowledged by name. It does not claim OLM can be removed from here —
it cannot, and §33.8 says what removal actually takes. It does not claim to
upgrade an OLM already running, and it refuses to write over one. And it makes no
claim at all about what OLM subsequently grants an operator: that is §12.5's
honest limit, unchanged, and §33 makes it reachable on more clusters rather than
narrower on any.

---

## 25. Onboarding (§34) — the credential a laptop already has

Every section above is about what happens after a cluster is registered. This
one is about the registration, because §34 added a way to create one from a file
instead of from a form, and a credential that arrives without anybody typing it
is worth being explicit about.

### 25.1 The read is not a client

`app/k8s/client.py` opens with *clients are built from a registered cluster's
stored API server endpoint and encrypted credential — never from a kubeconfig on
the server*. §34 reads a kubeconfig. Both are true, and the distinction is the
whole safety property:

* Discovery **lists**. It opens no socket and returns no credential material —
  only what *kind* of credential each context holds. A test scans whole response
  bodies for the key, because the interesting leak is never a field called `key`.
* An import **copies**, once, into the same encrypted registry every other
  cluster lives in.
* Nothing reads the file again. There is no refresh, no rotation-following and
  no request-time fallback to it, so a cluster's identity cannot change under a
  running console because somebody ran `kubectl config use-context`.

Had it been wired the other way — resolve the context at call time — a
registration would mean different things on different days and the audit trail
would name a cluster whose identity had moved. That was rejected in
[ADR-0012](adr-0012-kubeconfig-onboarding.md) and is the line a later change has
to cross deliberately.

### 25.2 The file is parsed, never loaded

`kubernetes.config.load_kube_config` executes the `exec` credential plugin of
whichever context it loads. Using it would mean that *listing* what is available
on a machine runs whatever binary a file on that machine names — `aws`,
`gke-gcloud-auth-plugin`, or anything a writable kubeconfig was edited to point
at. So the document is read with `app.yaml_dialect` (ADR-0009) and an `exec`
stanza is a fact about a context.

The refusal is a listed row with a sentence on it, not an omission. §0.1 applied
to a file: an operator whose only context is an EKS one has to be able to tell
"this console will not run your credential plugin" from "you have no kubeconfig",
and a filtered list says neither.

### 25.3 A registration nobody asked for, and the five things that stop it

Startup adoption writes a `Cluster` row without a request. That is a real thing
to be uneasy about, so it is fenced in five ways, every one of which must hold:
the switch (`ADMIN_AUTO_DISCOVER_LOCAL`, default on), **an empty registry**, a
context a local tool wrote *whose API server is also a loopback or private
address*, no visible reachability problem, and **exactly one** such context.

Two of those are the ones that matter. The empty-registry test is what keeps a
curated fleet from being added to. The double local test — a recognised
distribution **and** a local address, joined with `and` — is what stops a context
somebody named `kind-prod` pointing at a public endpoint from being adopted: a
console that boots and quietly registers a production cluster is a far worse
failure than one that asks.

Anything else logs the reason and registers nothing, and the row it does write
carries `origin: "autodiscovered"`, because the first question about a cluster
somebody does not remember registering is whether they registered it.

### 25.4 What a local cluster's credential actually is

A ServiceAccount token minted by `deploy/rbac.yaml` holds exactly the permissions
that file grants, and can be revoked without touching anything else. A `kind` or
`k3d` kubeconfig's client certificate is `cluster-admin`, because that is what
those tools write.

So an adopted cluster is registered with far more authority than a deliberately
onboarded one. That is appropriate for a throwaway cluster on a laptop and would
not be for anything else, and it is the sharpest reason adoption never takes a
remote context at any count. It is stated here rather than left implied by the
restriction, because a restriction whose reason is not written down is a
restriction somebody relaxes.

### 25.5 The private key on disk

The kubernetes client reads a certificate and key from *paths*, so building a
client-certificate transport writes a decrypted key to a temp file. It is created
mode 0600, tracked on the client bundle, and deleted when the bundle closes —
which happens on every cache rebuild, and a cluster's bundle is rebuilt whenever
its row is edited. The version of `build_configuration` that returned only the CA
path is what would have left one copy behind per rebuild; it now returns every
path it wrote, as one tuple, because a second return value is a second thing a
caller can forget.

### 25.6 What it does not claim

**It does not claim a discovered cluster answers.** Discovery opens no socket and
an import opens no socket. `status` on an imported or adopted row is `unknown`,
which is not `disconnected` — nothing has been tried — and §3's connection test
with its baseline permission matrix is unchanged as the thing that establishes
both reachability and whether the credential can do the job.

**It does not claim a loopback address is reachable, or fix it quietly.** A
`127.0.0.1` API server read from inside a container is flagged, adoption declines
it, and the URL is not rewritten: `host.docker.internal` fails certificate
verification rather than connecting, and the only way to make that work is to
turn verification off on somebody's behalf.

**It does not claim to have looked when it could not.** A kubeconfig that is
absent, unreadable or malformed is an `unavailable` entry naming which of the
three — including the uid, for the mode-600-file-versus-container case that until
§34 produced no signal at all and looked exactly like a machine with no
kubeconfig on it.
