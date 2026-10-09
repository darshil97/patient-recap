"""
Diarized speech, and turning it into the transcript format the pipeline reads.

Pure and I/O-free. An audio upload arrives here as a list of `Utterance`s from the
speech-to-text service, each tagged with an anonymous voice number. The LLM then
decides which voice is the doctor, the patient, or someone else (app/pipeline/
speakers.py), and `format_transcript` writes the result in the same shape as every
uploaded `.txt`:

    DOCTOR: Hello Jayesh, please have a seat. What brings you in today?
    PATIENT: I've been getting severe headaches over the past few months.
    OTHER (patient's wife): He has been waking up with them, too.

The split matters. The model only ever names a role per voice; it never rewrites
what was said. Every word in the stored transcript comes from the speech-to-text
output, so a confused model can mislabel a voice but cannot invent a symptom.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

DOCTOR = "doctor"
PATIENT = "patient"
OTHER = "other"
ROLES = (DOCTOR, PATIENT, OTHER)

HIGH, MEDIUM, LOW = "high", "medium", "low"
CONFIDENCES = (HIGH, MEDIUM, LOW)

# Long enough for "patient's mother" or "clinic receptionist", short enough that a
# label stays a label. Anything longer is the model writing prose.
MAX_DESCRIPTION_CHARS = 40

UNIDENTIFIED = "unidentified"

_WHITESPACE = re.compile(r"\s+")
# Characters that would break the `LABEL: text` line format or nest parentheses.
_LABEL_UNSAFE = re.compile(r"[():\[\]{}<>\r\n\t]")


@dataclass(frozen=True)
class Utterance:
    """One stretch of speech by one anonymous voice."""

    speaker: int
    text: str
    start: float = 0.0
    end: float = 0.0


@dataclass(frozen=True)
class DiarizedAudio:
    """What the speech-to-text service returned, reduced to what we use."""

    utterances: Tuple[Utterance, ...]
    duration_seconds: Optional[float] = None
    model: Optional[str] = None
    request_id: Optional[str] = None

    @property
    def speakers(self) -> List[int]:
        return sorted({u.speaker for u in self.utterances})


@dataclass(frozen=True)
class SpeakerRole:
    """Who one anonymous voice is."""

    speaker: int
    role: str
    description: Optional[str] = None
    confidence: str = MEDIUM

    @property
    def label(self) -> str:
        return label_for(self.role, self.description)


def clean_text(text: str) -> str:
    """One line per turn: collapse every run of whitespace, newlines included."""
    return _WHITESPACE.sub(" ", text or "").strip()


def clean_description(value: object) -> Optional[str]:
    """A model-supplied description made safe to put inside a label.

    Model output, so it is treated as untrusted text: anything that could end the
    label early (a colon) or start a new line is removed, and it is capped.
    """
    if not isinstance(value, str):
        return None
    text = clean_text(_LABEL_UNSAFE.sub(" ", value))
    if len(text) > MAX_DESCRIPTION_CHARS:
        text = text[:MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0].rstrip(" ,.;-")
    return text or None


def label_for(role: str, description: Optional[str] = None) -> str:
    """The line prefix a role is written with.

    DOCTOR and PATIENT match the existing transcripts exactly, which is what the
    Layer 1 prompt was written against. Anyone else is OTHER, with a description
    when the model could give one, so a parent speaking for a child reads as
    `OTHER (patient's mother)` rather than being folded into PATIENT.
    """
    if role == DOCTOR:
        return "DOCTOR"
    if role == PATIENT:
        return "PATIENT"
    return f"OTHER ({description})" if description else "OTHER"


def merge_turns(utterances: Sequence[Utterance]) -> List[Utterance]:
    """Join consecutive utterances by the same voice into one turn.

    The service splits on pauses, so one answer often arrives as several
    utterances. Blank utterances are dropped.
    """
    turns: List[Utterance] = []
    for utterance in sorted(utterances, key=lambda u: (u.start, u.end)):
        text = clean_text(utterance.text)
        if not text:
            continue
        if turns and turns[-1].speaker == utterance.speaker:
            last = turns[-1]
            turns[-1] = Utterance(last.speaker, f"{last.text} {text}",
                                  last.start, utterance.end)
        else:
            turns.append(Utterance(utterance.speaker, text,
                                   utterance.start, utterance.end))
    return turns


def render_anonymous(turns: Sequence[Utterance]) -> str:
    """The transcript with anonymous voice labels, for the identification prompt."""
    return "\n".join(f"Speaker {t.speaker}: {t.text}" for t in turns)


def turn_counts(turns: Sequence[Utterance]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for turn in turns:
        counts[turn.speaker] = counts.get(turn.speaker, 0) + 1
    return counts


def format_transcript(turns: Sequence[Utterance],
                      roles: Mapping[int, SpeakerRole]) -> str:
    """Write the turns as `LABEL: text` lines, one turn per line.

    Consecutive turns that end up with the same label are merged. That is how a
    voice the service split in two — common when someone moves nearer the
    microphone — becomes one speaker again once the model maps both halves to the
    same person.

    A voice missing from `roles` is written as OTHER (unidentified) rather than
    raising: the caller has already warned about it, and dropping its lines would
    silently remove part of the consultation.
    """
    lines: List[Tuple[str, str]] = []
    for turn in turns:
        role = roles.get(turn.speaker)
        label = role.label if role else label_for(OTHER, UNIDENTIFIED)
        if lines and lines[-1][0] == label:
            lines[-1] = (label, f"{lines[-1][1]} {turn.text}")
        else:
            lines.append((label, turn.text))
    return "\n".join(f"{label}: {text}" for label, text in lines)
