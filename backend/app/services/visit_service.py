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

from app.core.errors import ConflictError, NotFoundError
from app.db.models import JobRow, VisitRow
from app.db.repositories import insights as insights_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import visits as visits_repo
from app.db.repositories import jobs as jobs_repo
from app.db.tenancy import DoctorScope
from app.services import follow_up_service
from app.services.pagination import Page, clamp_limit

logger = logging.getLogger(__name__)


def schedule_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    visit_date: date,
    visit_time: Optional[time] = None,
    visit_number: Optional[int] = None,
    visit_type: Optional[str] = None,
    visit_status: str = "scheduled",
) -> VisitRow:
    """Create a visit for one of the scoped doctor's patients.

    visit_number is assigned sequentially when omitted, so callers needn't track
    it. A visit starts with no transcript: attaching one is a separate, explicit
    step through POST /visits/{visit_id}/transcript, which derives the storage
    address from the visit id rather than taking one from the caller (§8.19).

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
    visit_date: Optional[date] = None,
    visit_time: Optional[time] = None,
    visit_type: Optional[str] = None,
) -> VisitRow:
    """Correct a visit's own details: its date, time and type.

    Deliberately cannot change status or touch the transcript.

    Status is a lifecycle transition, not a field. Allowing it here skipped
    everything the transitions exist to do -- `/cancel`'s refusal once insights
    exist, its reopening of the follow-ups that visit had resolved, and the
    `completed_at` / `cancelled_at` stamps -- so a visit could end up cancelled
    with its follow-up still closed, silently dropping a patient who needs
    calling. `/complete` and `/cancel` are the only transitions (§8.17).

    The transcript is attached by upload, which derives its address from the
    visit id; a caller-supplied path could name another doctor's transcript
    (§8.19).
    """
    get_visit(conn, scope, visit_id)  # 404 if unknown or another doctor's

    changes: Dict[str, Any] = {}
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

    `reason` is stored on the visit. It used to reach a log line and stop there,
    so the API advertised a field it did not keep and the only answer to "why was
    this cancelled" was whichever log still existed.
    """
    visit = get_visit(conn, scope, visit_id)

    if visit.insights_generated or insights_repo.get_for_visit(conn, scope,
                                                              visit_id):
        raise ConflictError(
            "This visit has generated insights and cannot be cancelled.",
            details={"visit_id": visit_id})

    cleaned = (reason or "").strip() or None

    with conn.transaction():
        updated = visits_repo.mark_cancelled(conn, scope, visit_id, cleaned)
        # A cancelled visit no longer answers anything, so follow-ups it closed
        # must return to the queue or the patient is silently dropped.
        reopened = follow_up_service.reopen_for_cancelled_visit(conn, scope,
                                                               visit_id)

    logger.info("Cancelled %s%s (reason: %s)",
                visit.label,
                f", reopened {len(reopened)} follow-up(s)" if reopened else "",
                cleaned or "not given")
    return updated  # type: ignore[return-value]


def list_visits(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: Optional[str] = None,
    statuses: Optional[List[str]] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    has_transcript: Optional[bool] = None,
    pending_only: bool = False,
    sort: str = "date_desc",
    limit: Optional[int] = None,
    offset: int = 0,
) -> Page[VisitRow]:
    """One page of the scoped doctor's visits, with the matching total.

    Note the default page size. The repository still accepts `limit=None` and
    returns everything, which `list_for_patient` below relies on — a timeline
    must not be truncated — but an *unbounded list endpoint* is a different
    thing, and having no default is what pushed the schedule screen into picking
    a number and filtering the rest in the browser.

    A `patient_id` belonging to another doctor yields an empty page rather than
    an error, which is what a patient with no visits also returns.
    """
    limit = clamp_limit(limit)
    filters = {
        "patient_id": patient_id,
        "statuses": statuses,
        "date_from": date_from,
        "date_to": date_to,
        "has_transcript": has_transcript,
        "pending_only": pending_only,
    }
    return Page(
        items=visits_repo.list_visits(conn, scope, sort=sort, limit=limit,
                                      offset=offset, **filters),
        total=visits_repo.count_visits(conn, scope, **filters),
    )


def list_for_patient(conn: psycopg.Connection, scope: DoctorScope,
                     patient_id: str) -> List[VisitRow]:
    """A patient's visits, newest first. Deliberately unpaginated: a clinical
    history with rows silently missing is worse than a long page."""
    return visits_repo.list_visits(conn, scope, patient_id=patient_id,
                                   sort="date_desc")


def latest_job(conn: psycopg.Connection, scope: DoctorScope,
               visit_id: str) -> Optional["JobRow"]:
    """The most recent insight job for a visit, so a reload can resume polling.

    Goes through get_visit first so an unknown visit 404s and a visit that simply
    never had a job returns None — the route turns the second into a 204, and the
    UI needs to tell those apart.
    """
    get_visit(conn, scope, visit_id)
    return jobs_repo.latest_for_visit(conn, scope, visit_id)
