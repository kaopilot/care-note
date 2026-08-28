# Architecture

**Status:** Complete (Phases 0–8). The product surface, both bonus tracks
(self-learning importance, data decay), ambient voice capture, and the
evaluation work from Phase 8 are all built. 435 backend tests and 25 frontend
tests pass.

**How to read this document.** Sections were appended as each phase was built,
so it reads roughly in the order the system was made. The early sections
describe foundations that later ones extend rather than replace, which means a
detail from Phase 0 may have been revised further down. If you only want the
current state, read the Stack and Component diagram sections for the shape of
the system, then the Security posture summary, then Phases 4, 5 and 8.

---

## Stack

Every choice here optimises for how much working software can be built in 72
hours, not for how it would be run in production. Where those two point in
different directions the table says so, and the Security posture section lists
what would have to change.

| Layer | Choice | Reasoning |
|---|---|---|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0 | Dependency injection is the mechanism that makes RBAC unforgettable (below). Pydantic gives request validation free. |
| Database | SQLite (dev), Postgres-ready | Zero setup for a reviewer cloning the repo. No SQLite-specific SQL is used, so the URL swaps to Postgres unchanged. |
| Auth | JWT, HS256, seeded users | The brief asks for real *authorisation*, not real *authentication*. Signup/SSO would consume hours and earn nothing. |
| LLM | Single wrapper, stub provider by default | The repo runs end-to-end with no API key. Swapping providers touches one file. |
| Frontend | React 18 + Vite + Tailwind | Fast HMR; Tailwind avoids a CSS architecture decision under time pressure. |
| Tests | pytest | Named test files are a graded deliverable. |

Rejected: Django (heavier than needed, and its ORM/admin coupling fights a
custom RBAC layer); Supabase RLS (genuinely attractive for row-level clinic
scoping, but it moves the security story into a hosted console that a reviewer
cannot read in the repo — enforcement they can audit in source is worth more
here); Next.js full-stack (splitting auth logic across a JS middleware layer and
a Python service doubles the surface where a scoping check can go missing).

---

## Component diagram

Two boxes in this diagram carry the safety properties, and the rest is a
conventional web application. `require_access()` is the only way a request
reaches a route handler, so every role and clinic check happens in one place.
`llm_client.complete()` is the only code that can send text out to a model, so
redaction happens in one place too. Both are deliberately narrow: a rule
enforced at a single point cannot be forgotten in a handler somebody adds later.

```
                         ┌──────────────────────────────────┐
   Browser  ──────────▶  │  React SPA (Vite dev server)     │
   (role-specific UI)    │  UI-level gating = convenience   │
                         │  only; assumed compromised       │
                         └──────────────┬───────────────────┘
                                        │  HTTPS + Bearer JWT
                                        ▼
   ┌─────────────────────────────────────────────────────────────────┐
   │  FastAPI                                                        │
   │                                                                 │
   │   ┌─────────────────────────────────────────────────────────┐   │
   │   │  require_access(*roles)   ← THE security boundary        │   │
   │   │  · verifies JWT                                          │   │
   │   │  · checks role ∈ allowed                                 │   │
   │   │  · yields AccessScope(user, role, clinic_id, db)         │   │
   │   │    ...and yields NOTHING ELSE. No bare User escapes.     │   │
   │   └───────────────────────────┬─────────────────────────────┘   │
   │                               ▼                                 │
   │   ┌─────────────────────────────────────────────────────────┐   │
   │   │  Route handlers                                          │   │
   │   │  scope.query(Model)  → clinic filter applied here,       │   │
   │   │                        not in the handler                │   │
   │   │  scope.assert_can_write_type(...)  → policy.py matrix    │   │
   │   └───────────────────────────┬─────────────────────────────┘   │
   │                               ▼                                 │
   │   ┌──────────────────┐   ┌──────────────────────────────────┐   │
   │   │  SQLAlchemy      │   │  llm_client.complete()           │   │
   │   │  models          │   │  ┌────────────────────────────┐  │   │
   │   └────────┬─────────┘   │  │ redact_phi()  ← chokepoint │  │   │
   │            │             │  └────────────┬───────────────┘  │   │
   │            │             │  ┌────────────▼───────────────┐  │   │
   │            │             │  │ find_residual_phi()        │  │   │
   │            │             │  │ → raise PHILeakError       │  │   │
   │            │             │  └────────────┬───────────────┘  │   │
   │            │             └───────────────┼──────────────────┘   │
   └────────────┼─────────────────────────────┼──────────────────────┘
                ▼                             ▼
         ┌─────────────┐             ┌──────────────────┐
         │  SQLite/PG  │             │  LLM provider    │
         │  (at rest)  │             │  (stub by dflt)  │
         └─────────────┘             └──────────────────┘

   Audit/interaction logging runs alongside, carrying IDs + actions only.
```

---

## Phase 2 components

Everything below sits behind the same `AccessScope` dependency and the same
redaction chokepoint; no Phase 2 feature reaches the database or a model by a
new path.

```
                    ┌──────────────────────────────────────────┐
  transcript ─────► │ services/scribe.run_scribe               │
  (fixture or       │   redact_phi per segment  ──► segments    │
   Phase 5 audio)   │   llm_client.complete     ──► summary     │
                    │   (redacts again, fails closed)          │
                    └───────────────┬──────────────────────────┘
                                    │  Entry(author_role=system)
                                    ▼  + AIScribedNote + provenance
  manual write ────►  Entry ──► services/highlights.refresh_entry_highlights
                                    │        │
                                    │        └─► services/features.tag_span
                                    │                    (tags + human reasons)
                                    │        └─► services/scoring.score_span
                                    │                    (recency|risk|entity|
                                    │                     action|learned)
                                    ▼
                              Highlight rows  ──── read by ───►  services/glance
                                    ▲                                   │
  clinician accept/reject ──────────┘                                   ▼
        │                                                    GET /patients/{id}/glance
        └──► services/interactions.record_interaction ──► InteractionLog
                                                              │
                                                              ▼  (Phase 4)
                                                        FeatureWeight
                                                              │
                                                              └──► scoring.learned_component
```

The loop is closed everywhere except the last arrow. `InteractionLog` rows are
written from Phase 2 onward; `FeatureWeight` is read but not yet written, so the
learned term contributes exactly 0.0 and today's ranking is purely rule-based.

**Phase 4 update:** that last arrow is now connected, and nothing downstream
changed shape — the breakdown keys, the storage format and the components that
render them are all as Phase 2 left them. See *Phase 4 components* below.

**Scoring is precomputed on write.** This is the decision the latency figure
rests on — see *Latency* below.

---

## How RBAC is enforced

**File:** `backend/app/security/rbac.py` (mechanism) and
`backend/app/security/policy.py` (rules).

