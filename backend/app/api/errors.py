"""
Mapping application errors onto HTTP responses.

One place, so routers stay free of translation logic and every failure reaches a
client in the same JSON shape.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import (
    AppError,
    AudioStorageError,
    ConflictError,
    NotFoundError,
    QuotaExhaustedError,
    SpeechToTextError,
    SpeechToTextUnavailableError,
    TranscriptError,
    TranscriptTooLargeError,
    UnauthorizedError,
    ValidationError,
)

logger = logging.getLogger(__name__)

# Order matters: the first match wins, so subclasses come before their parents.
STATUS_BY_TYPE = (
    (UnauthorizedError, 401),
    (NotFoundError, 404),
    (ConflictError, 409),             # DuplicatePatientError subclasses this
    (TranscriptTooLargeError, 413),
    (QuotaExhaustedError, 429),
    (ValidationError, 422),
    (TranscriptError, 422),           # a bad path is the caller's mistake
    (SpeechToTextUnavailableError, 503),  # before its parent: retry shortly
    (SpeechToTextError, 502),         # Deepgram failed, not the upload
    (AudioStorageError, 503),         # retry the chunk
)

DEFAULT_STATUS = 500


def status_for(error: AppError) -> int:
    for error_type, status in STATUS_BY_TYPE:
        if isinstance(error, error_type):
            return status
    return DEFAULT_STATUS


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        status = status_for(exc)
        if status >= 500:
            logger.exception("Unhandled application error on %s", request.url.path)
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": exc.message, "details": exc.details},
        )
