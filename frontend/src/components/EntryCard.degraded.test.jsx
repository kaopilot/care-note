/**
 * A degraded AI summary has to look degraded.
 *
 * Scenario 9 asks what the clinician gets when the provider is down for an
 * hour. The backend answer was already right — a rule-derived summary, stored
 * and labelled — but the only trace in the interface was `ai_model_used`
 * rendered as a 10px grey monospace string in the provenance footer, beside
 * the pointer. A clinician reading a card during an outage saw what looked
 * like an ordinary AI summary.
 *
 * These pin the visible half of that claim, and pin it as *distinct from* the
 * confidence signal: "the model was unsure" and "no model read this consult"
 * are different problems with different responses.
 *
 * See DECISIONS.md D-082.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'

vi.mock('../lib/api', () => ({
  Api: {
    updateEntry: vi.fn().mockResolvedValue({}),
    regenerate: vi.fn().mockResolvedValue({}),
  },
}))

import EntryCard from './EntryCard'

function aiEntry(overrides = {}) {
  return {
    id: 'entry-ai-1',
    patient_id: 'patient-a1',
    author_role: 'system',
    author_id: 'system',
    author_name: 'AI scribe',
    timestamp: '2026-08-28T09:00:00+00:00',
    updated_at: '2026-08-28T09:00:00+00:00',
    type: 'ai_doctor_consult_summary',
    title: 'Doctor consult summary (AI-scribed)',
    content: 'Doctor consult: review and plan captured.',
    risk_level: 'medium',
    provenance_pointer: 'session:sess-1',
    version_number: 1,
    is_ai_scribed: true,
    conflict_flagged: false,
    supersedes_entry_id: null,
    decay_state: 'hot',
    ai_confidence: 0.61,
    ai_confidence_band: 'medium',
    ai_degraded: false,
    risk_floor_applied: false,
    ai_session_id: 'sess-1',
    ai_model_used: 'anthropic:claude-sonnet-4-5',
    ai_redaction_count: 4,
    comment_count: 0,
    open_comment_count: 0,
    highlight_count: 0,
    editable_by_me: true,
    ...overrides,
  }
}

function renderCard(entry) {
  return render(
    <ul>
      <EntryCard
        entry={entry}
        users={[]}
        canComment={false}
        canHighlight={false}
        canRestore={false}
        patientId="patient-a1"
        onChanged={() => {}}
        registerRef={() => {}}
      />
    </ul>,
  )
}

describe('degraded AI summaries', () => {
  it('says plainly that the summary was written without the model', () => {
    renderCard(
      aiEntry({
        ai_degraded: true,
        ai_model_used: 'offline-extractive-v1:provider-unavailable',
      }),
    )
    expect(screen.getByText(/written without the AI/i)).toBeTruthy()
  })

  it('does not label a healthy summary as degraded', () => {
    renderCard(aiEntry())
    expect(screen.queryByText(/written without the AI/i)).toBeNull()
  })

  it('keeps the summary readable rather than replacing it with an error', () => {
    renderCard(aiEntry({ ai_degraded: true }))
    expect(screen.getByText(/review and plan captured/i)).toBeTruthy()
  })

  it('separates degradation from low confidence', () => {
    // A degraded note here carries a *medium* confidence. If the two signals
    // were collapsed, this card would be silent about the outage.
    renderCard(aiEntry({ ai_degraded: true, ai_confidence: 0.61, ai_confidence_band: 'medium' }))
    expect(screen.getByText(/written without the AI/i)).toBeTruthy()
  })

  it('still marks the entry as AI-scribed, because it is', () => {
    renderCard(aiEntry({ ai_degraded: true }))
    // The degraded chip is an addition to the machine-authorship signals, not
    // a replacement for them — a rule-extracted summary is still not a person's
    // note, and the card must not start reading as one.
    expect(screen.getByText('◇ AI scribed')).toBeTruthy()
  })
})
