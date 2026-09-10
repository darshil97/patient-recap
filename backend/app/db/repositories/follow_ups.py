"""
Follow-up reads and writes.

`follow_ups` denormalises `patient_id` (§6) because the queue is the most-read
query in the application. It does **not** denormalise `doctor_id`, and the
reasoning that justified the first does not carry over to the second: the queue's
SELECT already joins `patients` unconditionally — for first_name, last_name,
phone and date_of_birth — so the join that a denormalised doctor_id would avoid
is one this query performs regardless. It would buy no read that is not already
paid for, and would cost an update anomaly: a stale `follow_ups.doctor_id` shows
one doctor another doctor's patient, silently. Tenancy is derived through
`follow_ups.patient_id -> patients.doctor_id`.

This queue is the highest-value leak surface in the schema. Every row carries a
patient's name and telephone number, because telephoning is the action on that
screen.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import psycopg

from app.db.models import FollowUpRow
from app.db.tenancy import DoctorScope, clause, is_scoped

_SELECT = """
    SELECT f.follow_up_id::text,
           f.source_visit_id::text,
           f.patient_id::text,
           f.due_earliest,
           f.due_latest,
           f.needs_confirmation,
           f.stated,
           f.purpose,
           f.status,
           f.resolved_by_visit_id::text,
           f.resolved_reason,
           f.resolved_at,
           f.dismissed_reason,
           f.dismissed_at,
           f.last_contacted_at,
           f.note,
           f.created_at,
           f.updated_at,
           p.doctor_id::text,
           p.external_id,
           p.first_name,
           p.last_name,
           p.phone,
           p.gender          AS patient_gender,
           date_part('year', age(sv.visit_date, p.date_of_birth))::int AS patient_age,
           sv.visit_number   AS source_visit_number,
           sv.visit_date     AS source_visit_date,
           i.chief_issue,
           i.next_visit_focus
    FROM follow_ups f
    JOIN patients p USING (patient_id)
    JOIN visits sv ON sv.visit_id = f.source_visit_id
    LEFT JOIN visit_insights i ON i.visit_id = f.source_visit_id
"""

SORTS = {"due_asc": "f.due_earliest ASC, f.created_at ASC",
         "due_desc": "f.due_earliest DESC, f.created_at DESC"}

# For the UPDATE and DELETE statements, which have no patients join to filter on.
_OWNED_BY_SCOPE = """
    EXISTS (SELECT 1 FROM patients p
            WHERE p.patient_id = follow_ups.patient_id
              AND p.doctor_id = %(scope_doctor_id)s)
"""

# NOTE: the upsert is written inline in upsert() below rather than kept as a
# constant here. It has to read its patient_id from a scoped SELECT on patients
# instead of from a VALUES list, which means the statement and its tenancy filter
# are one thing and cannot be used apart.


def _row(record: Optional[Dict[str, Any]]) -> Optional[FollowUpRow]:
    return FollowUpRow(**record) if record else None


def get(conn: psycopg.Connection, scope: DoctorScope,
        follow_up_id: str) -> Optional[FollowUpRow]:
    """One follow-up, or None when unknown or another doctor's."""
    clauses, params = clause(scope)
    conditions = ["f.follow_up_id = %s", *clauses]
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(conditions)}",
                    [follow_up_id, *params])
        return _row(cur.fetchone())


def get_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                  visit_id: str) -> Optional[FollowUpRow]:
    clauses, params = clause(scope)
    conditions = ["f.source_visit_id = %s", *clauses]
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(conditions)}",
                    [visit_id, *params])
        return _row(cur.fetchone())


def list_for_visits(conn: psycopg.Connection, scope: DoctorScope,
                    visit_ids: List[str]) -> List[FollowUpRow]:
    """Follow-ups for several visits at once, so a timeline is one query not N."""
    if not visit_ids:
        return []
    clauses, params = clause(scope)
    conditions = ["f.source_visit_id = ANY(%s)", *clauses]
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(conditions)}",
                    [list(visit_ids), *params])
        return [FollowUpRow(**r) for r in cur.fetchall()]


def _filters(
    scope: DoctorScope,
    status: Optional[str],
    due_before: Optional[date],
    due_after: Optional[date],
    needs_confirmation: Optional[bool],
    patient_id: Optional[str],
) -> tuple:
    """Tenancy first, then the caller's filters.

    Shared by list_follow_ups and count_follow_ups so the two cannot be scoped
    differently — a total computed over every doctor's queue beside a list showing
    one doctor's would be both a wrong number and a disclosure.
    """
    clauses, params = clause(scope)
    clauses = list(clauses)
    params = list(params)

    if status and status != "all":
        clauses.append("f.status = %s")
        params.append(status)
    if due_before:
        clauses.append("f.due_earliest <= %s")
        params.append(due_before)
    if due_after:
        clauses.append("f.due_earliest >= %s")
        params.append(due_after)
    if needs_confirmation is not None:
        clauses.append("f.needs_confirmation = %s")
        params.append(needs_confirmation)
    if patient_id:
        clauses.append("f.patient_id = %s")
        params.append(patient_id)
    return clauses, params


