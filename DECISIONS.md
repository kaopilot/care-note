# Decision Log

Running record. Append; never rewrite. If a later phase deviates from a
decision here, add a new entry saying so and why — don't silently drift.

Format: `D-nnn` · date · decision · reasoning · what it costs us.

---

## Phase 0 — 2026-08-25

### D-001 · Stack: FastAPI + SQLAlchemy + SQLite, React/Vite/Tailwind
The recommended default, adopted without substitution.

FastAPI's dependency injection is not incidental here — it is the mechanism
that makes the RBAC boundary un-forgettable (see D-003). That alone justified
it over Flask or Django.

**Cost:** SQLite means no true concurrent-write testing at the DB level. Phase
2.7's optimistic locking is application-level, which is what we'd want anyway,
but we cannot demonstrate row-level lock contention.

### D-002 · Authentication is deliberately minimal
JWT with `role` and `clinic_id` claims, seeded users, no signup or SSO,
PBKDF2 rather than argon2 (stdlib, one fewer dependency).

The brief grades *authorisation*. Building real authentication would consume
hours and earn nothing on the rubric.

**Cost:** Not production-shaped. No refresh tokens, no revocation, no rotation.
Documented in ARCHITECTURE.md rather than hidden.

### D-003 · RBAC fuses role and clinic into one inseparable dependency
`require_access(*roles)` yields an `AccessScope`, never a `User`. `AccessScope`
is also the only database handle a route receives, and its `query()` applies the
clinic filter before returning anything. Models lacking `clinic_id` raise
`TypeError` rather than being returned unfiltered.

The shared context requires the two checks never be separable. Enforcing that by
convention is fragile — the failure mode is silent, and it is exactly the bug
that leaks one clinic's records to another. Making the unscoped path
*non-existent* rather than merely discouraged is the difference between a rule
and a guarantee.

**Cost:** Slightly more ceremony than `db.query(Model)`, and every clinic-scoped
model must carry a denormalised `clinic_id`. Both are cheap; the property is not.

### D-004 · Staff CANNOT view `clinician_sections` — least privilege
*(Flagged in the shared context as a judgment call requiring explicit record.)*

The brief states clinicians can view `staff_notes` but is silent on the reverse.
Where a permission is unstated in a system holding medical records, the safer
default is to deny. Clinician sections contain differential diagnoses and
speculative reasoning that is written for a clinical reader.

**Cost:** May be more restrictive than a real clinic wants — nurses often
legitimately need the plan. Mitigated because `patient_instruction` and
`patient_summary` are clinician-authored and staff-visible, so the *actionable*
output of a clinician's thinking reaches staff; only the raw reasoning does not.
Easy to reverse: one line in `policy.VIEWABLE_TYPES`.

### D-005 · Staff CAN view AI-scribed notes
The brief grants clinicians "all AI-scribed notes" and is silent on staff. We
allow it, contra the D-004 default, because staff action the follow-ups those
summaries generate ("needs lab order", "waiting nurse follow-up"). Withholding
the source of a task while assigning the task would make the product worse at
its stated job.

Noting the tension openly: D-004 denies where silent, D-005 permits where
silent. The distinguishing question is whether the content is *decision-support
reasoning for a clinician* (deny) or *a record of what happened that another
role must act on* (permit).

### D-006 · Versions store full snapshots, not diffs
The brief leaves this to us.

Revert becomes a copy rather than a replay of an inverse patch chain — far
harder to get subtly wrong, and revert correctness is directly graded.
"View changes since X" is computed on read with `difflib`, which is cheap
because entries are prose-sized, not document-sized.

**Cost:** Storage grows with edit count. Irrelevant at prototype scale;
addressed at real scale by the decay policy (D-009).

### D-007 · Conflict rule: clinician precedence AND a review flag
The brief allows either. We do both, and the reason is the whole product thesis.

When a clinician edit conflicts with prior AI or patient memory, the clinician's
content wins immediately — the record is never blocked on a resolution workflow
mid-consult. But `Entry.conflict_flagged` is set and `supersedes_entry_id`
records what was overridden, so the disagreement stays visible rather than being
silently resolved.

