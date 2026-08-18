# ADR-0001: Dry-run first, not optimistic-with-undo

**Status:** accepted
**Date:** 2026-07
**Supersedes:** nothing
**Related:** [`safety-model.md`](safety-model.md), `backend/app/admin/mutate.py`

---

## Context

The console writes to production clusters. Every write UI has to choose where it
puts the friction, and there are two established shapes:

**Optimistic-with-undo.** The action happens immediately; the interface offers to
reverse it. This is what most modern software does, and it is right for most
software: it keeps the common path fast, and the rare mistake costs one click.
Gmail's "Undo send" is the canonical form.

**Confirm-first.** The action is described before it is taken, and taking it
requires a second, deliberate act. Slower on every action, including the
thousands that were fine.

The optimistic shape is genuinely better *when undo exists*. The question for
this project is whether it exists here.

## Decision

**Every mutation is dry-run first.** The UI's flow is fixed: build the request →
call with `dryRun: true` → show the returned unified diff → the operator confirms
→ call with `dryRun: false`. There is no button in this console that writes to a
cluster without having shown a diff first.

There is no undo feature, and there will not be one.

## Rationale

### There is no undo for a deleted StatefulSet

This is the whole argument, and it is not a rhetorical flourish.

Delete a StatefulSet and you can recreate the object from the YAML the console
still has. What you cannot recreate:

* Its PersistentVolumeClaims, if the reclaim policy deleted the volumes. The data
  is gone. Not "gone from the cluster" — gone.
* The ordinal identity of pods mid-rolling-update, and whatever quorum a
  clustered database had while they existed.
* The window itself. A StatefulSet running a primary database node that is
  deleted at 14:02 and recreated at 14:03 has still taken an outage, and
  "undone" does not describe what happened to the traffic in between.

The same holds for the drain button (pods are evicted; the workload's own
availability is what absorbed it), for a scale-to-zero (the connections are
closed), and for a rollback (the intermediate state existed and clients saw it).

An undo affordance in this console would therefore be a **claim it cannot
support**: it would restore an object and imply it had restored a state. Under
this project's standard — *a wrong answer delivered confidently is worse than no
answer* — offering it would be the single worst feature we could build. The
operator who believed it would take bigger risks precisely because they believed
it.

### The diff is cheap, and it is produced by the API server

The usual objection to confirm-first is that it taxes every action to protect
against the rare mistake. Two things make the tax small here:

* `dryRun=All` is one round trip on the same URL with the same body. It runs the
  whole admission chain — mutating webhooks, defaulting, validation, quota — and
  returns the object that *would* have been persisted. It is not a simulation we
  wrote, so it can be trusted about the things that actually surprise people.
* The diff is normalised so it is readable: bookkeeping fields no operator ever
  authors are removed from both sides, so a replica change is two lines rather
  than two lines inside two hundred.

And the tax buys more than mistake-prevention. It buys **`diff.changed: false`**
— the console can tell an operator that their change is a no-op before they make
it, which optimistic-with-undo cannot do at all.

### Dry-run works where undo cannot: read-only mode

Because a projection is a read, dry-run stays available when
`ADMIN_ALLOW_MUTATIONS` is false. An operator with no write permission at all can
still answer "what would this change?" That is a genuinely useful mode — audits,
change reviews, teaching someone what a button does — and it exists only because
the preview step is a first-class part of the flow rather than a safety net
bolted onto a write.

An undo-based design has no equivalent. There is nothing to preview.

## Consequences

**Accepted costs.**

* Every write is two round trips. On a slow API server that is visible.
* Two calls means a window between them. A webhook that changes, a quota that
  fills, or a node that fills in that window can still make the real write behave
  differently from its preview. The window is seconds, not zero, and
  `safety-model.md` §10 says so rather than pretending otherwise.
* The frontend carries more state per action: the pending request, the returned
  diff, the `resourceVersion` it was computed against. `MutationDialog` exists to
  hold that once for every action rather than seven times.
* Bulk actions are awkward. Confirming twenty diffs is not a workflow, so the
  console does not offer bulk mutation. That is a real capability gap and it is
  the price.

**What follows from it.**

* `PUT` carries the `resourceVersion` the diff was computed against, and a
  mismatch is `409 conflict` with a *fresh* diff rather than a blind overwrite.
  Dry-run-first is what makes optimistic concurrency natural here: there is
  already a version the operator was looking at.
* Every write endpoint's `dryRun` defaults to **true**. A caller that forgets the
  parameter gets a preview, not a write. Defaults should fail toward the
  reversible outcome.
* The audit trail records dry runs (`outcome: dry_run`) as well as real writes,
  so "who looked at what this change would do" is answerable — which turns out to
  matter during reviews more often than expected.

## Alternatives considered

**Optimistic with a compensating write.** Store the previous object and offer to
re-apply it. Rejected: it is undo for the object and not for the effect, which is
exactly the false claim described above. It also fails outright for delete, which
is the case that most needs it.

**Confirm dialog without a diff** ("Are you sure you want to scale checkout?").
Rejected as security theatre. It asks the operator to confirm the sentence they
just typed, not the change the cluster will make — and it cannot catch the
mutating webhook that rewrites the object, which is the class of surprise a
console is uniquely placed to show.

**Diff computed client-side.** Rejected. A client-side diff misses every
admission webhook in the cluster and every default the API server applies, so it
is confidently wrong in precisely the cases where the preview was worth having.
