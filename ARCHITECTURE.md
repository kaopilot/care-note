# Architecture

**Status:** Phase 0 complete. Scaffolding, schema, and the two safety boundaries
(RBAC, PHI redaction) are built and tested. No product features yet.

---

## Stack

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
it is worth more than the small amount of ceremony it costs.

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

Target: Glance View P95 ≤ 300 ms on a warm path. Not measurable yet — the view
does not exist until Phase 2.4.

Groundwork laid now: a composite index on `(patient_id, timestamp)` covers the
timeline's hot query, and `clinic_id` is indexed on every scoped table so the
RBAC predicate is never a table scan. Measurement method will be recorded here
in Phase 2.4 — the number reported in the final brief will be a measured
distribution over repeated warm requests, not a single sample or an assertion.

---

## Security posture

Three statuses are used throughout, and they mean different things:

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

The brief's central question: *we trust LLMs but only up to a point, then we
need reassurance from clinicians and staff.* Three structural answers, all
already visible in the Phase 0 schema rather than added later as UI polish:

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
with identical visual authority. Uniform confidence in a UI is itself a claim,
and usually a false one.
