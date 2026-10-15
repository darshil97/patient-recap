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
    """The model provider is throttling, out of quota, or briefly down. -> 429

    Distinct from other pipeline failures because it is worth retrying
    unchanged once quota resets, rather than investigating.
    """

    code = "quota_exhausted"


class ModelNotConfiguredError(PipelineError):
    """The selected model provider has no API key configured. -> 500

    A server configuration problem, raised before any call is made, with a
    message naming the variable to set.
    """

    code = "model_not_configured"


class TranscriptTooLargeError(AppError):
    """The uploaded transcript exceeds the size ceiling. -> 413"""

    code = "transcript_too_large"


class SpeechToTextError(AppError):
    """The speech-to-text service failed on a recording. -> 502

    For failures the doctor cannot fix by choosing a different file. An audio
    file the service cannot decode is a TranscriptError (422) instead, because
    that one is the upload's fault.
    """

    code = "speech_to_text_failed"


class AudioStorageError(AppError):
    """Recording bytes could not be stored or read back. -> 503

    Not a TranscriptError (422): a storage hiccup is not the upload's fault, and a
    503 is what tells the browser's chunk uploader that retrying is worthwhile.
    """

    code = "audio_storage_error"


class SpeechToTextUnavailableError(SpeechToTextError):
    """The speech-to-text service is throttling, timed out, or is down. -> 503

    Worth retrying unchanged in a moment, like QuotaExhaustedError for the LLM.
    """

    code = "speech_to_text_unavailable"


class UnauthorizedError(AppError):
    """Missing or wrong API key. -> 401"""

    code = "unauthorized"
