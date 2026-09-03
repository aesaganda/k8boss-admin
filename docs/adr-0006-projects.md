# ADR 0006 — Projects are five ordinary writes, not a template engine

**Status:** accepted
**Date:** 2026-09-02
**Supersedes nothing. Constrained by:** ADR-0004's boundary and ADR-0005's
reasoning, both of which this ADR argues it stays inside — and which are the
first things to check if you disagree.

## The decision

k8boss-admin reads one namespace together with what governs it — ResourceQuotas
with their usage, LimitRanges, the Pod Security level its labels declare,
RoleBindings, and a summary of its NetworkPolicies — and can create a namespace
together with those objects, as OpenShift's `oc new-project` does from a project
request template. `docs/api-contract.md` §17 is the normative shape.

The write is five objects, each an ordinary create through
`app.admin.apply.create_from_yaml` and therefore through the funnel, in a fixed
order, with a per-object outcome and no rollback. There is no template stored
anywhere: the request body is the template, the defaults live in the client,
and nothing in the console watches the objects afterwards. It is gated on
`ADMIN_ALLOW_MUTATIONS` alone.

## Why

The product direction is "vanilla Kubernetes, run the way a production OpenShift
cluster is run". Of everything OpenShift adds, the Project is the piece a
platform team uses most and misses first: `oc new-project` hands a team a
namespace that is already bounded (quota), already survivable (limit range
defaults, so a pod that states nothing still gets a request), already governed
(security level), already theirs (a binding), and often already isolated (a
network policy). On vanilla Kubernetes a team gets `kubectl create namespace`
and the rest arrives one incident at a time — the first noisy neighbour brings
the quota, the first `must specify requests.cpu` brings the limit range, the
first privileged pod brings the label.

Every one of those objects exists on vanilla Kubernetes. This console could
already create each of them through §4's YAML editor. What was missing was the
page that shows them together — the answer to "why will the next Deployment not
be admitted here" — and the act that creates them together, with the checks
that make the act honest.

## Why this is not a second shipped bundle

ADR-0004 drew a boundary: k8boss-admin installs **one pinned bundle of eight
objects, through the ordinary write funnel, and nothing else, ever.** A "project
request template" sounds like a second bundle. It is not, for the reasons
ADR-0005 gave about the Subscription, applied to five objects instead of one:

| ADR-0004's router | §17's project |
|---|---|
| Ships manifests, checked into this repo | Ships nothing. Every object is rendered from the request the operator sends |
| Pins an image and recommends a version | Pins nothing. There is no software here at all |
| Owes a version-bump obligation | Owes none. A ResourceQuota does not go stale |
| Installs a data plane this project chose | Writes governance objects the operator composed into a namespace the operator named |
| Carries a managed-by label and refuses to adopt objects without it | Carries no label. The console does not manage a namespace after creating it, and says so by leaving no fingerprint |

The honest test is the one CLAUDE.md states for §16: *could §4's YAML editor
have written this today, with no new code and no new gate?* Yes, five times. §17
adds discovery in front of it (whether the namespace exists), checks behind it
(the consequences, the per-object preflight on the dry run) and a report after
it (which object landed). None of that is a deployment engine, and the README's
four claims — no cluster state, every page a live read, no reconcile loop, one
narrow "installs software" exception — all survive unchanged.

## What this costs, stated rather than argued away

**The dry run cannot keep §3's promise for four of the five objects.** The API
server refuses a create into a namespace that does not exist, `dryRun=All`
included. So on a dry run only the Namespace carries the API server's own
projection; the four objects inside it are the console's rendering, diffed
against nothing, marked `projection: "rendered"`, with a preflight of the verb
the real write will need. The console says this plainly in the response and in
the dialog, and it is still a weaker preview than every other write in this
console gets. The alternative — creating the Namespace for real on the preview
so the rest can be projected — would be a write on the preview step, which is
the one thing the dry-run-first model forbids. Note that §14's router install has
exactly this shape on a fresh namespace and, when this ADR was written, did not
report it; that was recorded as a defect in §14 by the evaluation that produced
this ADR, and §14 has since been given the same rule.

