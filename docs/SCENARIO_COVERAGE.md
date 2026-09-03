# Care Note — Clinic Reality Assessment

Response to the sixteen scenarios and the twelve-capability list, assessed
against the build as it stands rather than as intended. Where a claim was
checked by running code rather than by reading it, the entry says so.

**Scenarios: 9 SURVIVES · 6 PARTIAL · 1 DOES NOT.**
**Capabilities: 4 SURVIVES · 7 PARTIAL · 1 DOES NOT.**

The shape of that result is worth naming before the detail. What survives is
what we made structural — access control fused into a single object, redaction
fused into the only code allowed to reach a network, highlights anchored to a
version number, safety classes routed around the ranking rather than protected
inside it. What is still partial is mostly operational: there is no way to send
a patient anything, nothing runs during a consult rather than after it, and a
clinic cannot change its own clinical vocabulary without a deploy. A page of
green ticks would be less use to a reviewer than an accurate one.

```bash
pytest tests/ -q          # 548 test functions, 872 cases
cd frontend && npm test   # 85 component tests
```

---

## Summary

| # | Scenario | Verdict | Tests |
|---|---|---|---|
| 1 | Patient with no email | **SURVIVES** | `test_enrolment.py`, `PatientAdmin.test.jsx` |
| 2 | Clinic isolation, one line | **PARTIAL** | `test_rbac_scope.py`, `test_phase1_cross_clinic.py`, `test_survival_scenarios.py` |
| 3 | Read your logs | **PARTIAL** | `test_failure_modes.py`, `test_url_surface.py`, `test_log_hygiene_properties.py` |
| 4 | Prove the ordering | **SURVIVES** | `test_llm_chokepoint.py`, `test_redaction.py` |
| 5 | Clinic B on Monday | **PARTIAL** | `test_clinic_config.py`, `test_phase1_cross_clinic.py` |
| 6 | Trilingual consult | **PARTIAL** | `test_language_risk_floor.py`, `test_multilingual_features.py` |
| 7 | Allergy at minute two | **DOES NOT** | `test_capture_timing.py` |
| 8 | Model hangs 45s | **PARTIAL** | `test_failure_modes.py`, `Resilience.test.jsx` |
| 9 | Provider 503 for an hour | **SURVIVES** | `test_failure_modes.py`, `EntryCard.degraded.test.jsx` |
| 10 | Two people, same note | **SURVIVES** | `test_concurrent_edits.py` |
| 11 | Link never received | **PARTIAL** | `test_delivery_state.py` |
| 12 | Patient summary wrong by one dosage | **SURVIVES** | `test_regeneration_and_dosage.py`, `test_delivery_state.py`, `DosageConfirm.test.jsx` |
| 13 | Allergy asserted vs denied | **SURVIVES** | `test_contradiction_denial.py`, `test_contradiction_grouping.py` |
| 14 | A number that means nothing | **SURVIVES** | `test_evaluation_and_abstention.py`, `test_language_risk_floor.py` |
| 15 | Ranking learns from what it showed | **SURVIVES** | `test_self_learning_importance.py`, `test_protected_surface.py` |
| 16 | Highlight cites edited source | **SURVIVES** | `test_highlight_provenance.py`, `test_review_defects.py`, `Phase9Surfaces.test.jsx` |

---

## The sixteen, in detail

### 1. The patient with no email — SURVIVES

**Where.** There is no email field anywhere in the schema. Login is a username
and a password, and a phone number is a first-class identifier type rather than
a workaround — `POST /patients` takes an `identifier_type` of `phone`, `nric`,
`mrn` or `internal`, and uses the identifier as the username. `dob` and `mrn` are
nullable, because those columns were a second and quieter way for the schema to
decide she is not a patient.

**What a nurse actually does.** The **Front desk** panel in the clinical view
takes a name and a phone number and returns a username and a one-time passcode.
The passcode is shown once and stored only as a hash, and the panel says so next
to it — a staff member who assumes it can be retrieved later will not write it
down, and the patient is locked out. Registering the same number twice is
refused rather than creating a second account on one phone.

**What could later break it.** There is no channel to send the passcode through,
so it has to be read out or written down in the room. That is scenario 11's gap
reaching into this one. Usernames are also globally unique rather than unique per
clinic, which is right for a shared login namespace and wrong for a person who is
a patient at two clinics — she cannot be registered at the second.

### 2. One line changes in a route handler — PARTIAL

