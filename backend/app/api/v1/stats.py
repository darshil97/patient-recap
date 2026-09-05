"""Home-screen counters."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends

from app.api.deps import get_db, require_auth
from app.schemas.stats import StatsOut
from app.services import stats_service

router = APIRouter(tags=["stats"], dependencies=[Depends(require_auth)])


@router.get("/stats", response_model=StatsOut, summary="Counts for the home screen")
def get_stats(conn: psycopg.Connection = Depends(get_db)) -> StatsOut:
    """Patient, visit and follow-up counts in one request.

    `overdue` and `due_next_7_days` are relative to the server's date, which is
    returned as `as_of` so the UI can label them honestly.

    `awaiting_transcript` (completed, no transcript) and `awaiting_insights`
    (transcript, no insights) are the two states needing doctor action.
    """
    return StatsOut.model_validate(stats_service.build_stats(conn))
