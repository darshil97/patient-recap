"""
Visit scheduling and lifecycle, scoped to one doctor.

    Schedule  ->  Complete  ->  Add Transcript  ->  Generate Insights

Each transition is explicit. Attaching a transcript deliberately does *not*
complete a visit: an implicit status change would contradict the Complete button
and make the real state depend on the order of two unrelated actions.

Every function takes a `DoctorScope`. A visit has no doctor_id of its own — it
derives one through its patient — so "which doctor is asking" has to travel down
from the request; there is nothing on the visit row to fall back on.
"""

from __future__ import annotations

import logging
from datetime import date, time
from typing import Any, Dict, List, Optional

import psycopg

from app.config import get_settings
from app.core.errors import ConflictError, NotFoundError, TranscriptError
from app.db.models import VisitRow
from app.db.repositories import insights as insights_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import visits as visits_repo
from app.db.tenancy import DoctorScope
from app.domain.transcripts import normalise_path, resolve_within
from app.storage import LOCAL
from app.services import follow_up_service

logger = logging.getLogger(__name__)


def _check_transcript(path: str) -> str:
    """Validate a client-supplied transcript path and normalise how it's stored.

    The path is confined to the configured transcripts directory; without that a
    payload of `../../etc/passwd` would be honoured.
    """
    settings = get_settings()
    resolved = resolve_within(path, settings.transcripts_dir)
    if not resolved.is_file():
        raise TranscriptError(f"Transcript not found: {path}",
                              details={"requested": path})
    return normalise_path(resolved, settings.transcripts_dir.parent)


def schedule_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    visit_date: date,
    visit_time: Optional[time] = None,
    visit_number: Optional[int] = None,
    visit_type: Optional[str] = None,
    visit_status: str = "scheduled",
    transcript_path: Optional[str] = None,
) -> VisitRow:
    """Create a visit for one of the scoped doctor's patients.

    visit_number is assigned sequentially when omitted, so callers needn't track
    it. transcript_path is optional because a scheduled visit has no recording
    yet; supplying it up front is supported so historical data loads in one call.

    Scheduling is what resolves outstanding follow-ups: any open follow-up from
    an earlier visit for this patient is closed here, in the same transaction as
    the insert, so the queue can never disagree with the visit list.

    A patient_id belonging to another doctor is a 404, from the same code path as
    a patient_id that does not exist at all.
    """
    if patients_repo.get(conn, scope, patient_id) is None:
        raise NotFoundError(f"No patient with id {patient_id}.")

    if visit_number is not None:
        clash = [v for v in visits_repo.list_visits(conn, scope,
                                                    patient_id=patient_id)
                 if v.visit_number == visit_number]
        if clash:
            raise ConflictError(
                f"Visit number {visit_number} already exists for this patient.",
                details={"visit_id": clash[0].visit_id})

    stored_path = _check_transcript(transcript_path) if transcript_path else None

    with conn.transaction():
        number = visit_number or visits_repo.next_visit_number(conn, scope,
                                                               patient_id)
        # First visit for a patient is the initial consultation unless told otherwise.
        resolved_type = visit_type or ("initial" if number == 1 else "follow_up")

        visit = visits_repo.create(
            conn,
            scope,
            patient_id=patient_id,
            visit_number=number,
            visit_date=visit_date,
            visit_time=visit_time or time(9, 0),
            visit_type=resolved_type,
            visit_status=visit_status,
            transcript_path=stored_path,
            # LOCAL, not the configured default, and not the caller's choice.
            # This path adopts an *already existing* transcript by reference, and
            # _check_transcript verifies it by stat-ing a file — so the only
            # backend it can honestly claim is the filesystem. A fresh upload
            # goes through transcript_service and lands on TRANSCRIPT_STORAGE.
            transcript_provider=LOCAL if stored_path else None,
        )
        # The INSERT selects its patient under the doctor filter, so None means
        # the patient is not this doctor's. Already caught above; kept because the
        # repository's contract allows it and a silent None would become an
        # AttributeError three lines later.
        if visit is None:
            raise NotFoundError(f"No patient with id {patient_id}.")

        follow_up_service.resolve_for_new_visit(
            conn, scope, patient_id, visit.visit_id, visit_date)

    logger.info("Scheduled %s", visit.label)
    return visit


