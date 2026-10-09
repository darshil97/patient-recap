"""
Chunked audio uploads: receive the pieces, stitch one recording, transcribe it.

    POST /visits/{id}/audio-uploads           start: checks, chunk plan  -> receiving
    PUT  /audio-uploads/{id}/chunks/{index}   one piece, exact size, idempotent
    POST /audio-uploads/{id}/finish           all pieces here            -> processing
         background: stitch -> delete chunks -> Deepgram + speaker LLM -> transcript
                     -> insight job queued -> upload completed -> job runs
    GET  /audio-uploads/{id}                  poll                 -> completed | failed
    GET  /visits/{id}/job                     then follow the brief as for a .txt

WHY CHUNKS

Every browser request goes through the Next.js BFF, and on Vercel a function's
request body is capped at 4.5 MB. A consultation recording is routinely larger, so
the browser sends it in `speech.chunk_bytes` pieces. Each piece must be *exactly*
its planned size, which is what makes "every chunk arrived" mean "the stitched file
is byte-for-byte the original".

WHAT IS KEPT

The stitched recording, permanently, at recordings/<visit>/<upload><ext>. The
chunks are deleted as soon as the recording is stored and its size verified. A
replacement recording is written beside the current one, and the old one is
removed only once the new upload has completed, so a failed replacement never
costs the visit the recording its transcript came from.

WHY PROCESSING IS A BACKGROUND TASK

Stitching plus speech-to-text plus a model call can run past both the BFF's
timeout and a Vercel function's duration limit. Same in-process pattern as insight
jobs (§8.9): state lives in Postgres, `finish` returns 202, the browser polls, and
startup fails anything a dead process left in 'processing'. A failed upload keeps
its bytes, so Finish again retries without re-uploading.

TENANCY ON THE ASYNC PATH

As in job_service: `process_upload` holds only an upload id, resolves the owning
doctor from it, and does every read and write under that scope.
"""

from __future__ import annotations

import dataclasses
import logging
import tempfile
from typing import Iterable, List, Optional

import psycopg

from app.config import get_settings
from app.core.errors import (
    AppError,
    AudioStorageError,
    ConflictError,
    ModelNotConfiguredError,
    NotFoundError,
    ValidationError,
)
from app.db.models import AudioUploadRow
from app.db.pool import connection
from app.db.repositories import audio_uploads as uploads_repo
from app.db.tenancy import DoctorScope
from app.domain.audio import (
    audio_extension,
    check_audio_size,
    chunk_count,
    content_type_for,
    display_filename,
    expected_chunk_size,
)
from app.llm.base import ChatClient
from app.services import audio_transcript_service, job_service, transcript_service
from app.services.job_service import WORKER_ID
from app.services.visit_service import get_visit
from app.speech.deepgram import KEY_VARIABLE as DEEPGRAM_KEY_VARIABLE
from app.storage import default_provider
from app.storage.audio import (
    CHUNK_CONTENT_TYPE,
    chunk_locator,
    get_audio_store,
    recording_locator,
)

logger = logging.getLogger(__name__)

# A receiving upload with no new chunk for this long is assumed abandoned (the tab
# that was sending it is gone) and its chunks are removed at startup.
STALE_AFTER_HOURS = 24
COPY_BUFFER_BYTES = 1024 * 1024


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _require_processing_configured() -> None:
    """Refuse to start an upload that could never be processed.

    Checked at the start rather than at the end, so a doctor is not told about a
    missing key after sending 100 MB.
    """
    settings = get_settings()
    if not settings.speech.deepgram.configured:
        raise ModelNotConfiguredError(
            f"Audio transcription is not configured. Set {DEEPGRAM_KEY_VARIABLE} in "
            f"backend/.env, or upload a text transcript instead.")
    if not settings.llm.api_key_configured:
        raise ModelNotConfiguredError(
            f"No API key is configured for the {settings.llm.provider} model "
            f"provider, which labels the speakers in a recording.")


def _delete_quietly(provider: str, locators: Iterable[str]) -> None:
    """Best-effort removal. A leftover file is a tidy-up job, not a failure."""
    try:
        store = get_audio_store(provider)
    except AppError:
        logger.warning("Could not open the %s audio store to tidy up", provider,
                       exc_info=True)
        return
    for locator in locators:
        try:
            store.delete(locator)
        except Exception:
            logger.warning("Could not delete %s (%s); it is safe to remove by hand",
                           locator, provider, exc_info=True)


