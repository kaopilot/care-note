import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PatientAdmin from './PatientAdmin'
import { Api, ApiError } from '../lib/api'

vi.mock('../lib/api', async () => {
  const actual = await vi.importActual('../lib/api')
  return {
    ...actual,
    Api: { enrolPatient: vi.fn(), patient: vi.fn() },
  }
})

const staff = { role: 'staff', name: 'Nurse A', clinic_name: 'Clinic A' }

async function open(user) {
  await user.click(screen.getByRole('button', { name: /front desk/i }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('registering a patient', () => {
  it('is hidden from a patient, who has no roster to manage', () => {
    const { container } = render(
      <PatientAdmin session={{ role: 'patient', name: 'Amira' }} />
    )
    expect(container.firstChild).toBeNull()
  })

  it('sends a phone identifier and shows the passcode exactly once', async () => {
    const user = userEvent.setup()
    Api.enrolPatient.mockResolvedValue({
      patient_id: 'p-new',
      name: 'Siti Rahman',
      username: '0198887777',
      one_time_passcode: '974562',
      reachable: true,
    })
    const onEnrolled = vi.fn()
    render(<PatientAdmin session={staff} onEnrolled={onEnrolled} />)
    await open(user)

    await user.type(screen.getByLabelText(/full name/i), 'Siti Rahman')
    await user.type(screen.getByLabelText(/^value$/i), '0198887777')
    await user.click(screen.getByRole('button', { name: /register and issue login/i }))

    await waitFor(() => expect(Api.enrolPatient).toHaveBeenCalledTimes(1))
    expect(Api.enrolPatient.mock.calls[0][0]).toMatchObject({
      name: 'Siti Rahman',
      identifier_type: 'phone',
      identifier: '0198887777',
      create_login: true,
    })
    expect(screen.getByText('974562')).toBeTruthy()
    // The staff member has to know this will not be here later, or they will
    // assume they can look it up and she will be locked out.
    expect(screen.getByText(/shown once/i)).toBeTruthy()
    expect(onEnrolled).toHaveBeenCalled()
  })

  it('does not require a date of birth', async () => {
    const user = userEvent.setup()
    Api.enrolPatient.mockResolvedValue({
      patient_id: 'p2', name: 'Walk In', username: 'p2',
      one_time_passcode: '111111', reachable: false,
    })
    render(<PatientAdmin session={staff} />)
    await open(user)
    await user.type(screen.getByLabelText(/full name/i), 'Walk In')
    await user.click(screen.getByRole('button', { name: /register and issue login/i }))

    await waitFor(() => expect(Api.enrolPatient).toHaveBeenCalled())
    expect(Api.enrolPatient.mock.calls[0][0].dob).toBeNull()
  })

  it('says who cannot be contacted rather than leaving it implied', async () => {
    const user = userEvent.setup()
    Api.enrolPatient.mockResolvedValue({
      patient_id: 'p3', name: 'Walk In', username: 'p3',
      one_time_passcode: '222222', reachable: false,
    })
    render(<PatientAdmin session={staff} />)
    await open(user)
    await user.type(screen.getByLabelText(/full name/i), 'Walk In')
    await user.click(screen.getByRole('button', { name: /register and issue login/i }))

    expect(await screen.findByText(/nothing can be sent to her/i)).toBeTruthy()
  })

  it('explains a duplicate identifier instead of showing a status code', async () => {
    const user = userEvent.setup()
    Api.enrolPatient.mockRejectedValue(
      new ApiError('That identifier is already registered.', 409, null)
    )
    render(<PatientAdmin session={staff} />)
    await open(user)
    await user.type(screen.getByLabelText(/full name/i), 'Siti Rahman')
    await user.type(screen.getByLabelText(/^value$/i), '0198887777')
    await user.click(screen.getByRole('button', { name: /register and issue login/i }))

    expect(await screen.findByText(/already registered to a patient/i)).toBeTruthy()
    expect(screen.queryByText(/409/)).toBeNull()
  })
})

describe('looking up a patient by id', () => {
  it('opens the record when the patient is in this clinic', async () => {
    const user = userEvent.setup()
    Api.patient.mockResolvedValue({ id: 'patient-a1', name: 'Amira Rahman', mrn: 'A-001' })
    const onFound = vi.fn()
    render(<PatientAdmin session={staff} onFound={onFound} />)
    await open(user)

    await user.type(screen.getByLabelText(/patient id/i), 'patient-a1')
    await user.click(screen.getByRole('button', { name: /look up/i }))

    expect(await screen.findByText('Amira Rahman')).toBeTruthy()
    expect(onFound).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'patient-a1' })
    )
  })

  it("refuses another clinic's patient with the same words as a bad id", async () => {
    const user = userEvent.setup()
    Api.patient.mockRejectedValue(new ApiError('Patient not found', 404, null))
    const onFound = vi.fn()
    render(<PatientAdmin session={staff} onFound={onFound} />)
    await open(user)

    await user.type(screen.getByLabelText(/patient id/i), 'patient-b1')
    await user.click(screen.getByRole('button', { name: /look up/i }))

    expect(await screen.findByText(/not found in this clinic/i)).toBeTruthy()
    // Existence is the thing being protected. If the copy distinguished
    // "wrong clinic" from "no such patient", this screen would confirm that a
    // patient exists in Clinic B — which is what the 404 is avoiding.
    expect(screen.getByText(/same answer is given/i)).toBeTruthy()
    expect(onFound).not.toHaveBeenCalled()
    expect(screen.queryByText(/patient-b1.*exists/i)).toBeNull()
  })

  it('gives a nonsense id the identical message, character for character', async () => {
    // The claim in D-104 is that the two answers do not differ. Asserting the
    // wrong-clinic message alone would leave that untested: the copy could
    // diverge for a bad id and this file would stay green while the interface
    // started confirming which patients exist elsewhere.
    async function messageFor(id) {
      Api.patient.mockRejectedValue(new ApiError('Patient not found', 404, null))
      const view = render(<PatientAdmin session={staff} onFound={vi.fn()} />)
      const user = userEvent.setup()
      await user.click(view.getByRole('button', { name: /front desk/i }))
      await user.type(view.getByLabelText(/patient id/i), id)
      await user.click(view.getByRole('button', { name: /look up/i }))
      const node = await view.findByText(/not found in this clinic/i)
      const text = node.parentElement.textContent
      view.unmount()
      return text
    }

    expect(await messageFor('patient-b1')).toBe(await messageFor('no-such-patient'))
  })

  it('never puts a patient id in the address bar', async () => {
    const user = userEvent.setup()
    Api.patient.mockResolvedValue({ id: 'patient-a1', name: 'Amira Rahman', mrn: 'A-001' })
    const before = window.location.href
    render(<PatientAdmin session={staff} />)
    await open(user)
    await user.type(screen.getByLabelText(/patient id/i), 'patient-a1')
    await user.click(screen.getByRole('button', { name: /look up/i }))

    await waitFor(() => expect(Api.patient).toHaveBeenCalled())
    // D-083 was a phone number reaching the access log through a URL. An opaque
    // id is less sensitive and browser history is still the wrong place for it.
    expect(window.location.href).toBe(before)
    expect(window.location.search).not.toContain('patient')
  })
})
