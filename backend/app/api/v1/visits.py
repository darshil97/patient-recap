"""
Visit endpoints: scheduling, lifecycle, transcripts, and insight generation.

Every handler takes the current doctor from `require_doctor` and passes it down.
`doctor_id` is never a body field or query parameter, so a client cannot claim to
act as another doctor. Path-param lookups 404 rather than 403 when the row belongs
to someone else (§8.12).
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional
from uuid import UUID

import psycopg
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    Query,
    Response,
    UploadFile,
    status,
)

from app.api.deps import CurrentDoctor, get_db, require_doctor
from app.config import get_settings
from app.schemas.common import ErrorResponse
from app.schemas.insights import (
    GenerateInsightsRequest,
    InsightOut,
    JobAccepted,
    JobOut,
)
from app.schemas.transcripts import TranscriptOut
from app.schemas.visits import (
    VisitCancelRequest,
    VisitCompleteRequest,
    VisitCreate,
    VisitOut,
    VisitUpdate,
)
from app.services import insight_service, job_service, transcript_service, visit_service
from app.services.pagination import MAX_PAGE_SIZE

# require_doctor depends on require_auth, so both gates apply to every route here
# even if a handler forgets to name the dependency.
router = APIRouter(prefix="/visits", tags=["visits"],
                   dependencies=[Depends(require_doctor)])


# --------------------------------------------------------------------------- #
# Scheduling and reads
# --------------------------------------------------------------------------- #

@router.post(
    "",
    response_model=VisitOut,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"model": ErrorResponse, "description": "Unknown patient"},
               409: {"model": ErrorResponse, "description": "Visit number taken"}},
    summary="Schedule a visit",
)
def schedule_visit(
    payload: VisitCreate,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """Create a visit for one of the signed-in doctor's patients.

    A `patient_id` belonging to another doctor is a 404, indistinguishable from a
    patient_id that does not exist — so the body cannot be used to probe for other
    practices' patient ids.

    `visit_number` is assigned sequentially when omitted. A visit starts with no
    transcript; upload one with `POST /visits/{visit_id}/transcript` once the
    consultation is complete.

    Scheduling also closes any open follow-up from an earlier visit for this
    patient, in the same transaction.
    """
    visit = visit_service.schedule_visit(
        conn,
        doctor.doctor_id,
        patient_id=str(payload.patient_id),
        visit_date=payload.visit_date,
        visit_time=payload.visit_time,
        visit_number=payload.visit_number,
        visit_type=payload.visit_type,
        visit_status=payload.visit_status,
    )
    return VisitOut.model_validate(visit)


@router.get("", response_model=List[VisitOut], summary="List visits")
def list_visits(
    response: Response,
    status_filter: Optional[List[str]] = Query(
        default=None, alias="status",
        description="Repeatable: scheduled | in_progress | completed | cancelled"),
    date_from: Optional[date] = Query(default=None, alias="from",
                                      description="Inclusive on visit_date"),
    date_to: Optional[date] = Query(default=None, alias="to",
                                    description="Inclusive on visit_date"),
    has_transcript: Optional[bool] = Query(default=None),
    patient_id: Optional[UUID] = Query(default=None),
    pending_only: bool = Query(default=False,
                               description="Only visits without insights"),
    sort: str = Query(default="date_desc",
                      pattern="^(date_desc|date_asc|patient)$"),
    limit: Optional[int] = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> List[VisitOut]:
    """The signed-in doctor's visits, newest first — what a schedule queue needs.

    Filtering by another doctor's `patient_id` returns an empty list, which is
    exactly what a patient with no visits returns, so a guessed id reveals nothing.
    """
    page = visit_service.list_visits(
        conn,
        doctor.doctor_id,
        patient_id=str(patient_id) if patient_id else None,
        statuses=status_filter,
        date_from=date_from,
        date_to=date_to,
        has_transcript=has_transcript,
        pending_only=pending_only,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Total-Count"] = str(page.total)
    return [VisitOut.model_validate(r) for r in page.items]


@router.get(
    "/{visit_id}",
    response_model=VisitOut,
    responses={404: {"model": ErrorResponse}},
    summary="Get one visit",
)
def get_visit(
    visit_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """One visit. 404, not 403, for another doctor's visit."""
    return VisitOut.model_validate(
        visit_service.get_visit(conn, doctor.doctor_id, str(visit_id)))


