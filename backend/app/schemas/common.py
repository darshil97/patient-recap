"""Shared response shapes."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """One error shape for every failure, so a UI can handle them uniformly."""

    code: str
    message: str
    details: Dict[str, Any] = {}


class HealthResponse(BaseModel):
    status: str
    database: str
    model: str
    api_keys_configured: int


class KeyHealth(BaseModel):
    index: int
    usable: bool
    kind: str
    length: int
    looks_like_google_key: bool
    detail: str = ""


class KeyHealthResponse(BaseModel):
    model: str
    keys: List[KeyHealth]


class VisitStatusRow(BaseModel):
    patient: str
    visit_number: int
    visit_date: str
    insights_generated: bool
    follow_up_type: Optional[str] = None
    follow_up_date: Optional[str] = None
    total_tokens: Optional[int] = None
