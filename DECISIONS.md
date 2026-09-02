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

### D-063 · A frontend test harness, scoped to what fails silently

Recorded as a known gap in the Phase 7 pass and closed immediately afterwards,
because the gap had a specific shape: three client fixes shipped with no test
that could see them, and one of the three — `readSelectionRange` — fails in the
worst possible way. It does not throw. It creates the highlight successfully, at
the wrong offsets, and mints a `provenance_pointer` at words nobody selected. In
a system whose entire argument is that every claim traces back to its source, a
citation that lands a few characters off is a worse failure than a crash,
because nothing about it looks wrong.

**vitest with jsdom, not a real browser.** What these tests cover is offset
arithmetic and conditional rendering, and neither needs a compositor. The one
thing jsdom genuinely cannot do is lay text out, so selections are built as
explicit `Range` objects rather than by simulating a drag — which is not a
workaround so much as the honest version of the same test: a `Range` is exactly
what `window.getSelection()` hands the real code, with the node and offset pair
stated rather than inferred from geometry.

**`Api` is mocked in the component tests.** What is under test is the
component's contract with the client wrapper — that Mark done sends `done` for
the right task id, that a decision triggers a reload — not the wrapper's
contract with the server. That second contract is already covered end to end,
against the real app and a real database, by `tests/`. Testing it twice in a
weaker environment would add confidence in proportion to nothing.

**Verified against the defects, not just against itself.** Ten of the 25 fail
when the two components are reverted to their Phase 6 state (4 of 12 in the
selection suite, 6 of 13 in the Glance View suite), checked by `git show`-ing
the old files back in and re-running. A regression test that has never seen the
regression is a description of current behaviour wearing a test's clothing.

**Scope, deliberately narrow.** `EntryCard`, `Comments`, `Timeline`,
`VersionHistory`, `VoiceCapture` and `PatientHome` have no component tests. The
two files chosen are the ones where a defect is invisible rather than loud —
everywhere else, a break shows up as a blank panel or a console error the first
time anyone opens the page. Writing thin tests across all nine components on the
final day would have bought coverage percentage rather than confidence.

**What is still wrong with it:** nothing exercises the real `fetch` path, so a
change to `lib/api.js` — a wrong URL, a dropped `credentials: 'include'` — is
caught by neither suite. There is no end-to-end browser test. And the harness
adds five devDependencies (recorded in `ATTRIBUTION.txt`) to a project that
previously shipped a frontend with none, which is a real cost for a 72-hour
build and is the reason it was deferred past Phase 6 in the first place.

### D-064 · The brief PDF is built by a script, and the toolchain note was wrong

`docs/TECHNICAL_BRIEF.pdf` was previously a one-off export, and `ATTRIBUTION.txt`
described it as rendered with headless Chrome. Two problems surfaced when the
Phase 7 fixes made the Markdown source move: the PDF silently went stale, and
Chrome was not available to regenerate it, so the attribution described a tool
that was not in fact used.

`scripts/build_brief.sh` now renders it — pandoc for Markdown, wkhtmltopdf for
layout, qpdf to drop the blank trailing page wkhtmltopdf emits when content ends
near a page boundary. The script **fails rather than installs** if the result
falls outside the required 2–3 pages, because the type sizes in it are tuned to
that constraint and the failure mode otherwise is a four-page brief nobody
notices until a reviewer does.

`ATTRIBUTION.txt` records the real toolchain and states that it changed. These
are build-time command-line tools rather than libraries: nothing in `backend/` or
`frontend/` imports them and their licences do not reach the source, but naming
them is cheaper than leaving a reviewer to wonder.

**The brief is now 2 pages rather than 3**, having grown by the Phase 7 material
and then been trimmed harder elsewhere — mostly the multilingual and deferred-
scope paragraphs, which were the longest passages carrying the least argument.

**Latency figures were re-measured, not carried forward.** The Phase 7 changes
touched the serialisation of every timestamp in the Glance View payload, so
quoting the Phase 6 number would have been asserting something no longer
tested. Three runs: P95 11.54 / 11.09 / 11.63 ms, against 14.26 / 13.30 / 15.94
in Phase 6. Lower, on a deeper chart — which is container load, not an
optimisation, and is written up that way in `ARCHITECTURE.md`. The older figures
are kept visible beside the new ones rather than overwritten.

---

## Phase 8 — Evaluation and abstention (2026-08-28)

Prompted by the 48-hour hint, which asks of the risk badge, the confidence label
and the importance score: *what is it, how would we know if it were wrong, and
what happens when it is?* Auditing the build against that question found three
things already answered well (extractive attribution, redaction accuracy, the
fatigue floor), two answered only on one code path, and two not answered at all.

The shape of the miss is worth naming: in every case the *mechanism* existed and
the *guarantee* did not. `_infer_risk()` was written, tested and correct — and
ran on one of two paths. Confidence was derived from real evidence — on one of
two paths. A guarantee that holds on the path you happened to exercise is not a
guarantee; it is a coincidence with good documentation.

### D-065 · Confidence is measured from the source on every path, never self-reported

The offline summariser derived confidence from hedging density in the
transcript. The live-model path took `parsed["confidence"]` — the model's own
opinion of its reliability — clamped it to 0..1, and displayed it. Self-reported
confidence is the thing the hint calls decoration, and it was decorating exactly
the path a real deployment would use.

`derived_confidence()` now runs on both paths and is what the clinician sees. A
live model's self-report is stored in `model_self_reported_confidence` and never
displayed: kept because a self-report that tracks the derived figure is evidence
the model is calibrated and one that does not is evidence it is not, which is
worth knowing later; not shown, because a number the model chose about itself
cannot be checked by the person reading it.

