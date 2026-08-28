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

---

## Phase 5 — Ambient consult capture (2026-08-27)

Voice capture is a bonus, and it is the phase where the build's central rule —
*redact before the text leaves* — meets the one input it cannot be applied to.
Most of what follows is about handling that honestly rather than pretending it
away.

### D-045 · Audio is never persisted, anywhere

Recordings arrive in memory, are transcribed, and are dropped when the request
ends. Nothing writes them to disk, to the database, or to a log.

A voice is biometric identifying data. It is PHI before a single word of it is
recognised, and unlike text there is no redacted form of it to keep — a
de-identified recording is not a thing that exists. Every other identifier in
this system has a placeholder; audio has only deletion.

Storing it would also have bought nothing the product needs. The clinician reads
the summary and, when they doubt it, the transcript. Re-listening is a
correction workflow for a system that expects to be wrong often enough to need
one, which is a different product.

`CaptureSession` records `audio_bytes_received` and `audio_retained` (always
false) so the claim is a stored fact a test can assert against rather than a
sentence in a README. `test_audio_is_never_retained` walks every column on the
row looking for the bytes.

**Cost:** a mis-transcription cannot be re-checked against what was actually
said. The transcript is the record, and if the recogniser mangled a dose, the
evidence that it did is gone. In production this is the trade to revisit first,
probably as short-retention encrypted audio with a hard TTL — but that needs a
retention policy, a key management story and a legal basis, none of which a
72-hour prototype should invent.

### D-046 · The stub recogniser announces that it is simulated

With no ASR provider configured, `_SimulatedProvider` returns a deterministic
fixture transcript. It cannot hear. Every capture it produces sets
`transcription_simulated = true`, and that flag reaches the entry card, the
transcript panel and the API payload's `notice` string.

The alternative — a stub that quietly emits plausible clinical text — would have
demoed better and been indefensible. This is a build whose entire argument is
that a clinician can tell where a claim came from. A recogniser that fabricates
a transcript and lets the interface imply speech recognition happened is the
exact failure the product exists to prevent, committed by the product itself.

Same reasoning as D-031, where offline summarisation reports
`model_used = offline-extractive-v1` rather than borrowing a model's name.

### D-047 · Overlap detection is arithmetic on timings, not diarisation

A segment starting before the previous one ended is two people talking at once.
That is computed, flagged in the transcript panel, and counted on the capture
row. It is worth surfacing because overlapping speech is where recognisers make
their worst mistakes, and a clinician reading a garbled line benefits from
knowing why it is garbled.

It is **not** acoustic diarisation. Nothing here separates voices from a mixed
waveform. Speaker labels come from whatever produced the turns — the simulated
recogniser, or the uploaded transcript's own labels. Real diarisation is a model
(pyannote, or a recogniser with speaker turns built in) and belongs behind the
`local` ASR provider when that is implemented.

The distinction matters because "speaker-labelled transcript" in the brief could
be read as a claim to diarisation, and this build does not have one. Stated here
and in the README gap list rather than left for a reviewer to discover.

### D-048 · Attribution is established by matching, never by self-citation

The obvious way to link a summary line to its source is to have the summariser
emit citations. It is also the way that produces confident, checkable-looking
pointers to segments that do not support the line: models hallucinate citations
at least as readily as they hallucinate content.

A false citation is worse than no citation, because it survives review. A
clinician who clicks through and lands on a real-looking segment has been given
*more* confidence in a wrong line, not less. That inverts the whole point.

So `services/attribution.py` establishes links after the fact by comparing the
generated summary against the stored segments:

| match | meaning | evidence |
|---|---|---|
| `verbatim` | the segment's words appear in the line, whitespace-normalised | re-derivable by anyone; nobody has to trust the summariser |
| `derived` | ≥55% of the line's distinctive words are shared with one segment | weaker, and labelled differently in the UI |
| *(no row)* | neither test passes | the line shows no source |

