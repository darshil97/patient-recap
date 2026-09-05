"""
Running the three-layer chain.

Layer 1 extracts what was said, Layer 2 interprets it, Layer 3 writes the visit
brief. Each layer consumes the previous layer's parsed output, so a run is three
sequential model calls.

No database access here by design: this module takes a transcript plus patient
facts and returns a PipelineResult. Persistence is the service layer's job.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from langchain_core.messages import HumanMessage

from app.core.errors import LayerOutputError, PipelineError
from app.domain.follow_up.resolver import resolve_follow_up
from app.domain.transcripts import Transcript
from app.llm.gemini import ChatClient
from app.pipeline.layers import LAYERS, LayerSpec
from app.pipeline.parsing import extract_json, response_text
from app.pipeline.prompts import build_jinja_env, render_prompt
from app.pipeline.results import LayerResult, PatientContext, PipelineResult

logger = logging.getLogger(__name__)

# Called with a dict describing progress so callers can render it live.
# Events: layer_start, layer_done, follow_up.
ProgressFn = Callable[[Dict[str, Any]], None]


def _emit(progress: Optional[ProgressFn], **event: Any) -> None:
    """Report progress, never letting a reporting bug break the run."""
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # pragma: no cover - reporting must not be fatal
        logger.debug("progress callback raised", exc_info=True)


def invoke_model(llm: ChatClient, prompt: str) -> Any:
    """Send a rendered prompt to the model.

    Plain HumanMessage on purpose: a LangChain ChatPromptTemplate would read the
    `{` in the prompts' JSON output-format blocks as template variables and
    raise. Jinja has already done the templating.
    """
    return llm.invoke([HumanMessage(content=prompt)])


def build_contexts(
    transcript: Transcript,
    patient: PatientContext,
) -> Dict[int, Dict[str, Any]]:
    """Per-layer Jinja context, minus the chained and derived values.

    `layer_1_output`, `layer_2_output` and `follow_up_resolved` are added by
    run_pipeline once the layers they come from have run.

    The transcript's `disease` is deliberately absent: Layer 1 must extract only
    what was said, uninfluenced by a confirmed diagnosis.
    """
    shared = {
        "patient_name": patient.name,
        "age": str(patient.age),
        "gender": patient.gender,
    }
    return {
        1: {**shared, "call_date": patient.call_date, "transcript_text": transcript.text},
        2: {**shared},
        3: {**shared, "call_date": patient.call_date},
    }


def run_layer(
    spec: LayerSpec,
    context: Dict[str, Any],
    llm: ChatClient,
    prompts_dir: Path,
    strict_json: bool = True,
    env: Any = None,
) -> LayerResult:
    """Render one layer's prompt, call the model, and parse the reply."""
    prompt = render_prompt(spec.template, context, prompts_dir, env=env)
    logger.debug("Layer %d (%s): sending %d chars", spec.number, spec.name, len(prompt))

    started = time.monotonic()
    message = invoke_model(llm, prompt)
    elapsed = time.monotonic() - started

    result = LayerResult(
        spec=spec,
        prompt=prompt,
        raw_response=response_text(message),
        data=None,
        usage=getattr(message, "usage_metadata", None),
        metadata=getattr(message, "response_metadata", None),
        seconds=elapsed,
    )
    result.data = extract_json(result.raw_response)

    if result.data is None:
        message_text = f"Layer {spec.number} ({spec.name}) did not return parseable JSON."
        if strict_json:
            # Downstream layers embed this output inside a ```json block, so
            # continuing with unparseable text would silently corrupt them.
            raise LayerOutputError(message_text, raw_response=result.raw_response)
        logger.warning("%s Passing raw text downstream.", message_text)

    return result


def run_pipeline(
    transcript: Transcript,
    patient: PatientContext,
    llm: ChatClient,
    prompts_dir: Path,
    stop_after: int = 3,
    strict_json: bool = True,
    progress: Optional[ProgressFn] = None,
) -> PipelineResult:
    """Run the chain end to end for one transcript.

    Args:
        transcript: an already-loaded transcript (see app.domain.transcripts).
        patient: name, age, gender and the real consultation date.
        llm: any client exposing `.invoke()`; normally the multi-key Gemini one.
        stop_after: highest layer to run, for iterating on an early prompt
            without paying for the whole chain.
        strict_json: raise if a layer's reply isn't parseable JSON.
        progress: optional callback receiving dict events.

    Returns:
        PipelineResult with each layer's prompt, reply, parsed data and token
        usage, plus `follow_up` resolved to calendar dates.
    """
    if stop_after not in LAYERS:
        raise PipelineError(f"stop_after must be one of {sorted(LAYERS)}, got {stop_after}")

    contexts = build_contexts(transcript, patient)
    env = build_jinja_env(prompts_dir)  # built once, reused across layers

    result = PipelineResult(
        patient=patient,
        model=str(getattr(llm, "model", "unknown")),
        transcript_path=transcript.relative_path,
    )

    for number in range(1, stop_after + 1):
        spec = LAYERS[number]
        context = dict(contexts[number])

        # Chain: each layer receives the parsed output of the ones before it.
        if number >= 2:
            context["layer_1_output"] = result.layers[1].as_prompt_input()
        if number >= 3:
            context["layer_2_output"] = result.layers[2].as_prompt_input()
            context["follow_up_resolved"] = json.dumps(
                result.follow_up.to_dict() if result.follow_up else {},
                indent=2, ensure_ascii=False)

        _emit(progress, event="layer_start", number=number, name=spec.name)

        result.layers[number] = run_layer(
            spec, context, llm, prompts_dir,
            strict_json=strict_json, env=env)

        _emit(progress, event="layer_done", number=number, name=spec.name,
              seconds=result.layers[number].seconds,
              prompt_chars=len(result.layers[number].prompt),
              usage=result.layers[number].usage or {})

        # Resolved as soon as Layer 1 lands, so Layer 3 receives calendar dates
        # instead of being asked to do the arithmetic itself.
        if number == 1:
            result.follow_up = resolve_follow_up(
                result.layers[1].data, patient.call_date)
            for warning in result.follow_up.warnings:
                logger.warning("Follow-up: %s", warning)
            _emit(progress, event="follow_up",
                  summary=result.follow_up.summary(),
                  due_earliest=result.follow_up.due_earliest,
                  follow_up_type=result.follow_up.type,
                  warnings=list(result.follow_up.warnings))

    return result
