"""
Speaker identification: which anonymous voice is the doctor, the patient, or
someone else.

One model call, made before the three-layer chain and only for audio uploads.
The model sees the diarized transcript with `Speaker 0:`-style labels and answers
with a role per voice. It does **not** return transcript text: the stored
transcript is assembled in Python from the speech-to-text output
(app/domain/diarization.py), so the worst a bad answer can do is mislabel a
voice, and the checks below refuse the answers that would make the transcript
unusable.

Same rules as the rest of app/pipeline: no I/O of its own, any client with
`.invoke()` will do, and the prompt is a Jinja template sent as a plain
HumanMessage (§8.1).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.errors import LayerOutputError, TranscriptError
from app.domain.diarization import (
    CONFIDENCES,
    DOCTOR,
    LOW,
    MEDIUM,
    OTHER,
    PATIENT,
    ROLES,
    UNIDENTIFIED,
    SpeakerRole,
    Utterance,
    clean_description,
    clean_text,
    render_anonymous,
)
from app.llm.base import ChatClient, model_name
from app.pipeline.layers import LayerSpec
from app.pipeline.results import PatientContext
from app.pipeline.runner import run_layer

logger = logging.getLogger(__name__)

# Not in LAYERS: it is not part of the insight chain and must not be counted as a
# layer by anything iterating over it. Number 0 because it runs before Layer 1.
SPEAKER_IDENTIFICATION = LayerSpec(0, "speaker_identification",
                                   "speaker_identification.md")

MAX_WARNINGS = 5
MAX_WARNING_CHARS = 300

_DIGITS = re.compile(r"\d+")


@dataclass
class SpeakerIdentification:
    """The model's answer, validated."""

    roles: Dict[int, SpeakerRole]
    warnings: List[str] = field(default_factory=list)
    model: str = "unknown"
    usage: Optional[Dict[str, Any]] = None
    seconds: Optional[float] = None


def _speaker_id(value: Any) -> Optional[int]:
    """0, "0" and "Speaker 0" all name voice 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _DIGITS.search(value)
        return int(match.group()) if match else None
    return None


def _warnings_from(data: Any) -> List[str]:
    raw = data.get("warnings") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    warnings: List[str] = []
    for item in raw:
        text = clean_text(item) if isinstance(item, str) else ""
        if text:
            warnings.append(text[:MAX_WARNING_CHARS])
    return warnings[:MAX_WARNINGS]


def parse_speaker_roles(
    data: Any,
    speakers: Sequence[int],
    raw_response: str = "",
) -> Tuple[Dict[int, SpeakerRole], List[str]]:
    """Validate the model's answer into one role per voice, plus warnings.

    Lenient about form (a speaker id written as "Speaker 1", an unknown
    confidence word), strict about substance: an unknown role becomes OTHER, and
    a voice the model left out is labelled OTHER (unidentified) with a warning,
    rather than being guessed or having its lines dropped.

    Raises LayerOutputError when there is no `speakers` list at all.
    """
    entries = data.get("speakers") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise LayerOutputError(
            "Speaker identification did not return a `speakers` list.",
            raw_response=raw_response)

    expected = set(speakers)
    roles: Dict[int, SpeakerRole] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        speaker = _speaker_id(entry.get("speaker"))
        if speaker is None or speaker not in expected:
            logger.warning("Speaker identification named an unknown voice %r; ignored",
                           entry.get("speaker"))
            continue
        if speaker in roles:
            logger.warning("Speaker identification named voice %d twice; kept the "
                           "first answer", speaker)
            continue

        role = str(entry.get("role") or "").strip().lower()
        if role not in ROLES:
            logger.warning("Speaker identification gave voice %d the role %r; "
                           "labelled it other", speaker, role)
            role = OTHER
        confidence = str(entry.get("confidence") or "").strip().lower()
        if confidence not in CONFIDENCES:
            confidence = MEDIUM
        description = clean_description(entry.get("description")) if role == OTHER else None

        roles[speaker] = SpeakerRole(speaker=speaker, role=role,
                                     description=description, confidence=confidence)

    warnings: List[str] = []
    missing = sorted(expected - set(roles))
    for speaker in missing:
        roles[speaker] = SpeakerRole(speaker=speaker, role=OTHER,
                                     description=UNIDENTIFIED, confidence=LOW)
    if missing:
        warnings.append(
            "Some speech could not be attributed to anyone and is labelled "
            "OTHER (unidentified) in the transcript.")

    return roles, warnings


def check_roles(roles: Dict[int, SpeakerRole]) -> List[str]:
    """Refuse a labelling the pipeline cannot use; warn about a doubtful one.

    No doctor, or nobody but the doctor, means the conversation has not been
    separated — the voices were merged, or the model could not tell them apart.
    Storing that would hand Layer 1 a transcript where the patient's symptoms are
    attributed to the doctor, which produces a confident, wrong record (§8.19).

    No *patient* is allowed, with a warning: a parent speaking for a young child
    is a real consultation, and labelling the parent OTHER is accurate.
    """
    found = {role.role for role in roles.values()}
    if DOCTOR not in found:
        raise TranscriptError(
            "No voice in this recording could be identified as the doctor, so the "
            "transcript cannot be labelled reliably. Check the recording, or "
            "upload a text transcript instead.",
            details={"roles": sorted(found)})
    if found == {DOCTOR}:
        raise TranscriptError(
            "Every voice in this recording was identified as the doctor, so the "
            "patient's words cannot be told apart. Check the recording, or upload "
            "a text transcript instead.",
            details={"roles": sorted(found)})

    warnings: List[str] = []
    if PATIENT not in found:
        others = sorted({r.description for r in roles.values()
                         if r.role == OTHER and r.description
                         and r.description != UNIDENTIFIED})
        who = f" ({', '.join(others)})" if others else ""
        warnings.append(
            "No voice was identified as the patient. Speech by others present"
            f"{who} is labelled OTHER.")

    for role in sorted(roles.values(), key=lambda r: r.speaker):
        if role.confidence == LOW and role.description != UNIDENTIFIED:
            warnings.append(f"{role.label} was identified with low confidence.")
    return warnings


def identify_speakers(
    turns: Sequence[Utterance],
    patient: PatientContext,
    llm: ChatClient,
    prompts_dir: Path,
    doctor_name: Optional[str] = None,
) -> SpeakerIdentification:
    """Ask the model who each voice is, and validate the answer.

    Raises LayerOutputError for an unparseable answer and TranscriptError when the
    answer shows the voices were not separated (see check_roles).
    """
    speakers = sorted({t.speaker for t in turns})
    context = {
        "patient_name": patient.name,
        "age": str(patient.age),
        "gender": patient.gender,
        "doctor_name": (doctor_name or "").strip() or "Not recorded",
        "speaker_ids": ", ".join(f"Speaker {s}" for s in speakers),
        "transcript_text": render_anonymous(turns),
    }

    result = run_layer(SPEAKER_IDENTIFICATION, context, llm, prompts_dir,
                       strict_json=True)
    roles, warnings = parse_speaker_roles(result.data, speakers,
                                          raw_response=result.raw_response)
    warnings.extend(check_roles(roles))
    # The model's own warnings last: ours are about whether the labels can be
    # trusted at all, theirs are about details.
    for warning in _warnings_from(result.data):
        if warning not in warnings:
            warnings.append(warning)

    return SpeakerIdentification(
        roles=roles,
        warnings=warnings[:MAX_WARNINGS + 3],
        model=model_name(llm),
        usage=result.usage,
        seconds=result.seconds,
    )
