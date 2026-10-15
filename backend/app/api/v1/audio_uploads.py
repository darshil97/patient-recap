"""
Chunked recording uploads: send the pieces, finish, poll.

Started with `POST /visits/{visit_id}/audio-uploads`, which returns the chunk
plan. Like `GET /jobs/{job_id}`, these routes are keyed on an upload id alone, so
every lookup is scoped through `visit -> patient -> doctor_id` and another
doctor's upload id is the same 404 as an unknown one.
"""

from __future__ import annotations

from typing import Optional
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
from app.schemas.transcripts import AudioUploadFinish, AudioUploadOut
from app.services import audio_upload_service

router = APIRouter(prefix="/audio-uploads", tags=["audio uploads"],
                   dependencies=[Depends(require_doctor)])


@router.put(
    "/{upload_id}/chunks/{index}",
    response_model=AudioUploadOut,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse,
                     "description": "The upload no longer accepts chunks"},
               413: {"model": ErrorResponse,
                     "description": "A live recording has reached the size "
                                    "ceiling"},
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
    remainder of `size_bytes`. For a live recording, whose size is not known yet,
    a chunk may be 1 to `chunk_bytes` here and the rule is enforced at finish;
    413 once the recording reaches the size ceiling.
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
                                    "the visit is not completed (live recording), "
                                    "or the upload is already processing, "
                                    "completed, discarded or replaced"},
               413: {"model": ErrorResponse, "description": "Recording too large"},
               422: {"model": ErrorResponse,
                     "description": "chunk_count does not match, a chunk other "
                                    "than the last is short, or nothing was "
                                    "recorded"}},
    summary="Finish a recording upload and start transcription",
)
def finish_upload(
    upload_id: UUID,
    background: BackgroundTasks,
    payload: Optional[AudioUploadFinish] = None,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> AudioUploadOut:
    """Stitch the chunks into one stored recording, transcribe it, and generate
    insights from the transcript.

    202, then poll `GET /audio-uploads/{upload_id}`: stitching, speech-to-text
    and speaker labelling together run past request timeouts. When it reaches
    `completed`, the insight job has already been queued — follow it with
    `GET /visits/{visit_id}/job`; there is no need to call `POST /insights`.

    A live recording needs its visit completed first, and sends
    `{"chunk_count": n}`: the plan is worked out from the chunks that arrived and
    must account for exactly that many. Omit it to process whatever arrived from
    an interrupted session.

    Also how a `failed` upload is retried — its chunks or recording are kept, so
    nothing is re-sent.
    """
    options = payload or AudioUploadFinish()
    upload = audio_upload_service.finish_upload(conn, doctor.doctor_id, str(upload_id),
                                                chunk_count=options.chunk_count)
    background.add_task(audio_upload_service.process_upload, upload.upload_id,
                        doctor.name)
    return AudioUploadOut.from_row(upload)


@router.post(
    "/{upload_id}/discard",
    response_model=AudioUploadOut,
    responses={404: {"model": ErrorResponse},
               409: {"model": ErrorResponse,
                     "description": "Processing, or already transcribed"}},
    summary="Discard an unfinished recording",
)
def discard_upload(
    upload_id: UUID,
    conn: psycopg.Connection = Depends(get_db),
    doctor: CurrentDoctor = Depends(require_doctor),
) -> AudioUploadOut:
    """Delete an unfinished upload's chunks and any stitched recording, and mark
    it `abandoned`.

    For an interrupted live recording the doctor chooses not to process, or a
    failed upload they will not retry. Idempotent. Refused while processing and
    once transcribed: a completed recording is the source of the visit's
    transcript.
    """
    return AudioUploadOut.from_row(
        audio_upload_service.discard_upload(conn, doctor.doctor_id, str(upload_id)))


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