def list_follow_ups(
    conn: psycopg.Connection,
    scope: DoctorScope,
    status: Optional[str] = "open",
    due_before: Optional[date] = None,
    due_after: Optional[date] = None,
    needs_confirmation: Optional[bool] = None,
    patient_id: Optional[str] = None,
    sort: str = "due_asc",
    limit: int = 100,
    offset: int = 0,
) -> List[FollowUpRow]:
    clauses, params = _filters(scope, status, due_before, due_after,
                               needs_confirmation, patient_id)
    sql = _SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {SORTS.get(sort, SORTS['due_asc'])} LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [FollowUpRow(**r) for r in cur.fetchall()]


def count_follow_ups(
    conn: psycopg.Connection,
    scope: DoctorScope,
    status: Optional[str] = "open",
    due_before: Optional[date] = None,
    due_after: Optional[date] = None,
    needs_confirmation: Optional[bool] = None,
    patient_id: Optional[str] = None,
) -> int:
    """Total matching the same filters, for X-Total-Count.

    This previously read `FROM follow_ups f` alone. It now joins patients purely
    to reach doctor_id — which is the join a denormalised `follow_ups.doctor_id`
    would have saved. Kept as a join anyway: it is one indexed lookup on a
    primary key, and correctness about who owns a row is worth more than avoiding
    it.
    """
    clauses, params = _filters(scope, status, due_before, due_after,
                               needs_confirmation, patient_id)
    sql = "SELECT count(*)::int AS n FROM follow_ups f JOIN patients p USING (patient_id)"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])


def upsert(
    conn: psycopg.Connection,
    scope: DoctorScope,
    source_visit_id: str,
    patient_id: str,
    due_earliest: date,
    due_latest: Optional[date],
    needs_confirmation: bool,
    stated: Optional[str],
    purpose: Optional[str],
) -> Optional[str]:
    """Create or refresh the follow-up for a visit, preserving human state.

    The patient id is resolved from `patients` under the doctor filter rather
    than inserted as given, so a mismatched pair cannot plant a queue entry —
    complete with the patient's phone number — in another doctor's follow-up list.
    Returns None when nothing matched.
    """
    tenancy, tenancy_params = clause(scope, "p.doctor_id")
    conditions = ["p.patient_id = %(patient_id)s", *tenancy]
    params: Dict[str, Any] = {
        "source_visit_id": source_visit_id,
        "patient_id": patient_id,
        "due_earliest": due_earliest,
        "due_latest": due_latest,
        "needs_confirmation": needs_confirmation,
        "stated": stated,
        "purpose": purpose,
    }
    if tenancy_params:
        params["scope_doctor_id"] = tenancy_params[0]
        conditions[-1] = "p.doctor_id = %(scope_doctor_id)s"

    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO follow_ups (source_visit_id, patient_id, due_earliest,
                                        due_latest, needs_confirmation, stated,
                                        purpose)
                SELECT %(source_visit_id)s, p.patient_id, %(due_earliest)s,
                       %(due_latest)s, %(needs_confirmation)s, %(stated)s,
                       %(purpose)s
                FROM patients p
                WHERE {' AND '.join(conditions)}
                ON CONFLICT (source_visit_id) DO UPDATE SET
                    due_earliest       = EXCLUDED.due_earliest,
                    due_latest         = EXCLUDED.due_latest,
                    needs_confirmation = EXCLUDED.needs_confirmation,
                    stated             = EXCLUDED.stated,
                    purpose            = EXCLUDED.purpose,
                    updated_at         = now()
                RETURNING follow_up_id::text AS follow_up_id""",
            params,
        )
        record = cur.fetchone()
    return record["follow_up_id"] if record else None


def delete_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                     source_visit_id: str) -> int:
    """Remove a visit's follow-up.

    Used when a regeneration no longer finds a scheduled follow-up, so a stale
    row does not linger in the queue.
    """
    conditions = ["source_visit_id = %(source_visit_id)s"]
    params: Dict[str, Any] = {"source_visit_id": source_visit_id}
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(f"DELETE FROM follow_ups WHERE {' AND '.join(conditions)}",
                    params)
        return cur.rowcount


def resolve_open_for_patient(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    new_visit_id: str,
    new_visit_date: date,
) -> List[str]:
    """Close open follow-ups answered by scheduling a new visit.

    Keyed on the *source visit's* date, not the due date: any open follow-up
    from an earlier visit is closed by booking the next one, whether or not the
    new date matches what was suggested. `<=` covers same-day booking.
    """
    conditions = [
        "f.source_visit_id = sv.visit_id",
        "f.patient_id      = %(patient_id)s",
        "f.status          = 'open'",
        "sv.visit_id      <> %(new_visit_id)s",
        "sv.visit_date    <= %(new_visit_date)s",
    ]
    params: Dict[str, Any] = {"patient_id": patient_id,
                              "new_visit_id": new_visit_id,
                              "new_visit_date": new_visit_date}
    if is_scoped(scope):
        conditions.append(
            "EXISTS (SELECT 1 FROM patients p"
            "        WHERE p.patient_id = f.patient_id"
            "          AND p.doctor_id = %(scope_doctor_id)s)")
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE follow_ups f
                SET status               = 'resolved',
                    resolved_by_visit_id = %(new_visit_id)s,
                    resolved_reason      = 'visit_scheduled',
                    resolved_at          = now(),
                    updated_at           = now()
                FROM visits sv
                WHERE {' AND '.join(conditions)}
                RETURNING f.follow_up_id::text AS follow_up_id""",
            params,
        )
        return [r["follow_up_id"] for r in cur.fetchall()]


