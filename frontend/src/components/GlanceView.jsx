/**
 * The Glance View — the Top Card.
 *
 * The brief's requirement is that this is fully readable and actionable in
 * under ten seconds. That budget is spent on attention, not pixels, so the
 * ordering below is the design:
 *
 *   1. What changed since you were last here     (the question you walk in with)
 *   2. What could hurt this patient              (risk, then AI uncertainty)
 *   3. What the system thinks matters, and why   (highlights, with reasons)
 *   4. What is outstanding, and whose it is      (open actions)
 *
 * Everything else lives in the timeline below. Refusing to surface things is
 * what makes the card readable; a Top Card that shows everything is a timeline
 * with extra steps.
 *
 * Accept and reject are single clicks, inline, with no navigation and no
 * confirmation. The button flips to a confirmed state immediately and the row
 * fades out of the suggestion list. This is not only a nicety: Phase 4 learns
 * from these decisions, and a high-friction control produces a sparse signal.
 */

import { useEffect, useState } from 'react'
import { Api } from '../lib/api'
import LearningPanel from './LearningPanel'
import { entryLabel, relativeAge, shortDateTime } from '../lib/format'
import {
  Button,
  Chip,
  ConfidenceChip,
  EmptyState,
  RiskChip,
  ScoreBreakdown,
  SectionTitle,
} from './Primitives'

