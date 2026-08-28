/**
 * Shared UI vocabulary.
 *
 * The text renderers at the bottom of this file are the important part. Note
 * and comment bodies are untrusted, multi-author, long-lived content displayed
 * across privilege boundaries — a staff note surfaces in a clinician's Glance
 * View. They are stored as plain text and rendered here as React text children,
 * which React escapes, so a stored payload is inert.
 *
 * `MentionText` and `SpanText` both work by SLICING THE STRING and returning an
 * array of elements. Neither builds a markup string at any point. That is the
 * whole reason a mention can be styled without reopening the hole that plain
 * text closes (D-015).
 */

import { RISK_LABEL, RISK_STYLE, SCORE_TERM_LABEL } from '../lib/format'

export function Chip({ children, tone = 'neutral', className = '', title }) {
  const tones = {
    neutral: 'bg-slate-100 text-slate-700 ring-slate-200',
    ai: 'bg-amber-50 text-amber-900 ring-amber-300',
    human: 'bg-white text-slate-700 ring-slate-300',
    alert: 'bg-rose-100 text-rose-900 ring-rose-300',
    good: 'bg-emerald-50 text-emerald-800 ring-emerald-300',
    info: 'bg-sky-50 text-sky-900 ring-sky-200',
  }
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ${tones[tone]} ${className}`}
    >
      {children}
    </span>
  )
}

/**
 * A risk indicator is never colour alone.
 *
 * Every coloured chip carries its own words. Cheap to do while building the
 * view the first time, and it is the difference between a usable interface and
 * one that silently fails a colour-blind clinician on the exact signal that
 * matters most.
 */
export function RiskChip({ level }) {
  if (!level || level === 'none') return null
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-semibold uppercase tracking-wide ring-1 ${RISK_STYLE[level]}`}
    >
      <span aria-hidden="true">▲</span>
      {RISK_LABEL[level] || level}
    </span>
  )
}