def _discard(conn: psycopg.Connection, uploads: List[AudioUploadRow]) -> None:
    """Retire unfinished uploads and remove their chunks and any stitched file."""
    if not uploads:
        return
    files = []
    for upload in uploads:
        locators = [chunk_locator(upload.upload_id, i)
                    for i in uploads_repo.chunk_sizes(conn, upload.upload_id)]
        if upload.audio_path:
            locators.append(upload.audio_path)
        files.append((upload.storage_provider, locators))

    uploads_repo.mark_abandoned(conn, [u.upload_id for u in uploads])
    for provider, locators in files:
        _delete_quietly(provider, locators)
    logger.info("Discarded %d unfinished audio upload(s)", len(uploads))


def _get(conn: psycopg.Connection, scope: DoctorScope, upload_id: str) -> AudioUploadRow:
    upload = uploads_repo.get(conn, scope, upload_id)
    if upload is None:
        raise NotFoundError(f"No audio upload with id {upload_id}.")
    return upload


# --------------------------------------------------------------------------- #
# Request path
# --------------------------------------------------------------------------- #

def start_upload(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    *,
    filename: Optional[str],
    size_bytes: int,
    replace: bool = False,
) -> AudioUploadRow:
    """Plan a chunked upload for a completed visit.

    Everything that would make the recording unusable is checked here, before a
    byte is sent: the visit rules, the format, the size, and the keys needed to
    process it. Any unfinished upload for the same visit (a closed tab, or a
    failed one the doctor chose not to retry) is discarded; one still processing
    is a conflict.
    """
    settings = get_settings()
    visit = get_visit(conn, scope, visit_id)
    transcript_service.check_can_attach(visit, replace)
    extension = audio_extension(filename)
    check_audio_size(size_bytes, settings.speech.max_upload_bytes)
    _require_processing_configured()

    unfinished = uploads_repo.unfinished_for_visit(conn, scope, visit_id)
    if any(u.status == "processing" for u in unfinished):
        raise ConflictError(
            "A recording for this visit is already being processed. Wait for it to "
            "finish before uploading another.",
            details={"visit_id": visit_id})
    _discard(conn, unfinished)

    chunk_bytes = settings.speech.chunk_bytes
    try:
        upload = uploads_repo.create(
            conn, scope,
            visit_id=visit_id,
            filename=display_filename(filename),
            extension=extension,
            total_bytes=size_bytes,
            chunk_bytes=chunk_bytes,
            chunk_count=chunk_count(size_bytes, chunk_bytes),
            replace_transcript=replace,
            storage_provider=default_provider(),
        )
    except psycopg.errors.UniqueViolation as exc:
        # Two starts for the same visit raced; the other one won.
        raise ConflictError("Another upload for this visit has just started.",
                            details={"visit_id": visit_id}) from exc
    if upload is None:
        raise NotFoundError(f"No visit with id {visit_id}.")

    logger.info("Started audio upload %s for %s (%d bytes in %d chunks)",
                upload.upload_id, visit.label, size_bytes, upload.chunk_count)
    return upload


def put_chunk(
    conn: psycopg.Connection,
    scope: DoctorScope,
    upload_id: str,
    index: int,
    data: bytes,
) -> AudioUploadRow:
    """Store one chunk. Sending the same index again replaces it.

    The bytes are stored before the chunk is recorded, so a recorded chunk always
    has its bytes; a failure in between leaves an unrecorded file that the resend
    overwrites.
    """
    upload = _get(conn, scope, upload_id)
    if upload.status != "receiving":
        raise ConflictError(
            f"This upload is {upload.status} and no longer accepts chunks.",
            details={"upload_id": upload_id, "status": upload.status})
    if not 0 <= index < upload.chunk_count:
        raise ValidationError(
            f"Chunk index {index} is out of range; this upload has "
            f"{upload.chunk_count} chunks (0 to {upload.chunk_count - 1}).",
            details={"index": index, "chunk_count": upload.chunk_count})

    expected = expected_chunk_size(index, upload.total_bytes, upload.chunk_bytes)
    if len(data) != expected:
        raise ValidationError(
            f"Chunk {index} is {len(data):,} bytes; it must be exactly {expected:,}.",
            details={"index": index, "size_bytes": len(data),
                     "expected_bytes": expected})

    store = get_audio_store(upload.storage_provider)
    locator = chunk_locator(upload_id, index)
    store.write_bytes(locator, data, CHUNK_CONTENT_TYPE)

    if not uploads_repo.record_chunk(conn, scope, upload_id, index, len(data)):
        # The upload stopped receiving while this chunk was in flight (a newer
        # upload for the visit discarded it). Do not leave the file behind.
        _delete_quietly(upload.storage_provider, [locator])
        raise ConflictError("This upload no longer accepts chunks.",
                            details={"upload_id": upload_id})
    return _get(conn, scope, upload_id)