**Bands are numeric and defined once.** `high >= 0.75`, `medium 0.60-0.75`,
`low < 0.60`. The chip renders the word and the percentage together, so "medium"
is never a floating adjective. Two defects surfaced while doing this and are the
reason the constant is now singular:

* The hedging formula existed **twice** — in `_extractive_summary` and in the
  new `derived_confidence` — which is precisely the drift the single-definition
  rule exists to stop. Collapsed.
* `glance.LOW_CONFIDENCE_THRESHOLD` restated `0.6` independently of the band
  boundary. Nothing tied them together, so the card could have rendered
  "medium" while the low-confidence flag fired beside it — worse than either
  being wrong alone, because a reader who sees an interface contradict itself
  stops believing all of it. Now imported, with a test asserting equality.

**Honest limit:** hedging density measures how certain the *speakers* were,
which correlates with but is not the same as how well the summary is supported.
It has one property self-report does not — it is computed from something a
reviewer can go and read. A number that can be checked against the transcript is
worth more here than a better-calibrated one that cannot.

### D-066 · Deterministic rules set a floor under risk; a model may only raise it

`_infer_risk()` matches explicit high-risk terms and tagged clinical entities and
is fully deterministic. It ran only when no live model was available. On the live
path, `_coerce_risk(summary["risk_level"])` took the model's ordinal at its word
— so a model that quietly called a transcript containing "chest pain" `low` would
have moved the badge down, and nothing would have noticed.

The stored level is now `max(model_proposed, deterministic_floor)`. The asymmetry
is the whole point: **ordinal drift is only dangerous in one direction.** A model
raising a level may have noticed something the keyword tables miss, which is a
recall gain worth having. A model lowering one silently removes a warning, which
is the failure the badge exists to prevent. Rules are a floor, not a ceiling.

`model_proposed_risk` and `risk_floor_applied` are both persisted, and a "Risk
set by rule" chip renders when they disagree, so a clinician asking "why does
this say high?" can distinguish *a rule matched words in the transcript* from
*a model felt strongly* without opening anything.

### D-067 · Patient-facing content is a severity class, enforced structurally

Showing a clinician a hallucinated line is a bad day — internal notes carry
provenance, get audited, and sit in front of someone trained to disbelieve them.
Showing a **patient** one is a different category of harm: no second reader, no
provenance rail to open, no basis to doubt.

The build already had the right behaviour — the scribe never wrote
`patient_summary` or `patient_instruction`, and AI types are absent from the
patient's viewable set — but it was an accident of how the code happened to be
written, not a rule anything enforced. Nothing would have failed if a later
edit had changed it.

**The rule is structural rather than procedural, deliberately.** The obvious
alternative is to generate patient-facing text and require clinician approval
before it publishes. Under time pressure, an approval step is a thing people
click through; it produces an audit trail showing a human approved something
they did not read. So instead: `PATIENT_FACING_TYPES` is writable only by
`Role.CLINICIAN`, `Role.SYSTEM` appears in no `WRITABLE_TYPES` entry at all, and
`assert_never_patient_facing()` runs at **import time** against the scribe's own
type map so a future edit fails to load rather than shipping model output to a
patient. A clinician may read an AI summary and write an instruction from it —
that path is intended, and the human authorship is real because they type the
words.

`test_the_guard_actually_fires` exists because a guard nobody has watched fail
is not known to work.

### D-068 · Contradictions between entries, including human-human, detected and never resolved

Phase 2 answered the question the brief asked: what happens when a clinician
disagrees with an AI note. That is the easier question, because the resolution
rule is given. It is not the question that hurts people.

**Clinicians and nurses contradict each other**, in different notes, hours apart,
and nobody is wrong on purpose. A nurse records "allergic to penicillin"; a
clinician, reading a different part of a fragmented record, prescribes
amoxicillin. Neither note is AI output, so no precedence rule applies, and
neither author can see the contradiction — because the fragmentation this
product exists to fix is the reason they are not reading the same page.

`services/contradictions.py` detects three classes, scoped to where being wrong
is worst: **allergy against administration** (including across drug class,
which is what catches penicillin→amoxicillin), **dose disagreement** on the same
drug, and **status disagreement** (started here, stopped there).

**Extraction, not inference, and no model.** Every finding is a regex or
vocabulary match over stored text, so it can be re-derived, points at the exact
two entries that produced it, and cannot drift between runs. A model asked "do
these contradict?" would be more sensitive and would also produce confident
disagreements that are not there — and a false contradiction on an allergy is
not a harmless false positive. It teaches a clinician that the flag means
nothing, which disarms it for the case that matters. Two such false positives
were caught in smoke testing before the tests were written:

* `1g` and `1000mg` read as a dose conflict. Mass units now normalise to mg.
* "Patient denies allergy to aspirin" fired a **critical** allergy flag. The
  main extraction loop checked negation; the free-text allergen fallback did
  not, and helpfully re-added the mention the main loop had correctly skipped.

**It resolves nothing.** There is no precedence rule for human-human
contradiction and inventing one would be a clinical decision this system has no
standing to make — "most recent wins" would silently discard an allergy recorded
last year in favour of a prescription written today. Both entries are surfaced,
both are quoted, both are clickable, and a person decides. The system's job here
is to make the disagreement impossible to miss, not to settle it.

It renders above everything else on the Glance View, including "what changed". A
clinician who reads exactly one line of the card should have read the most
dangerous thing the system knows.