The requirement is that role and clinic are checked *together*, never
separately. Enforcing that by discipline is fragile — eventually someone writes
a route that checks the role and forgets the clinic filter, and nothing fails
loudly. So the two are fused structurally:

1. `require_access(*roles)` is the only dependency a route may use to learn who
   the caller is. There is no exported `get_current_user`.
2. It yields an **`AccessScope`**, never a `User`. `AccessScope` is also the only
   handle to the database that a route receives.
3. `AccessScope.query(Model)` applies `Model.clinic_id == scope.clinic_id`
   before returning a query object. A handler that wants data has no unscoped
   path to reach for.
4. `AccessScope.query()` **raises `TypeError`** on any model without a
   `clinic_id` column, rather than returning it unfiltered. Fail closed.
5. `clinic_id` comes from the verified JWT only — never from a body, query
   param, or header. A token missing either the role or the clinic claim is
   rejected outright rather than defaulted.

Forgetting the clinic check is therefore not a mistake that can be made, because
there is no API that permits it. That property is what the design is buying;
it is worth the small amount of ceremony it costs.

Cross-clinic fetches by exact id return **404, not 403** — a 403 would confirm
the id exists somewhere, which is itself a leak.

Verified in `tests/test_rbac_pattern.py` (18 tests) and by live HTTP calls
against a running server. Phase 3's `test_rbac_scope.py` repeats this against
real product routes.

### The access matrix

Kept as data in `policy.py` so a reviewer can audit the rules in one table
without reading route code:

| | patient_note | staff_note | clinician_section | patient-facing | AI-scribed | system_event |
|---|---|---|---|---|---|---|
| **patient** | view/write | — | — | view | — | — |
| **staff** | view | view/write | — | view | view | view |
| **clinician** | view | view | view/write | view/write | view | view |
| **admin** | view | view | view | view | view | view |

Two judgment calls where the brief is silent, both recorded in `DECISIONS.md`:
staff cannot view `clinician_sections` (D-004, least privilege); staff *can*
view AI-scribed notes (D-005, they action the follow-ups). Admin reads
everything in-clinic but writes no clinical content (D-011) — oversight, not
authorship, so an admin account cannot quietly alter the record.

---

## API surface

The **Roles** column is the enforcement, not documentation of it — each entry
corresponds to a `require_access()` dependency on that route, checked on the
server. A role absent from the column gets a 403, or a 404 where confirming that
the record exists would itself leak something.

| Route | Roles | Notes |
|---|---|---|
| `POST /auth/login` | — | Sets httpOnly cookie; also returns a bearer token for non-browser clients |
| `GET /auth/me` | any authenticated | Session restore without client-side storage (D-020) |
| `POST /auth/logout` | — | Clears the cookie; tokens stay valid until expiry (no denylist) |
| `GET /patients` | any authenticated | Clinic-scoped; a patient login sees only itself |
| `GET /patients/{id}` | any authenticated | 404 across clinics, 403 for the wrong patient (D-022) |
| `GET /patients/{id}/entries` | any authenticated | Timeline, type-filtered per role in SQL (D-023) |
| `POST /patients/{id}/entries` | staff, clinician, patient | Type must be in the role's `WRITABLE_TYPES`; AI types refused (D-025) |
| `GET /entries/{id}` | any authenticated | The direct-API path an attacker uses; both dimensions apply |
| `PATCH /entries/{id}` | writable types only | Optimistic locking on `expected_version`; 409 carries current state |
| `GET /entries/{id}/versions` | any who may read the entry | Full history, including for AI notes |
| `GET /entries/{id}/diff` | any who may read the entry | Structured `{op, text}` operations, never rendered markup |
| `POST /entries/{id}/revert` | writable types only | Appends a new version; never rolls the number back |
| `POST /entries/{id}/supersede` | clinician | Clinician correction of AI/patient content (D-007) |
| `GET/POST /entries/{id}/comments` | staff, clinician, admin | Patients refused outright, not filtered |
| `POST /comments/{id}/resolve` · `/unresolve` | staff, clinician, admin | Reopening matters as much as resolving |
| `GET/POST /patients/{id}/tasks` | staff, clinician, admin | Assignment is clinic-scoped at the query |
| `POST /tasks/{id}/status` | staff, clinician, admin | Closing a task rescores the entry's highlights |
| `GET /clinic/users` | staff, clinician, admin | Mention/assignment directory; patients excluded |
| `GET /patients/{id}/highlights` | staff, clinician, admin | Type-filtered — never quotes an entry the role cannot read |
| `POST /highlights/{id}/accept` · `/reject` | clinician | Single POST, no body — interaction cost is the design |
| `POST /entries/{id}/highlights` | clinician | Manual highlight, allowed on read-only entry types |
| `POST /patients/{id}/highlights/refresh` | clinician, admin | Rescore on demand; makes learning demonstrable |
| `GET /provenance?pointer=` | any authenticated | Clinic-scoped resolution; a pointer is never an authorisation |
| `GET /patients/{id}/glance` | staff, clinician, admin | The Top Card. Reads precomputed scores |
| `GET /patients/{id}/my-care` | patient | Plain-language patient view; patient role only |
| `GET /scribe/templates` | any authenticated | Available synthetic transcripts |
| `POST /patients/{id}/scribe` | staff, clinician, patient | Patients limited to their own AI session type |
| `GET /health` | — | Liveness |
| `GET /clinic/learning` | staff, clinician, admin | Learned weights + evidence. Asserted to carry no patient text |
| `POST /clinic/learning/rebuild` | admin | Re-aggregates weights from the log. Not on any user-facing path |
| `GET /clinic/decay/preview` | clinician, admin | Dry-run classification of the decay pass |
| `POST /clinic/decay/run` | admin | Applies decay. `dry_run=True` by default |
| `GET /entries/{id}/archive` | clinician, admin | Metadata only (sizes, timestamps) — never the archived content |
| `POST /entries/{id}/restore` | clinician, admin | Audited restore of a cold entry; byte-exact |
| `POST /patients/{id}/captures` | patient, staff, clinician | Ambient capture (multipart). Kind checked against role; entry type derived from the token |
| `GET /captures/{session_id}` | staff, clinician, admin | Transcript + segments. Withheld from patients, including their own (D-049) |
| `GET /entries/{id}/attribution` | staff, clinician, admin | Which spoken segment produced each summary line |
| `/demo/*` | per-route | Phase 0 pattern demo, deliberately retained — see D-057 |

Every response carries `X-Response-Time-Ms` from a middleware wrapping the whole
app — the instrument behind the latency figures below.

Not one handler in `patient_routes.py` mentions `clinic_id` in a filter. Every
read goes through `AccessScope`, which applies the clinic predicate itself. That
is the property Phase 1 was built to test, and it held: the first real feature
routes required no change to the Phase 0 enforcement layer.

