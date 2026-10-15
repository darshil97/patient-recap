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

    # There is deliberately NO transcript_path and NO transcript_provider field.
    #
    # Both are server decisions, and accepting either from a client was the same
    # mistake twice. A provider the client names may not hold the bytes, which
    # makes the transcript unreadable and the visit ungeneratable (§8.22). A path
    # the client names is worse: the only check it could be given is that it
    # resolves inside `transcripts/`, which proves the file exists but *not* that
    # it belongs to this doctor — and since the locator is `transcripts/<visit
    # uuid>.txt` with no doctor_id in it, by design, any valid visit uuid passes.
    # One doctor could point their own visit at another doctor's transcript and
    # read a verbatim consultation through GET /visits/{id}/transcript.
    #
    # The address is now derived from visit_id at upload time and nowhere else:
    # POST /visits/{visit_id}/transcript is the only way a transcript is attached.
    #
    # Pydantic ignores unknown fields, so an older client still sending either one
    # is silently and harmlessly ignored rather than 422'd.

    @model_validator(mode="after")
    def _check_enums(self) -> "VisitCreate":
        if self.visit_type and self.visit_type not in VISIT_TYPES:
            raise ValueError(f"visit_type must be one of {VISIT_TYPES}")
        if self.visit_status not in VISIT_STATUSES:
            raise ValueError(f"visit_status must be one of {VISIT_STATUSES}")
        return self


class VisitUpdate(BaseModel):
    """Payload for PATCH /visits/{visit_id}: correct a visit's own details.

    Deliberately narrow. Three things it used to accept and no longer does:

      * `visit_status` — status is a lifecycle transition, not a field. Setting it
        here skipped everything the transitions exist to do: `/cancel`'s refusal
        once insights exist, its reopening of the follow-ups that visit had
        resolved, and the `completed_at` / `cancelled_at` stamps. A cancelled
        visit whose follow-up was never reopened drops a patient who still needs
        calling, with no error anywhere. Use `/complete` and `/cancel` (§8.17).
      * `transcript_path` — see VisitCreate; the address is derived from visit_id
        at upload time.
      * `transcript_provider` — a server-side storage decision (§8.22).

    `extra="forbid"`, which is deliberate and is the one place in the API that
    does this. Everywhere else an unknown field is ignored, so an older client
    keeps working — and that is right where the field was never honoured
    (`transcript_provider` on VisitCreate). Here all three removed fields *were*
    honoured. Ignoring them would answer 200 to "cancel this visit" and do
    nothing, which is exactly the class of silent failure this change exists to
    remove. A 422 naming the field says what happened.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    visit_date: Optional[date] = None
    visit_time: Optional[time] = None
    visit_type: Optional[str] = None

    @model_validator(mode="after")
    def _check(self) -> "VisitUpdate":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        if self.visit_type and self.visit_type not in VISIT_TYPES:
            raise ValueError(f"visit_type must be one of {VISIT_TYPES}")
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

    # Why, in the doctor's words. NULL means no reason was recorded — which is
    # every visit cancelled before the column existed, and is deliberately
    # distinct from an empty one.
    cancelled_reason: Optional[str] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
