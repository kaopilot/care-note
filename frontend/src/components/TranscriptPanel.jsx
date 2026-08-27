/**
 * The transcript behind an AI-scribed consult note.
 *
 * This is where Phase 5's provenance claim gets cashed. The entry card already
 * shows *which session* a note came from; this panel shows which spoken segment
 * each line came from, who said it, when, and how sure the recogniser was.
 *
 * Two directions of travel, because clinicians read both ways:
 *   summary line → the words behind it   (click a line in "Where each line came from")
 *   transcript   → what it produced      (matched segments are marked)
 *
 * Confidence uses the same ConfidenceChip and the same 0.6 threshold as the
 * Glance View rather than a second visual language for "the machine is unsure"
 * — that reuse is a Phase 5 instruction and it is also just correct: a clinician
 * should not have to learn two vocabularies for one idea.
 *
 * Everything here is rendered as React text children. Transcript text is
 * untrusted multi-author content like any note body, and it is never turned
 * into markup (D-015).
 */

import { useEffect, useState } from 'react'
import { Api } from '../lib/api'
import { Button, Chip, ConfidenceChip, SectionTitle } from './Primitives'

function clock(ms) {
  if (ms === null || ms === undefined) return '--:--'
  const total = Math.floor(ms / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

const SPEAKER_STYLE = {
  clinician: 'text-sky-900 bg-sky-50 ring-sky-200',
  staff: 'text-violet-900 bg-violet-50 ring-violet-200',
  patient: 'text-emerald-900 bg-emerald-50 ring-emerald-200',
  system: 'text-amber-900 bg-amber-50 ring-amber-300',
  other: 'text-slate-700 bg-slate-100 ring-slate-200',
}

const MATCH_LABEL = {
  verbatim: {
    text: 'spoken verbatim',
    tone: 'good',
    title: 'These words appear in the transcript exactly — the link is proved, not inferred.',
  },
  derived: {
    text: 'reworded',
    tone: 'neutral',
    title: 'The wording changed, but the line and the segment share enough distinctive terms to link them.',
  },
}

export default function TranscriptPanel({ entry }) {
  const [data, setData] = useState(null)
  const [links, setLinks] = useState([])
  const [error, setError] = useState(null)
  const [focused, setFocused] = useState(null) // segment sequence
  const sessionId = entry.ai_session_id

  useEffect(() => {
    let cancelled = false
    if (!sessionId) return undefined
    Promise.all([Api.capture_detail(sessionId), Api.attribution(entry.id)])
      .then(([detail, attribution]) => {
        if (cancelled) return
        setData(detail)
        setLinks(attribution)
      })
      .catch((err) => !cancelled && setError(err.message))
    return () => {
      cancelled = true
    }
  }, [sessionId, entry.id])

  if (!sessionId) return null
  if (error) {
    return (
      <p className="mt-2 rounded bg-slate-50 px-2 py-1 text-[11px] text-slate-600">
        No transcript is stored for this entry ({error}).
      </p>
    )
  }
  if (!data) {
    return <p className="mt-2 text-[11px] text-slate-500">Loading transcript…</p>
  }

  // `capture` is null for a note scribed from a fixture: there was no
  // recording, so there is nothing to say about one. The transcript and its
  // provenance render either way.
  const { capture, segments } = data
  const linkedSequences = new Set(links.map((link) => link.segment_sequence))

  return (
    <div className="mt-2 rounded border border-slate-300 bg-slate-50/70 p-2">
      {capture && (
      <>
      {/* --- capture provenance header --- */}
      <div className="flex flex-wrap items-center gap-1.5">
        <SectionTitle>Consult capture</SectionTitle>
        <Chip tone="neutral">{capture.kind === 'patient' ? 'Patient recording' : 'Clinical recording'}</Chip>
        {capture.transcription_simulated && (
          <Chip
            tone="alert"
            title="No speech recogniser is configured. This transcript was produced by a simulated recogniser and does not represent real audio."
          >
            ⚠ Simulated transcription
          </Chip>
        )}
        {capture.overlap_segments > 0 && (
          <Chip tone="neutral" title="Segments whose timings intersect — people speaking at once">
            ⇄ {capture.overlap_segments} overlapping
          </Chip>
        )}
        {capture.low_confidence_segments > 0 && (
          <Chip tone="ai" title="Segments the recogniser was unsure of">
            ◐ {capture.low_confidence_segments} low confidence
          </Chip>
        )}
        {capture.languages.map((language) => (
          <Chip key={language} tone="info" title="Language detected in this segment run">
            {language}
          </Chip>
        ))}
      </div>

      <dl className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-slate-500">
        <span>
          <dt className="inline">duration</dt>
          <dd className="ml-1 inline text-slate-700">
            {clock(capture.duration_ms)}
            {capture.duration_source === 'estimated_from_bytes' && ' (est.)'}
          </dd>
        </span>
        <span>
          <dt className="inline">segments</dt>
          <dd className="ml-1 inline text-slate-700">{capture.segment_count}</dd>
        </span>
        <span>
          <dt className="inline">identifiers removed</dt>
          <dd className="ml-1 inline text-slate-700">{capture.redaction_count}</dd>
        </span>
        <span>
          <dt className="inline">audio retained</dt>
          <dd className="ml-1 inline font-semibold text-emerald-700">
            {capture.audio_retained ? 'yes' : 'no'}
          </dd>
        </span>
        <span>
          <dt className="inline">recogniser</dt>
          <dd className="ml-1 inline text-slate-700">{capture.asr_model}</dd>
        </span>
      </dl>
      </>
      )}

      {/* --- line-level provenance --- */}
      <div className="mt-2">
        <SectionTitle count={links.length} hint="click a line to see the words behind it">
          Where each line came from
        </SectionTitle>
        {links.length === 0 ? (
          <p className="mt-1 text-[11px] text-slate-500">
            No line in this summary could be traced to a specific segment.
          </p>
        ) : (
          <ul className="mt-1 space-y-1">
            {links.map((link) => {
              const badge = MATCH_LABEL[link.match_type] || MATCH_LABEL.derived
              const active = focused === link.segment_sequence
              return (
                <li key={`${link.span_start}-${link.segment_sequence}`}>
                  <button
                    onClick={() =>
                      setFocused(active ? null : link.segment_sequence)
                    }
                    className={`w-full rounded px-2 py-1 text-left ring-1 transition ${
                      active
                        ? 'bg-amber-50 ring-amber-300'
                        : 'bg-white ring-slate-200 hover:bg-slate-50'
                    }`}
                  >
                    <span className="text-[12px] leading-snug text-slate-800">
                      {link.span_text}
                    </span>
                    <span className="mt-0.5 flex flex-wrap items-center gap-1.5">
                      <Chip tone={badge.tone} title={badge.title}>
                        {badge.text}
                      </Chip>
                      <span className="font-mono text-[10px] text-slate-500">
                        {link.speaker_label} · {clock(link.start_ms)}
                      </span>
                      {link.segment_confidence !== null && (
                        <ConfidenceChip confidence={link.segment_confidence} />
                      )}
                      {!link.resolves && (
                        <Chip tone="alert">source missing</Chip>
                      )}
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
        )}
      </div>

      {/* --- the transcript itself --- */}
      <details className="mt-2">
        <summary className="cursor-pointer text-[11px] font-medium text-slate-600">
          Full transcript ({segments.length} segments)
        </summary>
        <p className="mt-1 text-[10px] italic text-slate-500">{data.notice}</p>
        <ol className="mt-1 space-y-0.5">
          {segments.map((segment) => {
            const style = SPEAKER_STYLE[segment.speaker_label] || SPEAKER_STYLE.other
            const active = focused === segment.sequence
            return (
              <li
                key={segment.sequence}
                id={`segment-${segment.sequence}`}
                className={`rounded px-1.5 py-1 ${
                  active ? 'bg-amber-100 ring-1 ring-amber-400' : ''
                }`}
              >
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-[10px] text-slate-400">
                    {clock(segment.start_ms)}
                  </span>
                  <span
                    className={`rounded px-1 py-px text-[10px] font-medium ring-1 ${style}`}
                  >
                    {segment.speaker_label}
                  </span>
                  {segment.language && segment.language !== 'en' && (
                    <span
                      className="rounded bg-sky-50 px-1 py-px text-[10px] text-sky-900 ring-1 ring-sky-200"
                      title="Code-switched speech"
                    >
                      {segment.language}
                    </span>
                  )}
                  {segment.low_confidence && (
                    <span
                      className="rounded bg-orange-50 px-1 py-px text-[10px] font-medium text-orange-900 ring-1 ring-orange-300"
                      title="The recogniser was unsure of these words — verify before relying on them"
                    >
                      ◐ verify
                    </span>
                  )}
                  {segment.overlaps_previous && (
                    <span
                      className="rounded bg-slate-100 px-1 py-px text-[10px] text-slate-600 ring-1 ring-slate-300"
                      title="Starts before the previous segment ended — overlapping speech"
                    >
                      ⇄ overlap
                    </span>
                  )}
                  {linkedSequences.has(segment.sequence) && (
                    <span
                      className="rounded bg-emerald-50 px-1 py-px text-[10px] text-emerald-800 ring-1 ring-emerald-200"
                      title="This segment produced a line in the summary"
                    >
                      ▲ in summary
                    </span>
                  )}
                </span>
                <span className="mt-0.5 block whitespace-pre-wrap text-[12px] leading-snug text-slate-800">
                  {segment.text}
                </span>
              </li>
            )
          })}
        </ol>
      </details>
    </div>
  )
}

/** Toggle button + panel, so EntryCard stays declarative. */
export function TranscriptToggle({ entry }) {
  const [open, setOpen] = useState(false)
  if (!entry.is_ai_scribed || !entry.ai_session_id) return null
  return (
    <>
      <Button onClick={() => setOpen((value) => !value)}>
        {open ? 'Hide transcript' : 'Transcript & sources'}
      </Button>
      {open && <TranscriptPanel entry={entry} />}
    </>
  )
}
