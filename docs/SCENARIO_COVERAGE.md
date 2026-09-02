# Scenarios 1–16 → tests, and the capability self-assessment

Two tables. The first maps every clinic scenario to the tests that cover it and
states the current verdict. The second is the twelve-point capability list,
assessed the same way.

Verdicts are against the build as it stands, not as intended. Where something
does not survive, the row says so — a page of green ticks would be less use to a
reviewer than an accurate one.

```bash
pytest tests/ -q          # 628 backend
cd frontend && npm test   # 61 component
```

---

## Scenarios 1–16

| # | Scenario | Verdict | Tests |
|---|---|---|---|
| 1 | Patient with no email | **SURVIVES** | `test_enrolment.py` (10) — phone as username, login issued and used, permissive validation |
| 2 | Clinic isolation, one line | **PARTIAL** | `test_rbac_scope.py`, `test_phase1_cross_clinic.py`, `test_survival_scenarios.py` — enforcement is strong and *singular*; breaking it exposes every clinic (D-085) |
| 3 | Read your logs | **PARTIAL** | `test_failure_modes.py` (crash logs), `test_url_surface.py` (52) — a phone number was in a query string until D-083; access log still unrotated |
| 4 | Prove the ordering | **SURVIVES** | `test_llm_chokepoint.py` (no other module may reach a model), `test_redaction.py` |
| 5 | Clinic B on Monday | **PARTIAL** | `test_clinic_config.py` (10), `test_phase1_cross_clinic.py` — zero schema changes, config surface now real; clinical vocabulary still global (D-086) |
| 6 | Trilingual consult | **PARTIAL** | `test_language_risk_floor.py` (13), `test_multilingual_features.py` |
| 7 | Allergy at minute two | **DOES NOT** | `test_capture_timing.py` (3) — pins the batch boundary deliberately |
| 8 | Model hangs 45s | **PARTIAL** | `test_failure_modes.py::test_timeout_is_short_enough_for_a_consult` |
| 9 | Provider 503 for an hour | **SURVIVES** | `test_failure_modes.py`, `EntryCard.degraded.test.jsx` — degraded, and visibly so since D-082 |
| 10 | Two people, same note | **SURVIVES** | `test_concurrent_edits.py` |
| 11 | Link never received | **PARTIAL** | `test_delivery_state.py` (9) — reach modelled; there is no sender |
| 12 | Patient summary wrong by one dosage | **SURVIVES** | `test_delivery_state.py` (correction path), `test_regeneration_and_dosage.py` (the gate) |
| 13 | Allergy asserted vs denied | **SURVIVES** | `test_contradiction_denial.py` (11), `test_contradiction_grouping.py` (6) — one disagreement is one card since D-081 |
| 14 | A number that means nothing | **SURVIVES** | `test_evaluation_and_abstention.py`, `test_language_risk_floor.py` |
| 15 | Ranking learns from what it showed | **SURVIVES** | `test_self_learning_importance.py`, `test_protected_surface.py` (7) — critical classes bypass ranking entirely (D-084) |
| 16 | Highlight cites edited source | **SURVIVES** | `test_highlight_provenance.py`, `Phase9Surfaces.test.jsx` (side-by-side) |

**Tally: 9 SURVIVES · 6 PARTIAL · 1 DOES NOT.**

Scenario 3 moved **back**, from SURVIVES to PARTIAL, after a self-audit found a
patient's phone number travelling in a query string and therefore into the
access log (D-089). It was fixed and the convention is now a tested invariant,
but the log is still unrotated and unscrubbed, and the earlier SURVIVES was an
overclaim: it had been assessed against the crash path only. Downgrading it is
the honest read.

Scenario 7 stayed put, and the test for it asserts the limitation rather than
papering over it.

---

## The twelve capabilities

