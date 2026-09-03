import { useState } from 'react'
import { Api, ApiError } from '../lib/api'
import { Button, Chip, SectionTitle } from './Primitives'

/**
 * Front-desk operations on the patient roster: enrol someone, and look one up
 * by id.
 *
 * Both existed as API routes with no screen, which meant the two controls a
 * reviewer most wants to watch refuse something could only be demonstrated with
 * curl. That is a fair demonstration of a server-side check and a poor
 * demonstration of a product, so they are here now.
 *
 * **Lookup does not touch the address bar.** It would be one line to read a
 * patient id from `?patient=`, and it would put patient ids in browser history,
 * in referrer headers, and on the screen of every shared consult-room laptop.
 * D-083 is the record of us leaking a phone number through a URL and only
 * finding it in the access log afterwards; re-introducing ids to a URL for demo
 * convenience would be that lesson unlearned. The id goes in a form field and
 * out in a request path, which is where the allowlist in `test_url_surface.py`
 * already permits an opaque id to appear.
 *
 * See DECISIONS.md D-104.
 */
export default function PatientAdmin({ session, onEnrolled, onFound }) {
  const canEnrol = ['staff', 'clinician', 'admin'].includes(session.role)

  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [identifierType, setIdentifierType] = useState('phone')
  const [identifier, setIdentifier] = useState('')
  const [dob, setDob] = useState('')
  const [busy, setBusy] = useState(false)
  const [enrolError, setEnrolError] = useState(null)
  const [issued, setIssued] = useState(null)

  const [lookupId, setLookupId] = useState('')
  const [lookupBusy, setLookupBusy] = useState(false)
  const [lookupResult, setLookupResult] = useState(null)
  const [lookupError, setLookupError] = useState(null)

  if (!canEnrol) return null

  async function enrol(event) {
    event.preventDefault()
    if (!name.trim() || busy) return
    setBusy(true)
    setEnrolError(null)
    setIssued(null)
    try {
      const created = await Api.enrolPatient({
        name: name.trim(),
        dob: dob.trim() || null,
        identifier_type: identifierType,
        identifier: identifier.trim() || null,
        create_login: true,
      })
      setIssued(created)
      setName('')
      setIdentifier('')
      setDob('')
      onEnrolled?.(created)
    } catch (err) {
      setEnrolError(
        err instanceof ApiError && err.status === 409
          ? 'That identifier is already registered to a patient in this clinic.'
          : err.message
      )
    } finally {
      setBusy(false)
    }
  }

  async function lookup(event) {
    event.preventDefault()
    const id = lookupId.trim()
    if (!id || lookupBusy) return
    setLookupBusy(true)
    setLookupError(null)
    setLookupResult(null)
    try {
      const patient = await Api.patient(id)
      setLookupResult(patient)
      onFound?.(patient)
    } catch (err) {
      // 404 is the interesting answer, and it is the same answer whether the id
      // is nonsense or belongs to another clinic — a clinician here is not told
      // that a patient there exists.
      setLookupError(
        err instanceof ApiError && err.status === 404
          ? 'Patient not found in this clinic.'
          : err.message
      )
    } finally {
      setLookupBusy(false)
    }
  }

  return (
    <section className="mt-3 rounded border border-slate-300 bg-white">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold text-slate-700 hover:bg-slate-50"
        aria-expanded={open}
      >
        <span>{open ? '▾' : '▸'}</span>
        Front desk
        <span className="font-normal text-slate-500">
          register a patient · look one up by id
        </span>
      </button>

      {open && (
        <div className="grid gap-4 border-t border-slate-200 px-3 py-3 sm:grid-cols-2">
          <form onSubmit={enrol} className="space-y-2">
            <SectionTitle hint="A phone number is enough. Name is the only required field.">
              Register a patient
            </SectionTitle>

            <label className="block text-[11px] font-medium text-slate-600">
              Full name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Siti Rahman"
                className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 text-sm"
              />
            </label>

            <div className="flex gap-2">
              <label className="block text-[11px] font-medium text-slate-600">
                Identifier
                <select
                  value={identifierType}
                  onChange={(e) => setIdentifierType(e.target.value)}
                  className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 text-sm"
                >
                  <option value="phone">Phone</option>
                  <option value="nric">NRIC / IC</option>
                  <option value="mrn">Clinic MRN</option>
                  <option value="internal">None (walk-in)</option>
                </select>
              </label>
              <label className="block flex-1 text-[11px] font-medium text-slate-600">
                Value
                <input
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  disabled={identifierType === 'internal'}
                  placeholder="0198887777"
                  className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 text-sm disabled:bg-slate-100"
                />
              </label>
            </div>

            <label className="block text-[11px] font-medium text-slate-600">
              Date of birth <span className="font-normal text-slate-400">optional</span>
              <input
                value={dob}
                onChange={(e) => setDob(e.target.value)}
                placeholder="1968-03-11"
                className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 text-sm"
              />
            </label>

            <Button disabled={busy || !name.trim()}>
              {busy ? 'Registering…' : 'Register and issue login'}
            </Button>

            {enrolError && (
              <p className="rounded border border-rose-200 bg-rose-50 px-2 py-1 text-xs text-rose-800">
                {enrolError}
              </p>
            )}

            {issued && (
              <div className="rounded border border-emerald-300 bg-emerald-50 px-2 py-2 text-xs">
                <p className="font-semibold text-emerald-900">{issued.name} registered.</p>
                <p className="mt-1 text-emerald-900">
                  Username <span className="font-mono">{issued.username}</span>
                </p>
                <p className="mt-1 text-emerald-900">
                  One-time passcode{' '}
                  <span className="font-mono text-sm font-bold">
                    {issued.one_time_passcode}
                  </span>
                </p>
                {/* The server returns this exactly once and stores only its hash.
                    Saying so here is the difference between a staff member
                    writing it down and one assuming they can look it up. */}
                <p className="mt-1 text-[11px] text-emerald-800">
                  Shown once. It is not stored and cannot be retrieved — if it is
                  lost, issue a new one.
                </p>
                {!issued.reachable && (
                  <p className="mt-1 text-[11px] text-amber-800">
                    No contact identifier, so nothing can be sent to her. She can
                    still sign in with the passcode.
                  </p>
                )}
              </div>
            )}
          </form>

          <form onSubmit={lookup} className="space-y-2">
            <SectionTitle hint="Scoped to your clinic, server-side. Not a search across clinics.">
              Look up by patient id
            </SectionTitle>

            <label className="block text-[11px] font-medium text-slate-600">
              Patient id
              <input
                value={lookupId}
                onChange={(e) => setLookupId(e.target.value)}
                placeholder="patient-a1"
                className="mt-0.5 w-full rounded border border-slate-300 px-2 py-1 font-mono text-sm"
              />
            </label>

            <Button variant="quiet" disabled={lookupBusy || !lookupId.trim()}>
              {lookupBusy ? 'Looking up…' : 'Look up'}
            </Button>

            {lookupError && (
              <div className="rounded border border-rose-200 bg-rose-50 px-2 py-2 text-xs text-rose-900">
                <p className="font-semibold">{lookupError}</p>
                <p className="mt-1 text-[11px]">
                  The same answer is given for an id that does not exist and one
                  that belongs to another clinic. Refused by the server, not
                  hidden by this screen.
                </p>
              </div>
            )}

            {lookupResult && (
              <div className="rounded border border-slate-300 bg-slate-50 px-2 py-2 text-xs">
                <p className="font-semibold text-slate-900">{lookupResult.name}</p>
                <p className="mt-0.5 text-slate-600">
                  <Chip>{lookupResult.mrn || 'no MRN'}</Chip>{' '}
                  <span className="font-mono text-[10px]">{lookupResult.id}</span>
                </p>
              </div>
            )}
          </form>
        </div>
      )}
    </section>
  )
}
