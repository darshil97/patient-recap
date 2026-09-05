"""
Database layer: maps pipeline output into the visit_insights table.

Reads the connection string from CONNSTR (see .env). Visits are matched to
transcripts on visits.transcript_path, so the DB is the source of truth for
visit dates, patient age, and gender rather than anything hand-supplied.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pipeline import LAYERS, PROMPTS_DIR, PipelineResult, ResolvedFollowUp

BASE_DIR = Path(__file__).resolve().parent

# Bump when the shape of what we extract changes, so rows stay traceable to the
# logic that produced them.
EXTRACTION_VERSION = "follow-up-v1"


class DatabaseError(Exception):
    """Raised for connection problems and unresolvable visits."""


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

def prompt_version(prompts_dir: Path = PROMPTS_DIR) -> str:
    """Short hash of the prompt templates, so a row can be traced to its prompts."""
    digest = hashlib.sha256()
    for spec in sorted(LAYERS.values(), key=lambda s: s.number):
        path = prompts_dir / spec.template
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #

def connect(conninfo: Optional[str] = None) -> psycopg.Connection:
    """Open a connection. Falls back to CONNSTR from the environment.

    autocommit=True is deliberate and load-bearing. Without it, any query -
    including a plain SELECT - implicitly opens a transaction that stays open,
    and a later `with conn.transaction()` block degrades into a mere SAVEPOINT
    inside it. Every visit's write would then accumulate in one long transaction
    that only commits when the connection closes, so killing a long run would
    discard all of it. With autocommit on, `conn.transaction()` issues a real
    BEGIN/COMMIT, which is what makes each visit independently durable.
    """
    conninfo = conninfo or os.getenv("CONNSTR") or os.getenv("DATABASE_URL")
    if not conninfo:
        raise DatabaseError(
            "No connection string. Set CONNSTR (or DATABASE_URL) in your "
            "environment or .env file."
        )
    return psycopg.connect(conninfo, row_factory=dict_row, autocommit=True)


# --------------------------------------------------------------------------- #
# Visits
# --------------------------------------------------------------------------- #

@dataclass
class VisitRow:
    """A visit joined to its patient, with age evaluated as at the visit date."""

    visit_id: str
    patient_id: str
    first_name: str
    last_name: Optional[str]
    visit_number: int
    visit_date: date
    gender: Optional[str]
    age_at_visit: Optional[int]
    transcript_path: str
    insights_generated: bool = False

    @property
    def label(self) -> str:
        return f"{self.first_name} visit {self.visit_number} ({self.visit_date})"

    @property
    def prompt_gender(self) -> str:
        """'female' -> 'Female' for the prompt; the raw value is what's stored."""
        return (self.gender or "").strip().title() or "Not recorded"


_VISIT_SELECT = """
    SELECT v.visit_id::text,
           v.patient_id::text,
           p.first_name,
           p.last_name,
           v.visit_number,
           v.visit_date,
           p.gender,
           date_part('year', age(v.visit_date, p.date_of_birth))::int
               AS age_at_visit,
           v.transcript_path,
           v.insights_generated
    FROM visits v
    JOIN patients p USING (patient_id)
"""


def _to_visit(row: Dict[str, Any]) -> VisitRow:
    return VisitRow(**row)


def fetch_visits(
    conn: psycopg.Connection,
    transcript_path: Optional[str] = None,
    pending_only: bool = False,
) -> List[VisitRow]:
    """List visits, optionally one transcript or only those lacking insights."""
    clauses, params = [], []
    if transcript_path:
        clauses.append("v.transcript_path = %s")
        params.append(normalise_path(transcript_path))
    if pending_only:
        # The flag is the primary signal. The second half is a safety net: if an
        # insight row were deleted while the flag stayed TRUE, the visit would
        # otherwise be skipped forever. This way it gets regenerated instead.
        clauses.append(
            "(NOT v.insights_generated"
            " OR NOT EXISTS (SELECT 1 FROM visit_insights i"
            "                WHERE i.visit_id = v.visit_id))"
        )

    sql = _VISIT_SELECT
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY p.first_name, v.visit_number"

    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_to_visit(r) for r in cur.fetchall()]


def fetch_visit(conn: psycopg.Connection, transcript_path: str) -> VisitRow:
    """Resolve exactly one visit by transcript path."""
    found = fetch_visits(conn, transcript_path=transcript_path)
    if not found:
        raise DatabaseError(
            f"No visit row has transcript_path = {normalise_path(transcript_path)!r}. "
            "Run the seed data in sql/2_visit_schema.sql first."
        )
    return found[0]


def normalise_path(path: Union[str, Path]) -> str:
    """Match the 'transcripts/<name>.json' form stored in the visits table."""
    p = Path(path)
    try:
        p = p.resolve().relative_to(BASE_DIR)
    except (ValueError, OSError):
        pass
    return p.as_posix()


