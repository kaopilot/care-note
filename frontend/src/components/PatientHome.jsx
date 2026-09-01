/**
 * What the patient sees.
 *
 * Same record, same server-side policy, a different job. This view is not
 * scored on information density — it is scored on whether a non-clinical,
 * possibly anxious reader knows what to do next within about ten seconds.
 *
 * So: one column, larger type, generous spacing, and a deliberately calmer
 * palette than the clinical Glance View. No risk chips, no confidence
 * percentages, no provenance pointers, no scores. Those are instruments for
 * calibrating professional trust in machine output; showing a patient that a
 * summary scored 0.47 would communicate nothing except alarm.
 *
 * The labels come from the server (`labels`) so the plain-language vocabulary
 * lives beside the policy that decides what a patient may read, rather than
 * being reinvented in the client.
 */

import { shortDate } from '../lib/format'
import { EmptyState } from './Primitives'

export default function PatientHome({ care, timing, onRunSession, sessionBusy, voiceCapture }) {
  const labels = care.labels || {}

  return (
    <div className="mx-auto max-w-2xl">
      <header className="rounded-lg border border-slate-300 bg-white px-5 py-4">
        <h2 className="text-xl font-semibold">Hello, {care.patient.name.split(' ')[0]}</h2>
        <p className="mt-1 text-sm text-slate-600">
          This is what your care team has shared with you. It is not your full medical
          record — it is the part written for you to read.
        </p>
        {care.new_since_last_visit > 0 && (
          <p className="mt-2 inline-block rounded bg-emerald-50 px-2 py-1 text-sm text-emerald-900 ring-1 ring-emerald-200">
            {care.new_since_last_visit} new{' '}
            {care.new_since_last_visit === 1 ? 'update' : 'updates'} since you last looked
          </p>
        )}
      </header>

      {/* Above "what to do next", deliberately. If something she already acted
          on has changed, that outranks every other thing this page says — she
          may be taking the wrong dose right now. Plain language, no clinical
          shorthand, and it says what to actually do rather than just flagging
          that a change occurred. See DECISIONS.md D-074. */}
      {(care.corrections || []).length > 0 && (
        <section className="mt-4 rounded-lg border-2 border-amber-500 bg-amber-50 px-5 py-4">
          <h3 className="text-base font-semibold text-amber-950">
            Please read this first — something changed
          </h3>
          <ul className="mt-2 space-y-3">
            {(care.corrections || []).map((correction) => (
              <li key={correction.entry_id} className="rounded bg-white px-3 py-2 ring-1 ring-amber-300">
                <p className="text-sm font-semibold text-slate-900">{correction.title}</p>
                <p className="mt-1 text-sm leading-snug text-slate-800">
                  {correction.message}
                </p>
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-amber-900">
            If you are unsure what changed or what to do, call the clinic before
            your next dose.
          </p>
        </section>
      )}

      <section className="mt-4 rounded-lg border border-slate-300 bg-white px-5 py-4">
        <h3 className="text-base font-semibold text-slate-900">
          {labels.next_steps || 'What to do next'}
        </h3>
        {care.next_steps.length ? (
          <ol className="mt-2 space-y-2">
            {care.next_steps.map((step, index) => (
              <li key={index} className="flex gap-3">
                <span
                  aria-hidden="true"
                  className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white"
                >
                  {index + 1}
                </span>
                <span className="text-[15px] leading-relaxed text-slate-800">{step.text}</span>
              </li>
            ))}
          </ol>
        ) : (
          <p className="mt-2 text-[15px] text-slate-600">
            Nothing to do right now. Your care team will add steps here after your next
            visit.
          </p>
        )}
      </section>

      <section className="mt-4">
        <h3 className="text-base font-semibold text-slate-900">
          {labels.updates || 'What your care team wrote for you'}
        </h3>
        {care.updates.length ? (
          <ul className="mt-2 space-y-2">
            {care.updates.map((update) => (
              <li key={update.id} className="rounded-lg border border-slate-200 bg-white px-4 py-3">
                <div className="flex items-baseline justify-between gap-2">
                  <h4 className="text-sm font-semibold text-slate-900">{update.title}</h4>
                  <span className="text-xs text-slate-500">{shortDate(update.written_at)}</span>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-[15px] leading-relaxed text-slate-800">
                  {update.content}
                </p>
              </li>
            ))}
          </ul>
        ) : (
          <div className="mt-2">
            <EmptyState title="Nothing here yet">
              Summaries and instructions written for you will appear here after a visit.
            </EmptyState>
          </div>
        )}
      </section>

      <section className="mt-4">
        <h3 className="text-base font-semibold text-slate-900">
          {labels.your_notes || 'What you told us'}
        </h3>
        <p className="mt-1 text-sm text-slate-600">
          Anything you share before an appointment goes to your care team, so you do not
          have to remember it in the room.
        </p>
        {care.your_notes.length > 0 && (
          <ul className="mt-2 space-y-2">
            {care.your_notes.map((note) => (
              <li key={note.id} className="rounded-lg border border-slate-200 bg-white px-4 py-3">
                <div className="flex items-baseline justify-between gap-2">
                  <h4 className="text-sm font-semibold text-slate-900">
                    {note.title || 'Your note'}
                  </h4>
                  <span className="text-xs text-slate-500">{shortDate(note.written_at)}</span>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-[15px] leading-relaxed text-slate-800">
                  {note.content}
                </p>
              </li>
            ))}
          </ul>
        )}
        <button
          onClick={onRunSession}
          disabled={sessionBusy}
          className="mt-3 rounded bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
        >
          {sessionBusy ? 'Preparing your summary…' : 'Prepare for my next appointment'}
        </button>
        <p className="mt-1 text-xs text-slate-500">
          Runs a short question-and-answer session and writes a summary for your care team.
          Your name and contact details are removed before anything is processed.
        </p>

        {/* Voice capture, patient side. Deliberately placed under "What you
            told us" rather than given its own section: to the patient this is
            another way of telling their care team something, not a feature. */}
        {voiceCapture && <div className="mt-3">{voiceCapture}</div>}
      </section>

      {timing && (
        <p className="mt-6 text-center font-mono text-[10px] text-slate-400">
          loaded in {timing.clientMs.toFixed(0)}ms
        </p>
      )}
    </div>
  )
}