**Where.** `backend/app/security/rbac.py`, in `AccessScope.query()`. Every
database read goes through an object that has already narrowed itself to the
caller's clinic, and the clinic comes only from the signed login token, so a
client cannot supply or widen it. Route handlers never receive a plain `User` or
a bare database session, so there is no unfiltered path to reach for. Forgetting
the clinic filter is not a mistake the code lets you make.

**If that line had a bug.** Every patient in every clinic becomes visible to
every logged-in user. There is no second filter behind it. That is the deliberate
trade: one line to audit, one line to get wrong. Against the exact mutation the
question describes, hand-written access-control tests raise 15 failures and a
matrix enumerated from the live OpenAPI schema raises 48 — both catch it, and
only one tells you what it exposed.

**Why this is partial rather than survives.** The control is strong and
singular, and those are different properties. It is impossible to forget and it
is not defended in depth. We had been treating the first as though it implied the
second; the question asks about the second.

**What could later break it.** Service functions take a raw database handle and
re-derive the clinic from an object the route already fetched. That is safe
today. A future service written to take a `patient_id` instead of a `Patient`
would look identical at the call site and carry no clinic check at all.

### 3. Now read your logs — PARTIAL

**Where.** Application logging goes through one `log_event` function that records
an actor id, an action, a target type and id, a clinic id and a timestamp. Note
bodies, comment bodies and transcript text are never arguments to it. The error
middleware strips the database library's bound parameters, which is where a crash
would otherwise put a name and an NRIC into a log line in one go. No route
accepts patient data in a path or query parameter, and that is enforced by a test
reading every route off the live schema and checking its parameters against an
allowlist, plus a second test asserting the allowlist stays free of PHI-shaped
names so the invariant cannot be satisfied by widening it.

**Checked by running it.** With the server writing to a file, a consult
transcript containing a name, a phone number and an IC number was submitted
through the interface, then the log was grepped for each of them. Zero hits — and
zero for the clinical content as well, since logging a note body is the same leak
wearing a different hat. What the log carries for that request is an actor id, an
action, a target id, a clinic and a timestamp: enough to answer who did what to
which record when, and not enough to reconstruct anything about her.

**Why this is partial.** The ASGI access log records the full request line before
the application runs, and identically for requests that 404 or are refused by
access control, so neither `log_event` nor the error middleware can reach it. The
URL allowlist is what keeps that clean, and it is a convention enforced by a test
rather than a property of the transport. Logs are also unrotated and unscrubbed:
on a hosted platform that means indefinite retention in someone else's dashboard.
React additionally re-logs caught errors to the browser console in development
builds, message included, and an error boundary cannot suppress it.

**What breaks first.** Nothing a clinician ever sees, which is what makes this
the item to watch. It stays invisible until the people who can read logs are not
the people who should be reading charts.

### 4. Prove the ordering — SURVIVES

**Where.** `backend/app/ai/llm_client.py`, in `complete()`. In order: redact the
text, re-scan the redacted result for anything that still looks like an
identifier, refuse to send and raise if anything survived, and only then call the
model. Callers cannot pass pre-redacted text and skip the step, because it runs
unconditionally on every call.

**Why there is only one such path.** A test asserts that no other module in the
codebase imports a model SDK or reaches a model host. The guarantee is not that
every developer remembers; it is that there is one exit and the build fails if a
second appears.

**The visible artefact.** After a consult is captured, the transcript panel shows
the transcript as stored — `[NAME_3]`, `[PHONE_1]`, `[ID_1]` — and the entry card
shows how many identifiers were removed. That is what the model received. The
ordering itself is a property of the call path and no screen can display a call
path, so the test is what proves no route exists around it; the redacted
transcript is what the ordering produced.

**What could later break it.** The ordering is structural. What gets removed is a
pattern-and-name-list problem, and the residual check shares its patterns with
the redactor, so it cannot catch a format that neither describes. Phone coverage
is per-locale — Singapore local, Malaysian local, international with a `+`, and
anything following a cue word such as "call" or "hp" — and a number in an
unlisted national format still travels.

### 5. Clinic B onboards Monday — PARTIAL

**Schema changes needed: none.** Every clinical table already carries a clinic
id, and learned importance is stored per clinic, so Clinic B starts from zero
rather than inheriting Clinic A's habits. The build ships with two clinics
precisely so isolation is provable.

