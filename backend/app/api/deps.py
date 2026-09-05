"""
Request dependencies.

AUTH: off unless `API_KEY` is set. When it is, every route that depends on
`require_auth` needs a matching `X-API-Key` header; when it is not, the API stays
open so local development is unchanged. `GET /health` is exempt either way, so a
monitor can check liveness without holding the secret.

This is a shared secret, which suits a single practice. It is not per-user
identity, so it gives no audit trail of who did what — worth replacing with real
authentication if more than one person ever uses this.
"""

from __future__ import annotations

import hmac
from typing import Iterator, Optional

import psycopg
from fastapi import Depends, Header

from app.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.db.pool import connection


def get_db() -> Iterator[psycopg.Connection]:
    """A pooled connection for the life of one request."""
    with connection() as conn:
        yield conn


def settings_dep() -> Settings:
    return get_settings()


async def require_auth(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """Enforce the shared secret when one is configured.

    Compared with hmac.compare_digest rather than `==` so the check does not leak
    the key's length or content through response timing.
    """
    expected = get_settings().api_key
    if not expected:
        return  # open by design when unset

    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise UnauthorizedError(
            "Missing or invalid API key. Send it in the X-API-Key header.")


DbConnection = Depends(get_db)
AppSettings = Depends(settings_dep)
RequireAuth = Depends(require_auth)