The offline extractive summariser selects real utterances, so `verbatim` is the
common case on the default path — 7 of 7 lines on the demo consult. A live model
that paraphrases will produce more `derived` links and more unattributed lines,
and the coverage figure reported alongside the note will drop accordingly. That
is the correct behaviour: a note where three of eight lines trace to spoken words
is a different object from one where all eight do, and the clinician holding it
should be able to tell which.

**Cost:** a line that faithfully synthesises three separate segments gets no
attribution, because it matches none of them well enough. Under-claiming is the
right direction to fail in, but it is a real loss of recall, not a free win.

### D-049 · Transcripts are clinical-roles-only, including a patient's own

A patient may record — the brief asks for patient voice capture explicitly — and
gets back a receipt confirming what was sent, not the transcript and not the
clinical summary written from it.

The brief already says a patient cannot view raw AI-scribed notes. A raw
transcript is strictly more raw. And a consult recorded *in the patient view*
captures the clinician's half of the conversation too: serving it back would
route straight around the patient-facing filter that every other read path in
this build enforces carefully, and it would do so with the least-reviewed text
in the system.

This is least-privilege applied to a genuinely uncomfortable case — it is the
patient's own voice, in their own appointment, and they are refused it. Recorded
as a decision rather than buried, because a reviewer may reasonably disagree.
The counter-argument (patients have a right of access to their own record, and
in most jurisdictions a legal one) is real; the answer in production is a
subject-access request through a reviewed process, not an API endpoint that
hands over unreviewed clinical speech.

### D-050 · The name gazetteer expands to name parts (defect found in Phase 5)

`redact_phi_detailed`'s docstring stated that a caller-supplied gazetteer
catches "bare first-name mentions in prose". It did not. The gazetteer only ever
held full display names, so `"Hi Amira"` and `"Rahman said"` passed through
untouched — **including in Phase 2's own nurse-consult fixture**, which opens
with `Hi {first}`.

Found while running the first voice fixture through the pipeline and reading the
output. Two identifiers were redacted where three should have been.

Fixed inside `_Redactor.__init__` rather than at the call sites, so every present
and future caller gets it and none can forget — the same structural argument as
the redaction chokepoint itself. Titles and connectors are excluded, so `Dr Lim`
contributes `Lim` and not `Dr`.

**Cost:** more false positives. A clinic user named "Serene" means the word
*serene* is now redacted in prose. That is the correct direction for a redaction
boundary to err in, and it is a known limitation of a gazetteer approach rather
than a bug in this fix.

### D-051 · `start` and `stop` were too broad to be action cues (defect)

`ACTION_CUES` matched the bare verbs `start` and `stop`. In written clinical
notes this is mostly fine. In transcribed *speech* it is not: `"before we
start"` and `"When did it start?"` were both landing on the Glance View as
pending medication changes.

Replaced with phrase forms (`stop the`, `stop taking`, `switch you to`,
`start you on`, …). Real medication changes still fire; temporal uses no longer
do.

The asymmetry that justifies the change: on a card designed to be read in ten
seconds, a phantom open action costs more than a missed one. A missed action is
still in the timeline. A phantom one spends the clinician's attention and, worse,
teaches them the card is noise — and a Top Card nobody trusts is a Top Card
nobody reads, which is the failure mode the whole Glance View is built against.

### D-052 · No vocabulary for oedema (defect)

`RED_FLAG_TERMS` had no entry for swelling or oedema, so a consult whose entire
clinical content was ankle swelling produced a summary with no patient-reported
section at all. Peripheral oedema is one of the commonest adverse drug effects
in primary care and a patient describing it is describing the reason for the
visit. Added.

Recorded because it is the recall gap `features.py` warns about in its own
docstring, caught in the wild: the vocabulary only knows what it knows, and the
failure is silent — nothing is hidden, but nothing is promoted either.

### D-053 · The service worker caches the app shell and never the API

