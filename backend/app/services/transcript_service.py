"""
Storing, reading and removing visit transcripts.

One storage rule, no subdirectories:

    backend/transcripts/{visit_id}.txt

stored on the visit as `transcripts/{visit_id}.txt`. Converted historical files
and new uploads follow the identical convention, so replace, read and delete need
no special cases.

The doctor's own filename is kept in `visits.transcript_filename` for display
only and never touches the filesystem: a client-supplied name is attacker
controlled, and deriving the path from `visit_id` removes the whole class of
problem rather than trying to sanitise it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import psycopg

from app.config import get_settings
from app.core.errors import (
    ConflictError,
    TranscriptError,
    TranscriptTooLargeError,
)
from app.db.models import VisitRow
from app.db.repositories import insights as insights_repo
from app.db.repositories import visits as visits_repo
from app.domain.transcripts import (
    TRANSCRIPT_SUFFIX,
    decode_transcript_bytes,
    load_transcript,
)
from app.services.visit_service import get_visit

logger = logging.getLogger(__name__)

# Uploading is only allowed once the consultation is done, which is what makes an
# "Add Transcript" button legitimately disabled until then.
UPLOADABLE_STATUSES = ("completed",)


@dataclass
class TranscriptContent:
    """A transcript read back for display."""

    visit_id: str
    transcript_path: str
    filename: Optional[str]
    text: str
    character_count: int
    uploaded_at: Optional[datetime]


def _target_path(visit_id: str) -> Path:
    return get_settings().transcripts_dir / f"{visit_id}{TRANSCRIPT_SUFFIX}"


def _stored_path(visit_id: str) -> str:
    settings = get_settings()
    return f"{settings.transcripts_dir.name}/{visit_id}{TRANSCRIPT_SUFFIX}"


def _display_filename(client_filename: Optional[str]) -> Optional[str]:
    """Keep only the base name, for display. Never used to build a path."""
    if not client_filename:
        return None
    name = Path(client_filename.replace("\\", "/")).name.strip()
    return name or None


def validate_upload(filename: Optional[str], raw: bytes) -> str:
    """Check an upload and return its decoded text.

    Rejects rather than coerces: silently mis-parsing a Word document or a
    mis-encoded file would produce a confident, wrong clinical record.
    """
    settings = get_settings()
    display = _display_filename(filename) or "upload"

    suffix = Path(display).suffix.lower()
    if suffix != TRANSCRIPT_SUFFIX:
        raise TranscriptError(
            f"Transcripts must be {TRANSCRIPT_SUFFIX} files. "
            f"Got {suffix or 'no extension'}.",
            details={"filename": display, "suffix": suffix})

    if len(raw) > settings.max_transcript_bytes:
        raise TranscriptTooLargeError(
            f"Transcript is {len(raw):,} bytes; the limit is "
            f"{settings.max_transcript_bytes:,}.",
            details={"size_bytes": len(raw),
                     "max_bytes": settings.max_transcript_bytes})

    text = decode_transcript_bytes(raw, display).strip()
    if not text:
        raise TranscriptError("Transcript is empty.",
                              details={"filename": display})
    return text


def store_transcript(
    conn: psycopg.Connection,
    visit_id: str,
    filename: Optional[str],
    raw: bytes,
    replace: bool = False,
) -> VisitRow:
    """Validate and store an uploaded transcript against a completed visit."""
    visit = get_visit(conn, visit_id)

    if visit.visit_status not in UPLOADABLE_STATUSES:
        raise ConflictError(
            f"Visit is {visit.visit_status}; a transcript can only be attached to a "
            f"completed visit. Mark the visit complete first.",
            details={"visit_id": visit_id, "visit_status": visit.visit_status})

    if visit.transcript_path and not replace:
        raise ConflictError(
            "This visit already has a transcript. Send replace=true to overwrite it.",
            details={"visit_id": visit_id,
                     "transcript_filename": visit.transcript_filename})

    text = validate_upload(filename, raw)

    target = _target_path(visit_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n" so a Windows upload does not become CRLF on disk.
    target.write_text(text, encoding="utf-8", newline="\n")

    updated = visits_repo.set_transcript(
        conn, visit_id,
        transcript_path=_stored_path(visit_id),
        transcript_filename=_display_filename(filename),
    )
    logger.info("Stored transcript for %s (%d chars)", visit.label, len(text))
    return updated  # type: ignore[return-value]


def read_transcript(conn: psycopg.Connection, visit_id: str) -> TranscriptContent:
    """Read a visit's transcript so a brief can be checked against its source."""
    visit = get_visit(conn, visit_id)
    if not visit.transcript_path:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Visit {visit_id} has no transcript attached.",
                            details={"visit_id": visit_id})

    transcript = load_transcript(visit.transcript_path,
                                 get_settings().transcripts_dir)
    return TranscriptContent(
        visit_id=visit.visit_id,
        transcript_path=visit.transcript_path,
        filename=visit.transcript_filename,
        text=transcript.text,
        character_count=transcript.character_count,
        uploaded_at=visit.transcript_uploaded_at,
    )


def delete_transcript(conn: psycopg.Connection, visit_id: str) -> None:
    """Remove a visit's transcript file and clear its transcript columns.

    Refused once insights exist: removing the source of a generated brief would
    leave a clinical record that cannot be audited against what was said.
    """
    visit = get_visit(conn, visit_id)
    if not visit.transcript_path:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Visit {visit_id} has no transcript attached.",
                            details={"visit_id": visit_id})

    if insights_repo.get_for_visit(conn, visit_id) is not None:
        raise ConflictError(
            "This visit has generated insights. Deleting the transcript would "
            "leave a brief that cannot be checked against its source.",
            details={"visit_id": visit_id})

    target = _target_path(visit_id)
    if target.is_file():
        target.unlink()

    visits_repo.clear_transcript(conn, visit_id)
    logger.info("Deleted transcript for %s", visit.label)
