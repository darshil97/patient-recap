"""
Application error types.

Services raise these; the API layer maps them to HTTP status codes in one place
(app/api/errors.py) so routers stay free of error translation and the CLI can
catch the same exceptions without importing anything HTTP.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class AppError(Exception):
    """Base class for every expected failure."""

    code = "error"

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(AppError):
    """A referenced record does not exist. -> 404"""

    code = "not_found"


class ConflictError(AppError):
    """The request clashes with existing state. -> 409"""

    code = "conflict"


class ValidationError(AppError):
    """Semantically invalid input that Pydantic can't express. -> 422"""

    code = "invalid_request"


class DuplicatePatientError(ConflictError):
    """A probable duplicate patient already exists.

    Carries the existing record so the caller can decide whether to reuse it or
    resubmit with allow_duplicate=True.
    """

    code = "duplicate_patient"


class TranscriptError(AppError):
    """The transcript is missing, unreadable, or outside the allowed directory."""

    code = "transcript_error"


class PipelineError(AppError):
    """The LLM chain failed."""

    code = "pipeline_error"


class LayerOutputError(PipelineError):
    """A layer returned something the next layer cannot consume."""

    code = "layer_output_error"

    def __init__(self, message: str, raw_response: str = "",
                 details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)
        self.raw_response = raw_response


class QuotaExhaustedError(PipelineError):
    """Every configured API key is rate limited. -> 429

    Distinct from other pipeline failures because it is worth retrying
    unchanged once quota resets, rather than investigating.
    """

    code = "quota_exhausted"


class TranscriptTooLargeError(AppError):
    """The uploaded transcript exceeds the size ceiling. -> 413"""

    code = "transcript_too_large"


class UnauthorizedError(AppError):
    """Missing or wrong API key. -> 401"""

    code = "unauthorized"
