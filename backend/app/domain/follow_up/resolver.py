"""
Turning what the doctor said into calendar dates.

Layer 1 captures the follow-up as stated ("come back in three weeks") broken
into parts. The arithmetic happens here, in Python, because LLMs are unreliable
at date maths and it needs to be deterministic, auditable and unit-testable.
The verbatim phrase is kept alongside the computed date so a clinician can see
what was actually said.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from dateutil.relativedelta import relativedelta

from app.domain.follow_up.models import (
    CONDITIONAL_ONLY,
    NOT_DISCUSSED,
    SCHEDULED,
    VALID_ACTIONS,
    VALID_URGENCY,
    ResolvedFollowUp,
)

logger = logging.getLogger(__name__)

# Doctors speak in words ("three weeks"), so the model may pass a word through
# even though the schema asks for a number.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "couple": 2, "few": 3, "half": 0,
}

_UNIT_DELTA = {"day": "days", "week": "weeks", "month": "months", "year": "years"}


def _coerce_number(value: Any) -> Optional[float]:
    """Turn 3, '3', '3.0' or 'three' into a number. None if hopeless."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return float(_NUMBER_WORDS[text]) if text in _NUMBER_WORDS else None
    return None


def _normalise_unit(value: Any) -> Optional[str]:
    """Accept 'week', 'weeks', 'Weeks' -> 'week'. None if unrecognised."""
    if not isinstance(value, str):
        return None
    unit = value.strip().lower().rstrip("s")
    return unit if unit in _UNIT_DELTA else None


