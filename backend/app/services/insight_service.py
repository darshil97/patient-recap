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
from app.core.errors import (
    ConflictError,
    LayerOutputError,
    NotFoundError,
    QuotaExhaustedError,
)
from app.db.models import InsightRow, VisitRow
from app.db.repositories import insights as insights_repo
from app.db.repositories import visits as visits_repo
from app.db.repositories.insights import SaveResult
from app.db.tenancy import DoctorScope
from app.domain.follow_up.models import SCHEDULED, ResolvedFollowUp
from app.domain.transcripts import Transcript
from app.storage import store_for_visit
from app.llm.base import ChatClient
from app.llm.errors import is_quota_failure
from app.llm.factory import build_llm
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
        path = settings.llm.prompts_dir / spec.template
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

def require_complete_brief(result: PipelineResult) -> None:
    """Refuse to persist a run that did not produce a usable care plan.

    Layer 3 is the doctor-facing part: `chief_issue`, the remedy, the watch-outs,
    the questions for next time. The frontend renders it directly out of
    `insights_json`, so a payload without it is not a thin brief, it is a brief
    that cannot be displayed at all.

    That mattered because `insights_repo.save` sets `visits.insights_generated`
    TRUE in the same transaction (§8.7). A partial run therefore produced a visit
    that *claims* to have a brief, and opening it threw on `layer_3.chief_issue`.
    The HTTP API no longer offers `stop_after` at all, but the CLI still does —
    it is the right tool for iterating on an early prompt — so the guarantee has
    to live here, at the write, rather than in the absence of a request field.

    Raises LayerOutputError, which the CLI already reports per visit and the API
    maps like any other pipeline failure. Nothing is written, so the visit stays
    pending and a later full run picks it up.
    """
    layer_3 = result.layer_3
    if layer_3 is None:
        raise LayerOutputError(
            "The run stopped before Layer 3, so there is no care plan to store. "
            "Insights are only saved once all three layers have completed.")
    if not isinstance(layer_3.data, dict):
        raise LayerOutputError(
            "Layer 3 did not return a JSON object, so the care plan cannot be "
            "stored.",
            raw_response=layer_3.raw_response)


def load_visit_transcript(visit: VisitRow) -> Transcript:
    """Load the transcript a visit points at, from whichever backend holds it.

    Routed on `visit.transcript_provider`, not on the configured default, so a
    visit whose transcript predates a change of backend still generates. That is
    the property that lets the migration run in batches over live data: at any
    moment some visits are on disk and some are in the bucket, and both are
    readable.

    Called twice per generation on purpose — once by job_service.enqueue as a
    cheap pre-flight so a missing transcript is a 409 at request time rather than
    a job that fails a second later, and once here at generation time. Two reads
    of a 3-5 KB object is a fair price for that error message.
    """
    if not visit.transcript_path:
        raise ConflictError(
            f"Visit {visit.visit_id} has no transcript attached. "
            f"Upload one before generating insights.",
            details={"visit_id": visit.visit_id},
        )

    stored = store_for_visit(visit).read(visit.transcript_path)
    text = stored.text.strip()
    if not text:
        # The upload path rejects empty transcripts, so this means the stored copy
        # was truncated or emptied after the fact. Refuse rather than send an empty
        # prompt to the model, which would produce a confident, contentless brief.
        raise ConflictError(
            f"The stored transcript for visit {visit.visit_id} is empty.",
            details={"visit_id": visit.visit_id,
                     "transcript_path": visit.transcript_path,
                     "provider": stored.provider},
        )
    return Transcript.from_text(relative_path=stored.locator, text=text)


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
    scope: DoctorScope,
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
    QuotaExhaustedError when the model provider is rate limiting. Nothing is written
    unless all requested layers succeed, so a mid-chain failure leaves
    visits.insights_generated FALSE and the visit still pending.

    `visit` is expected to have been loaded under `scope` already; the scope is
    passed separately because the write path re-checks ownership inside its own
    transaction rather than trusting the row it was handed (see insights_repo.save).
    That redundancy is deliberate here and nowhere else: this function is entered
    from three directions — the HTTP route, the background job worker and the CLI —
    and it is the one that spends three model calls before writing a clinical
    brief.
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
            prompts_dir=get_settings().llm.prompts_dir,
            stop_after=stop_after,
            strict_json=strict_json,
            progress=progress,
        )
    except Exception as exc:
        # The provider's client has already retried; a throttle that outlasted
        # those retries is worth trying again later, unchanged.
        if is_quota_failure(exc):
            raise QuotaExhaustedError(str(exc)) from exc
        raise

    require_complete_brief(result)

    # No diagnosis is recorded. `disease` existed only as a prop for generating
    # the synthetic transcripts, so it must not reach the doctor; with it gone,
    # Layer 3's `chief_issue` is the condition descriptor everywhere. The
    # parameter stays so the CLI could pass a real clinical label later.
    row = build_insight_row(visit, result, primary_diagnosis=None)

    # The follow-up row is written inside the insight save transaction, so a
    # brief can never exist without its queue entry, or vice versa.
    def write_follow_up(active: psycopg.Connection) -> None:
        follow_up_service.sync_from_insights(active, scope, visit, result.follow_up)

    saved = insights_repo.save(conn, scope, row, on_saved=write_follow_up)

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

def get_insights(conn: psycopg.Connection, scope: DoctorScope,
                 visit_id: str) -> InsightRow:
    """One visit's full brief.

    Two 404s with different messages, and the distinction is only safe because
    both reads are scoped: "no visit" fires for an unknown visit *and* for another
    doctor's visit, so the more specific "no insights yet" can never be reached
    for a visit the caller does not own. Had the visit check been unscoped, the
    difference between the two messages would have told a caller that a visit id
    exists somewhere in the system.
    """
    if visits_repo.get(conn, scope, visit_id) is None:
        raise NotFoundError(f"No visit with id {visit_id}.")
    row = insights_repo.get_for_visit(conn, scope, visit_id)
    if row is None:
        raise NotFoundError(f"No insights generated for visit {visit_id} yet.")
    return row


def check_against_next_visit(
    conn: psycopg.Connection,
    scope: DoctorScope,
    visit: VisitRow,
    due: Optional[str],
) -> str:
    """Compare a resolved follow-up date to when the patient actually returned.

    Seeded visit dates were chained from each transcript's stated interval, so
    for any visit with a successor these should agree. A mismatch means either
    the extraction or the seed data is wrong, which makes this the most useful
    single signal that the pipeline is behaving.
    """
    later = [v for v in visits_repo.list_visits(conn, scope,
                                                patient_id=visit.patient_id)
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