Making the app installable needs a service worker. The default recipe every
offline-first guide reaches for is "cache API GETs so it works on a bad
connection". Applied here that would write consult summaries, staff notes and
transcript segments into the Cache Storage API — an origin-scoped store that
survives logout, survives the 60-minute token expiry, and is readable by any
script running on the origin.

That would undo D-016. The point of putting the session token in an httpOnly
cookie was that an injected script should not be able to read durable secrets;
caching the clinical data those secrets protect hands the script the data
directly and saves it the trouble of stealing anything.

So `/api` is network-only and never written to a cache. The shell — HTML, JS,
CSS, containing no patient data — is cached, which is the part that actually
matters for ambient capture: the recorder is local, and the upload can wait for
signal.

Registered in production builds only. In dev, Vite serves modules a caching
worker fights with, and stale-bundle confusion costs more than the feature is
worth while iterating.

### D-054 · Every AI-scribed note gets line-level attribution, not just captures

Attribution runs inside `run_scribe` rather than in the capture path. The
Phase 2 fixture scribe already wrote `TranscriptSegment` rows; the matching is
the same work, so it gets the same provenance.

Consequently `GET /captures/{session_id}` is keyed on the **segments**, not on a
`CaptureSession` row. Every AI-scribed note has a transcript behind it; only a
recording has a duration, a recogniser and a byte count. Keying on the capture
row made the endpoint report "no transcript is stored" for notes whose
transcript was sitting right there. `capture` comes back `null` for a fixture
session and the client omits that header.

### Deliberately not built in Phase 5

Listed so the gap is a decision rather than a discovery. All of these are in the
README's gap list too, in the same words.

* **Real speech recognition.** `_LocalWhisper` is a documented interface with
  `NotImplementedError` in its body, not a half-wired integration. Adding
  faster-whisper is a model download and a `transcribe()` call, and it changes
  that class and nothing else — but claiming it in a brief without having run it
  is exactly the kind of assertion this build refuses elsewhere.
* **Acoustic diarisation.** See D-047. Speaker labels come from the transcript
  source, not from separating voices.
* **Noisy-environment handling.** The browser's `echoCancellation`,
  `noiseSuppression` and `autoGainControl` constraints are requested on the
  media stream, which is genuine but is the browser's work, not ours. No
  acoustic preprocessing of our own.
* **Multi-device capture.** One recorder, one stream. Merging two devices'
  audio needs clock alignment across them, which is a real distributed-systems
  problem and not a UI one.
* **Multilingual medical terminology.** *Partially closed in Phase 6 — see
  D-058.* At the end of Phase 5 the position was: code-switched speech is
  carried through redaction, storage and summarisation intact and tagged per
  segment (`en-ms` in the fixtures), but `features.py` read English only, so a
  Malay symptom description was stored and shown faithfully and never
  recognised as a clinical entity. Phase 6 added a Malay clinical vocabulary.
  Translation and non-English *summary generation* remain unbuilt, consistent
  with D-019.
* **Streaming transcription.** Capture is upload-then-process. Live partial
  transcripts during a consult are a websocket and a different UX.

### Open questions carried out of Phase 5

* The `derived` match threshold (0.55) was set by hand against fixtures. It has
  never been tuned against real model output, because the default path produces
  verbatim matches and the live path has not been run at volume. It is a
  plausible number, not a validated one.
* Whether clinicians would actually open the transcript panel, or whether the
  confidence chip alone is what they act on, is unmeasurable from inside the
  system and would change what is worth building next.
* Patient-recorded consults raise a consent question this build does not model
  at all: the clinician is a party to that recording and is never asked. A
  production system needs a consent artefact on the capture, and probably a
  visible indicator in the clinical view that a patient recording exists.

---

## Phase 6 — Docs, polish, demo (2026-08-27)

### D-055 · Enum columns are compared with `==`, never `is` (defect found in Phase 6)