def transcript_diagnosis(transcript_path: Union[str, Path]) -> Optional[str]:
    """Read the transcript's own `disease` label.

    Read straight off the file rather than through the pipeline: the prompts
    deliberately withhold the diagnosis so Layer 1 can't be biased by it, but
    it is still the clinic's own label and belongs in the DB.
    """
    path = Path(transcript_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as fh:
        return (json.load(fh).get("disease") or "").strip() or None


# --------------------------------------------------------------------------- #
# Shaping the row
# --------------------------------------------------------------------------- #

def build_clinical_summary(layer_3: Any, follow_up: Optional[ResolvedFollowUp]) -> str:
    """Compose the doctor-facing brief from the Layer 3 care plan."""
    if not isinstance(layer_3, dict):
        return ""

    lines: List[str] = []
    if layer_3.get("chief_issue"):
        lines.append(str(layer_3["chief_issue"]))

    remedy = layer_3.get("prescribed_remedy")
    if isinstance(remedy, dict):
        bits = [remedy.get("name"), remedy.get("potency"), remedy.get("frequency")]
        detail = " | ".join(str(b) for b in bits if b)
        if detail:
            lines.append(f"Remedy: {detail}")

    watch = layer_3.get("critical_watch_outs")
    if isinstance(watch, list) and watch:
        lines.append("Watch: " + "; ".join(str(w) for w in watch if w))

    if layer_3.get("focus_for_next_call"):
        lines.append(f"Next call focus: {layer_3['focus_for_next_call']}")

    if follow_up is not None:
        lines.append(f"Follow-up: {follow_up.summary()}")

    return "\n".join(lines)


def _model_version(result: PipelineResult) -> Optional[str]:
    """Pull the concrete model build out of the response metadata, if present."""
    for _, layer in sorted(result.layers.items()):
        meta = layer.metadata or {}
        for key in ("model_version", "modelVersion", "model_name"):
            if meta.get(key):
                return str(meta[key])
    return None


def build_insight_row(
    visit: VisitRow,
    result: PipelineResult,
    primary_diagnosis: Optional[str] = None,
) -> Dict[str, Any]:
    """Flatten a PipelineResult into visit_insights columns."""
    layer_3 = result.layer_3.data if result.layer_3 else None
    follow_up = result.follow_up

    payload = {
        f"layer_{n}": (r.data if r.is_json else r.raw_response)
        for n, r in sorted(result.layers.items())
    }
    payload["follow_up"] = follow_up.to_dict() if follow_up else None

    usage = result.total_usage()

    # The schema's follow_up_type_matches_date constraint requires a date for
    # 'scheduled' and no date otherwise, so mirror that here rather than
    # letting a half-populated follow-up reach the database.
    fu_type = follow_up.type if follow_up else "not_discussed"
    fu_earliest = follow_up.due_earliest if follow_up else None
    if fu_type == "scheduled" and not fu_earliest:
        fu_type = "not_discussed"
    if fu_type != "scheduled":
        fu_earliest = None

    return {
        "visit_id": visit.visit_id,
        "clinical_summary": build_clinical_summary(layer_3, follow_up),
        "chief_issue": (layer_3 or {}).get("chief_issue") if isinstance(layer_3, dict) else None,
        "primary_diagnosis": primary_diagnosis,
        "patient_age": visit.age_at_visit,
        "patient_gender": visit.gender,
        "follow_up_type": fu_type,
        "follow_up_date": fu_earliest,
        "follow_up_date_latest": (follow_up.due_latest if follow_up and fu_earliest else None),
        "follow_up_stated": follow_up.stated if follow_up else None,
        "follow_up_needs_confirmation": bool(
            follow_up.needs_human_confirmation if follow_up else False),
        "next_visit_focus": (
            layer_3.get("focus_for_next_call") if isinstance(layer_3, dict) else None),
        "insights_json": payload,
        "model_name": result.model,
        "model_version": _model_version(result),
        "prompt_version": prompt_version(),
        "extraction_version": EXTRACTION_VERSION,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

# created_at is deliberately absent from the DO UPDATE list: it records when a
# visit's insights were first generated, while generated_at tracks the latest run.
_UPSERT = """
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


def save_insights(
    conn: psycopg.Connection,
    visit: VisitRow,
    result: PipelineResult,
    primary_diagnosis: Optional[str] = None,
) -> SaveResult:
    """Write one visit's insights and mark the visit as generated.

    Upserts on visit_id: a visit that already has insights gets its row updated
    in place, otherwise a new row is inserted. visits.insights_generated is set
    in the same transaction, so the flag can never claim a visit is done while
    its insight row is missing.

    Callers must only reach here after every layer has succeeded. A failure
    part-way through the chain raises before this point, leaving the flag FALSE.
    """
    row = build_insight_row(visit, result, primary_diagnosis)
    row["insights_json"] = Jsonb(row["insights_json"])

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(_UPSERT, row)
            written = cur.fetchone()

            # Same transaction as the insight row: the flag and the data commit
            # together or not at all.
            cur.execute(
                "UPDATE visits SET insights_generated = TRUE WHERE visit_id = %s",
                (visit.visit_id,),
            )

    return SaveResult(insight_id=written["insight_id"],
                      was_update=bool(written["was_update"]))