**Recall is honestly limited** and stated in `ARCHITECTURE.md`: detection rests
on `features.MEDICATIONS`, a watchlist rather than a formulary, and on doses
matching a numeric pattern. Negation handling is a 40-character lookbehind, not
real scope detection. A contradiction in vocabulary this module does not know is
simply not found — the failure mode is silence, never a wrong answer. A clinician
who believes this catches everything is worse off than one who knows it catches
three things well.

### D-069 · One suggestion slot is reserved against exposure bias

The learning loop only ever sees feedback on spans it chose to surface. A tag
scoring just below the cut is never shown, so never accepted, so never gains
weight, so is never shown. The ranking converges on whatever it favoured early
and has no mechanism for discovering it was wrong. This is a structural property
of learning from your own output, not a tuning problem, and it was previously
not addressed anywhere in the build.

One slot per entry is reserved for a candidate carrying a tag the clinic has
never given feedback on, when one exists and clears `MIN_SUGGESTION_SCORE`.

**Deterministic, not epsilon-greedy.** A coin flip would make the Glance View
differ between two loads of the same unchanged chart. On a clinical surface that
is a worse property than the bias it fixes: a clinician who sees the card change
under them stops trusting that it reflects the record. Selection is a function of
(entry content, feedback history), so the same chart shows the same card, and the
exploration slot resolves itself the moment the clinic gives feedback on that tag
once.

It displaces the **weakest** of the top candidates, never the strongest, and is
bounded by the same minimum-score floor as everything else — so it can promote an
under-explored candidate over a marginally better known one, and can never
surface something the rules found clinically meaningless.

**This narrows the loop; it does not close it.** Feedback is still only ever
collected on surfaced items, and a tag that never appears in any entry is never
explored. A real answer needs off-policy evaluation against held-out charts,
which needs data this build does not have.

---

## Phase 9 — addressing the clinic-scenario review

### D-070 · An outage is a decision point for the caller, not a crash

Scenarios 8 and 9. The build had a real extractive summariser and never ran it
when it mattered: the fallback triggered when the model returned a **successful**
response containing unparseable JSON, and a provider `503` raised
`httpx.HTTPStatusError` straight through `run_scribe` to an unhandled 500. The
degradation was wired to the wrong failure mode — the one where the model answers
badly, not the one where it does not answer.

Why it survived the whole build: `CARENOTE_LLM_PROVIDER` defaults to `stub`, and
the stub is in-process. It cannot time out, refuse a connection or return a 503.
Every test run, smoke script and demo executed against a provider physically
incapable of failing. That default is still correct — it makes the build runnable
without an API key and the tests deterministic — but it bought that determinism by
removing the entire failure surface from view, and nothing made the trade visible.
`_UnavailableProvider` plus `CARENOTE_LLM_FORCE_UNAVAILABLE` now puts it back, so
the outage path runs on every test run.

**The chokepoint translates, the caller decides.** `llm_client` converts timeouts,
transport errors, 5xx and 429 into one `LLMUnavailableError`. It does not choose
what to do about them. That belongs to each caller, because the right answer
differs by purpose: the scribe degrades to the deterministic summariser, and a
patient-facing generator would refuse outright (D-067). A blanket retry or a
blanket fallback inside the chokepoint would take that judgment away from the only
code that has the context to make it.

**4xx stays loud.** A 400 or a 401 is our bug — a malformed request, a bad key.
Degrading would hide it behind a summary that merely looks lower quality, and it
would be indistinguishable from a real outage in the audit log.

**A degraded note is labelled as degraded.** `model_used` is
`offline-extractive-v1:provider-unavailable`, distinct from the plain
`offline-extractive-v1` used when no model is configured at all. An unlabelled
fallback is arguably worse than an error: the clinician reads a thinner summary
with no way to know the model never ran.

**Timeout 60s → 8s.** 60 seconds is a batch-job timeout. A clinician is standing
next to a patient. A summary that takes longer than eight seconds has already
missed the consult it was meant to support.

**Known gap.** No circuit breaker: during a sustained outage, the 400th consult of
the hour still waits the full timeout to learn what the first one learned.
Bounded now rather than unbounded, but still paid per request.

### D-071 · Failures are logged by type and reference, never by message

Scenario 3. Redaction before the model was guarded carefully. The other exits were
not. `main.py` had no exception handlers at all, so an unhandled error reached
Starlette's default and uvicorn logged the full traceback — and SQLAlchemy embeds
bound parameters in its exception messages:

    [SQL: INSERT INTO versions ...]
    [parameters: ('e1', 2, 'Amira Rahman, NRIC S8412345D, allergic to penicillin')]

Name, NRIC and clinical content in one line, retained for as long as the container
logs are, with no rotation and no scrubbing.

`log_event` (D-014) makes it hard to log content **on purpose**. This is the other
case: content logged on our behalf by code we did not write. The threat model was
"a developer writes a careless `print()`". The real one was "a dependency logs
content while nobody is calling your function at all."

**Middleware, not `@app.exception_handler(Exception)`.** Starlette's
`ServerErrorMiddleware` calls a registered handler and then *re-raises* so the ASGI
server can log the traceback. A handler alone sanitises the response and leaves the
log leak untouched. Catching inside the middleware stack means the exception never
reaches `ServerErrorMiddleware` and uvicorn never prints it.

**Type name and route only, never `str(exc)`** — the message is exactly where the
parameters live. An eight-character reference goes to both the client and the log,
so a clinician can quote it and an engineer can find the request without the log
holding a patient.

