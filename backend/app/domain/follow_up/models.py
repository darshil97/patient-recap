"""Follow-up value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# follow_up_type values. Mirrored by a CHECK constraint on visit_insights.
SCHEDULED = "scheduled"
CONDITIONAL_ONLY = "conditional_only"
NOT_DISCUSSED = "not_discussed"

VALID_ACTIONS = {"contact_clinic", "record_and_report", "return_immediately"}
VALID_URGENCY = {"routine", "urgent"}


@dataclass
class ResolvedFollowUp:
    """A Layer 1 follow-up block resolved against the consultation date."""

    type: str = NOT_DISCUSSED
    commitment: str = "not_discussed"
    basis: str = "unspecified"
    call_date: Optional[str] = None
    due_earliest: Optional[str] = None
    due_latest: Optional[str] = None
    days_from_call: Optional[int] = None
    window_label: Optional[str] = None
    approximate: bool = False
    confidence: str = "high"
    needs_human_confirmation: bool = False
    stated: Optional[str] = None
    purpose: Optional[str] = None
    stated_by: Optional[str] = None
    come_sooner_if: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "commitment": self.commitment,
            "basis": self.basis,
            "call_date": self.call_date,
            "due_earliest": self.due_earliest,
            "due_latest": self.due_latest,
            "days_from_call": self.days_from_call,
            "window_label": self.window_label,
            "approximate": self.approximate,
            "confidence": self.confidence,
            "needs_human_confirmation": self.needs_human_confirmation,
            "stated": self.stated,
            "purpose": self.purpose,
            "stated_by": self.stated_by,
            "come_sooner_if": self.come_sooner_if,
            "warnings": self.warnings,
        }

    def summary(self) -> str:
        """One-line description for logs and CLI output."""
        if self.type == SCHEDULED and self.due_earliest:
            window = self.due_earliest
            if self.due_latest and self.due_latest != self.due_earliest:
                window = f"{self.due_earliest} to {self.due_latest}"
            label = f" ({self.window_label})" if self.window_label else ""
            flag = " [confirm]" if self.needs_human_confirmation else ""
            return f"due {window}{label}{flag}"
        if self.type == CONDITIONAL_ONLY:
            return f"no fixed date; {len(self.come_sooner_if)} return trigger(s)"
        return "not discussed on this call"
