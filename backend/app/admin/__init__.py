"""
The write side. Every change this console makes to a cluster goes through here.

Six modules, and the shape of the package is the argument:

* :mod:`app.admin.preflight` — ``SelfSubjectAccessReview`` (§9). Asks the cluster
  whether we may, before we try, so a denial names the missing grant instead of
  relaying a bare "forbidden".
* :mod:`app.admin.diff` — the unified diff (§1.5) and its digest, with the API
  server's bookkeeping normalised out of both sides so the operator sees their
  own change.
* :mod:`app.admin.mutate` — **the single write funnel**. Mutations gate,
  preflight, apply, diff, audit, in that order, once.
* :mod:`app.admin.apply` — create / replace / delete of any resource, with
  ``dryRun=All`` and mandatory optimistic concurrency on replace.
* :mod:`app.admin.scale` — the scale subresource, ``rollout restart`` and
  ``spec.suspend``.
* :mod:`app.admin.rollout` — revision history and rollback.

``apply``, ``scale`` and ``rollout`` are *callers* of ``mutate``; none of them
writes to a cluster itself. That is the whole design. §0 promises four things
about every mutation — preflighted, dry-runnable, diffed, audited — and four
promises spread across a dozen endpoints is four promises that will each be
forgotten once. Here they are one function, and an endpoint that wanted to skip
them would have to be written to bypass this package visibly.

Nothing is imported eagerly: ``app.api.resources`` imports ``app.admin.apply``
at module scope, and a package ``__init__`` that pulled in the whole write side
would make importing one endpoint import the Kubernetes discovery layer.
"""
