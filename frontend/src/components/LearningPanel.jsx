/**
 * What this clinic has taught the ranking.
 *
 * Collapsed by default, and that is the design rather than a default nobody
 * changed. The Glance View has a ten-second attention budget and this is not
 * one of the four questions a clinician walks into a room with — but it is the
 * question they will eventually ask, the first time the card surfaces something
 * they did not expect, and the answer needs to be one click away rather than
 * absent.
 *
 * Every row shows its evidence, not just a number. "Anticoagulation, promoted,
 * 4 confirmations" is auditable; "0.36" is a machine asserting something about
 * a clinician's own behaviour with no way to check it — which is the failure
 * mode this product exists to argue against.
 *
 * The floored rows are the interesting ones. A tag showing "2 dismissals" next
 * to a weight of zero is the safety rule made visible: the clinic's behaviour
 * was recorded honestly and deliberately not followed.
 */

import { useState } from 'react'
import { Api } from '../lib/api'
import { Button, Chip } from './Primitives'

// Reads `med:warfarin` as "Warfarin", `medclass:anticoagulant` as
// "Anticoagulants". The tag vocabulary is an internal key; a clinician should
// not have to learn it to read their own clinic's ranking.
const NAMESPACE_LABEL = {
  med: 'Medication',
  medclass: 'Drug class',
  symptom: 'Symptom',
  finding: 'Finding',
  entity: 'Clinical entity',
  action: 'Action',
  quality: 'Note quality',
}

function readableTag(tag) {
  const [namespace, ...rest] = String(tag).split(':')
  const value = rest.join(':').replace(/_/g, ' ')
  const label = NAMESPACE_LABEL[namespace] || namespace
  return { label, value: value.charAt(0).toUpperCase() + value.slice(1) }
}

export default function LearningPanel({ canRebuild }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function load() {
    setBusy(true)
    setError(null)
    try {
      setData(await Api.learning())
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function toggle() {
    const next = !open
    setOpen(next)
    if (next && !data) await load()
  }

  async function rebuild() {
    setBusy(true)
    try {
      setData(await Api.rebuildLearning())
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const weights = data?.weights || []
  const signals = Object.values(data?.signal_counts || {}).reduce((sum, n) => sum + n, 0)

  return (
    <div className="rounded border border-slate-200">
      <button
        onClick={toggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-slate-50"
      >
        <span aria-hidden="true" className="font-mono text-[10px] text-slate-400">
          {open ? '▾' : '▸'}
        </span>
        <span className="text-[11px] font-medium uppercase tracking-wider text-slate-500">
          What this clinic pays attention to
        </span>
      </button>

      {open && (
        <div className="border-t border-slate-200 px-2 py-2">
          {busy && !data && <p className="text-[11px] text-slate-500">Loading…</p>}
          {error && <p className="text-[11px] text-rose-700">{error}</p>}

          {data && (
            <>
              <p className="mb-2 text-[11px] leading-snug text-slate-600">
                Learned from {signals} clinician and staff interactions in this clinic.
                Confirming or dismissing a suggestion moves these. Nothing here is
                patient-specific.
              </p>

              {weights.length ? (
                <ul className="space-y-1">
                  {weights.map((row) => {
                    const { label, value } = readableTag(row.feature_tag)
                    return (
                      <li key={row.feature_tag} className="flex items-baseline gap-1.5">
                        <span className="w-24 shrink-0 truncate text-[11px] text-slate-800">
                          {value}
                        </span>
                        <span className="shrink-0 font-mono text-[10px] text-slate-400">
                          {label}
                        </span>
                        {row.floored && row.negative_signals > 0 ? (
                          <Chip
                            tone="alert"
                            className="shrink-0"
                            title="Dismissed by clinicians, but this system will not learn to hide safety-critical content"
                          >
                            never suppressed
                          </Chip>
                        ) : (
                          <span
                            className={`shrink-0 font-mono text-[10px] ${
                              row.weight > 0 ? 'text-emerald-700' : 'text-amber-700'
                            }`}
                          >
                            {row.direction === 'promotes' ? '▲' : '▼'}{' '}
                            {row.weight > 0 ? '+' : ''}
                            {row.weight.toFixed(2)}
                          </span>
                        )}
                        <span className="ml-auto shrink-0 font-mono text-[10px] text-slate-400">
                          +{row.positive_signals}/−{row.negative_signals}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              ) : (
                <p className="text-[11px] text-slate-500">
                  Nothing learned yet. Confirm or dismiss a few suggestions and this
                  fills in.
                </p>
              )}

              {canRebuild && (
                <div className="mt-2">
                  <Button variant="quiet" disabled={busy} onClick={rebuild}>
                    Recompute from history
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}
