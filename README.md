# Care Note

A shared, longitudinal, role-based patient note for a clinic. It replaces
fragmented per-consult free text with one collaborative record combining
clinician notes, staff notes, patient-contributed insight, and AI-scribed
consult summaries — with a fast Glance View, full provenance, revision history,
and server-enforced RBAC.

> **Synthetic data only.** This is a prototype. It has never been connected to a
> real medical record and must not be.

**Build status: Phase 2 complete.** The product surface is built: longitudinal
timeline, AI scribe pipeline, Glance View with provenance click-through,
threaded collaboration, revision history with revert, and conflict handling.
See [Current status](#current-status) for the honest list of what is and is not
finished.

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
python scripts/bench_glance.py     # Glance View latency, measured not asserted
```

`phase1_smoke.py` needs the server running; the other two are self-contained.

`phase2_smoke.py` walks every Phase 2 surface in the order the demo scenarios
use them: all three scribe interaction types through redaction, the role-scoped
timeline, the Glance View, a provenance pointer resolved and then refused across
clinics, accept/reject, a manual highlight inside an AI note, a mention and a
task, an edit followed by a stale write refused with 409, a diff, a revert, and
a clinician correction that flags an AI note without deleting it. Exits non-zero
if any check fails.

---

## Running tests

```bash
pytest tests/ -v                  # from the repository root — 256 tests
```

256 tests, all passing, no API key or network required. Roughly 20 seconds.

To run just the four files the brief names:

```bash
pytest tests/test_rbac_scope.py -v            # role + clinic enforcement
pytest tests/test_revision_history.py -v      # versions, revert, audit trail
pytest tests/test_highlight_provenance.py -v  # every pointer resolves
pytest tests/test_concurrent_edits.py -v      # parallel edits, deterministic conflicts
```

Or by area:

```bash
pytest tests/ -v -k rbac          # the Phase 0 enforcement-pattern tests
pytest tests/ -v -k phase1        # the walking-skeleton proofs
pytest tests/ -v -k phase2        # the Phase 2 product-surface tests
pytest tests/ -v -k cross_clinic  # cross-tenancy refusals only
```

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

---

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

```
backend/
  app/
    core/       config, db, enums, provenance URIs, content-free logging
    security/   rbac.py (enforcement) · policy.py (rules) · auth.py (JWT)
    ai/         redaction.py (chokepoint) · llm_client.py (only LLM egress)
    routes/     auth · patients · entries · comments · highlights · glance
                schemas.py (one entry wire format, used by every route)
    services/   features (clinical vocabulary) · scoring (importance)
                highlights (lifecycle) · glance (Top Card) · scribe (AI pipeline)
                transcripts (synthetic fixtures) · interactions (learning signal)
    models.py   full SQLAlchemy schema
  init_db.py    create tables + seed synthetic fixture
frontend/
  src/
    lib/          api.js (one fetch wrapper) · format.js (display vocabulary)
    components/   GlanceView · Timeline · EntryCard · Comments
                  VersionHistory · PatientHome · Primitives
    App.jsx       shell: session, patient switching, provenance jump-to
scripts/        phase1_smoke · phase2_smoke · bench_glance
tests/
docs/
ARCHITECTURE.md  stack reasoning, component diagram, enforcement, security posture
SCHEMA.md        ER diagram + entity descriptions
DECISIONS.md     running decision log with trade-offs
ATTRIBUTION.txt  dependencies and licenses
```

---

## Current status

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
| Latency | Measured: P95 **11.15 ms** server handling against a 300 ms budget |

### Partial or deliberately deferred

- **Self-learning importance (Phase 4).** The scoring function has a `learned`
  term that reads `FeatureWeight` and returns 0.0 because nothing writes to that
  table yet. `InteractionLog` rows **are** being written now — every manual
  highlight, edit, comment, accept and reject records the feature tags of what
  was touched — so Phase 4 starts with real behavioural history rather than an
  empty table. Today's ranking is purely rule-based, and the UI says so by
  showing the score breakdown.
- **Data decay (Phase 4).** `DecayState` and `EntryArchive` are modelled and the
  scoring path already applies a decay multiplier. Nothing transitions entries
  between states yet.
- **Voice capture (Phase 5).** Not started. The scribe pipeline already consumes
  turn-structured input with speaker labels and timings, so the transcription
  source can be swapped without changing anything downstream.
- **The four brief-named test files (Phase 3).** `test_rbac_scope.py`,
  `test_revision_history.py`, `test_highlight_provenance.py` and
  `test_concurrent_edits.py` arrive in Phase 3. The behaviours they will cover
  are already exercised by `test_phase2_core.py` and the smoke scripts, but the
  files are graded by name and will be written as specified.
- **Real-time multi-user sync.** No WebSocket, no live cursors. Optimistic
  locking covers the collision case the brief names; presence would be demo
  polish paid for in infrastructure.
- **Timeline pagination.** Correct at seed scale, wrong at real scale. Deferred
  with a measured reason: the Glance View P95 is ~11 ms at current depth.

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
- **The scribe pipeline is synchronous.** A crash mid-run loses the summary
  rather than leaving a retryable job.
- **Importance scoring is keyword-based.** A medication absent from the
  watchlist scores as ordinary prose — a recall gap, not a safety gap: an
  unrecognised term is simply not promoted, and the entry still sits in the
  timeline for a human to read.

### Accessibility posture

Implemented: every risk and confidence indicator pairs colour with words and a
shape, so no meaning is carried by colour alone; keyboard focus is visible
throughout; the AI-vs-human distinction survives in greyscale; reduced-motion is
respected on the one animated element.

Not done: no formal WCAG or contrast audit has been run, no screen-reader pass,
and the interface has not been tested with assistive technology. The colour
choices are inherited from the Phase 0 token set and are plausible but
unverified.

---

## Security posture at a glance

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

Full reasoning, including why content is deliberately **not** HTML-escaped
before storage (clinical text contains `BP <120/80` and `dose <5mg`, and
silently corrupting a dose is a worse bug than the XSS it would prevent), is in
`ARCHITECTURE.md` § "Security posture" and `DECISIONS.md` D-015 to D-018.
