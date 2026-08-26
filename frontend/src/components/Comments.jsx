/**
 * Threaded comments on one entry, plus the "assign to staff" affordance.
 *
 * Mentions are chosen from a picker rather than typed and pattern-matched. The
 * client sends user ids alongside the text; the server validates them against
 * its own clinic and silently drops anything that does not resolve. That means
 * a mention which renders as a mention is one that actually reached someone —
 * the styling is not a promise the system cannot keep.
 *
 * Bodies render through `MentionText`, which slices plain text into React
 * nodes. No markup is constructed anywhere in this file.
 */

import { useEffect, useState } from 'react'
import { Api } from '../lib/api'
import { relativeAge, roleLabel } from '../lib/format'
import { Button, Chip, MentionText, SectionTitle } from './Primitives'

export default function Comments({ entry, users, onChanged }) {
  const [threads, setThreads] = useState(null)
  const [draft, setDraft] = useState('')
  const [mentions, setMentions] = useState([])
  const [replyTo, setReplyTo] = useState(null)
  const [taskDraft, setTaskDraft] = useState('')
  const [assignee, setAssignee] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const usernames = users.map((user) => user.username)

  async function load() {
    try {
      setThreads(await Api.comments(entry.id))
      setError(null)
    } catch (err) {
      setError(err.message)
      setThreads([])
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry.id])

  function toggleMention(user) {
    setMentions((prev) =>
      prev.includes(user.id) ? prev.filter((id) => id !== user.id) : [...prev, user.id]
    )
    // Insert the handle into the text too, so the author can see what they are
    // about to send rather than trusting an invisible list.
    setDraft((prev) =>
      prev.includes(`@${user.username}`) ? prev : `${prev}${prev ? ' ' : ''}@${user.username} `
    )
  }

  async function submit() {
    if (!draft.trim()) return
    setBusy(true)
    try {
      await Api.addComment(entry.id, {
        body: draft,
        mentions,
        parent_comment_id: replyTo,
      })
      setDraft('')
      setMentions([])
      setReplyTo(null)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  async function toggleResolved(comment) {
    try {
      if (comment.status === 'open') await Api.resolveComment(comment.id)
      else await Api.unresolveComment(comment.id)
      await load()
      onChanged?.()
    } catch (err) {
      setError(err.message)
    }
  }

  async function assign() {
    if (!taskDraft.trim()) return
    setBusy(true)
    try {
      await Api.createTask(entry.patient_id, {
        description: taskDraft,
        assigned_to: assignee || null,
        entry_id: entry.id,
      })
      setTaskDraft('')
      setAssignee('')
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  const renderComment = (comment, depth = 0) => (
    <li key={comment.id} className={depth ? 'ml-5 border-l border-slate-200 pl-3' : ''}>
      <div className="rounded border border-slate-200 bg-white p-2">
        <div className="flex flex-wrap items-baseline gap-1.5 text-[11px] text-slate-500">
          <span className="font-medium text-slate-700">{comment.author_name}</span>
          <Chip>{roleLabel(comment.author_role)}</Chip>
          <span>{relativeAge(comment.created_at)}</span>
          {comment.status === 'resolved' && (
            <Chip tone="good">✓ Resolved by {comment.resolved_by_name}</Chip>
          )}
        </div>
        <div className="mt-1">
          <MentionText body={comment.body} usernames={usernames} />
        </div>
        <div className="mt-1 flex gap-2">
          <Button variant="quiet" onClick={() => setReplyTo(comment.id)}>
            Reply
          </Button>
          <Button variant="quiet" onClick={() => toggleResolved(comment)}>
            {comment.status === 'open' ? 'Mark resolved' : 'Reopen'}
          </Button>
        </div>
      </div>
      {comment.replies?.length > 0 && (
        <ul className="mt-1 space-y-1">
          {comment.replies.map((reply) => renderComment(reply, depth + 1))}
        </ul>
      )}
    </li>
  )

  return (
    <div className="mt-3 border-t border-slate-200 pt-3">
      <SectionTitle count={threads?.length}>Discussion</SectionTitle>
      {error && <p className="mt-1 text-xs text-rose-700">{error}</p>}

      {threads === null ? (
        <p className="mt-1 text-xs text-slate-500">Loading discussion…</p>
      ) : threads.length ? (
        <ul className="mt-2 space-y-2">{threads.map((comment) => renderComment(comment))}</ul>
      ) : (
        <p className="mt-1 text-xs text-slate-500">
          No discussion on this entry yet. Comments here are internal and are never
          shown to the patient.
        </p>
      )}

      <div className="mt-3 rounded border border-slate-200 bg-slate-50 p-2">
        {replyTo && (
          <p className="mb-1 text-[11px] text-slate-600">
            Replying in thread ·{' '}
            <button className="underline" onClick={() => setReplyTo(null)}>
              cancel
            </button>
          </p>
        )}
        <textarea
          className="w-full rounded border border-slate-300 p-2 text-sm"
          rows={2}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="Add a comment. Mention a colleague to bring them in."
        />
        <div className="mt-1 flex flex-wrap items-center gap-1">
          <span className="text-[11px] text-slate-500">Mention:</span>
          {users.map((user) => (
            <button
              key={user.id}
              onClick={() => toggleMention(user)}
              className={`rounded px-1.5 py-0.5 text-[11px] ring-1 ${
                mentions.includes(user.id)
                  ? 'bg-slate-900 text-white ring-slate-900'
                  : 'bg-white text-slate-700 ring-slate-300 hover:bg-slate-100'
              }`}
            >
              @{user.username}
            </button>
          ))}
          <Button
            variant="primary"
            className="ml-auto"
            disabled={busy || !draft.trim()}
            onClick={submit}
          >
            {busy ? 'Posting…' : 'Post comment'}
          </Button>
        </div>
      </div>

      <div className="mt-2 rounded border border-slate-200 bg-white p-2">
        <SectionTitle>Assign a task</SectionTitle>
        <div className="mt-1 flex flex-wrap gap-1">
          <input
            className="min-w-[12rem] flex-1 rounded border border-slate-300 px-2 py-1 text-sm"
            value={taskDraft}
            onChange={(event) => setTaskDraft(event.target.value)}
            placeholder="What needs doing, e.g. book monofilament testing"
          />
          <select
            className="rounded border border-slate-300 px-2 py-1 text-xs"
            value={assignee}
            onChange={(event) => setAssignee(event.target.value)}
          >
            <option value="">Unassigned</option>
            {users.map((user) => (
              <option key={user.id} value={user.id}>
                {user.name} ({user.role})
              </option>
            ))}
          </select>
          <Button disabled={busy || !taskDraft.trim()} onClick={assign}>
            Assign
          </Button>
        </div>
      </div>
    </div>
  )
}
