/**
 * Revision history: every version, a diff between any two, and revert.
 *
 * The diff arrives from the server as a list of `{op, text}` operations and is
 * rendered as elements. Nothing here builds markup from note content, so a note
 * containing `dose <5mg` diffs like any other line rather than disappearing
 * into an attempted tag (D-015).
 *
 * Revert is presented as what it is — a new version whose content matches an
 * old one. The button says "Restore this version" and the history afterwards
 * shows the restore as its own row, because a clinician needs to be able to see
 * that an undo happened, not just its result.
 */

import { useEffect, useState } from 'react'
import { Api } from '../lib/api'
import { relativeAge, roleLabel, shortDateTime } from '../lib/format'
import { Button, Chip, SectionTitle } from './Primitives'

export default function VersionHistory({ entry, canRevert, onChanged }) {
  const [versions, setVersions] = useState(null)
  const [compare, setCompare] = useState(null)
  const [diff, setDiff] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function load() {
    try {
      const rows = await Api.versions(entry.id)
      setVersions(rows)
      // Default comparison: the previous version against the current one —
      // "what changed since last time I read this" is the common question.
      if (rows.length > 1) setCompare({ from: rows[1].version_number, to: rows[0].version_number })
      setError(null)
    } catch (err) {
      setError(err.message)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.id, entry.version_number])

  useEffect(() => {
    if (!compare) return setDiff(null)
    Api.diff(entry.id, compare.from, compare.to)
      .then(setDiff)
      .catch((err) => setError(err.message))
  }, [entry.id, compare])

  async function revert(versionNumber) {
    setBusy(true)
    try {
      await Api.revert(entry.id, versionNumber)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (versions === null) {
    return <p className="mt-3 text-xs text-slate-500">Loading history…</p>
  }

  return (
    <div className="mt-3 border-t border-slate-200 pt-3">
      <SectionTitle count={versions.length} hint="newest first">
        Revision history
      </SectionTitle>
      {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}

      <ol className="mt-2 space-y-1">
        {versions.map((version) => {
          const isCurrent = version.version_number === entry.version_number
          return (
            <li
              key={version.id}
              className={`rounded border px-2 py-1.5 ${
                isCurrent ? 'border-slate-400 bg-slate-50' : 'border-slate-200'
              }`}
            >
              <div className="flex flex-wrap items-baseline gap-1.5 text-[11px] text-slate-600">
                <span className="font-mono font-semibold text-slate-800">
                  v{version.version_number}
                </span>
                {isCurrent && <Chip tone="good">Current</Chip>}
                {version.reverted_from_version && (
                  <Chip tone="info">Restored from v{version.reverted_from_version}</Chip>
                )}
                <span className="font-medium text-slate-700">
                  {version.edited_by_name || version.edited_by}
                </span>
                <Chip>{roleLabel(version.edited_by_role)}</Chip>
                <span title={shortDateTime(version.edited_at)}>
                  {relativeAge(version.edited_at)}
                </span>
                {version.change_summary && (
                  <span className="italic text-slate-500">“{version.change_summary}”</span>
                )}
                <span className="ml-auto flex gap-1">
                  {versions.length > 1 && (
                    <Button
                      variant="quiet"
                      onClick={() =>
                        setCompare({
                          from: version.version_number,
                          to: entry.version_number,
                        })
                      }
                    >
                      Changes since this
                    </Button>
                  )}
                  {canRevert && !isCurrent && (
                    <Button
                      disabled={busy}
                      onClick={() => revert(version.version_number)}
                      title="Creates a new version with this content; history is kept"
                    >
                      Restore this version
                    </Button>
                  )}
                </span>
              </div>
            </li>
          )
        })}
      </ol>

      {diff && (
        <div className="mt-2 rounded border border-slate-200 bg-white">
          <div className="flex items-baseline justify-between border-b border-slate-200 px-2 py-1 text-[11px] text-slate-600">
            <span>
              Changes from <span className="font-mono">v{diff.from_version}</span> to{' '}
              <span className="font-mono">v{diff.to_version}</span>
            </span>
            <span className="font-mono">
              <span className="text-emerald-700">+{diff.added}</span>{' '}
              <span className="text-rose-700">-{diff.removed}</span>
            </span>
          </div>
          {diff.lines.length ? (
            <div className="max-h-64 overflow-auto p-1 font-mono text-[12px] leading-snug">
              {diff.lines.map((line, index) => (
                <div
                  key={index}
                  className={
                    line.op === 'insert'
                      ? 'bg-emerald-50 text-emerald-900'
                      : line.op === 'delete'
                        ? 'bg-rose-50 text-rose-900 line-through decoration-rose-300'
                        : 'text-slate-600'
                  }
                >
                  <span aria-hidden="true" className="mr-1 select-none text-slate-400">
                    {line.op === 'insert' ? '+' : line.op === 'delete' ? '-' : ' '}
                  </span>
                  {line.text || '\u00a0'}
                </div>
              ))}
            </div>
          ) : (
            <p className="p-2 text-xs text-slate-500">No textual difference between these versions.</p>
          )}
        </div>
      )}
    </div>
  )
}