**Config changes needed: some, and they are real config.** `ClinicConfig` holds
per-clinic volume and retention thresholds, and an absent row means defaults, so
onboarding needs no migration and no setup step. Accounts are not a developer
task either: the Front desk panel creates patients and logins, so a clinic can be
populated by the people who work there.

**What stays global, on purpose.** Redaction patterns, the protected highlight
classes, contradiction severities and the dosage reference are asserted *not* to
be configurable, under one rule: a clinic may change what it sees, never what it
is protected from. Anything a clinic could turn down until an alert stopped
firing sits on the wrong side of that line.

**What breaks first.** Clinical vocabulary is still global —
`features.MEDICATIONS`, the red-flag terms, the Malay mappings — so Clinic B sees
red flags tuned to Clinic A's population, and a paediatric or oncology clinic
cannot add its own terms without a deploy. That is the gap holding this at
partial. It needs care rather than time: additions must be additive only, because
a clinic that could remove a term could remove `entity:allergy` and reach the
safety floor sideways.

### 6. The consult is trilingual — PARTIAL, and measured

**What the transcript produces.** The text survives intact, per speaker, with
timings and a language tag. Nothing is dropped.

**What everything downstream produces.** English and Malay both produce the
correct clinical tags, and produce the *same canonical tags*, so one concept is
one learnable feature rather than two. The deterministic risk floor is
language-independent: the same symptom rates the same in either language, which
matters because a safety mechanism that is weaker in one language is not a safety
mechanism. Romanised Hokkien produces no tags, and neither does a sentence that
mixes it in.

**Abstention rather than silence.** Content the tagger cannot read is flagged as
unread and surfaced on the card, rather than producing an empty tag list that is
indistinguishable from "nothing clinical was said". Substantiveness is measured
in characters rather than whitespace tokens, so unspaced scripts — Chinese,
Japanese — reach the flag instead of falling under a word-count bar.

**What breaks first.** A patient describes chest pain in Hokkien. The words are
in the record and readable by anyone who opens the timeline. They produce no
tags, no risk level and no highlight, and the Top Card carries a flag saying the
system could not read part of this consult. That is better than confident silence
and it is not understanding. More vocabulary is an arms race we lose; the flag is
the part that generalises.

### 7. A drug allergy at minute two — DOES NOT

**Where.** The recorder chunks while running, but it assembles and uploads the
whole file when recording stops. Everything after that — transcription,
redaction, summary, extraction — runs once, on the complete transcript.

**What breaks first.** The consult ends, the patient leaves, the summary appears,
and the allergy is on the card. It is accurate and correctly linked to its
source. It is eighteen minutes late. If a prescribing decision was made at minute
fifteen, the system held the contradicting fact for thirteen minutes and said
nothing.

**This is a product decision, and it is labelled as one.**
`tests/test_capture_timing.py` asserts the batch boundary rather than papering
over it, so the limitation is checkable rather than described. Post-hoc capture
and live capture are two products with the same feature name, and we built the
first.

**The smallest honest change.** The deterministic extractors —
`contradictions.detect` and `features.tag_span` — are pure functions over text
and need no model, so running them on partial transcript as it arrives would put
allergy detection roughly ten seconds behind the utterance. What that does not
solve is when it is acceptable to interrupt a doctor mid-sentence, which is the
genuinely hard part and wants a clinician rather than an engineer.

### 8. The model hangs for 45 seconds — PARTIAL

**Where.** There is an explicit timeout in the model client, set to 8 seconds. A
consult summary that takes longer has already missed its consult. The hang in the
question therefore never reaches 45 seconds: at 8 the call is abandoned and
routed to the degraded path in scenario 9, so the clinician gets a usable card
rather than an error. The interface also passes an `AbortController`, so the
clinician can abandon the call before the timeout rather than watching a spinner
with a patient in the room.

**What the clinician sees.** Up to eight seconds of a labelled processing state
on the entry, then a card marked as written without the model. The transcript is
stored before the model is called, so the clinical content survives regardless of
what the summary does.

**Why this is partial.** The call is synchronous and holds a worker for its
duration. There is no queue, no background retry and no way to ask for the
summary again later without re-running the capture. Under concurrent load the
timeout bounds the damage rather than preventing it.

### 9. The provider returns 503 for an hour — SURVIVES

**Where.** Connection errors, timeouts and non-200 responses all route to a
rule-based extractive summariser rather than raising. The entry is stored with
`ai_degraded` set and a model string naming the reason
(`offline-extractive-v1:provider-unavailable`), so the record itself says how it
was produced.