**Verification.** `tests/test_phase1_cross_role.py` (16 tests) and
`tests/test_phase1_cross_clinic.py` (15 tests) attack these routes directly over
HTTP with valid tokens for the wrong role and the wrong clinic. Both suites were
mutation-checked: reversing D-004 in `policy.py` fails exactly the two staff
visibility tests, and removing the clinic filter from `AccessScope.query()`
fails 12 of the 15 cross-clinic tests. A security test that cannot fail is worse
than no test, because it is mistaken for coverage.

---

## Where redaction happens

**File:** `backend/app/ai/redaction.py`, function `redact_phi(text) -> str`.
**Called from:** `backend/app/ai/llm_client.py`, function `complete()`.

Every LLM call in this system goes through `complete()`, and `complete()` runs
`redact_phi()` on the prompt and system message unconditionally. Callers cannot
pass pre-redacted text and opt out; redaction is idempotent, so the second pass
is harmless (`test_redaction_is_idempotent`).

After redacting, the payload is re-scanned by `find_residual_phi()`. If
unambiguous PHI survived, the call **raises `PHILeakError` instead of sending**.
A prototype that leaks quietly is worse than one that stops loudly.

Three tests keep the boundary from eroding as later phases add code:
`test_no_other_module_imports_an_llm_sdk`, `test_no_other_module_reaches_an_llm_host`,
and `test_chokepoint_still_calls_redaction` scan the source tree and fail if any
module other than `llm_client.py` reaches a model, or if a future edit removes
the redaction call.

### What redaction catches, and what it does not

Detected: Singapore NRIC/FIN, Malaysian MyKad, labelled MRN/patient IDs,
international and local phone numbers, emails, labelled dates of birth,
honorific- and label-anchored names (`Dr Lim Wei Sheng`, `Patient: Amira
Rahman`), patronymics (`bin`/`binti`/`a/l`/`s/o`), and any name in a
caller-supplied gazetteer of known synthetic names in scope.

**Stated gaps, because a security control nobody knows the edges of is worse
than a weaker one everybody does:**

- A bare lowercase or unanchored first name in running prose is missed unless it
  is in the gazetteer. `test_gazetteer_catches_bare_first_names` asserts this
  limitation explicitly rather than papering over it.
- Transliterated or unusual names absent from the gazetteer.
- Quasi-identifiers: a rare condition plus a precise date can identify a person
  even with every direct identifier removed. Regexes cannot address this.
- Addresses are not currently detected.

Regex over an NER model was a deliberate choice: the data is synthetic, so
recall against real-world name diversity is not what is being tested here — the
presence and un-bypassability of the boundary is. A regex pass is auditable line
by line, which matters more in a trust system than a few points of F1 from a
model nobody can inspect. Production would layer a clinical NER pass (Presidio,
scispaCy) *behind the same `redact_phi` signature*, so no downstream code would
change.

Redaction is consistent within a call: the same name maps to the same
placeholder every time, so `Mr Tan said Tan's cough worsened` still reads to the
model as one person. The placeholder map deliberately does **not** retain
original values — a reversible mapping would be a second copy of the PHI
(`test_placeholder_map_does_not_retain_originals`).

---

## Logging hygiene

**File:** `backend/app/core/audit_logging.py`.

`log_event()` accepts a fixed set of scalar fields. There is deliberately no
`message` or `content` parameter to reach for. Any metadata value over 64
characters, or under a key like `content`/`body`/`transcript`, is replaced with
a length marker before the line is emitted. Verified by
`test_no_prompt_content_in_logs` and by grepping a live server's log for seeded
patient names after exercising every route (clean).

---

## Latency

Target: Glance View P95 ≤ 300 ms on a warm path. What follows is the number, then
how it was produced, then what it does and does not prove — in that order,
because a latency figure without its method is not worth much.

### Measured

`scripts/bench_glance.py`, 200 iterations after 20 discarded warm-up requests,
against a chart of 11 entries carrying 6 highlights, SQLite on local disk:

| Segment | p50 | **p95** | p99 | max |
|---|---|---|---|---|
| Server handling (`X-Response-Time-Ms`) | 10.46 ms | **11.54 ms** | 12.97 ms | 13.41 ms |
| In-process wall clock | 11.36 ms | 12.53 ms | 14.51 ms | 19.53 ms |

**P95 server handling: 11.54 ms, against a 300 ms budget.**

Re-measured in Phase 7 after the fixes in D-059–D-061 touched every timestamp in
this payload, three consecutive runs: P95 **11.54 / 11.09 / 11.63 ms**. The
range is reported rather than the best run.

The history, kept rather than overwritten: 10.8–13.2 ms after Phase 4,
14.26/13.30/15.94 ms in Phase 6, and this range now. The Phase 7 figures are the
lowest of the three despite the chart being deepest, which is container load
rather than anything anyone optimised — reading a 3 ms move at roughly 4% of the
budget as an improvement would be reading noise as a result. The Phase 6 numbers
are left visible above for the same reason they were reported then: quietly
keeping whichever number looks best would undermine the point of recording
them at all.

### Method, and what the number excludes

Timing comes from a middleware that wraps every request and reports
`X-Response-Time-Ms`: the request arriving, every query running, the payload
serialising, the response leaving. That is the segment the application controls.

It **excludes network transit and browser render.** Those depend on where the
service is deployed and what it is opened on; folding a developer machine's
loopback into the figure would be inventing precision. The client measures its
own full round trip separately and displays both numbers in the Glance View
header, so the two segments are never conflated in the demo either.

Warm path means the first 20 iterations are discarded — they pay for connection
setup, SQLAlchemy compiling each query for the first time, and a cold page
cache. None of that is what a clinician opening their fifth chart of the morning
experiences.

### What the number is actually evidence for

Not that the product is fast on real infrastructure — SQLite on local disk with
8 entries is not that test. What it establishes is that **application work is a
small fraction of the budget**, and specifically that no N+1 is hiding in the
hot path. Two design decisions are what that rests on:

- **Highlight scores are computed on write, not on read.** Creating, editing or
  AI-scribing an entry runs `refresh_entry_highlights`; the Glance View reads
  precomputed rows and sorts them. Scoring the timeline per request would put an
  O(entries × sentences) loop inside the budget for no benefit, since the inputs
  only change when someone writes.
- **Timeline enrichment is batched.** Author names, AI provenance, comment counts
  and highlight counts are four grouped queries regardless of chart size
  (`patient_routes.enrich_entries`). Fetching them per row turns a 20-entry chart
  into 80 round trips.

### Honest limits

A real deployment means Postgres over a network, hundreds of entries per
patient, and concurrent load — none of which this measures. The headroom is
large enough (roughly 27×) that the conclusion is unlikely to invert, but the
measurement that would settle it is a loaded staging environment, not this
script. Recorded as a known gap rather than smoothed over.

