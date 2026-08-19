# ADR-0003 — Hash-chained audit records, and why the old ones stay unhashed

**Status.** Accepted.
**Context.** `docs/safety-model.md` §7.1, `docs/api-contract.md` §10.3.

---

## Context

`AuditRecord` was append-only, enforced by a SQLAlchemy session hook that refuses
to flush an update or a delete. That is a real guarantee and it is narrower than
it reads.

It protects the table against *this application*. It protects nothing against a
`psql` session, a restored backup, a volume snapshot edited offline, or anyone
who holds the database credentials the console itself holds. For a table whose
entire value is that it can be trusted at the worst moment, that is close to the
whole threat model.

The concrete failure: an operator scales the payments service to zero during an
incident, then edits the row to name a colleague. The guard never sees it — the
edit does not go through the ORM. The trail reads perfectly. The review reaches a
confident conclusion about the wrong person, which is this project's defect
standard applied to the one artefact that is supposed to settle such questions.

## Decision

**1. Every audit record is hash-chained.** `event_hash` is SHA-256 over the
record's immutable content plus the previous record's `event_hash`. Editing,
deleting, inserting or reordering a committed record breaks the links from that
point on, and `GET /api/audit/verify` reports where.

This *prevents* nothing. It makes tampering detectable, which is the strongest
honest promise an application can make about a table it does not own the storage
of. The distinction is stated in the API contract rather than left for a reader
to infer, because a reader who infers the stronger version stops thinking about
off-box export.

**2. Records written before chaining existed are never back-filled.**

This is the decision the ADR exists for. Back-filling is easy, it makes the
verify endpoint return a satisfying `intact`, and it is wrong: the hash would be
computed over whatever those records say *today*. If one was altered last month,
the chain would then attest the altered version — with a cryptographic signature
attached.

That converts "we do not know whether this was altered" into "this is verified".
It is strictly worse than having no chain at all, because no chain leaves the
uncertainty visible and a back-filled chain hides it behind a green badge. It is
the defect standard — a wrong answer delivered confidently — in its purest form.

So the old records keep null hashes, `verify` counts them as `unchained`, and the
verdict for the whole trail is withheld as `partial`.

**3. `partial` is a first-class verdict, not a softer `intact`.**

`intact` means every record verified and the links run unbroken from the first to
the last. `partial` means no break was found **and** the trail contains records
this mechanism cannot speak for. The API returns them as different values, the UI
renders them as different colours with different sentences, and a test asserts
that the `partial` panel does not contain the `intact` wording.

The same reasoning governs a windowed verification (`?limit=`): it genuinely
checks the newest N records, and it cannot prove the ones before them still link
back to the first, so it is always `partial` with `anchored: false`.

**4. A record that cannot be chained is written unchained, not dropped.**

Under contention the writer may fail to claim the chain tip (see 5). After a
bounded number of retries it writes the record with null hashes and logs at
ERROR. Losing the link costs the ability to prove that one record was not altered
afterwards, and `verify` says so. Losing the record costs the knowledge that the
action happened at all, and nothing anywhere indicates it is missing. The first
loss is visible; the second is not.

**5. Concurrency is arbitrated by a UNIQUE constraint on `prev_hash`.**

Two replicas that read the same chain tip compute the same `prev_hash`. Without
the constraint both INSERTs succeed and the chain silently forks into two
branches — and a forked chain can have an entire arm removed without the
remaining one noticing. With it, the second INSERT is refused by the database and
the writer retries against the new tip.

The alternative considered was an in-process lock, which is what K8Boss's
equivalent module uses and which its own docstring admits forks across replicas.
An engine-enforced constraint holds across replicas, across processes, and across
anything else that writes through this schema. It is also `ADD COLUMN` plus
`CREATE UNIQUE INDEX`, both identical on SQLite and PostgreSQL, so it costs no
engine divergence.

## Consequences

* Every audit write does one extra indexed `SELECT` for the chain tip. Measured
  against the Kubernetes call that dominates any request reaching this code, it
  is not observable — but it is not free, and it made one timing-sensitive
  WebSocket test start failing, which surfaced a latent race in `exec_ws.py`
  worth fixing on its own.
* An upgraded deployment reports `partial` forever, or until its pre-chain
  records age out of whatever retention the operator applies. That is the correct
  reading and the UI explains it rather than treating it as a problem to clear.
* The chain proves records were not altered **after** they were written. It says
  nothing about whether what was written was true: a compromised console writes
  truthful-looking records and chains them correctly.
* **Truncation from the end is not detected, and cannot be by this mechanism.**
  Deleting the newest N records leaves a shorter chain that is internally
  perfect, and `verify` reports `intact` — correctly, because every record it can
  see does verify. A chain proves the *integrity* of what is present; it cannot
  prove that nothing is missing from the end, because nothing in the table says
  where the end should be. Deleting from the *middle* is detected, because the
  records after the gap no longer link back.

  The general form is worse: anyone who can edit the table can drop it, and an
  empty table verifies vacuously. Both are availability problems, and a chain is
  not an availability mechanism.

  What actually addresses them is an anchor the attacker cannot reach: the export
  (§10.4) taken off-box on a schedule, a copy of the tip in a separate system, or
  an external append-only store. The console does not do any of those on its own
  today, and saying so is the point of this bullet — an operator who believes
  `intact` means "nothing has been removed" has been told something false by
  omission.
* `audit_records` gained four nullable columns, which `create_all` will not add
  to an existing table. `app/schema_upgrade.py` adds them and **refuses to start**
  if any is still missing afterwards — the alternative being a console that
  serves every page correctly and records nothing.

## Alternatives considered

**Recording the expected chain length or the tip in a second place.** This is the
missing half of tail-truncation detection, and it is deferred rather than
rejected: done inside the same database it is as removable as the trail, and done
outside it needs somewhere to put it (a second datastore, a periodic export
receipt, a log shipper's high-water mark) that this deployment does not currently
assume. Recording the count in the same table would look like a fix and be none.

**Signing each record with an asymmetric key.** Stronger: a verifier would not
need the console's cooperation, and the console could not forge history even for
itself. Rejected for now because it needs a key-management story this deployment
does not have — a signing key that lives beside the database it protects buys
very little over a chain, and one that does not needs an HSM or a KMS the product
does not currently assume. The chain is a strict improvement available today, and
it does not foreclose signing later: signing the chain tip periodically is the
natural next step.

**Writing the trail to an append-only external store** (a SIEM, an object store
with object lock). Correct, and orthogonal: it addresses availability, which the
chain does not. `GET /api/audit/export` is the first step toward it. Making the
console *depend* on such a store would mean an audit write can fail because a
third-party service is down, and §7 is explicit that a logging fault must never
turn into a failed write.

**Back-filling behind a flag.** Rejected. A flag that produces a chain attesting
possibly-altered records is a flag that will be set by someone who wants the
badge to be green, which is the exact motivation the mechanism must not serve.
