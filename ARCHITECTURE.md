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

## Transport and storage security

Honest statement of what is and is not in place, as the shared context requires:

| Requirement | Status |
|---|---|
| TLS in transit | **Not implemented locally.** Dev runs plain HTTP on localhost. In production TLS would terminate at the reverse proxy / platform load balancer (nginx, Fly, Render) with HSTS; the app would be bound to localhost behind it. |
| Encryption at rest | **Not implemented locally.** The dev SQLite file is unencrypted and gitignored. Production would use a managed Postgres with volume-level encryption (AWS RDS/GCP Cloud SQL default), or SQLCipher for a single-node deployment. |
| Secrets | Read from environment; `.env` gitignored, `.env.example` committed with placeholder values. The default JWT secret is a visible dev-only string that must be overridden. |
| Password storage | PBKDF2-HMAC-SHA256, 120k rounds, per-user salt. Production would use argon2id. |
| Data | 100% synthetic. Never connected to a real record. |

The honest gap: this is a 72-hour prototype and the crypto posture is
deployment configuration, not application code. Claiming otherwise would be the
kind of unearned assurance this product is specifically supposed to avoid.

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
