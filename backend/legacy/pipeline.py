"""
End-to-end 3-layer homeopathic consultation pipeline.

    transcript.json
        -> Layer 1  clinical extraction
        -> Layer 2  homeopathic analysis   (consumes Layer 1 JSON)
        -> Layer 3  actionable care plan   (consumes Layer 1 + Layer 2 JSON)

Prompts live in prompts/ as Jinja2 templates. The model is Gemini via LangChain.

Typical use:

    from pipeline import PatientInfo, run_pipeline

    result = run_pipeline(
        "transcripts/riya_1.json",
        PatientInfo(age=28, gender="Female", call_date="2026-08-22"),
    )
    print(result.layer_3.data)
    print(result.follow_up.summary())   # -> 'due 2026-09-12 (3 weeks)'

The next visit is extracted from the transcript by Layer 1 and resolved to a
calendar date in Python by resolve_follow_up(), so it is never a manual input
and the model never does date arithmetic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import (
    Any, Callable, Dict, List, Optional, Protocol, Tuple, Union,
)

from dateutil.relativedelta import relativedelta
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from langchain_core.messages import HumanMessage

from gemini_llm import ChatGoogleGenerativeAIWithRetry, load_api_keys

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = BASE_DIR / "prompts"

DEFAULT_MODEL = "gemini-3.7-flash"

Scalar = Union[str, int, float]


class ChatClient(Protocol):
    """The bit of a chat model the pipeline actually uses.

    Typed as a protocol so the multi-key wrapper, a bare ChatGoogleGenerativeAI,
    or a test stub are all acceptable.
    """

    model: str

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...


# Called with a dict describing what the pipeline is doing, so a caller can
# render live progress. Events: layer_start, layer_done, follow_up.
ProgressFn = Callable[[Dict[str, Any]], None]


def _emit(progress: Optional[ProgressFn], **event: Any) -> None:
    """Report progress, never letting a reporting bug break the run."""
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # pragma: no cover - reporting must not be fatal
        logger.debug("progress callback raised", exc_info=True)


# --------------------------------------------------------------------------- #
# Layer definitions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LayerSpec:
    """One step of the chain: a prompt template and the variables it needs."""

    number: int
    name: str
    template: str


LAYERS: Dict[int, LayerSpec] = {
    1: LayerSpec(1, "clinical_extraction", "layer_1_clinical_extraction.md"),
    2: LayerSpec(2, "homeopathic_analysis", "layer_2_homeopathic_analysis.md"),
    3: LayerSpec(3, "care_plan", "layer_3_care_plan.md"),
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #

class PipelineError(Exception):
    """Base error for this pipeline."""


class TranscriptError(PipelineError):
    """The transcript file is missing, malformed, or empty."""


class LayerOutputError(PipelineError):
    """A layer returned something that could not be used by the next layer."""

    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

@dataclass
class PatientInfo:
    """Patient metadata the prompts ask for but the transcript files don't store.

    `name` is optional: when omitted, the transcript's own `patient_name` is used.
    Values are stringified at render time, so an int age is fine.

    `call_date` must be the real consultation date (YYYY-MM-DD), not today's date:
    it is the anchor every relative follow-up ("in three weeks") is resolved
    against, so an approximate value silently produces a wrong due date.
    """

    age: Scalar
    gender: str
    call_date: str
    name: Optional[str] = None


@dataclass
class Transcript:
    """A loaded consultation transcript."""

    path: Path
    text: str
    patient_name: str


# --------------------------------------------------------------------------- #
# Follow-up resolution
#
# Layer 1 captures what the doctor said ("come back in three weeks") without
# doing any arithmetic. Turning that into a calendar date happens here, in
# Python, because date maths needs to be deterministic, auditable, and testable
# rather than left to the model.
# --------------------------------------------------------------------------- #

FOLLOW_UP_SCHEDULED = "scheduled"
FOLLOW_UP_CONDITIONAL = "conditional_only"
FOLLOW_UP_NONE = "not_discussed"

VALID_ACTIONS = {"contact_clinic", "record_and_report", "return_immediately"}
VALID_URGENCY = {"routine", "urgent"}

# Doctors speak in words ("three weeks"), so the model may pass a word through
# instead of a digit even though the schema asks for a number.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "a": 1, "an": 1, "couple": 2, "few": 3, "half": 0,
}

_UNIT_DELTA = {
    "day": "days",
    "week": "weeks",
    "month": "months",
    "year": "years",
}


@dataclass
class ResolvedFollowUp:
    """A Layer 1 follow-up block resolved against the consultation date."""

    type: str = FOLLOW_UP_NONE
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
        if self.type == FOLLOW_UP_SCHEDULED and self.due_earliest:
            window = self.due_earliest
            if self.due_latest and self.due_latest != self.due_earliest:
                window = f"{self.due_earliest} to {self.due_latest}"
            label = f" ({self.window_label})" if self.window_label else ""
            flag = " [confirm]" if self.needs_human_confirmation else ""
            return f"due {window}{label}{flag}"
        if self.type == FOLLOW_UP_CONDITIONAL:
            return f"no fixed date; {len(self.come_sooner_if)} return trigger(s)"
        return "not discussed on this call"


def _coerce_number(value: Any) -> Optional[float]:
    """Turn 3, '3', '3.0' or 'three' into a number. Returns None if hopeless."""
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
    """Accept 'week', 'weeks', 'Weeks' -> 'week'. Returns None if unrecognised."""
    if not isinstance(value, str):
        return None
    unit = value.strip().lower().rstrip("s")
    return unit if unit in _UNIT_DELTA else None


def _parse_date(value: Any) -> Optional[date]:
    """Parse an ISO date string. Deliberately strict: no guessing at formats."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _shift(anchor: date, amount: float, unit: str) -> date:
    """Add amount x unit to anchor using calendar-correct arithmetic.

    relativedelta is used rather than timedelta so that months land on the same
    day-of-month where possible instead of drifting by a fixed 30 days.
    """
    kwargs = {_UNIT_DELTA[unit]: int(round(amount))}
    return anchor + relativedelta(**kwargs)


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
        plural = "" if min_value == 1 else "s"
        body = f"{fmt(min_value)} {unit}{plural}"
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

        action = (item.get("action") or "contact_clinic")
        urgency = (item.get("urgency") or "routine")
        if action not in VALID_ACTIONS:
            warnings.append(f"unrecognised follow-up action: {action!r}")
        if urgency not in VALID_URGENCY:
            warnings.append(f"unrecognised follow-up urgency: {urgency!r}")

        condition = item.get("condition") or item.get("stated") or ""
        triggers.append({
            "stated": item.get("stated"),
            "condition": condition,
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

        # A range stays a range even if declared 'relative', and vice versa,
        # so the basis reflects the values rather than the model's label.
        basis = "range" if high != low else "relative"
        label = _label(low, high, unit, approximate)
        if anchor is None:
            return None, None, basis, label
        return _shift(anchor, low, unit), _shift(anchor, high, unit), basis, label

    if declared not in ("unspecified", "none", "null", ""):
        warnings.append(f"unrecognised timing.type: {declared!r}")
    return None, None, "unspecified", None


def resolve_follow_up(
    layer_1_data: Any,
    call_date: Optional[str],
) -> ResolvedFollowUp:
    """Resolve Layer 1's `follow_up` block into calendar dates.

    Tolerant by design: a missing or malformed block yields a
    `not_discussed` result with warnings attached rather than raising, so one
    bad extraction doesn't sink the whole run. Inspect `.warnings` to see what
    could not be resolved.
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
    if not isinstance(scheduled, dict):
        scheduled = {}

    timing = scheduled.get("timing")
    if not isinstance(timing, dict):
        timing = {}

    resolved.commitment = str(scheduled.get("commitment") or "not_discussed").strip().lower()
    resolved.stated = scheduled.get("stated") or None
    resolved.purpose = scheduled.get("purpose") or None
    resolved.stated_by = scheduled.get("stated_by") or None
    resolved.approximate = bool(timing.get("approximate"))
    resolved.come_sooner_if = _normalise_triggers(block.get("return_earlier_if"), warnings)

    # A follow-up spoken by the patient means the transcript's speaker labels are
    # probably swapped. Surface it instead of quietly trusting the attribution.
    if resolved.stated_by and resolved.stated_by.strip().lower() == "patient":
        warnings.append(
            "follow-up instruction is attributed to the patient; "
            "transcript speaker labels may be swapped"
        )

    earliest, latest, basis, label = _resolve_window(timing, anchor, warnings)
    resolved.basis = basis
    resolved.window_label = label

    if earliest is not None:
        resolved.type = FOLLOW_UP_SCHEDULED
        resolved.due_earliest = earliest.isoformat()
        resolved.due_latest = (latest or earliest).isoformat()
        if anchor is not None:
            resolved.days_from_call = (earliest - anchor).days
            if resolved.days_from_call < 0:
                warnings.append("resolved follow-up date falls before the call date")
    elif resolved.come_sooner_if:
        resolved.type = FOLLOW_UP_CONDITIONAL
    else:
        resolved.type = FOLLOW_UP_NONE

    if resolved.commitment == "definite" and resolved.type != FOLLOW_UP_SCHEDULED:
        warnings.append(
            "commitment was 'definite' but no usable timing was captured; "
            "the visit date needs confirming with the clinic"
        )
    if resolved.type == FOLLOW_UP_NONE and resolved.commitment == "not_discussed":
        warnings.append("next visit was not discussed on this call")

    # Confidence describes the resolved date only. A confidently-absent date
    # (conditional or not discussed) stays 'high'.
    if resolved.type == FOLLOW_UP_SCHEDULED:
        if resolved.basis == "range" or resolved.approximate:
            resolved.confidence = "medium"
        if any("unusable" in w or "unrecognised" in w or "swapped" in w for w in warnings):
            resolved.confidence = "low"
        resolved.needs_human_confirmation = resolved.confidence != "high"
    elif resolved.commitment in ("definite", "patient_discretion"):
        resolved.confidence = "low"
        resolved.needs_human_confirmation = True

    return resolved


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #

@dataclass
class LayerResult:
    """What one layer produced."""

    spec: LayerSpec
    prompt: str
    raw_response: str
    data: Any = None
    usage: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None

    @property
    def is_json(self) -> bool:
        return self.data is not None

    def as_prompt_input(self) -> str:
        """Serialize this layer's output for embedding in a downstream prompt."""
        if self.data is not None:
            return json.dumps(self.data, indent=2, ensure_ascii=False)
        return self.raw_response.strip()


@dataclass
class PipelineResult:
    """The full run: every layer that executed, in order."""

    transcript: Transcript
    patient: PatientInfo
    model: str
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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "transcript": self.transcript.path.name,
            "patient_name": self.transcript.patient_name,
            "call_date": self.patient.call_date,
            "model": self.model,
            "usage": self.total_usage(),
            "follow_up": self.follow_up.to_dict() if self.follow_up else None,
            "layers": {
                f"layer_{n}": {
                    "name": r.spec.name,
                    "output": r.data if r.is_json else r.raw_response,
                    "valid_json": r.is_json,
                }
                for n, r in sorted(self.layers.items())
            },
        }


# --------------------------------------------------------------------------- #
# Prompt rendering (Jinja2)
# --------------------------------------------------------------------------- #

def build_jinja_env(prompts_dir: Path = PROMPTS_DIR) -> Environment:
    """Jinja environment for the markdown prompt templates.

    autoescape stays off: these are plain-text prompts, not HTML, and escaping
    would corrupt quotes and angle brackets in the transcript.
    StrictUndefined makes a missing variable raise instead of silently rendering
    an empty string into a clinical prompt.
    """
    if not prompts_dir.is_dir():
        raise PipelineError(f"Prompts directory not found: {prompts_dir}")

    return Environment(
        loader=FileSystemLoader(str(prompts_dir)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_prompt(template_name: str, context: Dict[str, Any],
                  prompts_dir: Path = PROMPTS_DIR) -> str:
    """Render a prompt template from the prompts folder with the given context."""
    env = build_jinja_env(prompts_dir)
    return env.get_template(template_name).render(**context)


# --------------------------------------------------------------------------- #
# Transcript loading
# --------------------------------------------------------------------------- #

def load_transcript(path: Union[str, Path]) -> Transcript:
    """Load a transcript JSON file and validate the fields the prompts depend on.

    The file's `disease` field is deliberately NOT read into the prompt context:
    Layer 1's job is to capture only what was said in the call, and handing the
    model a confirmed diagnosis up front would bias that extraction.
    """
    path = Path(path)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.is_file():
        raise TranscriptError(f"Transcript not found: {path}")

    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise TranscriptError(f"{path.name}: invalid JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise TranscriptError(
            f"{path.name}: expected a JSON object, got {type(data).__name__}"
        )

    text = (data.get("transcript") or "").strip()
    if not text:
        raise TranscriptError(f"{path.name}: 'transcript' key is missing or empty")

    return Transcript(
        path=path,
        text=text,
        patient_name=(data.get("patient_name") or "").strip() or "Unknown",
    )


# --------------------------------------------------------------------------- #
# Model
# --------------------------------------------------------------------------- #

def build_llm(
    model: Optional[str] = None,
    temperature: float = 0.0,
    json_mode: bool = True,
    max_retries: int = 2,
    timeout: Optional[float] = 120.0,
    api_keys: Optional[List[str]] = None,
) -> ChatGoogleGenerativeAIWithRetry:
    """Create the Gemini client, backed by every API key that is configured.

    Free-tier keys are capped per day, so the client rotates across
    GOOGLE_API_KEY_1..N (falling back to a single GOOGLE_API_KEY) and moves to
    the next key when one is rate limited or rejected.

    json_mode sets response_mime_type=application/json so the API itself
    constrains the reply to JSON, which is what all three prompts ask for.
    """
    keys = api_keys if api_keys is not None else load_api_keys()
    if not keys:
        raise PipelineError(
            "No API key found. Set GOOGLE_API_KEY_1 (and optionally _2, _3), or "
            "a single GOOGLE_API_KEY, in your environment or .env "
            "(see .env.example)."
        )

    return ChatGoogleGenerativeAIWithRetry(
        model=model or os.getenv("GEMINI_MODEL", DEFAULT_MODEL),
        temperature=temperature,
        api_keys=keys,
        response_mime_type="application/json" if json_mode else None,
        max_retries=max_retries,
        timeout=timeout,
    )


def invoke_model(llm: ChatClient, prompt: str) -> Any:
    """Send a rendered prompt to the model.

    The prompt goes as a plain HumanMessage on purpose. A LangChain
    ChatPromptTemplate would read the `{` in the prompts' JSON output-format
    blocks as template variables and raise; Jinja already did the templating.
    """
    return llm.invoke([HumanMessage(content=prompt)])


def response_text(message: Any) -> str:
    """Flatten a response into plain text.

    Gemini 3.x models can return content as a list of blocks rather than a
    string, so `.text` is preferred over `.content`. In current langchain-core
    `.text` is a property returning a str subclass that is also callable for
    backwards compatibility, so the isinstance check must come before the
    callable check or accessing it emits a deprecation warning.
    """
    text = getattr(message, "text", None)
    if isinstance(text, str):
        return str(text)
    if callable(text):  # pragma: no cover - legacy langchain-core
        return str(text())

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort parse of a model reply into Python data.

    With json_mode on, the first attempt normally succeeds. The fenced-block and
    brace-slice fallbacks cover replies that arrive wrapped in markdown or with
    a stray preamble.
    """
    stripped = text.strip()
    if not stripped:
        return None

    candidates = [stripped]

    fenced = _FENCE_RE.search(stripped)
    if fenced:
        candidates.append(fenced.group(1))

    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = stripped.find(opener), stripped.rfind(closer)
        if 0 <= start < end:
            candidates.append(stripped[start:end + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


# --------------------------------------------------------------------------- #
# Layer execution
# --------------------------------------------------------------------------- #

def run_layer(
    spec: LayerSpec,
    context: Dict[str, Any],
    llm: ChatClient,
    prompts_dir: Path = PROMPTS_DIR,
    strict_json: bool = True,
) -> LayerResult:
    """Render one layer's prompt, call the model, and parse the reply."""
    prompt = render_prompt(spec.template, context, prompts_dir)
    logger.info("Layer %d (%s): sending %d chars", spec.number, spec.name, len(prompt))

    message = invoke_model(llm, prompt)
    raw = response_text(message)

    result = LayerResult(
        spec=spec,
        prompt=prompt,
        raw_response=raw,
        data=extract_json(raw),
        usage=getattr(message, "usage_metadata", None),
        metadata=getattr(message, "response_metadata", None),
    )

    if result.data is None:
        message_text = (
            f"Layer {spec.number} ({spec.name}) did not return parseable JSON."
        )
        if strict_json:
            # Downstream layers embed this output inside a ```json block, so
            # continuing with unparseable text would silently corrupt them.
            raise LayerOutputError(message_text, raw_response=raw)
        logger.warning("%s Passing raw text downstream.", message_text)

    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def build_contexts(
    transcript: Transcript,
    patient: PatientInfo,
) -> Dict[int, Dict[str, Any]]:
    """Build the per-layer Jinja context, minus the chained/derived values.

    `layer_1_output`, `layer_2_output` and `follow_up_resolved` are added by
    run_pipeline once the layers they come from have run.
    """
    name = patient.name or transcript.patient_name
    shared = {
        "patient_name": name,
        "age": str(patient.age),
        "gender": patient.gender,
    }

    return {
        1: {**shared, "call_date": patient.call_date, "transcript_text": transcript.text},
        2: {**shared},
        3: {**shared, "call_date": patient.call_date},
    }


def run_pipeline(
    transcript_path: Union[str, Path],
    patient: PatientInfo,
    llm: Optional[ChatClient] = None,
    model: Optional[str] = None,
    temperature: float = 0.0,
    stop_after: int = 3,
    strict_json: bool = True,
    prompts_dir: Path = PROMPTS_DIR,
    progress: Optional[ProgressFn] = None,
) -> PipelineResult:
    """Run the chain end to end for one transcript.

    Args:
        transcript_path: path to a transcript JSON file (absolute, or relative
            to this project directory).
        patient: metadata the transcripts don't carry (age, gender, call date,
            optional name override). `call_date` anchors follow-up resolution.
        llm: a prebuilt chat model. Pass one to reuse a client across calls;
            otherwise one is built from `model` / `temperature`.
        stop_after: highest layer to run (1, 2, or 3). Useful for iterating on
            an early prompt without paying for the whole chain.
        strict_json: raise if a layer's reply isn't parseable JSON. Turn off to
            forward the raw text to the next layer instead.
        progress: optional callback receiving dict events (layer_start,
            layer_done, follow_up) so callers can render live progress.

    Returns:
        PipelineResult holding each executed layer's prompt, raw reply, parsed
        data, and token usage, plus `follow_up` with the next visit resolved to
        calendar dates (populated once Layer 1 has run).
    """
    if stop_after not in LAYERS:
        raise ValueError(f"stop_after must be one of {sorted(LAYERS)}, got {stop_after}")

    transcript = load_transcript(transcript_path)
    contexts = build_contexts(transcript, patient)
    llm = llm or build_llm(model=model, temperature=temperature)

    result = PipelineResult(
        transcript=transcript,
        patient=patient,
        model=str(getattr(llm, "model", model or DEFAULT_MODEL)),
    )

    for number in range(1, stop_after + 1):
        context = dict(contexts[number])

        # Chain: each layer receives the parsed output of the ones before it.
        if number >= 2:
            context["layer_1_output"] = result.layers[1].as_prompt_input()
        if number >= 3:
            context["layer_2_output"] = result.layers[2].as_prompt_input()
            context["follow_up_resolved"] = json.dumps(
                result.follow_up.to_dict() if result.follow_up else {},
                indent=2, ensure_ascii=False,
            )

        spec = LAYERS[number]
        _emit(progress, event="layer_start", number=number, name=spec.name)
        started = time.monotonic()

        result.layers[number] = run_layer(
            spec, context, llm,
            prompts_dir=prompts_dir, strict_json=strict_json,
        )

        _emit(progress, event="layer_done", number=number, name=spec.name,
              seconds=time.monotonic() - started,
              prompt_chars=len(result.layers[number].prompt),
              usage=result.layers[number].usage or {})

        # Resolve the follow-up as soon as Layer 1 lands, so Layer 3 receives
        # calendar dates instead of being asked to do the arithmetic itself.
        if number == 1:
            result.follow_up = resolve_follow_up(
                result.layers[1].data, patient.call_date
            )
            logger.info("Follow-up: %s", result.follow_up.summary())
            for warning in result.follow_up.warnings:
                logger.warning("Follow-up: %s", warning)
            _emit(progress, event="follow_up",
                  summary=result.follow_up.summary(),
                  due_earliest=result.follow_up.due_earliest,
                  follow_up_type=result.follow_up.type,
                  warnings=list(result.follow_up.warnings))

    return result


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #

def save_result(
    result: PipelineResult,
    output_dir: Union[str, Path],
    include_prompts: bool = False,
) -> Path:
    """Write each layer's output (and optionally its prompt) under output_dir.

    Files land in `output_dir/<transcript stem>/`.
    """
    root = Path(output_dir)
    if not root.is_absolute():
        root = BASE_DIR / root
    run_dir = root / result.transcript.path.stem
    run_dir.mkdir(parents=True, exist_ok=True)

    for number, layer in sorted(result.layers.items()):
        stem = f"layer_{number}_{layer.spec.name}"
        if layer.is_json:
            (run_dir / f"{stem}.json").write_text(
                json.dumps(layer.data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        else:
            (run_dir / f"{stem}.txt").write_text(layer.raw_response, encoding="utf-8")
        if include_prompts:
            (run_dir / f"{stem}.prompt.md").write_text(layer.prompt, encoding="utf-8")

    (run_dir / "run.json").write_text(
        json.dumps(result.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return run_dir
