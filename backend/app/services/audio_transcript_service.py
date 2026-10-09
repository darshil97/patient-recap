"""
Turning a stored consultation recording into the visit's transcript.

    recording --Deepgram--> diarized utterances ("Speaker 0", "Speaker 1", ...)
              --LLM-------> which voice is the doctor, the patient, or someone else
              --Python----> DOCTOR: / PATIENT: / OTHER (...): lines
              --storage---> transcripts/{visit_id}.txt, exactly like a .txt upload

The output is indistinguishable from an uploaded text transcript, so everything
downstream — reading it back, the three-layer chain, deleting it — needs no audio
special case.

How the recording arrives is not this module's concern: audio_upload_service
receives it in chunks, stitches and stores it, then calls `transcribe_into_visit`
with a stream over the stored file. Keeping the two apart is what will let an
in-browser recorder reuse this unchanged.

Ordering is chosen so that nothing is paid for that cannot be stored: the visit
rules run first, the model client is built before Deepgram is called so a missing
LLM key fails up front, and the transcript size ceiling is checked before the
identification call. The visit is re-read and re-checked just before writing,
because a recording takes long enough to process that the visit can change
underneath it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import BinaryIO, List, Optional

import psycopg

from app.config import get_settings
from app.core.errors import AppError, QuotaExhaustedError, TranscriptError
from app.db.models import VisitRow
from app.db.tenancy import DoctorScope
from app.domain.audio import content_type_for
from app.domain.diarization import format_transcript, merge_turns, render_anonymous, turn_counts
from app.llm.base import ChatClient
from app.llm.errors import is_quota_failure
from app.llm.factory import build_llm
from app.pipeline.speakers import identify_speakers
from app.services import transcript_service
from app.services.insight_service import patient_context
from app.services.visit_service import get_visit
from app.speech import deepgram

logger = logging.getLogger(__name__)


@dataclass
class SpeakerSummary:
    """One voice and who it was identified as."""

    speaker: int
    role: str
    label: str
    confidence: str
    turns: int


@dataclass
class AudioTranscriptResult:
    """What transcribing a recording produced."""

    visit: VisitRow
    speakers: List[SpeakerSummary] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    duration_seconds: Optional[float] = None


def transcribe_into_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    *,
    audio: BinaryIO,
    size: int,
    extension: str,
    filename: Optional[str],
    replace: bool = False,
    doctor_name: Optional[str] = None,
    llm: Optional[ChatClient] = None,
) -> AudioTranscriptResult:
    """Transcribe a recording, label its speakers, and store it as the transcript.

    Scoped through get_visit exactly like a text upload, so a recording cannot be
    attached to another doctor's visit. `audio` is a binary stream positioned at
    the start and `size` its exact length; it is streamed to Deepgram, never read
    into memory whole. `extension` must already be one of the accepted ones.
    """
    settings = get_settings()

    visit = get_visit(conn, scope, visit_id)
    transcript_service.check_can_attach(visit, replace)

    # Built before Deepgram is called: a missing LLM key should fail before the
    # recording has been paid for, not after.
    client = llm or build_llm()

    diarized = deepgram.transcribe(audio, content_type=content_type_for(extension),
                                   size=size, config=settings.speech.deepgram)

    turns = merge_turns(diarized.utterances)
    if not turns:
        raise TranscriptError("No speech was detected in this recording.",
                              details={"duration_seconds": diarized.duration_seconds})
    if len({t.speaker for t in turns}) < 2:
        # Nothing for the model to decide, and nothing to label: one voice means
        # the conversation was not separated (or there was no conversation).
        raise TranscriptError(
            "Only one voice was detected in this recording, so the doctor and the "
            "patient cannot be told apart. Check the recording, or upload a text "
            "transcript instead.",
            details={"duration_seconds": diarized.duration_seconds})

    # Same ceiling as an uploaded .txt, checked before paying for the model call.
    # The labelled transcript is no longer than this one plus short labels.
    transcript_service.check_transcript_text(render_anonymous(turns))

    try:
        identification = identify_speakers(
            turns, patient_context(visit), client, settings.llm.prompts_dir,
            doctor_name=doctor_name)
    except AppError:
        raise
    except Exception as exc:
        if is_quota_failure(exc):
            raise QuotaExhaustedError(str(exc)) from exc
        raise

    text = transcript_service.check_transcript_text(
        format_transcript(turns, identification.roles))

    # Processing a recording takes long enough for the visit to change meanwhile
    # (a text transcript attached, the visit cancelled), so re-check before writing.
    visit = get_visit(conn, scope, visit_id)
    transcript_service.check_can_attach(visit, replace)
    updated = transcript_service.save_transcript_text(conn, scope, visit, text, filename)

    counts = turn_counts(turns)
    speakers = [
        SpeakerSummary(speaker=role.speaker, role=role.role, label=role.label,
                       confidence=role.confidence, turns=counts.get(role.speaker, 0))
        for role in sorted(identification.roles.values(), key=lambda r: r.speaker)
    ]
    logger.info("Labelled %d voices for %s as %s (%d warnings, model=%s)",
                len(speakers), visit.label,
                ", ".join(s.label for s in speakers),
                len(identification.warnings), identification.model)

    return AudioTranscriptResult(
        visit=updated,
        speakers=speakers,
        warnings=list(identification.warnings),
        duration_seconds=diarized.duration_seconds,
    )
