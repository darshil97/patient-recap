"""
Visit reads and writes.

Visits carry no `doctor_id` of their own: they reach their tenant through
`visits.patient_id -> patients.doctor_id`. The `JOIN patients p` that every read
here already needed for the patient's name and date of birth is the same join
that enforces tenancy, so scoping costs nothing extra.

Writes are the part to be careful with. Each one carries the doctor filter in its
own `WHERE` via a correlated subquery on patients, rather than checking ownership
first and then updating: a check-then-write leaves a window, and more importantly
it leaves two places that must both be right. An UPDATE whose WHERE cannot match
another doctor's row is safe even if a caller forgets to look first.
"""

from __future__ import annotations

from datetime import date, time
from typing import Any, Dict, List, Optional, Sequence

import psycopg

from app.db.models import VisitRow
from app.db.tenancy import DoctorScope, clause, is_scoped

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
           v.cancelled_reason,
           v.updated_at,
           p.doctor_id::text,
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

# Confines a write to visits whose patient belongs to the scoped doctor. Used in
# the UPDATE statements, which have no `patients` join to hang a filter on.
_OWNED_BY_SCOPE = """
    EXISTS (SELECT 1 FROM patients p
            WHERE p.patient_id = visits.patient_id
              AND p.doctor_id = %(scope_doctor_id)s)
"""


def _row(record: Optional[Dict[str, Any]]) -> Optional[VisitRow]:
    return VisitRow(**record) if record else None


def _write_conditions(scope: DoctorScope, visit_id: str) -> tuple:
    """`WHERE` conditions and named params for a scoped single-visit write."""
    conditions = ["visit_id = %(visit_id)s"]
    params: Dict[str, Any] = {"visit_id": visit_id}
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)
    return conditions, params


def get(conn: psycopg.Connection, scope: DoctorScope,
        visit_id: str) -> Optional[VisitRow]:
    """One visit, or None when unknown *or* owned by another doctor.

    Indistinguishable on purpose: the caller raises 404 for both, so a visit id
    from another doctor's practice cannot be confirmed to exist.
    """
    clauses, params = clause(scope)
    clauses = [*clauses, "v.visit_id = %s"]
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(clauses)}",
                    [*params, visit_id])
        return _row(cur.fetchone())


def get_by_transcript_path(conn: psycopg.Connection, scope: DoctorScope,
                           path: str) -> Optional[VisitRow]:
    """Find a visit by the transcript it points at.

    Scoped even though the path is server-generated from a visit id: an unscoped
    lookup here would let `--transcript` reach across doctors, and the CLI has to
    ask for that explicitly with ALL_DOCTORS rather than getting it by default.
    """
    clauses, params = clause(scope)
    clauses = [*clauses, "v.transcript_path = %s"]
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(clauses)}",
                    [*params, path])
        return _row(cur.fetchone())


def _filters(
    scope: DoctorScope,
    patient_id: Optional[str],
    statuses: Optional[Sequence[str]],
    date_from: Optional[date],
    date_to: Optional[date],
    has_transcript: Optional[bool],
    pending_only: bool,
) -> tuple:
    """Tenancy first, then the caller's filters.

    Tenancy is prepended here rather than added by each caller so that
    list_visits and count_visits cannot drift apart — a count that was scoped
    differently from its list would report a total the doctor cannot see.
    """
    clauses, params = clause(scope)
    clauses = list(clauses)
    params = list(params)

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
    scope: DoctorScope,
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
    """List one doctor's visits, newest first by default.

    A `patient_id` belonging to another doctor yields an empty list rather than
    an error, which is the same thing the doctor would see for a patient with no
    visits — so filtering by a guessed id reveals nothing.
    """
    clauses, params = _filters(scope, patient_id, statuses, date_from, date_to,
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
    scope: DoctorScope,
    patient_id: Optional[str] = None,
    pending_only: bool = False,
    statuses: Optional[Sequence[str]] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    has_transcript: Optional[bool] = None,
) -> int:
    """Total matching the same filters, for X-Total-Count."""
    clauses, params = _filters(scope, patient_id, statuses, date_from, date_to,
                               has_transcript, pending_only)
    sql = ("SELECT count(*)::int AS n FROM visits v "
           "JOIN patients p USING (patient_id)")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])


