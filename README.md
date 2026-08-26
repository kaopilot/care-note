# Care Note

A shared, longitudinal, role-based patient note for a clinic. It replaces
fragmented per-consult free text with one collaborative record combining
clinician notes, staff notes, patient-contributed insight, and AI-scribed
consult summaries — with a fast Glance View, full provenance, revision history,
and server-enforced RBAC.

> **Synthetic data only.** This is a prototype. It has never been connected to a
> real medical record and must not be.

**Build status: Phase 0 complete.** Architecture, schema, and both safety
boundaries (RBAC, PHI redaction) are built and tested. Product features begin in
Phase 1 — see [Current status](#current-status) for the honest list.

---

## Quick start

Requires Python 3.11+ and Node 18+.

```bash
# 1. Backend
cd backend
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp ../.env.example ../.env                            # optional; defaults work
python init_db.py                                     # create tables + seed
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

---

## Running tests

```bash
pytest tests/ -v            # from the repository root
pytest tests/ -v -k rbac    # just the access-control tests
```

63 tests, all passing, no API key or network required.

| File | Covers |
|---|---|
| `tests/test_rbac_pattern.py` | Role and clinic enforcement, server-side, via HTTP |
| `tests/test_redaction.py` | PHI detection, consistency, idempotence, and stated gaps |
| `tests/test_llm_chokepoint.py` | That the redaction boundary cannot be bypassed |
| `tests/test_provenance.py` | Pointer grammar, resolution, cross-clinic refusal |

The four test files named in the brief (`test_rbac_scope.py`,
`test_revision_history.py`, `test_highlight_provenance.py`,
`test_concurrent_edits.py`) arrive in Phase 3, once the features they test
exist.

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
    routes/     auth_routes.py · demo_rbac.py (throwaway, replaced in Phase 1)
    models.py   full SQLAlchemy schema
  init_db.py    create tables + seed synthetic fixture
frontend/       React + Vite + Tailwind scaffold
tests/
docs/
ARCHITECTURE.md  stack reasoning, component diagram, enforcement, security posture
SCHEMA.md        ER diagram + entity descriptions
DECISIONS.md     running decision log with trade-offs
ATTRIBUTION.txt  dependencies and licenses
```

---

## Current status

Built and tested:

- Full data schema — entries, versions, comments, highlights, AI-scribed notes,
  transcript segments, tasks, interaction/audit logs, feature weights, archive
- RBAC: role + clinic, server-enforced, proven by tests and live HTTP
- PHI redaction chokepoint with fail-closed verification
- LLM wrapper with an offline deterministic default provider
- Provenance pointer grammar and resolver
- Content-free audit logging
- Auth + seeded two-clinic synthetic fixture
- Frontend scaffold (intentionally unstyled — design pass is Phase 6)

Not built yet: the timeline UI, Glance View, AI scribe pipeline, comments and
mentions, revision history UI, self-learning importance, data decay
implementation, and voice capture. The `/demo/*` routes exist only to exercise
the RBAC pattern and are replaced by real routes in Phase 1.

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

Full reasoning, including why content is deliberately **not** HTML-escaped
before storage (clinical text contains `BP <120/80` and `dose <5mg`, and
silently corrupting a dose is a worse bug than the XSS it would prevent), is in
`ARCHITECTURE.md` § "Security posture" and `DECISIONS.md` D-015 to D-018.
