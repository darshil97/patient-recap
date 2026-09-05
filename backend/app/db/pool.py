"""
Database connection pool.

autocommit=True is deliberate and load-bearing. Without it, any query -
including a plain SELECT - implicitly opens a transaction that stays open, and a
later `with conn.transaction()` block degrades into a mere SAVEPOINT inside it.
Writes then accumulate in one long transaction that only commits when the
connection is returned, so an interrupted batch would discard all of it. With
autocommit on, `conn.transaction()` issues a real BEGIN/COMMIT, which is what
makes each visit independently durable.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None


def create_pool(conninfo: Optional[str] = None, *, open_now: bool = True) -> ConnectionPool:
    """Build a pool. Callers normally use get_pool() instead."""
    settings = get_settings()
    dsn = conninfo or settings.connstr
    if not dsn:
        raise RuntimeError(
            "No database connection string. Set CONNSTR in your environment or .env."
        )

    pool = ConnectionPool(
        conninfo=dsn,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    if open_now:
        pool.open()
    return pool


def get_pool() -> ConnectionPool:
    """The process-wide pool, created on first use."""
    global _pool
    if _pool is None:
        _pool = create_pool()
    return _pool


def close_pool() -> None:
    """Close the process-wide pool, if one was opened."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Borrow a connection from the pool."""
    with get_pool().connection() as conn:
        yield conn
