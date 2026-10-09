"""
Chunked recording uploads: send the pieces, finish, poll.

Started with `POST /visits/{visit_id}/audio-uploads`, which returns the chunk
plan. Like `GET /jobs/{job_id}`, these routes are keyed on an upload id alone, so
every lookup is scoped through `visit -> patient -> doctor_id` and another
doctor's upload id is the same 404 as an unknown one.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Path,
    UploadFile,
    status,
)

from app.api.deps import CurrentDoctor, get_db, require_doctor
from app.config import get_settings
from app.schemas.common import ErrorResponse
from app.schemas.transcripts import AudioUploadOut
from app.services import audio_upload_service

router = APIRouter(prefix="/audio-uploads", tags=["audio uploads"],
                   dependencies=[Depends(require_doctor)])


@router.put(
    "/{upload_id}/chunks/{index}",
    response_model=AudioUploadOut,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse,
                     "description": "The upload no longer accepts chunks"},
               422: {"model": ErrorResponse,
                     "description": "Index out of range, or not exactly the "
                                    "planned size"},
               503: {"model": ErrorResponse,
                     "description": "Storage unavailable; send the chunk again"}},
    summary="Send one chunk of a recording",
)
def put_chunk(
    upload_id: UUID,
    index: int = Path(..., ge=0, description="0-based chunk number"),
    file: UploadFile = File(..., description="Exactly the bytes of this chunk"),
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> AudioUploadOut:
    """Store chunk `index`. Idempotent: sending it again replaces it, so a chunk
    whose response was lost can simply be resent.

    Every chunk but the last must be exactly `chunk_bytes`; the last is the
    remainder of `size_bytes`.
    """
    # Read at most one byte past the ceiling: enough to reject an oversized chunk
    # without pulling an arbitrarily large body into memory.
    data = file.file.read(get_settings().speech.chunk_bytes + 1)
    upload = audio_upload_service.put_chunk(
        conn, doctor.doctor_id, str(upload_id), index, data)
    return AudioUploadOut.from_row(upload)


@router.post(
    "/{upload_id}/finish",
    response_model=AudioUploadOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse,
                     "description": "Chunks missing (listed in details.missing), "
                                    "or the upload is already processing, "
                                    "completed or replaced"}},
    summary="Finish a recording upload and start transcription",
)
def finish_upload(
    upload_id: UUID,
    background: BackgroundTasks,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> AudioUploadOut:
    """Stitch the chunks into one stored recording, transcribe it, and generate
    insights from the transcript.

    202, then poll `GET /audio-uploads/{upload_id}`: stitching, speech-to-text
    and speaker labelling together run past request timeouts. When it reaches
    `completed`, the insight job has already been queued — follow it with
    `GET /visits/{visit_id}/job`; there is no need to call `POST /insights`.

    Also how a `failed` upload is retried — its chunks or recording are kept, so
    nothing is re-sent.
    """
    upload = audio_upload_service.finish_upload(conn, doctor.doctor_id, str(upload_id))
    background.add_task(audio_upload_service.process_upload, upload.upload_id,
                        doctor.name)
    return AudioUploadOut.from_row(upload)


@router.get(
    "/{upload_id}",
    response_model=AudioUploadOut,
    responses={404: {"model": ErrorResponse}},
    summary="Poll a recording upload",
)
def get_upload(
    upload_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> AudioUploadOut:
    """Progress and outcome. `completed` means the transcript is attached."""
    return AudioUploadOut.from_row(
        audio_upload_service.get_upload(conn, doctor.doctor_id, str(upload_id)))