@router.patch(
    "/{visit_id}",
    response_model=VisitOut,
    responses={404: {"model": ErrorResponse}},
    summary="Correct a visit's details",
)
def update_visit(
    visit_id: UUID,
    payload: VisitUpdate,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """Correct a visit's date, time or type.

    This cannot change `visit_status` and cannot touch the transcript.
    Completion and cancellation are explicit transitions via `/complete` and
    `/cancel`, which record their own timestamps and — for `/cancel` — reopen the
    follow-ups that visit had resolved (§8.17). A transcript is attached by
    uploading it to `/transcript`, which derives its storage address from the
    visit id (§8.19).
    """
    visit = visit_service.update_visit(
        conn,
        doctor.doctor_id,
        str(visit_id),
        visit_date=payload.visit_date,
        visit_time=payload.visit_time,
        visit_type=payload.visit_type,
    )
    return VisitOut.model_validate(visit)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

@router.post(
    "/{visit_id}/complete",
    response_model=VisitOut,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse, "description": "Visit is cancelled"}},
    summary="Mark a visit completed",
)
def complete_visit(
    visit_id: UUID,
    payload: Optional[VisitCompleteRequest] = None,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """Complete a consultation, optionally correcting the actual date and time.

    Idempotent: completing an already-completed visit returns 200 and leaves
    `completed_at` unchanged, so a double click is harmless.
    """
    options = payload or VisitCompleteRequest()
    visit = visit_service.complete_visit(
        conn, doctor.doctor_id, str(visit_id),
        visit_date=options.visit_date,
        visit_time=options.visit_time,
    )
    return VisitOut.model_validate(visit)


@router.post(
    "/{visit_id}/cancel",
    response_model=VisitOut,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse, "description": "Visit has insights"}},
    summary="Cancel a visit",
)
def cancel_visit(
    visit_id: UUID,
    payload: Optional[VisitCancelRequest] = None,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """Cancel a visit and reopen any follow-up it had resolved.

    A cancelled visit no longer answers anything, so follow-ups it closed return
    to the queue; otherwise the patient would be silently dropped.
    """
    options = payload or VisitCancelRequest()
    visit = visit_service.cancel_visit(conn, doctor.doctor_id, str(visit_id),
                                       reason=options.reason)
    return VisitOut.model_validate(visit)


# --------------------------------------------------------------------------- #
# Transcript
# --------------------------------------------------------------------------- #

@router.post(
    "/{visit_id}/transcript",
    response_model=VisitOut,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse,
                     "description": "Visit not completed, or transcript already "
                                    "attached without replace=true"},
               413: {"model": ErrorResponse, "description": "Transcript too large"},
               422: {"model": ErrorResponse,
                     "description": "Not a .txt file, not UTF-8, or empty"}},
    summary="Upload a visit's transcript",
)
async def upload_transcript(
    visit_id: UUID,
    file: UploadFile = File(..., description="Plain UTF-8 .txt transcript"),
    replace: bool = Form(default=False,
                         description="Required to overwrite an existing transcript"),
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> VisitOut:
    """Attach a transcript to a completed visit.

    Only completed visits accept an upload, which is what makes an Add Transcript
    button legitimately disabled until the consultation is marked done.

    The file is stored as `transcripts/{visit_id}.txt` — never under the client's
    filename, which is attacker controlled. The original name is kept on the
    visit for display only.
    """
    raw = await file.read()
    visit = transcript_service.store_transcript(
        conn, doctor.doctor_id, str(visit_id),
        filename=file.filename, raw=raw, replace=replace)
    return VisitOut.model_validate(visit)


@router.get(
    "/{visit_id}/transcript",
    response_model=TranscriptOut,
    responses={404: {"model": ErrorResponse,
                     "description": "Unknown visit, or no transcript attached"}},
    summary="Read a visit's transcript",
)
def read_transcript(
    visit_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> TranscriptOut:
    """The transcript text, so a brief can be checked against its source.

    Verbatim consultation content. 404 for another doctor's visit.
    """
    return TranscriptOut.model_validate(
        transcript_service.read_transcript(conn, doctor.doctor_id, str(visit_id)))


@router.delete(
    "/{visit_id}/transcript",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse, "description": "Visit has insights"}},
    summary="Delete a visit's transcript",
)
def delete_transcript(
    visit_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> Response:
    """Remove the transcript file and clear its columns.

    Refused once insights exist: removing the source of a generated brief would
    leave a clinical record that cannot be audited.
    """
    transcript_service.delete_transcript(conn, doctor.doctor_id, str(visit_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Insights
# --------------------------------------------------------------------------- #

@router.post(
    "/{visit_id}/insights",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"model": ErrorResponse, "description": "Unknown visit"},
               409: {"model": ErrorResponse,
                     "description": "No transcript, insights exist, or a job is "
                                    "already running"}},
    summary="Generate insights for a visit",
)
def generate_insights(
    visit_id: UUID,
    background: BackgroundTasks,
    payload: Optional[GenerateInsightsRequest] = None,
    doctor: CurrentDoctor = Depends(require_doctor),
) -> JobAccepted:
    """Start the three-layer chain and return a job to poll.

    202 rather than the finished insight because the chain is three sequential
    model calls, roughly a minute, which is past common proxy timeouts.

    All three layers always run. A partial run produces a brief with no care plan
    — the doctor-facing part — so there is no `stop_after` to ask for one.

    `poll_url` and `insights_url` are **root-relative and already include the
    `/api/v1` prefix**. Join them against the server origin, not against a base
    URL that already ends in `/api/v1`.

    Regenerating overwrites the previous brief: `visit_insights` is upserted in
    place, so there is no history. Warn before offering it.
    """
    options = payload or GenerateInsightsRequest()
    job = job_service.enqueue(
        doctor.doctor_id,
        visit_id=str(visit_id),
        regenerate=options.regenerate,
        model=options.model,
    )
    background.add_task(job_service.run_job, job.job_id)

    prefix = get_settings().api_prefix
    return JobAccepted(
        job_id=job.job_id,
        visit_id=job.visit_id,
        status=job.status,
        poll_url=f"{prefix}/jobs/{job.job_id}",
        insights_url=f"{prefix}/visits/{visit_id}/insights",
    )


@router.get(
    "/{visit_id}/insights",
    response_model=InsightOut,
    responses={404: {"model": ErrorResponse,
                     "description": "Unknown visit, or no insights yet"}},
    summary="Read a visit's insights",
)
def read_insights(
    visit_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> InsightOut:
    """The full generated brief. 404 for another doctor's visit."""
    return InsightOut.model_validate(
        insight_service.get_insights(conn, doctor.doctor_id, str(visit_id)))


@router.get(
    "/{visit_id}/job",
    response_model=Optional[JobOut],
    responses={404: {"model": ErrorResponse, "description": "Unknown visit"}},
    summary="The latest insight job for a visit",
)
def latest_job(
    visit_id: UUID,
    response: Response,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
):
    """The most recent job for a visit, so a page reload can resume polling.

    Returns 204 when the visit has never had a job — distinguishable from a 404
    for an unknown visit, so the UI can tell "not generated yet" from "no such
    visit".
    """
    job = visit_service.latest_job(conn, doctor.doctor_id, str(visit_id))
    if job is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    return JobOut.model_validate(job)
