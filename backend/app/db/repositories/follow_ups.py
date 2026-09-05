"""Follow-up reads and writes."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import psycopg

from app.db.models import FollowUpRow

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

# Regeneration must not undo a human decision, so the doctor's own fields are
# excluded from the upsert's DO UPDATE list.
_UPSERT = """
    INSERT INTO follow_ups (source_visit_id, patient_id, due_earliest, due_latest,
                            needs_confirmation, stated, purpose)
    VALUES (%(source_visit_id)s, %(patient_id)s, %(due_earliest)s, %(due_latest)s,
            %(needs_confirmation)s, %(stated)s, %(purpose)s)
    ON CONFLICT (source_visit_id) DO UPDATE SET
        due_earliest       = EXCLUDED.due_earliest,
        due_latest         = EXCLUDED.due_latest,
        needs_confirmation = EXCLUDED.needs_confirmation,
        stated             = EXCLUDED.stated,
        purpose            = EXCLUDED.purpose,
        updated_at         = now()
    RETURNING follow_up_id::text AS follow_up_id, (xmax <> 0) AS was_update
"""


def _row(record: Optional[Dict[str, Any]]) -> Optional[FollowUpRow]:
    return FollowUpRow(**record) if record else None


def get(conn: psycopg.Connection, follow_up_id: str) -> Optional[FollowUpRow]:
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE f.follow_up_id = %s", (follow_up_id,))
        return _row(cur.fetchone())


def get_for_visit(conn: psycopg.Connection, visit_id: str) -> Optional[FollowUpRow]:
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE f.source_visit_id = %s", (visit_id,))
        return _row(cur.fetchone())


def list_for_visits(conn: psycopg.Connection,
                    visit_ids: List[str]) -> List[FollowUpRow]:
    """Follow-ups for several visits at once, so a timeline is one query not N."""
    if not visit_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE f.source_visit_id = ANY(%s)",
                    (list(visit_ids),))
        return [FollowUpRow(**r) for r in cur.fetchall()]


def _filters(
    status: Optional[str],
    due_before: Optional[date],
    due_after: Optional[date],
    needs_confirmation: Optional[bool],
    patient_id: Optional[str],
) -> tuple:
    clauses: List[str] = []
    params: List[Any] = []

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
    status: Optional[str] = "open",
    due_before: Optional[date] = None,
    due_after: Optional[date] = None,
    needs_confirmation: Optional[bool] = None,
    patient_id: Optional[str] = None,
    sort: str = "due_asc",
    limit: int = 100,
    offset: int = 0,
) -> List[FollowUpRow]:
    clauses, params = _filters(status, due_before, due_after,
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
    status: Optional[str] = "open",
    due_before: Optional[date] = None,
    due_after: Optional[date] = None,
    needs_confirmation: Optional[bool] = None,
    patient_id: Optional[str] = None,
) -> int:
    clauses, params = _filters(status, due_before, due_after,
                              needs_confirmation, patient_id)
    sql = "SELECT count(*)::int AS n FROM follow_ups f"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])


def upsert(
    conn: psycopg.Connection,
    source_visit_id: str,
    patient_id: str,
    due_earliest: date,
    due_latest: Optional[date],
    needs_confirmation: bool,
    stated: Optional[str],
    purpose: Optional[str],
) -> str:
    """Create or refresh the follow-up for a visit, preserving human state."""
    with conn.cursor() as cur:
        cur.execute(_UPSERT, {
            "source_visit_id": source_visit_id,
            "patient_id": patient_id,
            "due_earliest": due_earliest,
            "due_latest": due_latest,
            "needs_confirmation": needs_confirmation,
            "stated": stated,
            "purpose": purpose,
        })
        return cur.fetchone()["follow_up_id"]


def delete_for_visit(conn: psycopg.Connection, source_visit_id: str) -> int:
    """Remove a visit's follow-up.

    Used when a regeneration no longer finds a scheduled follow-up, so a stale
    row does not linger in the queue.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM follow_ups WHERE source_visit_id = %s",
                    (source_visit_id,))
        return cur.rowcount


def resolve_open_for_patient(
    conn: psycopg.Connection,
    patient_id: str,
    new_visit_id: str,
    new_visit_date: date,
) -> List[str]:
    """Close open follow-ups answered by scheduling a new visit.

    Keyed on the *source visit's* date, not the due date: any open follow-up
    from an earlier visit is closed by booking the next one, whether or not the
    new date matches what was suggested. `<=` covers same-day booking.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE follow_ups f
               SET status               = 'resolved',
                   resolved_by_visit_id = %(new_visit_id)s,
                   resolved_reason      = 'visit_scheduled',
                   resolved_at          = now(),
                   updated_at           = now()
               FROM visits sv
               WHERE f.source_visit_id = sv.visit_id
                 AND f.patient_id      = %(patient_id)s
                 AND f.status          = 'open'
                 AND sv.visit_id      <> %(new_visit_id)s
                 AND sv.visit_date    <= %(new_visit_date)s
               RETURNING f.follow_up_id::text AS follow_up_id""",
            {"patient_id": patient_id, "new_visit_id": new_visit_id,
             "new_visit_date": new_visit_date},
        )
        return [r["follow_up_id"] for r in cur.fetchall()]


def reopen_resolved_by_visit(conn: psycopg.Connection, visit_id: str) -> List[str]:
    """Return follow-ups to open when the visit that resolved them is cancelled.

    Without this, cancelling a visit silently loses a follow-up: the patient
    still needs calling but the row stays closed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE follow_ups
               SET status               = 'open',
                   resolved_by_visit_id = NULL,
                   resolved_reason      = NULL,
                   resolved_at          = NULL,
                   updated_at           = now()
               WHERE resolved_by_visit_id = %s AND status = 'resolved'
               RETURNING follow_up_id::text AS follow_up_id""",
            (visit_id,),
        )
        return [r["follow_up_id"] for r in cur.fetchall()]


def apply_update(
    conn: psycopg.Connection,
    follow_up_id: str,
    status: Optional[str] = None,
    dismissed_reason: Optional[str] = None,
    note: Optional[str] = None,
    mark_contacted: bool = False,
) -> Optional[FollowUpRow]:
    """Apply the outcome of a phone call.

    The status transitions clear the fields belonging to the state being left, so
    the table's consistency CHECKs cannot be violated by a partial write.
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

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE follow_ups SET {', '.join(sets)} "
            f"WHERE follow_up_id = %(follow_up_id)s",
            params,
        )
    return get(conn, follow_up_id)


def counts(conn: psycopg.Connection, today: date) -> Dict[str, int]:
    """Follow-up counts for the home screen.

    overdue and due_next_7_days are relative to the caller's date, which is
    passed in so the value and the `as_of` label in the response agree.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FILTER (WHERE status = 'open')::int AS open,
                      count(*) FILTER (WHERE status = 'resolved')::int AS resolved,
                      count(*) FILTER (WHERE status = 'dismissed')::int AS dismissed,
                      count(*) FILTER (WHERE status = 'open'
                                         AND due_earliest < %(today)s)::int AS overdue,
                      count(*) FILTER (WHERE status = 'open'
                                         AND due_earliest >= %(today)s
                                         AND due_earliest < %(today)s + 7)::int
                          AS due_next_7_days,
                      count(*) FILTER (WHERE status = 'open'
                                         AND needs_confirmation)::int
                          AS needs_confirmation
               FROM follow_ups""",
            {"today": today},
        )
        return {k: int(v) for k, v in cur.fetchone().items()}
