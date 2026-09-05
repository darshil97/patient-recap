"""Home-screen counters."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class VisitStats(BaseModel):
    total: int
    scheduled: int
    in_progress: int
    completed: int
    cancelled: int
    with_insights: int

    # The two states that need doctor action.
    awaiting_transcript: int
    awaiting_insights: int


class FollowUpStats(BaseModel):
    open: int
    overdue: int
    due_next_7_days: int
    needs_confirmation: int
    resolved: int
    dismissed: int


class StatsOut(BaseModel):
    """Counts for the home screen in one request.

    `overdue` and `due_next_7_days` are relative to the server's date, so
    `as_of` is returned for honest labelling.
    """

    patients: int
    visits: VisitStats
    follow_ups: FollowUpStats
    as_of: date
