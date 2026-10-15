"""Shared response shapes."""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """One error shape for every failure, so a UI can handle them uniformly."""

    code: str
    message: str
    details: Dict[str, Any] = {}


class HealthResponse(BaseModel):
    status: str
    database: str
    provider: str
    model: str
    api_key_configured: bool


class KeyHealthResponse(BaseModel):
    """Result of one minimal request with the configured provider's API key."""

    provider: str
    model: str
    key_variable: str
    configured: bool
    usable: bool
    # ok | not_configured | quota | bad_key | request
    kind: str
    detail: str = ""


class VisitStatusRow(BaseModel):
    patient: str
    visit_number: int
    visit_date: str
    insights_generated: bool
    follow_up_type: Optional[str] = None
    follow_up_date: Optional[str] = None
    total_tokens: Optional[int] = None