Every enum-valued column in this schema is declared `Mapped[SomeStrEnum]` but
backed by a `String(20)` column. SQLAlchemy stores the string and returns a
plain `str` on load — the column type never told it these were enums, so there
is nothing to coerce back through. For any reloaded row:

```
row.status == HighlightStatus.SUGGESTED   ->  True    (StrEnum compares equal)
row.status is HighlightStatus.SUGGESTED   ->  False   (different objects)
```

An object built in-session still holds the real member, so `is` works right up
until the first reload. That is why this survived five phases: it is correct in
the unit test that constructs the object and wrong in production, and it fails
**silently** — no exception, no traceback, just a branch that stops executing.

Three sites used `is`, and all three were live defects:

| Site | Effect |
|---|---|
| `highlights.refresh_entry_highlights` (×2) | The guard deleting superseded suggestions never fired. Every refresh appended a second copy of every highlight, and a refresh runs on entry create, entry edit, highlight accept/reject and clinic rebuild — so duplicates compounded. The seeded chart held 32 rows for 16 spans and the Top Card rendered each claim twice. |
| `comment_routes`, mention validation | `user.role is not Role.PATIENT` was always true, so a patient login could be stored as a mention on an internal thread they can never read (D-035). |
| `comment_routes`, task assignment | `assignee.role is Role.PATIENT` was always false, so the guard refusing patient assignees never refused one. A task could be assigned to the patient's own login, putting their name in the clinician's "Open actions" list as the responsible party. |

The duplicate-highlight one is the worst of the three, and not only cosmetically.
The whole provenance argument is that a surfaced claim traces to one source; the
same claim appearing twice reads as two independent sources agreeing, which is
the opposite of what the card is supposed to communicate. It was found by
looking at a screenshot, not by any test — 334 tests passed with it live.

**Decision: fix with `==` / `!=` and pin the class with a source scan**, rather
than migrate the columns to a real `Enum` type.

The migration is the better production answer and would make `is` safe
everywhere. It was rejected *here* for scope reasons on the final day: it
touches ten columns across every model, changes what the ORM returns to every
caller and serialiser in the codebase, and would be a wide, lightly-tested
change made hours before submission. Trading a narrow verified fix for a broad
unverified one at this point is the wrong risk. Recorded as a known gap rather
than done quietly badly — see the Phase 6 deferred list below.

What guards it in the meantime is `tests/test_phase6_regressions.py`: behavioural
coverage for all three defects, a test asserting the `str`-not-enum mechanism
itself (so it fails loudly if the columns are ever migrated and this decision
stops applying), and a scan that fails the build on any
`receiver.attribute is SomeEnum.MEMBER`. The scan allow-lists `scope.`, `self.`
and `payload.` — those are coerced at the JWT boundary in `security/rbac.py` and
by pydantic before a handler runs — and it has its own parametrised test proving
it flags what it claims to. Same technique as the LLM chokepoint scan and the
raw-HTML ban: make the careless form inexpressible rather than discouraged.

**What it costs:** a rule a contributor has to know, enforced by a regex rather
than by the type system. The regex is heuristic — it keys on attribute access,
so `is` against a bare local is not flagged and would not be caught.

### D-056 · Timeline legend wraps rather than sharing a row (defect found in Phase 6)

At a 375px viewport the timeline heading and its rail legend, laid out with
`justify-between`, were squeezed into two narrow columns whose wrapped lines
interleaved into unreadable text. Fixed with `flex-wrap`. Noted here because it
is the only rendering defect the mobile spot-check found, and because it is
evidence for what that check is worth: the desktop layout it was designed at
never showed it.

### Deferred out of Phase 6

* **Migrating enum columns to a real SQLAlchemy `Enum` type.** The structural
  fix for D-055. Deferred on the final day for the scope reason above; it is the
  first thing to do after submission.