**Visible to a clinician, not only to an auditor.** The card carries a chip
saying the note was written without the model. That is kept separate from the
confidence indicator on purpose: "the model was unsure" and "no model read this
consult" call for different responses from the person reading, and collapsing
them into one number loses the distinction.

**What happens if the provider stays down.** Nothing stops. The existing record
loads normally, because the Glance View reads stored rows and does no model work.
New consults produce extractive summaries, and every sentence shown was said by
someone in the room, because the fallback selects rather than writes.

**What could later break it.** Extraction is not summarisation. A long consult
produces a longer card, not a better one, and the fallback cannot tell a
clinician what a consult was *about*.

### 10. Two people open the same note at 09:14 — SURVIVES

**What is in the database at 09:15.** Both edits are accounted for and neither is
lost. The first save wins; the second is refused.

**Where.** Two layers. The client sends the version it read and a mismatch is
rejected with a 409. That check alone is not a lock, so behind it a
`uq_entry_version` database constraint physically prevents two edits claiming the
same version number — whichever commits second cannot, and that refusal is the
real guarantee.

**How either of them knows.** The one who loses gets an inline message naming the
version they were editing and the version the note is now at, with their typed
draft preserved so they can reconcile rather than retype. Both edits appear in
the version history with author and timestamp.

**One collision the system cannot have.** Access control already partitions who
writes what, so a clinician and a nurse cannot collide on the same section — they
cannot both reach it. The conflict that survives is two people in the same role,
and that is the one this handles.

**What could later break it.** Neither of them knows at 09:14 that the other is
in the note. There is no presence indicator, so the conflict is avoidable and the
product does not try to avoid it. And the resolution is "here is the current
text, reconcile it yourself" — fine for a two-line plan, not fine for a long note
under time pressure.

### 11. The appointment link is never received — PARTIAL

**There is no delivery path to trace, and that is the honest answer.** No email,
SMS, WhatsApp, push or webhook. No sender, no queue, no template, no provider
credential and no configuration key for one.

**What is modelled instead.** Reach is tracked rather than assumed: every piece
of content written *for* the patient carries a state of `unread`, `read` or
`corrected`, derived from data already being captured. Unread instructions are
surfaced to the clinician, and a correction the patient has not yet seen is
surfaced to both sides. The scope is deliberately content the clinic wrote for
her — her own notes are not something that can fail to reach her.

**What breaks first.** A clinician writes "come back in two weeks for a BP
check". The instruction is correct, versioned and traceable, and the clinician
can see she has not opened it — and cannot send it to her. The system reports the
gap accurately and cannot close it.

**The same gap applies internally.** Mentioning a colleague stores the mention
correctly and notifies nobody. A nurse tagged at 09:00 finds out when she next
opens that patient.

**Why partial rather than does-not.** A build that cannot send is a limitation. A
build that cannot tell you it did not send is a false assurance. This one tells
you.

### 12. The patient-facing summary is wrong in one dosage — SURVIVES

**The generation side is the strongest control in the build.** Patient-facing
entry types can only be written by a clinician. The system role cannot write any
content at all, and the AI pipeline raises rather than warns if a future change
ever points it at a patient-facing type, so even a mislabelled AI note is
unreadable by a patient. We chose this over generate-then-approve deliberately:
an approval step under time pressure is a button people click. Here the clinician
types the words, so the wrong dose in this scenario was typed by a human, which
is the correct place for that risk to sit.

**Before it saves.** Dosages in patient-facing text are checked against a 17-drug
adult single-dose reference. An implausible figure gates the write with a dialog
naming the drug, the stated dose and the expected range. The clinician can
override and the override is recorded, because a hard block on a real clinical
range teaches people to route around the check. The same sentence in a clinician
section saves without a prompt — the two are different severity classes, and the
gate is on the door to the patient rather than on the record generally.

**After it saves.** The clinician edits, a new version is recorded and the
original is preserved. The patient's view then leads with a banner naming what
changed, above everything else on the page, because if she was following the
earlier dose that is the only thing on the screen that matters.

**What could later break it.** The reference is 17 drugs with no formulary, no
interactions, no renal or weight adjustment and no frequency parsing. A wrong
dose for a drug outside that list passes silently. Nothing is recalled either,
because nothing was ever sent — see scenario 11.

### 13. Penicillin allergy versus "no known allergies" — SURVIVES

