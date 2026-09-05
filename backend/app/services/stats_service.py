"""Home-screen counters, gathered in one round trip."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

import psycopg

from app.db.repositories import follow_ups as follow_ups_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import visits as visits_repo


def build_stats(conn: psycopg.Connection, today: date | None = None) -> Dict[str, Any]:
    """Counts for the home screen.

    `today` is resolved once and returned as `as_of`, so the relative counts
    (overdue, due in the next 7 days) and the label the UI shows cannot disagree.
    """
    as_of = today or date.today()
    return {
        "patients": patients_repo.count(conn),
        "visits": visits_repo.counts(conn),
        "follow_ups": follow_ups_repo.counts(conn, as_of),
        "as_of": as_of,
    }