def get_visit(conn: psycopg.Connection, scope: DoctorScope,
              visit_id: str) -> VisitRow:
    """One of the scoped doctor's visits.

    404 rather than 403 for another doctor's visit: see get_patient.
    """
    visit = visits_repo.get(conn, scope, visit_id)
    if visit is None:
        raise NotFoundError(f"No visit with id {visit_id}.")
    return visit


def update_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    transcript_path: Optional[str] = None,
    visit_status: Optional[str] = None,
    visit_date: Optional[date] = None,
    visit_time: Optional[time] = None,
    visit_type: Optional[str] = None,
) -> VisitRow:
    """Patch a visit's details.

    Attaching a transcript here does not change the status. Completion and
    cancellation are explicit transitions with their own endpoints, so that the
    Complete button is the only thing that completes a visit.
    """
    get_visit(conn, scope, visit_id)  # 404 if unknown or another doctor's

    changes: Dict[str, Any] = {}
    if transcript_path is not None:
        # Same reasoning as schedule_visit: the path is verified by stat-ing a
        # file, so LOCAL is the only provider this can truthfully record.
        changes["transcript_path"] = _check_transcript(transcript_path)
        changes["transcript_provider"] = LOCAL
    if visit_status is not None:
        changes["visit_status"] = visit_status
    if visit_date is not None:
        changes["visit_date"] = visit_date
    if visit_time is not None:
        changes["visit_time"] = visit_time
    if visit_type is not None:
        changes["visit_type"] = visit_type

    updated = visits_repo.update(conn, scope, visit_id, changes)
    logger.info("Updated %s: %s", visit_id, ", ".join(changes) or "no changes")
    return updated  # type: ignore[return-value]


def complete_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    visit_date: Optional[date] = None,
    visit_time: Optional[time] = None,
) -> VisitRow:
    """Mark a consultation done, optionally correcting the actual date/time.

    Idempotent: completing an already-completed visit succeeds and leaves
    completed_at untouched, so the UI need not guard a double click.
    """
    visit = get_visit(conn, scope, visit_id)

    if visit.visit_status == "cancelled":
        raise ConflictError(
            "This visit is cancelled and cannot be completed.",
            details={"visit_id": visit_id, "visit_status": visit.visit_status})

    updated = visits_repo.mark_completed(conn, scope, visit_id, visit_date,
                                        visit_time)
    logger.info("Completed %s", updated.label if updated else visit_id)
    return updated  # type: ignore[return-value]


def cancel_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    reason: Optional[str] = None,
) -> VisitRow:
    """Cancel a visit and reopen anything it had resolved.

    Refused once insights exist: a generated brief is a record of a consultation
    that demonstrably happened, so cancelling it would contradict the data.
    """
    visit = get_visit(conn, scope, visit_id)

    if visit.insights_generated or insights_repo.get_for_visit(conn, scope,
                                                              visit_id):
        raise ConflictError(
            "This visit has generated insights and cannot be cancelled.",
            details={"visit_id": visit_id})

    with conn.transaction():
        updated = visits_repo.mark_cancelled(conn, scope, visit_id)
        # A cancelled visit no longer answers anything, so follow-ups it closed
        # must return to the queue or the patient is silently dropped.
        reopened = follow_up_service.reopen_for_cancelled_visit(conn, scope,
                                                               visit_id)

    logger.info("Cancelled %s%s (reason: %s)",
                visit.label,
                f", reopened {len(reopened)} follow-up(s)" if reopened else "",
                reason or "not given")
    return updated  # type: ignore[return-value]


def list_for_patient(conn: psycopg.Connection, scope: DoctorScope,
                     patient_id: str) -> List[VisitRow]:
    """A patient's visits, newest first."""
    return visits_repo.list_visits(conn, scope, patient_id=patient_id,
                                   sort="date_desc")
