"""Home-screen counters, gathered in one round trip, for one doctor."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

import psycopg

from app.db.repositories import follow_ups as follow_ups_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import visits as visits_repo
from app.db.tenancy import DoctorScope


def build_stats(conn: psycopg.Connection, scope: DoctorScope,
                today: date | None = None) -> Dict[str, Any]:
    """Counts for the home screen, covering only the scoped doctor's records.

    `today` is resolved once and returned as `as_of`, so the relative counts
    (overdue, due in the next 7 days) and the label the UI shows cannot disagree.

    Worth stating why counters matter as much as lists here: a leaking list looks
    obviously wrong the moment a doctor reads an unfamiliar patient name, but a
    leaking counter just looks like a slightly high number. All three counts are
    scoped at the query, not filtered afterwards.
    """
    as_of = today or date.today()
    return {
        "patients": patients_repo.count(conn, scope),
        "visits": visits_repo.counts(conn, scope),
        "follow_ups": follow_ups_repo.counts(conn, scope, as_of),
        "as_of": as_of,
    }
