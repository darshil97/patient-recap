"""The layer registry: what each step is called and which template it uses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class LayerSpec:
    """One step of the chain."""

    number: int
    name: str
    template: str


LAYERS: Dict[int, LayerSpec] = {
    1: LayerSpec(1, "clinical_extraction", "layer_1_clinical_extraction.md"),
    2: LayerSpec(2, "homeopathic_analysis", "layer_2_homeopathic_analysis.md"),
    3: LayerSpec(3, "care_plan", "layer_3_care_plan.md"),
}

FIRST_LAYER = min(LAYERS)
LAST_LAYER = max(LAYERS)
