"""Transcript response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TranscriptOut(BaseModel):
    """A transcript read back so a brief can be checked against its source.

    No `format` field: transcripts are plain text, and there is only one format.
    """

    model_config = ConfigDict(from_attributes=True)

    visit_id: str
    transcript_path: str
    filename: Optional[str] = None
    text: str
    character_count: int
    uploaded_at: Optional[datetime] = None


class SpeakerOut(BaseModel):
    """One voice in a recording, and who it was identified as."""

    model_config = ConfigDict(from_attributes=True)

    speaker: int = Field(description="The speech-to-text service's anonymous voice "
                                     "number. Not shown in the transcript.")
    role: Literal["doctor", "patient", "other"]
    label: str = Field(description="The line prefix used in the stored transcript, "
                                   "e.g. DOCTOR or OTHER (patient's mother)")
    confidence: Literal["high", "medium", "low"]
    turns: int = Field(description="How many turns this voice has in the transcript")


class AudioUploadCreate(BaseModel):
    """Start a chunked recording upload. The file itself follows in chunks."""

    model_config = ConfigDict(str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=255,
                          description="The recording's name; its extension decides "
                                      "the format. Kept for display only.")
    size_bytes: int = Field(gt=0, description="Exact size of the whole file")
    replace: bool = Field(default=False,
                          description="Required to overwrite an existing transcript")


class LiveRecordingCreate(BaseModel):
    """Start a live recording, sent in chunks while it is being recorded (§8.24).

    No size: it is not known until the recording stops.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=255,
                          description="A name for the recording, e.g. "
                                      "consultation.webm; its extension declares "
                                      "the recorder's format. Display only.")
    replace: bool = Field(default=False,
                          description="Required to overwrite an existing transcript")


class AudioUploadFinish(BaseModel):
    """Optional body for finishing an upload."""

    chunk_count: Optional[int] = Field(
        default=None, gt=0,
        description="How many chunks were sent. Fixes a live recording's plan; "
                    "omit it to process whatever arrived (an interrupted session). "
                    "Must match the plan for a file upload.")


AudioUploadStatus = Literal["receiving", "processing", "completed", "failed",
                            "abandoned", "superseded"]
AudioUploadMode = Literal["file", "live"]


class AudioUploadOut(BaseModel):
    """A chunked recording upload: its chunk plan, progress, and outcome.

    A `file` upload: send chunk `i` as bytes `[i * chunk_bytes, min((i + 1) *
    chunk_bytes, size_bytes))` of the file.

    A `live` upload is sent while it is recorded: every chunk is exactly
    `chunk_bytes` except the last. `size_bytes` and `chunk_count` are null until
    finish works them out from what arrived; `bytes_received` and `last_chunk_at`
    show how much has been saved and when, which is also what an interrupted
    session has to offer.

    `speakers` and `warnings` are filled in once `status` is `completed`;
    non-empty `warnings` mean the labels deserve a read. A `failed` upload with
    `retryable` true can be finished again without re-sending anything.
    """

    upload_id: str
    visit_id: str
    status: AudioUploadStatus
    mode: AudioUploadMode
    filename: Optional[str] = None
    size_bytes: Optional[int] = Field(
        description="The whole recording. Null only while a live recording is "
                    "being recorded.")
    chunk_bytes: int
    chunk_count: Optional[int] = Field(
        description="Null only while a live recording is being recorded.")
    chunks_received: int
    bytes_received: int
    last_chunk_at: Optional[datetime] = Field(
        default=None, description="When the latest chunk arrived; null once the "
                                  "chunks have been stitched")
    recording_stored: bool = Field(description="The chunks have been stitched into "
                                               "one stored recording")
    retryable: bool
    # No defaults, so the OpenAPI schema marks them required and the generated
    # types are plain arrays; from_row always supplies them (empty until done).
    speakers: List[SpeakerOut]
    warnings: List[str]
    duration_seconds: Optional[float] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Any) -> "AudioUploadOut":
        return cls(
            upload_id=row.upload_id,
            visit_id=row.visit_id,
            status=row.status,
            mode=row.mode,
            filename=row.filename,
            size_bytes=row.total_bytes,
            chunk_bytes=row.chunk_bytes,
            chunk_count=row.chunk_count,
            chunks_received=row.chunks_received,
            bytes_received=row.bytes_received,
            last_chunk_at=row.last_chunk_at,
            recording_stored=row.recording_stored,
            retryable=row.retryable,
            speakers=row.speakers or [],
            warnings=row.warnings or [],
            duration_seconds=row.duration_seconds,
            error_code=row.error_code,
            error_message=row.error_message,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )
