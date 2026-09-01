/**
 * The two gates, as a clinician meets them.
 *
 * Both refusals are server-side and correct (D-078, D-079). What these cover is
 * that the refusal arrives as a *decision* rather than as an error string — a
 * gate a clinician cannot act on is a dead end, and a dead end is what teaches
 * people to route around a check.
 */

import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import DosageConfirm from './DosageConfirm'

const DETAIL = {
  reason: 'dosage_needs_confirmation',
  message: 'This is going to the patient and contains a dose far outside the usual adult range.',
  findings: [
    {
      drug: 'metformin',
      stated: '5000mg',
      expected_low_mg: 250,
      expected_high_mg: 1000,
      message: '5000mg of metformin is far outside the usual adult range (250–1000 mg).',
    },
  ],
}

describe('dosage confirmation', () => {
  it('names the drug, the figure written and the expected range', () => {
    render(<DosageConfirm detail={DETAIL} onConfirm={() => {}} onCancel={() => {}} />)
    // Enough to tell a typo from a decision in one read.
    expect(screen.getByText(/5000mg/)).toBeTruthy()
    expect(screen.getByText(/metformin/)).toBeTruthy()
    expect(screen.getByText(/250–1000 mg/)).toBeTruthy()
  })

  it('says plainly that nothing was saved', () => {
    render(<DosageConfirm detail={DETAIL} onConfirm={() => {}} onCancel={() => {}} />)
    expect(screen.getByText(/Nothing has been saved/)).toBeTruthy()
  })

  it('warns that confirming is recorded', () => {
    // Most of what makes someone stop and reread is knowing the override is
    // attributable to them.
    render(<DosageConfirm detail={DETAIL} onConfirm={() => {}} onCancel={() => {}} />)
    expect(screen.getByText(/confirmation is recorded/)).toBeTruthy()
  })

  it('offers going back as well as confirming', () => {
    render(<DosageConfirm detail={DETAIL} onConfirm={() => {}} onCancel={() => {}} />)
    expect(screen.getByRole('button', { name: /go back and check/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /confirm and save/i })).toBeTruthy()
  })

  it('does not confirm on its own', async () => {
    const onConfirm = vi.fn()
    const onCancel = vi.fn()
    render(<DosageConfirm detail={DETAIL} onConfirm={onConfirm} onCancel={onCancel} />)

    await userEvent.click(screen.getByRole('button', { name: /go back and check/i }))
    expect(onConfirm).not.toHaveBeenCalled()
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('confirms only when the clinician says so', async () => {
    const onConfirm = vi.fn()
    render(<DosageConfirm detail={DETAIL} onConfirm={onConfirm} onCancel={() => {}} />)

    await userEvent.click(screen.getByRole('button', { name: /confirm and save/i }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('disables both actions while saving', () => {
    render(<DosageConfirm detail={DETAIL} onConfirm={() => {}} onCancel={() => {}} busy />)
    expect(screen.getByRole('button', { name: /go back and check/i }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: /saving/i }).disabled).toBe(true)
  })

  it('renders every finding when a note carries more than one', () => {
    render(
      <DosageConfirm
        detail={{
          ...DETAIL,
          findings: [
            DETAIL.findings[0],
            {
              drug: 'warfarin',
              stated: '500mg',
              expected_low_mg: 0.5,
              expected_high_mg: 15,
              message: 'far outside range',
            },
          ],
        }}
        onConfirm={() => {}}
        onCancel={() => {}}
      />
    )
    expect(screen.getByText(/metformin/)).toBeTruthy()
    expect(screen.getByText(/warfarin/)).toBeTruthy()
  })
})
