/**
 * Display vocabulary.
 *
 * Every place the system's own words would otherwise leak into the interface.
 * `ai_doctor_consult_summary` is a database value; a clinician should read
 * "Doctor consult - AI scribed". Keeping the mapping in one file is what stops
 * three components inventing three different names for the same thing.
 *
 * The two registers are deliberate. Clinical roles get clinical shorthand,
 * because density is the point on a ten-second card. The patient view gets
 * plain language with no abbreviations, because its reader may be anxious, on a
 * phone, and has never seen an ACR result in their life.
 */

export const ENTRY_LABEL = {
  patient_note: 'Patient note',
  staff_note: 'Staff note',
  clinician_section: 'Clinician section',
  patient_instruction: 'Instructions for patient',
  patient_summary: 'Patient summary',
  ai_doctor_consult_summary: 'Doctor consult - AI scribed',
  ai_nurse_consult_summary: 'Nurse consult - AI scribed',
  ai_patient_session_summary: 'Patient session - AI scribed',
  system_event: 'System event',
}

export const ROLE_LABEL = {
  patient: 'Patient',
  staff: 'Staff',
  clinician: 'Clinician',
  admin: 'Admin',
  system: 'Care Note AI',
}

/**
 * Authorship, encoded twice over: a colour and a shape.
 *
 * The brief requires AI-scribed notes be visually distinct from human ones, and
 * a colour alone would fail a colour-blind reader on the one distinction the
 * whole trust argument rests on. So machine entries also get a dashed rail and
 * a different typeface for their body text.
 */
export const ROLE_ACCENT = {
  patient: 'border-l-role-patient',
  staff: 'border-l-role-staff',
  clinician: 'border-l-role-clinician',
  system: 'border-l-role-system border-dashed',
  admin: 'border-l-slate-400',
}

export const RISK_STYLE = {
  none: 'bg-slate-100 text-slate-600 ring-slate-200',
  low: 'bg-sky-50 text-sky-800 ring-sky-200',
  medium: 'bg-amber-50 text-amber-900 ring-amber-300',
  high: 'bg-orange-100 text-orange-900 ring-orange-400',
  critical: 'bg-rose-100 text-rose-900 ring-rose-400',
}

export const RISK_LABEL = {
  none: 'No risk flag',
  low: 'Low risk',
  medium: 'Medium risk',
  high: 'High risk',
  critical: 'Critical',
}

export const TASK_LABEL = {
  open: 'Open',
  in_progress: 'In progress',
  done: 'Done',
  cancelled: 'Cancelled',
}

export function entryLabel(type) {
  return ENTRY_LABEL[type] || type
}

export function roleLabel(role) {
  return ROLE_LABEL[role] || role
}

/** Short absolute date. Clinicians read charts by date, not by "3 days ago". */
export function shortDate(value) {
  const date = new Date(value)
  return date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

export function shortDateTime(value) {
  const date = new Date(value)
  return `${shortDate(value)}, ${date.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  })}`
}

/** Relative age, for the "what changed" surfaces where recency is the point. */
export function relativeAge(value) {
  const seconds = (Date.now() - new Date(value).getTime()) / 1000
  if (seconds < 90) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 31) return `${days}d ago`
  const months = Math.round(days / 30)
  return months < 12 ? `${months}mo ago` : `${Math.round(months / 12)}y ago`
}

export function confidenceLabel(confidence) {
  if (confidence === null || confidence === undefined) return null
  const percent = Math.round(confidence * 100)
  if (confidence < 0.6) return { text: `AI confidence ${percent}% - verify`, tone: 'low' }
  if (confidence < 0.8) return { text: `AI confidence ${percent}%`, tone: 'medium' }
  return { text: `AI confidence ${percent}%`, tone: 'high' }
}

/** Readable name for a scoring term, used in the "why is this here" popover. */
export const SCORE_TERM_LABEL = {
  recency: 'Recent',
  risk: 'Risk level',
  entities: 'Clinical entities',
  open_actions: 'Unresolved actions',
  learned: 'Learned from this clinic',
  decay: 'Age adjustment',
  manual: 'Marked by a clinician',
  multiplier: 'Adjustment',
}
