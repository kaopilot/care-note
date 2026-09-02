/**
 * What a clinician sees when the app itself fails.
 *
 * Two failures that reach a user mid-consult and that nothing tested before:
 * a render crash, which unmounts the whole tree and leaves a white page, and a
 * session timeout, which is a routine afternoon event because sessions last 60
 * minutes with no refresh flow (D-016).
 *
 * Both were "handled" in the sense that the code did not loop or corrupt
 * anything, and neither told the user what had happened or what was safe. In a
 * clinical record a blank screen is indistinguishable from data loss, which is
 * the assumption these tests exist to stop the interface creating.
 *
 * See DECISIONS.md D-087.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'

import ErrorBoundary from './ErrorBoundary'

function Exploding() {
  throw new Error('note content: Amira Rahman, NRIC S8412345D, penicillin')
}

describe('a render crash', () => {
  beforeEach(() => {
    // React logs caught errors to console.error by design; silence it so the
    // suite output stays readable, and so the spy below measures our logging.
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => vi.restoreAllMocks())

  it('shows a recoverable message instead of a blank page', () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/stopped responding/i)).toBeTruthy()
    expect(screen.getByRole('button', { name: /reload/i })).toBeTruthy()
  })

  it('tells the clinician their saved work survived', () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )
    // The first question during a consult is not "what broke" but "did I lose
    // the note I just wrote".
    expect(screen.getByText(/nothing you saved has been lost/i)).toBeTruthy()
  })

  it('never renders the error message, which can carry note content', () => {
    const { container } = render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )
    const shown = container.textContent
    expect(shown).not.toMatch(/Amira Rahman/)
    expect(shown).not.toMatch(/S8412345D/)
    expect(shown).not.toMatch(/penicillin/)
  })

  it('logs only a type and a reference from our own handler', () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )

    // Our handler passes exactly one object. React's own logging is separate
    // and is asserted against below, because it behaves differently.
    const ours = console.error.mock.calls.filter(
      (call) => call[0] === 'carenote ui crash',
    )
    expect(ours).toHaveLength(1)
    const payload = JSON.stringify(ours[0][1])
    expect(payload).toMatch(/"type":"Error"/)
    expect(payload).not.toMatch(/S8412345D/)
    expect(payload).not.toMatch(/Amira Rahman/)
    expect(payload).not.toMatch(/penicillin/)
  })

  it('documents that React itself logs the raw error in development', () => {
    // KNOWN GAP, asserted rather than assumed. React re-logs a caught error to
    // console.error in development builds, message included — so a throw whose
    // message interpolates note content puts that content in the browser
    // console, and most real deployments forward the console to a third-party
    // error dashboard.
    //
    // We cannot suppress this from inside a boundary. The mitigations are to
    // ship production builds (React logs far less) and to never interpolate
    // record content into an error message in the first place. This test
    // exists so the gap is visible in the suite rather than only in a doc, and
    // it will start failing if React changes the behaviour — which is the
    // moment to revisit the note in DECISIONS.md D-087.
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )
    const everything = console.error.mock.calls
      .flat()
      .map((arg) => (typeof arg === 'string' ? arg : String(arg?.message || '')))
      .join(' ')
    expect(everything).toMatch(/S8412345D/)
  })

  it('gives a reference the clinician can quote', () => {
    render(
      <ErrorBoundary>
        <Exploding />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/^[0-9a-f]{8}$/)).toBeTruthy()
  })

  it('renders children untouched when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>the chart</p>
      </ErrorBoundary>,
    )
    expect(screen.getByText('the chart')).toBeTruthy()
    expect(screen.queryByText(/stopped responding/i)).toBeNull()
  })
})

// --- the network drops, which for a bedside PWA is the ordinary case -------

import { ApiError } from '../lib/api'

describe('losing the network', () => {
  const realFetch = global.fetch
  afterEach(() => {
    global.fetch = realFetch
    vi.restoreAllMocks()
  })

  async function callWith(method) {
    global.fetch = vi.fn().mockRejectedValue(new TypeError('Failed to fetch'))
    const { api } = await import('../lib/api')
    try {
      await api('/patients', method ? { method } : undefined)
      throw new Error('expected the request to fail')
    } catch (err) {
      return err
    }
  }

  it('reports a dropped connection as offline, not as a status code', async () => {
    const err = await callWith()
    expect(err).toBeInstanceOf(ApiError)
    expect(err.offline).toBe(true)
    expect(err.status).toBe(0)
  })

  it('never surfaces the browser’s own wording', async () => {
    // "Failed to fetch" is Chrome-specific, means nothing to a nurse, and on
    // some engines carries the full request URL.
    const err = await callWith()
    expect(err.message).not.toMatch(/failed to fetch/i)
    expect(err.message).not.toMatch(/\/api\//)
  })

  it('tells a writer their text was not saved and is still there', async () => {
    const err = await callWith('POST')
    expect(err.message).toMatch(/not saved/i)
    expect(err.message).toMatch(/still here/i)
  })

  it('reassures a reader that nothing was changed', async () => {
    const err = await callWith('GET')
    expect(err.message).toMatch(/nothing has been changed/i)
  })

  it('does not mistake an offline blip for a session timeout', async () => {
    // A 401 signs the user out. A dropped connection must not, or a nurse in a
    // lift loses her session and retypes a note she already wrote.
    const { onUnauthorized } = await import('../lib/api')
    const signedOut = vi.fn()
    const stop = onUnauthorized(signedOut)
    await callWith('POST')
    stop()
    expect(signedOut).not.toHaveBeenCalled()
  })
})
