"""Visit reads and writes."""

from __future__ import annotations

from datetime import date, time
from typing import Any, Dict, List, Optional, Sequence

import psycopg

from app.db.models import VisitRow

# age_at_visit is computed from date_of_birth as at visit_date, so historical
# visits keep the right age instead of drifting as the patient gets older.
_SELECT = """
    SELECT v.visit_id::text,
           v.patient_id::text,
           v.visit_number,
           v.visit_date,
           v.visit_time,
           v.visit_type,
           v.visit_status,
           v.transcript_path,
           v.transcript_provider,
           v.transcript_filename,
           v.transcript_uploaded_at,
           v.insights_generated,
           v.completed_at,
           v.cancelled_at,
           v.updated_at,
           p.first_name,
           p.last_name,
           p.external_id,
           p.gender,
           date_part('year', age(v.visit_date, p.date_of_birth))::int AS age_at_visit,
           v.created_at
    FROM visits v
    JOIN patients p USING (patient_id)
"""

SORTS = {
    # Chronological is the useful default for a queue; 'patient' preserves the
    # original ordering for anything that relied on it.
    "date_desc": "v.visit_date DESC, v.visit_number DESC",
    "date_asc": "v.visit_date ASC, v.visit_number ASC",
    "patient": "p.first_name, v.visit_number",
}


def _row(record: Optional[Dict[str, Any]]) -> Optional[VisitRow]:
    return VisitRow(**record) if record else None


def get(conn: psycopg.Connection, visit_id: str) -> Optional[VisitRow]:
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE v.visit_id = %s", (visit_id,))
        return _row(cur.fetchone())


def get_by_transcript_path(conn: psycopg.Connection, path: str) -> Optional[VisitRow]:
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE v.transcript_path = %s", (path,))
        return _row(cur.fetchone())


def _filters(
    patient_id: Optional[str],
    statuses: Optional[Sequence[str]],
    date_from: Optional[date],
    date_to: Optional[date],
    has_transcript: Optional[bool],
    pending_only: bool,
) -> tuple:
    clauses: List[str] = []
    params: List[Any] = []

    if patient_id:
        clauses.append("v.patient_id = %s")
        params.append(patient_id)
    if statuses:
        clauses.append("v.visit_status = ANY(%s)")
        params.append(list(statuses))
    if date_from:
        clauses.append("v.visit_date >= %s")
        params.append(date_from)
    if date_to:
        clauses.append("v.visit_date <= %s")
        params.append(date_to)
    if has_transcript is not None:
        clauses.append("v.transcript_path IS NOT NULL"
                       if has_transcript else "v.transcript_path IS NULL")
    if pending_only:
        # insights_generated is the primary signal. The second half is a safety
        # net: if an insight row were deleted while the flag stayed TRUE, the
        # visit would otherwise be skipped forever.
        clauses.append(
            "(NOT v.insights_generated"
            " OR NOT EXISTS (SELECT 1 FROM visit_insights i"
            "                WHERE i.visit_id = v.visit_id))"
        )
    return clauses, params


def list_visits(
    conn: psycopg.Connection,
    patient_id: Optional[str] = None,
    pending_only: bool = False,
    statuses: Optional[Sequence[str]] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    has_transcript: Optional[bool] = None,
    sort: str = "date_desc",
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[VisitRow]:
    """List visits, newest first by default."""
    clauses, params = _filters(patient_id, statuses, date_from, date_to,
                              has_transcript, pending_only)

    sql = _SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {SORTS.get(sort, SORTS['date_desc'])}"
    if limit is not None:
        sql += " LIMIT %s"
        params.append(limit)
    if offset:
        sql += " OFFSET %s"
        params.append(offset)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [VisitRow(**r) for r in cur.fetchall()]


def count_visits(
    conn: psycopg.Connection,
    patient_id: Optional[str] = None,
    pending_only: bool = False,
    statuses: Optional[Sequence[str]] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    has_transcript: Optional[bool] = None,
) -> int:
    """Total matching the same filters, for X-Total-Count."""
    clauses, params = _filters(patient_id, statuses, date_from, date_to,
                              has_transcript, pending_only)
    sql = ("SELECT count(*)::int AS n FROM visits v "
           "JOIN patients p USING (patient_id)")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])