---

## Security posture

This is the section to read if you want to know whether this build could be
trusted with real data. The short answer is no, and the table says why. Three
statuses are used throughout, and they mean different things:

- **Implemented** — built, tested, and verifiable in this repo.
- **Documented decision** — a deliberate choice for a 72-hour prototype, with
  the production shape stated. Not an oversight.
- **Known gap** — genuinely missing. Listed because a control whose edges nobody
  knows is worse than a weaker one everybody understands.

### Summary

| Area | Status |
|---|---|
| PHI redaction chokepoint | **Implemented** — regex + gazetteer, fail-closed. Not production-grade; see gaps below |
| Stored-XSS / content safety | **Implemented** — untrusted content never rendered as HTML, enforced by source scan |
| RBAC enforcement | **Implemented** — role + clinic fused, server-side, proven over HTTP |
| Logging hygiene | **Implemented** — content-free by construction, verified by grep and test |
| JWT storage (httpOnly cookie) | **Implemented** — `HttpOnly; SameSite=lax; Max-Age=3600` |
| CSRF defence | **Documented decision** — `SameSite=lax` only; no token-based defence |
| Token refresh / rotation / revocation | **Known gap** — no refresh flow, no denylist |
| Login rate limiting | **Known gap** |
| TLS in transit | **Documented decision** — terminates at the proxy in production |
| Encryption at rest | **Documented decision** — managed-Postgres volume encryption in production |
| Password hashing | **Documented decision** — PBKDF2 120k rounds; argon2id in production |
| Highlight staleness on edit | **Implemented** — anchored to a version, never silently re-anchored (D-030) |
| AI provenance and confidence | **Implemented** — session pointer, model path, redaction count, derived confidence, all surfaced |
| Conflict handling | **Implemented** — clinician precedence *and* a visible flag; disputed content never deleted |
| Comment isolation from patients | **Implemented** — refused at the route *and* stamped `is_internal` at write |
| Scribe failure recovery | **Known gap** — synchronous pipeline; a crash mid-run loses the summary rather than leaving a retryable job (D-032) |
| Redaction recall on unanticipated names | **Known gap** — gazetteer + patterns only; lowercase and transliterated names in running prose can survive (D-012) |
| Enum comparison correctness | **Implemented (guarded)** — `==` throughout; a source scan fails the build on identity comparison against an ORM-loaded enum column (D-055) |
| Enum columns typed as `String`, not `Enum` | **Known gap** — the structural fix for D-055. Reloaded values are plain `str`, so correctness rests on a regex scan rather than the type system |

**The current build is not safe for real PHI as-is.** This is stated in the
README as well as here, deliberately, so it cannot be missed by a reader who
only opens one file.

### PHI redaction — implemented

Single chokepoint `redact_phi()` in `backend/app/ai/redaction.py`, called
unconditionally by `complete()` in `backend/app/ai/llm_client.py`, which is the
only module in the codebase that reaches a model. Enforced structurally, not by
convention: three tests scan the source tree and fail if any other module
imports an LLM SDK or references an LLM endpoint, or if the redaction call is
removed. After redacting, the payload is re-scanned and the call raises
`PHILeakError` rather than sending if PHI survived.

**Known gap:** regex redaction is not adequate for real PHI. Bare lowercase
names in prose, transliterated names outside the gazetteer, addresses, and
quasi-identifier combinations (rare condition + precise date) are all missed.
Production needs a clinical NER pass or a vendor de-identification service in
front of this chokepoint — which fits behind the same `redact_phi` signature,
so no downstream code would change. Full detail in the section above.

### Stored XSS / content safety — implemented