`PHILeakError` and `LLMUnavailableError` are named to the client because their
messages are built from category names and carry no values, and because "nothing
was sent to the model" and "the model is down" are different things a clinician
should be able to tell apart. Everything else is opaque.

**Known gap, since narrowed — see D-083.** Uvicorn access logs still record the
full request line. This entry originally described that as "request paths, which
contain patient UUIDs — pseudonymous rather than identifying," which was too
generous twice over. The access log records the query string as well as the path,
it records it for requests that never reach the application at all (404s, RBAC
refusals, malformed ids), and nothing constrained what a URL was allowed to
carry. D-083 found an actual phone number going out that way and turned the
convention into a tested invariant. The log itself is still unrotated and
unscrubbed, and there is still no retention policy.

### D-072 · The risk floor works in tag space, and says so when it cannot read

Scenarios 6 and 14. Two defects with one cause.

`_infer_risk` matched a tuple of **English phrases** against raw text and only
fell through to `tag_span` afterwards. So the tagger was bilingual (D-058) and
the deterministic safety floor sitting on top of it was not. Measured before the
fix:

    "chest pain when I walk uphill"   -> high
    "sakit dada bila naik tangga"     -> medium

The same symptom rated lower because of the language the patient used. The
mechanism I am most confident about — a floor a model cannot lower — was
language-dependent, and the reason is worth naming: **D-058 fixed the assumption
at the tagger and nothing went looking for other layers that shared it.** The
floor was a separate code path making the same English-only assumption, and the
Malay fixtures all passed because they only ever exercised the tagger.

`HIGH_RISK_TAGS` is now a set of canonical tags, so the floor inherits every
language the tagger knows, now and whenever the vocabulary grows. Adding a
language becomes one change instead of two, and the second one can no longer be
forgotten.

It also surfaced a defect that had nothing to do with language: `fainted` was
absent from the old English list, so "she fainted at the bus stop" rated medium
in English too. `symptom:fainted` is in the tag set; syncope and fainting are the
same event and patients use the second word.

**Negation, asymmetrically.** The floor had no negation handling, so "denies
chest pain, no shortness of breath" — a clean history — rated high. That fails
loud rather than silent, which is the right direction, but alert fatigue is the
mechanism by which loud failures become silent ones. A symptom is now dropped
only if it appears *exclusively* inside a negation: one un-negated mention
anywhere sets the floor, because "no chest pain Monday, chest pain today" must
rate high. A denied symptom is downgraded to medium, never dropped — a pertinent
negative is still clinical content.

`NEGATION_RE` moved to `features.py` and `contradictions.py` imports it. Two
copies of a negation rule drift, and the two consumers would then disagree about
whether the same sentence asserts something.

**Abstention over silence.** Romanised Hokkien is transcribed faithfully, stored,
and then produces no tags, no risk level, no highlight and no card. The words sit
in the timeline where a human could read them, and the Glance View is silent
about the reason for the visit — silent *confidently*, with nothing to indicate
the tagger did not understand the language it was given.

The fix is not more vocabulary. That is an arms race this build loses, and every
round of it leaves the same failure mode intact for the next language.
`is_unreadable()` flags a turn that is substantive, produced no tags, **and** is
in a language outside the supported set, and the Glance View says so plainly.
Deliberately conservative on all three conditions: English small talk produces no
tags either and is not a gap in understanding.

The flag is separate from the low-confidence flag, for the same reason
`confidence_flags` is separate from `risk_flags`: "the system read this and is
unsure" and "the system did not read part of this" are different warnings and a
clinician acts on them differently.

A romanised Hokkien turn is now in the doctor-consult transcript and the clinical
capture fixture. Without a fixture the gap is invisible in every test and every
demo, which is exactly how it survived the original build.

**Known gaps.** Language identification is taken from the ASR provider's tag and
is not verified — a recogniser that mislabels Hokkien as English produces no flag.
Nothing yet flags *untagged* content in a supported language, which is the
larger recall gap. `unreadable_segment_count` is a new column; `create_all` does
not migrate an existing SQLite file, so a dev database from before this commit
needs re-seeding.

### D-073 · A denial is a claim, not an absence

Scenario 13. A nurse recorded a penicillin allergy; the patient told the AI she
had no known allergies. Both were in the timeline and `detect()` returned **zero**
contradictions. Verified by running it, not by reading the code.

The cause is a guard that is correct on its own. `_extract_claims` drops negated
mentions so that "patient denies allergy to aspirin" never becomes a critical
allergy alert. Dropping them also discarded the patient's denial, so it was never
compared against anything. The extractor could represent an assertion and an
absence, and had no way to represent a **denial** — a stated position that can
disagree with another stated position.

**Why the gap matters more than the missing alert.** The disagreement is the
signal. "Allergy recorded, patient denies it" means the patient forgot, or was
never told, or it was charted against the wrong record, or it was an intolerance
rather than a true allergy — and a clinician needs to know which. Showing only the
allergy is safe and wastes the one thing a longitudinal record exists to produce.
Showing only the denial would be lethal.

Negated allergy mentions now become `allergy_denial` claims, and `_BLANKET_DENIAL`
captures the form the scenario actually takes — "no known allergies", "NKDA", "nil
known allergies" — as a denial of `ANY_ALLERGEN`. Negation outside an allergy
context stays dropped: "not started on warfarin" contradicts nothing on its own.

**HIGH, not CRITICAL.** Unlike allergy-vs-administration, nothing dangerous has
happened yet — the safe action is already the one in force. This is a
reconciliation task, not an alarm. Rating it critical would dilute the level that
means "someone is about to be given a drug they react to", and that is the level
that has to keep working.

