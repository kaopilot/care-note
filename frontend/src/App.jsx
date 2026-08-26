import { useCallback, useEffect, useState } from 'react'

/**
 * Phase 1 walking skeleton: log in, pick a patient, read the timeline.
 *
 * Deliberately ugly. The point of this phase is proving the plumbing, not the
 * product — the design pass belongs in Phase 6, on the Glance View.
 *
 * Two things here are load-bearing rather than cosmetic:
 *
 * 1. No token is ever held in JavaScript. Login sets an httpOnly cookie
 *    server-side; every fetch below sends `credentials: 'include'` and the
 *    browser attaches it. The response body's `access_token` is deliberately
 *    ignored — reading it into state would be the first step toward
 *    localStorage, which D-016 rules out. Session is restored across refresh by
 *    asking GET /auth/me, not by remembering anything.
 *
 * 2. Note content is rendered as a text child, never as HTML. React escapes it,
 *    so a stored `<script>` in a note body is inert. This file contains no
 *    raw-HTML sink of any kind, and
 *    tests/test_sanitization.py::test_frontend_never_renders_raw_html scans it
 *    and fails the build if one appears (D-015). Note that the scan is a plain
 *    text search, so even naming the forbidden props in a comment trips it —
 *    which is the correct level of paranoia for this control.
 *
 * What the UI shows is NOT the security boundary. The server has already
 * filtered the timeline to what this role may read; this page just draws
 * whatever came back. Hiding things client-side would be theatre.
 */

const ROLE_STYLES = {
  clinician: 'border-l-4 border-l-indigo-500',
  staff: 'border-l-4 border-l-emerald-500',
  patient: 'border-l-4 border-l-amber-500',
  system: 'border-l-4 border-l-slate-400 border-dashed bg-slate-50',
}

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    const error = new Error(detail.detail || `HTTP ${response.status}`)
    error.status = response.status
    throw error
  }
  return response.status === 204 ? null : response.json()
}

function LoginForm({ onLoggedIn }) {
  const [username, setUsername] = useState('clinician_a')
  const [password, setPassword] = useState('carenote-demo')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      // The response body carries an access_token for non-browser clients.
      // We intentionally do not touch it — the cookie is our transport.
      await api('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ username, password }),
      })
      onLoggedIn(await api('/auth/me'))
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="max-w-sm">
      <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">Sign in</h2>
      <input
        className="mt-2 w-full border border-slate-300 px-2 py-1 text-sm"
        value={username}
        onChange={(e) => setUsername(e.target.value)}
        placeholder="username"
      />
      <input
        className="mt-2 w-full border border-slate-300 px-2 py-1 text-sm"
        type="password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="password"
      />
      <button
        className="mt-2 border border-slate-800 bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-50"
        onClick={submit}
        disabled={busy}
      >
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
      {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}
      <p className="mt-3 text-xs text-slate-500">
        Seeded accounts: clinician_a · staff_a · admin_a · patient_a · and the same four with
        _b. Password carenote-demo. All data synthetic.
      </p>
    </div>
  )
}

function Entry({ entry }) {
  const accent = ROLE_STYLES[entry.author_role] || 'border-l-4 border-l-slate-300'
  return (
    <li className={`${accent} mb-2 bg-white p-3`}>
      <div className="flex flex-wrap items-baseline gap-2 text-xs text-slate-500">
        <span className="font-mono">{entry.type}</span>
        <span>·</span>
        <span>{entry.author_role}</span>
        <span>·</span>
        <span>{new Date(entry.timestamp).toLocaleString()}</span>
        {entry.risk_level !== 'none' && (
          <span className="bg-rose-100 px-1 text-rose-800">risk: {entry.risk_level}</span>
        )}
        {entry.is_ai_scribed && (
          <span className="bg-slate-200 px-1 font-medium text-slate-700">AI-SCRIBED</span>
        )}
      </div>
      {entry.title && <div className="mt-1 text-sm font-semibold">{entry.title}</div>}
      {/* Text child, not HTML. React escapes this. See D-015. */}
      <p className="mt-1 whitespace-pre-wrap text-sm text-slate-800">{entry.content}</p>
      <div className="mt-1 font-mono text-[10px] text-slate-400">
        {entry.provenance_pointer} · v{entry.version_number}
      </div>
    </li>
  )
}