Precedence alone loses information: the fact that the AI said something
different is *itself* clinically interesting, and quietly discarding it is how a
system trains its users to stop trusting it. Flagging alone blocks care on
paperwork.

### D-008 · Provenance pointers are string URIs, not foreign keys
Targets are heterogeneous — a whole entry, a character span inside one, a turn
in an AI session, a diarised audio segment that is not a row in this database.
One resolvable grammar covers all of them; a foreign key covers one table.

**Cost:** No referential integrity from the DB. Mitigated by making `resolve()`
the sole dereference path, raising on dangling or out-of-range pointers, and
enforcing `clinic_id` inside it so a valid pointer never becomes a
cross-tenant read primitive.

### D-009 · Decay lifecycle modelled in Phase 0, implemented in Phase 4
`Entry.decay_state` (`hot`/`warm`/`cold`) and the `EntryArchive` table exist
now. Putting the column in from the start means the Glance View scorer can read
it from the moment that scorer exists — decay becomes a policy question rather
than a migration.

Safety constraint: an entry is never eligible for `cold` while it has an
unresolved task, an open comment, or an accepted highlight. Old ≠ unimportant,
and an outstanding action is the clearest signal that something still matters.

### D-010 · LLM stub provider is the default
`CARENOTE_LLM_PROVIDER=stub` unless explicitly overridden.

A reviewer must be able to clone the repo and have every test pass with no API
key and no network. Tests that depend on a live non-deterministic model are
tests that fail for the person grading them.

**Cost:** The stub does not produce real summaries, so summary *quality* is not
demonstrated by the test suite — only pipeline correctness. Phase 2.2 will run
the live provider for the demo and record which entries were generated that way.

### D-011 · Admin is oversight, not authorship
Admin reads everything within its clinic and writes no clinical content
(`WRITABLE_TYPES[ADMIN]` is empty).

The brief says "clinic-scoped oversight". Read-only oversight is a stronger
guarantee than read-write oversight: it means no admin account can quietly alter
a clinical record, which is precisely the integrity property an audit role
exists to provide.

**Cost:** If a demo scenario needs an admin to fix something, it can't. Acceptable.

### D-012 · Redaction is regex + gazetteer, not NER
The data is synthetic, so recall against real-world name diversity is not what's
being tested — the presence and un-bypassability of the boundary is. A regex
pass is auditable line by line, which is worth more in a trust system than F1
from a model nobody can inspect. Production would layer NER behind the same
`redact_phi` signature, changing no downstream code.

**Cost:** Real, and stated in ARCHITECTURE.md rather than hidden — bare
lowercase names in prose, unusual/transliterated names, and quasi-identifier
combinations are all missed. One test asserts the gazetteer limitation
explicitly so the gap is visible in the suite, not just the prose.

### D-013 · Fail closed on residual PHI
After redaction, `llm_client` re-scans and raises `PHILeakError` rather than
sending if unambiguous PHI survived.

A leak that happens silently is worse than a request that fails loudly. The
scan uses only unambiguous patterns (NRIC, MyKad, email, phone) because a false
positive here blocks a legitimate call.

### D-014 · Logging hygiene enforced structurally, not by convention
`log_event()` has no `message`/`content` parameter to reach for, and scrubs
oversized or suspiciously-keyed metadata values before emitting.

The shared context notes one careless `print()` defeats the redaction boundary.
Making the careless call *inexpressible* in the logging API is more durable than
a code-review rule.

---

## Open questions carried into Phase 1

- Should `patient_summary` be clinician-authored only, or AI-drafted and
  clinician-approved? Currently clinician-writable only. AI-drafted +
  approval gate is more useful and fits the trust thesis better — revisit when
  the AI scribe pipeline exists in Phase 2.2.
- Highlight spans are stored against `source_version_number`. Behaviour when the
  underlying entry is edited beneath a highlight is undefined; Phase 2.6 must
  decide whether to re-anchor, orphan, or invalidate. Leaning toward marking the
  highlight stale and surfacing that, since silently re-anchoring a highlight
  onto text nobody confirmed would be a trust violation.
