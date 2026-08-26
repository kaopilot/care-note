# Phase 0 — Verification Evidence

Captured 2026-08-25 15:23 UTC against a live server on port 8099.
Regenerate with: `python init_db.py && uvicorn app.main:app --port 8099`, then the calls below.

Everything here is an assertion the brief requires be *demonstrated* rather
than claimed. The pytest suite covers the same ground; this file exists so a
reviewer can see real HTTP responses without running anything.

---

## 1. RBAC enforcement, server-side

```

### 1. Unauthenticated

$ GET /demo/whoami   (no token)
  -> HTTP 401   {"detail":"Missing bearer token"}
     (401 expected)


### 2. Role dimension

$ GET /demo/clinician-only   as clinician_a
  -> HTTP 200   {"ok":true,"message":"clinician-only payload","user_id":"u-a-clinician"}
     (200 expected)

$ GET /demo/clinician-only   as staff_a
  -> HTTP 403   {"detail":"Role 'staff' is not permitted on this route"}
     (403 expected)

$ GET /demo/clinician-only   as patient_a
  -> HTTP 403   {"detail":"Role 'patient' is not permitted on this route"}
     (403 expected)


### 3. Clinic dimension

$ GET /demo/patients   as clinician_a (Clinic A)
  -> HTTP 200   {"clinic_id":"clinic-a","count":1,"patient_ids":["patient-a1"]}
     (only Clinic A ids)

$ GET /demo/patients   as clinician_b (Clinic B)
  -> HTTP 200   {"clinic_id":"clinic-b","count":1,"patient_ids":["patient-b1"]}
     (only Clinic B ids)

$ GET /demo/patients/patient-b1   as clinician_a  <-- CROSS-CLINIC BY EXACT ID
  -> HTTP 404   {"detail":"Patient not found"}
     (404 expected (not 403))

$ GET /demo/patients?clinic_id=clinic-b   as clinician_a  <-- CLAIM OVERRIDE ATTEMPT
  -> HTTP 200   {"clinic_id":"clinic-a","count":1,"patient_ids":["patient-a1"]}
     (param ignored)


### 4. Both dimensions together

$ GET /demo/patients/patient-a1   as patient_a (own record)
  -> HTTP 200   {"id":"patient-a1","clinic_id":"clinic-a","mrn":"MRN-A-40192"}
     (200 expected)

$ GET /demo/patients/patient-b1   as patient_a  <-- ANOTHER PATIENT
  -> HTTP 403   {"detail":"Patients may only access their own record"}
     (403 expected)

```

Note the two subtle cases:

- **Cross-clinic fetch returns 404, not 403.** A 403 would confirm that
  `patient-b1` exists somewhere in the system, which is itself a leak. The
  response is indistinguishable from a genuinely nonexistent id.
- **The `?clinic_id=clinic-b` override is silently ineffective.** `clinic_id`
  is read from the verified JWT only. There is no code path that reads it
  from a query param, body, or header, so the parameter is simply unused.

---

## 2. Log hygiene

Shared-context rule: logs carry IDs, action types and timestamps only —
never Entry content, Comment bodies, or transcript text.

Sample of the audit log after exercising every route above:

```
2026-08-25 15:22:45,064 carenote.audit INFO {'ts': '2026-08-25T15:22:45.064609+00:00', 'actor_id': 'u-a-clinician', 'action': 'auth.login', 'target_type': 'user', 'target_id': 'u-a-clinician', 'clinic_id': 'clinic-a', 'metadata': {'role': 'clinician'}}
2026-08-25 15:22:45,104 carenote.audit INFO {'ts': '2026-08-25T15:22:45.104322+00:00', 'actor_id': 'u-a-staff', 'action': 'auth.login', 'target_type': 'user', 'target_id': 'u-a-staff', 'clinic_id': 'clinic-a', 'metadata': {'role': 'staff'}}
2026-08-25 15:22:45,142 carenote.audit INFO {'ts': '2026-08-25T15:22:45.142748+00:00', 'actor_id': 'u-a-patient', 'action': 'auth.login', 'target_type': 'user', 'target_id': 'u-a-patient', 'clinic_id': 'clinic-a', 'metadata': {'role': 'patient'}}
2026-08-25 15:22:45,181 carenote.audit INFO {'ts': '2026-08-25T15:22:45.181812+00:00', 'actor_id': 'u-b-clinician', 'action': 'auth.login', 'target_type': 'user', 'target_id': 'u-b-clinician', 'clinic_id': 'clinic-b', 'metadata': {'role': 'clinician'}}
```

Grep of the full server log for seeded patient names, MRNs and the demo
password, after all of the traffic above:

```
$ grep -inE 'Amira|Rahman|Daniel Choo|MRN-|carenote-demo' /tmp/uvicorn2.log
(no matches)
```

Clean. Note that `MRN-A-40192` was returned in an HTTP *response body*
above and still does not appear in the logs — response payloads are never
logged.

---

## 3. Session cookie

Browser sessions carry the token in an httpOnly cookie so an injected script
cannot read it (DECISIONS.md D-016). Response header on login:

```
set-cookie: carenote_access=eyJhbGci...; HttpOnly; Max-Age=3600; Path=/; SameSite=lax
```

`HttpOnly` (unreadable from JavaScript), `SameSite=lax` (not sent on cross-site
state-changing requests), `Max-Age=3600` (60-minute bound, no refresh flow).
`Secure` is added when `CARENOTE_COOKIE_SECURE=true`; it is off for localhost
because a Secure cookie is not sent over plain HTTP.

Authorisation is identical across both token transports — a cookie-only session
is still clinic-scoped:

```
$ curl -b jar.txt localhost:8099/demo/patients/patient-b1     # cookie, no header
  -> HTTP 404   {"detail":"Patient not found"}

$ curl -X POST -b jar.txt -c jar.txt localhost:8099/auth/logout
$ curl -b jar.txt localhost:8099/demo/whoami
  -> HTTP 401
```

---

## 4. Test suite

```
$ pytest tests/ -q
96 passed in 2.28s
```

| File | Covers |
|---|---|
| `tests/test_rbac_pattern.py` | Role and clinic enforcement, server-side, via HTTP; cookie and bearer transports; token expiry |
| `tests/test_redaction.py` | PHI detection, consistency, idempotence, and stated gaps |
| `tests/test_sanitization.py` | Stored-XSS controls; clinical angle brackets survive verbatim |
| `tests/test_llm_chokepoint.py` | That the redaction boundary cannot be bypassed |
| `tests/test_provenance.py` | Pointer grammar, resolution, cross-clinic refusal |

No API key and no network access required — the LLM wrapper defaults to a
deterministic offline stub provider (DECISIONS.md D-010).
