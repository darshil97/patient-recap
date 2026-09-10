"""
Follow-up queue endpoints.

The queue is the screen the doctor works from, and every row on it carries a
patient's name and telephone number — so this list is the highest-value leak
surface in the API. It is scoped through `follow_ups.patient_id ->
patients.doctor_id`; follow_ups has no doctor_id column of its own, and
deliberately so (see app/db/repositories/follow_ups.py for why the patient_id
denormalisation does not justify a second one).
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import CurrentDoctor, get_db, require_doctor
from app.db.repositories import follow_ups as follow_ups_repo
from app.schemas.common import ErrorResponse
from app.schemas.follow_ups import FollowUpOut, FollowUpUpdate
from app.services import follow_up_service

router = APIRouter(prefix="/follow-ups", tags=["follow-ups"],
                   dependencies=[Depends(require_doctor)])


@router.get(
    "",
    response_model=List[FollowUpOut],
    summary="The follow-up queue",
)
def list_follow_ups(
    response: Response,
    status: str = Query(default="open",
                        pattern="^(open|resolved|dismissed|all)$"),
    due_before: Optional[date] = Query(default=None,
                                       description="Inclusive, on due_earliest"),
    due_after: Optional[date] = Query(default=None, description="Inclusive"),
    needs_confirmation: Optional[bool] = Query(default=None),
    patient_id: Optional[UUID] = Query(default=None),
    sort: str = Query(default="due_asc", pattern="^(due_asc|due_desc)$"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> List[FollowUpOut]:
    """The signed-in doctor's open follow-ups, in due-date order by default.

    Each row carries everything the queue needs, including the patient's phone
    number, because the action on this screen is to telephone them.

    No `days_overdue` field: that depends on today, and a server-computed value
    would go stale in a cached response, so it is derived client-side from
    `due_earliest`.
    """
    kwargs = {
        "status": status,
        "due_before": due_before,
        "due_after": due_after,
        "needs_confirmation": needs_confirmation,
        "patient_id": str(patient_id) if patient_id else None,
    }
    rows = follow_ups_repo.list_follow_ups(conn, doctor.doctor_id, sort=sort,
                                           limit=limit, offset=offset, **kwargs)
    response.headers["X-Total-Count"] = str(
        follow_ups_repo.count_follow_ups(conn, doctor.doctor_id, **kwargs))
    return [FollowUpOut.model_validate(r) for r in rows]


@router.get(
    "/{follow_up_id}",
    response_model=FollowUpOut,
    responses={404: {"model": ErrorResponse}},
    summary="Get one follow-up",
)
def get_follow_up(
    follow_up_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> FollowUpOut:
    """One follow-up. 404, not 403, for another doctor's."""
    return FollowUpOut.model_validate(
        follow_up_service.get_follow_up(conn, doctor.doctor_id, str(follow_up_id)))


@router.patch(
    "/{follow_up_id}",
    response_model=FollowUpOut,
    responses={404: {"model": ErrorResponse},
               422: {"model": ErrorResponse,
                     "description": "Invalid status, or dismissal without a reason"}},
    summary="Record the outcome of contacting a patient",
)
def update_follow_up(
    follow_up_id: UUID,
    payload: FollowUpUpdate,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> FollowUpOut:
    """Dismiss, reopen, note, or record that the patient was contacted.

    `status` accepts `dismissed` or `open` only. `resolved` is rejected: a
    follow-up resolves as a consequence of scheduling a visit, and setting it
    directly would create resolved rows with no visit behind them.

    `mark_contacted` is independent of status, so "I called and they are still
    deciding" leaves the row open while recording the call.

    Scoped in the UPDATE itself, not only checked first. Dismissing another
    doctor's follow-up would not surface as an error anywhere — the patient simply
    drops off a queue nobody is watching.
    """
    updated = follow_up_service.update_follow_up(
        conn,
        doctor.doctor_id,
        str(follow_up_id),
        status=payload.status,
        dismissed_reason=payload.dismissed_reason,
        note=payload.note,
        mark_contacted=payload.mark_contacted,
    )
    return FollowUpOut.model_validate(updated)
