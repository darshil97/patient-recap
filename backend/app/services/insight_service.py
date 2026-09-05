"""
Generating and storing insights for a visit.

The single orchestration point where the database, the LLM and the pipeline
meet. Both the HTTP API and the batch CLI go through here, so they cannot drift
apart in behaviour.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

import psycopg

from app.config import get_settings
from app.core.errors import ConflictError, NotFoundError, QuotaExhaustedError
from app.db.models import InsightRow, VisitRow
from app.db.repositories import insights as insights_repo
from app.db.repositories import visits as visits_repo
from app.db.repositories.insights import SaveResult
from app.domain.follow_up.models import SCHEDULED, ResolvedFollowUp
from app.domain.transcripts import Transcript, load_transcript
from app.llm.errors import AllKeysExhausted, is_quota_failure
from app.llm.gemini import ChatClient, build_llm
from app.pipeline.layers import LAYERS
from app.pipeline.results import PatientContext, PipelineResult
from app.pipeline.runner import ProgressFn, run_pipeline
from app.services import follow_up_service

logger = logging.getLogger(__name__)

# Bump when the shape of what we extract changes, so rows stay traceable to the
# logic that produced them.
EXTRACTION_VERSION = "follow-up-v1"


def prompt_version() -> str:
    """Short hash of the prompt templates, tracing a row back to its prompts."""
    settings = get_settings()
    digest = hashlib.sha256()
    for spec in sorted(LAYERS.values(), key=lambda s: s.number):
        path = settings.prompts_dir / spec.template
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Shaping the stored row
# --------------------------------------------------------------------------- #

def build_clinical_summary(layer_3: Any,
                           follow_up: Optional[ResolvedFollowUp]) -> str:
    """Compose the doctor-facing brief from the Layer 3 care plan."""
    if not isinstance(layer_3, dict):
        return ""

    lines: List[str] = []
    if layer_3.get("chief_issue"):
        lines.append(str(layer_3["chief_issue"]))

    remedy = layer_3.get("prescribed_remedy")
    if isinstance(remedy, dict):
        detail = " | ".join(str(b) for b in (remedy.get("name"), remedy.get("potency"),
                                            remedy.get("frequency")) if b)
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


def build_insight_row(
    visit: VisitRow,
    result: PipelineResult,
    primary_diagnosis: Optional[str],
) -> Dict[str, Any]:
    """Flatten a PipelineResult into visit_insights columns."""
    layer_3 = result.layer_3.data if result.layer_3 else None
    follow_up = result.follow_up
    usage = result.total_usage()

    # visit_insights has a CHECK requiring a date for 'scheduled' and none
    # otherwise, so mirror it here rather than letting a half-populated
    # follow-up reach the database.
    fu_type = follow_up.type if follow_up else "not_discussed"
    fu_earliest = follow_up.due_earliest if follow_up else None
    if fu_type == SCHEDULED and not fu_earliest:
        fu_type = "not_discussed"
    if fu_type != SCHEDULED:
        fu_earliest = None

    return {
        "visit_id": visit.visit_id,
        "clinical_summary": build_clinical_summary(layer_3, follow_up),
        "chief_issue": layer_3.get("chief_issue") if isinstance(layer_3, dict) else None,
        "primary_diagnosis": primary_diagnosis,
        "patient_age": visit.age_at_visit,
        "patient_gender": visit.gender,
        "follow_up_type": fu_type,
        "follow_up_date": fu_earliest,
        "follow_up_date_latest": follow_up.due_latest if follow_up and fu_earliest else None,
        "follow_up_stated": follow_up.stated if follow_up else None,
        "follow_up_needs_confirmation": bool(
            follow_up.needs_human_confirmation if follow_up else False),
        "next_visit_focus": (layer_3.get("focus_for_next_call")
                             if isinstance(layer_3, dict) else None),
        "insights_json": result.payload(),
        "model_name": result.model,
        "model_version": result.model_version(),
        "prompt_version": prompt_version(),
        "extraction_version": EXTRACTION_VERSION,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

def load_visit_transcript(visit: VisitRow) -> Transcript:
    """Load the transcript a visit points at, guarding the path."""
    if not visit.transcript_path:
        raise ConflictError(
            f"Visit {visit.visit_id} has no transcript attached. "
            f"PATCH the visit with a transcript_path first.",
            details={"visit_id": visit.visit_id},
        )
    return load_transcript(visit.transcript_path, get_settings().transcripts_dir)


def patient_context(visit: VisitRow) -> PatientContext:
    """The patient facts the prompts need, taken from the database.

    call_date is the visit's own date, which is what anchors every relative
    follow-up. Nothing here is client-supplied.
    """
    return PatientContext(
        name=visit.first_name or "Unknown",
        age=visit.age_at_visit if visit.age_at_visit is not None else "Not recorded",
        gender=visit.prompt_gender,
        call_date=visit.visit_date.isoformat(),
    )


def generate_for_visit(
    conn: psycopg.Connection,
    visit: VisitRow,
    llm: Optional[ChatClient] = None,
    model: Optional[str] = None,
    stop_after: int = 3,
    regenerate: bool = False,
    strict_json: bool = True,
    progress: Optional[ProgressFn] = None,
) -> "GenerationOutcome":
    """Run the chain for one visit and persist the result.

    Raises ConflictError if insights already exist and regenerate is False, and
    QuotaExhaustedError when every API key is rate limited. Nothing is written
    unless all requested layers succeed, so a mid-chain failure leaves
    visits.insights_generated FALSE and the visit still pending.
    """
    if visit.insights_generated and not regenerate:
        raise ConflictError(
            f"Visit {visit.visit_id} already has insights. "
            f"Pass regenerate=true to rebuild them.",
            details={"visit_id": visit.visit_id},
        )

    transcript = load_visit_transcript(visit)
    client = llm or build_llm(model=model)

    try:
        result = run_pipeline(
            transcript=transcript,
            patient=patient_context(visit),
            llm=client,
            prompts_dir=get_settings().prompts_dir,
            stop_after=stop_after,
            strict_json=strict_json,
            progress=progress,
        )
    except AllKeysExhausted as exc:
        raise QuotaExhaustedError(str(exc), details={"keys": exc.status}) from exc
    except Exception as exc:
        if is_quota_failure(exc):
            raise QuotaExhaustedError(str(exc)) from exc
        raise

    # No diagnosis is recorded. `disease` existed only as a prop for generating
    # the synthetic transcripts, so it must not reach the doctor; with it gone,
    # Layer 3's `chief_issue` is the condition descriptor everywhere. The
    # parameter stays so the CLI could pass a real clinical label later.
    row = build_insight_row(visit, result, primary_diagnosis=None)

    # The follow-up row is written inside the insight save transaction, so a
    # brief can never exist without its queue entry, or vice versa.
    def write_follow_up(active: psycopg.Connection) -> None:
        follow_up_service.sync_from_insights(active, visit, result.follow_up)

    saved = insights_repo.save(conn, row, on_saved=write_follow_up)

    logger.info("%s insights for %s (%s tokens)",
                saved.verb, visit.label, result.total_usage().get("total_tokens"))
    return GenerationOutcome(visit=visit, result=result, saved=saved)


class GenerationOutcome:
    """What a successful generation produced."""

    def __init__(self, visit: VisitRow, result: PipelineResult,
                 saved: SaveResult) -> None:
        self.visit = visit
        self.result = result
        self.saved = saved

    @property
    def total_tokens(self) -> Optional[int]:
        return self.result.total_usage().get("total_tokens")

    @property
    def follow_up(self) -> Optional[ResolvedFollowUp]:
        return self.result.follow_up


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #

def get_insights(conn: psycopg.Connection, visit_id: str) -> InsightRow:
    if visits_repo.get(conn, visit_id) is None:
        raise NotFoundError(f"No visit with id {visit_id}.")
    row = insights_repo.get_for_visit(conn, visit_id)
    if row is None:
        raise NotFoundError(f"No insights generated for visit {visit_id} yet.")
    return row


def check_against_next_visit(
    conn: psycopg.Connection,
    visit: VisitRow,
    due: Optional[str],
) -> str:
    """Compare a resolved follow-up date to when the patient actually returned.

    Seeded visit dates were chained from each transcript's stated interval, so
    for any visit with a successor these should agree. A mismatch means either
    the extraction or the seed data is wrong, which makes this the most useful
    single signal that the pipeline is behaving.
    """
    later = [v for v in visits_repo.list_visits(conn, patient_id=visit.patient_id)
             if v.visit_number == visit.visit_number + 1]
    if not later:
        return "no later visit on record (last in series)"
    actual = later[0].visit_date
    if due is None:
        return f"MISMATCH: no date resolved, but patient returned {actual}"
    if str(due) == str(actual):
        return f"matches visit {visit.visit_number + 1} actual date {actual}"
    return (f"MISMATCH: resolved {due} but visit "
            f"{visit.visit_number + 1} was {actual}")