def next_visit_number(conn: psycopg.Connection, scope: DoctorScope,
                      patient_id: str) -> int:
    """The next sequential visit number, so callers needn't track it.

    Scoped so the count cannot be read for a patient the caller does not own —
    otherwise a doctor could learn how many consultations another practice has
    recorded for a given patient id.
    """
    clauses, params = clause(scope)
    conditions = ["v.patient_id = %s", *clauses]
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT coalesce(max(v.visit_number), 0) + 1 AS next
                FROM visits v JOIN patients p USING (patient_id)
                WHERE {' AND '.join(conditions)}""",
            [patient_id, *params],
        )
        return int(cur.fetchone()["next"])


def create(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    visit_number: int,
    visit_date: date,
    visit_time: Optional[time],
    visit_type: str = "follow_up",
    visit_status: str = "scheduled",
    transcript_path: Optional[str] = None,
    transcript_provider: Optional[str] = None,
    transcript_filename: Optional[str] = None,
) -> Optional[VisitRow]:
    """Schedule a visit against a patient the scoped doctor owns.

    The INSERT selects its `patient_id` from `patients` under the doctor filter
    rather than taking the caller's value directly, so a patient id belonging to
    another doctor inserts nothing at all and this returns None. The caller turns
    that into a 404. Validating in the statement means there is no version of
    this function that writes a visit into someone else's records.
    """
    tenancy, tenancy_params = clause(scope, "p.doctor_id")
    owner_conditions = ["p.patient_id = %(patient_id)s", *tenancy]

    params: Dict[str, Any] = {
        "patient_id": patient_id, "visit_number": visit_number,
        "visit_date": visit_date, "visit_time": visit_time,
        "visit_type": visit_type, "visit_status": visit_status,
        "transcript_path": transcript_path,
        "transcript_provider": transcript_provider,
        "transcript_filename": transcript_filename,
    }
    if tenancy_params:
        params["scope_doctor_id"] = tenancy_params[0]
        owner_conditions[-1] = "p.doctor_id = %(scope_doctor_id)s"

    with conn.cursor() as cur:
        # transcript_path is cast explicitly: without a type Postgres cannot
        # infer the parameter inside the CASE and raises IndeterminateDatatype.
        cur.execute(
            f"""INSERT INTO visits (patient_id, visit_number, visit_date, visit_time,
                                    visit_type, visit_status, transcript_path,
                                    transcript_provider, transcript_filename,
                                    transcript_uploaded_at, updated_at)
                SELECT p.patient_id, %(visit_number)s, %(visit_date)s,
                       %(visit_time)s, %(visit_type)s, %(visit_status)s,
                       %(transcript_path)s, %(transcript_provider)s,
                       %(transcript_filename)s,
                       CASE WHEN %(transcript_path)s::text IS NULL
                            THEN NULL ELSE now() END,
                       now()
                FROM patients p
                WHERE {' AND '.join(owner_conditions)}
                RETURNING visit_id::text""",
            params,
        )
        record = cur.fetchone()
    if record is None:
        return None
    return get(conn, scope, record["visit_id"])


def update(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    changes: Dict[str, Any],
) -> Optional[VisitRow]:
    """Patch a visit. Only whitelisted columns may be changed.

    `visit_status` is not in the whitelist, and that is the point: status
    transitions go through mark_completed() and mark_cancelled() so each records
    its own timestamp, and so cancelling goes through the service that reopens
    the follow-ups it had resolved. It used to be here, which made the docstring
    above it false and gave PATCH a way past all of that (§8.17).

    The transcript columns are not here either. They are written by
    set_transcript() and set_transcript_provider(), which are the only two
    callers that know whether the bytes moved or only the row did.

    `patient_id` is not in the whitelist, so a visit cannot be moved to another
    patient — which would otherwise be a way to move it to another doctor.
    """
    allowed = {"visit_date", "visit_time", "visit_type"}
    updates = {k: v for k, v in changes.items() if k in allowed}
    if not updates:
        return get(conn, scope, visit_id)

    conditions, params = _write_conditions(scope, visit_id)
    params.update(updates)
    assignments = ", ".join(f"{col} = %({col})s" for col in updates)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE visits SET {assignments}, updated_at = now() "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
    return get(conn, scope, visit_id)


def set_transcript(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    transcript_path: str,
    transcript_filename: Optional[str],
    transcript_provider: str = "local",
) -> Optional[VisitRow]:
    """Attach a transcript, stamping the upload time from the database clock.

    Use this only when a doctor has supplied new content. `transcript_uploaded_at`
    is read by the UI as "the transcript changed at this moment": it compares it
    against `visit_insights.generated_at` and warns that a brief is out of date.
    So bumping it is a claim that the words changed, and anything that moves bytes
    without changing them must use `set_transcript_provider` instead.
    """
    conditions, params = _write_conditions(scope, visit_id)
    params.update({"transcript_path": transcript_path,
                   "transcript_filename": transcript_filename,
                   "transcript_provider": transcript_provider})
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET transcript_path        = %(transcript_path)s,
                    transcript_filename    = %(transcript_filename)s,
                    transcript_provider    = %(transcript_provider)s,
                    transcript_uploaded_at = now(),
                    updated_at             = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def set_transcript_provider(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    transcript_provider: str,
) -> Optional[VisitRow]:
    """Record that a transcript moved storage backend. Content unchanged.

    Exists because reusing `set_transcript` for a storage migration was a bug: it
    stamps `transcript_uploaded_at = now()`, which the UI reads as "the transcript
    was replaced", so migrating all 18 transcripts to object storage put a
    clinical-safety warning on every brief in the application and advised
    regenerating them — 54 model calls against a 20/day quota, to rebuild briefs
    from byte-identical input.

    Moving bytes is not an upload. This touches `transcript_provider` and
    `updated_at` only, and deliberately leaves `transcript_path`,
    `transcript_filename` and `transcript_uploaded_at` alone.
    """
    conditions, params = _write_conditions(scope, visit_id)
    params["transcript_provider"] = transcript_provider
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET transcript_provider = %(transcript_provider)s,
                    updated_at          = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def clear_transcript(conn: psycopg.Connection, scope: DoctorScope,
                     visit_id: str) -> Optional[VisitRow]:
    conditions, params = _write_conditions(scope, visit_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET transcript_path        = NULL,
                    transcript_filename    = NULL,
                    transcript_provider    = NULL,
                    transcript_uploaded_at = NULL,
                    updated_at             = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def mark_in_progress(conn: psycopg.Connection, scope: DoctorScope,
                     visit_id: str) -> Optional[VisitRow]:
    """Start a scheduled consultation.

    The status condition is in the UPDATE itself rather than checked beforehand,
    so a visit completed or cancelled in another tab between the service's read
    and this write is left alone; the caller sees the unchanged status and
    refuses. Re-starting an in-progress visit matches and changes nothing.
    """
    conditions, params = _write_conditions(scope, visit_id)
    conditions.append("visit_status IN ('scheduled', 'in_progress')")
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET visit_status = 'in_progress',
                    updated_at   = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def mark_completed(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit_id: str,
    visit_date: Optional[date] = None,
    visit_time: Optional[time] = None,
) -> Optional[VisitRow]:
    """Mark a visit completed, optionally correcting the actual date/time.

    completed_at is only set on the first transition, so completing twice is
    idempotent and the UI need not guard a double click.
    """
    conditions, params = _write_conditions(scope, visit_id)
    params.update({"visit_date": visit_date, "visit_time": visit_time})
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET visit_status  = 'completed',
                    completed_at  = coalesce(completed_at, now()),
                    cancelled_at  = NULL,
                    visit_date    = coalesce(%(visit_date)s, visit_date),
                    visit_time    = coalesce(%(visit_time)s, visit_time),
                    updated_at    = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def mark_cancelled(conn: psycopg.Connection, scope: DoctorScope, visit_id: str,
                   reason: Optional[str] = None) -> Optional[VisitRow]:
    """Cancel a visit, recording why if the doctor said.

    `cancelled_at` uses coalesce so re-cancelling keeps the original time, but
    `cancelled_reason` is overwritten: a second cancel with a better reason is a
    correction, and the first one was never shown to anybody.
    """
    conditions, params = _write_conditions(scope, visit_id)
    params["cancelled_reason"] = reason
    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE visits
                SET visit_status     = 'cancelled',
                    cancelled_at     = coalesce(cancelled_at, now()),
                    cancelled_reason = %(cancelled_reason)s,
                    updated_at       = now()
                WHERE {' AND '.join(conditions)}""",
            params,
        )
    return get(conn, scope, visit_id)


