"""Job polling."""

from __future__ import annotations

from typing import List
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query

from app.api.deps import get_db, require_auth
from app.db.repositories import jobs as jobs_repo
from app.schemas.common import ErrorResponse
from app.schemas.insights import JobOut
from app.services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"],
                   dependencies=[Depends(require_auth)])


@router.get(
    "/{job_id}",
    response_model=JobOut,
    responses={404: {"model": ErrorResponse}},
    summary="Poll an insight job",
)
def get_job(job_id: UUID) -> JobOut:
    """Current state of a job.

    `status` is one of queued, running, succeeded, failed, failed_quota.
    `failed_quota` means every API key is rate limited, which is worth retrying
    unchanged once the daily limit resets. `layers_completed` shows how far the
    chain got.
    """
    return JobOut.model_validate(job_service.get_job(str(job_id)))


@router.get("", response_model=List[JobOut], summary="List recent jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_db),
) -> List[JobOut]:
    return [JobOut.model_validate(j) for j in jobs_repo.list_recent(conn, limit=limit)]
