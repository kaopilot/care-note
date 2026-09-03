/**
 * One fetch wrapper for the whole client.
 *
 * Two things here are load-bearing rather than convenience:
 *
 * 1. No token is ever held in JavaScript. Login sets an httpOnly cookie
 *    server-side and every request below sends `credentials: 'include'`. The
 *    login response body carries an access_token for non-browser clients; this
 *    client deliberately ignores it, because reading it into state is the first
 *    step toward browser storage, which D-016 rules out.
 *
 * 2. Timing is reported for both segments. `serverMs` comes from the
 *    X-Response-Time-Ms header the API sets; `clientMs` is the full round trip
 *    measured here. The technical brief quotes both, because a latency number
 *    that does not say which segment it covers is not a measurement.
 */

export class ApiError extends Error {
  constructor(message, status, detail, { offline = false } = {}) {
    super(message)
    this.status = status
    this.detail = detail
    // True when the request never reached the server. Distinct from every
    // other failure: nothing was written, so retrying is safe, and the user
    // needs telling that rather than being shown a status code.
    this.offline = offline
  }
}

// Whether the request reached the server at all. `fetch` rejects with a bare
// TypeError for DNS failure, a dropped connection, a blocked request and a
// stopped server alike, so the browser's own message ("Failed to fetch" in
// Chrome, something else in Firefox) is neither stable nor readable by a nurse
// on a ward. This app is a PWA used at the bedside, so losing the network is
// the ordinary case, not the exception, and it deserves a sentence someone can
// act on. See DECISIONS.md D-088.
function offlineError(method) {
  const writing = method && method.toUpperCase() !== 'GET'
  return new ApiError(
    writing
      ? 'Could not reach the server, so this was not saved. Your text is still here — check the connection and try again.'
      : 'Could not reach the server. Check the connection and try again — nothing has been changed.',
    0,
    null,
    { offline: true },
  )
}

async function request(path, options = {}) {
  const started = performance.now()
  // FormData must NOT get an explicit Content-Type: the browser has to set it
  // itself so it can append the multipart boundary. Setting it by hand here
  // produces a body the server cannot parse.
  const isForm = options.body instanceof FormData
  let response
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      credentials: 'include',
      headers: {
        ...(isForm ? {} : { 'Content-Type': 'application/json' }),
        ...(options.headers || {}),
      },
    })
  } catch {
    // Deliberately not re-raising the original: its message is
    // browser-specific and says nothing useful, and on some engines it
    // includes the full request URL.
    throw offlineError(options.method)
  }
  const clientMs = performance.now() - started
  const serverMs = Number(response.headers.get('X-Response-Time-Ms') || 0)

  if (!response.ok) {
    // A 401 means the session ended — the cookie expired, or was cleared.
    // Sessions last 60 minutes with no refresh flow (D-016), so this is a
    // routine afternoon event, not an exception. Announce it once, centrally,
    // rather than letting every caller render its own dead end: the failure
    // the clinician sees otherwise is a red line next to a chart that has
    // silently stopped working.
    //
    // `silent401` exempts the session-restore probe. `me()` runs on every page
    // load and 401s for anyone who has simply never signed in; without this,
    // a first-time visitor would be told their session had timed out.
    if (response.status === 401 && !options.silent401) notifyUnauthorized()

    const body = await response.json().catch(() => ({}))
    const detail = body.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || `Request failed (${response.status})`
    throw new ApiError(message, response.status, detail)
  }
  const data = response.status === 204 ? null : await response.json()
  return { data, clientMs, serverMs }
}

// --- session-expiry notification -----------------------------------------
//
// A tiny listener set rather than a framework: one subscriber (App) today, and
// the alternative is threading a callback through every component that happens
// to make a request.

const unauthorizedListeners = new Set()

export function onUnauthorized(handler) {
  unauthorizedListeners.add(handler)
  return () => unauthorizedListeners.delete(handler)
}

function notifyUnauthorized() {
  for (const handler of unauthorizedListeners) {
    try {
      handler()
    } catch {
      // A broken listener must not turn a session timeout into a crash.
    }
  }
}

export async function api(path, options) {
  const { data } = await request(path, options)
  return data
}

export async function apiTimed(path, options) {
  return request(path, options)
}

const body = (payload) => ({ method: 'POST', body: JSON.stringify(payload) })

