"""
Chunked audio upload bookkeeping.

`audio_uploads` reaches its tenant the same way `insight_jobs` does: `visit_id ->
visits.patient_id -> patients.doctor_id`. Every read and write that starts from a
client-supplied id takes a `DoctorScope` second and carries the tenancy predicate
in its own WHERE, so an upload id from another doctor's visit finds nothing and the
caller answers 404.

The unscoped functions below are reached only after a scoped lookup has already
proved ownership (the chunk and status bookkeeping, keyed on an upload id the
service just loaded under scope), or from the background worker after it resolved
the owning doctor (`owner_doctor_id`), or at startup with no request at all
(`fail_orphans`, `stale_receiving`). Same split as jobs_repo.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from app.db.models import AudioUploadRow
from app.db.tenancy import DoctorScope, is_scoped

ACTIVE = ("receiving", "processing")

# The chunk rows are deleted once a recording is stitched, so after that the
# progress figures come from the plan, and last_chunk_at (only meaningful while
# receiving, to show when an interrupted live recording stopped) is NULL.
_COLUMNS = """
    u.upload_id::text, u.visit_id::text, u.status, u.mode, u.filename, u.extension,
    u.total_bytes, u.chunk_bytes, u.chunk_count, u.replace_transcript,
    u.storage_provider, u.audio_path, u.duration_seconds, u.speakers, u.warnings,
    u.error_code, u.error_message, u.worker_id,
    CASE WHEN u.audio_path IS NOT NULL THEN u.chunk_count
         ELSE (SELECT count(*)::int FROM audio_upload_chunks c
               WHERE c.upload_id = u.upload_id)
    END AS chunks_received,
    CASE WHEN u.audio_path IS NOT NULL THEN coalesce(u.total_bytes, 0)
         ELSE (SELECT coalesce(sum(c.size_bytes), 0)::bigint
               FROM audio_upload_chunks c WHERE c.upload_id = u.upload_id)
    END AS bytes_received,
    (SELECT max(c.received_at) FROM audio_upload_chunks c
     WHERE c.upload_id = u.upload_id) AS last_chunk_at,
    u.created_at, u.updated_at, u.completed_at
"""

# Correlated against the outer query's upload row, aliased `u`.
_OWNED_BY_SCOPE = """
    EXISTS (SELECT 1 FROM visits v
            JOIN patients p ON p.patient_id = v.patient_id
            WHERE v.visit_id = u.visit_id
              AND p.doctor_id = %(scope_doctor_id)s)
"""


def _row(record: Optional[Dict[str, Any]]) -> Optional[AudioUploadRow]:
    return AudioUploadRow(**record) if record else None


def _scoped(scope: DoctorScope, *conditions: str) -> Tuple[List[str], Dict[str, Any]]:
    clauses = list(conditions)
    params: Dict[str, Any] = {}
    if is_scoped(scope):
        clauses.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)
    return clauses, params


# --------------------------------------------------------------------------- #
# Scoped: everything that starts from a client-supplied id
# --------------------------------------------------------------------------- #

def create(
    conn: psycopg.Connection,
    scope: DoctorScope,
    *,
    visit_id: str,
    filename: Optional[str],
    extension: str,
    total_bytes: Optional[int],
    chunk_bytes: int,
    chunk_count: Optional[int],
    replace_transcript: bool,
    storage_provider: str,
    mode: str = "file",
) -> Optional[AudioUploadRow]:
    """Start an upload for a visit the scoped doctor owns.

    INSERT ... SELECT under the doctor filter, like jobs_repo.create, so another
    doctor's visit id inserts nothing and returns None. A live upload passes no
    plan (`total_bytes` and `chunk_count` None); the table refuses that for a
    file upload.
    """
    conditions = ["v.visit_id = %(visit_id)s"]
    params: Dict[str, Any] = {
        "visit_id": visit_id, "filename": filename, "extension": extension,
        "total_bytes": total_bytes, "chunk_bytes": chunk_bytes,
        "chunk_count": chunk_count, "replace_transcript": replace_transcript,
        "storage_provider": storage_provider, "mode": mode,
    }
    if is_scoped(scope):
        conditions.append("p.doctor_id = %(scope_doctor_id)s")
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO audio_uploads AS u
                    (visit_id, mode, filename, extension, total_bytes, chunk_bytes,
                     chunk_count, replace_transcript, storage_provider)
                SELECT v.visit_id, %(mode)s, %(filename)s, %(extension)s,
                       %(total_bytes)s, %(chunk_bytes)s, %(chunk_count)s,
                       %(replace_transcript)s, %(storage_provider)s
                FROM visits v
                JOIN patients p ON p.patient_id = v.patient_id
                WHERE {' AND '.join(conditions)}
                RETURNING {_COLUMNS}""",
            params,
        )
        return _row(cur.fetchone())


def get(conn: psycopg.Connection, scope: DoctorScope,
        upload_id: str) -> Optional[AudioUploadRow]:
    """One upload, or None when unknown or belonging to another doctor's visit."""
    conditions, params = _scoped(scope, "u.upload_id = %(upload_id)s")
    params["upload_id"] = upload_id
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM audio_uploads u WHERE {' AND '.join(conditions)}",
            params,
        )
        return _row(cur.fetchone())


def latest_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                     visit_id: str) -> Optional[AudioUploadRow]:
    conditions, params = _scoped(scope, "u.visit_id = %(visit_id)s")
    params["visit_id"] = visit_id
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_COLUMNS} FROM audio_uploads u
                WHERE {' AND '.join(conditions)}
                ORDER BY u.created_at DESC LIMIT 1""",
            params,
        )
        return _row(cur.fetchone())


def unfinished_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                         visit_id: str) -> List[AudioUploadRow]:
    """Uploads for a visit that are receiving, processing, or failed."""
    conditions, params = _scoped(
        scope, "u.visit_id = %(visit_id)s",
        "u.status IN ('receiving', 'processing', 'failed')")
    params["visit_id"] = visit_id
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM audio_uploads u WHERE {' AND '.join(conditions)}",
            params,
        )
        return [AudioUploadRow(**r) for r in cur.fetchall()]


def record_chunk(conn: psycopg.Connection, scope: DoctorScope, upload_id: str,
                 index: int, size_bytes: int) -> bool:
    """Note that chunk `index` is stored. Idempotent: a resend replaces it.

    Only while the upload is receiving, and only for the scoped doctor's upload;
    False when neither holds, which the caller reports as a conflict.
    """
    conditions, params = _scoped(scope, "u.upload_id = %(upload_id)s",
                                 "u.status = 'receiving'")
    params.update({"upload_id": upload_id, "index": index, "size": size_bytes})
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO audio_upload_chunks (upload_id, chunk_index, size_bytes)
                SELECT u.upload_id, %(index)s, %(size)s
                FROM audio_uploads u
                WHERE {' AND '.join(conditions)}
                ON CONFLICT (upload_id, chunk_index)
                DO UPDATE SET size_bytes = EXCLUDED.size_bytes, received_at = now()
                RETURNING chunk_index""",
            params,
        )
        return cur.fetchone() is not None


def claim(conn: psycopg.Connection, scope: DoctorScope, upload_id: str,
          worker_id: str) -> Optional[AudioUploadRow]:
    """Move a receiving or failed upload to processing, atomically.

    A conditional UPDATE rather than read-then-write, so a double click on Finish
    claims it once and the second request gets None.
    """
    conditions, params = _scoped(scope, "u.upload_id = %(upload_id)s",
                                 "u.status IN ('receiving', 'failed')")
    params.update({"upload_id": upload_id, "worker_id": worker_id})
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE audio_uploads u
                SET status = 'processing', worker_id = %(worker_id)s,
                    error_code = NULL, error_message = NULL, updated_at = now()
                WHERE {' AND '.join(conditions)}
                RETURNING {_COLUMNS}""",
            params,
        )
        return _row(cur.fetchone())


def claim_live(conn: psycopg.Connection, scope: DoctorScope, upload_id: str,
               worker_id: str, *, total_bytes: int,
               chunk_count: int) -> Optional[AudioUploadRow]:
    """Fix a live recording's plan and move it to processing, atomically.

    The plan was worked out from the chunk rows a moment ago; the WHERE checks
    the rows still say exactly that -- the same count, the same total, nothing
    past the last index -- in the statement that writes it. So a chunk that
    lands in between makes this match nothing (None, a conflict the browser
    retries) instead of producing a plan that disagrees with what is stored.
    """
    conditions, params = _scoped(
        scope, "u.upload_id = %(upload_id)s", "u.status = 'receiving'",
        "u.mode = 'live'", "u.chunk_count IS NULL",
        """(SELECT count(*) FROM audio_upload_chunks c
            WHERE c.upload_id = u.upload_id) = %(chunk_count)s""",
        """(SELECT coalesce(sum(c.size_bytes), 0) FROM audio_upload_chunks c
            WHERE c.upload_id = u.upload_id) = %(total_bytes)s""",
        """NOT EXISTS (SELECT 1 FROM audio_upload_chunks c
                       WHERE c.upload_id = u.upload_id
                         AND c.chunk_index >= %(chunk_count)s)""")
    params.update({"upload_id": upload_id, "worker_id": worker_id,
                   "total_bytes": total_bytes, "chunk_count": chunk_count})
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE audio_uploads u
                SET status = 'processing', worker_id = %(worker_id)s,
                    total_bytes = %(total_bytes)s, chunk_count = %(chunk_count)s,
                    error_code = NULL, error_message = NULL, updated_at = now()
                WHERE {' AND '.join(conditions)}
                RETURNING {_COLUMNS}""",
            params,
        )
        return _row(cur.fetchone())


# --------------------------------------------------------------------------- #
# Unscoped: keyed on an upload id whose ownership is already established
# --------------------------------------------------------------------------- #