**A partial project has no rollback.** Five writes, and the fourth can fail.
Deleting the three that landed would be three more writes the operator did not
approve, and deleting a namespace is not a recoverable operation. So the
response reports which objects exist, `created` is false, and the operator
finishes the job through §4. The asymmetry is real: a partial project is more
work to complete by hand than a whole one was to create.

**A RoleBinding to `admin` grants what `admin` aggregates on that cluster.** The
plan names the role; it does not enumerate what the aggregated ClusterRole
holds, because that is whatever operators and controllers have aggregated into
it on *this* cluster and it changes without anyone editing this console. The
console's own ServiceAccount writes the binding, and RBAC escalation prevention
will refuse it unless the ServiceAccount holds what the role grants or holds
`bind` on it — the hint names that, the way §14's names `escalate`.

**The console's user is not a cluster identity, and this feature makes that
gap more visible.** `oc new-project` binds the *requester*. §17 binds whichever
subject the operator types, because the console acts as one ServiceAccount per
cluster and has no identity of its own to bind. The evaluation that produced
this ADR named per-user impersonation as the next architectural step;
[`adr-0007-impersonation.md`](adr-0007-impersonation.md) is that decision written
down, and it is proposed rather than accepted. Until it is taken, the dialog asks
for the subject rather than pretending to know it.

## What was rejected

**A console-side project template, configured once and applied on every
create.** The obvious feature, and the one that would turn this into ADR-0004's
second bundle: a list of objects this project would have to maintain, version and
have opinions about, stored in the console's database, applied by the console on
its own initiative. The defaults belong to the organisation that has them, and
the request body is where they go. If a deployment wants them enforced, the
honest place for that is an admission webhook or a GitOps controller, both of
which this console explicitly is not.

**Adopting an existing namespace and adding what it lacks.** "Turn this
namespace into a project" is a reasonable ask and the wrong feature for a
multi-object write: the console cannot know what else is in the namespace or who
it belongs to, and a partial outcome over somebody's running workloads is worse
than a partial outcome over an empty one. Each addition is available through §4,
where it is one object and one diff. The refusal to adopt is stricter than
§14's managed-by rule on purpose.

**Creating the Namespace for real on the preview so the rest could be
server-projected.** Rejected in the paragraph above: a write on the preview step
breaks the model this product is judged on, for a better diff.

**Refusing a project with no quota, or with a quota and no LimitRange.**
Considered and rejected on ADR-0004's reasoning about the OperatorGroup: an
administrator may have a reason, the console cannot know it, and a wall is not
an explanation. Both are consequences the operator acknowledges by name, with
the sentence that says what happens otherwise.

**Deleting a project.** `kubectl delete namespace` deletes everything in it and
cannot be undone. §4's delete exists, with its own typed confirmation, and a
"Delete project" button beside "New project" would be a symmetry that invites
the click.

## The boundary

For whoever reads this next and is deciding whether the next thing belongs here:

> A project is **five ordinary creates into a namespace that does not exist,
> through the ordinary write funnel, from a request the operator composed, and
> nothing else.** No stored template, no adoption of an existing namespace, no
> delete, no reconcile, no label claiming management, and no claim about what a
> binding grants, what a policy enforces, or what a Pod Security default the
> console cannot read applies.

ADR-0004's boundary stands beside this one and is unchanged: one pinned bundle of
eight objects. ADR-0005's stands too: one Subscription. §17 extends neither, and
the day somebody argues that a stored project template is "really just a bigger
request body" is the day all three need re-reading.

If §17 is ever cut, nothing else moves. `services/projects.py`,
`admin/projects.py` and `api/projects.py` are self-contained; the shapers they
added to `resources/shaping.py` are pure functions nothing else calls; the only
shared code is the funnel, the envelope and `namespace_row`, which moved into
`shaping.py` where a pure function belongs and is used by §5 unchanged.

## See also

- `docs/api-contract.md` §17 — normative: the read model's tri-states, the
  plan, the consequence codes, the per-object report and what `projection`
  means.
- `docs/safety-model.md` §13 — the five properties applied five times over, and
  §13.3 on the dry run's honest limit.
- `docs/adr-0004-shipped-router.md` and `docs/adr-0005-operator-portal.md` — the
  two boundaries this ADR argues it stays inside.
- `docs/rbac.md` — the three write grants §17 adds and what `bind` is for.
