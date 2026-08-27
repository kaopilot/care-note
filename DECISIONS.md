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

---

## Phase 0 addendum — security posture review (2026-08-25)

Added after review flagged two areas the original Phase 0 left silent. Both were
genuine omissions in the phase plan, not just in the write-up.

### D-015 · Stored XSS: never render untrusted content as HTML; do NOT sanitize on write

This is a rich-text, multi-author, long-lived note system whose content crosses
privilege boundaries — a staff note surfaces in a clinician's Glance View. Stored
XSS is the natural vulnerability class and no phase doc mentioned it.

Controls, strongest first:
1. Untrusted content is never rendered as HTML. React escapes text children, so
   a stored `<script>` is inert. A source scan fails the build if
   `dangerouslySetInnerHTML` / `innerHTML =` appears, and a second test fails if
   a Markdown renderer is added without review.
2. `sanitize_for_storage()` strips control characters, NFC-normalises, caps length.
3. `find_injection_markers()` flags suspicious payloads as metadata.

**The deviation:** we deliberately do *not* HTML-escape or tag-strip on write,
contrary to the usual advice. Clinical prose legitimately contains angle
brackets — `BP <120/80`, `dose <5mg`, `sats <92% on RA`. Escaping on write
stores `BP &lt;120/80`, which React escapes again on render, showing a literal
`&lt;`. Tag-stripping is worse: `<5mg` can be consumed entirely, silently
turning a dose limit into `mg`.

Silently altering the text of a clinical note is a patient-safety bug, and a
worse one than the XSS it would defend against — control #1 already neutralises
the XSS, whereas nothing catches a corrupted dose. Escaping belongs at the
render boundary, and `escape_html()` is provided for surfaces that genuinely
emit HTML (PDF export, emailed summaries).

**Cost:** protection is at the render boundary and the frontend scan, not the
database. A future non-React API consumer that renders content as HTML without
calling `escape_html()` would be vulnerable.

**Constraint on Phase 2:** any Markdown renderer must be configured with raw
HTML disabled (`html: false`). A test enforces that this decision is revisited
rather than drifted past.

### D-016 · JWT: httpOnly cookie, 60-minute TTL, no refresh flow

Previously "JWT with a role claim" and a silent 12-hour default — underspecified
on expiry, refresh, and client-side storage.

- **Storage: httpOnly cookie** (`HttpOnly; SameSite=lax; Path=/; Max-Age=3600`,
  plus `Secure` in production). localStorage was rejected: it is readable by any
  injected script, so one stored-XSS bug becomes durable account takeover. This
  composes directly with D-015 — the two controls defend the same attack chain
  at different links. The Vite `/api` proxy makes the frontend same-origin with
  the backend, so cookie auth works without `SameSite=None` contortions.
- **Bearer header still accepted** for tests, curl and non-browser clients.
  Header wins over cookie when both are present: explicit authority beats
  ambient, and an attacker cannot set headers cross-origin.
- **TTL: 60 minutes.** Bounded stolen-token lifetime; survives a consult. A test
  fails if this is raised above 120 minutes while no refresh flow exists.
- **No refresh, no rotation, no revocation denylist.** `/auth/logout` clears the
  browser cookie, but a token copied elsewhere stays valid until expiry.

**Honest sharp edge:** login also returns the token in the response body for
non-browser clients. A careless frontend could persist it to localStorage and
undo the whole benefit. Accepted for prototype ergonomics, documented rather
than hidden.

**Cost / known gaps:** no refresh means expiry forces re-login; no denylist
means no immediate revocation; no login rate limiting (mitigated only in that
login responses are identical for unknown-user and wrong-password, so accounts
cannot be enumerated).

### D-017 · CSRF: SameSite=lax only

Introducing cookie auth introduces CSRF exposure that bearer-header-only auth
did not have. `SameSite=lax` stops cookies riding along on cross-site
state-changing requests in modern browsers, which is proportionate for a
prototype with no cross-origin surface.

**Cost:** no defence-in-depth. Production should add a double-submit token, or
require the bearer header for mutations so ambient cookie authority alone cannot
perform a write.

### D-018 · No sanitization library added

`bleach`/`nh3` would be the reflex choice. Not added, because under D-015 there
is nothing for them to do: we are not producing sanitized HTML, we are declining
to produce HTML at all. Adding an HTML sanitizer would imply the content is
rendered as HTML somewhere, which is precisely the belief we do not want a
future contributor to form.

