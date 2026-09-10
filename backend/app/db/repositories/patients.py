"""
Patient reads and writes.

Every function here takes a `DoctorScope` as its second positional argument.
`patients.doctor_id` is the root of the tenancy boundary — every other table
reaches its owning doctor through this one — so an unfiltered query here is a
patient list leaking between doctors rather than a mildly wrong result. See
app/db/tenancy.py for why the scope is positional and required.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

import psycopg

from app.db.models import PatientRow
from app.db.tenancy import DoctorScope, clause, doctor_id_of

# visit_count / last_visit_date / age are derived here so there is a single
# definition of each, rather than three in the frontend. `age` is as at today,
# unlike VisitRow.age_at_visit which is as at the visit.
#
# The visits subquery needs no doctor filter of its own: it is joined on
# patient_id, and the outer query has already confined `p` to one doctor, so a
# count can only ever aggregate that doctor's own patient's visits.
_SELECT = """
    SELECT p.patient_id::text,
           p.doctor_id::text,
           p.external_id,
           p.first_name,
           p.last_name,
           p.date_of_birth,
           p.gender,
           p.phone,
           p.email,
           p.created_at,
           p.updated_at,
           date_part('year', age(p.date_of_birth))::int AS age,
           coalesce(v.visit_count, 0)::int              AS visit_count,
           v.last_visit_date
    FROM patients p
    LEFT JOIN (
        SELECT patient_id, count(*) AS visit_count, max(visit_date) AS last_visit_date
        FROM visits GROUP BY patient_id
    ) v ON v.patient_id = p.patient_id
"""

SORTS = {"name": "p.first_name, p.last_name",
         "created_desc": "p.created_at DESC"}


def _row(record: Optional[Dict[str, Any]]) -> Optional[PatientRow]:
    return PatientRow(**record) if record else None


def _where(scope: DoctorScope, *extra: str) -> tuple:
    """Assemble a WHERE clause with the tenancy filter always first."""
    clauses, params = clause(scope)
    return list(clauses) + list(extra), params


def get(conn: psycopg.Connection, scope: DoctorScope,
        patient_id: str) -> Optional[PatientRow]:
    """One patient, or None when it does not exist *or* is another doctor's.

    Those two cases are deliberately indistinguishable. The caller turns None
    into a 404, so a patient id belonging to a different doctor reads as "no such
    patient" — a 403 would confirm the id exists and leak that another doctor has
    a record under it.
    """
    clauses, params = _where(scope, "p.patient_id = %s")
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(clauses)}",
                    [*params, patient_id])
        return _row(cur.fetchone())


def get_by_external_id(conn: psycopg.Connection, scope: DoctorScope,
                       external_id: str) -> Optional[PatientRow]:
    """Look up a clinic patient number within one doctor's patients.

    external_id is globally unique across all doctors (§8.10) because it is
    system-generated from one sequence. This lookup is still scoped: it feeds the
    registration conflict check, and returning another doctor's row there would
    put their patient_id in a 409 body. `external_id_taken` covers the global
    constraint without disclosing anything.
    """
    clauses, params = _where(scope, "p.external_id = %s")
    with conn.cursor() as cur:
        cur.execute(f"{_SELECT} WHERE {' AND '.join(clauses)}",
                    [*params, external_id])
        return _row(cur.fetchone())


def external_id_taken(conn: psycopg.Connection, external_id: str) -> bool:
    """Whether any doctor already uses this clinic number.

    Deliberately unscoped, and deliberately returns a bare boolean rather than a
    row: the UNIQUE constraint on external_id is global, so a caller needs to
    know the insert would fail, but must not learn *whose* patient holds it.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM patients WHERE external_id = %s LIMIT 1",
                    (external_id,))
        return cur.fetchone() is not None


def find_probable_duplicate(
    conn: psycopg.Connection,
    scope: DoctorScope,
    first_name: str,
    last_name: Optional[str],
    date_of_birth: date,
) -> Optional[PatientRow]:
    """Look for one of *this doctor's* patients with the same name and DOB.

    Advisory only. Matching is case- and whitespace-insensitive, which catches
    the common double-entry case without pretending demographics are a reliable
    key.

    The doctor filter is the point of this function, not an add-on (§8.10). Two
    doctors having a patient with the same name and date of birth is ordinary,
    not a coincidence — shared surnames and a 365-day birthday space make it
    routine at any scale. Matching globally therefore did two wrong things at
    once: it refused a legitimate registration with a 409 the doctor could do
    nothing about, and it answered with the other doctor's `patient_id` and
    `external_id`, which is a cross-tenant disclosure that a patient by that
    name exists elsewhere. A soft conflict is only meaningful against the
    registering doctor's own list.
    """
    clauses, params = _where(
        scope,
        "lower(btrim(p.first_name)) = lower(btrim(%s))",
        "lower(btrim(coalesce(p.last_name, ''))) = lower(btrim(coalesce(%s, '')))",
        "p.date_of_birth = %s",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"{_SELECT} WHERE {' AND '.join(clauses)} LIMIT 1",
            [*params, first_name, last_name, date_of_birth],
        )
        return _row(cur.fetchone())


