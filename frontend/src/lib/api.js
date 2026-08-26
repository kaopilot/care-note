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
  constructor(message, status, detail) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request(path, options = {}) {
  const started = performance.now()
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  const clientMs = performance.now() - started
  const serverMs = Number(response.headers.get('X-Response-Time-Ms') || 0)

  if (!response.ok) {
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

export async function api(path, options) {
  const { data } = await request(path, options)
  return data
}

export async function apiTimed(path, options) {
  return request(path, options)
}

const body = (payload) => ({ method: 'POST', body: JSON.stringify(payload) })

export const Api = {
  me: () => api('/auth/me'),
  login: (username, password) => api('/auth/login', body({ username, password })),
  logout: () => api('/auth/logout', { method: 'POST' }),

  patients: () => api('/patients'),
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

  scribeTemplates: () => api('/scribe/templates'),
  runScribe: (patientId, interactionType) =>
    api(`/patients/${patientId}/scribe`, body({ interaction_type: interactionType })),
}
