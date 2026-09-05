"""Insight job bookkeeping."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg

from app.db.models import JobRow

_COLUMNS = """
    job_id::text, visit_id::text, status, layers_completed, stop_after,
    regenerate, model_name, insight_id::text, error_code, error_message,
    total_tokens, worker_id, created_at, started_at, finished_at
"""

ACTIVE = ("queued", "running")


def _row(record: Optional[Dict[str, Any]]) -> Optional[JobRow]:
    return JobRow(**record) if record else None


def get(conn: psycopg.Connection, job_id: str) -> Optional[JobRow]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM insight_jobs WHERE job_id = %s", (job_id,))
        return _row(cur.fetchone())


def active_for_visit(conn: psycopg.Connection, visit_id: str) -> Optional[JobRow]:
    """The queued or running job for a visit, if any.

    A partial unique index guarantees there is at most one, so a double-submit
    cannot start the chain twice and pay for two sets of API calls.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_COLUMNS} FROM insight_jobs
                WHERE visit_id = %s AND status = ANY(%s)""",
            (visit_id, list(ACTIVE)),
        )
        return _row(cur.fetchone())


def latest_for_visit(conn: psycopg.Connection, visit_id: str) -> Optional[JobRow]:
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_COLUMNS} FROM insight_jobs
                WHERE visit_id = %s ORDER BY created_at DESC LIMIT 1""",
            (visit_id,),
        )
        return _row(cur.fetchone())


def create(
    conn: psycopg.Connection,
    visit_id: str,
    stop_after: int = 3,
    regenerate: bool = False,
    model_name: Optional[str] = None,
) -> JobRow:
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO insight_jobs (visit_id, stop_after, regenerate, model_name)
                VALUES (%s, %s, %s, %s)
                RETURNING {_COLUMNS}""",
            (visit_id, stop_after, regenerate, model_name),
        )
        return _row(cur.fetchone())  # type: ignore[return-value]


def mark_running(conn: psycopg.Connection, job_id: str, worker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE insight_jobs
               SET status = 'running', started_at = now(), worker_id = %s
               WHERE job_id = %s""",
            (worker_id, job_id),
        )


def record_layer(conn: psycopg.Connection, job_id: str, layers_completed: int) -> None:
    """Progress ticker, so a poller can see how far along a run is."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE insight_jobs SET layers_completed = %s WHERE job_id = %s",
            (layers_completed, job_id),
        )


def mark_succeeded(conn: psycopg.Connection, job_id: str, insight_id: str,
                   total_tokens: Optional[int]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE insight_jobs
               SET status = 'succeeded', finished_at = now(),
                   insight_id = %s, total_tokens = %s,
                   error_code = NULL, error_message = NULL
               WHERE job_id = %s""",
            (insight_id, total_tokens, job_id),
        )


def mark_failed(conn: psycopg.Connection, job_id: str, code: str, message: str,
                quota: bool = False) -> None:
    """Fail a job. Quota gets its own status because it is worth retrying
    unchanged once the daily limit resets, rather than investigating."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE insight_jobs
               SET status = %s, finished_at = now(),
                   error_code = %s, error_message = %s
               WHERE job_id = %s""",
            ("failed_quota" if quota else "failed", code, message[:2000], job_id),
        )


def fail_orphans(conn: psycopg.Connection, worker_id: str) -> int:
    """Fail jobs left mid-flight by a previous process.

    Jobs run in-process, so a crash or restart leaves a row stuck in 'running'
    forever. Called at startup: anything still active that this worker did not
    claim cannot be making progress.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE insight_jobs
               SET status = 'failed', finished_at = now(),
                   error_code = 'interrupted',
                   error_message = 'Worker stopped before the job finished.'
               WHERE status = ANY(%s) AND coalesce(worker_id, '') <> %s""",
            (list(ACTIVE), worker_id),
        )
        return cur.rowcount


def list_recent(conn: psycopg.Connection, limit: int = 50) -> List[JobRow]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM insight_jobs ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [JobRow(**r) for r in cur.fetchall()]