def _search_clause(q: Optional[str]) -> tuple:
    """Case-insensitive match across name, clinic number and phone.

    Phone is matched on digits only, so '+91 98765 43210' is found by typing
    '9876543210'.
    """
    if not q or not q.strip():
        return [], []
    term = f"%{q.strip().lower()}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    clause_sql = ("(lower(p.first_name) LIKE %s"
                  " OR lower(coalesce(p.last_name, '')) LIKE %s"
                  " OR lower(coalesce(p.first_name, '') || ' ' "
                  "          || coalesce(p.last_name, '')) LIKE %s"
                  " OR lower(p.external_id) LIKE %s")
    params: List[Any] = [term, term, term, term]
    if digits:
        clause_sql += " OR regexp_replace(coalesce(p.phone, ''), '\\D', '', 'g') LIKE %s"
        params.append(f"%{digits}%")
    clause_sql += ")"
    return [clause_sql], params


def list_all(
    conn: psycopg.Connection,
    scope: DoctorScope,
    q: Optional[str] = None,
    sort: str = "name",
    limit: int = 100,
    offset: int = 0,
) -> List[PatientRow]:
    """One doctor's patients, optionally filtered by a search term.

    Note the search runs *inside* the doctor filter, not alongside it: an
    unscoped search over external_id would let one doctor probe for another
    doctor's clinic numbers by prefix.
    """
    tenancy, params = clause(scope)
    search, search_params = _search_clause(q)
    params = [*params, *search_params]

    sql = _SELECT
    clauses = [*tenancy, *search]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY {SORTS.get(sort, SORTS['name'])} LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [PatientRow(**r) for r in cur.fetchall()]


def count_all(conn: psycopg.Connection, scope: DoctorScope,
              q: Optional[str] = None) -> int:
    """Total matching the same search, for X-Total-Count.

    Scoped for the same reason as the list: a global count would tell every
    doctor how many patients the others have, and a global count with `q` set
    would answer "does a patient named X exist anywhere" without returning a row.
    """
    tenancy, params = clause(scope)
    search, search_params = _search_clause(q)
    params = [*params, *search_params]

    sql = "SELECT count(*)::int AS n FROM patients p"
    clauses = [*tenancy, *search]
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])


def create(
    conn: psycopg.Connection,
    scope: DoctorScope,
    first_name: str,
    last_name: Optional[str],
    date_of_birth: date,
    gender: Optional[str],
    phone: Optional[str] = None,
    email: Optional[str] = None,
    external_id: Optional[str] = None,
) -> PatientRow:
    """Insert a patient owned by the scoped doctor.

    doctor_id comes from the scope, never from the payload: a client that could
    name the owner could plant a patient in another doctor's list.

    external_id is left to the column default (next_patient_external_id()) when
    not supplied, so the clinic number is generated by the database rather than
    raced between application processes.
    """
    owner = doctor_id_of(scope)

    columns = ["doctor_id", "first_name", "last_name", "date_of_birth",
               "gender", "phone", "email"]
    values: List[Any] = [owner, first_name, last_name, date_of_birth,
                         gender, phone, email]
    if external_id:
        columns.append("external_id")
        values.append(external_id)

    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO patients ({', '.join(columns)}) "
            f"VALUES ({placeholders}) RETURNING patient_id::text",
            values,
        )
        patient_id = cur.fetchone()["patient_id"]
    return get(conn, scope, patient_id)  # type: ignore[return-value]


def update(
    conn: psycopg.Connection,
    scope: DoctorScope,
    patient_id: str,
    changes: Dict[str, Any],
) -> Optional[PatientRow]:
    """Patch demographics for one of the scoped doctor's patients.

    `external_id` is not updatable: it is the identity key. `doctor_id` is not
    updatable either — reassigning a patient to another doctor is a transfer of
    clinical custody, not a demographics correction, and would silently move
    every visit, insight and follow-up with it.

    The doctor filter is in the UPDATE's own WHERE, not checked beforehand, so
    there is no window between the check and the write.
    """
    allowed = {"first_name", "last_name", "date_of_birth", "gender", "phone", "email"}
    updates = {k: v for k, v in changes.items() if k in allowed}
    if not updates:
        return get(conn, scope, patient_id)

    tenancy, tenancy_params = clause(scope, "doctor_id")
    conditions = ["patient_id = %(patient_id)s"]
    params: Dict[str, Any] = {**updates, "patient_id": patient_id}
    if tenancy:
        conditions.append("doctor_id = %(scope_doctor_id)s")
        params["scope_doctor_id"] = tenancy_params[0]

    assignments = ", ".join(f"{col} = %({col})s" for col in updates)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE patients SET {assignments}, updated_at = now() "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
    return get(conn, scope, patient_id)


def count(conn: psycopg.Connection, scope: DoctorScope) -> int:
    """Patient count for the home screen, for this doctor only."""
    clauses, params = clause(scope, "doctor_id")
    sql = "SELECT count(*)::int AS n FROM patients"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()["n"])