export const Api = {
  onUnauthorized,
  me: () => api('/auth/me', { silent401: true }),
  login: (username, password) => api('/auth/login', body({ username, password })),
  logout: () => api('/auth/logout', { method: 'POST' }),

  patients: () => api('/patients'),
  // One patient by id. The 404 this returns for another clinic's patient is the
  // visible half of D-085 — same answer as a nonexistent id, so a clinician here
  // is not told a patient there exists.
  patient: (patientId) => api(`/patients/${patientId}`),
  enrolPatient: (payload) => api('/patients', body(payload)),
  entries: (patientId) => api(`/patients/${patientId}/entries`),
  glance: (patientId) => apiTimed(`/patients/${patientId}/glance`),
  myCare: (patientId) => apiTimed(`/patients/${patientId}/my-care`),

  createEntry: (patientId, payload) => api(`/patients/${patientId}/entries`, body(payload)),
  updateEntry: (entryId, payload) =>
    api(`/entries/${entryId}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  supersede: (entryId, payload) => api(`/entries/${entryId}/supersede`, body(payload)),

  versions: (entryId) => api(`/entries/${entryId}/versions`),
  diff: (entryId, from, to) =>
    api(`/entries/${entryId}/diff?from_version=${from}&to_version=${to}`),
  revert: (entryId, toVersion) =>
    api(`/entries/${entryId}/revert`, body({ to_version: toVersion })),

  comments: (entryId) => api(`/entries/${entryId}/comments`),
  addComment: (entryId, payload) => api(`/entries/${entryId}/comments`, body(payload)),
  resolveComment: (commentId) => api(`/comments/${commentId}/resolve`, { method: 'POST' }),
  unresolveComment: (commentId) => api(`/comments/${commentId}/unresolve`, { method: 'POST' }),

  tasks: (patientId) => api(`/patients/${patientId}/tasks`),
  createTask: (patientId, payload) => api(`/patients/${patientId}/tasks`, body(payload)),
  setTaskStatus: (taskId, status) => api(`/tasks/${taskId}/status`, body({ status })),

  clinicUsers: () => api('/clinic/users'),

  acceptHighlight: (id) => api(`/highlights/${id}/accept`, { method: 'POST' }),
  rejectHighlight: (id) => api(`/highlights/${id}/reject`, { method: 'POST' }),
  manualHighlight: (entryId, payload) => api(`/entries/${entryId}/highlights`, body(payload)),
  resolveProvenance: (pointer) => api(`/provenance?pointer=${encodeURIComponent(pointer)}`),

  learning: () => api('/clinic/learning'),
  rebuildLearning: () => api('/clinic/learning/rebuild', { method: 'POST' }),
  decayPreview: () => api('/clinic/decay/preview'),
  runDecay: (dryRun = true) => api(`/clinic/decay/run?dry_run=${dryRun}`, { method: 'POST' }),
  restoreEntry: (entryId) => api(`/entries/${entryId}/restore`, { method: 'POST' }),
  entryArchive: (entryId) => api(`/entries/${entryId}/archive`),

  // Phase 5 — ambient consult capture. `form` is a FormData carrying either an
  // audio blob or transcript text; the server decides the entry type from the
  // caller's role, so nothing here names one.
  capture: (patientId, form) =>
    api(`/patients/${patientId}/capture`, { method: 'POST', body: form }),
  captures: (patientId) => api(`/patients/${patientId}/captures`),
  capture_detail: (sessionId) => api(`/captures/${sessionId}`),
  attribution: (entryId) => api(`/entries/${entryId}/attribution`),

  scribeTemplates: () => api('/scribe/templates'),
  // `options` carries an AbortSignal so a clinician can abandon a slow model
  // call rather than watching a spinner with a patient in the room (D-070).
  // Regeneration targets an existing session. The server reuses the entry and
  // appends a version, so accepted highlights and comments survive (D-078).
  regenerateScribe: (patientId, interactionType, sessionId) =>
    api(`/patients/${patientId}/scribe`, body({
      interaction_type: interactionType,
      session_id: sessionId,
      regenerate: true,
    })),

  runScribe: (patientId, interactionType, options = {}) =>
    api(`/patients/${patientId}/scribe`, {
      ...body({ interaction_type: interactionType }),
      ...options,
    }),
}