**The assertion always reports first**, so the clinician reads "allergy
recorded … but denied" rather than the reverse. As with every other class here,
the system reports and does not resolve: there is no precedence rule between a
nurse's note and a patient's own account, and inventing one would be a clinical
decision this system has no standing to make (D-068).

**A defect found while testing this.** The first blanket-denial pattern matched
"denies allergy **to aspirin**", so a specific denial of one drug registered as a
blanket denial of all of them and contradicted an unrelated penicillin allergy.
Fixed with a negative lookahead; pinned by
`test_a_denial_of_one_drug_does_not_contradict_an_allergy_to_another`. A
contradiction detector that cries wolf is worse than one with gaps — the gap
loses a finding, the false positive teaches people to stop reading all of them.

**Known gaps.** Temporal ordering is not considered: an allergy recorded in 2019
and denied today reads the same as the reverse, though the second is far more
likely to be a genuine correction. Denials are only compared against allergies,
not against dose or status claims. And a denial in a language the vocabulary does
not cover is not detected at all (D-072).

### D-074 · Reach is modelled; delivery is not, because nothing delivers

Scenarios 11 and 12. There is no sender in this build — no email, no SMS, no
WhatsApp, no push — and this decision does not add one. The failure it fixes is
narrower and worse than the missing integration:

> A clinician writes "come back in two weeks for a BP check", marks it done, and
> moves on. The patient never opens the portal. The instruction is correct,
> versioned, traceable and unread, and the system reports success.

A build that cannot send is a limitation. A build that cannot tell you it did not
send is a false assurance. The second is what shipped, and it is the part that is
fixable without an integration.

**Three states, and one deliberately absent.** `unread`, `read`, and `corrected`
— the patient read an earlier version and it has since changed. `dispatched` is
**not** modelled, because nothing dispatches; inventing the state would put the
same false assurance in a new place. When a sender exists, it slots in between
`written` and `read` and the two existing states keep their meaning.

**No new storage.** `PatientView` has recorded the patient's own read timestamps
since D-033. Nothing had ever asked it this question. The data to detect "written
but never read" was already being collected, which is worth saying plainly: this
was not a missing capability, it was a question nobody thought to ask of data
already in the database.

**`corrected` is the scenario-12 answer.** Patient-facing content is already
protected on the generation side — no machine-written text can reach a patient at
all (D-067) — so a wrong dosage in an instruction was typed by a clinician, which
is the right place for that risk to sit. What was missing was the correction path.
The clinician edits, a new version is recorded, the original is preserved, and the
patient sees different text with nothing marking it as a correction. She took the
wrong dose on Tuesday; on Friday she sees a different number and has no way to
know it contradicts what she read. The patient view now leads with a plain-language
banner: *"This was updated after you last read it… if you were following the
earlier version, stop and read this one."*

**Corrections are computed before `touch_view`.** That call rolls the read marker
forward on page load, so computing after it would let the warning vanish on the
exact page load meant to show it — the same shape of defect as D-060, caught this
time because D-060 had already taught us to look for it. Pinned by
`test_reading_the_page_does_not_swallow_the_correction`.

**Unreachable is distinct from unread.** If no `User` row links to the patient,
the clinician summary reports `reachable: false` rather than counting it as merely
unopened. "She has not read it" and "there is no way for her to read it" are
different problems for a clinic, and collapsing them would hide scenario 1 behind
scenario 11.

**Known gaps.** Read state is per-patient, not per-entry: opening the portal marks
everything current as read, so a patient who opens the page and reads nothing
registers as having read it. Per-entry acknowledgement is the honest version and
is not built. Nothing yet escalates a correction that stays unread — there is no
scheduler, and no channel for it to escalate to.

### D-075 · Enrolment is clinic work, not a developer task

Scenario 1: a patient who exists for the clinic as a phone number in a WhatsApp
thread. Scenario 5: a second clinic onboarding on Monday.

**The identity model was never the obstacle.** There is no email column anywhere
in this schema — login is username plus password, so a phone number works as a
username today with no code change. What did not exist was any way to create the
row. Every account in the build existed because a developer ran `init_db.py`. A
nurse holding the number had no screen anywhere that turned it into a record. The
patient was not rejected; she was unreachable, which in a clinic is the same
outcome.

That absence was invisible during the build for a specific reason worth naming:
`init_db.py` runs in Phase 1 step 1, so from the first commit onwards every test,
script and demo started from a fully populated database. "How does a patient come
to exist?" never arose as a question because patients always already existed. The
seed silently stood in for a capability. **Anything a seed script provides is a
feature you have not built and will not notice missing.**

**A second, quieter exclusion in the schema.** `Patient.dob` and `Patient.mrn`
were both `NOT NULL`. Requiring a date of birth before a patient can exist
excludes anyone who does not know it or will not give it at a front desk;
requiring an MRN means she cannot be registered until some other system has
assigned one. Both are now nullable, and enrolment issues a provisional MRN
rather than refusing. This was found by writing the route, not by reading the
model — the constraint only became visible when something tried to insert a real
walk-in.

**`identifier_type` is explicit** — `phone`, `nric`, `mrn`, `internal`. A username
that happens to contain digits is not the same as the clinic knowing it identifies
this person by her phone number, and the difference matters the first time
somebody tries to reach her.

**A login is optional.** Plenty of patients will never use a portal, and forcing a
credential nobody wants produces dormant accounts that look like reach. The Glance
View reports `reachable: false` instead (D-074), which is the honest version.

**Phone validation is deliberately permissive** — `+65`, `01x-`, spaces, dashes.
Strict validation is exactly how you exclude the person this route exists for.

