"""Health and diagnostics."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends

from app.api.deps import get_db, require_auth
from app.config import get_settings
from app.llm.gemini import check_keys
from app.schemas.common import HealthResponse, KeyHealth, KeyHealthResponse

# GET /health is deliberately unauthenticated so a monitor can check liveness
# without holding the shared secret. It stays exempt from the doctor-identity gate
# too: a liveness probe is not a doctor, and requiring it to be one would mean
# monitoring could not distinguish "the service is down" from "my headers are
# wrong".
#
# The key-health route below requires the API key, because each call spends one
# request from every Gemini key's daily quota. It deliberately does *not* require
# a doctor identity: it reports on the server's own credentials and touches no
# patient data, so there is no tenant to scope it to.
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Service health")
def health(conn: psycopg.Connection = Depends(get_db)) -> HealthResponse:
    settings = get_settings()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        database = "ok"
    except Exception as exc:  # pragma: no cover
        database = f"error: {exc}"

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        database=database,
        model=settings.gemini_model,
        api_keys_configured=len(settings.api_keys),
    )


@router.get(
    "/health/api-keys",
    response_model=KeyHealthResponse,
    dependencies=[Depends(require_auth)],
    summary="Check which Gemini keys are usable",
)
def api_key_health() -> KeyHealthResponse:
    """Send one minimal request per key to see which work.

    Never poll this: each call consumes one request from every working key's
    daily quota, and the free tier allows only 20 per key per day.
    """
    settings = get_settings()
    results = check_keys(settings.gemini_model, settings.api_keys)
    return KeyHealthResponse(
        model=settings.gemini_model,
        keys=[KeyHealth(**r) for r in results],
    )
