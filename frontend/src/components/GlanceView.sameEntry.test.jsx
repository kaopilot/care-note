/**
 * A contradiction inside one consult, as a clinician meets it.
 *
 * D-089 made the detector able to compare an entry with itself, which is what
 * a transcript needs — `run_scribe` writes one Entry per consult, so an allergy
 * at minute two and a prescription at minute nineteen live in the same row.
 *
 * That created a rendering problem the backend tests could not see. The card
 * was built for two entries: it labels each side by authorship ("Written by a
 * person" / "AI-scribed entry") and tags the pair "Human vs human" when neither
 * side is machine-written. For a same-entry pair both labels are identical and
 * both buttons open the same entry — so the clinician reads it as two sources
 * that happen to carry one id, which looks like a bug in the record rather than
 * a disagreement in the consult.
 *
 * These cover the substitution: "Within one consult", and sides labelled by
 * *order* rather than by author, because order is the only thing that
 * distinguishes them and it is also the thing that decides which dose stands.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

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

const NOW = '2026-08-28T09:00:00+00:00'

function glance(contradictions) {
  return {
    patient: { id: 'patient-a2', name: 'Marcus Teo', mrn: 'MRN-A-40193', dob: '1990-01-02' },
    generated_at: NOW,
    since: NOW,
    whats_new: { since: NOW, count: 0, entries: [], first_visit: false },
    highlights: [],
    open_actions: [],
    risk_flags: [],
    confidence_flags: [],
    conflicts: [],
    contradictions,
    counts: { entries: 3, ai_scribed: 2, open_tasks: 0 },
  }
}

function sameEntryCard(overrides = {}) {
  return {
    kind: 'self_correction',
    severity: 'medium',
    subject: 'amoxicillin',
    detail:
      'Amoxicillin was corrected within one consult — 500mg first, then 250mg. ' +
      'The later figure stands; both are shown because a mis-heard correction ' +
      'reads exactly like a real one.',
    human_human: false,
    same_entry: true,
    left: {
      entry_id: 'entry-a2-ai-consult',
      pointer: 'entry://entry-a2-ai-consult',
      quote: 'I will start you on amoxicillin 500mg three times a day for five days.',
      is_ai: true,
    },
    right: {
      entry_id: 'entry-a2-ai-consult',
      pointer: 'entry://entry-a2-ai-consult',
      quote: 'Sorry, correction, make that amoxicillin 250mg three times a day.',
      is_ai: true,
    },
    also_left: [],
    also_right: [],
    entry_count: 1,
    pair_count: 1,
    ...overrides,
  }
}

describe('a contradiction inside one consult', () => {
  it('says the disagreement is within one consult', () => {
    render(<GlanceView glance={glance([sameEntryCard()])} onJumpTo={() => {}} />)
    expect(screen.getByText('Within one consult')).toBeTruthy()
  })

  it('never claims two humans disagreed when there is only one entry', () => {
    // The pre-existing chip would have read "Involves an AI note" here, which
    // is true and useless: it describes authorship when the useful fact is
    // that nobody else is involved and nothing needs reconciling with anyone.
    render(<GlanceView glance={glance([sameEntryCard()])} onJumpTo={() => {}} />)
    expect(screen.queryByText('Human vs human')).toBeFalsy()
  })

  it('distinguishes the two sides by order, since authorship cannot', () => {
    render(<GlanceView glance={glance([sameEntryCard()])} onJumpTo={() => {}} />)
    expect(screen.getByText(/Said first/)).toBeTruthy()
    expect(screen.getByText(/Said later/)).toBeTruthy()
  })

  it('shows both figures rather than silently applying the later one', () => {
    render(<GlanceView glance={glance([sameEntryCard()])} onJumpTo={() => {}} />)
    // Each figure appears twice — once in the summary line and once in the
    // quote it came from. That duplication is the point: the summary tells a
    // clinician what happened, the quotes let them check it against the
    // transcript without leaving the card.
    expect(screen.getAllByText(/500mg/).length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText(/250mg/).length).toBeGreaterThanOrEqual(2)
  })

  it('keeps both sides openable, so provenance stays addressable', async () => {
    const onJumpTo = vi.fn()
    render(<GlanceView glance={glance([sameEntryCard()])} onJumpTo={onJumpTo} />)
    await userEvent.click(screen.getByText(/Said later/))
    expect(onJumpTo).toHaveBeenCalledWith('entry-a2-ai-consult')
  })

  it('still labels a genuine two-author disagreement by authorship', () => {
    // The substitution must be conditional. A cross-entry human-vs-human
    // disagreement is exactly the case where "who said it" is the useful fact,
    // and losing that label to a blanket change would be a regression.
    const crossEntry = sameEntryCard({
      kind: 'dose_disagreement',
      same_entry: false,
      human_human: true,
      left: { entry_id: 'e1', pointer: 'entry://e1', quote: 'Metformin 1g BD.', is_ai: false },
      right: { entry_id: 'e2', pointer: 'entry://e2', quote: 'Metformin 500mg BD.', is_ai: false },
    })
    render(<GlanceView glance={glance([crossEntry])} onJumpTo={() => {}} />)
    expect(screen.getByText('Human vs human')).toBeTruthy()
    expect(screen.queryByText('Within one consult')).toBeFalsy()
    expect(screen.queryByText(/Said first/)).toBeFalsy()
  })
})