def reopen_resolved_by_visit(conn: psycopg.Connection, scope: DoctorScope,
                             visit_id: str) -> List[str]:
    """Return follow-ups to open when the visit that resolved them is cancelled.

    Without this, cancelling a visit silently loses a follow-up: the patient
    still needs calling but the row stays closed.
    """
    conditions = ["resolved_by_visit_id = %(visit_id)s", "status = 'resolved'"]
    params: Dict[str, Any] = {"visit_id": visit_id}
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(
            f"""UPDATE follow_ups
                SET status               = 'open',
                    resolved_by_visit_id = NULL,
                    resolved_reason      = NULL,
                    resolved_at          = NULL,
                    updated_at           = now()
                WHERE {' AND '.join(conditions)}
                RETURNING follow_up_id::text AS follow_up_id""",
            params,
        )
        return [r["follow_up_id"] for r in cur.fetchall()]


def apply_update(
    conn: psycopg.Connection,
    scope: DoctorScope,
    follow_up_id: str,
    status: Optional[str] = None,
    dismissed_reason: Optional[str] = None,
    note: Optional[str] = None,
    mark_contacted: bool = False,
) -> Optional[FollowUpRow]:
    """Apply the outcome of a phone call.

    The status transitions clear the fields belonging to the state being left, so
    the table's consistency CHECKs cannot be violated by a partial write.

    Scoped in the UPDATE itself: without it, one doctor could dismiss another
    doctor's follow-up, which does not read as an error anywhere — the patient
    simply drops off a queue nobody is watching.
    """
    sets: List[str] = ["updated_at = now()"]
    params: Dict[str, Any] = {"follow_up_id": follow_up_id}

    if status == "dismissed":
        sets += ["status = 'dismissed'", "dismissed_at = now()",
                 "dismissed_reason = %(dismissed_reason)s",
                 # Leaving 'resolved' means shedding its fields.
                 "resolved_at = NULL", "resolved_reason = NULL",
                 "resolved_by_visit_id = NULL"]
        params["dismissed_reason"] = dismissed_reason
    elif status == "open":
        sets += ["status = 'open'", "dismissed_at = NULL", "dismissed_reason = NULL",
                 "resolved_at = NULL", "resolved_reason = NULL",
                 "resolved_by_visit_id = NULL"]

    if note is not None:
        sets.append("note = %(note)s")
        params["note"] = note
    if mark_contacted:
        sets.append("last_contacted_at = now()")

    conditions = ["follow_up_id = %(follow_up_id)s"]
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params["scope_doctor_id"] = str(scope)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE follow_ups SET {', '.join(sets)} "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
    return get(conn, scope, follow_up_id)


def counts(conn: psycopg.Connection, scope: DoctorScope,
           today: date) -> Dict[str, int]:
    """Follow-up counts for the home screen, for one doctor.

    overdue and due_next_7_days are relative to the caller's date, which is
    passed in so the value and the `as_of` label in the response agree.
    """
    clauses, params_list = clause(scope)
    sql = """SELECT count(*) FILTER (WHERE f.status = 'open')::int AS open,
                    count(*) FILTER (WHERE f.status = 'resolved')::int AS resolved,
                    count(*) FILTER (WHERE f.status = 'dismissed')::int AS dismissed,
                    count(*) FILTER (WHERE f.status = 'open'
                                       AND f.due_earliest < %(today)s)::int AS overdue,
                    count(*) FILTER (WHERE f.status = 'open'
                                       AND f.due_earliest >= %(today)s
                                       AND f.due_earliest < %(today)s + 7)::int
                        AS due_next_7_days,
                    count(*) FILTER (WHERE f.status = 'open'
                                       AND f.needs_confirmation)::int
                        AS needs_confirmation
             FROM follow_ups f
             JOIN patients p USING (patient_id)"""
    params: Dict[str, Any] = {"today": today}
    if clauses:
        sql += " WHERE p.doctor_id = %(scope_doctor_id)s"
        params["scope_doctor_id"] = params_list[0]

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {k: int(v) for k, v in cur.fetchone().items()}