export function ConfidenceChip({ confidence }) {
  if (confidence === null || confidence === undefined) return null
  const percent = Math.round(confidence * 100)
  const low = confidence < 0.6
  return (
    <span
      title="How clearly the source transcript supported this summary"
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ${
        low
          ? 'bg-orange-50 text-orange-900 ring-orange-300'
          : 'bg-slate-100 text-slate-600 ring-slate-200'
      }`}
    >
      <span aria-hidden="true">{low ? '◐' : '●'}</span>
      AI confidence {percent}%{low ? ' — verify' : ''}
    </span>
  )
}

export function Button({ children, variant = 'default', className = '', ...rest }) {
  const variants = {
    default: 'bg-white text-slate-800 ring-1 ring-slate-300 hover:bg-slate-50',
    primary: 'bg-slate-900 text-white hover:bg-slate-700',
    accept: 'bg-emerald-600 text-white hover:bg-emerald-500',
    reject: 'bg-white text-slate-600 ring-1 ring-slate-300 hover:bg-slate-100',
    quiet: 'text-slate-600 underline decoration-slate-300 hover:decoration-slate-600',
  }
  return (
    <button
      {...rest}
      className={`rounded px-2 py-1 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${variants[variant]} ${className}`}
    >
      {children}
    </button>
  )
}

export function SectionTitle({ children, count, hint }) {
  return (
    <div className="flex items-baseline gap-2">
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-slate-500">
        {children}
      </h3>
      {count !== undefined && (
        <span className="font-mono text-[11px] text-slate-400">{count}</span>
      )}
      {hint && <span className="text-[11px] text-slate-400">{hint}</span>}
    </div>
  )
}

export function EmptyState({ title, children }) {
  return (
    <div className="rounded border border-dashed border-slate-300 bg-white/60 p-4 text-sm text-slate-600">
      <p className="font-medium text-slate-700">{title}</p>
      {children && <p className="mt-1 text-slate-500">{children}</p>}
    </div>
  )
}

/**
 * Why a highlight is on the card, as arithmetic a clinician can read.
 *
 * The score breakdown is stored per highlight precisely so this can exist. A
 * ranker that cannot explain itself is the wrong tool for a product whose
 * purpose is calibrating trust in machine output.
 */
/**
 * The arithmetic behind a suggestion, rendered so a clinician can read it.
 *
 * Negative terms are shown, not filtered out. A dampened item is one this
 * clinic has repeatedly dismissed, and that is the most interesting thing the
 * card can say about why something is ranked low — hiding it would leave the
 * learning visible only when it flatters the system.
 */
export function ScoreBreakdown({ breakdown }) {
  const terms = Object.entries(breakdown || {}).filter(
    ([key, value]) => key !== 'multiplier' && value !== 0
  )
  if (!terms.length) return null
  return (
    <dl className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px] text-slate-500">
      {terms.map(([key, value]) => (
        <span key={key} className="whitespace-nowrap">
          <dt className="inline">{SCORE_TERM_LABEL[key] || key}</dt>
          <dd
            className={`ml-1 inline ${value < 0 ? 'font-semibold text-amber-700' : 'text-slate-700'}`}
            title={
              value < 0
                ? 'Ranked down: this clinic has repeatedly dismissed content like this'
                : undefined
            }
          >
            {value > 0 ? '+' : ''}
            {value.toFixed(2)}
          </dd>
        </span>
      ))}
    </dl>
  )
}

// --------------------------------------------------------------------------
// Text rendering
// --------------------------------------------------------------------------

const MENTION_PATTERN = /(@[A-Za-z0-9_.-]+)/g

/**
 * Render a comment body, styling @mentions as elements.
 *
 * Parsing runs over the STORED PLAIN TEXT and emits React nodes. The mention is
 * matched against the clinic's real usernames, so a body containing
 * "@not_a_real_person" renders as ordinary text rather than as something that
 * looks like it notified someone.
 */
export function MentionText({ body, usernames = [] }) {
  const known = new Set(usernames.map((name) => name.toLowerCase()))
  const parts = String(body || '').split(MENTION_PATTERN)
  return (
    <p className="whitespace-pre-wrap text-sm text-slate-800">
      {parts.map((part, index) => {
        const isMention =
          part.startsWith('@') && known.has(part.slice(1).toLowerCase())
        return isMention ? (
          <span
            key={index}
            className="rounded bg-slate-900/5 px-1 font-medium text-slate-900 ring-1 ring-slate-300"
          >
            {part}
          </span>
        ) : (
          part
        )
      })}
    </p>
  )
}

/**
 * Entry content, with an optional emphasised character range.
 *
 * Used for provenance click-through: clicking a highlight scrolls to its entry
 * and emphasises the exact span it was drawn from, rather than leaving the
 * clinician to find the sentence themselves in a wall of text.
 *
 * Each emitted segment carries `data-start`, its offset in the original string.
 * `readSelectionRange` below uses that to turn a browser text selection back
 * into character offsets, which is what makes manual highlighting possible even
 * once the content has been split into several nodes.
 */
export function SpanText({ content, emphasis, mono = false, className = '' }) {
  const text = String(content || '')
  const base = `whitespace-pre-wrap text-sm leading-relaxed ${
    mono ? 'font-mono text-[13px] text-slate-800' : 'text-slate-800'
  } ${className}`

  if (!emphasis || emphasis.start >= emphasis.end || emphasis.start >= text.length) {
    return (
      <p className={base} data-start="0">
        {text}
      </p>
    )
  }

  const start = Math.max(0, emphasis.start)
  const end = Math.min(text.length, emphasis.end)
  // `data-start` on the <p> as well as on each child, so that a selection
  // resolving to the paragraph itself still has an anchor to measure from.
  return (
    <p className={base} data-start="0">
      <span data-start="0">{text.slice(0, start)}</span>
      <mark className="span-emphasis" data-start={String(start)}>
        {text.slice(start, end)}
      </mark>
      <span data-start={String(end)}>{text.slice(end)}</span>
    </p>
  )
}

/**
 * Turn the current browser selection into `{start, end}` offsets in the entry's
 * stored content, or null if the selection is empty or outside `container`.
 */
export function readSelectionRange(container) {
  const selection = window.getSelection()
  if (!selection || selection.isCollapsed || !container) return null

  /** Character offset at which the segment containing `node` begins. */
  const segmentStart = (node) => {
    let element = node.nodeType === 3 ? node.parentElement : node
    while (element && element !== container && !element.dataset?.start) {
      element = element.parentElement
    }
    if (!element || !container.contains(element)) return null
    return Number(element.dataset?.start || 0)
  }

  const offsetOf = (node, offset) => {
    if (!node) return null

    // A TEXT node's offset is a character offset within that node, so the
    // enclosing segment's start plus the offset is the answer.
    if (node.nodeType === 3) {
      const base = segmentStart(node)
      return base === null ? null : base + offset
    }

    // An ELEMENT node's offset is a CHILD INDEX, not a character offset — a
    // triple-click, or a drag ending past the last character, reports the
    // paragraph itself with offset 0..childCount. Reading that as a character
    // offset put the highlight a few characters into the entry instead of on
    // the selected words. Resolve it to the boundary before that child, or to
    // the end of the last child when the index is one past the end.
    if (!container.contains(node)) return null
    const children = node.childNodes
    const boundary = children[offset]
    if (boundary) return segmentStart(boundary)

    const last = children[children.length - 1]
    if (!last) return segmentStart(node)
    const base = segmentStart(last)
    return base === null ? null : base + (last.textContent || '').length
  }

  const anchor = offsetOf(selection.anchorNode, selection.anchorOffset)
  const focus = offsetOf(selection.focusNode, selection.focusOffset)
  if (anchor === null || focus === null) return null

  const start = Math.min(anchor, focus)
  const end = Math.max(anchor, focus)
  return end - start >= 3 ? { start, end } : null
}
