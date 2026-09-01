/**
 * The human gate on patient-facing dosages, made clickable.
 *
 * The server refuses a patient-facing write carrying an order-of-magnitude dose
 * (D-079). This is what the clinician sees when that happens. Three things it
 * deliberately does:
 *
 *   1. Names the drug, the figure written, and the expected range — so the
 *      clinician can tell in one read whether it is a typo or a decision.
 *   2. Puts "Go back and check" first, and styles it as the primary action.
 *      Confirm is available and secondary; a dialog whose obvious button is
 *      "proceed" is a speed bump, not a gate.
 *   3. Says plainly that confirming is recorded. A gate nobody can see the far
 *      side of is not a gate, and knowing the override is attributable is most
 *      of what makes someone stop and reread.
 */

export default function DosageConfirm({ detail, onConfirm, onCancel, busy }) {
  const findings = detail?.findings || []

  return (
    <div
      className="mt-2 rounded border-2 border-rose-500 bg-rose-50 p-3"
      role="alertdialog"
      aria-label="Dosage needs confirmation"
    >
      <p className="text-sm font-semibold text-rose-950">
        This is going to the patient — check the dose first
      </p>

      <ul className="mt-2 space-y-1.5">
        {findings.map((finding, index) => (
          <li
            key={`${finding.drug}-${index}`}
            className="rounded bg-white px-2 py-1.5 ring-1 ring-rose-300"
          >
            <span className="text-xs font-semibold text-slate-900">
              {finding.stated} {finding.drug}
            </span>
            <span className="ml-2 font-mono text-[11px] text-slate-500">
              usual adult range {finding.expected_low_mg}–{finding.expected_high_mg} mg
            </span>
          </li>
        ))}
      </ul>

      <p className="mt-2 text-[11px] leading-snug text-rose-900">
        Nothing has been saved. If this is deliberate — a specialist regimen, or a
        dose you have checked against the source — you can confirm it. Your
        confirmation is recorded against this entry.
      </p>

      <div className="mt-2.5 flex flex-wrap gap-2">
        <button
          onClick={onCancel}
          disabled={busy}
          className="rounded bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          Go back and check
        </button>
        <button
          onClick={onConfirm}
          disabled={busy}
          className="rounded border border-rose-400 px-3 py-1.5 text-xs text-rose-900 hover:border-rose-600 hover:bg-rose-100 disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'I have checked — confirm and save'}
        </button>
      </div>
    </div>
  )
}
