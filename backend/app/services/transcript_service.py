"""
Storing, reading and removing visit transcripts.

One address rule, no subdirectories, and it is the same on every backend:

    transcripts/{visit_id}.txt

Stored on the visit as `transcript_path`, with `visits.transcript_provider`
recording *which* backend holds it — `local` for the filesystem, or
`neon_object_storage` for a Neon Object Storage bucket. Converted historical
files, new uploads and objects all follow the identical convention, so replace,
read and delete need no special cases and no provider branches in this module.

Everything provider-independent stays here: upload validation, the
completed-visit rule, the refusal to delete a transcript once insights exist.
Only the bytes move, and that goes through app/storage/. Adding a third backend
should not require touching this file.

The doctor's own filename is kept in `visits.transcript_filename` for display
only and never reaches a path or an object key: a client-supplied name is
attacker controlled, and deriving the address from `visit_id` removes the whole
class of problem rather than trying to sanitise it (§8.19).
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
from app.db.tenancy import DoctorScope
from app.domain.transcripts import TRANSCRIPT_SUFFIX, decode_transcript_bytes
from app.storage import (
    default_provider,
    get_store,
    store_for_visit,
    transcript_locator,
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


# _target_path / _stored_path used to build a filesystem path here. Both are gone:
# the address is now produced by app.storage.transcript_locator so that exactly one
# function in the codebase knows the shape of a transcript address, and both
# backends agree on it.


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
    scope: DoctorScope,
    visit_id: str,
    filename: Optional[str],
    raw: bytes,
    replace: bool = False,
) -> VisitRow:
    """Validate and store an uploaded transcript against a completed visit.

    The scoped get_visit is what stops an upload landing on another doctor's
    visit. It matters more here than on a read: the transcript is written to
    transcripts/{visit_id}.txt derived from the path argument, so an unscoped
    version would let one doctor overwrite the source recording behind another
    doctor's generated brief.

    Writes go to whichever backend TRANSCRIPT_STORAGE names, and the provider is
    recorded on the visit alongside the path. A replacement is written to the
    *currently configured* backend rather than the one holding the old copy, and
    the old copy is removed afterwards — so replacing a transcript is also how a
    single visit migrates, and it can never end up with a live object and a live
    file both claiming to be the source.
    """
    visit = get_visit(conn, scope, visit_id)

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

    provider = default_provider()
    locator = transcript_locator(visit_id)
    get_store(provider).write(locator, text)

    # The row is updated only after the bytes are durable. If the write above
    # raises, the visit keeps pointing at whatever it pointed at before, which for
    # a first upload is nothing at all — never at an address holding no data.
    updated = visits_repo.set_transcript(
        conn, scope, visit_id,
        transcript_path=locator,
        transcript_filename=_display_filename(filename),
        transcript_provider=provider,
    )

    # Replacing across backends leaves the old copy behind. Clean it up now that
    # the row points at the new one, so a later migration or audit does not find
    # two copies and have to guess which is current. Best-effort: the visit is
    # already correct, so failing to delete a superseded copy must not fail the
    # upload.
    old_provider = visit.transcript_provider
    if visit.transcript_path and old_provider and old_provider != provider:
        try:
            get_store(old_provider).delete(visit.transcript_path)
            logger.info("Removed superseded %s copy of %s",
                        old_provider, visit.transcript_path)
        except Exception:
            logger.warning("Could not remove the superseded %s copy of %s; it is "
                           "now orphaned and safe to delete by hand",
                           old_provider, visit.transcript_path, exc_info=True)

    logger.info("Stored transcript for %s (%d chars, %s)",
                visit.label, len(text), provider)
    return updated  # type: ignore[return-value]


def read_transcript(conn: psycopg.Connection, scope: DoctorScope,
                    visit_id: str) -> TranscriptContent:
    """Read a visit's transcript so a brief can be checked against its source.

    Verbatim consultation text — the rawest patient data the system holds. Scoped
    through get_visit, so an unknown visit and another doctor's visit both 404.
    """
    visit = get_visit(conn, scope, visit_id)
    if not visit.transcript_path:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Visit {visit_id} has no transcript attached.",
                            details={"visit_id": visit_id})

    # store_for_visit reads the provider off the row, not from settings, so a
    # transcript stored before TRANSCRIPT_STORAGE changed is still readable.
    stored = store_for_visit(visit).read(visit.transcript_path)
    return TranscriptContent(
        visit_id=visit.visit_id,
        transcript_path=visit.transcript_path,
        filename=visit.transcript_filename,
        text=stored.text,
        character_count=stored.character_count,
        uploaded_at=visit.transcript_uploaded_at,
    )


def delete_transcript(conn: psycopg.Connection, scope: DoctorScope,
                      visit_id: str) -> None:
    """Remove a visit's stored transcript and clear its transcript columns.

    Refused once insights exist: removing the source of a generated brief would
    leave a clinical record that cannot be audited against what was said.

    Deletes from the backend the row names, so this works the same whether the
    bytes are on disk or in the bucket.
    """
    visit = get_visit(conn, scope, visit_id)
    if not visit.transcript_path:
        from app.core.errors import NotFoundError

        raise NotFoundError(f"Visit {visit_id} has no transcript attached.",
                            details={"visit_id": visit_id})

    if insights_repo.get_for_visit(conn, scope, visit_id) is not None:
        raise ConflictError(
            "This visit has generated insights. Deleting the transcript would "
            "leave a brief that cannot be checked against its source.",
            details={"visit_id": visit_id})

    # Bytes first, then the row. The other order would leave a visit with no
    # transcript_path pointing at data nobody can reach or clean up.
    removed = store_for_visit(visit).delete(visit.transcript_path)

    visits_repo.clear_transcript(conn, scope, visit_id)
    logger.info("Deleted transcript for %s (%s, %s)", visit.label,
                visit.transcript_provider or "local",
                "removed" if removed else "was already absent")
