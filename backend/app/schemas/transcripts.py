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


AudioUploadStatus = Literal["receiving", "processing", "completed", "failed",
                            "abandoned", "superseded"]


class AudioUploadOut(BaseModel):
    """A chunked recording upload: its chunk plan, progress, and outcome.

    Send chunk `i` as bytes `[i * chunk_bytes, min((i + 1) * chunk_bytes,
    size_bytes))` of the file. `speakers` and `warnings` are filled in once
    `status` is `completed`; non-empty `warnings` mean the labels deserve a read
    before insights are generated. A `failed` upload with `retryable` true can be
    finished again without re-sending anything.
    """

    upload_id: str
    visit_id: str
    status: AudioUploadStatus
    filename: Optional[str] = None
    size_bytes: int
    chunk_bytes: int
    chunk_count: int
    chunks_received: int
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
            filename=row.filename,
            size_bytes=row.total_bytes,
            chunk_bytes=row.chunk_bytes,
            chunk_count=row.chunk_count,
            chunks_received=row.chunks_received,
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
