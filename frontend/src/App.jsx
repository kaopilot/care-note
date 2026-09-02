/**
 * Care Note — application shell.
 *
 * Holds three things and delegates the rest: who is signed in, which patient is
 * open, and where a provenance click should land.
 *
 * Provenance click-through is the piece worth reading. Clicking a highlight
 * sets `emphasis` to the entry id plus the character span the highlight was
 * drawn from, scrolls that entry into view, and the entry renders its content
 * with that exact range marked. Not "jumps to the note" — jumps to the words.
 * That is the difference between a citation and a gesture at one.
 *
 * What this page draws is never the access boundary. The server has already
 * filtered the timeline, the Glance View and the comment threads to what this
 * role may read; hiding things here as well would be theatre, and the tests
 * that matter call the API directly.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ErrorBoundary from './components/ErrorBoundary'
import { Api, ApiError } from './lib/api'
import DosageConfirm from './components/DosageConfirm'
import GlanceView from './components/GlanceView'
import PatientHome from './components/PatientHome'
import Timeline from './components/Timeline'
import VoiceCapture from './components/VoiceCapture'
import { Button, Chip } from './components/Primitives'
import { roleLabel } from './lib/format'

const WRITABLE_TYPE = {
  staff: { type: 'staff_note', label: 'staff note' },
  clinician: { type: 'clinician_section', label: 'clinician section' },
  patient: { type: 'patient_note', label: 'note for your care team' },
}

const SCRIBE_LABEL = {
  doctor_patient_consult: 'Doctor consult',
  nurse_patient_consult: 'Nurse consult',
  ai_patient_session: 'AI patient session',
}

function LoginForm({ onLoggedIn }) {
  const [username, setUsername] = useState('clinician_a')
  const [password, setPassword] = useState('carenote-demo')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      // The response body carries an access_token for non-browser clients.
      // This client ignores it — the httpOnly cookie is the transport (D-016).
      await Api.login(username, password)
      onLoggedIn(await Api.me())
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      onSubmit={submit}
      className="mx-auto mt-10 max-w-sm rounded-lg border border-slate-300 bg-white p-5"
    >
      <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-slate-500">
        Sign in
      </h2>
      <input
        className="mt-3 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
        value={username}
        onChange={(event) => setUsername(event.target.value)}
        placeholder="username"
        autoComplete="username"
      />
      <input
        className="mt-2 w-full rounded border border-slate-300 px-2 py-1.5 text-sm"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        placeholder="password"
        autoComplete="current-password"
      />
      <Button variant="primary" className="mt-3 w-full py-1.5" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </Button>
      {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}
      <p className="mt-3 text-xs leading-relaxed text-slate-500">
        Seeded accounts: clinician_a · staff_a · admin_a · patient_a, and the same four
        with _b for the second clinic. Password carenote-demo. All data is synthetic.
      </p>
    </form>
  )
}

function Workspace({ session, onSignOut }) {
  const [patients, setPatients] = useState([])
  const [selected, setSelected] = useState(null)
  const [entries, setEntries] = useState([])
  const [glance, setGlance] = useState(null)
  const [care, setCare] = useState(null)
  const [timing, setTiming] = useState(null)
  const [users, setUsers] = useState([])
  const [emphasis, setEmphasis] = useState(null)
  const [processing, setProcessing] = useState(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState(null)
  const entryRefs = useRef({})

  const isPatient = session.role === 'patient'
  const isClinical = ['staff', 'clinician', 'admin'].includes(session.role)
  const writable = WRITABLE_TYPE[session.role]

  useEffect(() => {
    Api.patients()
      .then((rows) => {
        setPatients(rows)
        if (rows.length) setSelected(rows[0].id)
      })
      .catch((err) => setError(err.message))
    if (isClinical) Api.clinicUsers().then(setUsers).catch(() => setUsers([]))
  }, [isClinical])

  const load = useCallback(
    async (patientId) => {
      if (!patientId) return
      try {
        if (isPatient) {
          const { data, clientMs, serverMs } = await Api.myCare(patientId)
          setCare(data)
          setTiming({ clientMs, serverMs })
        } else {
          // Glance first: it is the surface with a latency budget, and timing
          // it alongside the timeline fetch would measure the wrong thing.
          const { data, clientMs, serverMs } = await Api.glance(patientId)
          setGlance(data)
          setTiming({ clientMs, serverMs })
          // The patient view does not render a timeline, so it does not pay for
          // one. Fetching entries there would be a second round trip whose
          // result is thrown away on the view most likely to be opened on a
          // phone, on mobile data.
          setEntries(await Api.entries(patientId))
        }
        setError(null)
      } catch (err) {
        setError(err.message)
      }
    },
    [isPatient]
  )

  useEffect(() => {
    if (selected) load(selected)
  }, [selected, load])

  const registerRef = useCallback(
    (entryId) => (element) => {
      entryRefs.current[entryId] = element
    },
    []
  )

  /** Land on the words, not just the note. */
  const jumpTo = useCallback((entryId, highlight) => {
    setEmphasis(
      highlight
        ? { entryId, start: highlight.span_start, end: highlight.span_end }
        : { entryId, start: 0, end: 0 }
    )
    const element = entryRefs.current[entryId]
    if (element) element.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }, [])

  // The server refuses a patient-facing write carrying an implausible dose
  // (D-079). Holding the 409 detail here is what turns that refusal into a
  // decision the clinician can make, rather than an error they have to
  // interpret.
  const [dosageGate, setDosageGate] = useState(null)

  async function addEntry({ dosageConfirmed = false } = {}) {
    if (!draft.trim() || !writable) return
    try {
      await Api.createEntry(selected, {
        type: writable.type,
        content: draft,
        dosage_confirmed: dosageConfirmed,
      })
      setDosageGate(null)
      setDraft('')
      await load(selected)
    } catch (err) {
      if (err.status === 409 && err.detail?.reason === 'dosage_needs_confirmation') {
        // Not an error message. The draft is kept exactly as typed so the
        // clinician can correct the figure rather than retype the note.
        setDosageGate(err.detail)
        return
      }
      setError(err.message)
    }
  }

  // Scenario 8: the model hangs and a clinician is standing next to a patient.
  // The 8-second server timeout (D-070) bounds the wait; this gives them a way
  // out before it elapses. Aborting is safe because the transcript is written
  // before the model is called, so cancelling loses the summary and never the
  // consult.
  const scribeAbort = useRef(null)

  function cancelScribe() {
    scribeAbort.current?.abort()
  }

  async function runScribe(interactionType) {
    const controller = new AbortController()
    scribeAbort.current = controller
    setProcessing(`${SCRIBE_LABEL[interactionType]} — generating summary`)
    setError(null)
    try {
      await Api.runScribe(selected, interactionType, { signal: controller.signal })
      await load(selected)
    } catch (err) {
      if (err?.name === 'AbortError') {
        // Not an error state. They chose this, and the transcript is safe.
        setError('Summary cancelled. The transcript was saved — you can retry.')
      } else {
        setError(err instanceof ApiError ? err.message : String(err))
      }
    } finally {
      scribeAbort.current = null
      setProcessing(null)
    }
  }

  const patient = useMemo(
    () => patients.find((row) => row.id === selected),
    [patients, selected]
  )

  return (
    <div className="mx-auto max-w-6xl px-4 pb-16">
      <header className="flex flex-wrap items-center gap-3 border-b border-slate-300 py-3">
        <div>
          <h1 className="text-base font-semibold tracking-tight">Care Note</h1>
          <p className="text-[11px] text-slate-500">
            Shared longitudinal patient record · synthetic data only
          </p>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
          <span className="font-medium text-slate-800">{session.name}</span>
          <Chip>{roleLabel(session.role)}</Chip>
          <span className="text-slate-500">{session.clinic_name}</span>
          <Button variant="quiet" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </header>

      {patients.length > 1 && (
        <nav className="mt-3 flex flex-wrap gap-1">
          {patients.map((row) => (
            <button
              key={row.id}
              onClick={() => setSelected(row.id)}
              className={`rounded px-2 py-1 text-xs ring-1 transition ${
                selected === row.id
                  ? 'bg-slate-900 text-white ring-slate-900'
                  : 'bg-white text-slate-700 ring-slate-300 hover:bg-slate-100'
              }`}
            >
              {row.name} <span className="font-mono text-[10px] opacity-70">{row.mrn}</span>
            </button>
          ))}
        </nav>
      )}

      {error && (
        <p className="mt-3 rounded border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800">
          {error}
        </p>
      )}

      {isPatient ? (
        care && (
          <div className="mt-5">
            <PatientHome
              care={care}
              timing={timing}
              sessionBusy={Boolean(processing)}
              onRunSession={() => runScribe('ai_patient_session')}
              voiceCapture={
                <VoiceCapture
                  patientId={selected}
                  kind="patient"
                  disabled={Boolean(processing)}
                  onCaptured={() => load(selected)}
                />
              }
            />
          </div>
        )
      ) : (
        <>
          {glance && (
            <div className="mt-4">
              <GlanceView
                glance={glance}
                timing={timing}
                canDecide={session.role === 'clinician'}
                onJumpTo={jumpTo}
                onChanged={() => load(selected)}
              />
            </div>
          )}

          <Timeline
            entries={entries}
            processing={processing}
            onCancelProcessing={cancelScribe}
            patientId={selected}
            emphasis={emphasis}
            users={users}
            session={session}
            onChanged={() => load(selected)}
            registerRef={registerRef}
            composer={
              <div className="mt-3 grid gap-2 lg:grid-cols-2">
                {writable && (
                  <div className="rounded border border-slate-200 bg-white p-2">
                    <label className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
                      Add a {writable.label}
                    </label>
                    <textarea
                      className="mt-1 w-full rounded border border-slate-300 p-2 text-sm"
                      rows={2}
                      value={draft}
                      onChange={(event) => setDraft(event.target.value)}
                      placeholder="Plain text. Clinical notation such as BP <130/80 is stored exactly as written."
                    />
                    <Button
                      variant="primary"
                      disabled={!draft.trim()}
                      onClick={() => addEntry()}
                    >
                      Add to record
                    </Button>
                    {dosageGate && (
                      <DosageConfirm
                        detail={dosageGate}
                        onCancel={() => setDosageGate(null)}
                        onConfirm={() => addEntry({ dosageConfirmed: true })}
                      />
                    )}
                  </div>
                )}
                {session.role !== 'admin' && (
                  <div className="rounded border border-slate-200 bg-white p-2">
                    <label className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
                      Capture a consult
                    </label>
                    <p className="mt-1 text-[11px] text-slate-500">
                      Runs a synthetic transcript through redaction, then summarisation.
                      Identifiers are stripped before any text leaves the server.
                    </p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {Object.entries(SCRIBE_LABEL).map(([value, label]) => (
                        <Button
                          key={value}
                          disabled={Boolean(processing) || !patient}
                          onClick={() => runScribe(value)}
                        >
                          {label}
                        </Button>
                      ))}
                    </div>
                  </div>
                )}
                {/* Live capture is a clinical-view affordance; the server
                    refuses a clinical capture from a patient login and a
                    patient capture from this one. Admin authors nothing. */}
                {session.role !== 'admin' && (
                  <VoiceCapture
                    patientId={selected}
                    kind="clinical"
                    disabled={Boolean(processing) || !patient}
                    onCaptured={() => load(selected)}
                  />
                )}
              </div>
            }
          />
        </>
      )}
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(null)
  const [checking, setChecking] = useState(true)
  // Set when the server stops accepting our cookie. Sessions last 60 minutes
  // and there is deliberately no refresh flow (D-016), so this fires on a real
  // clinic laptop most afternoons — it needs a real answer, not a red line.
  const [expired, setExpired] = useState(false)

  useEffect(() => {
    // A 401 from any request means the session ended, wherever it happened.
    // Handling it centrally beats each caller rendering its own dead end.
    return Api.onUnauthorized(() => {
      setSession(null)
      setExpired(true)
    })
  }, [])

  useEffect(() => {
    // Restore an existing session from the httpOnly cookie. Nothing is read
    // from browser storage because nothing was ever written there.
    Api.me()
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setChecking(false))
  }, [])

  async function signOut() {
    await Api.logout().catch(() => {})
    setExpired(false)
    setSession(null)
  }

  if (checking) {
    return <p className="p-8 text-sm text-slate-500">Checking session…</p>
  }
  return session ? (
    <ErrorBoundary>
      <Workspace session={session} onSignOut={signOut} />
    </ErrorBoundary>
  ) : (
    <div className="px-4">
      <h1 className="mt-10 text-center text-xl font-semibold tracking-tight">Care Note</h1>
      <p className="mt-1 text-center text-sm text-slate-600">
        Shared longitudinal patient record
      </p>
      {/* Signed out by the clock, not by choice. Say which, say that saved
          work survived, and put the sign-in directly underneath — a session
          that ends without explanation reads as data loss. */}
      {expired && (
        <div className="mx-auto mt-4 max-w-sm rounded border border-amber-300 bg-amber-50 p-3">
          <p className="text-sm font-medium text-amber-900">
            Your session timed out
          </p>
          <p className="mt-1 text-sm text-amber-900">
            Sessions end after 60 minutes. Anything you saved is in the record —
            sign in again to carry on.
          </p>
        </div>
      )}
      <LoginForm onLoggedIn={(next) => { setExpired(false); setSession(next) }} />
    </div>
  )
}
