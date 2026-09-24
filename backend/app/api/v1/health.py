"""Health and diagnostics."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.api.deps import require_auth
from app.config import get_settings
from app.db.pool import connection
from app.llm.gemini import check_keys
from app.schemas.common import HealthResponse, KeyHealth, KeyHealthResponse

logger = logging.getLogger(__name__)

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
def health() -> HealthResponse:
    """Liveness, plus one `SELECT 1` so a passing check means the DB is reachable.

    The connection is borrowed *inside* the handler rather than through
    `Depends(get_db)`. As a dependency it was acquired before the body ran, so
    the one failure this route exists to report — the database being unreachable
    — raised out of the dependency and returned a bare 500. The `degraded` branch
    below could only ever fire for a database that accepted a connection and then
    failed a `SELECT 1`, which is close to nothing.

    The exception is logged, never returned. This route is deliberately
    unauthenticated so a monitor can use it without holding the shared secret,
    and psycopg's connection errors carry the host, user and database name.

    Still 200 when degraded. UptimeRobot polls this every 12 minutes, which is
    what keeps the Render instance warm *and* the Neon branch from suspending
    (§9.5); a non-2xx here would turn every cold start into a page.
    """
    settings = get_settings()
    try:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        database = "ok"
    except Exception:
        logger.exception("Health check could not reach the database")
        database = "error"

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
