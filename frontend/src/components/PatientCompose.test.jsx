import { describe, it, expect, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PatientHome from './PatientHome'
import { WRITABLE_TYPES } from '../App'

function care(overrides = {}) {
  return {
    patient: { id: 'patient-a1', name: 'Amira Rahman' },
    labels: {},
    next_steps: [],
    updates: [],
    your_notes: [],
    corrections: [],
    new_since_last_visit: [],
    since: null,
    generated_at: new Date().toISOString(),
    ...overrides,
  }
}

describe('a patient writing to her care team', () => {
  it('has somewhere to write, which is what the section already promised', async () => {
    // The copy said "anything you share before an appointment goes to your care
    // team". The notes rendered and nothing could create one, so the promise was
    // unkeepable through the interface.
    const onAddNote = vi.fn().mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(<PatientHome care={care()} onAddNote={onAddNote} />)

    await user.type(
      screen.getByLabelText(/write something for your care team/i),
      'The evening tablet upsets my stomach.'
    )
    await user.click(screen.getByRole('button', { name: /send to my care team/i }))

    await waitFor(() => expect(onAddNote).toHaveBeenCalledTimes(1))
    expect(onAddNote).toHaveBeenCalledWith('The evening tablet upsets my stomach.')
  })

  it('clears the box on success so she does not send it twice', async () => {
    const user = userEvent.setup()
    render(<PatientHome care={care()} onAddNote={vi.fn().mockResolvedValue(undefined)} />)
    const box = screen.getByLabelText(/write something for your care team/i)

    await user.type(box, 'Feet tingling.')
    await user.click(screen.getByRole('button', { name: /send to my care team/i }))

    await waitFor(() => expect(box.value).toBe(''))
  })

  it('keeps her words when the save fails', async () => {
    // Losing what she typed is worse than showing an error: she is the one
    // reader here with no second chance to remember it before the appointment.
    const user = userEvent.setup()
    render(
      <PatientHome care={care()} onAddNote={vi.fn().mockRejectedValue(new Error('offline'))} />
    )
    const box = screen.getByLabelText(/write something for your care team/i)

    await user.type(box, 'Dizzy in the mornings.')
    await user.click(screen.getByRole('button', { name: /send to my care team/i }))

    // She is told, rather than left with a button that quietly re-enables —
    // which reads as "sent".
    expect(await screen.findByText(/did not send/i)).toBeTruthy()
    expect(box.value).toBe('Dizzy in the mornings.')
  })

  it('will not send an empty note', () => {
    render(<PatientHome care={care()} onAddNote={vi.fn()} />)
    expect(screen.getByRole('button', { name: /send to my care team/i }).disabled).toBe(true)
  })
})

describe('what each role may compose', () => {
  it('offers a clinician the patient-facing types, not just their own section', () => {
    // The single-type version left three surfaces dark at once: nothing written
    // for the patient could be authored, her view had nothing new to show, and
    // the dosage gate fires only on patient-facing types so it was unreachable.
    const types = WRITABLE_TYPES.clinician.map((entry) => entry.type)
    expect(types).toContain('clinician_section')
    expect(types).toContain('patient_instruction')
    expect(types).toContain('patient_summary')
  })

  it('marks exactly the patient-facing types as such', () => {
    const flagged = WRITABLE_TYPES.clinician
      .filter((entry) => entry.patientFacing)
      .map((entry) => entry.type)
      .sort()
    expect(flagged).toEqual(['patient_instruction', 'patient_summary'])
  })

  it('does not offer staff or patients anything the server would refuse', () => {
    // Mirrors security/policy.WRITABLE_TYPES. The server is the enforcement;
    // this asserts the picker cannot offer a button that always fails.
    expect(WRITABLE_TYPES.staff.map((e) => e.type)).toEqual(['staff_note'])
    expect(WRITABLE_TYPES.patient.map((e) => e.type)).toEqual(['patient_note'])
    expect(WRITABLE_TYPES.admin).toBeUndefined()
  })
})
