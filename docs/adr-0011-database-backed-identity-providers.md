# ADR 0011 — identity providers are configured from the console, one per kind

**Status:** accepted
**Date:** 2026-09-18
**Amends:** §12.4's sentence — *"Several concurrent providers **of the same kind**
is a real design change — a table, a CRUD surface, per-row encrypted secrets and
a subject-collision story across issuers — not a config key, and the console says
it does not do that rather than half-doing it."* That sentence stands. What
changes is narrower than it sounds, and the difference is the whole ADR.

**Decided with the argument against it intact.** The product owner asked for the
providers to be configurable from the console after being shown §12.4's refusal
and the reasoning behind it, and chose to proceed. The costs below are what was
weighed, not what was discovered afterwards.

## The decision

A provider's configuration can be written from the console, into a new
`identity_providers` table, **one row per kind** — `kind` is unique, so there is
at most one LDAP directory, one OIDC issuer, one OAuth server, one OpenShift
server and one SAML provider. The `LDAP_*` / `OIDC_*` / `OAUTH_*` /
`OPENSHIFT_*` / `SAML_*` environment variables **remain**, as the fallback for a
kind with no row.

Resolution, in `app/identity/provider_config.py`:

1. a row, **including a disabled one** — that is this deployment's decision about
   that kind;
2. otherwise the environment.

Deleting a row falls back to the environment rather than to "off".

Secret fields — the four client secrets and the LDAP bind password — are stored
in one Fernet blob per row, with the same key as cluster tokens, and are returned
by nothing. The SAML IdP certificate is *not* a secret: it is a certificate, and
it is stored in the clear like a cluster's CA.

## What §12.4 refused, and why this is not it

§12.4's four costs, taken one at a time:

**A table.** Yes. That is a real cost and it is paid: a schema, a migration path,
a second place a provider can be configured. Mitigated by the fallback — a
deployment that never opens the screen behaves exactly as it did, and the table
stays empty.

**A CRUD surface.** Yes, and it is administrator-only, audited on every terminal
state including the refusals, and validated at the write rather than at somebody
else's next sign-in.

**Per-row encrypted secrets.** Yes, and this is the part that is genuinely new
risk. It is the same risk the console already carries for cluster bearer tokens,
with the same key, the same module, and one failure mode made explicit rather
than inherited: a secret that no longer decrypts is reported as **unreadable**,
never as configured, because "the password is set" sends an administrator to
debug the directory while the actual fault is the encryption key.

**A subject-collision story across issuers.** **This one does not arise**, and it
is the reason this decision is defensible at all. The collision §12.4 named is
two issuers asserting the same `sub` onto one account — which requires two
providers of the same kind. `kind` is unique in the table, so a second OIDC
issuer cannot exist to collide with the first. Account binding
(`provision_federated_user`) is untouched: one `auth_source` per account, one
subject per row, refusals unchanged.

So the design change §12.4 refused was *N providers per kind*. What this ADR
allows is *one provider per kind, configured in a second place*. The one-of-each
constraint is not an implementation convenience — it is the load-bearing half of
this decision, enforced by a database constraint rather than by a check somebody
can move.

## Why

The console could browse every resource a cluster serves, list every session,
name every account and its `auth_source` — and the answer to "why can nobody
from the directory sign in" was: read the container's environment, edit a
Compose file you may not have, restart the deployment. On a console whose entire
argument is that an operator should be able to see what is true and change it
with the consequences in front of them, identity was the one thing that could
only be changed from outside.

The narrower version of that: **an operator looking at the Users page could not
find out which directory an `ldap` account came from, or which group made it an
administrator.** Those two values decide who administers this console, and they
were invisible to the screen that lists administrators.

## What this costs

**Two places to look.** A provider can now be configured in the environment or
in the database, and an operator who does not know which is in effect is one
step worse off than before. Paid down deliberately: every read reports
`source`, every card says "configured in this console" or "in the environment
(`LDAP_*`)", and every refusal's hint is source-aware — a console that told
somebody to set `OIDC_ISSUER` while a stored row was in effect would be sending
them to change something that changes nothing.

**A write path that decides who you are.** This is the sharpest cost. Until now,
changing how the console authenticates required access to the deployment; it now
requires the `admin` console role. An administrator who can already create
administrators and revoke sessions is not a meaningful escalation — but the
blast radius of one mistake is larger than any other screen's, because pointing
the issuer somewhere else is pointing the front door somewhere else. What stands
against it: the role gate, validation at the write, an audit row for every
attempt including the refused ones, and the local-account path that
`ensure_bootstrap_admin` guarantees, which cannot be switched off from this
screen and is what makes a misconfiguration recoverable without a database
client.

**Secrets in the console's database.** Discussed above. A deployment that
prefers not to hold them can simply never use the screen; the environment path
is unchanged and is still what the README documents first.

**A cache nobody is allowed to add.** `resolve()` reads the row on every sign-in
and on every load of the public discovery endpoint. That is a primary-key lookup
on a table with at most five rows, and it stays uncached for the same reason
§0.2 forbids caching an access review: a cached provider configuration outlives
the edit that changed it, and an administrator who switches a directory off and
watches somebody sign in through it thirty seconds later has been lied to by a
cache.

## What was considered and not done

**Replacing the environment variables entirely.** Rejected: a fresh deployment
would then start with no way to sign in but the bootstrap local account, the
~60 lines of provider variables in `docker-compose.yml` would have to go, and a
deployment upgrading into this release would lose its configured identity
provider at the moment it restarted. The fallback costs one sentence per screen
and keeps every existing deployment working.

**A "test connection" button.** Not done. It is the obvious next request and it is
a different feature: a bind attempt from the console with credentials that have
just been typed, whose failure modes (a timeout against a firewalled host, a
partial TLS handshake) need their own error vocabulary. Validation at the write
covers the misconfigurations that are checkable without reaching the network,
which is what the console can honestly promise today.

**Several providers of one kind.** Still refused, and §12.4's paragraph is left
in place as the reason. If it is ever wanted, this ADR is what has to be
re-read: slug-based routes, handshake cookies keyed per row, accounts bound to
`(provider, subject)` rather than to a subject, and an answer for two issuers
asserting one name. None of that is groundwork this ADR lays — it deliberately
does not.
