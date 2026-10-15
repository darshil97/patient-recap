"""
Turn a DOCTOR:/PATIENT: transcript into a two-voice audio file, for testing
recording uploads.

    python -m cli.synthesize_transcript archive/shalin_1.txt
    python -m cli.synthesize_transcript archive/shalin_1.txt -o /tmp/shalin_1.wav \
        --doctor-voice aura-2-orpheus-en --patient-voice aura-2-luna-en
    python -m cli.synthesize_transcript archive/shalin_1.txt --dry-run

Each turn is spoken by Deepgram's Aura text-to-speech in the voice for its
label, with a pause between turns, and everything is written as one mono 16-bit
WAV. Lines labelled anything other than DOCTOR or PATIENT (e.g. `OTHER (patient's
mother):`) get a third voice; a line with no label continues the previous turn.

Uses DEEPGRAM_API_KEY and speech.deepgram.base_url, the same as transcription.
Text-to-speech is billed per character; `--dry-run` prints the count without
calling anything.

Why WAV: Deepgram returns raw PCM for `encoding=linear16&container=none`, and
raw PCM joins cleanly — no re-encoding and no ffmpeg. It is also large (about
2.9 MB per minute at 24 kHz), so a full consultation is well over the 4.5 MB
request limit and exercises the chunked upload path.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import httpx

from app.config import get_settings

SPEAK_PATH = "/v1/speak"
DEFAULT_DOCTOR_VOICE = "aura-2-orpheus-en"   # masculine, clear, professional
DEFAULT_PATIENT_VOICE = "aura-2-luna-en"     # feminine, friendly, natural
DEFAULT_OTHER_VOICE = "aura-2-hyperion-en"   # masculine, Australian: distinct from both

# Kept well under the per-request text limit; long turns are split at sentences.
MAX_REQUEST_CHARS = 1800
BYTES_PER_SAMPLE = 2  # linear16
MAX_ATTEMPTS = 4

_LINE = re.compile(r"^\s*([A-Z][A-Z ]*?)\s*(?:\(([^)]*)\))?\s*:\s*(.*)$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Turn:
    label: str          # DOCTOR, PATIENT, or OTHER (description)
    role: str           # doctor | patient | other
    text: str


def parse_transcript(text: str) -> List[Turn]:
    """Lines starting `LABEL:` begin a turn; other non-blank lines continue it."""
    turns: List[Turn] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _LINE.match(line)
        if match:
            name, description, spoken = match.groups()
            name = name.strip()
            role = name.lower() if name in ("DOCTOR", "PATIENT") else "other"
            label = f"{name} ({description})" if description else name
            turns.append(Turn(label=label, role=role, text=spoken.strip()))
        elif turns:
            turns[-1].text = f"{turns[-1].text} {line}".strip()
        else:
            raise ValueError(f"The first line has no speaker label: {line[:60]!r}")
    return [t for t in turns if t.text]


def split_for_request(text: str, limit: int = MAX_REQUEST_CHARS) -> List[str]:
    """Pieces under `limit`, broken at sentence ends (or words, if a sentence is
    longer than the limit on its own)."""
    if len(text) <= limit:
        return [text]
    pieces: List[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def silence(seconds: float, sample_rate: int) -> bytes:
    return b"\x00" * (int(seconds * sample_rate) * BYTES_PER_SAMPLE)


def speak(client: httpx.Client, base_url: str, api_key: str, voice: str,
          text: str, sample_rate: int) -> bytes:
    """One text-to-speech request, returning raw 16-bit mono PCM."""
    params = {"model": voice, "encoding": "linear16", "container": "none",
              "sample_rate": str(sample_rate)}
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.post(f"{base_url}{SPEAK_PATH}", params=params,
                                   headers=headers, json={"text": text})
        except httpx.TransportError as exc:
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f"Could not reach Deepgram: {exc}") from exc
            time.sleep(2 ** attempt)
            continue
        if response.status_code == 200:
            audio = response.content
            return audio[: len(audio) - len(audio) % BYTES_PER_SAMPLE]
        if (response.status_code == 429 or response.status_code >= 500) and attempt < MAX_ATTEMPTS:
            time.sleep(2 ** attempt)
            continue
        try:
            detail = response.json().get("err_msg") or response.text
        except ValueError:
            detail = response.text
        if response.status_code in (401, 403):
            detail = f"{detail} (check DEEPGRAM_API_KEY in backend/.env)"
        raise RuntimeError(f"Deepgram text-to-speech failed ({response.status_code}): "
                           f"{str(detail)[:300]}")
    raise RuntimeError("unreachable")  # pragma: no cover


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize a DOCTOR:/PATIENT: transcript into a two-voice WAV "
                    "with Deepgram text-to-speech.")
    parser.add_argument("transcript", type=Path, help="Path to the .txt transcript")
    parser.add_argument("-o", "--output", type=Path,
                        help="Output .wav (default: next to the transcript)")
    parser.add_argument("--doctor-voice", default=DEFAULT_DOCTOR_VOICE)
    parser.add_argument("--patient-voice", default=DEFAULT_PATIENT_VOICE)
    parser.add_argument("--other-voice", default=DEFAULT_OTHER_VOICE,
                        help="For any label other than DOCTOR or PATIENT")
    parser.add_argument("--sample-rate", type=int, default=24000,
                        choices=(8000, 16000, 24000, 32000, 48000))
    parser.add_argument("--pause", type=float, default=0.6,
                        help="Seconds of silence between turns (default 0.6)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the turns and character count; call nothing")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.transcript.is_file():
        print(f"No such file: {args.transcript}")
        return 1

    try:
        turns = parse_transcript(args.transcript.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if not turns:
        print("The transcript has no spoken lines.")
        return 1

    voices = {"doctor": args.doctor_voice, "patient": args.patient_voice,
              "other": args.other_voice}
    chars = sum(len(t.text) for t in turns)
    speakers: dict = {}
    for turn in turns:
        count, _ = speakers.get(turn.label, (0, turn.role))
        speakers[turn.label] = (count + 1, turn.role)
    print(f"{len(turns)} turns, {chars:,} characters: "
          + ", ".join(f"{label} x{count} -> {voices[role]}"
                      for label, (count, role) in speakers.items()))
    if args.dry_run:
        return 0

    settings = get_settings().speech.deepgram
    api_key = (settings.api_key or "").strip()
    if not api_key:
        print("DEEPGRAM_API_KEY is not set in backend/.env.")
        return 1

    output = args.output or args.transcript.with_suffix(".wav")
    gap = silence(args.pause, args.sample_rate)
    breath = silence(0.15, args.sample_rate)
    frames = bytearray()

    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        for number, turn in enumerate(turns, start=1):
            print(f"  [{number:>3}/{len(turns)}] {turn.label:<10} {len(turn.text):>5} chars",
                  flush=True)
            for index, piece in enumerate(split_for_request(turn.text)):
                if index:
                    frames += breath
                try:
                    frames += speak(client, settings.base_url, api_key,
                                    voices[turn.role], piece, args.sample_rate)
                except RuntimeError as exc:
                    print(f"Error: {exc}")
                    return 1
            frames += gap

    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(BYTES_PER_SAMPLE)
        wav.setframerate(args.sample_rate)
        wav.writeframes(bytes(frames))

    seconds = len(frames) / (BYTES_PER_SAMPLE * args.sample_rate)
    size_mb = output.stat().st_size / 1_000_000
    print(f"Wrote {output} — {seconds / 60:.1f} min of audio, {size_mb:.1f} MB "
          f"({time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
