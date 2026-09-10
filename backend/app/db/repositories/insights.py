"""
Insight persistence.

`visit_insights` has no `doctor_id` column and does not need one: it reaches its
tenant through `visit_id -> visits.patient_id -> patients.doctor_id`. The scoping
is expressed as an EXISTS predicate rather than a join so the SELECT column list
and the upsert stay exactly as they were.

This is the table holding the actual clinical content — the extraction, the
remedy reasoning, the care plan. An unscoped read here is not a listing bug, it
is one doctor reading another's consultation notes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from app.db.models import InsightRow
from app.db.tenancy import DoctorScope, is_scoped

_COLUMNS = """
    insight_id::text, visit_id::text, clinical_summary, chief_issue,
    primary_diagnosis, patient_age, patient_gender,
    follow_up_type, follow_up_date, follow_up_date_latest,
    follow_up_stated, follow_up_needs_confirmation,
    next_visit_focus, insights_json,
    model_name, model_version, prompt_version, extraction_version,
    input_tokens, output_tokens, total_tokens,
    generated_at, created_at
"""

# The tenancy predicate, correlated against whichever visit_id the outer query
# is looking at. Written as a subquery on visits+patients so it can be dropped
# into any statement without touching its shape.
_OWNED_BY_SCOPE = """
    EXISTS (SELECT 1 FROM visits v
            JOIN patients p ON p.patient_id = v.patient_id
            WHERE v.visit_id = visit_insights.visit_id
              AND p.doctor_id = %s)
"""

# created_at is deliberately absent from the DO UPDATE list: it records when a
# visit's insights were first generated, while generated_at tracks the latest run.
_UPSERT = f"""
    INSERT INTO visit_insights (
        visit_id, clinical_summary, chief_issue, primary_diagnosis,
        patient_age, patient_gender,
        follow_up_type, follow_up_date, follow_up_date_latest,
        follow_up_stated, follow_up_needs_confirmation,
        next_visit_focus, insights_json,
        model_name, model_version, prompt_version, extraction_version,
        input_tokens, output_tokens, total_tokens
    ) VALUES (
        %(visit_id)s, %(clinical_summary)s, %(chief_issue)s, %(primary_diagnosis)s,
        %(patient_age)s, %(patient_gender)s,
        %(follow_up_type)s, %(follow_up_date)s, %(follow_up_date_latest)s,
        %(follow_up_stated)s, %(follow_up_needs_confirmation)s,
        %(next_visit_focus)s, %(insights_json)s,
        %(model_name)s, %(model_version)s, %(prompt_version)s, %(extraction_version)s,
        %(input_tokens)s, %(output_tokens)s, %(total_tokens)s
    )
    ON CONFLICT (visit_id) DO UPDATE SET
        clinical_summary             = EXCLUDED.clinical_summary,
        chief_issue                  = EXCLUDED.chief_issue,
        primary_diagnosis            = EXCLUDED.primary_diagnosis,
        patient_age                  = EXCLUDED.patient_age,
        patient_gender               = EXCLUDED.patient_gender,
        follow_up_type               = EXCLUDED.follow_up_type,
        follow_up_date               = EXCLUDED.follow_up_date,
        follow_up_date_latest        = EXCLUDED.follow_up_date_latest,
        follow_up_stated             = EXCLUDED.follow_up_stated,
        follow_up_needs_confirmation = EXCLUDED.follow_up_needs_confirmation,
        next_visit_focus             = EXCLUDED.next_visit_focus,
        insights_json                = EXCLUDED.insights_json,
        model_name                   = EXCLUDED.model_name,
        model_version                = EXCLUDED.model_version,
        prompt_version               = EXCLUDED.prompt_version,
        extraction_version           = EXCLUDED.extraction_version,
        input_tokens                 = EXCLUDED.input_tokens,
        output_tokens                = EXCLUDED.output_tokens,
        total_tokens                 = EXCLUDED.total_tokens,
        generated_at                 = now()
    RETURNING insight_id::text AS insight_id, (xmax <> 0) AS was_update
"""


@dataclass
class SaveResult:
    """Outcome of writing one visit's insights."""

    insight_id: str
    was_update: bool

    @property
    def verb(self) -> str:
        return "updated" if self.was_update else "inserted"


def get_for_visit(conn: psycopg.Connection, scope: DoctorScope,
                  visit_id: str) -> Optional[InsightRow]:
    """One visit's insights, or None when unknown or another doctor's."""
    conditions = ["visit_id = %s"]
    params: List[Any] = [visit_id]
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params.append(str(scope))

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM visit_insights "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
        record = cur.fetchone()
    return InsightRow(**record) if record else None


def list_for_visits(conn: psycopg.Connection, scope: DoctorScope,
                    visit_ids: List[str]) -> List[InsightRow]:
    """Insights for several visits at once, so a timeline is one query not N.

    Scoped even though the caller normally derives `visit_ids` from an
    already-scoped visit list: the filter has to hold on its own, because "the
    caller already checked" is exactly the assumption that stops being true after
    a refactor.
    """
    if not visit_ids:
        return []
    conditions = ["visit_id = ANY(%s)"]
    params: List[Any] = [list(visit_ids)]
    if is_scoped(scope):
        conditions.append(_OWNED_BY_SCOPE)
        params.append(str(scope))

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM visit_insights "
            f"WHERE {' AND '.join(conditions)}",
            params,
        )
        return [InsightRow(**r) for r in cur.fetchall()]


def save(
    conn: psycopg.Connection,
    scope: DoctorScope,
    row: Dict[str, Any],
    on_saved: Optional[Callable[[psycopg.Connection], None]] = None,
) -> SaveResult:
    """Upsert one visit's insights and mark the visit as generated.

    One row per visit: a visit that already has insights gets its row updated in
    place. visits.insights_generated is set in the same transaction, so the flag
    can never claim a visit is done while its insight row is missing.

    `on_saved` runs inside that same transaction, which is how the follow-up
    write joins it. The invariant is that insights, the generated flag, and the
    follow-up row all land together or not at all.

    Callers must only reach here after every layer has succeeded; a failure
    part-way through the chain raises earlier, leaving the flag FALSE.

    Ownership is re-checked at the top of the transaction rather than trusted
    from the caller. This write is reached from three directions — the HTTP
    route, the background job worker, and the CLI — and generation is expensive
    enough that a visit id arriving from the wrong place would otherwise burn
    three model calls before writing a brief into another doctor's record.
    """
    payload = dict(row)
    payload["insights_json"] = Jsonb(payload["insights_json"])
    visit_id = payload["visit_id"]

    with conn.transaction():
        if is_scoped(scope):
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT 1 FROM visits v
                       JOIN patients p ON p.patient_id = v.patient_id
                       WHERE v.visit_id = %s AND p.doctor_id = %s""",
                    (visit_id, str(scope)),
                )
                if cur.fetchone() is None:
                    from app.core.errors import NotFoundError

                    raise NotFoundError(f"No visit with id {visit_id}.")

        with conn.cursor() as cur:
            cur.execute(_UPSERT, payload)
            written = cur.fetchone()
            cur.execute(
                "UPDATE visits SET insights_generated = TRUE WHERE visit_id = %s",
                (visit_id,),
            )
        if on_saved is not None:
            on_saved(conn)

    return SaveResult(insight_id=written["insight_id"],
                      was_update=bool(written["was_update"]))