export default function GlanceView({ glance, timing, onJumpTo, onChanged, canDecide }) {
  const [decided, setDecided] = useState({})
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  // Optimistic decisions are keyed by highlight id, and the reload that follows
  // a decision brings back the real status. Holding the local map across that
  // reload left a "Confirmed" pill attached to an id the server had already
  // answered for — and, when suggestions were regenerated, to nothing at all.
  // Clearing on each new payload makes the server the single source of truth.
  useEffect(() => {
    setDecided({})
  }, [glance.generated_at])

  /**
   * Close or cancel a task without leaving the card.
   *
   * The endpoint existed from Phase 2.5 and nothing called it: tasks could be
   * created from a comment thread but never finished, so "Open actions" only
   * ever grew. That also quietly distorted ranking — `action_score` reads the
   * open-task count, so an action nobody could close kept its entry pinned.
   */
  async function setTaskStatus(task, next) {
    setBusy(task.id)
    setError(null)
    try {
      await Api.setTaskStatus(task.id, next)
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  async function decide(highlight, accepted) {
    setBusy(highlight.id)
    setError(null)
    try {
      if (accepted) await Api.acceptHighlight(highlight.id)
      else await Api.rejectHighlight(highlight.id)
      // Optimistic local confirmation so the click feels instant; the reload
      // that follows is what makes it true.
      setDecided((prev) => ({ ...prev, [highlight.id]: accepted ? 'accepted' : 'rejected' }))
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(null)
    }
  }

  const { whats_new: whatsNew, highlights, risk_flags: riskFlags } = glance
  const confidenceFlags = glance.confidence_flags || []
  const openActions = glance.open_actions || []
  const conflicts = glance.conflicts || []

  return (
    <section
      aria-label="Glance view"
      className="rounded-lg border border-slate-300 bg-white shadow-sm"
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-slate-200 px-4 py-3">
        <div>
          <h2 className="text-lg font-semibold leading-tight">{glance.patient.name}</h2>
          <p className="font-mono text-[11px] text-slate-500">
            {glance.patient.mrn} · born {glance.patient.dob} · {glance.counts.entries} entries ·{' '}
            {glance.counts.ai_scribed} AI-scribed
          </p>
        </div>
        {timing && (
          <p
            className="font-mono text-[10px] text-slate-400"
            title="Server handling time, then full browser round trip"
          >
            glance {timing.serverMs.toFixed(0)}ms server / {timing.clientMs.toFixed(0)}ms
            round trip
          </p>
        )}
      </header>

      <div className="grid gap-4 px-4 py-4 lg:grid-cols-[1.6fr_1fr]">
        <div className="space-y-4">
          {/* 1 — what changed */}
          <div>
            <SectionTitle
              count={whatsNew.count || undefined}
              hint={
                whatsNew.first_visit
                  ? 'first time you have opened this chart'
                  : whatsNew.since
                    ? `since ${shortDateTime(whatsNew.since)}`
                    : undefined
              }
            >
              New since your last visit
            </SectionTitle>
            {whatsNew.count > 0 ? (
              <ul className="mt-1.5 space-y-1">
                {whatsNew.entries.map((entry) => (
                  <li key={entry.id}>
                    <button
                      onClick={() => onJumpTo(entry.id)}
                      className="group flex w-full items-baseline gap-2 rounded border border-slate-200 bg-slate-50 px-2 py-1.5 text-left hover:border-slate-400"
                    >
                      <span
                        aria-hidden="true"
                        className={
                          entry.is_ai_scribed
                            ? 'text-role-system'
                            : 'text-slate-400 group-hover:text-slate-700'
                        }
                      >
                        {entry.is_ai_scribed ? '◇' : '•'}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="text-xs font-medium text-slate-800">
                          {entry.title || entryLabel(entry.type)}
                        </span>
                        <span
                          className={`ml-1 block truncate text-[11px] ${
                            entry.is_ai_scribed ? 'font-mono text-slate-600' : 'text-slate-500'
                          }`}
                        >
                          {entry.preview}
                        </span>
                      </span>
                      <span className="whitespace-nowrap font-mono text-[10px] text-slate-400">
                        {relativeAge(entry.timestamp)}
                      </span>
                    </button>
                  </li>
                ))}
                {/* The server caps this list. Saying "8 of 12" is the honest
                    version of a count that does not match the rows under it. */}
                {whatsNew.count > whatsNew.entries.length && (
                  <li className="px-2 text-[11px] text-slate-500">
                    and {whatsNew.count - whatsNew.entries.length} more in the timeline
                    below
                  </li>
                )}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-slate-500">
                {whatsNew.first_visit
                  ? 'Nothing marked as new — this is your first look at this chart.'
                  : 'No new entries since you were last here.'}
              </p>
            )}
          </div>

          {/* 3 — highlights */}
          <div>
            <SectionTitle count={highlights.length} hint="ranked, with the reason shown">
              What matters now
            </SectionTitle>
            {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}
            {highlights.length ? (
              <ul className="mt-1.5 space-y-2">
                {highlights.map((highlight) => {
                  const decision = decided[highlight.id]
                  return (
                    <li
                      key={highlight.id}
                      className={`rounded border bg-white p-2.5 transition ${
                        decision === 'rejected'
                          ? 'border-slate-200 opacity-40'
                          : highlight.status === 'accepted' || decision === 'accepted'
                            ? 'border-emerald-300 bg-emerald-50/40'
                            : 'border-slate-200'
                      }`}
                    >
                      <div className="flex flex-wrap items-center gap-1.5">
                        {highlight.is_ai_scribed ? (
                          <Chip tone="ai" title="Drawn from a machine-generated summary">
                            ◇ From AI note
                          </Chip>
                        ) : (
                          <Chip tone="human">• {entryLabel(highlight.entry_type)}</Chip>
                        )}
                        {highlight.is_manual && <Chip tone="good">Marked by clinician</Chip>}
                        {(highlight.status === 'accepted' || decision === 'accepted') && (
                          <Chip tone="good">✓ Confirmed</Chip>
                        )}
                        {highlight.stale && (
                          <Chip
                            tone="alert"
                            title={`Anchored to version ${highlight.source_version_number}; the entry has been edited since`}
                          >
                            Source edited since
                          </Chip>
                        )}
                        <span className="ml-auto font-mono text-[10px] text-slate-400">
                          {relativeAge(highlight.entry_timestamp)}
                        </span>
                      </div>

                      {/* The claim itself, in the register of whoever wrote it. */}
                      <button
                        onClick={() => onJumpTo(highlight.entry_id, highlight)}
                        className="mt-1.5 block w-full text-left"
                        title="Open the source entry at this exact span"
                      >
                        <span
                          className={`text-sm leading-snug text-slate-900 underline decoration-slate-300 underline-offset-2 hover:decoration-slate-700 ${
                            highlight.is_ai_scribed ? 'font-mono text-[13px]' : ''
                          }`}
                        >
                          {highlight.span_text}
                        </span>
                      </button>

                      <p className="mt-1 text-[11px] text-slate-600">
                        <span className="font-medium text-slate-700">Why: </span>
                        {highlight.risk_reason}
                      </p>
                      <ScoreBreakdown breakdown={highlight.score_breakdown} />

                      <div className="mt-1.5 flex items-center gap-2">
                        <button
                          onClick={() => onJumpTo(highlight.entry_id, highlight)}
                          className="font-mono text-[10px] text-slate-400 underline decoration-dotted hover:text-slate-700"
                          title="Provenance pointer — click to open the source"
                        >
                          {highlight.provenance_pointer}
                        </button>
                        {canDecide && highlight.status === 'suggested' && !decision && (
                          <span className="ml-auto flex gap-1">
                            <Button
                              variant="accept"
                              disabled={busy === highlight.id}
                              onClick={() => decide(highlight, true)}
                            >
                              Confirm
                            </Button>
                            <Button
                              variant="reject"
                              disabled={busy === highlight.id}
                              onClick={() => decide(highlight, false)}
                            >
                              Dismiss
                            </Button>
                          </span>
                        )}
                        {decision && (
                          <span className="ml-auto text-[11px] font-medium text-slate-600">
                            {decision === 'accepted' ? 'Confirmed' : 'Dismissed'}
                          </span>
                        )}
                      </div>
                    </li>
                  )
                })}
              </ul>
            ) : (
              <EmptyState title="Nothing surfaced yet">
                Highlights appear once there are notes containing medications,
                symptoms, allergies or outstanding actions.
              </EmptyState>
            )}
          </div>
        </div>

        {/* 2 and 4 — flags and outstanding work */}
        <div className="space-y-4">
          <div>
            <SectionTitle count={riskFlags.length}>Risk flags</SectionTitle>
            {riskFlags.length ? (
              <ul className="mt-1.5 space-y-1">
                {riskFlags.map((flag) => (
                  <li key={flag.entry_id}>
                    <button
                      onClick={() => onJumpTo(flag.entry_id)}
                      className="flex w-full items-center gap-2 rounded border border-slate-200 px-2 py-1.5 text-left hover:border-slate-400"
                    >
                      <RiskChip level={flag.level} />
                      <span className="min-w-0 flex-1 truncate text-xs text-slate-700">
                        {flag.title || entryLabel(flag.entry_type)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-slate-500">No risk-flagged entries.</p>
            )}
          </div>

          {confidenceFlags.length > 0 && (
            <div>
              {/* Kept separate from risk on purpose: "this might be dangerous"
                  and "this might be wrong" are different warnings and a
                  clinician acts on them differently. */}
              <SectionTitle count={confidenceFlags.length}>AI needs checking</SectionTitle>
              <ul className="mt-1.5 space-y-1">
                {confidenceFlags.map((flag) => (
                  <li key={flag.entry_id}>
                    <button
                      onClick={() => onJumpTo(flag.entry_id)}
                      className="w-full rounded border border-orange-200 bg-orange-50/60 px-2 py-1.5 text-left hover:border-orange-400"
                    >
                      <ConfidenceChip confidence={flag.confidence} />
                      <span className="mt-1 block truncate text-xs text-slate-700">
                        {flag.title || entryLabel(flag.type)}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {conflicts.length > 0 && (
            <div>
              <SectionTitle count={conflicts.length}>Clinician corrections</SectionTitle>
              <ul className="mt-1.5 space-y-1">
                {conflicts.map((conflict) => (
                  <li key={conflict.entry_id}>
                    <button
                      onClick={() => onJumpTo(conflict.entry_id)}
                      className="w-full rounded border border-slate-300 bg-slate-50 px-2 py-1.5 text-left text-xs hover:border-slate-500"
                    >
                      Correction on record
                      {conflict.supersedes_type
                        ? ` — supersedes ${entryLabel(conflict.supersedes_type)}`
                        : ''}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div>
            <SectionTitle count={openActions.length}>Open actions</SectionTitle>
            {openActions.length ? (
              <ul className="mt-1.5 space-y-1">
                {openActions.map((action) => (
                  <li
                    key={`${action.kind}-${action.id}`}
                    className="rounded border border-slate-200 px-2 py-1.5"
                  >
                    <div className="flex items-baseline gap-1.5">
                      <Chip tone={action.kind === 'task' ? 'info' : 'neutral'}>
                        {action.kind === 'task' ? 'Task' : 'Thread'}
                      </Chip>
                      <span className="truncate text-[11px] text-slate-500">
                        {action.assigned_to_name}
                      </span>
                    </div>
                    <button
                      onClick={() => action.entry_id && onJumpTo(action.entry_id)}
                      className="mt-0.5 block text-left text-xs text-slate-800 hover:underline"
                    >
                      {action.description}
                    </button>
                    {/* Closing an action is the other half of raising one.
                        Inline and single-click, for the same reason
                        accept/reject is: an outstanding item nobody can tick
                        off stops meaning anything. */}
                    {action.kind === 'task' && (
                      <div className="mt-1 flex gap-1">
                        <Button
                          variant="accept"
                          disabled={busy === action.id}
                          onClick={() => setTaskStatus(action, 'done')}
                        >
                          Mark done
                        </Button>
                        <Button
                          variant="reject"
                          disabled={busy === action.id}
                          onClick={() => setTaskStatus(action, 'cancelled')}
                          title="Cancel this task. It stays in the audit log."
                        >
                          Cancel
                        </Button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-slate-500">Nothing outstanding.</p>
            )}
          </div>

          {/* Collapsed by default: not one of the four questions this card is
              for, but the one a clinician asks the first time it surfaces
              something unexpected. One click, not absent. */}
          <LearningPanel canRebuild={canDecide} />
        </div>
      </div>
    </section>
  )
}
