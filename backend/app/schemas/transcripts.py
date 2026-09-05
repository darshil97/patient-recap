"""Transcript response models."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


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
