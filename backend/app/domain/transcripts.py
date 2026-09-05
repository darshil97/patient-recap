"""
Reading transcript files.

Transcripts are plain UTF-8 text, one file per visit, named by visit id.

Transcript paths arrive from API payloads, so every read is confined to the
configured transcripts directory. Without that guard a payload of
`../../etc/passwd` would be honoured, and there is no auth in front of this API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Union

from app.core.errors import TranscriptError

TRANSCRIPT_SUFFIX = ".txt"


@dataclass
class Transcript:
    """A loaded consultation transcript.

    Text only. The seeded JSON files also carried `patient_name` and `disease`,
    but neither is needed: the pipeline renders the name from the database
    (`PatientContext.name`), and `disease` was only ever a prop for generating
    the synthetic transcripts and must not reach the doctor.
    """

    path: Path
    relative_path: str
    text: str

    @property
    def character_count(self) -> int:
        return len(self.text)


def normalise_path(path: Union[str, Path], base_dir: Path) -> str:
    """Render a path in the 'transcripts/<name>' form stored on visits."""
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(base_dir).as_posix()
    except (ValueError, OSError):
        return candidate.as_posix()


def resolve_within(path: Union[str, Path], allowed_dir: Path) -> Path:
    """Resolve a client-supplied path, refusing anything outside allowed_dir.

    Both sides are fully resolved before comparison so `..` segments and
    symlinks cannot be used to escape.
    """
    allowed = allowed_dir.resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        # Paths are stored as 'transcripts/<visit_id>.txt', i.e. relative to the
        # project root, so strip a leading directory that already names the
        # allowed dir before joining.
        parts = candidate.parts
        if parts and parts[0] == allowed.name:
            candidate = Path(*parts[1:]) if len(parts) > 1 else Path()
        candidate = allowed / candidate

    resolved = candidate.resolve()
    if resolved != allowed and allowed not in resolved.parents:
        raise TranscriptError(
            "Transcript path resolves outside the allowed transcripts directory.",
            details={"requested": str(path), "allowed_dir": str(allowed)},
        )
    return resolved


def decode_transcript_bytes(raw: bytes, label: str) -> str:
    """Decode transcript bytes as UTF-8, tolerating a Windows byte-order mark.

    utf-8-sig is tried *first* on purpose. A BOM is itself valid UTF-8, so
    decoding as plain utf-8 succeeds and silently leaves a U+FEFF at the start
    of the text — which would then be sent to the model as part of the
    transcript. utf-8-sig strips the BOM when present and behaves exactly like
    utf-8 when it is not.

    Anything that is not UTF-8 is rejected rather than mangled with
    errors='replace': a corrupted transcript would reach the model and produce a
    plausible-looking but wrong clinical record, which is worse than a clear
    failure.

    Shared with the upload endpoint so the API and the loader cannot disagree
    about what counts as a readable transcript.
    """
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue

    raise TranscriptError(
        f"{label}: not valid UTF-8. Re-save the file as UTF-8 plain text.")


def _read_text(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TranscriptError(f"{path.name}: could not be read ({exc})") from exc
    return decode_transcript_bytes(raw, path.name)


def load_transcript(path: Union[str, Path], allowed_dir: Path) -> Transcript:
    """Load and validate a transcript.

    The wrong-suffix message names the fix, because a stale `.json` path is the
    one way this fails after the migration to text transcripts.
    """
    resolved = resolve_within(path, allowed_dir)
    if not resolved.is_file():
        raise TranscriptError(f"Transcript not found: {path}",
                              details={"requested": str(path)})

    if resolved.suffix.lower() != TRANSCRIPT_SUFFIX:
        raise TranscriptError(
            f"{resolved.name}: transcripts must be {TRANSCRIPT_SUFFIX}. "
            f"Run cli.convert_transcripts if this is legacy seed data.",
            details={"requested": str(path), "suffix": resolved.suffix})

    text = _read_text(resolved).strip()
    if not text:
        raise TranscriptError(f"{resolved.name}: file is empty")

    return Transcript(
        path=resolved,
        relative_path=normalise_path(resolved, allowed_dir.resolve().parent),
        text=text,
    )