**Cost:** if a later phase does need real rich text (formatting, tables), this
decision reverses and a sanitizer becomes mandatory at that moment.

### D-019 Deferred scope: translation & handwritten note capture

Considered multilingual patient summaries and OCR-based handwritten note capture. Deferred both for the 72-hour build:

Multilingual summaries: low-cost future path (extend the existing Phase 2.2 LLM call to emit a second-language summary when a patient's preferred language is set) — deferred only for time, not architecture.
Handwriting OCR: deferred structurally, not just for time. It's a different ingestion pipeline (image → OCR → redact → summarize), redaction is materially harder on noisy OCR output than clean transcript text, and medical handwriting OCR accuracy is a hard problem even for well-resourced products. Ambient voice capture (Phase 5) already solves the underlying "fast unstructured capture" need more safely.
---

## Phase 1 — Walking skeleton (2026-08-26)

Nothing in the Phase 0 RBAC design had to change. `AccessScope` absorbed the
first real feature routes without a single handler needing to mention
`clinic_id`, which is the property Phase 1 existed to test. The entries below
are additions and clarifications, not reversals.

### D-020 · `GET /auth/me` — session restore without client-side storage

The browser holds its token only in the httpOnly cookie (D-016), which
JavaScript cannot read by design. Without a "who am I" route, the frontend would
have to remember its own role and clinic across a page refresh, and the obvious
place to put that is `localStorage` — precisely what D-016 exists to prevent.
One cheap authenticated round-trip removes the temptation entirely.

`/auth/me` reads everything from the verified token. It accepts no parameters,
so there is nothing for a caller to supply.

**Cost:** one extra request on page load. Accepted; the alternative was a
storage decision we had already ruled out.

### D-021 · `Clinic` is looked up explicitly, not through `AccessScope.query()`

`AccessScope.query()` refuses any model without a `clinic_id` column
(fail-closed, D-003). `Clinic` is the tenant row *itself* — it has `id`, not
`clinic_id` — so the guard fires on it correctly.

Rather than weaken the guard by special-casing `Clinic` inside `AccessScope`,
`/auth/me` queries it directly with a comment stating why. This is the
"handle it explicitly with a documented reason" escape the Phase 0 error message
asks for.

It is safe because the id being looked up **is** `scope.clinic_id`, which came
from the verified token. No caller-supplied value reaches that query.

**Cost:** a precedent that could be cargo-culted. Any future direct
`scope.db.query()` must carry the same justification or it is a bug. Phase 3
should add a test that greps route modules for `scope.db.query(` and requires an
adjacent justification comment.

### D-022 · Cross-clinic misses return 404, cross-role refusals return 403

Two different refusals, deliberately, and the tests assert the specific codes so
a refactor cannot quietly collapse them:

* **Cross-clinic → 404.** A 403 means "this exists and you may not have it",
  which turns every endpoint into an enumeration oracle: an attacker walks ids
  and learns which patients exist at other clinics without ever reading one.
  404 tells them nothing.
* **Cross-role, same clinic → 403.** Here the caller is a legitimate user of a
  record that genuinely exists in their own clinic; they are simply not
  permitted this slice of it. Returning 404 would be lying to a colleague, and
  it makes real permission problems undebuggable.

**Cost:** the distinction is subtle and easy to lose. Mitigated by asserting the
exact status code in `test_phase1_cross_clinic.py` rather than `in (403, 404)`.

### D-023 · Type filtering is pushed into SQL, not applied after fetching

`list_entries` filters with `Entry.type.in_(viewable_types_for(role))` in the
query rather than fetching the timeline and dropping rows in Python.

Filtering in Python means rows the caller may not see are briefly in process
memory, where a stray log line, an exception repr, a `len(rows)` in a later
refactor, or a debugger can expose them. Never loading them is a stronger
property than loading and discarding them.

**Cost:** the policy matrix must be expressible as a SQL predicate. If a later
rule needs per-row logic (e.g. "staff may see a clinician section they are
named in"), this pattern has to be revisited rather than quietly abandoned.

### D-024 · Manual entries are their own provenance; AI entries point at a session

Every timeline entry carries a non-null `provenance_pointer`. For a manually
authored note that pointer is `entry://<its own id>` — it was written here, not
derived from anything. For an AI-scribed note it is `session://<session_id>`,
resolving back through `AIScribedNote` to the interaction that produced it.

The alternative was leaving `provenance_pointer` null for manual notes. Rejected
because every consumer would then need a null branch, and the first one to
forget it produces an entry with no traceable origin — in a product whose
central claim is that everything is traceable.

`resolve()` enforces the clinic boundary on pointers too, so a valid pointer
string cannot be used to read across tenancy.

**Cost:** a self-referential pointer looks redundant. It is: the value is in the
invariant holding without exceptions, not in the pointer itself.

### D-025 · AI-scribed types cannot be created through the manual write route

`POST /patients/{id}/entries` refuses any type in `AI_SCRIBED_TYPES` outright,
before the role check. Those entries carry `author_role=system` and must
originate from the Phase 2.2 scribe pipeline, which routes through
`redact_phi()`.

If a clinician could POST one, a client could fabricate machine provenance —
and provenance is the product's trust claim. It would also route text around the
redaction chokepoint, since the manual write path has no reason to call it.

**Cost:** Phase 2.2's pipeline must construct entries through a service function
rather than by calling this route internally.

### D-026 · Phase 0's demo routes stay for now

`/demo/*` and `tests/test_rbac_pattern.py` are retained even though real routes
now exist. They prove the enforcement pattern independently of any feature,
which is a useful second opinion while the feature surface is one module.

Phase 3 folds those assertions into the real-route suite and deletes both.
Recorded here so it is a scheduled removal rather than dead code nobody dares
touch.

### D-027 · Phase 1 latency figure is a lower bound, not a measurement

The brief targets P95 ≤ 300ms for the Glance View on a warm path. There is no
Glance View yet, so `test_phase1_skeleton.py` measures its cheapest ancestor: 20
warm, in-process timeline reads against SQLite, no network, no browser, no
serialisation over the wire.

Observed P95 is single-digit milliseconds. That number is **not** evidence the
target is met — it is a floor, recorded now so Phase 2 can watch how much of the
budget highlights, comments and AI summaries consume as they land on this same
path. The honest measurement needs the real Glance View, a seeded dataset of
realistic size, and timing taken at the browser.

**Cost:** none, provided the caveat travels with the number. Phase 6's brief must
not quote the figure without it.

### D-028 · Test fixtures were added alongside Phase 0's, not merged into them

Phase 0's `seeded` fixture is asserted against exactly (`== ["patient-a1"]`), so
widening it to Phase 1's richer seed would have broken passing tests that were
testing something real. `seeded_p1` / `client_p1` sit beside it.

**Cost:** two fixtures to keep roughly in step with `init_db.py`. Cheaper than
either editing Phase 0's assertions to accommodate new data, or having Phase 1
test against a seed too thin to distinguish four roles.

### Deferred / cut in Phase 1

* **Entry editing and revision history.** `Version` v1 is written at creation so
  no entry exists without one, but there is no edit route yet. Phase 2.7 owns
  optimistic locking and conflict handling; building half of it here would mean
  building it twice.
* **Pagination on the timeline.** Correct at seed scale and wrong at real scale.
  Deferred deliberately: it interacts with the Glance View's scoring and with
  Phase 4's decay states, and choosing a cursor scheme before those exist would
  be guessing.
* **A `system` role login.** `Role.SYSTEM` is an `author_role`, not an account.
  No credentials are seeded for it and none should be.
* **Frontend routing / state library.** One component tree, no router. The UI is
  scaffolding for the plumbing; investing here before Phase 6's design pass
  would be wasted.

### Open questions carried into Phase 2

* The frontend XSS source scan is a plain-text search, so it fails on its own
  documentation — naming the forbidden props in a comment trips it. Worked
  around by rewording. If Phase 2 adds more frontend files this will recur;
  consider scanning with a JSX-aware parse, or excluding comment nodes.
* `list_entries` returns full content for every entry in the timeline. Fine at
  seed scale; once the Glance View exists, the list endpoint should probably
  return summaries and defer bodies to the detail route.
* Admin currently has the clinician's full read surface. That satisfies
  "clinic-scoped oversight across all patient data", but an oversight role
  arguably should not read clinical reasoning by default. Left as is because the
  brief's wording is explicit; flagged because it is the most privileged read
  path in the system.

---

## Phase 2 — Core product surface (2026-08-26)

### D-029 · Importance scoring is a weighted sum over named features, not a model
The Glance View ranks with `W_RECENCY·recency + W_RISK·risk + W_ENTITY·entities
+ W_ACTION·unresolved + W_LEARNED·learned`, over tags produced by keyword and
pattern matching in `services/features.py`.

A clinical NER model would have better recall on prose we did not anticipate.
It would also be unexplainable, and this product's entire thesis is that a
clinician can see *why* something was surfaced before deciding whether to trust
it. Every `risk_reason` shown on the card is generated from the same table that
produced the tag, and the per-term score breakdown is stored on the highlight
and rendered in the UI. A ranker nobody can audit would undercut the product it
was serving.

Known cost: a medication absent from the watchlist scores as ordinary prose.
That is a recall gap, not a safety gap — an unrecognised term is simply not
promoted, and the entry still sits in the timeline. The failure mode is "less
helpful", never "silently hid something".

### D-030 · Highlights anchor to a version and go stale; they never re-anchor
Phase 1 left this open. Resolved: a `Highlight` stores
`source_version_number`, and staleness is `highlight.source_version_number !=
entry.version_number`. Stale highlights resolve their span text against the
*version snapshot they were made against*, and the UI marks them "source edited
since".

The alternative — silently moving the span onto the current text — would show a
clinician's confirmed highlight sitting over words nobody approved. Invalidating
outright was rejected too: the fact that a clinician thought something mattered
survives the sentence being reworded.

### D-031 · Offline summarisation is real extractive summarisation, and says so
With no API key the stub provider returns non-JSON, and `_extractive_summary`
takes over: it selects the highest-signal utterances from the already-redacted
transcript using the same feature vocabulary the Glance View scores on.

The lazy option was to store the stub's `[STUB SUMMARY 4f3a2b1c]` output. A
reviewer with no key would then see placeholder text where a consult summary
should be and could not judge the product at all. `model_used` records which
path ran (`offline-extractive-v1` vs `provider:model`), so provenance never
overstates itself — the note does not claim a model wrote it when one did not.

Confidence is *derived* on that path, from hedging density in the source
transcript, rather than asserted. A session where the patient said "maybe", "I
think" and "not sure" throughout produces a summary the UI marks lower — which
is the calibration signal the brief asks for, and it demonstrably varies
(patient session ≈0.47 vs nurse consult ≈0.77 on the seeded transcripts).

### D-032 · The scribe pipeline is synchronous; the processing state is client-rendered
A background worker needs its own session, failure surface and retry story, none
of which the demo exercises. The pipeline runs inside the request, and the
client shows a shaped placeholder card for its duration.
`CARENOTE_SCRIBE_DELAY_MS` (default 0, including in tests) makes that state
observable when recording the demo.

Honest limit: a crash mid-pipeline loses the summary rather than leaving a
retryable job. Acceptable when the input is a fixture; not acceptable once the
input is a recording someone cannot reproduce, which is a Phase 5 concern.

### D-033 · "What's new" compares against a held marker, not the last page load
`PatientView` stores two timestamps. `last_viewed_at` moves on every load;
`previous_viewed_at` is the comparison point and only rolls forward when more
than `VIEW_SESSION_GAP` (20 minutes) has passed.

With one timestamp, opening the Glance View would clear the very thing it just
showed you — a refresh, or a second monitor, and the news is gone. First visit
returns no marker at all rather than captioning an entire chart as new.

### D-034 · Redaction placeholder collision fixed (defect found in Phase 2)
`nric` and `mrn` are separate categories sharing the `[ID_{n}]` template, and
counters were keyed per category — so the first NRIC and the first MRN in a
document both rendered as `[ID_1]`. A model reading `MRN-[ID_1], NRIC [ID_1]`
would read one identifier where there were two. Counters are now keyed on the
token template. Found by running a real transcript through the scribe pipeline;
worth recording because it is exactly the class of bug that unit tests over
single-identifier strings do not catch.

### D-035 · Comments are staff/clinician/admin only; patients are not participants
The brief says a patient cannot *view* internal comments. This build also
refuses patient *writes*, and enforces the read rule twice: refused at the
route, and every internal role's comment is stamped `is_internal=True` at
creation, so a later route that forgets the check still cannot leak one.

A patient's voice reaches the record through `patient_note` entries and AI
session summaries, which are first-class timeline content. Letting them write
into a thread they cannot read the rest of would be worse than not offering it.

### D-036 · Typography carries provenance
Human-authored content is set in the UI sans; machine-generated summaries and
transcript text in mono. Alongside the dashed rail, the rail colour and the
explicit "AI scribed" label, that is four independent signals for one
distinction.

The brief makes AI-vs-human distinction a hard requirement, and one signal is
not enough for it: colour alone fails a colour-blind reader on the exact
distinction the trust argument rests on. System font stacks rather than
webfonts — the build must run offline for a reviewer with no network, and every
dependency has to earn its line in `ATTRIBUTION.txt`.

### Deferred / cut in Phase 2
* **Real-time multi-user sync.** No WebSocket, no live cursors. Optimistic
  locking plus a 409 that carries the current state covers the collision case
  the brief actually names; presence would be demo polish paid for in
  infrastructure.
* **Timeline pagination.** Still deferred, now with a measured reason: the
  Glance View P95 is ~11ms at seed depth, so the pressure is not there yet. It
  will be at a few hundred entries per patient.
* **Highlight generation for staff-authored content viewed by staff.** Works,
  but staff cannot accept/reject, so suggestions are advisory to them. Flagged
  because the learning loop therefore only hears from clinicians.
* **Editing a comment.** Resolve/unresolve and reply exist; editing a posted
  comment does not. Version history on comments would be a second, near-
  duplicate implementation of `Version` for much less value.

### Open questions carried into Phase 3
* `patient_summary` is still clinician-writable only. Now that the scribe
  pipeline exists, AI-drafted-plus-clinician-approval fits the trust thesis
  better and is a small change — deliberately not made mid-phase.
* `list_entries` still returns full content for every entry. The Glance View
  now exists, so the split (summaries in the list, bodies on demand) is finally
  well-defined. Deferred: it changes a response shape three components read.
* The learned scoring term reads `FeatureWeight` and returns 0.0 because
  nothing writes to that table yet. `InteractionLog` rows *are* being written
  from Phase 2 onward, so Phase 4 starts with real behavioural history rather
  than an empty table.

---

## Phase 3 — Required automated tests (2026-08-26)

The four files the brief names by name now exist, at 83 tests between them, on
top of the 173 already in the repo. Phase 3 was meant to be a write-tests-
against-what-exists phase with no product change. It found one real defect, and
the entry recording it is the substantive part of this section.

### D-037 · The optimistic lock needed a second line of defence

**Found by writing `test_concurrent_edits.py`.** The Phase 2 version check reads
`entry.version_number`, compares it to `expected_version`, and only then writes.
That is check-then-act, not a lock: between the read and the commit there is a
window in which a second caller can pass the same comparison holding the same
starting version.

What actually made the system safe was already there — the `uq_entry_version`
unique constraint on `(entry_id, version_number)` means the second transaction
cannot write a second version 2, so **no edit was ever silently lost**. The
guarantee the brief asks for held. What did not hold was the contract around it:
the loser surfaced an unhandled `IntegrityError` as a **500**. A 500 tells the
user nothing, carries none of the current state, and looks like a crash rather
than a resolution — so "deterministic resolution strategy" was true of the data
and false of the API.

`_appending_version` in `entry_routes.py` now wraps the write region and
translates that constraint violation into exactly the 409 the pre-check
produces, via a shared `_version_conflict` body. A client cannot distinguish
"your version was already stale when you asked" from "someone beat you to the
commit by milliseconds", which is correct — both mean *reload before you save*.
Applied to revert as well as update, because revert appends a version by the
same path and races the same way.

**Why the interleaved tests missed it.** Every same-section test written first
was `read → read → write → write` against one shared session, which is
deterministic and proves the lost-update property exactly — but a single session
serialises everything through itself, so the racy window never opens. Only real
threads against a file-backed database with a session per request exposed it.
Both styles are kept, and the file says why: the interleaved tests are the
specification, the threaded ones are the thing that finds what the
specification forgot to say.

**Cost / remaining gap:** SQLite serialises writers with a database-level lock,
so under heavier contention a writer can time out with `OperationalError`
("database is locked") rather than reaching the constraint at all. That is
deliberately *not* translated into a 409 — a lock timeout is an infrastructure
failure, and reporting it as "someone else edited this" would be a lie about
what happened. Postgres with row-level locking removes the distinction; it is
noted rather than fixed because the prototype's storage decision (D-001) is
SQLite.

### D-038 · Concurrent reverts may both legitimately succeed

Not a defect, but non-obvious enough to record, because the first draft of the
test asserted otherwise and was flaky as a result.

`revert` takes a `to_version` — a *target*, not a base — and no
`expected_version`. So a second reverter that reads after the first has
committed is not stale: it performs a valid sequential revert to the same
target and returns 200. Only a reverter that read the same base and lost the
commit race gets a 409.

Both outcomes are correct, so the number of successes under parallel reverts is
genuinely non-deterministic. `test_parallel_reverts_never_crash_or_fork_the_history`
therefore asserts the invariants that do hold — no crash, contiguous version
chain with no duplicates or gaps, content lands on the target regardless of how
many reverts landed — rather than a success count that would make the test
flaky rather than strict.

Adding `expected_version` to revert would make it deterministic and was
considered. Rejected: reverting is a recovery action, usually taken *because*
the record is in a state the user did not expect, and requiring them to first
prove they know what that state is adds a failure mode to the operation people
reach for when something has already gone wrong. Reverting twice to v1 yields
v1 either way.

### Testing decisions

**Mutation checking extended to all four files.** Each new suite was verified to
fail when the behaviour it asserts is deliberately broken — eight mutations,
tabulated in `README.md`. The two that matter most: disabling the D-037 guard
fails 3 tests, and switching to last-write-wins fails 4. Coverage that cannot
fail is not coverage.

**Provenance is asserted at two layers on purpose.** `resolve()` is called
directly for *every* highlight in the database, so a highlight on an entry type
a given role would be refused is still checked; and the API route is asserted
separately, because the route adds the role and clinic checks that make a
pointer a reference rather than an authorisation. Neither layer alone covers
the requirement.

**The parallel fixture is local to `test_concurrent_edits.py`**, not added to
`conftest.py`. It needs a file-backed engine and a session per request, which is
different enough from the shared in-memory single-session fixture that folding
the two together would complicate every other test in the suite to serve four.
Same reasoning as D-028.

**One test asserts against `EntryOut` not having a field.**
`test_clinic_id_is_taken_from_the_token_not_the_request` checks the stored row
rather than the response, because the wire format deliberately omits
`clinic_id` — the API cannot confirm the property, only the database can.

### Deferred / cut in Phase 3
* **`OperationalError` translation** under SQLite write-lock contention — see
  D-037. Deliberately left as a crash-with-a-real-cause rather than mislabelled
  as a conflict.
* **A concurrency test for comments.** Comments are append-only with no version
  field, so there is no lost-update hazard to demonstrate; two people commenting
  at once simply produces two comments.
* **Load/latency testing under concurrency.** The P95 figure in
  `ARCHITECTURE.md` is measured on a warm single-user path. What concurrent
  read latency looks like is unmeasured and is stated as unmeasured rather than
  extrapolated.

### Open questions carried into Phase 4
* The three open questions from Phase 2 are all still open and still deliberate
  (`patient_summary` authorship, `list_entries` payload size, the empty
  `FeatureWeight` table). Phase 4 closes the last of them by construction.
* `InteractionLog` rows have been accumulating since Phase 2 but nothing has
  ever read them. Phase 4 is the first consumer, so the first thing it should
  do is check that the tags being written are actually the shape the scorer
  wants — a schema that was never read back is a schema that was never tested.

---

## Phase 4 — Self-learning importance and data decay (2026-08-27)

Both bonus tracks are built rather than described. The phase closed the last of
the open questions carried since Phase 2 — the empty `FeatureWeight` table — and
found two real defects doing it, which are D-040 and D-042 below.

### D-039 · Authorship is recorded but never learned from

**Found by auditing the tags Phase 2 was writing**, which the Phase 3 notes
flagged as the first thing Phase 4 should do: *a schema that was never read back
is a schema that was never tested.*

Creating an entry was logging `InteractionAction.EDIT` with the tags of its own
content. Fed into a learned weight, that trains the ranking on **what this clinic
writes about most**, which is volume, not attention. A clinic that sees a lot of
diabetes would learn that diabetes matters — not because anyone stopped and
attended to it, but because they typed it often. The Glance View would then
promote the most common thing on every chart, which is close to the opposite of
triage.

Added `InteractionAction.CREATE`, weighted `0.0`. Same treatment for `VIEW`:
opening a chart is unavoidable, so counting it would learn that everything
matters.

Both are still written to `InteractionLog`. Recorded is not the same as
learned-from, and a behavioural history with the authorship events deleted would
be worse for any future analysis than one with them labelled.

**Alternative rejected:** give `CREATE` a small positive weight (0.1). Tempting
because writing about something *is* weak evidence of caring about it, but the
volume asymmetry swamps it — a clinic writes hundreds of notes for every
highlight it confirms, so even a small weight would dominate the deliberate
signals through sheer count.

### D-040 · The learned term is recomputed from the log, never nudged

`FeatureWeight` is a **materialised view** over `InteractionLog`, not a running
tally. Every write path calls `learning.recompute_tags()`, which rescans the log
for the tags just touched and recomputes them from scratch; `rebuild_clinic()`
does the same for all tags at once. Both call one accumulation function.

The obvious cheaper design is an incremental nudge — `weight += 0.1` on accept.
Rejected because it creates two formulas that must agree forever and silently
diverge the first time one is changed. Since the weights are a claim about a
clinician's own behaviour, a version that cannot be reproduced from the evidence
is not auditable, and "the system says you care about this" with no way to check
it is precisely the failure mode this product argues against.

The cost is one unindexed scan per write path, on writes only — never on the
Glance View read path, which still reads precomputed scores. The scaling answer
(a normalised tag join table) is recorded in `SCHEMA.md` and not built.

**Coupling decision:** `learning.apply_signal()` is called from inside
`interactions.record_interaction()`, not from the six routes that record
signals. Recording a behavioural signal and learning from it are one operation,
enforced in one place — the same reasoning as the redaction chokepoint. A rule
repeated at six call sites is a convention waiting to be forgotten.

**Known boundary:** weights are clinic-scoped but rescoring is triggered
per-patient. A patient nobody has touched keeps stale scores until their chart
is written to or `POST /clinic/learning/rebuild` runs (a nightly job in
production). Rescoring the whole clinic on every click was the alternative and
is unbounded work on a hot path. The staleness is visible and bounded; the
alternative is a latency cliff nobody sees coming.

### D-041 · Learning is asymmetric: safety vocabulary is never dampened

Weights for `entity:allergy`, `risk:critical`, `symptom:anaphylaxis`,
`symptom:suicidal`, `symptom:self-harm` and `symptom:sepsis` are floored at
zero. Clinician behaviour can promote them; it can never suppress them.

A clinician dismissing three warfarin suggestions should teach the system to
stop nagging about warfarin — that is the feature working. A clinician
dismissing three anaphylaxis suggestions must **not** teach it to stop
mentioning anaphylaxis, because the cost of a missed allergy is not symmetric
with the cost of one extra line on a card. The learning rule is not symmetric
either.

This is deliberately a **floor, not a filter**. The dismissals are still
recorded, still counted, and still shown on `GET /clinic/learning` as negative
signals sitting next to a weight of 0.0. The seed demonstrates it: `entity:allergy`
reads `+0/−2` at weight `0.00`. Hiding the evidence would make the system look
like it agreed with the clinician; showing it says plainly *we recorded what you
did and we are not going to act on it here.*

**Alternative rejected:** let the weight go negative but clamp the final score.
Equivalent in effect, worse in explanation — the transparency surface would then
show a negative number that the ranking does not actually use.

### D-042 · Cold entries are down-weighted, never excluded

`SCHEMA.md` said since Phase 0 that cold entries were "excluded from scoring".
The code never did this — `scoring.DECAY_MULTIPLIER` has always had cold at 0.4
— and building the policy confirmed the code was right and the document wrong.
The document is corrected rather than the code.

An entry can be the only record of an allergy and still be four years old. Age
is a prior about relevance, never a proof of irrelevance. Excluding cold entries
from scoring would mean the one place the system is most likely to hold
something nobody remembers is the one place it refuses to look.

Recorded rather than quietly fixed because a schema document that disagreed with
the scorer for three phases is worth knowing about: it survived that long
precisely because nothing had exercised the path.

### D-043 · Compression is reversible, offline, and holds after a restore

Three sub-decisions on the one operation in this system that rewrites stored
clinical text.

**No LLM in the compression path.** The summary is extractive — real sentences
from the original, selected by the same feature tagger the Glance View scores
on, kept verbatim and in their original order. An abstractive summariser
hallucinating during archival would corrupt the record permanently, silently,
and at the moment nobody is looking at it. The cost is that summaries read as
clipped rather than fluent, which is the correct trade for an operation whose
output replaces what a clinician wrote.

**A restore sets `decay_hold_until`.** Without it, a clinician who reopened a
four-year-old note to read it properly would find it recompressed by the next
nightly pass. That reads as the system arguing with them. Thirty days, then the
policy resumes.

**`dry_run=True` is the default** on both `decay.run()` and
`POST /clinic/decay/run`, and applying is admin-only. Admin is the oversight
role (D-011) and cannot author clinical content, which makes it the right holder
of a lifecycle operation that rewrites stored text without adding any clinical
claim to the record.

**Provenance defect found here.** Span pointers index the entry's *full* text.
Compressing `Entry.content` without redirecting resolution moves every offset
onto different words, or overruns the end and reports a dangling pointer for a
perfectly valid highlight — which would have broken the requirement Phase 3's
`test_highlight_provenance.py` exists to protect, silently, and only for old
entries. `provenance.resolve()` now reads through `decay.original_content()`,
cold entries stop minting new spans, and manual highlighting on a cold entry is
refused with a message telling the clinician to restore first rather than
anchoring to the wrong words.

### D-044 · The decay report does not claim a storage saving it cannot show

The first version of `decay.run()` returned `bytes_saved` by comparing
`Entry.content` before and after. That figure ignored what the archive costs.

Base64 inflates zlib's output by about a third, so on the seeded 455-byte note
the archive costs 376 bytes against a 391-byte reduction — a net saving of
fifteen bytes. The honest figure is that compression buys a **7× smaller hot
row**, which is what a timeline load actually reads, and roughly break-even
total storage at prototype note lengths. Total storage turns meaningfully
positive on notes of a few KB, where the compression ratio beats the base64
overhead.

`decay.run()` now reports `hot_bytes_before`, `hot_bytes_after`,
`archive_bytes` and `net_storage_delta` separately. Keeping the single number
would have been a more impressive line in the brief and a false one.

### Deferred / cut in Phase 4

* **Normalised tag index for `InteractionLog`.** The `LIKE` prefilter is one
  unindexed scan per write. Schema for the replacement is in `SCHEMA.md`; not
  built because the prototype cannot demonstrate needing it.
* **Per-user normalisation of learning signals.** One enthusiastic clinician
  currently counts the same as consensus across a practice. Saturation bounds
  the damage (asserted), but at real volume signals should be normalised per
  user before aggregation.
* **Automatic decay scheduling.** No cron, no background worker. `run_decay.py`
  and the admin endpoint are explicit triggers. A prototype that silently
  rewrote clinical text on a timer would be harder to reason about during a
  demo, and the policy is the interesting part.
* **Learning from *where* in an entry a comment landed.** Comments attach to
  entries, not spans, so commenting reinforces every tag in the note. Span-level
  comment anchoring would sharpen the signal and is a Phase 2 schema change, not
  a Phase 4 one.
* **Decaying `Version` snapshots.** Cold compresses `Entry.content` only; the
  version chain still holds every full snapshot, so this is a hot-row
  optimisation rather than true storage reduction. Compressing history would
  need care not to break revert, and revert correctness is directly graded.

### Open questions carried out of Phase 4

* Two of Phase 2's three open questions remain open and still deliberate
  (`patient_summary` authorship, `list_entries` payload size). The third — the
  empty `FeatureWeight` table — is closed by this phase.
* The learning loop has never been observed with more than one clinician's
  behaviour in it. Clinic A's seeded history is a single synthetic cohort, so
  disagreement between two clinicians in the same clinic is untested behaviour,
  not a designed one.
* Whether promoted content actually shortens a clinician's time-to-decision is
  the outcome the whole feature exists for, and it is not measurable from inside
  the system. It needs instrumented users.