def finish_upload(conn: psycopg.Connection, scope: DoctorScope,
                  upload_id: str) -> AudioUploadRow:
    """Hand a fully received (or failed, retryable) upload to the background task.

    The caller schedules `process_upload` after this returns. Missing chunks are a
    409 naming them, so the browser can resend exactly those.
    """
    upload = _get(conn, scope, upload_id)
    if upload.status == "processing":
        raise ConflictError("This recording is already being processed.",
                            details={"upload_id": upload_id})
    if upload.status == "completed":
        raise ConflictError("This recording has already been transcribed.",
                            details={"upload_id": upload_id})
    if upload.status in ("abandoned", "superseded"):
        raise ConflictError("This upload was replaced by a newer one.",
                            details={"upload_id": upload_id, "status": upload.status})

    if not upload.recording_stored:
        received = uploads_repo.chunk_sizes(conn, upload_id)
        missing = [i for i in range(upload.chunk_count) if i not in received]
        if missing:
            raise ConflictError(
                f"{len(missing)} of {upload.chunk_count} chunks have not arrived yet.",
                details={"upload_id": upload_id, "missing": missing[:100]})

    claimed = uploads_repo.claim(conn, scope, upload_id, WORKER_ID)
    if claimed is None:
        raise ConflictError("This upload changed while finishing; reload and try again.",
                            details={"upload_id": upload_id})
    logger.info("Audio upload %s queued for processing", upload_id)
    return claimed


def get_upload(conn: psycopg.Connection, scope: DoctorScope,
               upload_id: str) -> AudioUploadRow:
    """Poll one upload. 404 for another doctor's, like GET /jobs/{id}."""
    return _get(conn, scope, upload_id)


def latest_upload(conn: psycopg.Connection, scope: DoctorScope,
                  visit_id: str) -> Optional[AudioUploadRow]:
    """The newest upload for a visit, so a reload can resume polling."""
    get_visit(conn, scope, visit_id)
    return uploads_repo.latest_for_visit(conn, scope, visit_id)


# --------------------------------------------------------------------------- #
# Background path
# --------------------------------------------------------------------------- #

def _stitch(conn: psycopg.Connection, upload: AudioUploadRow) -> str:
    """Join the chunks into one recording, verify it, then delete the chunks.

    Assembled in a temporary file on disk, not in memory, and stored with a single
    PUT. Each chunk's size is re-checked on the way through, and the stored
    object's size against the planned total, before anything is deleted: until
    the recording is verified, the chunks are the only copy.
    """
    store = get_audio_store(upload.storage_provider)
    target = recording_locator(upload.visit_id, upload.upload_id, upload.extension)

    with tempfile.TemporaryFile() as assembled:
        for index in range(upload.chunk_count):
            expected = expected_chunk_size(index, upload.total_bytes, upload.chunk_bytes)
            copied = 0
            with store.open(chunk_locator(upload.upload_id, index)) as part:
                while True:
                    block = part.read(COPY_BUFFER_BYTES)
                    if not block:
                        break
                    assembled.write(block)
                    copied += len(block)
            if copied != expected:
                raise AudioStorageError(
                    f"Chunk {index} is {copied:,} bytes in storage; expected "
                    f"{expected:,}. Upload the recording again.",
                    details={"upload_id": upload.upload_id, "index": index})

        size = assembled.tell()
        assembled.seek(0)
        store.write_file(target, assembled, size, content_type_for(upload.extension))

    stored = store.size(target)
    if stored != upload.total_bytes:
        raise AudioStorageError(
            f"The stored recording is {stored} bytes; expected {upload.total_bytes}.",
            details={"upload_id": upload.upload_id})

    uploads_repo.set_audio_path(conn, upload.upload_id, target)
    _delete_quietly(upload.storage_provider,
                    [chunk_locator(upload.upload_id, i) for i in range(upload.chunk_count)])
    logger.info("Stitched audio upload %s into %s (%d bytes)",
                upload.upload_id, target, size)
    return target


