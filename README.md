# Care Note

A shared, longitudinal, role-based patient note for a clinic. It replaces
fragmented per-consult free text with one collaborative record combining
clinician notes, staff notes, patient-contributed insight, and AI-scribed
consult summaries — with a fast Glance View, full provenance, revision history,
and server-enforced RBAC.

> **Synthetic data only.** This is a prototype. It has never been connected to a
> real medical record and must not be.

**Build status: complete (Phases 0–8).** Longitudinal timeline, AI scribe
pipeline, Glance View with provenance click-through, threaded collaboration,
revision history with revert, conflict handling, contradiction detection,
adaptive importance, data decay, and ambient voice capture. See
[Current status](#current-status) for the full list of what is and is not
finished, including the parts that are stubbed.

**If you have twenty minutes**, run the Quick start below, log in as
`clinician_a`, and follow the [end-to-end walkthrough](#end-to-end-walkthrough).
That covers the Glance View, provenance click-through, revision history and the
role scoping. If you are reviewing rather than running it, the three sections
worth reading are [Where redaction happens](#where-redaction-happens),
[How RBAC is enforced](#how-rbac-is-enforced), and
[Known gaps](#known-gaps-stated-plainly).

**Deliverables:** [`docs/TECHNICAL_BRIEF.md`](docs/TECHNICAL_BRIEF.md)
(3 pages, PDF alongside it, rebuilt by `scripts/build_brief.sh`) ·
[`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) ·
[`ATTRIBUTION.txt`](ATTRIBUTION.txt) · `pytest tests/ -q` (486 tests) ·
`cd frontend && npm test` (25 component tests).

---

## Quick start

Requires Python 3.11+ and Node 18+.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example ../.env                            # optional; defaults work
python init_db.py --reset                             # create tables + seed
uvicorn app.main:app --reload                         # http://localhost:8000

# 2. Frontend (second terminal)
cd frontend
npm install
npm run dev                                           # http://localhost:5173
```

Interactive API docs: <http://localhost:8000/docs>

> **Upgrading from an earlier checkout?** Phase 4 added the
> `Entry.decay_hold_until` column, and there is no migration framework (D-001 —
> SQLite, no Alembic). `python init_db.py --reset` is required; without it
> queries against `entries` will fail on the missing column.

### Seeded logins

All accounts use the password `carenote-demo`. Two clinics exist so that
cross-clinic isolation can be *demonstrated*, not merely asserted.

| Username | Role | Clinic |
|---|---|---|
| `clinician_a` | clinician | Clinic A |
| `staff_a` | staff | Clinic A |
| `admin_a` | admin | Clinic A |
| `patient_a` | patient | Clinic A |
| `clinician_b` | clinician | Clinic B |
| `staff_b` | staff | Clinic B |
| `admin_b` | admin | Clinic B |
| `patient_b` | patient | Clinic B |

Clinic B is a full mirror of Clinic A rather than a stub, so cross-clinic
refusals can be proved in **both** directions.

### Seeing the scoping work

Sign in as each of `clinician_a`, `staff_a`, `admin_a`, `patient_a` and look at
the same patient (Amira Rahman). The timeline is different every time, and the
difference is decided server-side:

| Role | Entries visible | Not visible |
|---|---|---|
| clinician | all | — |
| admin | all | — (oversight: reads all, authors nothing) |
| staff | fewer | `clinician_section` (documented assumption D-004) |
| patient | fewest | staff notes, clinician sections, raw AI notes, all comments |

The UI is not doing this filtering. `GET /patients/patient-a1/entries` returns
different rows depending on the token, and asking for a hidden entry by id
directly still fails — which is what `scripts/phase1_smoke.py` demonstrates.

### End-to-end walkthrough

With the backend running:

```bash
python scripts/phase1_smoke.py     # 29 checks — access control, over real HTTP
python scripts/phase2_smoke.py     # 35 checks — the whole product surface
python scripts/phase4_smoke.py     # adaptive importance + data decay
python scripts/phase5_smoke.py     # 31 checks — ambient voice capture
python scripts/bench_glance.py     # Glance View latency, measured not asserted
```

`phase1_smoke.py` needs the server running on port 8000; the other four are
self-contained (they drive the app in-process and need no server).

`phase2_smoke.py` walks every Phase 2 surface in the order the demo scenarios
use them: all three scribe interaction types through redaction, the role-scoped
timeline, the Glance View, a provenance pointer resolved and then refused across
clinics, accept/reject, a manual highlight inside an AI note, a mention and a
task, an edit followed by a stale write refused with 409, a diff, a revert, and
a clinician correction that flags an AI note without deleting it. Exits non-zero
if any check fails.

---

## Running tests

Everything runs offline. There is no API key to obtain, no service to start, and
no network access needed — the LLM provider defaults to an offline stub and the
database is a local SQLite file created by the test run.

```bash
pytest tests/ -v                  # from the repository root — 509 tests
```

509 backend tests plus 44 frontend component tests, all passing, no API key or
network required. Roughly 38 seconds.

To run just the four files the brief names:

```bash
pytest tests/test_rbac_scope.py -v            # role + clinic enforcement
pytest tests/test_revision_history.py -v      # versions, revert, audit trail
pytest tests/test_highlight_provenance.py -v  # every pointer resolves
pytest tests/test_concurrent_edits.py -v      # parallel edits, deterministic conflicts
pytest tests/test_self_learning_importance.py -v   # BONUS — adaptive prioritisation
pytest tests/test_voice_capture.py -v              # BONUS — ambient consult capture
```

**Scenario coverage.** [`docs/SCENARIO_COVERAGE.md`](docs/SCENARIO_COVERAGE.md)
maps all sixteen clinic scenarios and the twelve-capability list to the tests
that cover them, each with a verdict. Current tally: **11 SURVIVES · 4 PARTIAL ·
1 DOES NOT** on the scenarios, **6 · 5 · 1** on the capabilities.

The files from the clinic-scenario review:

```bash
pytest tests/test_failure_modes.py -v         # provider outage, crash-log hygiene, timeout
pytest tests/test_language_risk_floor.py -v   # risk floor parity across languages, negation
pytest tests/test_contradiction_denial.py -v  # allergy asserted in one entry, denied in another
pytest tests/test_delivery_state.py -v        # written / read / corrected, and unreachable
pytest tests/test_enrolment.py -v             # registering a patient who has only a phone number
pytest tests/test_regeneration_and_dosage.py -v  # regeneration safety, dose reference checks
pytest tests/test_capture_timing.py -v        # when the system learns of an early allergy
```

Or by area:

```bash
pytest tests/ -v -k rbac          # the Phase 0 enforcement-pattern tests
pytest tests/ -v -k phase1        # the walking-skeleton proofs
pytest tests/ -v -k phase2        # the Phase 2 product-surface tests
pytest tests/ -v -k cross_clinic  # cross-tenancy refusals only
pytest tests/test_self_learning_importance.py tests/test_data_decay.py -v  # Phase 4 bonuses
pytest tests/test_voice_capture.py -v -k provenance   # segment-level provenance only
pytest tests/test_phase7_reported_bugs.py -v         # regressions for the Phase 7 defects
pytest tests/test_evaluation_and_abstention.py -v     # what each number means, and how we'd know it was wrong
```

`test_phase7_reported_bugs.py` is worth running on its own if you are reviewing
the fixes described in `DECISIONS.md` D-059 through D-062. Ten of its fifteen
tests fail against the Phase 6 code. That is deliberate — a regression test that
has never failed only describes what the code does today.

### Frontend component tests

```bash
cd frontend
npm install
npm test                          # vitest run — 25 tests, ~3 seconds
npm run test:watch                # while working on a component
```

jsdom, not a real browser: what these cover is offset arithmetic and conditional
rendering, neither of which needs a compositor. Two files:

- `src/components/Primitives.selection.test.jsx` — `readSelectionRange`, the
  pure DOM arithmetic behind manual highlighting. Selections are built as
  explicit `Range` objects rather than by simulating a drag, because jsdom does
  not lay text out — but a `Range` is what the browser hands the real code
  anyway. Four of these twelve fail against the Phase 6 implementation.
- `src/components/GlanceView.test.jsx` — the task controls, the accept/reject
  flow and the what's-new count. `Api` is mocked; what is under test is the
  component's contract with the client wrapper, not the wrapper's contract with
  the server, which `tests/` already covers end to end. Six of these thirteen
  fail against the Phase 6 component.

The suites are independent: `pytest` needs no npm install, `vitest` needs no
running backend.

⚠️ Do not pass `-p no:logging`. `test_llm_chokepoint.py` uses pytest's `caplog`
fixture to prove that no prompt text reaches the logs; disabling the logging
plugin errors that test rather than skipping it.

A captured run is saved at [`docs/PHASE3_TEST_EVIDENCE.md`](docs/PHASE3_TEST_EVIDENCE.md).

### Mutation checking

Access-control and history tests were **deliberately broken to confirm they can
fail**. A security test that cannot fail is worse than no test, because it gets
mistaken for coverage.

| Mutation | Tests that fail |
|---|---|
| Reverse D-004 (let staff view `clinician_sections`) | 4 in `test_rbac_scope.py` |
| Remove the clinic filter from `AccessScope.query()` | 15 in `test_rbac_scope.py`, 12 in the Phase 1 suites |
| Make revert delete later `Version` rows | 4 in `test_revision_history.py` |
| `resolve()` returns `{}` instead of raising on a dangling pointer | 1 in `test_highlight_provenance.py` |
| Highlights re-anchor on edit instead of going stale | 1 in `test_highlight_provenance.py` |
| Drop the span fragment from highlight pointers | 3 in `test_highlight_provenance.py` |
| Disable the D-037 conflict guard | 3 in `test_concurrent_edits.py` |
| Last-write-wins instead of optimistic locking | 4 in `test_concurrent_edits.py` |
| Make the learned term always return 0.0 | 4 in `test_self_learning_importance.py` |
| Remove the `NEVER_DAMPENED` safety floor | 1 in `test_self_learning_importance.py` |
| Let `CREATE` train the ranking | 1 in `test_self_learning_importance.py` |
| Drop the clinic filter from the evidence read | 1 in `test_self_learning_importance.py` |
| Remove evidence time-decay | 1 in `test_self_learning_importance.py` |
| Remove weight saturation (unbounded learning) | 1 in `test_self_learning_importance.py` |
| Resolve provenance against compressed content | 1 in `test_data_decay.py` |
| Disable the decay protection rules | 5 in `test_data_decay.py` |
| Exclude cold entries from scoring entirely | 1 in `test_data_decay.py` |
| Truncate instead of extracting when summarising | 1 in `test_data_decay.py` |

| File | Covers |
|---|---|
| `tests/test_rbac_scope.py` | **Required.** Cross-role writes, D-004 staff visibility, patient isolation, cross-clinic refusal, verbatim payload round-trip |
| `tests/test_revision_history.py` | **Required.** Version increments, revert-by-append, audit trail carries metadata only |
| `tests/test_highlight_provenance.py` | **Required.** Every pointer resolves to the exact span the card displayed |
| `tests/test_concurrent_edits.py` | **Required.** Different-section and same-section collisions, interleaved and genuinely threaded |
| `tests/test_rbac_pattern.py` | Role and clinic enforcement, server-side, via HTTP |
| `tests/test_redaction.py` | PHI detection, consistency, idempotence, and stated gaps |
| `tests/test_llm_chokepoint.py` | That the redaction boundary cannot be bypassed |
| `tests/test_phase1_cross_role.py` | Cross-role reads and writes refused server-side |
| `tests/test_phase1_cross_clinic.py` | Cross-tenancy reads and writes refused server-side |
| `tests/test_phase1_skeleton.py` | Login, entry creation, scoped views, latency floor |
| `tests/test_provenance.py` | Pointer grammar, resolution, cross-clinic refusal |
| `tests/test_sanitization.py` | Content safety; scans the frontend for raw-HTML sinks |
| `tests/test_phase2_core.py` | Scribe redaction, Glance View contents, staleness, conflict rule |
| `tests/test_self_learning_importance.py` | **Required (bonus).** Adaptive prioritisation end to end, its boundaries, and what it refuses to learn |
| `tests/test_data_decay.py` | Reversible compression, protection rules, provenance survival across the decay boundary |

### What the required tests found

`test_concurrent_edits.py` found a real defect. The optimistic-lock check reads
`version_number`, compares it, and only then writes — check-then-act, not a
lock. Under genuine parallelism two callers both passed the comparison, and the
`uq_entry_version` constraint refused the second, which meant **no edit was ever
lost**. But that refusal surfaced as an unhandled `IntegrityError` and a 500,
which tells the user nothing and carries none of the state they need to recover.

Interleaved tests reported everything as correct; only real threads opened the
window. Fixed in `_appending_version` (`backend/app/routes/entry_routes.py`) so
both detection paths return the same 409, and pinned by
`test_the_loser_of_a_real_race_gets_a_conflict_not_a_crash`. Recorded as D-037.

---

## How the AI scribe works

The scribe turns a consult transcript into a timeline entry. The steps below
matter mostly for two reasons: redaction happens before the text goes anywhere
near a model and again inside the client that sends it, and the entry that comes
out is marked as machine-authored in the data itself rather than by a label the
interface could get wrong.

```
POST /patients/{id}/scribe {"interaction_type": "doctor_patient_consult"}
```

1. A synthetic transcript is materialised for that patient, identifiers and all
   (`services/transcripts.py` — every name, NRIC and phone number in it is
   invented, and they are there so redaction has something real to remove).
2. Each turn is redacted and stored **already redacted** as a
   `TranscriptSegment`. The database never holds an identifying transcript.
3. The redacted transcript goes to `llm_client.complete()`, which redacts again
   — idempotent, and it cannot know what its caller did — and refuses to send if
   anything identifying survives.
4. The summary is parsed as JSON. With no API key the stub provider returns
   non-JSON and a deterministic extractive summariser takes over, selecting the
   highest-signal utterances using the same feature vocabulary the Glance View
   scores on. `model_used` records which path ran, so the note never claims a
   model wrote it when one did not.
5. The result is stored as an `Entry` with `author_role=system`, the correct
   `ai_*_summary` type, and `provenance_pointer` = `session://<session_id>`,
   alongside an `AIScribedNote` carrying the session id, model, redaction count
   and confidence.
6. Highlights are generated and scored immediately, so the Glance View reads
   rows rather than scoring on the hot path.

**Confidence is derived, not asserted.** On the offline path it comes from
hedging density in the source transcript: the seeded patient session (full of
"maybe", "I think", "not sure") lands around 0.47 and is flagged for
verification; the nurse consult, which is mostly measurements, lands around
0.77. To run against a real model instead, set `CARENOTE_LLM_PROVIDER=anthropic`
and `ANTHROPIC_API_KEY`; nothing else changes.

---

## Where redaction happens

**`backend/app/ai/redaction.py` → `redact_phi(text: str) -> str`**

It is called from exactly one place: **`backend/app/ai/llm_client.py` →
`complete()`**, which runs it on every prompt and system message
unconditionally before any provider sees them. Callers cannot opt out.

After redacting, `find_residual_phi()` re-scans the payload. If unambiguous PHI
survived, the call raises `PHILeakError` **instead of sending** — fail closed.

Three tests keep this from eroding as the codebase grows: they scan the source
tree and fail if any module other than `llm_client.py` imports an LLM SDK or
references an LLM endpoint, or if a future edit removes the redaction call.

Detected: NRIC/FIN, MyKad, labelled MRN/patient IDs, phone numbers (SG/MY/
international), emails, labelled DOBs, honorific- and label-anchored names,
patronymics, plus any name in a caller-supplied gazetteer.
**Known gaps are stated in `ARCHITECTURE.md` § "What redaction catches, and what
it does not"** — bare lowercase names in prose, transliterated names outside the
gazetteer, and quasi-identifier combinations are not caught. One test asserts
that limitation explicitly rather than hiding it.

### Voice capture is where this rule meets its limit

Audio takes the same route — `asr_client` → turns → `run_scribe` → `redact_phi`
per turn → `llm_client` (which redacts again) — so every *word* is redacted
before a model sees it, and `TranscriptSegment` rows are stored already
redacted.

But the ordering is forced and it is worth being blunt about:

> **Audio cannot be redacted before transcription.** `redact_phi()` is regex
> over text; there is no regex over a waveform. To redact a recording you must
> first know what was said, and that is transcription.

So the chokepoint protects the transcript, not the recording. Whoever transcribes
hears the patient say their own name. The default recogniser runs in-process so
nothing leaves the machine; a hosted one would receive un-redacted speech and is
therefore gated fail-closed behind `CARENOTE_ASR_ALLOW_AUDIO_EGRESS`. The audio
itself is never persisted. See § "Ambient consult capture" and DECISIONS.md
D-045/D-046.

---

## What happens when things fail

Two chokepoints, mirroring the redaction one above.

**`backend/app/ai/llm_client.py` → `LLMUnavailableError`.** Timeouts, transport
errors, 5xx and 429 all become one domain exception. The chokepoint translates;
it does not decide. Each caller chooses whether degrading is safe for its
purpose — the scribe falls back to the deterministic extractive summariser and
labels the result `offline-extractive-v1:provider-unavailable`, and a
patient-facing generator would refuse outright. 4xx deliberately stays loud: a
bad API key hiding behind a slightly worse summary is indistinguishable from a
real outage.

Timeout is 8 seconds, configurable via `CARENOTE_LLM_TIMEOUT_SECONDS`. It was
60, which is a batch-job timeout rather than one a clinician standing next to a
patient can use.

**`backend/app/core/errors.py` → `install_error_handlers()`.** Unhandled
exceptions are logged as type, method, path and an eight-character reference —
never `str(exc)`, which is exactly where SQLAlchemy puts bound parameters. The
same reference goes back to the client, so a clinician can quote it and an
engineer can find the request without the log holding a patient.

It is middleware rather than `@app.exception_handler(Exception)` for a specific
reason: Starlette's `ServerErrorMiddleware` calls a registered handler and then
**re-raises** so the ASGI server can log the traceback. A handler alone would
sanitise the response and leave the log leak exactly as it was.

To see both paths under test:

```bash
CARENOTE_LLM_FORCE_UNAVAILABLE=true pytest tests/test_failure_modes.py -v
```

That flag installs a provider that always fails. It exists because the default
stub provider is in-process and *cannot* fail — which is why the outage path went
unexercised for the entire original build.

## How RBAC is enforced

**`backend/app/security/rbac.py`** (mechanism) ·
**`backend/app/security/policy.py`** (the rules, as a readable matrix)

Server-side, always, on **two dimensions checked together — role AND clinic**.
The frontend also hides things, but that is a convenience and is assumed
compromised.

The two checks are fused so they cannot come apart:

1. `require_access(*roles)` is the only dependency a route may use to learn who
   the caller is. There is no exported `get_current_user`.
2. It yields an **`AccessScope`** — never a `User` — and `AccessScope` is also
   the only database handle a route receives.
3. `AccessScope.query(Model)` applies the clinic filter *before* returning a
   query. Handlers never write a clinic predicate themselves.
4. Querying a model with no `clinic_id` raises `TypeError` rather than returning
   it unfiltered. Fail closed.
5. `clinic_id` comes from the verified JWT only — never a body, query param, or
   header. A token missing role or clinic is rejected, never defaulted.

Forgetting the clinic check is therefore not a mistake that can be made — there
is no API that permits it.

Try it against a running server:

```bash
TOKEN=$(curl -s -X POST localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"clinician_a","password":"carenote-demo"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl localhost:8000/demo/patients -H "Authorization: Bearer $TOKEN"
# -> only Clinic A patients

curl -i localhost:8000/demo/patients/patient-b1 -H "Authorization: Bearer $TOKEN"
# -> 404. Cross-clinic fetch by exact id. 404 not 403, so the response cannot
#    be used to probe whether an id exists in another clinic.
```

---

## Layout

Three directories matter more than the others if you are looking for the parts
that carry the safety properties: `security/` holds the access rules and their
enforcement, `ai/` holds redaction and the only code that talks to a model, and
`services/` holds the domain logic — scoring, the scribe, contradictions,
learning and decay.

```
backend/
  app/
    core/       config, db, enums, provenance URIs, content-free logging
    security/   rbac.py (enforcement) · policy.py (rules) · auth.py (JWT)
    ai/         redaction.py (chokepoint) · llm_client.py (only LLM egress)
                asr_client.py (only audio→text path; egress gate)
    routes/     auth · patients · entries · comments · highlights · glance
                learning (learned weights + decay lifecycle)
                capture (voice capture, transcript, line attribution)
                schemas.py (one entry wire format, used by every route)
    services/   features (clinical vocabulary) · scoring (importance)
                highlights (lifecycle) · glance (Top Card) · scribe (AI pipeline)
                transcripts (synthetic fixtures) · interactions (learning signal)
                learning (behaviour → weights) · decay (hot/warm/cold lifecycle)
                capture (voice orchestration) · attribution (line → segment)
    models.py   full SQLAlchemy schema
  init_db.py    create tables + seed synthetic fixture
frontend/
  public/         manifest.webmanifest · sw.js (shell cache; never caches /api)
                  icons/
  src/
    lib/          api.js (one fetch wrapper) · format.js (display vocabulary)
    components/   GlanceView · Timeline · EntryCard · Comments
                  VersionHistory · PatientHome · LearningPanel · Primitives
                  VoiceCapture · TranscriptPanel (transcript + line sources)
    App.jsx       shell: session, patient switching, provenance jump-to
scripts/        phase1_smoke · phase2_smoke · phase4_smoke · phase5_smoke
                bench_glance · run_decay (the nightly decay job, as a script)
tests/
docs/
ARCHITECTURE.md  stack reasoning, component diagram, enforcement, security posture
SCHEMA.md        ER diagram + entity descriptions
DECISIONS.md     running decision log with trade-offs
ATTRIBUTION.txt  dependencies and licenses
```

---

## Current status

The three subsections below are meant to be read together and in order: what
works, what is partial or was deliberately left out, and what is missing
outright. The third is the longest, which is intentional — a reviewer who finds
an undisclosed gap has reason to wonder what else was not mentioned.

### Built and working

| Area | State |
|---|---|
| Longitudinal timeline, all entry types | Full metadata, date-grouped, empty and processing states defined |
| AI-vs-human distinction | Four independent signals: rail style, rail colour, typeface, explicit label |
| AI scribe pipeline | Three interaction types, transcript → redaction → summary → Entry + provenance |
| Provenance click-through | Lands on the character span, not just the note |
| Glance View | What's new, ranked highlights with reasons, risk flags, AI-confidence flags, open actions |
| Accept / reject | Single click, inline, immediate confirmation; rejections persist as signal |
| Manual highlighting | Select text in any readable entry, including AI notes |
| Threaded collaboration | Replies, resolve/unresolve, validated @mentions, task assignment |
| Revision history | Every edit versions, diff between any two, revert as a new version |
| Concurrency | Optimistic locking; stale writes refused with 409 carrying current state |
| Conflict handling | Clinician precedence **and** a visible flag; disputed content never deleted |
| Patient view | Plain language, calmer register, no scores or clinical shorthand |
| Self-learning importance | Clinician behaviour adapts the ranking; bounded, clinic-scoped, inspectable, and floored on safety vocabulary |
| Data decay | hot → warm → cold with byte-exact reversible compression and protection rules |
| Ambient voice capture | Record in-browser, upload audio, or paste a transcript — all three produce a scribed entry |
| Segment-level provenance | Every summary line links to the spoken segment behind it, with speaker, timestamp and confidence |
| Installable PWA | Manifest + service worker; `/api` is never cached |
| Latency | Measured: P95 **13.3–15.9 ms** server handling across three runs, against a 300 ms budget |
| Model outage handling | Provider 5xx / timeout / transport failure degrades to the deterministic summariser, visibly labelled (D-070) |
| Crash log hygiene | Sanitised-error middleware — type, route and a reference id, never the exception message (D-071) |
| Risk floor language parity | Works in canonical tag space, so English and Malay produce the same floor (D-072) |
| Unreadable content | A substantive turn in an unsupported language is flagged rather than silently producing nothing (D-072) |
| Allergy asserted vs denied | Detected as its own contradiction class at HIGH (D-073) |
| Patient reach | `unread` / `read` / `corrected`; a correction the patient has not seen is surfaced to both sides (D-074) |
| Patient enrolment | Staff can register a patient and issue a login; a phone number is a first-class identifier (D-075) |

### Phase 9 — changes made after the clinic-scenario review

The reviewers supplied sixteen scenarios drawn from real clinic operations and
asked for a self-assessment. Working through them produced six new decisions
(D-070 to D-075) and 51 new tests. The full assessment, including the items still
unresolved, is the honest account; this table is the summary.

| Scenario | Was | Now |
|---|---|---|
| 1 — patient with no email | Identity model fine, but nothing could create the row | Staff-scoped enrolment, phone as identifier |
| 3 — read your logs | Unhandled 500 leaked name + NRIC + content via SQLAlchemy bound parameters | Sanitised middleware; three tests fail without it |
| 6 — trilingual consult | Risk floor English-only; Hokkien produced nothing, silently | Floor is language-independent; unreadable content flagged |
| 8 — model hangs | 60s timeout, no cancel | 8s timeout; cancel still missing |
| 9 — provider 503 | Unhandled 500; the fallback only fired on unparseable JSON | Degrades to the extractive summariser, labelled |
| 11 — link never received | No delivery path, and no way to know | Reach modelled honestly; still no sender |
| 12 — wrong dosage | Correction invisible to the patient as a correction | Plain-language correction banner, computed before the read marker moves |
| 13 — allergy vs denial | Returned **zero** contradictions | `assertion_vs_denial` at HIGH |

Two root causes ran through several of these and are worth stating plainly, since
they are more useful than the individual fixes:

1. **The stub provider cannot fail.** It is in-process, so it cannot time out,
   refuse a connection or return a 503. Every test run and demo for the whole
   build executed against a provider physically incapable of failing, which is
   why the outage path was never exercised. `CARENOTE_LLM_FORCE_UNAVAILABLE` now
   puts that path back under test.
2. **The seed script stood in for features.** `init_db.py` runs in Phase 1 step
   1, so patients always already existed and "how does a patient come to exist?"
   never arose. Anything a seed provides is a feature you have not built and will
   not notice missing.

### Partial or deliberately deferred

- **Self-learning per-user normalisation.** One enthusiastic clinician currently
  counts the same as consensus across a practice. Weight saturation bounds the
  damage and that bound is asserted, but signals should be normalised per user
  before aggregation at real volume.
- **Learning rescore scope.** Weights are clinic-wide; rescoring fires
  per-patient on the write path. A patient nobody has touched keeps stale scores
  until their chart is written to or an admin runs
  `POST /clinic/learning/rebuild` — a nightly job in production. The alternative,
  rescoring an entire clinic on every click, is unbounded work on a hot path.
- **Decay of `Version` snapshots.** Cold compresses `Entry.content` only; the
  version chain still holds every full snapshot. Compression here is a hot-row
  optimisation, not true storage reduction — see the honest byte accounting
  below.
- **Decay scheduling.** No cron and no background worker. `scripts/run_decay.py`
  and the admin endpoint are explicit triggers. A prototype that silently
  rewrote clinical text on a timer would be harder to reason about.
- **Voice capture (Phase 5).** Built, with one large caveat: **the default
  recogniser is a simulated stub that does not perform speech recognition.** See
  "Ambient consult capture" below for exactly what is real and what is not.
- **Real-time multi-user sync.** No WebSocket, no live cursors. Optimistic
  locking covers the collision case the brief names; presence would be demo
  polish paid for in infrastructure.
- **Timeline pagination.** Correct at seed scale, wrong at real scale. Deferred
  with a measured reason: the Glance View P95 is ~11 ms at current depth.

Nothing added to the Glance View read path after Phase 2 — `services/glance.py`
never calls the scorer or touches `FeatureWeight`; it reads scores precomputed
on write. Re-measured on the final build across three runs: P95 **14.3 / 13.3 /
15.9 ms**, against a 300 ms budget. That is a little above the 10.8–13.2 ms
recorded after Phase 4; the chart is two entries deeper and the container was
under different load. At roughly 5% of budget neither the spread nor the drift
changes any conclusion, and the range is reported rather than the best run —
quietly keeping whichever number looks best is the sort of thing this
README exists not to do.

### Known gaps, stated plainly

**This build is not safe for real PHI as-is.** Specifically:

- **No token refresh, rotation or revocation.** Logout clears the cookie; a
  token copied elsewhere stays valid until it expires (60 minutes).
- **No login rate limiting.**
- **No TLS and no encryption at rest locally.** Both are documented decisions
  that terminate at the proxy and storage layer in production, not implemented
  here.
- **Redaction is regex plus a name gazetteer, not clinical NER.** Lowercase or
  transliterated names in running prose can survive. Fails closed on anything it
  does detect post-redaction, but recall is bounded by the patterns.
- **The scribe pipeline is synchronous.** Provider *unavailability* is now
  handled and degrades visibly (D-070), but a process crash mid-run still loses
  the summary rather than leaving a retryable job.
- **No circuit breaker.** During a sustained outage, every consult pays the full
  8-second timeout to learn what the first one already learned.
- **No cancel on a slow model call.** The timeout is now 8 seconds rather than
  60, so the wait is bounded, but a clinician cannot abandon it early.
- **There is no sender.** No email, SMS, WhatsApp or push. Patient-facing content
  is pull-only, and `dispatched` is deliberately not modelled rather than faked
  (D-074). Read state is also per-patient, not per-entry: opening the portal
  marks everything current as read.
- **Language identification is unverified.** It is taken from the ASR provider's
  tag, so a recogniser that mislabels Hokkien as English produces no unreadable
  flag (D-072). Nothing flags untagged content in a *supported* language, which
  is the larger recall gap.
- **Contradiction detection ignores time.** An allergy recorded in 2019 and
  denied today reads identically to the reverse, though the second is far more
  likely to be a genuine correction (D-073).
- **Clinic provisioning is still `init_db.py`.** Deliberate — tenant creation is
  an operator path, not an in-app button. But per-clinic configuration is a real
  gap: clinical vocabulary, red-flag terms, decay thresholds and confidence bands
  are module-level constants shared by every clinic, so a second clinic cannot
  tune any of them without a deploy (D-075).
- **Importance scoring is keyword-based.** A medication absent from the
  watchlist scores as ordinary prose — a recall gap, not a safety gap: an
  unrecognised term is simply not promoted, and the entry still sits in the
  timeline for a human to read.
- **The learning loop has never seen more than one clinician's behaviour.** The
  seeded history is a single synthetic cohort, so disagreement between two
  clinicians in the same clinic is untested behaviour rather than designed
  behaviour.
- **`InteractionLog` tag matching is an unindexed scan.** One per write path.
  The normalised join table that fixes it is specified in `SCHEMA.md` and not
  built.
- **Voice capture does not actually transcribe.** The default recogniser is a
  simulated stub that returns a fixed synthetic transcript. It flags itself
  everywhere it appears, but no audio has ever been transcribed by this build.
- **Audio cannot be redacted before transcription.** There is no regex over a
  waveform, so the redaction chokepoint that protects every other input cannot
  protect a recording. The mitigation is architectural — keep transcription
  inside the trust boundary — and the hosted path is gated fail-closed rather
  than solved.
- **No consent model for patient-made recordings.** The clinician is a party to
  a recording made in the patient view and is never asked, and the clinical view
  shows no indicator that one exists.
- **Enum columns are typed `String`, not `Enum`.** SQLAlchemy therefore returns
  a plain `str` on load, so `is` comparisons against enum members are always
  false. Three shipped that way and were live defects (D-055); they are fixed
  and a source scan now fails the build on the pattern, but correctness here
  rests on a regex rather than the type system. The column migration is the real
  fix and is not done.
- **No formal accessibility audit.** Colour is never the sole signal and
  keyboard focus is visible, but no WCAG/contrast audit or screen-reader pass
  has been run.
- **Mobile is checked, not optimised.** One 375px spot-check per role, no
  responsive redesign and no touch-target audit.
- **Whether any of this actually helps is unmeasured.** Whether promoted content
  shortens a clinician's time-to-decision is the outcome the feature exists for
  and cannot be measured from inside the system. It needs instrumented users.
- **UTC offsets on the wire are enforced by convention, not by types.** Every
  response datetime goes through `UtcDateTime` / `iso_utc` (D-061), and four
  regression tests walk the actual payloads — but a new endpoint that writes
  `created_at: datetime` reintroduces the bug silently until someone adds it to
  the sweep. The columns are still naive `DateTime` because SQLite has nowhere
  to put an offset.
- **The what's-new session cap is a guess.** `MAX_MARKER_AGE` is four hours
  (D-060), chosen as roughly one clinic session and validated against nothing.
  It should be set from how these charts are actually used.
- **Contradiction detection catches three things well, not everything.** Allergy
  vs administration, dose disagreement, and medication status. It rests on a
  medication watchlist rather than a formulary, doses must match a numeric
  pattern, and negation handling is a short lookbehind rather than real scope
  detection. The failure mode is silence, never a wrong answer — but **the
  absence of a contradiction flag is not evidence that the notes agree.**
- **Confidence is a proxy, and an unvalidated one.** It measures hedging density
  in the source transcript, which correlates with but is not the same as how
  well the summary is supported. It has never been checked against labelled
  data. Its one virtue over model self-report is that a reviewer can go and read
  the thing it was computed from.
- **Exposure bias is narrowed, not closed.** One suggestion slot per entry is
  reserved for an unexposed tag, but feedback is still only ever collected on
  items the system chose to surface. Closing it properly needs off-policy
  evaluation against held-out charts.
- **Frontend test coverage is narrow.** A vitest harness now covers
  `readSelectionRange` and the Glance View's action controls (25 tests), which
  are the pieces that fail silently. `EntryCard`, `Comments`, `Timeline`,
  `VersionHistory`, `VoiceCapture` and `PatientHome` have no component tests,
  and nothing exercises the real `fetch` path — `Api` is mocked. There is no
  end-to-end browser test of any kind.
- **Staff are told about corrections they cannot read.** A clinician correction
  is written as a `clinician_section`, which staff may not view under D-004, but
  the disputed original still shows them a "Correction on record" row that leads
  nowhere they are allowed. A consequence of the least-privilege default, left
  as a documented dead end rather than resolved — see the closing note in
  DECISIONS.md D-062's section.
- **`PATCH /entries/{id}` clears the title when `title` is omitted.** Our own UI
  always sends it, so this is invisible in the app and live for any other
  client. Deliberately not fixed in the Phase 7 pass: it changes the meaning of
  an existing request shape.

### Accessibility posture

Implemented: every risk and confidence indicator pairs colour with words and a
shape, so no meaning is carried by colour alone; keyboard focus is visible
throughout; the AI-vs-human distinction survives in greyscale; reduced-motion is
respected on the one animated element.

Not done: no formal WCAG or contrast audit has been run, no screen-reader pass,
and the interface has not been tested with assistive technology. The colour
choices are inherited from the Phase 0 token set and are plausible but
unverified.

### Mobile posture — checked, not optimised

One spot-check at a 375px viewport in a real browser, for both the clinician and
patient views. Result: `scrollWidth == clientWidth == 375`, no horizontally
overflowing elements, both views usable — and one genuine defect, the timeline
legend colliding with its heading and interleaving into unreadable text, fixed
in D-056. Worth noting that the desktop layout it was designed at never showed
it.

What that claim does *not* cover: no responsive redesign, no touch-target size
audit, no testing on a physical device, and no check of the voice recorder on
mobile Safari specifically — which matters, because the PWA capture flow is the
one feature most likely to be used on a phone. "Mobile-checked" here means the
layout survives a narrow viewport, nothing stronger.

---

## Adaptive importance and data decay

Two bonus features, both built rather than only described. The first makes the
Glance View's ranking adapt to what a clinic pays attention to; the second stops
old entries from crowding a chart forever. The interesting part of both is what
they are *not* allowed to do, covered below.

### What the ranking learns, and from what

Every time a clinician or staff member highlights a phrase, confirms or dismisses
a suggestion, comments, or edits, the system records the **feature tags** of what
they touched — `med:warfarin`, `symptom:bleeding` — never the prose. Those tags
aggregate into a weight per clinic, which nudges how similar content ranks on
future Glance Views.

```
InteractionLog  →  time-decayed evidence  →  FeatureWeight  →  Glance View score
   (tags only)         (90-day half-life)      (−1 … +1)        (capped at 25%)
```

The seeded demo clinic shows all three behaviours at once. Open the Glance View
as `clinician_a` and expand **"What this clinic pays attention to"**:

| Tag | Weight | What it means |
|---|---|---|
| `med:warfarin` | **+0.36** | confirmed and hand-highlighted repeatedly — this clinic runs an anticoagulation service and the ranking has noticed |
| `finding:bp_elevated` | **−0.39** | dismissed three times — routine BP is handled by a nurse-led pathway here and does not belong on a doctor's top card |
| `entity:allergy` | **+0.00**, from 2 dismissals | recorded honestly, deliberately not acted on |

That last row is the design, not a bug. See *What it refuses to learn* below.

**Where to look in the code:** `backend/app/services/learning.py` is the whole
loop. `record_interaction()` in `services/interactions.py` calls it, so a signal
cannot be logged without the learner seeing it.

### What it refuses to learn

| Guarantee | Where |
|---|---|
| Cannot invent a highlight, only reorder ones a rule already justified | `highlights.refresh_entry_highlights` rule 1 runs before scoring |
| Cannot exceed 25% of the score, however many times reinforced | weights saturate in (−1, 1) × `W_LEARNED` |
| Cannot be trained to suppress allergy, anaphylaxis, sepsis, critical risk or self-harm content | `learning.NEVER_DAMPENED` — floored at zero (D-041) |
| Cannot cross a clinic boundary | scoped on the evidence read *and* the weight write |
| Cannot be trained by patients, or by authoring volume | role filter; `CREATE` and `VIEW` weighted 0.0 (D-039) |
| Cannot drift from its evidence | `FeatureWeight` is a materialised view over `InteractionLog`; rebuild must reproduce it exactly |

### Data decay

Entries age `hot → warm → cold`. Cold replaces `Entry.content` with an
**extractive** summary — real sentences by the original author, never
paraphrased — and compresses the original into `EntryArchive`, recoverable byte
for byte.

```bash
python scripts/run_decay.py --clinic clinic-a            # preview; changes nothing
python scripts/run_decay.py --clinic clinic-a --apply    # actually compress
```

Preview is the default, and applying is admin-only. This is the only operation in
the system that rewrites stored clinical text.

**Nothing that still matters is compressed.** An entry is held at `warm` forever
if it has an unresolved task, an open comment, a clinician-confirmed highlight, a
flagged conflict, high/critical risk, or safety-critical content. The seed shows
both halves: `entry-a1-hist-2026` (201 days) is compressed, while
`entry-a1-hist-2025` (498 days) is held because it documents a penicillin
allergy. Old does not mean settled.

**Honest byte accounting.** On the seeded note:

| Measure | Bytes |
|---|---|
| `Entry.content` before → after | 455 → 64 |
| `EntryArchive` cost | +376 |
| **Net storage delta** | **−15** |

Base64 inflates zlib's output by about a third, so at these note lengths the
archive eats nearly the whole saving. What compression genuinely buys is a **7×
smaller hot row** — what a timeline load actually reads. Total storage turns
meaningfully positive on notes of a few KB. `decay.run()` reports the read-path
and archive figures separately rather than netting them into one flattering
number (D-044).

**Provenance survives it.** Span pointers index the entry's full text, so
compressing content would move every offset onto different words — or overrun the
end and report a dangling pointer for a valid highlight. `provenance.resolve()`
reads through `decay.original_content()`, which returns the archive for cold
entries. `scripts/phase4_smoke.py` confirms all 37 seeded pointers still resolve
after a decay pass.

---

## Ambient consult capture (voice)

Voice-to-note capture for both patient and clinical users. Three ways in, all
producing the same kind of AI-scribed entry:

| Path | Where | Real? |
|---|---|---|
| Record in the browser | Clinical view and patient view, mobile or laptop | Recording is real; **transcription is simulated** |
| Upload an audio file | Same | Same |
| Paste or upload a transcript | Same | **Fully real** — no recogniser involved |

### Read this before judging the demo

**The default speech recogniser does not recognise speech.** With no ASR
provider configured, `_SimulatedProvider` returns a fixed synthetic transcript
derived deterministically from the audio's digest. Your recording is genuinely
uploaded, measured and discarded — but the words that come back were not heard.

Every capture it produces is flagged `transcription_simulated`, and that flag
appears on the entry card, in the transcript panel, and in the API payload. You
will see **⚠ Simulated transcription** in the UI. That is deliberate: a system
whose entire argument is that a clinician can trace any claim to its source
must not itself imply that speech recognition happened when it did not
(DECISIONS.md D-046).

If you want a path with no simulation anywhere, paste a transcript. Nothing is
invented on that path — the text you supply is parsed, redacted, summarised and
attributed, and no recogniser is credited for it.

### Why the recogniser is not just wired up to a real one

Because of a constraint worth stating plainly:

> Audio cannot be redacted before transcription. `redact_phi()` is regex over
> text and there is no regex over a waveform. To redact a recording you must
> first know what was said — and that is transcription.

So the redaction chokepoint that protects every other input in this system
**cannot** protect audio. Whoever transcribes hears the patient say their own
name, in their own voice. A voice is biometric identifying data, so the
recording is PHI before a single word is recognised.

That leaves a choice about *who transcribes*, and `backend/app/ai/asr_client.py`
makes it explicit:

- `stub` (default) — in-process, nothing leaves the machine, flags itself.
- `local` — the production answer: faster-whisper or whisper.cpp beside the API,
  inside the same trust boundary as the database. **Documented interface,
  deliberately unimplemented.**
- `remote` — a hosted recogniser. Refuses to run unless
  `CARENOTE_ASR_ALLOW_AUDIO_EGRESS=true`, and **fails closed** rather than
  degrading to the stub, because a control that silently downgrades is one
  nobody notices is off.

### What happens to the audio

It is read into memory, transcribed, and dropped when the request ends. It is
never written to disk, to the database, or to a log. `CaptureSession` records
how many bytes arrived and that none were kept, so the claim is a stored fact
rather than a sentence here — `test_audio_is_never_retained` walks every column
on the row looking for the bytes (DECISIONS.md D-045).

**The cost:** a mis-transcription can never be checked against what was actually
said. The transcript is the record.

### Provenance back to spoken segments

Click **Transcript & sources** on any AI-scribed note. You get:

- the speaker-labelled transcript with timestamps, per-segment confidence,
  code-switch tags and overlap markers;
- a **"Where each line came from"** list mapping each summary line to the
  segment that produced it — click one to highlight that segment.

Links are established **by matching after the fact, never by asking the model to
cite itself**. Models hallucinate citations as readily as content, and a
citation that looks checkable and is wrong leaves a clinician *more* confident in
a bad line. So:

- **verbatim** — the segment's words are in the line, re-derivable by anyone;
- **derived** — vocabulary survived rewording; labelled as weaker;
- **no row** — the line shows no source rather than a plausible wrong one.

Coverage is reported per note. On the seeded demo consult, 7 of 7 lines attribute
verbatim.

### Who can capture what

Enforced server-side, not by which button the client draws:

| Role | May capture | Produces | May read transcripts |
|---|---|---|---|
| `patient` | patient captures, own record only | `ai_patient_session_summary` | **No** — including their own (D-049) |
| `staff` | clinical captures | `ai_nurse_consult_summary` | Yes |
| `clinician` | clinical captures | `ai_doctor_consult_summary` | Yes |
| `admin` | nothing — oversight, not authorship | — | Yes |

The entry type is derived from the authenticated role, so no request field can
enter a recording as a different kind of encounter than the caller was in. A
patient gets a receipt for what they sent, not the clinical note written from it
— a consult they recorded contains the clinician's half too.

### Fully implemented vs. stretch, stated plainly

**Implemented and demonstrable**

- Upload-based pipeline end to end (audio, audio file, or transcript)
- Live in-browser recording via `MediaRecorder`, mobile and laptop
- Installable PWA (manifest, service worker, icons)
- PHI redaction before any text reaches a model, asserted against the database
- Audio never persisted, asserted against the database
- Speaker-labelled transcript with timestamps and per-segment confidence
- Confidence markers reusing the Glance View's chip and its 0.6 threshold
- Code-switching: per-segment language tags, non-English text preserved intact
- Overlapping-speech detection from timings
- Line-level provenance to transcript segments, with match strength and coverage
- Capture view boundary and cross-clinic isolation, tested at the API layer

**Not implemented — stretch or deliberately deferred**

- **Real speech recognition.** The stub does not transcribe. `_LocalWhisper` is
  an interface with `NotImplementedError` in its body, not a half-wired
  integration.
- **Acoustic diarisation.** Speaker labels come from the transcript source.
  Overlap detection is arithmetic on timings, not voice separation (D-047).
- **Noisy-environment handling.** The browser's `echoCancellation`,
  `noiseSuppression` and `autoGainControl` are requested on the stream — that is
  the browser's work, not ours. No acoustic preprocessing of our own.
- **Multi-device capture.** One recorder, one stream. Merging two devices needs
  clock alignment across them.
- **Multilingual medical terminology — partial.** A Malay clinical vocabulary is
  built (D-058): `bengkak`, `demam`, `sesak nafas` and eleven more map to the
  *same* canonical tag their English counterparts emit, so a Malay symptom
  description now scores and reaches the Glance View, and Phase 4's learned
  weights transfer across the language a patient used. What is **not** built:
  translation, non-English summary generation, and any language other than
  Malay — Mandarin and Tamil are equally common in the same clinics and are not
  covered. The fourteen terms were written from general knowledge and **need
  native-speaker and clinical review before this goes near a real clinic**; the
  mechanism is proven, the word list is a demonstration.
- **Negation, in either language.** "Tiada demam" (no fever) tags a fever
  concern — and so does "Patient denies chest pain" in English. Pre-existing,
  not introduced by the Malay vocabulary, and pinned by a test in both languages
  so a future fix has to address both. Fails in the safe direction: a ruled-out
  symptom is shown for a human to dismiss, never a real one suppressed.
- **Streaming transcription.** Upload-then-process; no live partial transcripts.
- **Consent artefact on patient recordings.** The clinician is a party to a
  patient-made recording and is never asked. Not modelled at all.
- **Audio content scanning.** MIME type and size are checked; file content is
  not scanned or transcoded.

### Trying it

```bash
python scripts/phase5_smoke.py     # 31 checks over real HTTP, no server needed
pytest tests/test_voice_capture.py -v
```

In the UI: log in as `clinician_a`, open a patient, and use **Ambient consult
capture**. Then log in as `patient_a` to see the patient-side recorder and the
receipt it returns.

---

## Security posture at a glance

The same table as in `ARCHITECTURE.md`, repeated here so it is not missed by
someone who only opens one file. Roughly a third of the rows are gaps rather
than features, and that is the honest ratio for a 72-hour prototype.

> **This build is not safe for real PHI as-is.** It is a prototype on synthetic
> data. The gaps below are disclosed deliberately, not discovered later.

| Area | Status |
|---|---|
| PHI redaction chokepoint | Implemented — regex + gazetteer, fail-closed. Not production-grade |
| Stored-XSS / content safety | Implemented — untrusted content never rendered as HTML, enforced by source scan |
| RBAC (role + clinic, server-side) | Implemented — proven over HTTP |
| Logging hygiene | Implemented — content-free by construction |
| JWT storage | Implemented — httpOnly cookie, `SameSite=lax`, 60-minute TTL |
| CSRF defence | Documented decision — `SameSite=lax` only |
| Token refresh / rotation / revocation | **Known gap** |
| Login rate limiting | **Known gap** |
| TLS in transit | Documented decision — terminates at the proxy in production |
| Encryption at rest | Documented decision — managed-Postgres encryption in production |
| AI note immutability | Implemented — no role can edit an AI summary in place; corrections supersede |
| Comment isolation from patients | Implemented — refused at the route *and* stamped internal at write |
| Provenance as reference, not authority | Implemented — pointer resolution is clinic-scoped |
| Scribe failure recovery | **Known gap** — synchronous; a crash mid-run loses the summary |
| Redaction recall on unanticipated names | **Known gap** — patterns + gazetteer only |
| Learning substrate contains no prose | Implemented — tags and counts only; asserted to leak no patient text |
| Learned weights partitioned per clinic | Implemented — scoped on evidence read *and* weight write |
| Safety vocabulary cannot be trained into silence | Implemented — floored at zero, dismissals still shown (D-041) |
| Archived content recoverable | Implemented — byte-exact round trip asserted |
| Archive endpoint returns metadata only | Implemented — reading an original is an audited restore |
| Applying data decay | Implemented — admin-only, `dry_run` by default |
| Per-user normalisation of learning signals | **Known gap** — one enthusiast counts as consensus |
| `Version` snapshots not compressed by decay | **Known gap** — cold is a hot-row optimisation only |
| Audio never persisted | Implemented — in memory only; asserted against the database, not the serialiser |
| Un-redacted audio egress | Implemented (fail-closed gate) — remote ASR refuses without explicit opt-in and does not degrade to the stub |
| Simulated transcription disclosed | Implemented — flag reaches entry card, transcript panel and API payload |
| Capture view boundary (patient ↔ clinical) | Implemented — checked against role server-side; entry type derived from the token |
| Transcripts withheld from patients | Implemented — including their own recording (D-049) |
| Service worker never caches `/api` | Implemented — shell-only cache (D-053) |
| Real speech recognition | **Known gap** — the default recogniser is a simulated stub |
| Acoustic diarisation | **Known gap** — speaker labels come from the transcript source |
| Consent artefact on patient recordings | **Known gap** — the clinician is a party and is never asked |
| Audio content scanning | **Known gap** — MIME and size checked; content not scanned or transcoded |
| Enum comparison correctness | Implemented (guarded) — `==` throughout; a source scan fails the build on identity comparison against an ORM-loaded enum column (D-055) |
| Enum columns typed `String`, not `Enum` | **Known gap** — the structural fix for D-055; correctness rests on a regex scan, not the type system |
| Formal accessibility / WCAG audit | **Known gap** — colour is never the sole signal, but no audit was run |

Full reasoning, including why content is deliberately **not** HTML-escaped
before storage (clinical text contains `BP <120/80` and `dose <5mg`, and
silently corrupting a dose is a worse bug than the XSS it would prevent), is in
`ARCHITECTURE.md` § "Security posture" and `DECISIONS.md` D-015 to D-018.