def counts(conn: psycopg.Connection, scope: DoctorScope) -> Dict[str, int]:
    """Visit counts for the home screen, for one doctor.

    awaiting_transcript and awaiting_insights are the two states that need
    doctor action, so they are computed here rather than in the frontend.

    Scoped through the patients join: unscoped counters were the quietest leak of
    the lot, since a number on a dashboard does not look like someone else's data
    until you notice it counting visits the doctor cannot open.
    """
    clauses, params = clause(scope)
    sql = """SELECT count(*)::int AS total,
                    count(*) FILTER (WHERE v.visit_status = 'scheduled')::int
                        AS scheduled,
                    count(*) FILTER (WHERE v.visit_status = 'in_progress')::int
                        AS in_progress,
                    count(*) FILTER (WHERE v.visit_status = 'completed')::int
                        AS completed,
                    count(*) FILTER (WHERE v.visit_status = 'cancelled')::int
                        AS cancelled,
                    count(*) FILTER (WHERE v.insights_generated)::int
                        AS with_insights,
                    count(*) FILTER (WHERE v.visit_status = 'completed'
                                       AND v.transcript_path IS NULL)::int
                        AS awaiting_transcript,
                    count(*) FILTER (WHERE v.transcript_path IS NOT NULL
                                       AND NOT v.insights_generated)::int
                        AS awaiting_insights
             FROM visits v
             JOIN patients p USING (patient_id)"""
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    result = {k: int(v) for k, v in row.items()}
    result["pending"] = result["total"] - result["with_insights"]
    return result