def _queue_insights(scope: DoctorScope, visit_id: str, regenerate: bool) -> Optional[str]:
    """Queue insight generation for a freshly transcribed visit.

    Best-effort: the transcript is already stored, so a refusal here (a job
    already running because the doctor clicked Generate meanwhile, say) must not
    fail the upload. The visit then sits with a transcript and a Generate
    insights button, which is the retry path.
    """
    try:
        job = job_service.enqueue(scope, visit_id=visit_id, regenerate=regenerate)
    except AppError as exc:
        logger.warning("Transcribed visit %s, but could not queue insights: %s",
                       visit_id, exc.message)
        return None
    return job.job_id


def process_upload(upload_id: str, doctor_name: Optional[str] = None,
                   llm: Optional[ChatClient] = None) -> None:
    """Stitch (if not yet), transcribe, attach, then generate insights.
    Background task; never raises.

    Every outcome is recorded on the upload row, because the browser is polling
    it. A failure keeps the chunks or the recording, so it can be retried.

    Insights start here rather than from the browser, so a brief is compiled
    whether or not the tab that uploaded the recording is still open. The job is
    queued *before* the upload is marked completed, so a poller that sees
    `completed` always finds the job behind it via GET /visits/{id}/job. It runs
    after the upload's own connection is returned to the pool.
    """
    job_id = _process(upload_id, doctor_name, llm)
    if job_id:
        job_service.run_job(job_id)


def _process(upload_id: str, doctor_name: Optional[str],
             llm: Optional[ChatClient]) -> Optional[str]:
    """The upload half of process_upload. Returns the queued insight job, if any."""
    job_id: Optional[str] = None
    with connection() as conn:
        owner = uploads_repo.owner_doctor_id(conn, upload_id)
        if owner is None:
            logger.error("Audio upload %s has no resolvable owning doctor; not "
                         "processing", upload_id)
            uploads_repo.mark_failed(conn, upload_id, "not_found",
                                     "The visit behind this upload no longer exists.")
            return None

        upload = uploads_repo.get(conn, owner, upload_id)
        if upload is None or upload.status != "processing":
            logger.warning("Audio upload %s is %s, not processing; skipping",
                           upload_id, upload.status if upload else "gone")
            return None

        try:
            if not upload.audio_path:
                upload = dataclasses.replace(upload, audio_path=_stitch(conn, upload))

            store = get_audio_store(upload.storage_provider)
            with store.open(upload.audio_path) as recording:
                result = audio_transcript_service.transcribe_into_visit(
                    conn, owner, upload.visit_id,
                    audio=recording,
                    size=upload.total_bytes,
                    extension=upload.extension,
                    filename=upload.filename,
                    replace=upload.replace_transcript,
                    doctor_name=doctor_name,
                    llm=llm,
                )

            # A new transcript makes any earlier brief describe a different
            # consultation text, so an existing one is rebuilt, not kept.
            job_id = _queue_insights(owner, upload.visit_id,
                                     regenerate=result.visit.insights_generated)

            superseded = uploads_repo.mark_completed(
                conn, upload_id,
                speakers=[dataclasses.asdict(s) for s in result.speakers],
                warnings=result.warnings,
                duration_seconds=result.duration_seconds,
            )
            for provider, path in superseded:
                _delete_quietly(provider, [path])
            logger.info("Audio upload %s completed; insight job %s", upload_id,
                        job_id or "not queued")

        except AppError as exc:
            uploads_repo.mark_failed(conn, upload_id, exc.code, exc.message)
            logger.warning("Audio upload %s failed: %s", upload_id, exc.message)

        except Exception:  # pragma: no cover - unexpected
            uploads_repo.mark_failed(conn, upload_id, "internal_error",
                                     "Something went wrong while processing the "
                                     "recording. Try again.")
            logger.exception("Audio upload %s failed unexpectedly", upload_id)
    return job_id


def reap(conn: psycopg.Connection) -> None:
    """Startup housekeeping, cross-doctor like jobs_repo.fail_orphans.

    Fails uploads a dead process left in 'processing' (they keep their bytes and
    are retryable), and discards uploads nobody has sent a chunk to for a day.
    Called from job_service.reap_orphans, the one startup reaper.
    """
    failed = uploads_repo.fail_orphans(conn, WORKER_ID)
    if failed:
        logger.warning("Failed %d interrupted audio upload(s) from a previous run",
                       failed)
    _discard(conn, uploads_repo.stale_receiving(conn, STALE_AFTER_HOURS))
