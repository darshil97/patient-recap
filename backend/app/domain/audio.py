"""
Which recordings are accepted, and how an upload is split into chunks.

Pure. Shared by the upload service (to validate a file before any bytes move),
the audio store (to validate a recording's key suffix), and the speech-to-text
call (for the Content-Type it sends).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Optional

from app.core.errors import TranscriptError, TranscriptTooLargeError

# Extension -> the Content-Type a recording is stored with and sent to Deepgram.
# Derived from the extension, never from the browser's Content-Type, so no
# client-supplied value reaches a storage object or an outbound header. All are
# formats Deepgram decodes; it sniffs the container regardless.
AUDIO_CONTENT_TYPES: Dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
}


def display_filename(client_filename: Optional[str]) -> Optional[str]:
    """Keep only the base name, for display. Never used to build a key."""
    if not client_filename:
        return None
    name = Path(client_filename.replace("\\", "/")).name.strip()
    return name or None


def audio_extension(filename: Optional[str]) -> str:
    """The accepted extension of a recording's filename, or a TranscriptError
    naming the formats that are accepted."""
    display = display_filename(filename) or "upload"
    suffix = Path(display).suffix.lower()
    if suffix not in AUDIO_CONTENT_TYPES:
        accepted = ", ".join(sorted(AUDIO_CONTENT_TYPES))
        raise TranscriptError(
            f"Recordings must be one of {accepted}. Got {suffix or 'no extension'}.",
            details={"filename": display, "suffix": suffix,
                     "accepted": sorted(AUDIO_CONTENT_TYPES)})
    return suffix


def content_type_for(extension: str) -> str:
    return AUDIO_CONTENT_TYPES[extension]


def check_audio_size(size: int, limit: int) -> None:
    if size <= 0:
        raise TranscriptError("The recording is empty.")
    if size > limit:
        raise TranscriptTooLargeError(
            f"Recording is {size:,} bytes; the limit is {limit:,}.",
            details={"size_bytes": size, "max_bytes": limit})


def chunk_count(total_bytes: int, chunk_bytes: int) -> int:
    """How many chunks cover the file. Mirrors the table's CHECK constraint."""
    return math.ceil(total_bytes / chunk_bytes)


def expected_chunk_size(index: int, total_bytes: int, chunk_bytes: int) -> int:
    """The exact size chunk `index` must have.

    Every chunk but the last is exactly `chunk_bytes`; the last is the remainder.
    Insisting on exact sizes is what makes "every chunk arrived" equivalent to
    "the stitched file is byte-for-byte the original".
    """
    count = chunk_count(total_bytes, chunk_bytes)
    if index < count - 1:
        return chunk_bytes
    return total_bytes - chunk_bytes * (count - 1)
