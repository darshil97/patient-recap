"""Visit request/response models."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

VISIT_TYPES = ("initial", "follow_up")
VISIT_STATUSES = ("scheduled", "in_progress", "completed", "cancelled")


class VisitCreate(BaseModel):
    """Payload for POST /visits."""

    model_config = ConfigDict(str_strip_whitespace=True)

    # Typed as UUID so a malformed id is a 422 here rather than a database error
    # from Postgres failing to parse it.
    patient_id: UUID

    # The real consultation date. It anchors every relative follow-up
    # ("in three weeks"), so it is required rather than defaulted to today.
    visit_date: date

    visit_time: Optional[time] = None

    # Omit to have the next sequential number assigned.
    visit_number: Optional[int] = Field(default=None, ge=1)

    # Omit and it is 'initial' for visit 1, 'follow_up' otherwise.
    visit_type: Optional[str] = None
    visit_status: str = "scheduled"

    # Optional: a scheduled visit has no recording yet. Attach it later with
    # PATCH /visits/{visit_id}. Supplying it here loads historical data in one call.
    #
    # There is deliberately NO transcript_provider field. Which backend holds a
    # transcript is a server-side storage decision (§8.22) and the client cannot
    # know it: it is TRANSCRIPT_STORAGE for a fresh upload, and 'local' for a path
    # supplied here, because verifying a pre-existing transcript means stat-ing a
    # file. Accepting it from the client let a caller either override the
    # configured backend or, worse, claim a provider that does not hold the bytes
    # — which makes the transcript unreadable and the visit ungeneratable.
    #
    # Pydantic ignores unknown fields, so an older client still sending
    # transcript_provider is silently and harmlessly ignored rather than 422'd.
    transcript_path: Optional[str] = None

    @model_validator(mode="after")
    def _check_enums(self) -> "VisitCreate":
        if self.visit_type and self.visit_type not in VISIT_TYPES:
            raise ValueError(f"visit_type must be one of {VISIT_TYPES}")
        if self.visit_status not in VISIT_STATUSES:
            raise ValueError(f"visit_status must be one of {VISIT_STATUSES}")
        return self


class VisitUpdate(BaseModel):
    """Payload for PATCH /visits/{visit_id}.

    Mainly used to attach the transcript once the consultation has happened,
    which also moves the visit to 'completed' unless a status is given.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    # No transcript_provider here either; see VisitCreate above.
    transcript_path: Optional[str] = None
    visit_status: Optional[str] = None
    visit_date: Optional[date] = None
    visit_time: Optional[time] = None
    visit_type: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "VisitUpdate":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        if self.visit_type and self.visit_type not in VISIT_TYPES:
            raise ValueError(f"visit_type must be one of {VISIT_TYPES}")
        if self.visit_status and self.visit_status not in VISIT_STATUSES:
            raise ValueError(f"visit_status must be one of {VISIT_STATUSES}")
        return self


class VisitCompleteRequest(BaseModel):
    """Optional corrections applied at the moment the visit is completed."""

    visit_date: Optional[date] = None
    visit_time: Optional[time] = None


class VisitCancelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    reason: Optional[str] = Field(default=None, max_length=2000)


class VisitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    visit_id: str
    patient_id: str
    visit_number: int
    visit_date: date
    visit_time: Optional[time]
    visit_type: Optional[str]
    visit_status: str

    # Patient identity, so a queue row needs no second request.
    patient_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    external_id: Optional[str] = None
    gender: Optional[str] = None
    age_at_visit: Optional[int] = None

    transcript_path: Optional[str] = None
    transcript_provider: Optional[str] = None
    transcript_filename: Optional[str] = None
    transcript_uploaded_at: Optional[datetime] = None

    # has_insights is insights_generated under a name that reads correctly in a
    # UI; the original is kept so nothing that relies on it breaks.
    insights_generated: bool
    has_insights: bool

    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
