"""
Insight job bookkeeping.

`insight_jobs` reaches its tenant through `visit_id -> visits.patient_id ->
patients.doctor_id`, the same derivation `visit_insights` uses.

This table was the least obvious leak of the set. `GET /jobs/{job_id}` is keyed
only by a job id — no patient, no visit anywhere in the path — so nothing about
the route's shape suggests it touches tenant data, and the frontend polls it in a
loop every 2.5 seconds. Unscoped, it reported another doctor's generation
progress, their error messages, and their token spend.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import psycopg

from app.db.models import JobRow
from app.db.tenancy import ALL_DOCTORS, DoctorScope, is_scoped

_COLUMNS = """
    job_id::text, visit_id::text, status, layers_completed, stop_after,
    regenerate, model_name, insight_id::text, error_code, error_message,
    total_tokens, worker_id, created_at, started_at, finished_at
"""

ACTIVE = ("queued", "running")

# Correlated against the outer query's job row.
_OWNED_BY_SCOPE = """
    EXISTS (SELECT 1 FROM visits v
            JOIN patients p ON p.patient_id = v.patient_id
            WHERE v.visit_id = insight_jobs.visit_id
              AND p.doctor_id = %(scope_doctor_id)s)
"""


def _row(record: Optional[Dict[str, Any]]) -> Optional[JobRow]:
    return JobRow(**record) if record else None


def _scoped(scope: DoctorScope, *conditions: str) -> tuple:
    """Conditions plus named params, with the tenancy predicate appended."""
    clauses = list(conditions)
    params: Dict[str, Any] = {}
    if is_scoped(scope):
        clauses.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)
    return clauses, params


def get(conn: psycopg.Connection, scope: DoctorScope,
        job_id: str) -> Optional[JobRow]:
    """One job, or None when unknown or belonging to another doctor's visit."""
    conditions, params = _scoped(scope, "job_id = %(job_id)s")
    params["job_id"] = job_id
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM insight_jobs WHERE {' AND '.join(conditions)}",
            params,
        )
        return _row(cur.fetchone())


def owner_doctor_id(conn: psycopg.Connection, job_id: str) -> Optional[str]:
    """Which doctor a job belongs to, resolved through its visit.

    Deliberately unscoped, because it is what *establishes* the scope. The
    background worker picks a job up by id with no request context behind it, so
    it resolves the owning doctor here and then performs every subsequent read and
    write scoped to that doctor. That keeps the invariant "every query names a
    tenant" true even on the async path, instead of carving out an exception for
    it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT p.doctor_id::text AS doctor_id
               FROM insight_jobs j
               JOIN visits v ON v.visit_id = j.visit_id
               JOIN patients p ON p.patient_id = v.patient_id
               WHERE j.job_id = %s""",
            (job_id,),
        )
        record = cur.fetchone()
    return record["doctor_id"] if record else None


def active_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                     visit_id: str) -> Optional[JobRow]:
    """The queued or running job for a visit, if any.

    A partial unique index guarantees there is at most one, so a double-submit
    cannot start the chain twice and pay for two sets of API calls.
    """
    conditions, params = _scoped(
        scope, "visit_id = %(visit_id)s", "status = ANY(%(active)s)")
    params.update({"visit_id": visit_id, "active": list(ACTIVE)})
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM insight_jobs WHERE {' AND '.join(conditions)}",
            params,
        )
        return _row(cur.fetchone())


def latest_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                     visit_id: str) -> Optional[JobRow]:
    conditions, params = _scoped(scope, "visit_id = %(visit_id)s")
    params["visit_id"] = visit_id
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT {_COLUMNS} FROM insight_jobs
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC LIMIT 1""",
            params,
        )
        return _row(cur.fetchone())


def create(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    stop_after: int = 3,
    regenerate: bool = False,
    model_name: Optional[str] = None,
) -> Optional[JobRow]:
    """Queue a job for a visit the scoped doctor owns.

    The visit id is selected from `visits` under the doctor filter rather than
    inserted directly, so queuing against another doctor's visit inserts nothing
    and returns None. Worth guarding at the write and not only at the route: a job
    is what eventually spends three model calls and writes a clinical brief.
    """
    conditions = ["v.visit_id = %(visit_id)s"]
    params: Dict[str, Any] = {"visit_id": visit_id, "stop_after": stop_after,
                              "regenerate": regenerate, "model_name": model_name}
    if is_scoped(scope):
        conditions.append("p.doctor_id = %(scope_doctor_id)s")
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO insight_jobs (visit_id, stop_after, regenerate,
                                          model_name)
                SELECT v.visit_id, %(stop_after)s, %(regenerate)s, %(model_name)s
                FROM visits v
                JOIN patients p ON p.patient_id = v.patient_id
                WHERE {' AND '.join(conditions)}
                RETURNING {_COLUMNS}""",
            params,
        )
        return _row(cur.fetchone())


def mark_running(conn: psycopg.Connection, job_id: str, worker_id: str) -> None:
    """Progress bookkeeping, keyed on a job id the worker already owns.

    The four mark_*/record_* functions below take no scope. They are only ever
    reached from run_job(), which has already resolved the job's owning doctor via
    owner_doctor_id() and re-read the job under that scope; adding a redundant
    tenancy predicate to a status write would suggest the job id might be
    untrusted here, which it is not. Every path that *accepts* a job id from a
    client goes through get() instead.
    """
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

    Deliberately cross-doctor, and one of only two places in the codebase that is.
    Startup has no request and therefore no doctor; reaping only the first
    doctor's abandoned jobs would leave every other doctor's stuck in 'running'
    with a UI polling them forever.
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


def list_recent(conn: psycopg.Connection, scope: DoctorScope,
                limit: int = 50) -> List[JobRow]:
    """Recent jobs for one doctor.

    Unscoped, this listed every doctor's generation history — visit ids, error
    messages and token counts — from a route with no id in it at all.
    """
    conditions, params = _scoped(scope)
    params["limit"] = limit
    sql = f"SELECT {_COLUMNS} FROM insight_jobs"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY created_at DESC LIMIT %(limit)s"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [JobRow(**r) for r in cur.fetchall()]


__all__ = [
    "ACTIVE", "ALL_DOCTORS", "active_for_visit", "create", "fail_orphans", "get",
    "latest_for_visit", "list_recent", "mark_failed", "mark_running",
    "mark_succeeded", "owner_doctor_id", "record_layer",
]
