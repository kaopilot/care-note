/**
 * A render crash must not take the whole chart with it.
 *
 * React unmounts the entire tree when a component throws during render, so
 * without a boundary any bug in any card gives a clinician a white page —
 * mid-consult, with a patient in the room, and no indication of whether their
 * last note saved. There is no worse failure mode in this product: a visible
 * error is recoverable, a blank screen is indistinguishable from data loss.
 *
 * Two deliberate choices about what this shows.
 *
 * **It says what is safe.** The reassurance a clinician needs first is not
 * "something went wrong", it is "nothing you typed was lost and the record is
 * intact". Writes are server-side and committed before the response returns, so
 * a render crash cannot have discarded a saved note, and saying so is honest.
 *
 * **It carries no error text.** `error.message` can hold anything the throwing
 * code put in it — including entry content interpolated into a template
 * string — so it is never rendered and never logged. This is the same rule as
 * the backend's crash handler (D-071): type and reference only. Rendering a
 * stack trace onto the screen of a shared consult-room laptop is its own PHI
 * leak, and one that happens in front of the patient.
 *
 * See DECISIONS.md D-087.
 */

import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { crashed: false, reference: null }
  }

  static getDerivedStateFromError() {
    // No error object kept in state — see the module note on why.
    return {
      crashed: true,
      reference: Math.random().toString(16).slice(2, 10),
    }
  }

  componentDidCatch(error) {
    // Console only, name only. Never the message, never the component stack:
    // both can carry interpolated note content, and a browser console is
    // forwarded to third-party error dashboards in most real deployments.
    console.error('carenote ui crash', {
      type: error?.name || 'Error',
      reference: this.state.reference,
    })
  }

  render() {
    if (!this.state.crashed) return this.props.children

    return (
      <div className="mx-auto mt-10 max-w-lg rounded border border-rose-300 bg-rose-50 p-4">
        <h2 className="text-sm font-semibold text-rose-900">
          This screen stopped responding
        </h2>
        <p className="mt-2 text-sm text-rose-900">
          Nothing you saved has been lost. Notes and comments are written to the
          record as you save them, so the patient&rsquo;s history is intact — it is
          this view that failed, not the data behind it.
        </p>
        <p className="mt-2 text-sm text-rose-900">
          Reload to carry on. If it happens again, quote reference{' '}
          <span className="font-mono">{this.state.reference}</span> to whoever
          supports this system.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-3 rounded bg-rose-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-800"
        >
          Reload this page
        </button>
      </div>
    )
  }
}
