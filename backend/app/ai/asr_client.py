"""The only module permitted to turn audio into text.

Read this before the rest of Phase 5, because the honest version of ambient
capture starts with an uncomfortable fact.

The problem redaction cannot solve
----------------------------------
Everywhere else in this codebase the rule is "redact before the text leaves".
`llm_client.complete()` enforces it structurally and cannot be bypassed. That
rule **cannot be applied to audio**. `redact_phi()` is regex over text; there is
no regex over a waveform. To redact a consult recording you must first know what
was said, and knowing what was said is transcription. So the ordering is forced:

    audio ──► transcribe ──► redact ──► summarise

which means that whoever transcribes hears the patient say their own name, in
their own voice. A voice is itself biometric identifying data, so the audio is
PHI before a single word of it is recognised.

There is no clever fix, only a choice about who does the transcribing. This
module makes that choice explicit and refuses to make it silently:

* **`stub` (default).** Runs in-process. No audio leaves the machine, ever.
  It cannot really recognise speech, so it says so — every capture it produces
  is flagged `simulated=True` and that flag is carried to every surface that
  displays the note. See DECISIONS.md D-046.
* **`local` (documented, not implemented).** Where a real build belongs:
  faster-whisper or whisper.cpp running beside the API, inside the same trust
  boundary as the database. Same interface as below; only `_LocalWhisper.run`
  is missing, deliberately, rather than half-built.
* **`remote`.** A hosted recogniser. This is the path that ships unredacted
  patient speech to a third party, and it is therefore **refused unless someone
  has explicitly said yes** by setting `CARENOTE_ASR_ALLOW_AUDIO_EGRESS=true`.
  Without that flag the call raises `AudioEgressBlocked` rather than quietly
  falling back to the stub — a security control that degrades into a working
  request teaches everyone to ignore it.

A transcript upload does not come through here at all. Nothing was recognised,
so no recogniser gets to claim it (`services/capture.py` handles that path).

Audio is never persisted
------------------------
Bytes arrive in memory, are handed to a provider, and are dropped when the
request ends. Nothing writes them to disk, to the database, or to a log. The
`CaptureSession` row records how many bytes arrived and that none were kept, so
the claim is auditable rather than merely stated.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.core.audit_logging import log_event
from app.core.config import settings
from app.services.transcripts import Turn

# Containers the browser's MediaRecorder and common upload flows produce.
ACCEPTED_AUDIO_MIME = frozenset(
    {
        "audio/webm",
        "audio/webm;codecs=opus",
        "audio/ogg",
        "audio/ogg;codecs=opus",
        "audio/mp4",
        "audio/mpeg",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/flac",
        "video/webm",  # MediaRecorder on some browsers labels audio-only this way
    }
)

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB — roughly 30 min of Opus at 96kbps

# Nominal Opus bitrate used to ESTIMATE duration from byte count. This is an
# estimate and is labelled as one wherever it is shown; parsing container
# timestamps properly is a real-ASR concern and the real ASR reports duration
# itself.
_NOMINAL_BYTES_PER_SECOND = 12_000


class AudioEgressBlocked(RuntimeError):
    """Raised when a remote recogniser was requested without explicit opt-in."""


class UnsupportedAudio(ValueError):
    """Raised for an empty, oversized, or unrecognised audio payload."""


@dataclass
class Transcription:
    turns: list[Turn]
    provider: str
    model: str
    simulated: bool
    duration_ms: int
    duration_estimated: bool = True
    languages: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Simulated recogniser
# --------------------------------------------------------------------------

# What the stub "hears". EVERY LINE IS INVENTED, and the identifiers below have
# never belonged to anyone — they are planted for the same reason Phase 2's
# fixtures plant them: a redaction boundary nobody has watched work is a claim
# rather than a control.
#
# These deliberately differ from the Phase 2 templates in three ways that only
# matter once audio is involved:
#   * confidence varies per segment, and some segments fall below the 0.6 bar
#     the Glance View already uses to flag "verify this";
#   * two segments overlap in time, because people talk over each other;
#   * speakers code-switch mid-sentence, because in a Singapore or Malaysian
#     clinic they do.
_CLINICAL_CAPTURE: tuple[Turn, ...] = (
    Turn("clinician", "Okay {first}, before we start — can you confirm this is you, "
                      "NRIC {nric}?", 0, 4800, 0.94, "en"),
    Turn("patient", "Yes doctor.", 4600, 5900, 0.91, "en"),
    Turn("clinician", "So the swelling in your ankle. When did it start?", 5900, 9800,
         0.93, "en"),
    Turn("patient", "About four days. The swelling is worst at night — malam paling "
                    "sakit. Cannot sleep properly.", 9800, 17200, 0.62, "en-ms"),
    Turn("clinician", "Any redness, any fever?", 17200, 19400, 0.95, "en"),
    # Romanised Hokkien, code-switched. Ordinary for an older patient in this
    # region, and the build has no vocabulary for it: this turn is transcribed
    # and stored faithfully and then produces no tags, no risk level and no
    # card. It is here so that the "content I could not read" flag has
    # something real to fire on, rather than the gap being invisible in every
    # fixture and therefore in every demo. See D-072.
    Turn("patient", "Bo lah, bo sio joah. Ka joah tioh e kha there thiam thiam, "
                    "bo hoat tou khun.", 19400, 22600, 0.48, "nan"),
    Turn("patient", "No fever. But the skin over the swelling feels tight, like mahu "
                    "pecah.", 22600, 25100, 0.54, "en-ms"),
    Turn("clinician", "Are you still on the amlodipine five milligrams?",
         25100, 28900, 0.88, "en"),
    Turn("patient", "Yes, every morning. Never miss.", 28900, 31600, 0.9, "en"),
    Turn("clinician", "Ankle swelling is a known side effect of amlodipine. I want to "
                      "switch you to losartan fifty milligrams daily and review in two "
                      "weeks.", 31600, 41000, 0.86, "en"),
    Turn("patient", "Is it because of the medicine ah? I thought it was the walking.",
         40200, 45500, 0.71, "en"),
    Turn("clinician", "Most likely the medicine. Stop the amlodipine from tomorrow. "
                      "I'll arrange a blood pressure check with the nurse next week.",
         45500, 54000, 0.89, "en"),
    Turn("clinician", "If the swelling goes up your calf or you get chest pain, come "
                      "straight in. Clinic line is {phone}.", 54000, 62000, 0.83, "en"),
)

_PATIENT_CAPTURE: tuple[Turn, ...] = (
    Turn("patient", "I'm recording this so I don't forget what the doctor said. "
                    "It's {name}.", 0, 6200, 0.87, "en"),
    Turn("patient", "He said the swelling in my ankle is from the blood pressure "
                    "tablet, the amlodipine one.", 6200, 13000, 0.79, "en"),
    Turn("patient", "I need to stop that one from tomorrow and start a new one, "
                    "losartan I think. Fifty milligrams.", 13000, 21500, 0.58, "en"),
    Turn("patient", "There's a blood pressure check with the nurse next week. I have to "
                    "call to book it, on {phone}.", 21500, 29000, 0.76, "en"),
    Turn("patient", "And if it goes up my leg or I get chest pain I should come in "
                    "straight away.", 29000, 35500, 0.81, "en"),
    Turn("patient", "I want to ask next time whether I still need to take it if my "
                    "pressure is already okay.", 35500, 43000, 0.69, "en"),
)

# Synthetic identifiers, format-valid so the redaction patterns genuinely fire.
_STUB_NRIC = "S8412345D"
_STUB_PHONE = "+65 6123 4567"


def _fill(text: str, *, name: str, nric: str, phone: str) -> str:
    return (
        text.replace("{name}", name)
        .replace("{first}", name.split()[0])
        .replace("{nric}", nric)
        .replace("{phone}", phone)
    )


def _scale_to_duration(turns: list[Turn], duration_ms: int) -> list[Turn]:
    """Stretch or squeeze the fixture onto the real recording's length.

    Without this a thirty-second recording and a five-minute one produce
    identical timings, and the timestamps beside each segment would be visibly
    fictional the moment anyone checked them against the audio they just made.
    They remain simulated either way — but they should at least not contradict
    the one real measurement we have.
    """
    if not turns or duration_ms <= 0:
        return turns
    span = max(turn.end_ms for turn in turns)
    if span <= 0:
        return turns
    factor = duration_ms / span
    return [
        Turn(
            speaker=turn.speaker,
            text=turn.text,
            start_ms=int(turn.start_ms * factor),
            end_ms=int(turn.end_ms * factor),
            confidence=turn.confidence,
            language=turn.language,
        )
        for turn in turns
    ]


class _SimulatedProvider:
    """In-process, offline, and honest about being unable to hear.

    Deterministic on the audio's digest so the same recording always produces
    the same transcript — a demo that changed its story on every replay would
    be useless for showing provenance.
    """

    name = "stub"
    model = "simulated-asr-v1"
    simulated = True

    def run(self, audio: bytes, *, kind: str, patient_name: str, duration_ms: int):
        digest = hashlib.sha256(audio).hexdigest()
        template = _PATIENT_CAPTURE if kind == "patient" else _CLINICAL_CAPTURE
        turns = [
            Turn(
                speaker=turn.speaker,
                text=_fill(
                    turn.text, name=patient_name, nric=_STUB_NRIC, phone=_STUB_PHONE
                ),
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                confidence=turn.confidence,
                language=turn.language,
            )
            for turn in template
        ]
        # The digest picks how much of the fixture a short recording "reaches",
        # so two different files do not produce byte-identical notes.
        if duration_ms and duration_ms < 20_000:
            keep = max(3, (int(digest[:2], 16) % len(turns)) or 3)
            turns = turns[:keep]
        return _scale_to_duration(turns, duration_ms)


class _LocalWhisper:
    """Where a production build puts speech recognition: in-process, or on a
    sidecar inside the same trust boundary as the database.

    Deliberately unimplemented rather than half-implemented. Wiring
    faster-whisper here is a model download and a `transcribe()` call; what it
    is not is a thing to claim in a brief without having run it. The interface
    is fixed so that adding it changes this class and nothing else.
    """

    name = "local"
    model = settings.asr_model
    simulated = False

    def run(self, audio: bytes, *, kind: str, patient_name: str, duration_ms: int):
        raise NotImplementedError(
            "CARENOTE_ASR_PROVIDER=local is the documented production path but is "
            "not implemented in this prototype. Use the default stub provider, or "
            "upload a transcript, which needs no recogniser at all."
        )


class _RemoteProvider:
    """A hosted recogniser — the path that sends patient speech off-box.

    Reachable only with `CARENOTE_ASR_ALLOW_AUDIO_EGRESS=true`. The gate is in
    `transcribe()` below rather than here so that it cannot be skipped by
    instantiating this class directly.
    """

    name = "remote"
    model = settings.asr_model
    simulated = False

    def run(self, audio: bytes, *, kind: str, patient_name: str, duration_ms: int):
        raise NotImplementedError(
            "No remote ASR vendor is configured in this prototype. The egress gate "
            "is implemented and tested; the vendor call deliberately is not."
        )


def _provider():
    choice = settings.asr_provider
    if choice == "local":
        return _LocalWhisper()
    if choice == "remote":
        return _RemoteProvider()
    return _SimulatedProvider()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def estimate_duration_ms(audio: bytes) -> int:
    """Rough duration from byte count. Labelled an estimate everywhere shown."""
    if not audio:
        return 0
    return int(len(audio) / _NOMINAL_BYTES_PER_SECOND * 1000)


def transcribe(
    audio: bytes,
    *,
    mime: str | None,
    kind: str,
    patient_name: str,
    duration_ms: int | None = None,
    actor_id: str | None = None,
    clinic_id: str | None = None,
) -> Transcription:
    """Audio in, speaker-labelled turns out. The only way to transcribe here.

    `duration_ms` is the browser's own measurement when it has one; MediaRecorder
    knows how long it recorded far better than we can infer from byte count.
    """
    if not audio:
        raise UnsupportedAudio("No audio received")
    if len(audio) > MAX_AUDIO_BYTES:
        raise UnsupportedAudio(
            f"Audio exceeds the {MAX_AUDIO_BYTES // (1024 * 1024)}MB limit"
        )
    normalised = (mime or "").split(";")[0].strip().lower()
    if normalised and normalised not in {m.split(";")[0] for m in ACCEPTED_AUDIO_MIME}:
        raise UnsupportedAudio(f"Unsupported audio type: {mime}")

    provider = _provider()

    # The gate. A remote recogniser receives speech that redaction has not
    # touched and cannot touch, so it needs a deliberate act to enable — and it
    # fails closed rather than degrading to the stub, because a control that
    # silently downgrades is one nobody will ever notice is off.
    if provider.name == "remote" and not settings.asr_allow_audio_egress:
        log_event(
            actor_id=actor_id,
            action="asr.blocked_audio_egress",
            target_type="capture",
            clinic_id=clinic_id,
            metadata={"provider": provider.name, "bytes": len(audio)},
        )
        raise AudioEgressBlocked(
            "Refusing to send un-redacted patient audio to a third-party "
            "recogniser. Audio cannot be redacted before transcription. Set "
            "CARENOTE_ASR_ALLOW_AUDIO_EGRESS=true only if that transfer is "
            "covered by a data processing agreement."
        )

    measured = duration_ms if duration_ms and duration_ms > 0 else None
    effective_duration = measured or estimate_duration_ms(audio)

    # Metadata only. The audio is not logged, and neither is anything it said.
    log_event(
        actor_id=actor_id,
        action="asr.transcribe",
        target_type="capture",
        clinic_id=clinic_id,
        metadata={
            "provider": provider.name,
            "model": provider.model,
            "simulated": provider.simulated,
            "bytes": len(audio),
            "duration_ms": effective_duration,
            "audio_retained": False,
        },
    )

    turns = provider.run(
        audio, kind=kind, patient_name=patient_name, duration_ms=effective_duration
    )
    languages = sorted({turn.language for turn in turns if turn.language})

    return Transcription(
        turns=turns,
        provider=provider.name,
        model=provider.model,
        simulated=provider.simulated,
        duration_ms=effective_duration,
        duration_estimated=measured is None,
        languages=languages,
    )
