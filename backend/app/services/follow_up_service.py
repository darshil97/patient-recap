"""
Follow-up lifecycle rules, scoped to one doctor.

A follow-up moves between three states:

    open       the default; the patient needs calling
    resolved   a later visit was scheduled — caused by scheduling, never set
               directly by a client
    dismissed  the doctor called and the patient is not returning

`last_contacted_at` is independent of status, so "I phoned them and they are
still deciding" stays open while recording that the call happened.

The follow-up queue is the screen a doctor works from, and every row on it
carries a patient's name and telephone number. It is the most sensitive list in
the application, which is why the scope travels through every function here
rather than being applied once at the route.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

import psycopg

from app.core.errors import NotFoundError, ValidationError
from app.db.models import FollowUpRow, VisitRow
from app.db.repositories import follow_ups as follow_ups_repo
from app.db.tenancy import DoctorScope
from app.domain.follow_up.models import SCHEDULED, ResolvedFollowUp
from app.services.pagination import Page, clamp_limit

logger = logging.getLogger(__name__)

# Only these are settable through the API. 'resolved' is deliberately excluded:
# resolution is caused by scheduling a visit, and letting a client set it would
# create resolved rows with no visit behind them.
SETTABLE_STATUSES = ("open", "dismissed")


def sync_from_insights(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit: VisitRow,
    follow_up: Optional[ResolvedFollowUp],
) -> Optional[str]:
    """Mirror a generated follow-up into the tracking table.

    Call inside the same transaction as the insight write, so insights and
    follow-up land together or not at all.

    Only `scheduled` follow-ups enter the queue: `conditional_only` has no date
    and cannot be sorted into a due-date list (its return triggers render on the
    visit brief instead), and `not_discussed` produces nothing. Either of those
    clears any existing row, so a regeneration that no longer finds a follow-up
    does not leave a stale one in the queue.
    """
    if follow_up is None or follow_up.type != SCHEDULED or not follow_up.due_earliest:
        removed = follow_ups_repo.delete_for_visit(conn, scope, visit.visit_id)
        if removed:
            logger.info("Removed stale follow-up for %s (now %s)",
                        visit.label, follow_up.type if follow_up else "none")
        return None

    follow_up_id = follow_ups_repo.upsert(
        conn,
        scope,
        source_visit_id=visit.visit_id,
        patient_id=visit.patient_id,
        due_earliest=follow_up.due_earliest,
        due_latest=follow_up.due_latest,
        needs_confirmation=follow_up.needs_human_confirmation,
        stated=follow_up.stated,
        purpose=follow_up.purpose,
    )
    if follow_up_id is None:
        # The upsert resolves patient_id through the doctor filter, so None means
        # the visit's patient is not this doctor's. Reachable only through a bug,
        # never through a request — but it must not pass silently: the caller is
        # inside the insight transaction, and swallowing this would commit a brief
        # whose queue entry was quietly dropped.
        raise NotFoundError(
            f"No patient with id {visit.patient_id}.",
            details={"visit_id": visit.visit_id})

    logger.info("Follow-up %s for %s due %s",
                follow_up_id, visit.label, follow_up.due_earliest)
    return follow_up_id


def resolve_for_new_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    new_visit_id: str,
    new_visit_date: date,
) -> List[str]:
    """Close open follow-ups answered by a newly scheduled visit."""
    resolved = follow_ups_repo.resolve_open_for_patient(
        conn, scope, patient_id, new_visit_id, new_visit_date)
    if resolved:
        logger.info("Scheduling %s resolved %d follow-up(s): %s",
                    new_visit_id, len(resolved), ", ".join(resolved))
    return resolved


def reopen_for_cancelled_visit(conn: psycopg.Connection, scope: DoctorScope,
                               visit_id: str) -> List[str]:
    """Reopen follow-ups whose resolving visit has been cancelled."""
    reopened = follow_ups_repo.reopen_resolved_by_visit(conn, scope, visit_id)
    if reopened:
        logger.info("Cancelling %s reopened %d follow-up(s): %s",
                    visit_id, len(reopened), ", ".join(reopened))
    return reopened


def list_follow_ups(
    conn: psycopg.Connection,
    scope: DoctorScope,
    status: Optional[str] = "open",
    due_before: Optional[date] = None,
    due_after: Optional[date] = None,
    needs_confirmation: Optional[bool] = None,
    patient_id: Optional[str] = None,
    sort: str = "due_asc",
    limit: Optional[int] = None,
    offset: int = 0,
) -> Page[FollowUpRow]:
    """One page of the queue, with the matching total.

    Every row here carries a patient's name and telephone number, so the count
    beside it has to be scoped identically or the number itself says something
    about another practice. Taking both from one set of arguments is what makes
    that structural rather than a thing to remember.
    """
    limit = clamp_limit(limit)
    filters = {
        "status": status,
        "due_before": due_before,
        "due_after": due_after,
        "needs_confirmation": needs_confirmation,
        "patient_id": patient_id,
    }
    return Page(
        items=follow_ups_repo.list_follow_ups(conn, scope, sort=sort,
                                              limit=limit, offset=offset,
                                              **filters),
        total=follow_ups_repo.count_follow_ups(conn, scope, **filters),
    )


def get_follow_up(conn: psycopg.Connection, scope: DoctorScope,
                  follow_up_id: str) -> FollowUpRow:
    """One of the scoped doctor's follow-ups. 404 for another doctor's."""
    row = follow_ups_repo.get(conn, scope, follow_up_id)
    if row is None:
        raise NotFoundError(f"No follow-up with id {follow_up_id}.")
    return row


def update_follow_up(
    conn: psycopg.Connection,
    scope: DoctorScope,
    follow_up_id: str,
    status: Optional[str] = None,
    dismissed_reason: Optional[str] = None,
    note: Optional[str] = None,
    mark_contacted: bool = False,
) -> FollowUpRow:
    """Record the outcome of contacting a patient."""
    get_follow_up(conn, scope, follow_up_id)  # 404 if unknown or another doctor's

    if status is not None:
        if status == "resolved":
            raise ValidationError(
                "A follow-up cannot be resolved directly. Schedule a visit for the "
                "patient and it resolves automatically.",
                details={"allowed": list(SETTABLE_STATUSES)})
        if status not in SETTABLE_STATUSES:
            raise ValidationError(
                f"status must be one of {list(SETTABLE_STATUSES)}.",
                details={"allowed": list(SETTABLE_STATUSES)})
        if status == "dismissed" and not (dismissed_reason or "").strip():
            raise ValidationError(
                "dismissed_reason is required when dismissing a follow-up.")

    updated = follow_ups_repo.apply_update(
        conn, scope, follow_up_id,
        status=status,
        dismissed_reason=(dismissed_reason or "").strip() or None,
        note=note,
        mark_contacted=mark_contacted,
    )
    logger.info("Follow-up %s updated (status=%s contacted=%s)",
                follow_up_id, status or "unchanged", mark_contacted)
    return updated  # type: ignore[return-value]
