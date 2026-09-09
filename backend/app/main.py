"""
FastAPI application.

    uvicorn app.main:app --reload

Interactive docs at /docs once running.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.api.v1.router import api_router
from app.config import get_settings
from app.core.logging import configure_logging
from app.db.pool import close_pool, get_pool

logger = logging.getLogger(__name__)

DESCRIPTION = """
Turns recorded homeopathic consultations into structured clinical records.

Typical flow:

1. `POST /patients` register the patient
2. `POST /visits` schedule the consultation
3. `PATCH /visits/{visit_id}` attach the transcript once the call has happened
4. `POST /visits/{visit_id}/insights` start the three-layer chain (returns 202)
5. `GET /jobs/{job_id}` poll until the status is terminal
6. `GET /visits/{visit_id}/insights` read the result

Insight generation is asynchronous because the chain is three sequential model
calls, roughly a minute in total.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()

    get_pool()  # fail fast if the database is unreachable

    # Jobs run in-process, so a crash leaves rows stuck in 'running'. Anything
    # still active that this process did not claim cannot be making progress.
    from app.services.job_service import reap_orphans

    reap_orphans()

    logger.info("Ready. model=%s api_keys=%d transcripts=%s",
                settings.gemini_model, len(settings.api_keys),
                settings.transcripts_dir)
    try:
        yield
    finally:
        close_pool()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Consultation Insights API",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
    )

    # Permissive by default for local UI development. Narrow CORS_ORIGINS before
    # this is exposed anywhere real.
    #
    # allow_credentials is off: auth is a plain X-API-Key header, which is not a
    # CORS credential, and 'Allow-Credentials: true' alongside 'Allow-Origin: *'
    # is rejected by browsers anyway.
    #
    # X-Total-Count has to be named in expose_headers or the browser hides it
    # from JavaScript: only a short safelist of response headers is readable
    # cross-origin by default, so paginated lists would arrive with no total.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count"],
    )

    register_error_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()
