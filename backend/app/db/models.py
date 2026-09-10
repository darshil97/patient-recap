"""Row shapes returned by the repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Dict, Optional


@dataclass
class DoctorRow:
    """The owner of a set of patients. Tenancy anchor, not a user account.

    Neon Auth owns login, sessions and passwords. This exists so patients can
    foreign-key to an owner. `auth_user_id` is Neon Auth's subject id and is the
    preferred resolution key because, unlike email, it never changes; it is
    Optional because the seed migration creates the first doctor before that
    doctor has ever signed in.
    """

    doctor_id: str
    email: str
    auth_user_id: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[datetime] = None

    @property
    def display_name(self) -> str:
        return self.name or self.email


@dataclass
class PatientRow:
    patient_id: str
    # The owning doctor. Every other table in the schema reaches its tenant
    # through this column; see app/db/tenancy.py.
    doctor_id: str
    external_id: str
    first_name: str
    last_name: Optional[str]
    date_of_birth: Optional[date]
    gender: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Derived in the query rather than the frontend, so there is one definition
    # of the arithmetic. `age` is as at today, unlike VisitRow.age_at_visit.
    visit_count: int = 0
    last_visit_date: Optional[date] = None
    age: Optional[int] = None

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p)

    @property
    def patient_name(self) -> str:
        return self.full_name or "Unknown"


@dataclass
class VisitRow:
    """A visit joined to its patient, with age evaluated as at the visit date."""

    visit_id: str
    patient_id: str
    visit_number: int
    visit_date: date
    visit_time: Optional[time]
    visit_type: Optional[str]
    visit_status: str
    transcript_path: Optional[str]
    transcript_provider: Optional[str]
    insights_generated: bool
    transcript_filename: Optional[str] = None
    transcript_uploaded_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    # Joined from patients, not stored on visits. A visit's tenant is derived
    # through its patient (§6), so this is read-only provenance: knowing which
    # doctor a loaded visit belongs to without a second query.
    doctor_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    external_id: Optional[str] = None
    gender: Optional[str] = None
    age_at_visit: Optional[int] = None
    created_at: Optional[datetime] = None

    @property
    def label(self) -> str:
        who = self.first_name or "patient"
        return f"{who} visit {self.visit_number} ({self.visit_date})"

    @property
    def patient_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or "Unknown"

    @property
    def has_insights(self) -> bool:
        """Same flag as insights_generated, named for how a UI reads it."""
        return self.insights_generated

    @property
    def prompt_gender(self) -> str:
        """'female' -> 'Female' for the prompt; the raw value is what's stored."""
        return (self.gender or "").strip().title() or "Not recorded"


@dataclass
class InsightRow:
    insight_id: str
    visit_id: str
    clinical_summary: Optional[str]
    chief_issue: Optional[str]
    primary_diagnosis: Optional[str]
    patient_age: Optional[int]
    patient_gender: Optional[str]
    follow_up_type: str
    follow_up_date: Optional[date]
    follow_up_date_latest: Optional[date]
    follow_up_stated: Optional[str]
    follow_up_needs_confirmation: bool
    next_visit_focus: Optional[str]
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


@dataclass
class FollowUpRow:
    """A tracked follow-up, joined to its patient and source visit.

    Lives in its own table rather than on visit_insights: regeneration upserts
    that row in place, so resolution state stored there would be silently
    overwritten by a rerun. A follow-up's lifecycle belongs to the clinic, not
    to the model.
    """

    follow_up_id: str
    source_visit_id: str
    patient_id: str
    due_earliest: date
    due_latest: Optional[date]
    needs_confirmation: bool
    stated: Optional[str]
    purpose: Optional[str]
    status: str
    resolved_by_visit_id: Optional[str] = None
    resolved_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    last_contacted_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    # Joined for the queue, which is the most-read query in the application.
    # doctor_id is joined from patients, NOT stored on follow_ups: see the module
    # docstring in app/db/repositories/follow_ups.py for why patient_id is
    # denormalised here but doctor_id deliberately is not.
    doctor_id: Optional[str] = None
    external_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None
    source_visit_number: Optional[int] = None
    source_visit_date: Optional[date] = None
    chief_issue: Optional[str] = None
    next_visit_focus: Optional[str] = None

    @property
    def patient_name(self) -> str:
        return " ".join(p for p in (self.first_name, self.last_name) if p) or "Unknown"


@dataclass
class JobRow:
    job_id: str
    visit_id: str
    status: str
    layers_completed: int
    stop_after: int
    regenerate: bool
    model_name: Optional[str]
    insight_id: Optional[str]
    error_code: Optional[str]
    error_message: Optional[str]
    total_tokens: Optional[int]
    worker_id: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ("succeeded", "failed", "failed_quota")