def owner_doctor_id(conn: psycopg.Connection, upload_id: str) -> Optional[str]:
    """Which doctor an upload belongs to. Establishes the worker's scope, like
    jobs_repo.owner_doctor_id."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.doctor_id::text AS doctor_id
               FROM audio_uploads u
               JOIN visits v ON v.visit_id = u.visit_id
               JOIN patients p ON p.patient_id = v.patient_id
               WHERE u.upload_id = %s""",
            (upload_id,),
        )
        record = cur.fetchone()
    return record["doctor_id"] if record else None


def chunk_sizes(conn: psycopg.Connection, upload_id: str) -> Dict[int, int]:
    """index -> stored size, for every chunk recorded so far."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT chunk_index, size_bytes FROM audio_upload_chunks
               WHERE upload_id = %s ORDER BY chunk_index""",
            (upload_id,),
        )
        return {r["chunk_index"]: r["size_bytes"] for r in cur.fetchall()}


def mark_abandoned(conn: psycopg.Connection, upload_ids: List[str]) -> None:
    """Retire unfinished uploads whose files the caller is removing."""
    if not upload_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE audio_uploads
               SET status = 'abandoned', audio_path = NULL, updated_at = now()
               WHERE upload_id = ANY(%s::uuid[])
                 AND status IN ('receiving', 'processing', 'failed')""",
            (upload_ids,),
        )
        cur.execute("DELETE FROM audio_upload_chunks WHERE upload_id = ANY(%s::uuid[])",
                    (upload_ids,))


def set_audio_path(conn: psycopg.Connection, upload_id: str, audio_path: str) -> None:
    """Record the stitched recording and forget the chunk rows, together.

    Same transaction, so the row never claims both a recording and a partial set
    of chunks, or neither.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE audio_uploads SET audio_path = %s, updated_at = now()
                   WHERE upload_id = %s""",
                (audio_path, upload_id),
            )
            cur.execute("DELETE FROM audio_upload_chunks WHERE upload_id = %s",
                        (upload_id,))


def mark_completed(
    conn: psycopg.Connection,
    upload_id: str,
    *,
    speakers: Any,
    warnings: Any,
    duration_seconds: Optional[float],
) -> List[Tuple[str, str]]:
    """Complete an upload, superseding the visit's previous recording.

    Returns (storage_provider, audio_path) of each superseded recording, for the
    caller to delete once this has committed. The older row is updated first:
    `uq_audio_uploads_one_recording` allows one completed recording per visit and
    a partial unique index cannot be deferred to the end of the transaction.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """WITH old AS (
                       SELECT o.upload_id, o.storage_provider, o.audio_path
                       FROM audio_uploads o
                       JOIN audio_uploads n ON n.visit_id = o.visit_id
                       WHERE n.upload_id = %(upload_id)s
                         AND o.upload_id <> n.upload_id
                         AND o.status = 'completed'
                       FOR UPDATE OF o)
                   UPDATE audio_uploads a
                   SET status = 'superseded', audio_path = NULL, updated_at = now()
                   FROM old
                   WHERE a.upload_id = old.upload_id
                   RETURNING old.storage_provider, old.audio_path""",
                {"upload_id": upload_id},
            )
            superseded = [(r["storage_provider"], r["audio_path"])
                          for r in cur.fetchall() if r["audio_path"]]
            cur.execute(
                """UPDATE audio_uploads
                   SET status = 'completed', speakers = %s, warnings = %s,
                       duration_seconds = %s, error_code = NULL,
                       error_message = NULL, completed_at = now(), updated_at = now()
                   WHERE upload_id = %s""",
                (Jsonb(speakers), Jsonb(warnings), duration_seconds, upload_id),
            )
    return superseded


def mark_failed(conn: psycopg.Connection, upload_id: str, code: str,
                message: str) -> None:
    """Fail an upload, keeping whatever bytes it has so a retry can use them."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE audio_uploads
               SET status = 'failed', error_code = %s, error_message = %s,
                   updated_at = now()
               WHERE upload_id = %s""",
            (code, message[:2000], upload_id),
        )


# --------------------------------------------------------------------------- #
# Startup housekeeping: cross-doctor on purpose, like jobs_repo.fail_orphans
# --------------------------------------------------------------------------- #

def fail_orphans(conn: psycopg.Connection, worker_id: str) -> int:
    """Fail uploads a previous process left in 'processing'.

    They keep their chunks or recording, so they are retryable from the UI.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE audio_uploads
               SET status = 'failed', error_code = 'interrupted',
                   error_message = 'The server restarted while this recording was '
                                   'being processed. Try again.',
                   updated_at = now()
               WHERE status = 'processing' AND coalesce(worker_id, '') <> %s""",
            (worker_id,),
        )
        return cur.rowcount


def stale_receiving(conn: psycopg.Connection, older_than_hours: int) -> List[AudioUploadRow]:
    """Uploads still receiving with no chunk for `older_than_hours`: the tab that
    was sending them is gone."""
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_COLUMNS} FROM audio_uploads u
                WHERE u.status = 'receiving'
                  AND coalesce((SELECT max(c.received_at) FROM audio_upload_chunks c
                                WHERE c.upload_id = u.upload_id), u.created_at)
                      < now() - make_interval(hours => %s)""",
            (older_than_hours,),
        )
        return [AudioUploadRow(**r) for r in cur.fetchall()]
