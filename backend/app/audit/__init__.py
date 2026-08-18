"""
The audit trail (§10): every attempted write, including the ones that failed.

One module, :mod:`app.audit.recorder`, with two functions — ``record`` and
``query`` — over the append-only :class:`app.models.AuditRecord`.

Two decisions are worth stating at package level, because both are the kind that
look like oversights until they are written down.

**Denials and failures are recorded, not just successes.** A trail of successful
writes answers "what changed". The question actually asked after an incident is
"who tried", and a trail that drops the 403s cannot answer it. Every terminal
state of :func:`app.admin.mutate.mutate` lands here: ``applied``, ``dry_run``,
``denied``, ``failed``, ``conflict``.

**The trail is best-effort at the point of writing, and says so.** By the time a
real write is audited it has already reached the cluster, so a failed INSERT
cannot un-apply it. Raising here would report a failure for a change that
happened — the worst possible direction to be wrong in. So ``record`` logs at
ERROR and returns ``None``, and the mutation response carries ``auditId: null``,
which is the signal that the change is real and its record is not.
"""
