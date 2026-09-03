# ADR 0007 — The console acts as one ServiceAccount, and impersonation is the conditional exception

**Status:** proposed
**Date:** 2026-09-03
**Supersedes nothing. Constrained by:** `docs/safety-model.md` §8's two gates and
§7's audit trail, both of which this decision changes the meaning of.

**This ADR does not authorise an implementation.** It records a decision that has
been implicit since the first cluster was registered, states what it costs, and
sets the conditions under which the exception may be built. Nothing in the tree
implements impersonation today and nothing here says to start. Status stays
`proposed` until somebody with the deployment context in front of them accepts
or rejects it; an ADR that said `accepted` would be this document commissioning
its own feature.

## The decision, as it stands today

**Every call k8boss-admin makes to a cluster is made as that cluster's single
registered credential.** `Cluster.token_encrypted` holds one bearer token,
`app/k8s/client.py::build_configuration` puts it on one `Configuration`, and
`app/k8s/client.py::_cluster_api_client` hands that to every typed and dynamic
client for the lifetime of the request. There is no per-user path through it.

The console's own identity — local password, LDAP bind, or OIDC single sign-on
(§12) — decides **whether you may reach the console** and, through
`app/identity/roles.py`, whether you are one of its two roles: `admin`, which
gates user administration and the audit export, or `user`. It decides nothing
about the cluster. Two operators signed in with different console roles have
*identical* power over every registered cluster, because the cluster only ever
sees the ServiceAccount.

Three consequences follow, and all three are already true and already documented:

* **Preflight answers about the console, not about you.** §0.2's
  `SelfSubjectAccessReview` asks "may *this credential* do this", which is the
  question that decides whether the write succeeds — so the answer is correct.
  It is correct about the wrong subject.
* **The audit trail's actor is the console's, not the cluster's.**
  `AuditRecord.actor` comes from `app/k8s/context.py::get_current_user`, which is
  the verified session username when §12 auth is on and the advisory
  `X-K8Boss-User` when it is off. The *cluster's* own audit log, meanwhile,
  records the ServiceAccount for every write this console makes. Two trails, one
  of which cannot name a person, and correlating them means trusting the
  console's.
* **§17 binds a subject the operator types.** `oc new-project` binds the
  requester because OpenShift knows who the requester is. §17 asks, because this
  console does not.

## What impersonation would actually change

Kubernetes has a first-class answer: `Impersonate-User`, `Impersonate-Group`,
`Impersonate-Uid` and `Impersonate-Extra-*` request headers. The API server
authenticates the ServiceAccount, checks it holds `impersonate` on the requested
subject, and then evaluates **the whole request** — authorization, admission,
and its own audit record — as the impersonated user.

That is not a partial fix. It is the fix:

* `SelfSubjectAccessReview` under impersonation answers for the operator. Every
  §11.4 disabled button, every `rbac_denied` naming a missing grant, becomes a
  statement about the person reading it.
* The cluster's audit log names both parties — impersonator and impersonated —
  so "who scaled the payments service to zero" is answerable from the cluster,
  by someone who does not trust this console at all. That is a materially
  stronger claim than ADR-0003 can make about a table this application owns.
* §17's RoleBinding could bind the requester, and §18's Pod Security patch would
  be refused for an operator whose own RBAC does not permit it, rather than
  succeeding because the console's does.
* An organisation that has spent real effort on cluster RBAC would get that
  effort back through this console instead of having it flattened to one
  credential.

## What it costs, stated rather than argued away

**`impersonate` on `users` with no `resourceNames` is cluster-admin by proxy.**
An account that can impersonate any user can impersonate the most powerful user
on the cluster, so the grant replaces a deployment's blast radius — "whatever
`deploy/rbac.yaml` grants" — with "everything anybody on this cluster can do".

**This argument is weaker against the manifests as this repository currently
ships them, and that is worth stating rather than hiding.** `deploy/rbac.yaml`
opts in: the writer ClusterRoleBinding is applied, and the writer ClusterRole
carries a wildcard `apiGroups: ["*"] / resources: ["*"]` write rule. Its own
header says what that means in plain words — cluster-admin wearing a different
name, write over RBAC itself, and therefore a privilege-escalation path for
anyone who can reach the console's port. An account that can write RBAC can
already grant itself anything; adding `impersonate` to *that* ServiceAccount
would add attribution, not power.

So the blast-radius objection is really an objection on behalf of the other
deployment — the one the same file invites, with the wildcard rule deleted and
the console left holding only the typed verbs this contract's features actually
use. That deployment has a genuinely bounded ServiceAccount, and an unrestricted
impersonation grant would unbound it in one line. Any implementation is designed
for that deployment, because the opted-in one has already made the trade and does
not need protecting.

`resourceNames` narrows the grant to a listed set of users or groups, and that is
the only form worth proposing. It is also a list somebody has to maintain, and an
operator missing from it cannot use the console at all — a failure mode that
looks like a broken console rather than a missing grant, unless the console is
careful to say which it is.

**The console's identity provider becomes a cluster credential issuer.** This is
the part that decides the shape of the exception, and it is easy to miss.

If a local account in this console's own password table can cause a request to
arrive at the API server as `alice`, then `SECRET_REVEAL_ENABLED` and password
policy and the sign-in throttle are no longer protecting a console — they are
protecting the cluster's authorisation model, and this application's user table
has quietly become an identity provider the cluster trusts. Nothing in the API
server checks that the name in `Impersonate-User` corresponds to anyone real; it
checks only that the impersonator is permitted to say it.