**The passcode is returned once and never stored.** Six digits, because it gets
read aloud or written on an appointment card; a long random string would be copied
down wrongly, which is its own kind of access failure. Only the hash is persisted,
and the passcode is never logged.

**Scoping is unchanged, not re-implemented.** `clinic_id` comes from the caller's
token and is not accepted from the body, and `issue_login` goes through
`scope.get_or_404`, so a staff member in Clinic A cannot enrol into or issue
credentials for Clinic B. Pinned by `test_staff_cannot_issue_a_login_for_another_clinics_patient`.

**What this does not fix.** Clinic creation is still `init_db.py` — deliberately,
since provisioning a tenant is not routine clinic work and wants an operator path
rather than an in-app button. The rest of scenario 5 also stands: clinical
vocabulary, red-flag terms, decay thresholds and confidence bands remain
module-level constants shared by every clinic, so a second clinic still cannot
tune any of them without a deploy. That is the larger half of the multi-tenancy
gap and it is not addressed here.

### D-076 · Stale provenance shows both versions, not just a warning

The reviewers' item 16 gained a sentence in the second round: *"Can the system
mark dependent output stale and show the original and current versions side by
side?"* We had the first half (D-030) and not the second.

Telling a clinician "the source note changed" without showing what it changed to
leaves them to open the entry and diff it by eye — which is the work the system
was supposed to do. Staleness was addressable but not yet *inspectable*.

`highlights.current_text()` is the deliberate counterpart to `anchored_text()`.
`anchored_text` refuses to show current content under an old claim, because that
would be a quiet lie about what a clinician confirmed. `current_text` exposes the
same coordinates in the live entry explicitly, so the UI can put them side by
side and label which is which. The two functions disagree on purpose, and the
comparison block is where that disagreement becomes useful.

**Returns None when the offsets no longer land inside the content** — an entry
edited shorter is the common case. The card then says "This part of the note no
longer exists", which is a real answer. A truncated fragment would read like a
quote and be worse than saying nothing.

Both version numbers are named on the card (`v2 → v5`) rather than only the fact
of a change, so a clinician can go to the version history and see exactly which
edit did it.

**UI-only tests, added at the same time.** `Phase9Surfaces.test.jsx` covers this,
the delivery states, the unreadable-content flag and the patient correction
banner. Every one of those had correct backend logic and nothing asserting that
anything rendered it — and a delivery state computed correctly and never drawn is
indistinguishable, from the clinician's side, from not having been computed. One
real regression was caught while writing them: `confidence_flags` was keyed on
`entry_id` alone, so an entry raising both an unread and a low-confidence flag
collapsed into a single React child.

### D-077 · A slow model call can be abandoned

Scenario 8's remaining half. D-070 bounded the wait at 8 seconds; this gives the
clinician a way out before it elapses. `runScribe` now carries an `AbortSignal`
and the processing card has a Cancel button.

Safe to abandon for a reason that is an accident of ordering rather than a
design, and worth naming as such: transcript segments are written **before** the
model is called (`scribe.py`), so cancelling loses the summary and never the
consult. Cancellation is reported as a plain statement rather than an error —
*"Summary cancelled. The transcript was saved — you can retry"* — because the
clinician chose it and nothing went wrong.

**Known gap.** Aborting the browser request does not cancel the server-side call;
the request completes and its result is discarded. Correct behaviour for a
clinician standing in a room, wasteful under load. A real fix needs the scribe to
be a job that can be cancelled, which is the same work as making it retryable
after a crash (D-032).

### D-078 · Regeneration reuses the entry, and refuses to overwrite a person

Capability: *"AI regeneration that preserves human-confirmed and completed
state."* Before this it was undefined behaviour, found by probing rather than
reading: a fresh session id produced a **duplicate** summary entry for one
consult, and passing the same session id **crashed** on the
`transcript_segments (session_id, sequence)` unique constraint. Neither is an
answer to "the model produced a poor summary, run it again."

**Regeneration reuses the entry and appends a version.** The entry id is what
every accepted highlight, comment, task and provenance pointer anchors to, so
keeping it is what makes the capability true rather than aspirational. The
previous summary survives as a version, so it stays revertible. Transcript
segments are not re-recorded: the transcript is the source of truth and
regeneration re-reads it.

A useful consequence falls out of D-076 — highlights anchored to the old version
go stale and render side by side, so a clinician sees what they confirmed next to
what the model now says and decides. That is better than either silently
re-anchoring or dropping them.

**The expensive half.** Keeping accepted highlights is cheap and mostly fell out
of D-059. The requirement that matters is that **a clinician's own words are
never replaced by a model's second attempt.** So regeneration refuses outright
when any version of the entry was written by a non-system role. Merging would
mean deciding which of a clinician's sentences to keep, and that is a clinical
judgement this system has no standing to make — the same rule that stops the
contradiction detector picking a winner between two humans (D-068).

Refusing is only acceptable because it is recoverable, and the message says how:
revert to the machine version and regenerate, or copy the edit out first.
Overwriting would not be recoverable. `409`, not `400` — nothing about the
request is malformed; the record is in a state where honouring it would destroy
something.

**Known gaps.** Regeneration is all-or-nothing: there is no way to regenerate one
section of a summary while keeping another. Completed tasks survive because they
hang off the entry id, but nothing re-links a task to a section that no longer
exists in the new text.

### D-079 · Dosages are checked against a reference, and patient-facing ones need a human