* **Rebuilding highlights for existing charts.** The fix stops duplicates being
  created; it does not clean up a database seeded before it. `init_db.py --reset`
  or `POST /patients/{id}/highlights/refresh` does. Acceptable because all data
  here is synthetic and disposable; a real deployment would need a one-off
  backfill.
* **A formal accessibility audit.** Stated in the README as a known gap rather
  than attempted badly in the last hours.

### D-057 · The `/demo/*` pattern routes are retained, reversing D-026

D-026 said Phase 3 would fold the pattern assertions into the real-route suite
and delete both `/demo/*` and `tests/test_rbac_pattern.py`. Phase 3 did the
first half — `test_rbac_scope.py` covers the real routes — and never did the
second. Found in the Phase 6 sweep, where the module docstring still read
"Delete before submission if they are still here and unused."

**Decision: keep them, and say so.** They are not unused: 18 tests exercise
them, and those tests are worth keeping for a reason that only became clear
later. They assert role and clinic enforcement against a surface carrying no
product logic at all, so when one fails it is unambiguously the enforcement
layer that broke, not a feature. Every other RBAC test now runs through routes
with filtering, policy lookups and serialisation in the path. Deleting the
routes would delete the only tests that isolate the boundary itself.

They are gated by the same `require_access` dependency as everything else and
return nothing a caller's own token does not already assert, so retaining them
costs no exposure — but they are also not product surface, and a reviewer
opening `/docs` will see them. Recorded here so they read as a decision rather
than as code nobody dared touch.

**What this costs:** a `/demo` namespace in a production-shaped API. A real
deployment should either strip the router behind an environment flag or move
these tests to an app fixture that mounts the routes only under pytest. Both are
small; neither was worth doing on the final day.

### D-058 · A Malay clinical vocabulary, mapped to canonical English tags

Phase 5 left `features.py` reading English only. The consequence was not subtle
once looked at directly: identical clinical content produced tags in English and
**nothing at all** in Malay.

```
"Ankle swelling worst at night."            -> ['symptom:swelling']
"Kaki bengkak, malam paling teruk."         -> []
"She fainted, numbness in both feet."       -> ['symptom:fainted', 'symptom:numbness']
"Dia pengsan, kaki kebas dua belah."        -> []
```

No tags means no score, which means the span never reaches the Glance View. In a
Singapore or Malaysian clinic this is not an edge case, and it fails in the
worst available direction: the patients least likely to be understood in English
are exactly the ones the system quietly stops surfacing. The brief also lists
multilingual medical terminology as extra credit, but the reason to build it is
the first one.

**Design: each Malay term maps to the canonical English vocabulary key**, so
`bengkak` emits `symptom:swelling` — the identical string `swelling` emits.
Tags are the dictionary keys Phase 4 learns weights against. Emitting
`symptom:bengkak` would have created a second, unrelated feature, and a clinic's
learned attention would not transfer across whichever language a patient
happened to use — which would have made the multilingual support actively worse
than nothing for the learning layer, while looking like a feature.

**Scope, deliberately narrow: only terms whose English counterpart already
exists.** This makes the change purely additive — no English key is added or
altered, so no English input can behave differently, which
`test_english_prose_picks_up_no_malay_tags` asserts. Terms with no counterpart
(`gatal` itchy, `muntah` vomiting, `cirit-birit` diarrhoea) were left out rather
than added on both sides; adding them would be a scoring change to English prose
smuggled in under a translation heading, and it would need its own decision and
its own re-measurement.

This is **recall for a clinical watchlist, not translation.** The system still
stores and displays the patient's original words verbatim; it never rewrites what
someone said into English. The `risk_reason` names the term that actually matched
— "Oedema (Malay: bengkak)" — because an unexplained English reason sitting over
Malay source text reads as a mistranslation of the patient.

**Malay only, and that is a deliberate stopping point.** Mandarin, Tamil and
Hokkien are all common in the same clinics. Adding three more languages from the
same generalist knowledge that produced this one would multiply an unreviewed
risk rather than reduce a gap. Malay was chosen because it is the language the
Phase 5 capture fixtures actually contain.

