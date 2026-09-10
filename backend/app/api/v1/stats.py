"""Home-screen counters, for the signed-in doctor."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends

from app.api.deps import CurrentDoctor, get_db, require_doctor
from app.schemas.stats import StatsOut
from app.services import stats_service

router = APIRouter(tags=["stats"], dependencies=[Depends(require_doctor)])


@router.get("/stats", response_model=StatsOut, summary="Counts for the home screen")
def get_stats(
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> StatsOut:
    """Patient, visit and follow-up counts in one request, for this doctor only.

    `overdue` and `due_next_7_days` are relative to the server's date, which is
    returned as `as_of` so the UI can label them honestly.

    `awaiting_transcript` (completed, no transcript) and `awaiting_insights`
    (transcript, no insights) are the two states needing doctor action.

    Every number here is scoped at the query. A leaking list is obvious the moment
    a doctor reads a name they do not recognise; a leaking counter just looks like
    a number that is slightly too high, which is why these were as important to fix
    as the lists.
    """
    return StatsOut.model_validate(
        stats_service.build_stats(conn, doctor.doctor_id))