def _parse_date(value: Any) -> Optional[date]:
    """Parse an ISO date. Deliberately strict: no guessing at formats."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _shift(anchor: date, amount: float, unit: str) -> date:
    """Add amount x unit to anchor using calendar-correct arithmetic.

    relativedelta rather than timedelta so months land on the same day-of-month
    where possible (31 Jan + 1 month = 28 Feb) instead of drifting by 30 days.
    """
    return anchor + relativedelta(**{_UNIT_DELTA[unit]: int(round(amount))})


def _label(min_value: Optional[float], max_value: Optional[float],
           unit: Optional[str], approximate: bool) -> Optional[str]:
    """Human-readable window, e.g. '3 weeks' or 'about 3 to 4 months'."""
    if unit is None or min_value is None:
        return None

    def fmt(n: float) -> str:
        return str(int(n)) if float(n).is_integer() else str(n)

    if max_value is not None and max_value != min_value:
        body = f"{fmt(min_value)} to {fmt(max_value)} {unit}s"
    else:
        body = f"{fmt(min_value)} {unit}{'' if min_value == 1 else 's'}"
    return f"about {body}" if approximate else body


def _normalise_triggers(raw: Any, warnings: List[str]) -> List[Dict[str, Any]]:
    """Validate the come-sooner conditions, keeping unknown values visible."""
    if raw in (None, ""):
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        warnings.append("return_earlier_if was not a list; ignored")
        return []

    triggers: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            triggers.append({"stated": item, "condition": item,
                             "action": "contact_clinic", "urgency": "routine"})
            continue
        if not isinstance(item, dict):
            continue

        action = item.get("action") or "contact_clinic"
        urgency = item.get("urgency") or "routine"
        if action not in VALID_ACTIONS:
            warnings.append(f"unrecognised follow-up action: {action!r}")
        if urgency not in VALID_URGENCY:
            warnings.append(f"unrecognised follow-up urgency: {urgency!r}")

        triggers.append({
            "stated": item.get("stated"),
            "condition": item.get("condition") or item.get("stated") or "",
            "action": action,
            "urgency": urgency,
        })
    return triggers


def _resolve_window(
    timing: Dict[str, Any],
    anchor: Optional[date],
    warnings: List[str],
) -> Tuple[Optional[date], Optional[date], str, Optional[str]]:
    """Resolve a timing block to (earliest, latest, basis, window_label)."""
    declared = str(timing.get("type") or "unspecified").strip().lower()
    approximate = bool(timing.get("approximate"))

    if declared == "absolute":
        target = _parse_date(timing.get("absolute_date"))
        if target is None:
            warnings.append("timing.type was 'absolute' but absolute_date was unusable")
            return None, None, "unspecified", None
        return target, target, "absolute", target.isoformat()

    if declared in ("relative", "range"):
        unit = _normalise_unit(timing.get("unit"))
        low = _coerce_number(timing.get("min_value"))
        high = _coerce_number(timing.get("max_value"))
        if low is None and high is not None:
            low = high
        if high is None:
            high = low

        if unit is None or low is None:
            warnings.append(
                f"timing.type was {declared!r} but unit/values were unusable "
                f"(unit={timing.get('unit')!r}, min={timing.get('min_value')!r})"
            )
            return None, None, "unspecified", None

        if high < low:
            warnings.append("max_value was below min_value; values swapped")
            low, high = high, low

        # basis follows the values, not the model's label, so a range mislabelled
        # 'relative' is still treated as a range.
        basis = "range" if high != low else "relative"
        label = _label(low, high, unit, approximate)
        if anchor is None:
            return None, None, basis, label
        return _shift(anchor, low, unit), _shift(anchor, high, unit), basis, label

    if declared not in ("unspecified", "none", "null", ""):
        warnings.append(f"unrecognised timing.type: {declared!r}")
    return None, None, "unspecified", None


def resolve_follow_up(layer_1_data: Any, call_date: Optional[str]) -> ResolvedFollowUp:
    """Resolve Layer 1's `follow_up` block into calendar dates.

    Tolerant by design: a missing or malformed block yields a `not_discussed`
    result with warnings attached rather than raising, so one bad extraction
    doesn't sink a whole batch. Inspect `.warnings` for what couldn't resolve.
    """
    resolved = ResolvedFollowUp(call_date=call_date)
    warnings = resolved.warnings

    anchor = _parse_date(call_date)
    if anchor is None:
        warnings.append(
            f"call_date {call_date!r} is not an ISO date (YYYY-MM-DD); "
            "relative follow-ups cannot be resolved"
        )

    block = layer_1_data.get("follow_up") if isinstance(layer_1_data, dict) else None
    if not isinstance(block, dict):
        warnings.append("Layer 1 output contained no usable 'follow_up' block")
        return resolved

    scheduled = block.get("scheduled_visit")
    scheduled = scheduled if isinstance(scheduled, dict) else {}
    timing = scheduled.get("timing")
    timing = timing if isinstance(timing, dict) else {}

    resolved.commitment = str(scheduled.get("commitment") or "not_discussed").strip().lower()
    resolved.stated = scheduled.get("stated") or None
    resolved.purpose = scheduled.get("purpose") or None
    resolved.stated_by = scheduled.get("stated_by") or None
    resolved.approximate = bool(timing.get("approximate"))
    resolved.come_sooner_if = _normalise_triggers(block.get("return_earlier_if"), warnings)

    # A follow-up spoken by the patient means the transcript's speaker labels are
    # probably swapped. Surface it rather than quietly trusting the attribution.
    if resolved.stated_by and resolved.stated_by.strip().lower() == "patient":
        warnings.append(
            "follow-up instruction is attributed to the patient; "
            "transcript speaker labels may be swapped"
        )

    earliest, latest, basis, label = _resolve_window(timing, anchor, warnings)
    resolved.basis = basis
    resolved.window_label = label

    if earliest is not None:
        resolved.type = SCHEDULED
        resolved.due_earliest = earliest.isoformat()
        resolved.due_latest = (latest or earliest).isoformat()
        if anchor is not None:
            resolved.days_from_call = (earliest - anchor).days
            if resolved.days_from_call < 0:
                warnings.append("resolved follow-up date falls before the call date")
    elif resolved.come_sooner_if:
        resolved.type = CONDITIONAL_ONLY
    else:
        resolved.type = NOT_DISCUSSED

    if resolved.commitment == "definite" and resolved.type != SCHEDULED:
        warnings.append(
            "commitment was 'definite' but no usable timing was captured; "
            "the visit date needs confirming with the clinic"
        )
    if resolved.type == NOT_DISCUSSED and resolved.commitment == "not_discussed":
        warnings.append("next visit was not discussed on this call")

    # Confidence describes the resolved date only. A confidently-absent date
    # (conditional or not discussed) stays 'high'.
    if resolved.type == SCHEDULED:
        if resolved.basis == "range" or resolved.approximate:
            resolved.confidence = "medium"
        if any("unusable" in w or "unrecognised" in w or "swapped" in w for w in warnings):
            resolved.confidence = "low"
        resolved.needs_human_confirmation = resolved.confidence != "high"
    elif resolved.commitment in ("definite", "patient_discretion"):
        resolved.confidence = "low"
        resolved.needs_human_confirmation = True

    return resolved