Capability: *"Medical terminology and dosage confirmation — should what was
captured be confirmed through medical references and human confirmation?"* And
the 48-hour hint: *"Patient facing generation is a higher severity class… what's
sent to the patient needs more visible human approvals and/or rules."*

The build extracted doses and compared them **against each other** (D-068), so it
could tell that two entries disagreed about a metformin dose and could not tell
that one of them said 5000mg. The realistic failure is not a wild hallucination —
it is a decimal point.

**A reference table, not a formulary.** Adult single-dose ranges for the
medications already on the watchlist. It cannot say a dose is *correct*, only
that it is outside a range where almost nothing legitimate lives. Paediatric,
oncology and specialist regimens are out of scope by design, which is exactly why
an out-of-range figure produces a question for a human rather than a block.

**Three bands, and only one of them gates.** `plausible` · `unusual` ·
`implausible`. Gating on `unusual` would put a confirmation dialog on ordinary
prescribing, which is how a safety prompt becomes a reflex click — the same
alert-fatigue argument as the learning floors (D-041).

**The threshold was wrong on the first attempt and the fix is principled rather
than tuned.** An order of magnitude let metformin 5000mg through as merely
`unusual`, which is the precise case the hint describes. The ranges are
*single-dose*, and a legitimate daily total reaches roughly 3× a single dose
(TDS), so beyond 3× the upper bound exceeds any plausible daily total let alone
one administration. Metformin 1500mg BD stays `unusual`; 5000mg becomes
`implausible`.

**Acknowledgement, not refusal.** A hard block would be wrong — a clinician who
knows what they are doing must not be prevented from recording it, and refusal
teaches people to route around the check. The gate makes them say explicitly, in
the audit trail, that they meant it. What was overridden is recorded, not merely
that something was: a gate nobody can see the far side of is not a gate.

**Only patient-facing types are gated.** Internal notes get audited by people who
can read a BNF. This is for what leaves the building. It pairs with D-067 — no
machine-written text can be patient-facing at all, so the gate is catching a
human's typo, which is the residual risk that structural control deliberately
leaves in place.

**Known gaps.** Seventeen medications, adult ranges only, mg-mass only — insulin
is dosed in units and is excluded rather than guessed at. Frequency is not parsed,
so "500mg six times daily" reads as plausible. There is no interaction checking,
no renal or weight adjustment, and no real drug database behind it.

### D-081 · One clinical disagreement is one card, however many entries evidence it

Scenarios 13 and 15. `contradictions.detect` works pairwise. That is the right
primitive — it is what makes every finding individually checkable and
individually citable — and it is the wrong unit to put in front of a clinician.

A penicillin allergy re-recorded at four routine visits, against two entries
recording no known allergies, produces eight pairs. All eight say the same
clinical thing: *this chart disagrees with itself about penicillin.*

Duplication was the smaller problem. The Glance View caps the contradiction list
at `MAX_CONTRADICTIONS = 5`, so the copies filled the cap and an unrelated
metformin dose disagreement was evicted from the card entirely. A real,
unresolved, clinically dangerous conflict was made invisible by a *different*
conflict being mentioned often — and it got monotonically worse the longer the
record grew, which is the one failure mode a longitudinal product cannot afford.

`group()` collapses the display unit to `(kind, subject)` and carries every
supporting entry with its own pointer in `also_left` / `also_right`. Nothing is
discarded: a card that said "and 7 others" without pointers would trade an
alert-fatigue problem for a provenance one, and scenario 16 requires findings
stay addressable. `human_human` is the pessimistic reading — true if *any* pair
in the group is human-human, because if even one pair pits two people against
each other then no precedence rule settles it.

**How this was missed.** Every existing test used exactly one assertion and one
denial — the single shape where pairwise and grouped output are identical. It
was found by probing the running app with a realistic chart, not by the suite.

**Known gap.** Nothing monitors the dismissal rate on contradictions, so alert
fatigue is still measured by nobody.

### D-082 · Degraded is a first-class field, and a badge, not a model string

Scenario 9. The backend answer was already right: on a provider outage the
scribe degrades to the deterministic extractive summariser and records
`model_used = "offline-extractive-v1:provider-unavailable"`. D-070 called that
"legible as degraded."

It was legible to an auditor reading the database. In the interface the only
trace was `ai_model_used`, rendered as a 10px grey monospace string in the
provenance footer beside the pointer — a machine-facing identifier in the place
on the card a clinician looks least. During an hour-long outage a clinician
would read what appeared to be an ordinary AI summary.

Three changes. The magic string became `DEGRADED_MODEL_LABEL` with an
`is_degraded()` helper, because two surfaces answering "was this degraded?" by
substring-matching the same literal is how they drift apart. `ai_degraded`
became a wire field, so no client has to parse an identifier to render a safety
signal. And the card gained a chip in the existing vocabulary.

**Kept distinct from confidence, deliberately.** Low confidence means the model
was unsure. Degraded means no model read this consult at all. They call for
different things from a clinician — the first invites scepticism about a
judgement, the second says no judgement was made — so collapsing them into one
"trust this less" badge would lose the more actionable of the two.

**Known gap.** Nothing tells the clinician the outage is ongoing rather than
historic; the badge is per-entry, and there is no service-health surface.

### D-083 · No patient data travels in a URL, and a test enforces it

Scenario 3, and the door the earlier answer missed. `log_event` (D-014) governs
what we log on purpose. The sanitised error middleware (D-071) governs what
crashes log on our behalf. Neither touches the ASGI access log, which records
the full request line — method, path *and query string* — before the
application sees the request, and identically whether it succeeded, 404'd, or
was refused by RBAC.