This is a rich-text, multi-author, long-lived note system where content crosses
privilege boundaries (a staff note surfaces in a clinician's Glance View), so
stored XSS is the natural vulnerability class. Controls, strongest first:

1. **Untrusted content is never rendered as HTML.** Note and comment bodies are
   plain text; React escapes text children by default, so a stored `<script>` is
   inert. `test_frontend_never_renders_raw_html` scans the frontend and fails
   the build if `dangerouslySetInnerHTML`, `innerHTML =`, or `outerHTML =`
   appears — the same source-scanning technique that keeps the LLM chokepoint
   honest. A second test fails if a Markdown renderer is added without review,
   since Markdown with HTML passthrough re-opens exactly this hole.
2. **Write-time normalisation** — `sanitize_for_storage()` strips NUL and
   control characters, NFC-normalises Unicode, normalises line endings, and caps
   length at 50k characters.
3. **Write-time detection** — `find_injection_markers()` records that a payload
   *looked like* an injection attempt, as metadata, without altering the text.

**We deliberately do not escape or strip HTML on write**, which departs from the
usual "sanitize before storage" advice. The reason is clinical, not technical:
clinical prose legitimately contains angle brackets (`BP <120/80`, `dose <5mg`,
`sats <92% on RA`). Escaping on write stores `BP &lt;120/80`, which React then
escapes *again* on render, showing the clinician a literal `&lt;`. Tag-stripping
is worse — `<5mg` can be eaten entirely, silently turning a dose limit into
`mg`. Silently altering the text of a clinical note is a patient-safety bug, and
a worse one than the XSS it would defend against, because control #1 has already
neutralised the XSS whereas nothing catches a corrupted dose. So content is
stored verbatim and escaping lives at the render boundary.

`escape_html()` exists for surfaces that genuinely emit HTML — PDF export, an
emailed patient summary, any server-rendered page. Those must call it; React
surfaces must not.

**Known gap:** enforcement is at the render boundary and the frontend scan, not
at the database. A future non-React consumer of the API that renders content as
HTML without calling `escape_html()` would be vulnerable. The scan covers this
repo's frontend only.

### Authentication and session handling

**Implemented.** JWT (HS256) with `sub`, `role`, `clinic_id`, `iat` and `exp`
claims, issued at login. Seeded users only, no signup or SSO — the brief grades
authorisation, not authentication (D-002).

**Token storage — implemented as httpOnly cookie.** On login the token is set as
`carenote_access` with `HttpOnly; SameSite=lax; Path=/; Max-Age=3600`, plus
`Secure` when `CARENOTE_COOKIE_SECURE=true` (off for localhost, required in
production). localStorage was rejected: it is readable by any injected script,
so a single stored-XSS bug would escalate into durable account takeover. This
choice composes directly with the XSS controls above (D-016).

The API also accepts `Authorization: Bearer` for tests, curl and non-browser
clients. The header wins when both are present — explicit authority beats
ambient, and a cross-origin attacker cannot set a header, so the ordering does
not weaken the CSRF posture.

**Sharp edge, stated:** login also returns the token in the response body, for
non-browser clients. The browser client must use the cookie and must not persist
that value. This is a convenience that a careless frontend could undo.

**Token expiry — implemented.** 60 minutes (`CARENOTE_JWT_TTL_MINUTES`). Short
enough to bound a stolen token, long enough to survive a consult. A test fails
if the TTL is raised above 120 minutes while no refresh flow exists.

**CSRF — documented decision.** `SameSite=lax` is the only defence. It stops
cookies riding along on cross-site state-changing requests in modern browsers,
which is proportionate for a prototype with no cross-origin surface. Production
should add a double-submit token or require the bearer header for mutations.

**Known gaps:** no refresh flow, so the TTL is the whole session budget and
expiry means re-login. Tokens are stateless with no denylist, so `/auth/logout`
clears the browser cookie but a token copied elsewhere stays valid until it
expires. No rate limiting on login — acceptable for seeded prototype accounts,
not for production; note the login handler already returns identical responses
for unknown-user and wrong-password, so accounts cannot be enumerated.

### RBAC enforcement — implemented

Server-side, on role and clinic together, via a single non-separable dependency
(`require_access` in `backend/app/security/rbac.py`, rules in `policy.py`). The
common real-world RBAC bug is a route that checks role but forgets clinic scope;
here that is not expressible, because the only database handle a route receives
is an `AccessScope` that has already narrowed. Full mechanism above.

Proven by direct API calls in `tests/test_rbac_pattern.py` and a live HTTP
transcript in `docs/PHASE0_VERIFICATION.md` — not by UI-level hiding, which is
assumed compromised. Phase 3's `test_rbac_scope.py` repeats this against the
real product routes.

### Transport and storage encryption — documented decision

| | Local dev | Production posture |
|---|---|---|
| TLS | Not implemented; plain HTTP on localhost | Terminates at the reverse proxy / platform load balancer (nginx, Fly, Render, ALB) with HSTS; app bound to localhost behind it. Not hand-rolled in application code |
| At rest | SQLite, unencrypted, gitignored | Managed Postgres with volume-level encryption (RDS / Cloud SQL / Supabase default), or SQLCipher for single-node |
| Secrets | Env vars; `.env` gitignored, `.env.example` committed with placeholders | Platform secret manager. The default JWT secret is a visibly fake dev string that must be overridden |
| Passwords | PBKDF2-HMAC-SHA256, 120k rounds, per-user salt | argon2id |

This is deployment configuration rather than application code, which is why it
is a decision rather than a gap — but it does mean the build is not safe for
real PHI as-is.

### Logging hygiene — implemented

`log_event()` in `backend/app/core/audit_logging.py` accepts a fixed set of
scalar fields. There is deliberately no `message` or `content` parameter to
reach for, and any metadata value over 64 characters or under a key like
`content`/`body`/`transcript` is replaced with a length marker before emitting.
Logs carry actor ID, action, target type/ID, clinic ID and timestamp only.

One careless log line defeats the redaction effort as completely as skipping
`redact_phi()`, so it gets the same treatment: the careless call is made
inexpressible rather than discouraged. Verified by
`test_no_prompt_content_in_logs` and by grepping a live server's log for seeded
patient names and MRNs after exercising every route — clean, including an MRN
that was returned in a response body (`docs/PHASE0_VERIFICATION.md` §2).

### Data handling

All data is synthetic and hand-written for this project. The system has never
been connected to a real medical record.

---

## Trust calibration — the design thesis

Clinical staff will trust an AI-assisted record up to a point, and then they
need a reason to believe it. Three answers to that, all built into the Phase 0
schema rather than added later as interface polish:

1. **AI output is a suggestion until a human accepts it.** `Highlight.status`
   starts at `suggested` and requires a clinician decision. No AI-generated
   claim reaches the Glance View as fact on its own authority.
2. **Every claim carries a working link to its source.** `provenance_pointer` is
   required (non-nullable) on `Highlight`, and `resolve()` raises rather than
   degrading when a pointer dangles. Verification is one click, not a request to
   trust.
3. **Provenance is structural, not cosmetic.** The `AIScribedNote` row is what
   makes an entry AI-authored, plus `author_role='system'`. A UI cannot
   accidentally render an AI note as a clinician's, because the distinction is
   in the data.

Plus a fourth, quieter one: `AIScribedNote.confidence` is persisted, so the
interface can show *how sure the model was* rather than presenting every summary
with identical visual authority. Presenting everything with the same certainty
is itself a claim about the content,
and usually a false one.

---

## Phase 4 components

Two bonus features from the requirements: making the Glance View's ranking adapt
to what a clinic actually pays attention to, and compressing old entries that no
longer earn their place in a chart. Neither needed new infrastructure or new
dependencies, and both sit behind the same `AccessScope` dependency as
everything else.

### The learning loop, closed

```
  clinician / staff action
  (highlight, accept, reject, comment, edit)
        │
        ▼
  services/interactions.record_interaction()          ◄── the chokepoint
        │  writes InteractionLog(content_features = TAGS ONLY)
        │  then calls, in the same call, unconditionally:
        ▼
  services/learning.apply_signal()
        │  role filter  (clinician | staff only)
        │  clinic scope (evidence read AND weight write)
        ▼
  services/learning.recompute_tags()
        │  rescan InteractionLog for the touched tags
        │  evidence = Σ action_weight × 0.5^(age_days / 90)
        │  weight   = evidence / (|evidence| + 2.5)      → bounded (−1, 1)
        │  floor at 0 for NEVER_DAMPENED tags
        ▼
  FeatureWeight(clinic_id, feature_tag)
        │
        ▼
  services/scoring.learned_component()  → W_LEARNED × mean(weights)
        │
        ▼
  highlights.refresh_patient_highlights()   ◄── on the write path, not on read
        │
        ▼
  Highlight.score / .score_breakdown  ──► GET /patients/{id}/glance
                                     ──► GET /clinic/learning  (transparency)
```

`record_interaction()` calling `apply_signal()` itself is the load-bearing part.
Recording a behavioural signal and learning from it are one operation, enforced
in one place, so no future route can log an interaction the learning table never
sees. Same reasoning as the redaction chokepoint.

**Nothing here runs on the Glance View read path.** Scores are recomputed when
weights move — on accept, reject, manual highlight, or a clinic rebuild — so the
300ms budget is unaffected. This is the same decision as Phase 2's precomputed
scoring, extended to a new class of write.

### What the learned term can and cannot do

| Property | Mechanism |
|---|---|
| Cannot invent a highlight | `refresh_entry_highlights` rule 1 (no clinical reason → no highlight) runs **before** scoring. Learning re-ranks candidates the rule layer already found; it never creates one. |
| Cannot dominate the score | Weights saturate in (−1, 1) and are multiplied by `W_LEARNED = 0.25`, so the learned term is capped at a quarter of the rule-based total. |
| Cannot silence safety content | `NEVER_DAMPENED` floors allergy, critical-risk, anaphylaxis, sepsis and self-harm tags at zero (D-041). |
| Cannot cross a clinic | `(clinic_id, feature_tag)` unique constraint, plus clinic scoping on the evidence read *and* the weight write. |
| Cannot be trained by patients | Role filter: `clinician` and `staff` only. |
| Cannot drift from its evidence | `FeatureWeight` is a materialised view; incremental and rebuild paths share one function (D-040). |

### Data decay

```
  scripts/run_decay.py  ──┐
  POST /clinic/decay/run ─┴──► services/decay.run(dry_run=True by default)
                                     │
                                     ▼  per entry: classify()
                     ┌───────────────┴───────────────┐
                     │                               │
              age < 45d → hot            age ≥ 45d → protection check
                                                     │
                        ┌────────────────────────────┴────────────┐
                   protected → warm                        not protected
                   (open task / open comment /                   │
                    accepted highlight / conflict /       age ≥ 180d and
                    high|critical risk / allergy,         length ≥ 220ch
                    anaphylaxis, sepsis, self-harm)              │
                                                                 ▼
                                                         compress() → cold
                                                     ┌───────────┴──────────┐
                                            Entry.content =        EntryArchive =
                                            extractive summary     zlib+base64(original)
                                                     │
                                                     ▼
                                     provenance.resolve() reads through
                                     decay.original_content() → the ARCHIVE,
                                     so every span pointer still resolves
```

`restore()` is the inverse and is asserted byte-exact. It sets
`decay_hold_until` so the next pass does not immediately undo it, and it appends
**no `Version`** — nothing about the clinical content changed, and putting a
storage-tier event into a clinical audit trail would make the revision history
harder to read for what it is actually for.

### Security posture — Phase 4 additions

| Control | Status | Note |
|---|---|---|
| Learning substrate contains no prose | **implemented** | `InteractionLog.content_features` stores extracted tags only. `GET /clinic/learning` is asserted to leak no patient text (`test_the_learning_surface_carries_no_patient_data`). |
| Learned weights are clinic-partitioned | **implemented** | Enforced on read and write; asserted against a populated neighbouring clinic, not an empty one. |
| Decay is clinic-scoped | **implemented** | `test_decay_is_clinic_scoped`. |
| Archive endpoint returns metadata, not content | **implemented** | Reading an original is an audited restore; `GET /entries/{id}/archive` returns sizes and timestamps so it cannot be a way round that. |
| Applying decay is admin-only | **implemented** | Preview available to clinicians; `dry_run=True` default. |
| Compressed content is recoverable | **implemented** | Byte-exact round trip asserted. |
| Per-user normalisation of learning signals | **known gap** | One enthusiastic clinician counts the same as practice consensus. Saturation bounds it; normalisation is the real answer at volume. |
| Decay scheduling | **documented decision** | No cron or worker. Explicit trigger only — see D-043. |
| `Version` snapshots are not compressed | **known gap** | Cold is a hot-row optimisation; the version chain still holds full snapshots. See D-044 and the Phase 4 deferred list. |

### Latency — unchanged, and why

The Glance View P95 figure in *Latency* above still stands: Phase 4 added no
work to that path. Every new computation happens on a write (accept, reject,
manual highlight, rebuild, decay run). The one measurable change is that
`POST /highlights/{id}/accept` now rescores the patient's chart before
returning, which is bounded by that patient's entry count — a write-path cost
paid to keep the read path clean.

`POST /clinic/learning/rebuild` and `POST /clinic/decay/run` are deliberately
**not** on any user-facing path. Both are administrative operations whose cost
scales with clinic size, and neither is called during a consult.

---

## Phase 5 components — ambient consult capture

Voice capture lets a clinician or a patient record a consult and have it become
an AI-scribed entry, rather than typing one. It is a bonus feature and the least
finished part of the build: recording and the pipeline behind it are real, but
the speech recogniser is a simulated stub, so **no audio has ever actually been
transcribed by this system.** That is stated on every surface the feature
touches, and the first subsection below explains why we did not simply connect a
real one.

### The one place the redaction rule cannot apply

Everywhere else in this system the rule is *redact before the text leaves*, and
`llm_client.complete()` enforces it structurally: no code path reaches a model
without passing through `redact_phi()` first.

That rule **cannot be applied to audio**. `redact_phi()` is regex over text, and
there is no regex over a waveform. To redact a consult recording you must first
know what was said, and knowing what was said *is* transcription. The ordering
is forced:

```
  audio ──► transcribe ──► redact ──► summarise
             ▲
             └─ whoever does this hears the patient say their own name,
                in their own voice, before any identifier is removed
```

A voice is itself biometric identifying data, so the recording is PHI before a
single word of it is recognised. There is no clever fix — only a choice about
*who transcribes*. `app/ai/asr_client.py` makes that choice explicit and refuses
to make it silently:

| Provider | Where it runs | Audio leaves the box? | Status |
|---|---|---|---|
| `stub` (default) | in-process | never | implemented; flags every capture `simulated` |
| `local` | sidecar inside the trust boundary | never | documented interface, `NotImplementedError` body |
| `remote` | third-party vendor | **yes, un-redacted** | gated; refuses without explicit opt-in |

The gate is the design point. `remote` raises `AudioEgressBlocked` unless
`CARENOTE_ASR_ALLOW_AUDIO_EGRESS=true`, and it **fails closed** — it does not
degrade to the stub. A security control that silently downgrades into a working
request is one nobody ever notices is off.

### Capture pipeline

```
  ┌─────────────────────────┐
  │ PWA (mobile or laptop)  │   MediaRecorder ─┐
  │  or audio file upload   │                  │
  │  or pasted transcript   │ ─────────────────┤
  └─────────────────────────┘                  │
                                               ▼
                              POST /patients/{id}/capture   (multipart)
                                               │
                        role + clinic checked together (require_access)
                        capture kind checked against the caller's VIEW
                                               │
                          ┌────────────────────┴───────────────────┐
                    audio │                                        │ transcript
                          ▼                                        ▼
                asr_client.transcribe()                    capture.parse_transcript()
                 · egress gate                              · JSON or speaker-labelled text
                 · audio dropped after                      · no recogniser credited
                          └────────────────────┬───────────────────┘
                                               ▼
                                       list[Turn]   ← the Phase 2 contract, unchanged
                                               │
                                    scribe.run_scribe()   ← untouched by Phase 5
                                               │
                    redact_phi per turn ──► llm_client ──► structured summary
                                               │
                    Entry (author_role=system) + AIScribedNote
                    + TranscriptSegment[]  (stored already redacted)
                    + SummaryAttribution[] (line ──► segment)
                    + Highlight[]
```

The load-bearing fact: **Phase 5 added no new AI path.** Voice capture converts
a recording into the same `list[Turn]` that Phase 2's fixtures produce, and
hands it to the same `run_scribe`. Redaction, summarisation, provenance,
highlight generation, versioning and RBAC behave exactly as they already did,
and are covered by tests that already existed. Phase 0 modelling transcripts as
speaker-labelled turns with timings and confidence — rather than as flat text —
is what made this a source-swap instead of a parallel pipeline.

### Entry type is derived from the token, not the request

`capture.interaction_type_for(kind, role)` decides what kind of encounter a
recording becomes:

| Caller role | `kind` accepted | Entry type produced |
|---|---|---|
| `patient` | `patient` only | `ai_patient_session_summary` |
| `staff` | `clinical` only | `ai_nurse_consult_summary` |
| `clinician` | `clinical` only | `ai_doctor_consult_summary` |
| `admin` | none — refused by the dependency | — |

There is no field on the request that could enter a recording into the record as
a different kind of encounter than the one the caller was actually in. The
brief's "patient capture in patient view, clinical capture in clinical view" is
therefore a server-side fact, not a matter of which button the client draws.

### Provenance back to spoken segments

`services/attribution.py` links each summary line to the transcript segment that
produced it, **by matching after the fact rather than by asking the summariser
to cite itself**. The reasoning is in D-048; the short version is that models
hallucinate citations as readily as content, and a citation that looks checkable
and is wrong leaves a clinician more confident in a bad line, not less.

A `verbatim` link is re-derivable by anyone: the segment's words are in the
line. A `derived` link survived rewording and is labelled as weaker. A line that
matches nothing gets no row, and the interface says so. Coverage is reported
alongside the note, because a summary where 3 of 8 lines trace to spoken words
is a different object from one where all 8 do.

On the default offline path the demo consult attributes 7 of 7 lines verbatim.

### Signals read from the timings

Overlapping speech, per-segment confidence and code-switching are surfaced in
the transcript panel. Overlap is arithmetic — a segment starting before the
previous one ended — and is flagged because overlapping speech is where
recognisers fail worst. It is **not acoustic diarisation**; speaker labels come
from the transcript source, never from separating voices in a mixed waveform
(D-047).

Confidence reuses the Glance View's `ConfidenceChip` and its 0.6 threshold
rather than inventing a second visual language for "the machine is unsure". One
idea, one vocabulary.

### The service worker's caching policy is a privacy control

Making the app installable needs a service worker. The default offline-first
recipe — cache API GETs — would write consult summaries, staff notes and
transcript segments into the Cache Storage API: origin-scoped, surviving logout
and the 60-minute token TTL, readable by any script on the origin.

That would undo the reasoning behind D-016. Putting the token in an httpOnly
cookie was meant to stop an injected script reading durable secrets; caching the
clinical data those secrets protect hands the script the data directly.

`/api` is therefore network-only and never cached. Only the app shell — HTML,
JS, CSS, no patient data — is cached, which is the part that actually helps
ambient capture: the recorder is local, and the upload can wait for signal.

### Security posture — Phase 5 additions

| Control | Status | Note |
|---|---|---|
| Audio never persisted | **implemented** | In memory only, dropped at end of request. `audio_retained` stored as a fact; `test_audio_is_never_retained` walks every column looking for the bytes. |
| Un-redacted audio egress | **implemented (fail-closed gate)** | `remote` ASR raises `AudioEgressBlocked` without explicit opt-in and does not downgrade to the stub. Both directions tested. |
| Simulated transcription disclosed | **implemented** | `transcription_simulated` reaches the entry card, transcript panel and API `notice`. |
| Capture view boundary | **implemented** | Patient↔clinical kind checked against role server-side; entry type derived from the token. |
| Transcripts withheld from patients | **implemented** | Including a patient's own recording — it contains the clinician's half (D-049). |
| Segment text redacted at rest | **implemented** | Asserted against the DB, not the serialiser. |
| Service worker never caches `/api` | **implemented** | Network-only for API; shell-only cache (D-053). |
| Stored-XSS scan covers shipped JS outside `src/` | **implemented** | Extended in Phase 5 when `public/sw.js` became the first such file; verified by planting an offender. |
| Real speech recognition | **known gap** | The default recogniser is a simulated stub. No audio has ever been transcribed by this build. |
| Acoustic diarisation | **known gap** | Speaker labels come from the transcript source (D-047). |
| Consent artefact on patient recordings | **known gap** | The clinician is a party to a patient-made recording and is never asked. Not modelled at all. |
| Audio upload virus/format scanning | **known gap** | MIME and size are checked; content is not scanned or transcoded. |

### Latency — unchanged

The Glance View P95 figure in *Latency* above still stands. Capture is a write
path, and a slow one (transcription plus summarisation), but it adds nothing to
the read path being measured. The transcript panel is lazy-loaded on click and
is not part of the Glance View render.


---

## Phase 6 — final verification

No new components. This phase packaged the deliverables and ran the checks that
only a finished build can be subjected to. Three of them found something.

### The mobile spot-check

One pass at a 375px viewport in a real browser, both roles. Result:
`scrollWidth == clientWidth == 375` with zero horizontally overflowing elements,
and one genuine defect — the timeline legend interleaving with its heading
(D-056). Documented as **mobile-checked, not mobile-optimised**: the layout
survives a narrow viewport and is usable, but no responsive redesign was done
and no touch-target audit was run.

### The log spot-check

An entry and a comment were deliberately written containing a seeded patient
name, an NRIC-format identifier and a phone number, then login, patient list,
timeline, Glance View, entry create, comment create, scribe and highlight routes
were all exercised against a running server. The log was then grepped for eight
probe terms spanning names, identifiers, MRN, comment body text and summary
prose. **Zero hits.** What the log does carry is one JSON line per action with
actor id, action, target type/id, clinic id, timestamp, and scalar counts
(`{'returned': 7}`, `{'highlights': 6}`, `{'redactions': 4}`). The
`llm.request` line records `prompt_chars` as a number and no prompt.

### The defects no test did find, and what they share

The duplicate-highlight bug (D-055) was live through 334 passing tests and was
found by looking at the Glance View. The tests asserted that highlights resolve,
that provenance points at real spans, that scoring shifts with learning — all
true of a duplicated row. None asserted *how many* rows came back, because
nobody thought to.

Five more were reported afterwards by someone using the build, all live through
385 passing tests (D-059–D-062): a hand-marked highlight vanished from the card;
confirming one suggestion made the others 404; "new since your last visit"
stayed empty for a whole session and then stopped advancing; a task could be
raised and never closed; and every timestamp left the API without a UTC offset,
so a note written seconds ago rendered "8h ago" in the timezone this was demoed
in.

**They are one class, not five accidents.** Each lives in the seam between two
pieces of individually correct code. The manual-highlight bonus is right where
it is written and right where it is recomputed — the ordering of the two is what
is wrong. The timestamps are right in the database and right in the browser —
the contract between them was never stated. Nothing that tests a unit in
isolation can see this, and the suite was heavily weighted toward exactly that.
The Phase 7 regressions are written as end-to-end sequences instead — open the
chart, write a note, reload — and ten of fifteen fail against the previous
commit.

Worth stating plainly in a document that spends a lot of words on structural
enforcement: source scans and fused dependencies catch the classes of error they
were designed for, and every one of these walked past all of them. Two methods
they did not walk past were *looking at the product* and *someone else using
it*. Neither is a formality once the tests are green; on this build they were
the only things that worked.

---

## Phase 8 components — evaluation and abstention

Three numbers appear on the Glance View: a risk badge, a confidence label and an
importance score. Each is now answerable on four questions — what it is, how you
would know it was wrong, what happens when it is, and when it declines to answer
at all. What follows is the implementation side; the reasoning is in
`DECISIONS.md` D-065 to D-069 and the summary table is in the technical brief.

### The risk badge — rules floor it, models may only raise it

```
transcript ──► _infer_risk()  ──────────────► deterministic floor
           └─► LLM ──► summary["risk_level"] ─► model proposal
                                                      │
                            stored = max(floor, proposal)
                            AIScribedNote.model_proposed_risk  = proposal
                            AIScribedNote.risk_floor_applied   = floor > proposal
```

`_infer_risk()` runs on **both** paths — previously it ran only when no live
model was available, which meant the guarantee held on the path nobody would
deploy. It matches an explicit high-risk term list (`chest pain`, `bleeding`,
`melaena`, `syncope`, `suicidal`, `self-harm`, `anaphylaxis`, `sepsis`,
`haemoptysis`, `collapse`) and, failing that, medium-risk tag prefixes.

The asymmetry is the design: a model *raising* a level may have caught something
the keyword tables miss, so that is allowed. A model *lowering* one silently
removes a warning, so that is not. `risk_floor_applied` drives a "Risk set by
rule" chip in the UI, so the provenance of the badge itself is visible.

**Abstention:** an unparseable or unknown `risk_level` from a model falls back to
`low` rather than guessing, and the deterministic floor still applies on top —
so a malformed model response cannot suppress a rule-detected red flag.

### The confidence label — measured from the source, banded numerically

| Band | Range | Meaning |
|---|---|---|
| high | ≥ 0.75 | little hedging in the source; the summary restates things the transcript said plainly |
| medium | 0.60 – 0.75 | some hedging; worth a glance at the source |
| low | < 0.60 | source substantially uncertain; the card flags it and says verify |

`scribe.derived_confidence()` is the single definition, computed from
`features.uncertainty_ratio()` over the redacted transcript, bounded to
0.35–0.90. `glance.LOW_CONFIDENCE_THRESHOLD` **imports**
`scribe.CONFIDENCE_LOW_BAND` rather than restating `0.6`; before this they were
independent constants that happened to agree, and an interface that renders
"medium" while its own low-confidence flag fires teaches the reader to distrust
both numbers.

`AIScribedNote.model_self_reported_confidence` stores what a live model claimed
about itself. It is never displayed and never scored on — it exists so the two
series can be compared later, which is the only way to find out whether a given
model is calibrated.

**Abstention:** the ceiling is 0.90, never 1.0. A summariser reading a transcript
it did not hear, through a recogniser that may have erred, has no basis for
certainty.

### Contradiction detection

`services/contradictions.py`. Pairwise over entries, sentence-level within them,
running on write rather than on the Glance View read path. Every finding cites
**both** entries with a quote and a resolvable pointer, and carries a
`human_human` flag distinguishing the case where no precedence rule exists.

| Class | Severity | Trigger |
|---|---|---|
| `allergy_vs_administration` | critical | allergy recorded for a drug, or a drug in the same class, that another entry records as given or prescribed |
| `dose_disagreement` | high | same drug, two doses, compared on units normalised to mg |
| `status_disagreement` | medium | one entry stops a drug, another has it running |

Findings sort most-severe-first, so a truncated card drops status disagreements
before it drops allergies. The section renders full-width above "what changed":
a clinician who reads one line of the card should have read the most dangerous
thing the system knows.

**Nothing is resolved.** There is no precedence rule between two humans, and
"most recent wins" would discard an allergy recorded last year in favour of a
prescription written today.

#### Recall limits, stated plainly

* Detection rests on `features.MEDICATIONS`, a **watchlist, not a formulary**. A
  drug not on it is invisible to this module.
* Doses must match `\d+(\.\d+)? (mg|mcg|g|ml|units|iu)`. "Two tablets" is not a
  dose to this code.
* Negation is a 40-character lookbehind for `no|not|never|denies|avoid|without|
  nil`, not real scope detection. "No history of the penicillin allergy her
  sister has" is beyond it.
* Only three classes are covered. Contradictory diagnoses, conflicting vital
  signs and disagreeing follow-up intervals are not detected.

The failure mode throughout is **silence, never a wrong answer**. That is the
right direction for this control — but it means the absence of a flag is not
evidence of agreement, and the README says so where a clinician would read it.

### Exposure bias — one reserved slot

`highlights._keep_with_exploration()`. Of `MAX_SUGGESTIONS_PER_ENTRY` slots, one
is given to the highest-scoring candidate whose tags the clinic has never seen
feedback on, if such a candidate exists and clears `MIN_SUGGESTION_SCORE`. It
displaces the weakest of the top, never the strongest.

Deterministic on (entry content, feedback history) — not epsilon-greedy. A card
that differs between two loads of an unchanged chart is a worse property on a
clinical surface than the bias it corrects.

This narrows the feedback loop; it does not close it. Feedback is still only
collected on surfaced items. Closing it properly needs off-policy evaluation
against held-out charts, which needs data this build does not have.

### Security posture — Phase 8 additions

| Area | Status |
|---|---|
| Risk ordinal cannot be lowered by a model | **Implemented** — deterministic floor on both paths, provenance stored per note |
| Displayed confidence is never model self-reported | **Implemented** — derived on both paths; self-report stored, never rendered |
| Patient-facing generation | **Implemented** — structurally impossible; import-time guard on the scribe's type map |
| Human-human clinical contradiction detection | **Implemented (narrow)** — three classes, deterministic, never auto-resolved |
| Contradiction recall | **Known gap** — watchlist not formulary; crude negation; absence of a flag is not evidence of agreement |
| Exposure bias in the learning loop | **Partially mitigated** — one reserved exploration slot; no off-policy evaluation |
| Confidence calibration | **Known gap** — hedging density is a proxy for speaker certainty, not for summary support; never validated against labelled data |
