/**
 * The longitudinal timeline.
 *
 * Grouped by date rather than rendered as one flat list. A chart is read by
 * visit — "what happened in February" — and a continuous feed with no visible
 * seams makes a six-month gap look identical to a six-minute one. The date
 * heading is the structural device, and it encodes something true: entries
 * cluster around encounters.
 *
 * Two states that are easy to leave as blank gaps and are defined here instead:
 * the empty chart, and the moment between submitting a transcript and the
 * summary existing. The second is a real state a user will see, because the
 * scribe pipeline is synchronous and takes as long as it takes.
 */

import { useMemo } from 'react'
import { shortDate } from '../lib/format'
import EntryCard from './EntryCard'
import { EmptyState, SectionTitle } from './Primitives'

function groupByDate(entries) {
  const groups = []
  let current = null
  for (const entry of entries) {
    const day = shortDate(entry.timestamp)
    if (!current || current.day !== day) {
      current = { day, entries: [] }
      groups.push(current)
    }
    current.entries.push(entry)
  }
  return groups
}

/**
 * The AI-scribed entry that does not exist yet.
 *
 * Shown from the moment a transcript is submitted until the summary comes back.
 * Deliberately shaped like the card it will become — same rail, same monospace
 * body — so the timeline does not reflow underneath the reader when the real
 * entry arrives.
 */
function ProcessingCard({ label }) {
  return (
    <li className="border-l-4 border-dashed border-l-role-system bg-white p-3 shadow-sm ring-1 ring-slate-200">
      <div className="flex items-center gap-2">
        <span className="motion-pulse inline-block h-2 w-2 animate-pulse rounded-full bg-role-system" />
        <span className="text-xs font-medium text-slate-700">{label}</span>
      </div>
      <p className="mt-1.5 font-mono text-[13px] text-slate-500">
        Redacting identifiers, then summarising the transcript…
      </p>
      <div className="mt-2 space-y-1" aria-hidden="true">
        <div className="h-2 w-4/5 rounded bg-slate-100" />
        <div className="h-2 w-3/5 rounded bg-slate-100" />
      </div>
    </li>
  )
}

export default function Timeline({
  entries,
  processing,
  emphasis,
  users,
  session,
  onChanged,
  registerRef,
  composer,
}) {
  const groups = useMemo(() => groupByDate(entries), [entries])
  const canComment = ['staff', 'clinician', 'admin'].includes(session.role)
  const canHighlight = session.role === 'clinician'
  const canRestore = ['clinician', 'admin'].includes(session.role)

  return (
    <section className="mt-5">
      <div className="flex items-baseline justify-between">
        <SectionTitle count={entries.length} hint="newest first, all sources">
          Longitudinal timeline
        </SectionTitle>
        <p className="text-[11px] text-slate-400">
          Solid rail = written by a person · dashed rail and monospace = machine generated
        </p>
      </div>

      {composer}

      {!entries.length && !processing && (
        <div className="mt-3">
          <EmptyState title="No entries visible on this chart yet">
            Notes you add, and AI summaries generated from consults, will appear here in
            date order. What you can see is decided by the server, not by this page.
          </EmptyState>
        </div>
      )}

      {processing && (
        <ul className="mt-3">
          <ProcessingCard label={processing} />
        </ul>
      )}

      {groups.map((group) => (
        <div key={group.day} className="mt-4">
          <div className="sticky top-0 z-10 -mx-1 bg-paper/95 px-1 py-1 backdrop-blur">
            <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-slate-500">
              {group.day}
            </h3>
          </div>
          <ul className="mt-1.5 space-y-2">
            {group.entries.map((entry) => (
              <EntryCard
                key={entry.id}
                entry={entry}
                emphasis={emphasis?.entryId === entry.id ? emphasis : null}
                users={users}
                canComment={canComment}
                canHighlight={canHighlight}
                canRestore={canRestore}
                onChanged={onChanged}
                registerRef={registerRef(entry.id)}
              />
            ))}
          </ul>
        </div>
      ))}
    </section>
  )
}
