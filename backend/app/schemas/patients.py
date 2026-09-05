"""Patient request/response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)


class PatientCreate(BaseModel):
    """Payload for POST /patients."""

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: str = Field(min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)

    # Required, not age: age is derivable at any visit date, and storing it
    # instead would make historical visits wrong as the patient gets older.
    date_of_birth: date

    gender: Optional[Literal["male", "female", "others"]] = None
    phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[EmailStr] = None

    # Omit to let the database generate one ('P-2026-00042').
    external_id: Optional[str] = Field(default=None, max_length=64)

    # Set true to register someone who shares a name and date of birth with an
    # existing patient.
    allow_duplicate: bool = False

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_future(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        return value


class PatientUpdate(BaseModel):
    """Correct a patient's demographics. At least one field required.

    `external_id` is not editable: it is the identity key. `date_of_birth`
    matters most here, because a wrong one silently corrupts `age_at_visit` on
    every one of the patient's visits.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    first_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    last_name: Optional[str] = Field(default=None, max_length=100)
    date_of_birth: Optional[date] = None
    gender: Optional[str] = Field(default=None, max_length=32)
    phone: Optional[str] = Field(default=None, max_length=32)
    email: Optional[EmailStr] = None

    @field_validator("date_of_birth")
    @classmethod
    def _not_in_future(cls, value: Optional[date]) -> Optional[date]:
        if value and value > date.today():
            raise ValueError("date_of_birth cannot be in the future")
        return value

    @model_validator(mode="after")
    def _at_least_one(self) -> "PatientUpdate":
        if not self.model_fields_set:
            raise ValueError("provide at least one field to update")
        return self


class PatientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    patient_id: str
    external_id: str
    first_name: str
    last_name: Optional[str]
    patient_name: str
    date_of_birth: Optional[date]
    gender: Optional[str]
    phone: Optional[str]
    email: Optional[str]

    # Derived in the query so there is one definition of the arithmetic.
    # `age` is as at today, unlike VisitOut.age_at_visit.
    age: Optional[int] = None
    visit_count: int = 0
    last_visit_date: Optional[date] = None

    created_at: Optional[datetime] = None