The same objection applies with less force to LDAP and with least force to OIDC,
and the difference is whether the cluster and the console are believing *the same
issuer*. Where a cluster's `--oidc-issuer-url` is the issuer the console
authenticates against, the console is asserting an identity the cluster would
have derived itself from the same token; that is a defensible mapping. Where they
differ — or where there is no cluster OIDC at all — the console is inventing one.

**Groups are where the RBAC actually lives, and this console keeps them
carefully.** `app/identity/oidc.py::identity_from_claims` distinguishes an
**absent** groups claim from an **empty** one, on the stated grounds that an
issuer which did not tell us must not demote anybody. Under impersonation that
distinction stops being about console roles and becomes load-bearing on the
cluster: impersonating a user with an empty group list, when the issuer merely
omitted the claim, would silently strip every group-derived permission the
operator actually holds — and produce a page full of correctly-reported denials
for permissions they have. The existing tri-state is exactly the right shape for
this and would have to be honoured: **absent groups means impersonation cannot
proceed**, not that it proceeds with none.

**It splits the console in two, operationally.** Some calls cannot be
impersonated and must stay as the ServiceAccount: reading the cluster list,
discovery caching shared across users, `/api/health`. Others must be
impersonated or the feature is a lie. Getting that split wrong in either
direction is worse than not having the feature — a read that quietly falls back
to the ServiceAccount would show an operator data their own RBAC forbids, which
is this project's defect standard with a security consequence attached.

## What is rejected

**Impersonation on by default, or as a global switch.** The grant it needs is
too large to be a default and too cluster-specific to be one flag for every
registered cluster. A deployment with one cluster whose OIDC matches the
console's and a second cluster with no OIDC at all must be able to have this on
for the first and off for the second.

**Impersonating from local or LDAP sessions.** Per the argument above: the
console's own password store must not become a cluster credential issuer. This
is the condition that keeps the exception honest, and it is the one most likely
to be argued away later by someone who wants the feature on a cluster that has no
OIDC. The answer there is to give the cluster an issuer, not to give the console
one.

**Impersonating groups the console derived rather than received.** The console
maps a group to its own `admin` role through `roles.py`; that mapping is the
console's opinion. Sending it to the API server as `Impersonate-Group` would put
this application's opinion into a cluster's authorisation decision. Only groups
as the issuer stated them.

**Dropping the ServiceAccount model.** Even with impersonation, the console needs
a credential of its own for the calls above, and the two-gate model in §8 stays:
`ADMIN_ALLOW_MUTATIONS` is a property of the deployment and is unaffected by who
is signed in. Impersonation narrows the RBAC gate, not the mutations gate.

**Recording the impersonated subject in the audit trail without impersonating.**
It was considered — write `actor` and an `impersonated` column and change
nothing else — and it is worse than the current state. A trail that names a
subject the cluster never saw reads as attribution and is a claim this
application cannot support, which is precisely the pattern ADR-0003 refuses when
it declines to back-fill pre-chain records.

## The conditions, if this is ever accepted

For whoever picks this up. Each of these is a thing that would have to be true,
not a suggestion:

> 1. **Per-cluster, opt-in, off by default**, stored on the `Cluster` row beside
>    `authentication_type` and surfaced in every `ClusterPublic` response the way
>    `skip_tls_verify` already is — a setting that can never be silently in
>    effect.
> 2. **OIDC sessions only.** A local or LDAP session on an impersonating cluster
>    gets a refusal naming the reason, not a fallback to the ServiceAccount.
> 3. **Absent groups refuses.** An OIDC identity whose groups claim was absent
>    cannot impersonate; empty is fine, unknown is not.
> 4. **No silent fallback, anywhere.** Every call is either impersonated or
>    documented as one of the named ServiceAccount calls. A request that could
>    not build impersonation headers fails; it does not proceed as the console.
> 5. **The audit row records both**, and the response says which identity the
>    preflight answered for, so §11.4's disabled button can say *whose*
>    permission is missing.
> 6. **`deploy/rbac.yaml` ships the grant commented out and `resourceNames`-shaped**,
>    named in that file's own "what is live in this file" header the way every
>    other opt-in there is, with a comment saying plainly that an unrestricted
>    `impersonate users` is cluster-admin by proxy.

## The boundary

> The console has **one credential per cluster** and its own users are console
> users, not cluster identities. Impersonation, if it is ever added, is a
> per-cluster opt-in that requires a shared issuer, refuses rather than falls
> back, and never sends this application's opinion about somebody's groups to an
> API server.

Until then, the honest position is the one §17 already takes: the dialog asks
for the subject rather than pretending to know it, and `docs/safety-model.md` §14
says the console's user is not a cluster identity. **The gap is documented, not
papered over** — which is the only defensible state for a gap this size.

## See also

- `docs/safety-model.md` §8 — the two gates, and why RBAC and the mutations gate
  are independent. Impersonation changes what the first one is about.
- `docs/safety-model.md` §7 — the audit trail, and §14's admission that the actor
  is the console's.
- `docs/adr-0003-audit-hash-chain.md` — tamper evidence versus tamper prevention,
  and the standard this ADR applies to attribution.
- `docs/adr-0006-projects.md` — where the gap was named as the next architectural
  step, and why §17 asks for the subject.
- `docs/api-contract.md` §12 — what the console's own identity is and is not.
- `docs/rbac.md` — every permission by feature, and the ServiceAccount they all
  belong to.
