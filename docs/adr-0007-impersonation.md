# ADR 0007 — The console acts as one ServiceAccount, and impersonation is the conditional exception

**Status:** accepted
**Proposed:** 2026-09-03 · **Accepted:** 2026-09-06 by the repository owner
**Supersedes nothing. Constrained by:** `docs/safety-model.md` §8's two gates and
§7's audit trail, both of which this decision changes the meaning of.

**This ADR did not authorise its own implementation, and did not have to.** It
was written to record a decision that had been implicit since the first cluster
was registered, to state what the exception would cost, and to set the conditions
under which it could be built — then to wait. It waited three days. The
implementation landed on 2026-09-06 against the six conditions below, each of
which is now a named test in `backend/tests/test_impersonation.py`.

**What changed, in one paragraph.** `Cluster.impersonation_enabled` is a
per-cluster, off-by-default flag. When it is set, every call this console makes
to that cluster carries `Impersonate-User` and `Impersonate-Group` for the
signed-in operator, so the API server evaluates authorization, admission and its
own audit log as that person. Sessions that cannot supply a cluster identity are
**refused** with a new error code — `impersonation_unavailable`, 403,
deliberately not `rbac_denied` — rather than served as the console. The grant it
needs ships commented out and `resourceNames`-shaped in `deploy/rbac.yaml`.

**Two questions the implementation forced, recorded here because neither was
settled by the text below.** See *What the implementation decided* at the end.

## The decision this ADR records

*Written before the implementation and kept in its original tense, because the
argument is the record. It describes the **default**, which acceptance did not
change: a cluster that has not opted in behaves exactly as described below, and
that is every cluster until somebody sets the flag.*

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

Three consequences follow, and all three are true of every cluster that has not
opted in — which is the default, and was every cluster when this was written:

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

## What impersonation actually changes

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

## The conditions, now met

Each of these was a thing that would have to be true. Each is now a test named
for it; the parenthetical says where it lives.

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

| # | Where it is enforced | The test that would fail |
|---|---|---|
| 1 | `Cluster.impersonation_enabled`, nullable and read through `bool()`; echoed by `to_public_dict`; refused at the form by `_validate_impersonation` when the console has no OIDC | `test_a_cluster_that_did_not_ask_is_not_impersonated`, `test_a_null_flag_is_off_rather_than_a_third_state`, `test_the_flag_is_surfaced_like_skip_tls_verify`, `test_turning_it_on_without_oidc_is_refused_at_the_form` |
| 2 | `impersonation.decide` on `auth_source`, and again on `settings.auth_enabled` for legacy proxy mode | `test_a_password_session_cannot_become_a_cluster_identity`, `test_legacy_proxy_mode_cannot_impersonate`, `test_the_refusal_is_not_rbac_denied` |
| 3 | `decide` on `idp_groups is None`; `_decode_groups` degrades an unparseable blob to absent rather than empty | `test_an_absent_groups_claim_refuses`, `test_an_empty_groups_claim_impersonates` |
| 4 | `decide` raises inside `get_clients` before a bundle exists; `impersonatable` is a property of the transport; `as_service_account` is the only suppression and its call sites are enumerated | `test_a_refused_session_never_receives_a_transport`, `test_a_transport_that_may_never_impersonate_ignores_the_decision`, `test_the_service_account_exemptions_are_exactly_these` |
| 5 | `AuditRecord.impersonated_user` read from request context; `subject` on every `PreflightResult` and in `RBACDenied.message` | `test_the_audit_row_records_the_console_user_and_the_cluster_identity`, `test_a_denial_names_the_subject_it_was_refused_for` |
| 6 | `deploy/rbac.yaml`, commented out, with `resourceNames` on both rules | asserted by reading the file: no live rule carries the `impersonate` verb |

## What the implementation decided

Two questions the text above did not settle. Both are recorded here rather than
only in a docstring, because both are places where an implementer could
reasonably have gone the other way.

### `system:authenticated` is sent, and the issuer did not state it

The API server attaches `system:authenticated` to every request it authenticates
itself, and **does not** attach it to an impersonated one. Sending only the
issuer's groups therefore strips every grant bound to that group — including the
discovery rules on a default cluster — so the operator is shown accurate denials
for permissions they demonstrably hold. The reliable end of that is a ClusterRole
widened to fix a problem that was never RBAC.

This sits close to *"Impersonating groups the console derived rather than
received"*, which is rejected above, and the distinction is worth stating.
That rejection is about `roles.py` — the console's **opinion**, its mapping of a
group to its own `admin` role, which must never reach an authorization decision.
`system:authenticated` is nobody's opinion: it is what the API server itself
would have attached had the operator presented the same token directly, and the
whole claim this feature makes is that the console asserts an identity the
cluster would have derived itself. It is the one group sent that the issuer did
not state, it is a named constant, and `deploy/rbac.yaml`'s commented grant lists
it first so the `resourceNames` set matches what is actually sent.

### The audit column is hashed only when it is set

`AuditRecord.impersonated_user` is **not** appended to `HASHED_FIELDS`. The
verifier recomputes a stored row's hash from that row's own columns, so appending
a name there changes the computation for every row ever written — including every
row written before the column existed, whose stored digests were computed without
it — and the whole table verifies as `broken`. That is a false "this row was
modified" alarm delivered by a schema change, which is precisely what ADR-0003
refuses to manufacture and what `_canonical` exists to prevent.

So it lives in `OPTIONAL_HASHED_FIELDS`: a key in the payload when the value is
not None, and no key at all when it is. That is not a hole. Every mutation of the
field crosses the boundary — `NULL` → a name adds a key the stored digest did not
cover, a name → `NULL` removes one, one name → another changes its value — and
all three recompute to something other than what is stored. What the omission
costs is the ability to distinguish "written before the column existed" from
"written by a console acting as itself", and those are the same fact about
attribution: the API server saw the ServiceAccount.

## The boundary

> The console has **one credential per cluster** and its own users are console
> users, not cluster identities. Impersonation is a per-cluster opt-in that
> requires a shared issuer, refuses rather than falls back, and never sends this
> application's opinion about somebody's groups to an API server.

That is unchanged by acceptance: it is now the description of a feature rather
than of a hypothetical one. The default is still one credential per cluster, and
§17 still asks for the subject — because the cluster it is creating a project on
may well be one that does not impersonate.

**What acceptance does not do.** It does not make impersonation the recommended
posture, and it does not make the wildcard writer role safe. A deployment that
has kept that rule gains attribution from this feature and no containment; a
deployment that deleted it gains both, and is the one the narrow grant was
written for.

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
- `docs/api-contract.md` §27 — the normative shape: the flag, the error code, the
  `subject` on a preflight result and the second name on an audit row.
- `docs/safety-model.md` §18 — what changes about the preflight, the audit trail
  and the refusal once a cluster acts as the operator.
- `backend/app/k8s/impersonation.py` — the decision, the headers, and the one
  suppression.
