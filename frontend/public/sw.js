/**
 * Care Note service worker.
 *
 * A PWA needs one of these to be installable, which is what Phase 5's mobile
 * recording asks for. The interesting decision here is what it must NOT do.
 *
 * The default recipe every offline-first tutorial reaches for is "cache API
 * GETs so the app works on a bad connection". Applied here that would write
 * consult summaries, staff notes, comment threads and transcript segments into
 * the Cache Storage API — an origin-scoped store that survives logout, survives
 * the 60-minute token expiry, and is readable by any script running on the
 * origin. The whole point of D-016 putting the session token in an httpOnly
 * cookie was that an XSS payload should not be able to read durable secrets;
 * caching the clinical data those secrets protect would hand the payload the
 * data directly and skip the theft entirely.
 *
 * So: **anything under /api is network-only and is never written to a cache.**
 * The app shell — the HTML, JS and CSS that contain no patient data — is cached
 * so the interface loads instantly and a recording can be started on a flaky
 * connection. That is the part of offline support that actually matters for
 * ambient capture: the recorder is local, the upload can wait for signal.
 *
 * See DECISIONS.md D-053.
 */

const SHELL_CACHE = 'care-note-shell-v1'

// Only origin-relative, patient-free assets. Vite fingerprints its build output,
// so hashed assets are added on first fetch rather than listed here.
const SHELL_ASSETS = ['/', '/index.html', '/manifest.webmanifest']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
      // A shell asset missing at install time must not wedge the worker.
      .catch(() => self.skipWaiting())
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)))
      )
      .then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', (event) => {
  const { request } = event
  const url = new URL(request.url)

  // Never cache anything that carries patient data, and never serve it from a
  // cache either — a stale clinical record is its own kind of harm.
  if (url.pathname.startsWith('/api')) return

  // Cross-origin and non-GET requests are left entirely alone.
  if (request.method !== 'GET' || url.origin !== self.location.origin) return

  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok && response.type === 'basic') {
            const copy = response.clone()
            caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy))
          }
          return response
        })
        .catch(() => cached)
      // Cache-first for shell assets keeps the recorder reachable offline;
      // the network copy refreshes it for next time.
      return cached || network
    })
  )
})

/**
 * Clear the shell cache on sign-out.
 *
 * The shell holds no patient data, so this is belt-and-braces rather than a
 * control — but a shared clinic tablet is a real deployment and leaving less
 * behind on it costs nothing.
 */
self.addEventListener('message', (event) => {
  if (event.data === 'care-note:signed-out') {
    event.waitUntil(caches.delete(SHELL_CACHE))
  }
})