**Where.** An `assertion_vs_denial` contradiction class. The negation guard is
kept, so a denial on its own never becomes a critical alert — but denials are
recorded rather than discarded, and compared against positive claims elsewhere in
the chart.

**What the clinician sees.** One card saying these two records disagree, with
both entries cited and the disagreement deliberately unresolved. There is no
precedence rule between two humans, and inventing one would be a clinical
decision this system has no standing to make. The allergy separately carries a
critical risk level and sits on the protected list, so no amount of dismissal
suppresses it.

**Why the disagreement is the point.** "Allergy recorded, patient denies it"
means the patient forgot, or was never told, or it was recorded against the wrong
chart, or it was an intolerance rather than an allergy — and a clinician needs to
know which. Showing only the allergy is safe. Showing only the denial would be
lethal. Showing neither as a conflict wastes the one thing a longitudinal record
was supposed to produce.

**One disagreement is one card.** A real chart re-records an allergy at every
visit, and pairwise detection over four assertions and two denials would produce
eight findings that all say one thing. The display unit is the disagreement
rather than the pair, with every supporting entry keeping its own pointer, so a
frequently-repeated conflict cannot fill the card and evict an unrelated one.

**What could later break it.** Recall is bounded by a 17-drug watchlist rather
than a formulary, and drug-class reasoning covers the classes we listed. A
conflict about a drug nobody typed into that list produces silence.

### 14. Pick one of the three numbers — SURVIVES

**The risk badge.** Deterministic rules set a floor the model cannot go below.
The model may raise a risk level; it can never lower one. That asymmetry is
deliberate: keyword lists miss things, so a model noticing something they missed
must not be suppressed, while a model calling a red flag routine must not be
obeyed.

**How you would know if it were wrong.** Three things are stored per note: what
the model asked for, whether a rule overrode it, and the result. "Why does this
say high?" is answerable from the record — a rule said so, or a model did, and
you can see which. The floor is deterministic across runs and independent of the
language the note was written in.

**What the system does when it is wrong.** Too high: a clinician rejects it and
the ranking stops suggesting that kind of content, unless it is a protected
class, in which case it keeps appearing. Too low: a clinician highlights the span
by hand, which outranks anything the machine proposed and promotes similar
content in future.

**The confidence label, since it is the weaker of the three.** It is computed
from hedging in the source transcript rather than reported by the model, so it is
deterministic, recomputable, and derived from text a reviewer can go and read.
Measured honestly, it tracks how certain the speakers sounded and not whether the
summary is correct: a plainly-stated wrong dosage scores high, and correctly
hedged accurate prose scores low. It is scaffolding rather than a validated
metric, and it is labelled as a prompt to verify rather than a correctness score.
The stronger control alongside it is abstention — a summary line that cannot be
traced back to the transcript is shown with no citation at all, rather than one
that looks checkable and is not.

**The blind spot.** The risk floor is negation-aware; the tag extractor is not,
so "no anaphylaxis" still emits an anaphylaxis tag. That tag is on the protected
list, which means a clinician dismissing it cannot teach the system to stop — the
mechanism that prevents alert fatigue manufactures one false-positive class of
its own. It fails loud rather than silent, which is the right direction, and
alert fatigue is how loud failures become silent ones.

### 15. The ranking only learns from what it already showed — SURVIVES

**The tired-Tuesday problem.** Six safety tags — allergy, critical risk,
anaphylaxis, sepsis, suicidal, self-harm — bypass ranking entirely. Learning
orders the protected set; it does not decide membership. Their learned weight is
also floored at zero, so behaviour can promote them and can never suppress them.
A clinician dismissing three warfarin suggestions should teach the system to stop
nagging about warfarin. A clinician dismissing three anaphylaxis suggestions must
never teach it to stop mentioning anaphylaxis. The cost of a missed allergy is
not symmetric with the cost of one extra line on a card, so the rule is not
symmetric either.

**The exposure-bias problem.** One slot per note is reserved for a candidate
carrying a tag the clinic has never given feedback on. It is deliberately not
random — a coin flip would make the card change between page loads, which for a
clinical surface is worse than the bias it fixes. It is bounded by the same
minimum score as everything else, so it can promote something under-explored and
can never surface something the rules found meaningless.