**What is genuinely still wrong with it:**

* **Every term needs native-speaker and clinical review.** This vocabulary was
  written from general knowledge, not from a Malaysian clinical lexicon or by
  someone who practises in one. The mechanism is proven; the word list is a
  demonstration, not a validated resource. It should not go near a real clinic
  before a Malay-speaking clinician has read all fourteen entries.
* **Negation is not handled — in either language.** "Tiada demam" (no fever)
  tags `symptom:febrile`. This was found while testing this change but is
  **pre-existing and not introduced by it**: "Patient denies chest pain" and
  "Without swelling or redness" fail identically in English and always have.
  Both are pinned by `test_negation_is_not_handled_in_either_language`, so the
  day someone adds negation handling it must be applied to both languages at
  once rather than one being fixed and the other quietly left behind. Not fixed
  here because a negation guard changes English scoring, needs its own decision
  and its own Glance View re-measurement, and this is the final day. The failure
  direction is the safe one: a ruled-out symptom is surfaced for a human to
  dismiss, never a real one suppressed.
* **`jatuh` (fall) also appears in place names.** A referral letter naming one
  can register a falls-risk symptom. Asserted in
  `test_known_false_positive_is_documented_not_denied` so it is a recorded
  property rather than a surprise. Same failure direction: less precise, never
  silently hiding.

---

## Phase 7 — Reported defects (2026-08-28)

Four bugs reported against the Phase 6 build, plus three found while
reproducing them. Every one survived a green 385-test suite, and they have a
shape in common: each lives in the seam between two pieces of individually
correct code. The manual-highlight bonus is right where it is written and right
where it is recomputed — it is the ordering of the two that is wrong. The
timestamps are right in the database and right in the browser; only the contract
between them was unstated. Tests that exercise one component at a time cannot
see any of this, which is why the regressions in
`tests/test_phase7_reported_bugs.py` are written as end-to-end sequences —
open the chart, write a note, reload — rather than as unit assertions.

### D-059 · A suggestion's id is stable across regeneration (defect found in Phase 7)

`refresh_entry_highlights` deleted every `SUGGESTED` row and re-created the
survivors with fresh uuids. That function runs on **every write to the chart**:
entry create, edit, revert, supersede, task create, task status change, comment
resolve, voice capture, and each highlight accept or reject.

A highlight's id is what the Glance View hands back to `POST
/highlights/{id}/accept`. So the card a clinician was looking at held ids the
server had already deleted. Confirming one suggestion regenerated the other
five, and every subsequent Confirm returned `404 Highlight not found`:

```
suggested on card: 6
accept first -> 200
  accept next -> 404 Highlight not found     (x5)