That makes a URL a logging sink. And the convention that URLs carry only opaque
ids had already failed: `POST /patients/{patient_id}/login` took the patient's
phone number as a query parameter. The feature written for scenario 1 — *she
exists for the clinic as a phone number* — wrote that number into an unrotated,
unscrubbed log on every use. Scenario 1's answer leaked through scenario 3's
door.

The identifier moved into the request body. More usefully, `tests/test_url_surface.py`
turns the convention into an invariant: every path and query parameter on every
route must appear in an explicit allowlist of opaque ids, enums, integers and
structural pointers, and a second test asserts the allowlist itself contains
nothing PHI-shaped, so the invariant cannot be satisfied by quietly widening it.
The check is structural rather than behavioural because the failure mode is a
plausible-looking route added months from now, not existing code misbehaving.

**Known gap.** Patient and entry UUIDs are still in the access log. They are
pseudonymous and, unlike a phone number, meaningless outside this database — a
real deployment should still rotate and scrub, and there is no retention policy.

### D-084 · A protected clinical class bypasses ranking, and survives dismissal

Scenario 15. The feedback asks what stops the ranking learning to bury an
allergy because a tired clinician swiped one away on a Tuesday. Our answer was
`NEVER_DAMPENED` (D-041), and re-reading it against the question showed it
**floors the wrong quantity**.

`NEVER_DAMPENED` stops learning driving a protected tag's own weight below zero.
Surfacing is a top-`MAX_HIGHLIGHTS` cut over score. So two routes to
invisibility were still open, and neither involved dampening anything:

* **Relative displacement.** Other tags rising is sufficient. A clinic that
  interacts heavily with medication changes lifts those scores until an allergy
  falls off the bottom of a six-slot card, its own weight untouched at zero the
  whole time. A floor on a tag cannot reach a decision made by comparison.
* **A single dismissal.** `_top_highlights` filtered `status != REJECTED` in the
  query, so one rejection removed a highlight from the Glance View permanently.
  For a suggestion about a follow-up call that is correct. For an allergy it is
  the exact failure the scenario describes, reachable in one tap.

**The fix is structural, not another weight.** Highlights whose feature tags
intersect `learning.NEVER_DAMPENED` are surfaced regardless of rank, and a
dismissed one is demoted to the end of the card rather than deleted from it.
Ranking still orders the protected set; it no longer decides membership of it.

**One list, not two.** The protected set *is* `NEVER_DAMPENED`, imported rather
than restated. A second hand-maintained list of "things that matter" would drift
from it within a phase and the drift would be silent. The two mechanisms are
complements: one floors a learned weight, the other decides visibility, and
neither can substitute for the other.

**The exemption is visible.** Protected highlights carry `protected` and
`protected_reason` on the wire and render an "Always shown" chip. An unranked
item appearing with no stated reason is its own trust problem — a clinician
cannot otherwise distinguish a safety exemption from a ranking bug.

**Trade-off, stated.** On a chart with many protected findings the card grows
past `MAX_HIGHLIGHTS`. We took that over the alternative, because a card that
stays exactly six items long by dropping an allergy is the wrong kind of tidy.

**Known gaps.** Nothing measures how often the protected set is large enough to
be its own fatigue source — the failure mode we may have moved rather than
removed. Dismissed protected items accumulate on the card with no ageing-off
rule. And the protected set is a six-tag vocabulary, so a critical class the
tagger does not know is not protected: the failure mode there is silence, as
everywhere else recall is bounded by `features.py`.

### D-085 · Clinic isolation is strong, and singular — verdict downgraded to PARTIAL

Scenario 2 asks two questions. We had answered the first well and the second not
at all.

*Where is it enforced?* `AccessScope.query` (`security/rbac.py`). One method,
fused into the type routes receive, with no unscoped path available to reach for.
D-003 argued that making enforcement impossible to forget beats making it
redundant, and that argument still holds.

*Assume that line has a bug.* We had never measured it. `test_survival_scenarios.py`
now does, by dropping the clinic predicate and issuing real requests:

    GET /patients/patient-b1  -> 200  (leaked)
    GET /patients             -> every patient in both clinics

**Nothing else catches it.** No row-level security — SQLite has none and the
build never grew a Postgres path. No per-tenant connection or schema. No
assertion at the serialisation boundary. Every route reaches data through that
one method, so the blast radius of one wrong line is *every patient in every
clinic*, and the only thing standing between that bug and production is the test
suite.

**So scenario 2 moves SURVIVES → PARTIAL.** Not because the control is weak — it
is the strongest single control in the build — but because "enforced in exactly
one place" and "defended in depth" are different properties, and the scenario
asks about the second. Claiming SURVIVES answered the half we had built.

**What we would add first**, in order of value per hour:

1. A `before_execute` hook in `core/db.py` that refuses a SELECT against a
   clinic-scoped table with no `clinic_id` predicate, with an explicit opt-out
   context manager for the seed, decay and learning-rebuild jobs that legitimately
   run cross-clinic. Independent module, independent mechanism, catches the
   failure even when `rbac.py` is wrong.
2. Postgres with RLS, moving the predicate below the application entirely.
3. A response-boundary assertion that every serialised object carries the
   caller's `clinic_id`.

(1) was scoped and **deliberately not attempted** at this point in the build: it
touches every query path in the system, and the failure mode of getting it
subtly wrong at 11pm before a deadline is a false sense of a second layer, which
is worse than a documented single one. The test that measures the blast radius
was the honest thing to ship instead, and it will fail the day a real second
layer lands — which is the right moment to revisit the verdict.
