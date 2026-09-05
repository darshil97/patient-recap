"""Patient timeline: the whole record in one request."""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from app.schemas.follow_ups import FollowUpSummary
from app.schemas.insights import InsightSummary
from app.schemas.patients import PatientOut
from app.schemas.visits import VisitOut


class TimelineVisit(VisitOut):
    """A visit with its generated brief and tracked follow-up attached.

    Both are null when absent, which is the normal state for a visit that is
    scheduled but not yet held.
    """

    insight: Optional[InsightSummary] = None
    follow_up: Optional[FollowUpSummary] = None


class TimelineOut(BaseModel):
    """A patient plus every visit, newest first.

    Exists to collapse what would otherwise be one request per visit: rendering
    a six-visit history took seven round trips.
    """

    patient: PatientOut
    visits: List[TimelineVisit]
