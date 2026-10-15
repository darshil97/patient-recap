"""Insight and job request/response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict


class GenerateInsightsRequest(BaseModel):
    """Payload for POST /visits/{visit_id}/insights.

    The transcript is not sent: it is loaded by reference from the visit's
    transcript_path, and patient facts come from the database, so nothing about
    the clinical content is client-supplied here.

    There is deliberately no `stop_after`. A brief is only meaningful once all
    three layers have run -- Layer 3 is the doctor-facing part, and the frontend
    renders it directly -- so a partial run produced an `insights_json` with no
    `layer_3` while still flipping `insights_generated` to TRUE, and opening that
    visit crashed the page. The knob still exists where it is useful: on
    `cli/load_insights.py --stop-after`, which is operator-only and used for
    iterating on an early prompt without paying for the whole chain.
    """

    # Rebuild insights that already exist. Without this a second submit is a 409.
    regenerate: bool = False

    # Override the model id within the configured llm.provider (a Gemini name,
    # or a Bedrock model id). On Gemini each model has its own daily quota.
    model: Optional[str] = None


class JobOut(BaseModel):
    """A job's current state. Poll this after a 202."""

    model_config = ConfigDict(from_attributes=True)

    job_id: str
    visit_id: str

    # queued | running | succeeded | failed | failed_quota
    # failed_quota is separate because it is worth retrying unchanged once the
    # daily limit resets, rather than investigating.
    status: str

    layers_completed: int
    stop_after: int
    regenerate: bool
    model_name: Optional[str]
    insight_id: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    total_tokens: Optional[int]
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class JobAccepted(BaseModel):
    """202 response: where to look next."""

    job_id: str
    visit_id: str
    status: str
    poll_url: str
    insights_url: str


class InsightSummary(BaseModel):
    """The slice of an insight shown on a patient timeline.

    Summary fields only: `insights_json` is 20-30 KB per visit, so including it
    would make a six-visit timeline a 200 KB payload. The full record stays on
    GET /visits/{visit_id}/insights.
    """

    model_config = ConfigDict(from_attributes=True)

    insight_id: str
    chief_issue: Optional[str] = None
    next_visit_focus: Optional[str] = None
    follow_up_type: str
    follow_up_date: Optional[date] = None
    follow_up_date_latest: Optional[date] = None
    follow_up_needs_confirmation: bool = False
    total_tokens: Optional[int] = None
    generated_at: Optional[datetime] = None


class InsightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    insight_id: str
    visit_id: str
    clinical_summary: Optional[str]

    # With primary_diagnosis gone from the API, this is the condition descriptor
    # everywhere. It is a full sentence from Layer 3, not a label.
    chief_issue: Optional[str]

    patient_age: Optional[int]
    patient_gender: Optional[str]

    follow_up_type: str
    follow_up_date: Optional[date]
    follow_up_date_latest: Optional[date]
    follow_up_stated: Optional[str]
    follow_up_needs_confirmation: bool

    next_visit_focus: Optional[str]

    # {"layer_1": ..., "layer_2": ..., "layer_3": ..., "follow_up": ...}
    insights_json: Dict[str, Any]

    model_name: str
    model_version: Optional[str]
    prompt_version: Optional[str]
    extraction_version: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    generated_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
