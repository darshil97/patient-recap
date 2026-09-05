"""Follow-up request/response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

FOLLOW_UP_STATUSES = ("open", "resolved", "dismissed")

# What a client may set. 'resolved' is excluded on purpose: resolution is caused
# by scheduling a visit, so allowing it here would create resolved rows with no
# visit behind them.
SETTABLE_STATUSES = ("open", "dismissed")


class FollowUpOut(BaseModel):
    """Everything a queue row needs, in one request.

    `phone` is included because the action on this screen is to telephone the
    patient.

    Deliberately absent: any `days_overdue` or `is_overdue` field. "Overdue"
    depends on today, and a server-computed value goes stale in a cached
    response, so the frontend derives it from `due_earliest`.
    """

    model_config = ConfigDict(from_attributes=True)

    follow_up_id: str
    status: str

    patient_id: str
    external_id: Optional[str] = None
    patient_name: str
    phone: Optional[str] = None
    patient_age: Optional[int] = None
    patient_gender: Optional[str] = None

    source_visit_id: str
    source_visit_number: Optional[int] = None
    source_visit_date: Optional[date] = None

    due_earliest: date
    due_latest: Optional[date] = None
    needs_confirmation: bool
    stated: Optional[str] = None
    purpose: Optional[str] = None

    # Layer 3 fields, so a queue row can show what the visit was about without a
    # second request.
    chief_issue: Optional[str] = None
    next_visit_focus: Optional[str] = None

    resolved_by_visit_id: Optional[str] = None
    resolved_reason: Optional[str] = None
    resolved_at: Optional[datetime] = None
    dismissed_reason: Optional[str] = None
    dismissed_at: Optional[datetime] = None
    last_contacted_at: Optional[datetime] = None
    note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FollowUpUpdate(BaseModel):
    """The outcome of a phone call. At least one field required."""

    model_config = ConfigDict(str_strip_whitespace=True)

    status: Optional[str] = None
    dismissed_reason: Optional[str] = Field(default=None, max_length=2000)
    note: Optional[str] = Field(default=None, max_length=4000)

    # Records that the patient was contacted without changing status, so
    # "I called, they are still deciding" is expressible.
    mark_contacted: bool = False

    @model_validator(mode="after")
    def _check(self) -> "FollowUpUpdate":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        if self.status is not None and self.status not in SETTABLE_STATUSES:
            if self.status == "resolved":
                raise ValueError(
                    "a follow-up cannot be resolved directly; schedule a visit "
                    "for the patient and it resolves automatically")
            raise ValueError(f"status must be one of {list(SETTABLE_STATUSES)}")
        if self.status == "dismissed" and not (self.dismissed_reason or "").strip():
            raise ValueError("dismissed_reason is required when dismissing")
        return self


class FollowUpSummary(BaseModel):
    """The slice of a follow-up shown on a patient timeline."""

    model_config = ConfigDict(from_attributes=True)

    follow_up_id: str
    status: str
    due_earliest: date
    due_latest: Optional[date] = None
    needs_confirmation: bool = False
    resolved_by_visit_id: Optional[str] = None