**Measured rather than argued.** `scripts/eval_learning.py` reports, for the
seeded clinic: displacement 0.15, exposure concentration **0.77**, blind-tag rate
0.16, and zero protected classes displaced. The 0.77 is the bias as a number —
ten of thirteen visible slots go to tags this clinic has already given feedback
on. We are not claiming that is a good number, because there is nothing to
compare it against. We are claiming it moves when the system changes, which is
what an argument cannot do.

**Three further bounds.** Learning can reorder highlights the rules already
found; it can never invent one, and that check runs before scoring. Weights
saturate and evidence half-lives at ninety days, so no tag can dominate and a
clinic is allowed to stop caring about something. And the interaction log is the
source of truth — weights rebuild from scratch to identical values, so there is
no drifting shortcut formula.

**How it affects trust.** A ranking that adapts and will not tell you how is
exactly the opaque machine judgement this product exists to replace. So a clinic
can read every learned tag, its weight, its positive and negative signal counts,
and which tags are protected from suppression. The safety floors are visible as
data rather than as a claim in a document.

**What could later break it.** The exploration slot is per-note and only fires
when there are more candidates than slots, so a sparse chart never explores. The
protected list is six hand-written strings and protects exactly what we thought
to type. One enthusiastic clinician counts the same as practice consensus. And
nothing monitors the loop: nothing alerts if dismissal rates spike, which is
precisely the signature of the scenario in the question, and the data to detect
it is being recorded and read by nothing.

### 16. A highlight cites a source that has since been edited — SURVIVES

**Where.** Every highlight stores the version number it was made against, not
just a position in the text. If the note has since changed, the highlight is
marked stale and its text resolves against the snapshot it was anchored to, so it
shows the words as they read when a clinician confirmed them rather than whatever
now sits at those coordinates. Position lookups validate their bounds and refuse
rather than returning a wrong slice.

**Content changes two ways, and both are covered.** An edit moves the version
number. Archival does not: the retention policy compresses an old note to an
extractive summary and deliberately creates no version, because archival is not
an authorship event and recording it as one would make the audit trail claim a
person edited a note nobody touched. A cold entry's content therefore matches no
version snapshot by construction, and every highlight on it is stale whatever its
version number says. The card distinguishes the two cases: an edited source
quotes the version change, an archived one says the note has been shortened,
because "v1 → v1" would read as a bug and explain nothing.

**Against the three options in the question.** It still resolves, it does not
silently point at different text, and it does not vanish. A stale highlight also
does not paint a box at coordinates that stopped describing the text — the
timeline scrolls to the note and marks nothing, because pointing confidently at
the wrong words is worse than pointing at the note.

**Stale is not lost.** The highlighted words resolve against the version snapshot
that compression never touches, the full original is archived byte for byte, and
restoring the note makes its highlights current again.

**What could later break it.** Version snapshots are uncompressed, and if a
future retention policy ever pruned them the anchoring would break even though
the highlighted words survive. Any such policy would have to treat versions
referenced by a highlight as protected, and nothing enforces that today.
Separately, staleness is binary: it says "this changed", not "the change touched
this sentence", which is the question a clinician actually has.

---

## The twelve capabilities