| Capability | Verdict | Where, and what is missing |
|---|---|---|
| Streaming real-consult audio, noisy ASR | **DOES NOT** | `ai/asr_client.py` is a fixture-backed stub. Upload is whole-file. No noise handling, no live stream. The pipeline consumes turn-structured input, so the recogniser is swappable — but nothing streams today |
| Speaker attribution and diarization | **PARTIAL** | Speaker labels and per-segment confidence are carried end to end and drive provenance (`test_voice_capture.py`). Overlap is detected by **timing arithmetic**, not acoustics (D-047). No acoustic diarization |
| Within-statement code-switching | **PARTIAL** | `en`/`ms` handled and tagged, including mid-sentence (`test_multilingual_features.py`). Romanised Hokkien produces no tags and is **flagged as unread** rather than silently ignored (D-072). The flag silently failed for Chinese, Japanese and Tamil until D-090 — substantiveness was measured in whitespace tokens |
| Multilingual downstream processing | **PARTIAL** | English and Malay produce the *same canonical tags*, so one concept is one learnable feature, and the risk floor is language-independent (D-058, D-072). Only two languages |
| Medication and dosage confirmed against references + human | **PARTIAL** | `services/dosage.py` — 17-drug adult single-dose reference; implausible doses gate patient-facing writes with a recorded human override (D-079). No formulary, no interactions, no renal/weight adjustment, no frequency parsing |
| Immutable, version-bound provenance | **SURVIVES** | Highlights anchor to `source_version_number`; stale ones resolve against the old snapshot and render side by side with current text (D-030, D-076) |
| Extraction under negation, correction, conflicting sources | **PARTIAL** | Negation-aware risk floor (D-072), `assertion_vs_denial` (D-073). Spoken self-correction inside one transcript is now detected behind an explicit cue (D-089) — a correction phrased without one is not caught, and "no wait" is a pinned known miss |
| Real-time collaborative editing without lost updates | **PARTIAL** | No lost updates — optimistic version check plus a `uq_entry_version` constraint that is the real serialisation point (D-037). **Not real-time**: no presence, no live cursors, no CRDT. Two people find out at save, not at 09:14 |
| AI regeneration preserving human-confirmed state | **SURVIVES** | Reuses the entry so accepted highlights, comments and tasks survive; **refuses outright** when a human has edited (D-078) |
| Contradictory human / patient / AI assertions | **PARTIAL** | Five classes, both sides cited, `human_human` and `same_entry` marked, deliberately never resolved (D-068, D-073, D-089). Recall is bounded by a 17-drug watchlist, not a formulary — the failure mode is silence |
| Audience-appropriate outputs | **PARTIAL — by refusal** | Separate patient view in plain language with a distinct visual register. The system does **not** generate per audience: no machine text can become patient-facing at all (D-067), so a clinician writes it. That is a deliberate non-implementation, not a gap we missed |
| Self-learning: clinic-scoped, bounded, auditable, fatigue-resistant, exposure-bias evaluated | **PARTIAL** | Clinic-scoped, bounded and auditable all hold: per-clinic weights, saturation, 90-day half-life, `NEVER_DAMPENED` floors, `/clinic/learning`. **Not evaluated** — the exploration slot is per-entry novelty, not clinic feedback history, and nothing measures residual bias (D-069 corrected, D-091) |

**Tally: 4 SURVIVES · 7 PARTIAL · 1 DOES NOT.**

Four rows moved down after a post-submission audit that probed running code
instead of re-reading the docs (D-089, D-090, D-091). Two were genuine
overclaims, one was a defect hiding inside a claim, and one is a capability we
declined to build on purpose and should have labelled as such. The earlier
tally read 6 · 5 · 1.

---

## What re-auditing our own answers found

Three defects, all inside rows we had already marked SURVIVES, all found by
probing a running instance with a realistic chart rather than by the test suite.
That is the pattern worth reporting: **each one was invisible to its own tests
because the tests used the minimal shape of the case.**

1. **Contradictions fanned out N×M** (D-081). Every test used one assertion and
   one denial — the single shape where pairwise and grouped output are
   identical. A real chart re-records an allergy at every visit. Four
   assertions against two denials produced eight cards, which filled the
   five-card cap and evicted an unrelated metformin dose disagreement from the
   Glance View entirely. A real conflict made invisible by a different one being
   mentioned often, getting worse the longer the record grew.
2. **Degradation was legible to an auditor, not a clinician** (D-082). The
   outage label lived only in `ai_model_used`, rendered as a 10px grey
   monospace string in the provenance footer. The data was right and the card
   read like an ordinary AI summary.
3. **A phone number in a query string** (D-089). The enrolment route built for
   scenario 1 put the patient's phone number in the URL, and the access log
   records the full request line before the application sees it. Scenario 1's
   answer leaking through scenario 3's door.

The third is why scenario 3 is now PARTIAL rather than SURVIVES. It was assessed
against the crash path, which was the door we had just finished guarding, and
not against the sink that logs every request whether or not our code runs.

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
3. **Per-clinic vocabulary (5).** Volume and retention are configurable per
   clinic since D-086, and onboarding needs no migration and no setup step. What
   is still global is the clinical vocabulary — `features.MEDICATIONS`, red-flag
   terms, Malay mappings — so a paediatric or oncology clinic cannot add its own
   terms without a deploy. It needs care rather than time: additions must be
   additive only, because a clinic that could remove a term could remove
   `entity:allergy` and reach the safety floor sideways.
4. **Presence (10).** Correctness holds, but two people still discover a
   collision at save rather than avoiding it at 09:14.