function Timeline({ session, onSignOut }) {
  const [patients, setPatients] = useState([])
  const [selected, setSelected] = useState(null)
  const [entries, setEntries] = useState([])
  const [error, setError] = useState(null)
  const [draft, setDraft] = useState('')
  const [loadMs, setLoadMs] = useState(null)

  useEffect(() => {
    api('/patients')
      .then((rows) => {
        setPatients(rows)
        if (rows.length) setSelected(rows[0].id)
      })
      .catch((err) => setError(err.message))
  }, [])

  const loadEntries = useCallback((patientId) => {
    const started = performance.now()
    api(`/patients/${patientId}/entries`)
      .then((rows) => {
        setEntries(rows)
        setLoadMs(Math.round(performance.now() - started))
        setError(null)
      })
      .catch((err) => {
        setEntries([])
        setError(err.message)
      })
  }, [])

  useEffect(() => {
    if (selected) loadEntries(selected)
  }, [selected, loadEntries])

  // Which type this role is allowed to author. Mirrors policy.WRITABLE_TYPES;
  // the server refuses anything else regardless of what is sent from here.
  const writableType = {
    staff: 'staff_note',
    clinician: 'clinician_section',
    patient: 'patient_note',
  }[session.role]

  async function addEntry() {
    try {
      await api(`/patients/${selected}/entries`, {
        method: 'POST',
        body: JSON.stringify({ type: writableType, content: draft }),
      })
      setDraft('')
      loadEntries(selected)
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-200 pb-2">
        <div className="text-sm">
          <span className="font-semibold">{session.name}</span>{' '}
          <span className="text-slate-500">
            ({session.role} · {session.clinic_name})
          </span>
        </div>
        <button className="text-xs text-slate-600 underline" onClick={onSignOut}>
          Sign out
        </button>
      </div>

      <div className="mt-3 flex flex-wrap gap-1">
        {patients.map((p) => (
          <button
            key={p.id}
            onClick={() => setSelected(p.id)}
            className={`border px-2 py-1 text-xs ${
              selected === p.id ? 'border-slate-800 bg-slate-800 text-white' : 'border-slate-300'
            }`}
          >
            {p.name} · {p.mrn}
          </button>
        ))}
      </div>

      <div className="mt-2 text-xs text-slate-500">
        Server returned {entries.length} entr{entries.length === 1 ? 'y' : 'ies'} for this role
        {loadMs !== null && ` · ${loadMs}ms round-trip`}
      </div>

      {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}

      <ul className="mt-3">
        {entries.map((entry) => (
          <Entry key={entry.id} entry={entry} />
        ))}
        {!entries.length && !error && (
          <li className="text-sm text-slate-500">Nothing visible to this role.</li>
        )}
      </ul>

      {writableType && (
        <div className="mt-4 border-t border-slate-200 pt-3">
          <label className="text-xs text-slate-500">
            Add a <span className="font-mono">{writableType}</span>
          </label>
          <textarea
            className="mt-1 w-full border border-slate-300 p-2 text-sm"
            rows={3}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Plain text. Angle brackets such as BP <130/80 are stored verbatim."
          />
          <button
            className="mt-1 border border-slate-800 bg-slate-800 px-3 py-1 text-sm text-white disabled:opacity-40"
            onClick={addEntry}
            disabled={!draft.trim()}
          >
            Add entry
          </button>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [session, setSession] = useState(null)
  const [checking, setChecking] = useState(true)

  useEffect(() => {
    // Restore an existing session from the httpOnly cookie. Nothing is read
    // from localStorage because nothing was ever written there.
    api('/auth/me')
      .then(setSession)
      .catch(() => setSession(null))
      .finally(() => setChecking(false))
  }, [])

  async function signOut() {
    await api('/auth/logout', { method: 'POST' }).catch(() => {})
    setSession(null)
  }

  return (
    <main className="mx-auto max-w-3xl p-8 font-sans">
      <h1 className="text-2xl font-semibold">Care Note</h1>
      <p className="mt-1 text-sm text-slate-600">
        Shared longitudinal patient note · Phase 1 walking skeleton · synthetic data only
      </p>
      <p className="mt-1 text-xs text-slate-400">
        What you can see below is decided by the server, not by this page.
      </p>

      <section className="mt-6">
        {checking ? (
          <p className="text-sm text-slate-500">Checking session…</p>
        ) : session ? (
          <Timeline session={session} onSignOut={signOut} />
        ) : (
          <LoginForm onLoggedIn={setSession} />
        )}
      </section>
    </main>
  )
}