| Capability | Verdict | Where, and what is missing |
|---|---|---|
| Streaming real-consult audio, noisy ASR | **DOES NOT** | `ai/asr_client.py` is a fixture-backed stub and upload is whole-file. No noise handling, no live stream. The pipeline consumes turn-structured input, so the recogniser is swappable — but nothing streams |
| Speaker attribution and diarization | **PARTIAL** | Speaker labels and per-segment confidence are carried end to end and drive provenance. Overlap is detected by timing arithmetic rather than acoustics. No acoustic diarization |
| Within-statement code-switching | **PARTIAL** | English and Malay are handled and tagged, including mid-sentence. Romanised Hokkien produces no tags and is flagged as unread rather than silently ignored. Substantiveness is measured in characters, so unspaced scripts reach the flag |
| Multilingual downstream processing | **PARTIAL** | English and Malay produce the same canonical tags, so one concept is one learnable feature, and the risk floor is language-independent. Two languages only |
| Medication and dosage confirmed against references and a human | **PARTIAL** | `services/dosage.py` holds a 17-drug adult single-dose reference; implausible doses gate patient-facing writes with a recorded human override. No formulary, no interactions, no renal or weight adjustment, no frequency parsing |
| Immutable, version-bound provenance | **SURVIVES** | Highlights anchor to `source_version_number`. Stale ones resolve against the old snapshot and render beside the current text; compression marks them stale rather than silently moving them |
| Extraction under negation, correction and conflicting sources | **PARTIAL** | A negation-aware risk floor and an `assertion_vs_denial` class. Spoken self-correction inside one transcript is detected behind an explicit cue; a correction phrased without one is not caught, and "no wait" is a pinned known miss |
| Real-time collaborative editing without lost updates | **PARTIAL** | No lost updates — an optimistic version check plus a `uq_entry_version` constraint that is the real serialisation point. Not real-time: no presence, no live cursors, no CRDT. Two people find out at save, not at 09:14 |
| AI regeneration preserving human-confirmed state | **SURVIVES** | Reuses the entry so accepted highlights, comments and tasks survive, and refuses outright when a human has edited |
| Contradictory human, patient and AI assertions | **PARTIAL** | Five classes, both sides cited, `human_human` and `same_entry` marked, deliberately never resolved. Recall is bounded by a 17-drug watchlist rather than a formulary — the failure mode is silence |
| Audience-appropriate outputs | **PARTIAL — by refusal** | A separate patient view in plain language with a distinct visual register. The system does not generate per audience: no machine-written text can become patient-facing at all, so a clinician writes it. A deliberate non-implementation rather than a gap |
| Self-learning: clinic-scoped, bounded, auditable, fatigue-resistant, exposure-bias evaluated | **PARTIAL** | Clinic-scoped, bounded and auditable all hold: per-clinic weights, saturation, a 90-day half-life, protected-class floors, and a `/clinic/learning` view. Exposure bias is measured — concentration 0.77 on the seeded clinic — but the exploration slot is per-entry novelty rather than clinic feedback history, so the measurement names the bias without correcting it |

---

## What exercising the running system added

Most of the assessment above was written by reading code. We then went back and
drove the running product by hand: a realistic multi-drug chart, a patient
editing her own note, a consult transcript with a name and a phone number in it,
a 400-day-old entry going through the retention policy. Three things came out of
that, and each says something about where the remaining risk sits rather than
only about a defect that is now closed.

**Tests written from inside an assumption cannot escape it.** Every contradiction
test used one assertion and one denial, which is the single shape where pairwise
and grouped output are identical, so the fan-out on a real chart was invisible to
all of them. Abstention tests used romanised Latin script, so a word-count
threshold that fails on Chinese went unnoticed. The response was to change the
shape of the tests rather than their number — property generation over the full
unicode range, and an access-control matrix enumerated from the live schema
rather than hand-written — and that is what now covers the class rather than the
instance.

**The seam between two correct modules is where the defects are.** Two constants
with the same name in different modules holding different sets. Two functions
sharing a regular expression but disagreeing about what to do with the match. A
retention path that rewrites content while a staleness check watches a version
number. Every module in this build carries a long comment defending its own
behaviour, and none of them describes what it assumes about the module next to
it. On present evidence that is where the next one will be.

**Nothing tests that a route is reachable by a person.** Several capabilities the
API supported had no screen: registering a patient, writing an instruction *for*
the patient rather than about them, and a patient adding a note to her own
record. The backend was correct in each case and the suite was green, because the
suite drives the API. The gap between "the route works" and "a user can get to
the route" is covered by neither the backend tests nor the component tests.
Closing those three also brought the dosage gate into reach, since it fires only
on patient-facing writes and nothing in the interface could previously produce
one.

## The four that would move next

1. **Streaming capture (7, and the first capability).** The deterministic
   extractors are pure functions over text and need no model, so running them on
   partial transcript would put allergy detection roughly ten seconds behind the
   utterance. `test_capture_timing.py` asserts exactly that boundary, so the
   claim is checkable rather than a promise. What it does not solve is the
   interruption policy, which wants a clinician.
2. **A sender (11).** The delivery state machine is built and one state,
   `dispatched`, is deliberately absent because nothing dispatches. Adding a
   channel slots in without disturbing `unread` / `read` / `corrected`.
3. **Per-clinic vocabulary (5).** Volume and retention are already per-clinic and
   onboarding needs no migration. What is still global is the clinical
   vocabulary, so a paediatric or oncology clinic cannot add its own terms
   without a deploy. It needs care rather than time: additions must be additive
   only, because a clinic that could remove a term could remove `entity:allergy`
   and reach the safety floor sideways.
4. **Presence (10).** Correctness holds, but two people still discover a
   collision at save rather than avoiding it at 09:14.
