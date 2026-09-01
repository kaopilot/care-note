# Scenarios 1–16 → tests, and the capability self-assessment

Two tables. The first maps every clinic scenario to the tests that cover it and
states the current verdict. The second is the twelve-point capability list,
assessed the same way.

Verdicts are against the build as it stands, not as intended. Where something
does not survive, the row says so — a page of green ticks would be less use to a
reviewer than an accurate one.

```bash
pytest tests/ -q          # 509 backend
cd frontend && npm test   # 44 component
```

---

## Scenarios 1–16

| # | Scenario | Verdict | Tests |
|---|---|---|---|
| 1 | Patient with no email | **SURVIVES** | `test_enrolment.py` (10) — phone as username, login issued and used, permissive validation |
| 2 | Clinic isolation, one line | **SURVIVES** | `test_rbac_scope.py`, `test_phase1_cross_clinic.py`, `test_rbac_pattern.py` |
| 3 | Read your logs | **SURVIVES** | `test_failure_modes.py::test_crash_log_carries_no_patient_data` and siblings — all three fail without the middleware |
| 4 | Prove the ordering | **SURVIVES** | `test_llm_chokepoint.py` (no other module may reach a model), `test_redaction.py` |
| 5 | Clinic B on Monday | **PARTIAL** | `test_phase1_cross_clinic.py`, `test_enrolment.py`. No per-clinic config exists — see below |
| 6 | Trilingual consult | **PARTIAL** | `test_language_risk_floor.py` (13), `test_multilingual_features.py` |
| 7 | Allergy at minute two | **DOES NOT** | `test_capture_timing.py` (3) — pins the batch boundary deliberately |
| 8 | Model hangs 45s | **PARTIAL** | `test_failure_modes.py::test_timeout_is_short_enough_for_a_consult` |
| 9 | Provider 503 for an hour | **SURVIVES** | `test_failure_modes.py` — degraded summary produced and labelled |
| 10 | Two people, same note | **SURVIVES** | `test_concurrent_edits.py` |
| 11 | Link never received | **PARTIAL** | `test_delivery_state.py` (9) — reach modelled; there is no sender |
| 12 | Patient summary wrong by one dosage | **SURVIVES** | `test_delivery_state.py` (correction path), `test_regeneration_and_dosage.py` (the gate) |
| 13 | Allergy asserted vs denied | **SURVIVES** | `test_contradiction_denial.py` (11) |
| 14 | A number that means nothing | **SURVIVES** | `test_evaluation_and_abstention.py`, `test_language_risk_floor.py` |
| 15 | Ranking learns from what it showed | **SURVIVES** | `test_self_learning_importance.py` |
| 16 | Highlight cites edited source | **SURVIVES** | `test_highlight_provenance.py`, `Phase9Surfaces.test.jsx` (side-by-side) |

**Tally: 11 SURVIVES · 4 PARTIAL · 1 DOES NOT.**

The three that moved furthest — 3, 9 and 13 — were all DOES NOT in the first
assessment. Scenario 7 stayed put, and the test for it asserts the limitation
rather than papering over it.

---

## The twelve capabilities

| Capability | Verdict | Where, and what is missing |
|---|---|---|
| Streaming real-consult audio, noisy ASR | **DOES NOT** | `ai/asr_client.py` is a fixture-backed stub. Upload is whole-file. No noise handling, no live stream. The pipeline consumes turn-structured input, so the recogniser is swappable — but nothing streams today |
| Speaker attribution and diarization | **PARTIAL** | Speaker labels and per-segment confidence are carried end to end and drive provenance (`test_voice_capture.py`). Overlap is detected by **timing arithmetic**, not acoustics (D-047). No acoustic diarization |
| Within-statement code-switching | **PARTIAL** | `en`/`ms` handled and tagged, including mid-sentence (`test_multilingual_features.py`). Romanised Hokkien produces no tags and is **flagged as unread** rather than silently ignored (D-072) |
| Multilingual downstream processing | **PARTIAL** | English and Malay produce the *same canonical tags*, so one concept is one learnable feature, and the risk floor is language-independent (D-058, D-072). Only two languages |
| Medication and dosage confirmed against references + human | **PARTIAL** | `services/dosage.py` — 17-drug adult single-dose reference; implausible doses gate patient-facing writes with a recorded human override (D-079). No formulary, no interactions, no renal/weight adjustment, no frequency parsing |
| Immutable, version-bound provenance | **SURVIVES** | Highlights anchor to `source_version_number`; stale ones resolve against the old snapshot and render side by side with current text (D-030, D-076) |
| Extraction under negation, correction, conflicting sources | **SURVIVES** | Negation-aware risk floor (D-072), `assertion_vs_denial` (D-073), version history as the correction record |
| Real-time collaborative editing without lost updates | **PARTIAL** | No lost updates — optimistic version check plus a `uq_entry_version` constraint that is the real serialisation point (D-037). **Not real-time**: no presence, no live cursors, no CRDT. Two people find out at save, not at 09:14 |
| AI regeneration preserving human-confirmed state | **SURVIVES** | Reuses the entry so accepted highlights, comments and tasks survive; **refuses outright** when a human has edited (D-078) |
| Contradictory human / patient / AI assertions | **SURVIVES** | Four classes, both sides cited, `human_human` marked, and deliberately never resolved (D-068, D-073) |
| Audience-appropriate outputs | **SURVIVES** | Separate patient view in plain language with a distinct visual register; no machine text can be patient-facing at all (D-067) |
| Self-learning: clinic-scoped, bounded, auditable, fatigue-resistant, exposure-bias evaluated | **SURVIVES** | Per-clinic weights, saturation and 90-day half-life, `NEVER_DAMPENED` floors, deterministic exploration slot, and a `/clinic/learning` endpoint that shows a clinic what it taught the system (D-041, D-069) |

**Tally: 6 SURVIVES · 5 PARTIAL · 1 DOES NOT.**

---

## The four that would move next

1. **Streaming capture (7, and the first capability).** The deterministic
   extractors — `contradictions.detect` and `features.tag_span` — are pure
   functions over text and need no model, so running them on partial transcript
   would put allergy detection roughly ten seconds behind the utterance.
   `test_capture_timing.py` asserts exactly that, so the claim is checkable
   rather than a promise. What it does not solve is the interruption policy:
   when is it acceptable to interrupt a doctor mid-sentence? That wants a
   clinician, not an engineer.
2. **A sender (11).** The delivery state machine is built and one state —
   `dispatched` — is deliberately absent because nothing dispatches. Adding a
   channel slots in without disturbing `unread` / `read` / `corrected`.
3. **Per-clinic configuration (5).** Vocabulary, red-flag terms, decay
   thresholds and confidence bands are module constants shared by every tenant.
   The data model is already multi-tenant; the configuration surface does not
   exist.
4. **Presence (10).** Correctness holds, but two people still discover a
   collision at save rather than avoiding it at 09:14.
