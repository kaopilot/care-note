/**
 * Ambient consult capture.
 *
 * Three ways in, deliberately, because a demo that depends on a working
 * microphone is a demo that fails in the room it matters in:
 *
 *   1. record here (MediaRecorder, mobile or laptop),
 *   2. upload an audio file,
 *   3. paste or upload a transcript — no recogniser involved at all.
 *
 * All three post to the same endpoint and produce the same kind of entry. The
 * server decides what kind of encounter it was from the caller's role, so this
 * component never sends an entry type and could not forge one if it tried.
 *
 * What this component promises the user, it promises accurately:
 * the recording is uploaded, transcribed, redacted and dropped. It is not kept,
 * and it is never written to browser storage — the Blob lives in a ref for the
 * lifetime of the page and dies with it.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Api } from '../lib/api'
import { Button, Chip } from './Primitives'

const MIME_PREFERENCES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/mp4',
]

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return null
  return MIME_PREFERENCES.find((type) => MediaRecorder.isTypeSupported?.(type)) || null
}

function formatClock(ms) {
  const total = Math.floor(ms / 1000)
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
}

export default function VoiceCapture({ patientId, kind, onCaptured, disabled }) {
  const [state, setState] = useState('idle') // idle | recording | ready | sending
  const [elapsed, setElapsed] = useState(0)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const [showTranscript, setShowTranscript] = useState(false)
  const [transcript, setTranscript] = useState('')
  const [supported, setSupported] = useState(true)

  const recorderRef = useRef(null)
  const chunksRef = useRef([])
  const blobRef = useRef(null)
  const streamRef = useRef(null)
  const startedRef = useRef(0)
  const tickRef = useRef(null)

  const isPatient = kind === 'patient'

  useEffect(() => {
    setSupported(
      typeof navigator !== 'undefined' &&
        Boolean(navigator.mediaDevices?.getUserMedia) &&
        Boolean(pickMimeType())
    )
  }, [])

  const releaseMicrophone = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (tickRef.current) {
      clearInterval(tickRef.current)
      tickRef.current = null
    }
  }, [])

  // The microphone must not outlive the component. Without this, navigating
  // away mid-consult leaves the recording indicator lit and the room live.
  useEffect(() => releaseMicrophone, [releaseMicrophone])

  async function startRecording() {
    setError(null)
    setNotice(null)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      streamRef.current = stream
      const mimeType = pickMimeType()
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      chunksRef.current = []
      recorder.ondataavailable = (event) => {
        if (event.data?.size) chunksRef.current.push(event.data)
      }
      recorder.onstop = () => {
        blobRef.current = new Blob(chunksRef.current, {
          type: mimeType || 'audio/webm',
        })
        chunksRef.current = []
        releaseMicrophone()
        setState('ready')
      }
      recorderRef.current = recorder
      startedRef.current = Date.now()
      recorder.start(1000)
      setState('recording')
      setElapsed(0)
      tickRef.current = setInterval(
        () => setElapsed(Date.now() - startedRef.current),
        250
      )
    } catch (err) {
      releaseMicrophone()
      setState('idle')
      setError(
        err?.name === 'NotAllowedError'
          ? 'Microphone permission was refused. You can upload a file or paste a transcript instead.'
          : `Could not start recording: ${err.message}`
      )
    }
  }

  function stopRecording() {
    recorderRef.current?.stop()
  }

  function discard() {
    blobRef.current = null
    setState('idle')
    setElapsed(0)
    setNotice(null)
  }

  async function submit({ file, text, source }) {
    setState('sending')
    setError(null)
    setNotice(null)
    const form = new FormData()
    form.append('kind', kind)
    form.append('source', source)
    if (file) {
      form.append('audio', file, file.name || 'consult.webm')
      // The browser knows how long it recorded far better than the server can
      // infer from a byte count, so it sends the measurement it actually has.
      if (source === 'live_recording') form.append('duration_ms', String(elapsed))
      form.append(
        'device_label',
        `${navigator.platform || 'unknown device'} · ${
          navigator.userAgent.includes('Mobile') ? 'mobile' : 'desktop'
        }`
      )
    } else {
      form.append('transcript', text)
    }

    try {
      const result = await Api.capture(patientId, form)
      blobRef.current = null
      setTranscript('')
      setShowTranscript(false)
      setState('idle')
      setElapsed(0)
      setNotice(result.message)
      onCaptured?.(result)
    } catch (err) {
      setState('idle')
      setError(err.message)
    }
  }

  const busy = disabled || state === 'sending'

  return (
    <div
      className={`rounded border p-2 ${
        isPatient ? 'border-slate-300 bg-white' : 'border-slate-200 bg-white'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <label
          className={
            isPatient
              ? 'text-sm font-semibold text-slate-900'
              : 'text-[11px] font-medium uppercase tracking-wider text-slate-500'
          }
        >
          {isPatient ? 'Record your appointment' : 'Ambient consult capture'}
        </label>
        {state === 'recording' && (
          <span className="inline-flex items-center gap-1.5 rounded bg-rose-50 px-1.5 py-0.5 text-[11px] font-semibold text-rose-800 ring-1 ring-rose-300">
            <span className="h-2 w-2 animate-pulse rounded-full bg-rose-600" aria-hidden="true" />
            Recording {formatClock(elapsed)}
          </span>
        )}
        {state === 'sending' && <Chip tone="info">Transcribing and summarising…</Chip>}
      </div>

      <p
        className={`mt-1 ${
          isPatient ? 'text-sm text-slate-600' : 'text-[11px] text-slate-500'
        }`}
      >
        {isPatient
          ? 'Names and contact details are removed before anything is processed. The recording itself is not kept — only the summary your care team reads.'
          : 'Audio is transcribed, redacted and discarded. Identifiers are stripped before any text reaches a model; the recording is never stored.'}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-1">
        {state === 'idle' && supported && (
          <Button variant="primary" disabled={busy} onClick={startRecording}>
            ● Start recording
          </Button>
        )}
        {state === 'recording' && (
          <Button variant="primary" onClick={stopRecording}>
            ■ Stop
          </Button>
        )}
        {state === 'ready' && (
          <>
            <span className="text-[11px] text-slate-600">
              {formatClock(elapsed)} recorded
            </span>
            <Button
              variant="primary"
              disabled={busy}
              onClick={() =>
                submit({
                  file: new File([blobRef.current], 'consult.webm', {
                    type: blobRef.current?.type || 'audio/webm',
                  }),
                  source: 'live_recording',
                })
              }
            >
              Transcribe and add to record
            </Button>
            <Button variant="quiet" onClick={discard}>
              Discard
            </Button>
          </>
        )}

        {state === 'idle' && (
          <>
            <label className="cursor-pointer rounded px-2 py-1 text-xs font-medium text-slate-800 ring-1 ring-slate-300 transition hover:bg-slate-50">
              Upload audio
              <input
                type="file"
                accept="audio/*"
                className="hidden"
                disabled={busy}
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) submit({ file, source: 'audio_upload' })
                }}
              />
            </label>
            <Button variant="quiet" onClick={() => setShowTranscript((open) => !open)}>
              {showTranscript ? 'Hide transcript entry' : 'Paste a transcript'}
            </Button>
          </>
        )}
      </div>

      {!supported && state === 'idle' && (
        <p className="mt-1 text-[11px] text-slate-500">
          This browser cannot record audio. Upload a file or paste a transcript instead.
        </p>
      )}

      {showTranscript && (
        <div className="mt-2">
          <textarea
            className="w-full rounded border border-slate-300 p-2 font-mono text-[12px]"
            rows={4}
            value={transcript}
            onChange={(event) => setTranscript(event.target.value)}
            placeholder={
              isPatient
                ? 'patient: the doctor said to stop the amlodipine\npatient: blood pressure check next week'
                : 'clinician: how long has the ankle been swollen?\npatient: about four days\nclinician: switch to losartan 50mg daily'
            }
          />
          <p className="mt-1 text-[11px] text-slate-500">
            One line per turn, <span className="font-mono">speaker: what they said</span>.
            Timestamps like <span className="font-mono">[00:12]</span> and a JSON array of
            turns are both accepted.
          </p>
          <Button
            variant="primary"
            className="mt-1"
            disabled={busy || !transcript.trim()}
            onClick={() => submit({ text: transcript, source: 'transcript_upload' })}
          >
            Add to record
          </Button>
        </div>
      )}

      {notice && (
        <p className="mt-1.5 rounded bg-emerald-50 px-2 py-1 text-[12px] text-emerald-900 ring-1 ring-emerald-200">
          {notice}
        </p>
      )}
      {error && <p className="mt-1.5 text-xs text-rose-700">{error}</p>}
    </div>
  )
}
