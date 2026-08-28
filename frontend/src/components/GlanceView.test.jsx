/**
 * The Glance View's action controls.
 *
 * These cover the three client-side defects fixed in the Phase 7 pass, all of
 * which were invisible to the API tests because the endpoints they call were
 * always correct — the bug was that nothing called them, or that local state
 * outlived the answer.
 *
 * `Api` is mocked rather than stubbed through fetch. What is under test here is
 * the component's contract with the client wrapper — that Mark done sends
 * `done` for the right task id, that a decision triggers a reload — not the
 * wrapper's contract with the server, which `tests/` already covers end to end
 * against the real app.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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

import { Api } from '../lib/api'
import GlanceView from './GlanceView'

const NOW = '2026-08-28T09:00:00+00:00'

function glanceFixture(overrides = {}) {
  return {
    patient: { id: 'patient-a1', name: 'Amira Rahman', mrn: 'MRN-A-40192', dob: '1968-03-11' },
    generated_at: NOW,
    since: '2026-08-28T08:00:00+00:00',
    whats_new: { since: '2026-08-28T08:00:00+00:00', count: 0, entries: [], first_visit: false },
    highlights: [],
    open_actions: [],
    risk_flags: [],
    confidence_flags: [],
    conflicts: [],
    counts: { entries: 8, ai_scribed: 1, open_tasks: 0 },
    ...overrides,
  }
}

function task(overrides = {}) {
  return {
    kind: 'task',
    id: 'task-1',
    description: 'book monofilament testing',
    status: 'open',
    assigned_to: 'u-a-staff',
    assigned_to_name: 'Nurse Priya Nair',
    assigned_to_role: 'staff',
    entry_id: 'entry-a1-clin',
    due_at: null,
    created_at: '2026-08-28T08:30:00+00:00',
    ...overrides,
  }
}

function highlight(overrides = {}) {
  return {
    id: 'hl-1',
    entry_id: 'entry-a1-clin',
    span_start: 0,
    span_end: 40,
    span_text: 'T2DM with suboptimal control. HbA1c 8.4%',
    risk_reason: 'Medication mentioned; unresolved action',
    provenance_pointer: 'entry://entry-a1-clin#span:0-40',
    status: 'suggested',
    score: 0.62,
    score_breakdown: { recency: 0.28, risk: 0.16, entities: 0.15 },
    feature_tags: ['med:metformin'],
    created_by_role: 'system',
    is_manual: false,
    stale: false,
    source_version_number: 1,
    entry_type: 'clinician_section',
    entry_title: 'Assessment',
    entry_timestamp: '2026-08-28T08:30:00+00:00',
    entry_author_role: 'clinician',
    is_ai_scribed: false,
    ai_confidence: null,
    can_decide: true,
    ...overrides,
  }
}

function renderGlance(glance, props = {}) {
  return render(
    <GlanceView
      glance={glance}
      timing={{ serverMs: 12, clientMs: 30 }}
      onJumpTo={props.onJumpTo || vi.fn()}
      onChanged={props.onChanged || vi.fn()}
      canDecide={props.canDecide ?? true}
    />
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('open actions — closing a task', () => {
  /**
   * The endpoint and the client wrapper both existed from Phase 2.5 and nothing
   * called either, so a task could be raised and never finished. That also fed
   * a wrong open-task count into the ranking on every write.
   */

  it('offers Mark done and Cancel on a task row', () => {
    renderGlance(glanceFixture({ open_actions: [task()] }))

    expect(screen.getByRole('button', { name: /mark done/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /^cancel$/i })).toBeTruthy()
  })

  it('sends done for the right task and reloads the card', async () => {
    const onChanged = vi.fn()
    renderGlance(glanceFixture({ open_actions: [task({ id: 'task-42' })] }), { onChanged })

    await userEvent.click(screen.getByRole('button', { name: /mark done/i }))

    await waitFor(() => expect(Api.setTaskStatus).toHaveBeenCalledWith('task-42', 'done'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('sends cancelled rather than deleting', async () => {
    renderGlance(glanceFixture({ open_actions: [task({ id: 'task-42' })] }))

    await userEvent.click(screen.getByRole('button', { name: /^cancel$/i }))

    await waitFor(() =>
      expect(Api.setTaskStatus).toHaveBeenCalledWith('task-42', 'cancelled')
    )
  })

  it('offers no status controls on a comment thread', () => {
    const thread = {
      kind: 'comment',
      id: 'c-1',
      description: 'Please chase the ACR result.',
      status: 'open',
      assigned_to: null,
      assigned_to_name: 'the team',
      assigned_to_role: null,
      entry_id: 'entry-a1-clin',
      due_at: null,
      created_at: '2026-08-28T08:45:00+00:00',
      author_name: 'Dr Lim Wei Sheng',
      author_role: 'clinician',
    }
    renderGlance(glanceFixture({ open_actions: [thread] }))

    expect(screen.queryByRole('button', { name: /mark done/i })).toBeNull()
  })

  it('surfaces a failure instead of silently doing nothing', async () => {
    Api.setTaskStatus.mockRejectedValueOnce(new Error('Task not found'))
    renderGlance(glanceFixture({ open_actions: [task()] }))

    await userEvent.click(screen.getByRole('button', { name: /mark done/i }))

    expect(await screen.findByText(/task not found/i)).toBeTruthy()
  })
})

describe('highlight decisions — optimistic state must not outlive the answer', () => {
  /**
   * `decided` was keyed by highlight id and never cleared, so after the reload
   * it left a "Confirmed" pill attached to an id the server had already
   * answered for — and, once suggestions were regenerated, to nothing at all.
   */

  it('confirms a suggestion and reloads', async () => {
    const onChanged = vi.fn()
    renderGlance(glanceFixture({ highlights: [highlight()] }), { onChanged })

    await userEvent.click(screen.getByRole('button', { name: /confirm/i }))

    await waitFor(() => expect(Api.acceptHighlight).toHaveBeenCalledWith('hl-1'))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('clears the local decision when a new payload arrives', async () => {
    const { rerender } = renderGlance(glanceFixture({ highlights: [highlight()] }))

    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(await screen.findByText(/^dismissed$/i)).toBeTruthy()

    // The reload lands: same highlight, still suggested per the server.
    rerender(
      <GlanceView
        glance={glanceFixture({
          generated_at: '2026-08-28T09:00:05+00:00',
          highlights: [highlight()],
        })}
        timing={{ serverMs: 12, clientMs: 30 }}
        onJumpTo={vi.fn()}
        onChanged={vi.fn()}
        canDecide
      />
    )

    expect(screen.queryByText(/^dismissed$/i)).toBeNull()
    expect(screen.getByRole('button', { name: /confirm/i })).toBeTruthy()
  })

  it('shows no decide controls to a role that may not decide', () => {
    renderGlance(glanceFixture({ highlights: [highlight()] }), { canDecide: false })

    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /dismiss/i })).toBeNull()
  })
})

describe("what's new — the count must match the rows under it", () => {
  const entry = (id) => ({
    id,
    type: 'staff_note',
    author_role: 'staff',
    author_id: 'u-a-staff',
    timestamp: '2026-08-28T08:45:00+00:00',
    title: `Note ${id}`,
    preview: 'BP 138/86 seated.',
    risk_level: 'none',
    risk_label: 'No risk flag',
    is_ai_scribed: false,
    confidence: null,
    version_number: 1,
  })

  it('says how many more are in the timeline when the list is truncated', () => {
    const entries = ['e1', 'e2', 'e3'].map(entry)
    renderGlance(
      glanceFixture({
        whats_new: { since: NOW, count: 12, entries, first_visit: false },
      })
    )

    expect(screen.getByText(/and 9 more in the timeline below/i)).toBeTruthy()
  })

  it('says nothing extra when the list is complete', () => {
    const entries = ['e1', 'e2'].map(entry)
    renderGlance(
      glanceFixture({
        whats_new: { since: NOW, count: 2, entries, first_visit: false },
      })
    )

    expect(screen.queryByText(/more in the timeline below/i)).toBeNull()
  })

  it('distinguishes a first visit from a quiet chart', () => {
    const { unmount } = renderGlance(
      glanceFixture({
        whats_new: { since: null, count: 0, entries: [], first_visit: true },
      })
    )
    expect(screen.getByText(/first look at this chart/i)).toBeTruthy()
    unmount()

    renderGlance(
      glanceFixture({
        whats_new: { since: NOW, count: 0, entries: [], first_visit: false },
      })
    )
    expect(screen.getByText(/no new entries since you were last here/i)).toBeTruthy()
  })
})

describe('provenance click-through', () => {
  it('jumps to the exact span a highlight was drawn from', async () => {
    const onJumpTo = vi.fn()
    const hl = highlight()
    renderGlance(glanceFixture({ highlights: [hl] }), { onJumpTo })

    await userEvent.click(screen.getByText(hl.span_text))

    expect(onJumpTo).toHaveBeenCalledWith('entry-a1-clin', hl)
  })

  it('jumps to the entry, without a span, from a risk flag', async () => {
    const onJumpTo = vi.fn()
    renderGlance(
      glanceFixture({
        risk_flags: [
          {
            entry_id: 'entry-a1-clin',
            level: 'high',
            label: 'High risk',
            entry_type: 'clinician_section',
            title: 'Assessment',
            timestamp: '2026-08-28T08:30:00+00:00',
            is_ai_scribed: false,
          },
        ],
      }),
      { onJumpTo }
    )

    await userEvent.click(screen.getByText('Assessment'))

    expect(onJumpTo).toHaveBeenCalledWith('entry-a1-clin')
  })
})
