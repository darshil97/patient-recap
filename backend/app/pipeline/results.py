"""What a pipeline run produces."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.domain.follow_up.models import ResolvedFollowUp
from app.pipeline.layers import LayerSpec


@dataclass
class LayerResult:
    """What one layer produced."""

    spec: LayerSpec
    prompt: str
    raw_response: str
    data: Any = None
    usage: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    seconds: Optional[float] = None

    @property
    def is_json(self) -> bool:
        return self.data is not None

    def as_prompt_input(self) -> str:
        """Serialize this layer's output for embedding in a downstream prompt."""
        if self.data is not None:
            return json.dumps(self.data, indent=2, ensure_ascii=False)
        return self.raw_response.strip()


@dataclass
class PatientContext:
    """The patient facts the prompts need.

    Sourced from the database, never hand-supplied alongside a transcript.
    `call_date` must be the real consultation date: it anchors every relative
    follow-up, so an approximate value silently produces a wrong due date.
    """

    name: str
    age: Any
    gender: str
    call_date: str


@dataclass
class PipelineResult:
    """A full run: every layer that executed, plus the resolved follow-up."""

    patient: PatientContext
    model: str
    transcript_path: str
    layers: Dict[int, LayerResult] = field(default_factory=dict)
    follow_up: Optional[ResolvedFollowUp] = None

    @property
    def layer_1(self) -> Optional[LayerResult]:
        return self.layers.get(1)

    @property
    def layer_2(self) -> Optional[LayerResult]:
        return self.layers.get(2)

    @property
    def layer_3(self) -> Optional[LayerResult]:
        return self.layers.get(3)

    def total_usage(self) -> Dict[str, int]:
        """Sum token counts across layers, for cost visibility."""
        totals: Dict[str, int] = {}
        for result in self.layers.values():
            for key, value in (result.usage or {}).items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
        return totals

    def model_version(self) -> Optional[str]:
        """The concrete model build, from response metadata if the API sent it."""
        for _, layer in sorted(self.layers.items()):
            meta = layer.metadata or {}
            for key in ("model_version", "modelVersion", "model_name"):
                if meta.get(key):
                    return str(meta[key])
        return None

    def payload(self) -> Dict[str, Any]:
        """Everything the run produced, for storage in insights_json."""
        out: Dict[str, Any] = {
            f"layer_{n}": (r.data if r.is_json else r.raw_response)
            for n, r in sorted(self.layers.items())
        }
        out["follow_up"] = self.follow_up.to_dict() if self.follow_up else None
        return out