```

The single-click-then-reload flow in `GlanceView.decide` masked it, because the
reload fetched fresh ids before the next click. It surfaced the moment anything
made the open card stale — two quick confirmations, a colleague adding a note,
the scribe finishing.

**Suggestions are now updated in place, keyed on `(span_start, span_end)`.** The
same words are the same claim, so the same row carries them; only spans that
stop being candidates are deleted. Ids survive regeneration, and an accept
issued against a card a few seconds old still resolves.

Keying on the span rather than on a content hash is deliberate. A span whose
text changed under it is still the same claim about the same place in the note
— that is what `source_version_number` and the `stale` flag already exist to
say. Re-minting the id there would throw away a clinician's in-flight decision
to signal something the UI already signals.

**What is still wrong with it:** editing an entry genuinely does move its spans,
so suggestions on the *edited* entry still get new ids. That is correct — the
offsets no longer mean the same thing — but it means "your open card keeps
working" holds for every entry except the one you just changed.
`test_unrelated_writes_do_not_renumber_open_suggestions` pins exactly that
boundary rather than pretending it is absolute.

### D-060 · The what's-new marker is seeded on first view and capped in age (defect found in Phase 7)

Two defects in `glance.touch_view`, both reported as "since your last visit
doesn't update", and both affecting clinician, staff and admin identically.

**First, the marker was NULL for a whole session.** The first view stored
`previous_viewed_at = None` and returned `None`, which is right — captioning an
entire chart as new on the one view that most needs to be readable is noise.
But it left nothing for the *next* load to compare against, and that load
returned `None` too, and so did the one after. A clinician could open a chart,
write a note, reload, and be told this was their first look with nothing new:

```
1st open                            first_visit=True  since=None  new=0
reload right after writing a note   first_visit=True  since=None  new=0
reload again                        first_visit=True  since=None  new=0
```

`previous_viewed_at` is now seeded to `now` on insert. The function still
returns `None` on the genuine first view — that behaviour was never the bug —
but the second load of a session compares against the moment the session
started.

**Second, the marker never rolled forward for an active user.** It only advanced
when the gap between two *consecutive page loads* exceeded `VIEW_SESSION_GAP`
(20 minutes). Someone with the chart open, refreshing through a shift, never
opened such a gap, so the window only ever widened and "new since your last
visit" quietly became "new since this morning".

`MAX_MARKER_AGE` (4 hours) now caps how stale the comparison point may get: past
it the marker advances on the next load even mid-session. Roughly one clinic
session — long enough not to interrupt a working one, short enough that the
label on the section stays true.

**The trade-off, stated plainly:** four hours is a guess. It is a named constant
rather than an inline literal precisely because it is the kind of number that
should be argued with, and ideally set from how these charts are actually used
rather than from how we imagine they are. D-033's guarantee — that reading the
what's-new group does not destroy it — is unchanged and is pinned by
`test_the_marker_holds_still_across_a_rapid_refresh`, so a future adjustment to
the cap cannot silently reintroduce the refresh-eats-the-news bug.

### D-061 · Every timestamp leaves the API with an explicit UTC offset (defect found in Phase 7)

Every datetime here is UTC. SQLite's `DATETIME` column has no timezone, so
SQLAlchemy returns naive datetimes, and Pydantic serialises a naive datetime
with no offset at all. The API was emitting two different things side by side:

```
glance.generated_at    : 2026-08-28T00:52:43.329852+00:00
glance.since           : 2026-08-27T23:22:42.518090+00:00
entry.timestamp        : 2026-08-28T00:52:42.767309          <- no offset
highlight.entry_ts     : 2026-08-27T00:52:41.484561          <- no offset
open_action.created_at : 2026-08-25T00:52:41.493042          <- no offset
```

ISO 8601 says a date-time with no designator is **local time**, and browsers
follow it. Verified with `TZ=Asia/Singapore` — the timezone this build was
written in and demoed from:

```
naive parsed as: 2026-08-27T16:52:42.767Z -> age 8.00h
aware parsed as: 2026-08-28T00:52:42.767Z -> age 0.00h
```

A note written seconds ago rendered as **"8h ago"**. West of UTC the arithmetic
went negative and `relativeAge` returned "just now" for everything inside the
offset. Date group headings in the timeline landed on the wrong day. And it was
*inconsistent*: `since` was built from an already-aware value and converted
correctly, so the "since 08:52" hint sat directly above entry ages measured in a
different frame — which is worse than being uniformly wrong, because it looks
like the data disagrees with itself.

`app/core/timeutil.py` now holds the single answer. `as_utc` labels a naive
value as UTC (it is never anything else here); `UtcDateTime` applies it as a
Pydantic `BeforeValidator` on every response-model datetime field; `iso_utc`
does the same for the hand-built dicts in `services/glance.py`, which returns
plain dicts and so cannot use the annotation.

**Nothing about storage changed.** Migrating the columns to
`DateTime(timezone=True)` would be the deeper fix, but SQLite has nowhere to put
the offset, so it would change the declaration without changing the behaviour —
a fix that reads better than it works. The contract at the boundary is where the
ambiguity actually was, so that is where it is resolved.

**What is still wrong with it:** this is enforced by convention plus a
regression test, not by the type system. A new response model that writes
`created_at: datetime` will silently reintroduce the bug.
`test_glance_timestamps_are_all_offset_qualified` and its three siblings walk
the actual payloads rather than the annotations, so a new *field on an existing
surface* is caught; an entirely new endpoint is not, until someone adds it to
the sweep.

### D-062 · Tasks can be closed from the Glance View (defect found in Phase 7)

`POST /tasks/{task_id}/status` has existed since Phase 2.5 and works. `Api.
setTaskStatus` has existed in the client since the same phase. **Nothing ever
called it**, and `Api.tasks` was never fetched either — so a task could be
raised from a comment thread and never finished. "Open actions" only grew.

This was not only a missing button. `services/scoring.action_score` reads the
open-task count, so an action nobody could close kept its entry's highlights
pinned to the top of the card indefinitely, and `refresh_patient_highlights`
faithfully recomputed that wrong answer on every write. A stuck task quietly
distorted the ranking it was supposed to inform.

Mark done / Cancel are now inline on task rows, single-click, no navigation —
the same interaction cost as accept/reject, and for the same reason set out in
Phase 2.4: an affordance with friction on it does not get used, and an
outstanding item nobody ticks off stops meaning anything.

Cancelled tasks are set to `cancelled` rather than deleted, consistent with how
rejected highlights are kept (see the module docstring in
`routes/highlight_routes.py`). The `AuditLog` row records who closed what and
when.

**Scope note:** the fix is a UI wiring, so it is covered by API-level tests
(`test_closing_a_task_removes_it_from_open_actions`,
`test_staff_can_close_a_task_assigned_to_them`) and by nothing that exercises
the button itself. There is no frontend test harness in this build — recorded
here as a known gap rather than papered over.

### Also fixed in this pass, not warranting their own decision

* **`Primitives.readSelectionRange` mishandled element-node selection
  boundaries.** A browser reports a selection anchored on an element with an
  offset that is a *child index*, not a character offset. Triple-clicking a
  paragraph, or dragging past the last character, produced a small number that
  was read as a character position, so the highlight landed a few characters
  into the entry instead of on the selected words. Element boundaries are now
  resolved to the character offset before that child, or to the end of the last
  child when the index is one past the end.

* **`GlanceView`'s optimistic `decided` map survived the reload.** Keyed by
  highlight id and never cleared, it left a "Confirmed" pill attached to an id
  the server had already answered for. Cleared on each new payload; the server
  is the single source of truth for a decision.

* **`whats_new.count` could exceed the list under it.** `MAX_WHATS_NEW` caps the
  entries but `count` is the true total, so the card could say 12 and show 8.
  Now says "and N more in the timeline below" rather than silently disagreeing
  with itself.

### Considered and deliberately not fixed

* **`PATCH /entries/{id}` clears the title when `title` is omitted.** The field
  defaults to `None` and `_append_version` writes it through, so a content-only
  edit from any client that is not our own UI silently drops the title. The fix
  is a `model_fields_set` check, but it changes the meaning of an existing
  request shape — "absent" would stop meaning "null" — and that deserves its own
  decision and its own test rather than being folded into a bug-fix pass.

* **Staff are told a clinician correction exists that they cannot read.**
  `supersede_entry` writes the correction as `CLINICIAN_SECTION`, which staff
  may not view under D-004. Staff see the original chipped "Disputed — see
  correction" and a "Correction on record" row on the Glance View, and clicking
  either goes nowhere they are allowed. This is a real consequence of the
  least-privilege default, not an accident — but it is currently an undocumented
  dead end. Resolving it means either widening D-004 or suppressing the chip for
  staff, and both are policy changes, not fixes.
