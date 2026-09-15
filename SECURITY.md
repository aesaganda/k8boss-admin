# Security policy

k8boss-admin is a console that writes to production Kubernetes clusters. A
vulnerability here does not stay contained to this application — it can turn
into an unauthorized write, an RBAC bypass, or a leaked cluster credential on
every cluster an instance is registered against. Report privately and give us
a chance to ship a fix before any detail becomes public.

## Reporting a vulnerability

**Preferred: GitHub Security Advisories.** Open one from this repository's
[Security tab → Report a vulnerability](../../security/advisories/new). It is
private by construction — nobody but you and the maintainer sees it until it
is disclosed — and keeps the report, the fix, and the eventual CVE in one
place.

**Fallback:** if you cannot use GitHub's flow, email
**erensaganda@gmail.com** with `[SECURITY]` in the subject. Encrypt with PGP
if the report itself is sensitive enough to warrant it; say so in the email
and we'll arrange a key.

Please do not open a public issue, discussion, or pull request for a
suspected vulnerability before it has been triaged.

### What to include

- The endpoint, module, or UI flow involved (`app/admin/mutate.py`,
  `app/k8s/impersonation.py`, a specific SSO provider under `app/identity/`,
  etc. — a file/line reference if you have one)
- Whether the issue requires an authenticated session, a specific RBAC grant,
  or a specific feature flag to be enabled (`ADMIN_ALLOW_MUTATIONS`,
  `ADMIN_OLM_INSTALL_ENABLED`, …)
- A minimal reproduction — a `curl` sequence or a short test against the fake
  Kubernetes client in `backend/tests/conftest.py` is ideal, since it needs no
  real cluster
- The impact as you see it: what a successful exploit lets an attacker do,
  and against whom (another tenant's cluster, the console's own database,
  another operator's session)

## Scope

**In scope** — this is what the project asks you to look hard at:

- Anything that lets a write reach a cluster **without** going through
  `backend/app/admin/mutate.py`'s five steps (gate, preflight, apply, diff,
  audit) — see [`docs/safety-model.md`](docs/safety-model.md)
- A preflight (`SelfSubjectAccessReview`) that can be bypassed, cached, or
  made to say `allowed: true` when the API server would say no
- A dry-run (`dryRun=All`) that is not the same code path as the real write,
  or a response that reports `applied: true` without `dry_run: false` and a
  succeeded call
- Optimistic-concurrency bypass — an update that lands without the
  `resourceVersion` check, or a `409 conflict` that doesn't actually block
  the write
- Gaps in the audit trail: a write (especially a failed, denied, or
  conflicting one) that leaves no `AuditRecord`, or a way to make the
  hash chain in `backend/app/audit/` accept a modified history as valid
- Anything in `backend/app/identity/` — local password hashing, session
  handling, LDAP bind, and the four SSO providers (OIDC, OAuth 2.0, the
  OpenShift OAuth server, SAML 2.0) — especially signature/assertion
  verification (SAML's XML Signature Wrapping class of bugs is explicitly
  why `signxml` is used instead of a hand-rolled verifier; a bypass there is
  as serious as it gets)
- [ADR-0007](docs/adr-0007-impersonation.md)'s impersonation decision: any
  path where a call meant to act as the signed-in operator instead runs as
  the console's own ServiceAccount, or vice versa
- Encryption of stored cluster credentials (`ENCRYPTION_KEY` handling) and
  the Secret-reveal gate (`SECRET_REVEAL_ENABLED`)
- The YAML dialect (`backend/app/yaml_dialect.py`, `frontend/src/components/clusterYaml.js`)
  — since it is hand-rolled resolver behavior rather than a stock parser,
  a scalar that resolves differently than intended is a real bug class here
- Anything that lets a session, cluster, or namespace boundary be crossed —
  reading or writing a cluster the caller was never granted

**Out of scope** — please don't spend time on these here:

- Whether a NetworkPolicy is actually *enforced* on the wire. §8.3's
  correlation is a listing subtraction, not an enforcement check, and the
  README says so; that limitation is documented behavior, not a bug
- A user with a genuinely broad RBAC grant (or the console's own
  ServiceAccount, when impersonation is off) doing genuinely destructive
  things they were authorized to do — `force` on a drain, deleting a
  namespace after reading what goes with it, etc. The safety model is about
  **not doing things nobody confirmed**, not about limiting what a properly
  authorized operator can do to their own cluster
- Denial of service against a cluster you already have write access to
- Vulnerabilities in a third-party dependency with no additional impact
  through this application (report those upstream — we track and bump pins
  deliberately; see `backend/requirements.txt`'s comments for why some are
  held at an exact version)
- The vendored Operator Lifecycle Manager release under `deploy/olm/` —
  that is upstream's byte-for-byte release, pinned by SHA-256 and verifiable
  yourself; report OLM vulnerabilities to
  [operator-framework/operator-lifecycle-manager](https://github.com/operator-framework/operator-lifecycle-manager)

## Supported versions

This project has not cut a tagged release yet — until it does, only the
`main` branch is supported, and a fix means a merged commit rather than a
backport. Once versioning starts, this section will list which lines still
receive security fixes.

## What to expect

k8boss-admin has one maintainer. Response times are best-effort, not an SLA:

- Acknowledgement of a report: within 5 business days
- An initial assessment (confirmed, needs more information, or not
  applicable) after that: as fast as the report allows — a report with a
  concrete reproduction goes much faster than one without
- A fix, mitigation, or documented decision not to fix, with a timeline
  communicated once triage is done

If you have heard nothing after 7 days, it is fair to follow up on the same
thread — that is a nudge, not a reason to escalate to a public disclosure.

## Disclosure

We ask for coordinated disclosure: please hold public details until a fix is
released or we agree on a timeline together. Credit is welcome and will be
given in the advisory and the release notes unless you ask otherwise.
