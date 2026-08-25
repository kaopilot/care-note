import { useEffect, useState } from 'react'

/**
 * Phase 0 scaffold. Deliberately unstyled beyond the bare minimum.
 *
 * Its only job is to prove the frontend builds and can reach the API through
 * the Vite proxy. Phase 1 replaces it with the login + bare timeline; Phase 2
 * builds the real product surface. Do not invest in visual design here — the
 * design pass belongs in Phase 6, on the Glance View and timeline only.
 */
export default function App() {
  const [health, setHealth] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch('/api/health')
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setError('Backend unreachable. Start it with: uvicorn app.main:app --reload'))
  }, [])

  return (
    <main className="mx-auto max-w-2xl p-8 font-sans">
      <h1 className="text-2xl font-semibold">Care Note</h1>
      <p className="mt-1 text-sm text-slate-600">
        Shared longitudinal patient note · Phase 0 scaffold · synthetic data only
      </p>

      <section className="mt-6 rounded border border-slate-200 p-4">
        <h2 className="text-sm font-medium uppercase tracking-wide text-slate-500">
          Backend status
        </h2>
        {error && <p className="mt-2 text-sm text-rose-700">{error}</p>}
        {health && (
          <pre className="mt-2 text-sm text-slate-800">{JSON.stringify(health, null, 2)}</pre>
        )}
        {!health && !error && <p className="mt-2 text-sm text-slate-500">Checking…</p>}
      </section>
    </main>
  )
}
