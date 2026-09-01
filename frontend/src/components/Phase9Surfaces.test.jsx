/**
 * The Phase 9 UI surfaces.
 *
 * Every one of these has working backend logic and, before this file, no test
 * that anything rendered it. That gap is the reason they exist: a delivery
 * state computed correctly and never drawn is indistinguishable, from the
 * clinician's side, from not having been computed at all.
 *
 * `Api` is mocked for the same reason as the sibling suite — what is under test
 * is what the component draws given a payload, not the wrapper's contract with
 * the server, which `tests/` covers end to end.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../lib/api', () => ({
  Api: {
    setTaskStatus: vi.fn().mockResolvedValue({}),
    acceptHighlight: vi.fn().mockResolvedValue({}),
    rejectHighlight: vi.fn().mockResolvedValue({}),
    learning: vi.fn().mockResolvedValue({ weights: [], signal_counts: {} }),
    rebuildLearning: vi.fn().mockResolvedValue({ weights: [], signal_counts: {} }),
  },
}))

import GlanceView from './GlanceView'
import PatientHome from './PatientHome'

const NOW = '2026-08-28T09:00:00+00:00'

function glanceFixture(overrides = {}) {
  return {
    patient: { id: 'patient-a1', name: 'Amira Rahman', mrn: 'MRN-A-40192', dob: '1968-03-11' },
    generated_at: NOW,
    since: '2026-08-28T08:00:00+00:00',
    whats_new: { since: NOW, count: 0, entries: [], first_visit: false },
    highlights: [],
    open_actions: [],
    risk_flags: [],
    confidence_flags: [],
    conflicts: [],
    contradictions: [],
    patient_delivery: { reachable: true, unread_count: 0, corrected_unread_count: 0, items: [] },
    counts: { entries: 8, ai_scribed: 1, open_tasks: 0 },
    ...overrides,
  }
}

function staleHighlight(overrides = {}) {
  return {
    id: 'hl-1',
    entry_id: 'entry-a1-nurse',
    span_start: 10,
    span_end: 40,
    span_text: 'allergic to penicillin',
    current_span_text: 'tolerates penicillin well',
    risk_reason: 'Allergy recorded at triage',
    provenance_pointer: 'entry://entry-a1-nurse#span:10-40',
    status: 'suggested',
    score: 0.8,
    score_breakdown: [],
    feature_tags: ['entity:allergy'],
    created_by_role: 'system',
    is_manual: false,
    stale: true,
    source_version_number: 2,
    entry_version_number: 5,
    entry_type: 'staff_note',
    entry_title: 'Triage note',
    entry_timestamp: NOW,
    entry_author_role: 'staff',
    is_ai_scribed: false,
    ai_confidence: null,
    can_decide: true,
    ...overrides,
  }
}

// --- scenario 16: stale provenance shows both versions -------------------

describe('stale highlight', () => {
  it('shows the anchored text and the current text side by side', () => {
    render(
      <GlanceView
        glance={glanceFixture({ highlights: [staleHighlight()] })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    // Appears twice by design: as the claim, and again in the comparison.
    expect(screen.getAllByText(/allergic to penicillin/).length).toBeGreaterThan(1)
    // ...and what a naive implementation would have shown in its place.
    expect(screen.getAllByText(/tolerates penicillin well/).length).toBeGreaterThan(0)
  })

  it('names both version numbers so the change is addressable', () => {
    render(
      <GlanceView
        glance={glanceFixture({ highlights: [staleHighlight()] })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    const byContent = (needle) => (_c, element) =>
      element?.textContent?.replace(/\s+/g, ' ').includes(needle)
    expect(screen.getAllByText(byContent('Highlighted (v2)')).length).toBeGreaterThan(0)
    expect(
      screen.getAllByText(byContent('Now at that position (v5)')).length
    ).toBeGreaterThan(0)
  })

  it('says so plainly when the span no longer exists rather than showing a fragment', () => {
    render(
      <GlanceView
        glance={glanceFixture({
          highlights: [staleHighlight({ current_span_text: null })],
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(
      screen.getByText(/This part of the note no longer exists/)
    ).toBeTruthy()
  })

  it('draws no comparison block for a highlight that is not stale', () => {
    render(
      <GlanceView
        glance={glanceFixture({
          highlights: [staleHighlight({ stale: false, current_span_text: null })],
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(screen.queryByText(/Now at that position/)).toBeNull()
  })
})

// --- scenarios 11/12: did it reach the patient ---------------------------

describe('patient reach', () => {
  it('flags a correction the patient has not seen', () => {
    render(
      <GlanceView
        glance={glanceFixture({
          patient_delivery: {
            reachable: true,
            unread_count: 0,
            corrected_unread_count: 1,
            items: [
              {
                entry_id: 'e-instr',
                title: 'Your next steps',
                type: 'patient_instruction',
                state: 'corrected',
                version: 2,
                label:
                  'Corrected since the patient last read it — they may be acting on the old version',
              },
            ],
          },
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(screen.getByText('Corrected, not seen')).toBeTruthy()
    expect(screen.getByText(/acting on the old version/)).toBeTruthy()
  })

  it('distinguishes a patient with no login from one who simply has not read it', () => {
    render(
      <GlanceView
        glance={glanceFixture({
          patient_delivery: {
            reachable: false,
            unread_count: 1,
            corrected_unread_count: 0,
            items: [
              {
                entry_id: 'e-instr',
                title: 'Your next steps',
                type: 'patient_instruction',
                state: 'unread',
                version: 1,
                label: 'No patient login exists — this cannot be read by the patient',
              },
            ],
          },
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(screen.getByText('Patient not reachable')).toBeTruthy()
    expect(screen.getByText(/Register one from the patient record/)).toBeTruthy()
  })
})

// --- scenario 6: content the system could not read -----------------------

describe('unreadable content flag', () => {
  it('is shown without a confidence number attached to it', () => {
    render(
      <GlanceView
        glance={glanceFixture({
          confidence_flags: [
            {
              entry_id: 'e-ai',
              type: 'ai_doctor_consult_summary',
              title: 'Consult summary',
              confidence: null,
              confidence_band: 'unread',
              label:
                '1 part of this consult were in a language the system cannot read — open the transcript',
              session_id: 's-1',
              model_used: 'stub',
              timestamp: NOW,
            },
          ],
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(screen.getByText('Not read by the system')).toBeTruthy()
    expect(screen.getByText(/language the system cannot read/)).toBeTruthy()
  })

  it('renders both an unread and a low-confidence flag on the same entry', () => {
    // Regression: keying on entry_id alone collapsed these into one.
    render(
      <GlanceView
        glance={glanceFixture({
          confidence_flags: [
            {
              entry_id: 'e-ai',
              type: 'ai_doctor_consult_summary',
              title: 'Consult summary',
              confidence: null,
              confidence_band: 'unread',
              label: 'part of this consult could not be read',
              timestamp: NOW,
            },
            {
              entry_id: 'e-ai',
              type: 'ai_doctor_consult_summary',
              title: 'Consult summary',
              confidence: 0.41,
              confidence_band: 'low',
              timestamp: NOW,
            },
          ],
        })}
        onJumpTo={() => {}}
        onChanged={() => {}}
        canDecide
      />
    )
    expect(screen.getByText('Not read by the system')).toBeTruthy()
    expect(screen.getAllByText(/Consult summary/).length).toBe(2)
  })
})

// --- scenario 12, patient side ------------------------------------------

describe('patient correction banner', () => {
  function careFixture(overrides = {}) {
    return {
      patient: { id: 'patient-a1', name: 'Amira Rahman' },
      generated_at: NOW,
      since: NOW,
      new_since_last_visit: 0,
      labels: {},
      corrections: [],
      next_steps: [],
      updates: [],
      your_notes: [],
      ...overrides,
    }
  }

  it('leads the page when something the patient read has changed', () => {
    render(
      <PatientHome
        care={careFixture({
          corrections: [
            {
              entry_id: 'e-instr',
              title: 'Your next steps',
              message:
                'This was updated after you last read it. Please check it again — if you were following the earlier version, stop and read this one.',
            },
          ],
        })}
      />
    )
    expect(screen.getByText(/Please read this first/)).toBeTruthy()
    expect(screen.getByText(/stop and read this one/)).toBeTruthy()
  })

  it('tells the patient what to do if they are unsure', () => {
    render(
      <PatientHome
        care={careFixture({
          corrections: [{ entry_id: 'e', title: 'T', message: 'changed' }],
        })}
      />
    )
    expect(screen.getByText(/call the clinic before/)).toBeTruthy()
  })

  it('shows nothing when there is nothing to correct', () => {
    render(<PatientHome care={careFixture()} />)
    expect(screen.queryByText(/Please read this first/)).toBeNull()
  })
})
