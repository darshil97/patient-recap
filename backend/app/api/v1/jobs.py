"""
Job polling.

Worth a note on why these two routes needed the most attention of any in the API.
Neither has a patient or a visit anywhere in its path — `GET /jobs/{job_id}` is
keyed on nothing but a job id, and `GET /jobs` on nothing at all — so neither
*looks* like it returns tenant data. Both did. A job row carries the visit id it
is generating for, the error message if it failed, and the token spend; the
frontend polls the first of them every 2.5 seconds while a brief generates. Both
are now scoped through `visit -> patient -> doctor_id`, and an unknown job id and
another doctor's job id return the same 404.
"""

from __future__ import annotations

from typing import List
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentDoctor, get_db, require_doctor
from app.db.repositories import jobs as jobs_repo
from app.schemas.common import ErrorResponse
from app.schemas.insights import JobOut
from app.services import job_service

router = APIRouter(prefix="/jobs", tags=["jobs"],
                   dependencies=[Depends(require_doctor)])


@router.get(
    "/{job_id}",
    response_model=JobOut,
    responses={401: {"model": ErrorResponse},
               404: {"model": ErrorResponse}},
    summary="Poll an insight job",
)
def get_job(
    job_id: UUID,
    doctor: CurrentDoctor = Depends(require_doctor),
) -> JobOut:
    """Current state of one of the signed-in doctor's jobs.

    `status` is one of queued, running, succeeded, failed, failed_quota.
    `failed_quota` means every API key is rate limited, which is worth retrying
    unchanged once the daily limit resets. `layers_completed` shows how far the
    chain got.

    404 for a job belonging to another doctor's visit.
    """
    return JobOut.model_validate(job_service.get_job(doctor.doctor_id, str(job_id)))


@router.get("", response_model=List[JobOut], summary="List recent jobs")
def list_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> List[JobOut]:
    """Recent jobs for the signed-in doctor only.

    This route takes no id at all, which is what made it easy to overlook: it
    previously returned every doctor's generation history in one response.
    """
    return [JobOut.model_validate(j)
            for j in jobs_repo.list_recent(conn, doctor.doctor_id, limit=limit)]