def next_visit_number(conn: psycopg.Connection, patient_id: str) -> int:
    """The next sequential visit number, so callers needn't track it."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(max(visit_number), 0) + 1 AS next FROM visits "
            "WHERE patient_id = %s",
            (patient_id,),
        )
        return int(cur.fetchone()["next"])


def create(
    conn: psycopg.Connection,
    patient_id: str,
    visit_number: int,
    visit_date: date,
    visit_time: Optional[time],
    visit_type: str = "follow_up",
    visit_status: str = "scheduled",
    transcript_path: Optional[str] = None,
    transcript_provider: Optional[str] = None,
    transcript_filename: Optional[str] = None,
) -> VisitRow:
    with conn.cursor() as cur:
        # transcript_path is cast explicitly: without a type Postgres cannot
        # infer the parameter inside the CASE and raises IndeterminateDatatype.
        cur.execute(
            """INSERT INTO visits (patient_id, visit_number, visit_date, visit_time,
                                   visit_type, visit_status, transcript_path,
                                   transcript_provider, transcript_filename,
                                   transcript_uploaded_at, updated_at)
               VALUES (%(patient_id)s, %(visit_number)s, %(visit_date)s,
                       %(visit_time)s, %(visit_type)s, %(visit_status)s,
                       %(transcript_path)s, %(transcript_provider)s,
                       %(transcript_filename)s,
                       CASE WHEN %(transcript_path)s::text IS NULL
                            THEN NULL ELSE now() END,
                       now())
               RETURNING visit_id::text""",
            {"patient_id": patient_id, "visit_number": visit_number,
             "visit_date": visit_date, "visit_time": visit_time,
             "visit_type": visit_type, "visit_status": visit_status,
             "transcript_path": transcript_path,
             "transcript_provider": transcript_provider,
             "transcript_filename": transcript_filename},
        )
        visit_id = cur.fetchone()["visit_id"]
    return get(conn, visit_id)  # type: ignore[return-value]


def update(
    conn: psycopg.Connection,
    visit_id: str,
    changes: Dict[str, Any],
) -> Optional[VisitRow]:
    """Patch a visit. Only whitelisted columns may be changed.

    Deliberately does not touch visit_status: status transitions go through
    mark_completed() and mark_cancelled() so each one records its own timestamp.
    """
    allowed = {"visit_date", "visit_time", "visit_type", "visit_status",
               "transcript_path", "transcript_provider", "transcript_filename"}
    updates = {k: v for k, v in changes.items() if k in allowed}
    if not updates:
        return get(conn, visit_id)

    assignments = ", ".join(f"{col} = %({col})s" for col in updates)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE visits SET {assignments}, updated_at = now() "
            f"WHERE visit_id = %(visit_id)s",
            {**updates, "visit_id": visit_id},
        )
    return get(conn, visit_id)


def set_transcript(
    conn: psycopg.Connection,
    visit_id: str,
    transcript_path: str,
    transcript_filename: Optional[str],
    transcript_provider: str = "local",
) -> Optional[VisitRow]:
    """Attach a transcript, stamping the upload time from the database clock."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE visits
               SET transcript_path        = %s,
                   transcript_filename    = %s,
                   transcript_provider    = %s,
                   transcript_uploaded_at = now(),
                   updated_at             = now()
               WHERE visit_id = %s""",
            (transcript_path, transcript_filename, transcript_provider, visit_id),
        )
    return get(conn, visit_id)


def clear_transcript(conn: psycopg.Connection, visit_id: str) -> Optional[VisitRow]:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE visits
               SET transcript_path        = NULL,
                   transcript_filename    = NULL,
                   transcript_provider    = NULL,
                   transcript_uploaded_at = NULL,
                   updated_at             = now()
               WHERE visit_id = %s""",
            (visit_id,),
        )
    return get(conn, visit_id)


def mark_completed(
    conn: psycopg.Connection,
    visit_id: str,
    visit_date: Optional[date] = None,
    visit_time: Optional[time] = None,
) -> Optional[VisitRow]:
    """Mark a visit completed, optionally correcting the actual date/time.

    completed_at is only set on the first transition, so completing twice is
    idempotent and the UI need not guard a double click.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE visits
               SET visit_status  = 'completed',
                   completed_at  = coalesce(completed_at, now()),
                   cancelled_at  = NULL,
                   visit_date    = coalesce(%s, visit_date),
                   visit_time    = coalesce(%s, visit_time),
                   updated_at    = now()
               WHERE visit_id = %s""",
            (visit_date, visit_time, visit_id),
        )
    return get(conn, visit_id)


def mark_cancelled(conn: psycopg.Connection, visit_id: str) -> Optional[VisitRow]:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE visits
               SET visit_status = 'cancelled',
                   cancelled_at = coalesce(cancelled_at, now()),
                   updated_at   = now()
               WHERE visit_id = %s""",
            (visit_id,),
        )
    return get(conn, visit_id)


def counts(conn: psycopg.Connection) -> Dict[str, int]:
    """Visit counts for the home screen.

    awaiting_transcript and awaiting_insights are the two states that need
    doctor action, so they are computed here rather than in the frontend.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*)::int AS total,
                      count(*) FILTER (WHERE visit_status = 'scheduled')::int
                          AS scheduled,
                      count(*) FILTER (WHERE visit_status = 'in_progress')::int
                          AS in_progress,
                      count(*) FILTER (WHERE visit_status = 'completed')::int
                          AS completed,
                      count(*) FILTER (WHERE visit_status = 'cancelled')::int
                          AS cancelled,
                      count(*) FILTER (WHERE insights_generated)::int
                          AS with_insights,
                      count(*) FILTER (WHERE visit_status = 'completed'
                                         AND transcript_path IS NULL)::int
                          AS awaiting_transcript,
                      count(*) FILTER (WHERE transcript_path IS NOT NULL
                                         AND NOT insights_generated)::int
                          AS awaiting_insights
               FROM visits"""
        )
        row = cur.fetchone()
    result = {k: int(v) for k, v in row.items()}
    result["pending"] = result["total"] - result["with_insights"]
    return result
