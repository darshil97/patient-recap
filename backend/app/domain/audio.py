"""
Which recordings are accepted, and how an upload is split into chunks.

Pure. Shared by the upload service (to validate a file before any bytes move),
the audio store (to validate a recording's key suffix), and the speech-to-text
call (for the Content-Type it sends).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Mapping, Optional, Tuple

from app.core.errors import (
    ConflictError,
    TranscriptError,
    TranscriptTooLargeError,
    ValidationError,
)

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


# --------------------------------------------------------------------------- #
# Live recordings (§8.24): sent while recording, so the plan comes last
# --------------------------------------------------------------------------- #

def check_live_chunk(index: int, size: int, chunk_bytes: int, limit: int) -> None:
    """What can be checked about a live chunk before the recording has ended.

    Which chunk is the last is not known yet, so the exact-size rule cannot be
    applied here; live_plan applies it at finish. What can be refused now is a
    chunk that no plan could contain, and a recording that has outgrown the
    ceiling. Every earlier chunk is `chunk_bytes`, so the recording's size after
    this one is `index * chunk_bytes + size` without counting anything.
    """
    if size <= 0:
        raise ValidationError(f"Chunk {index} is empty.",
                              details={"index": index, "size_bytes": size})
    if size > chunk_bytes:
        raise ValidationError(
            f"Chunk {index} is {size:,} bytes; live recording chunks are at most "
            f"{chunk_bytes:,}.",
            details={"index": index, "size_bytes": size, "max_bytes": chunk_bytes})
    if index * chunk_bytes + size > limit:
        raise TranscriptTooLargeError(
            f"This recording has reached the {limit:,}-byte limit. End the session "
            f"to process what has been recorded.",
            details={"index": index, "max_bytes": limit})


def live_plan(sizes: Mapping[int, int], chunk_bytes: int,
              declared_count: Optional[int] = None) -> Tuple[int, int]:
    """(total_bytes, chunk_count) of a live recording, from the chunks that arrived.

    The same rule a file upload's plan imposes, checked after the fact: chunks
    0..n-1 all present, every one but the last exactly `chunk_bytes`. Holding it
    is what lets the stitched recording be verified byte-for-byte like any other.

    `declared_count` is how many chunks the recorder says it sent. Without it --
    an interrupted session, whose recorder is gone -- the chunks that arrived are
    the recording.
    """
    if not sizes:
        raise TranscriptError("Nothing was recorded: no audio reached the server.")

    count = max(sizes) + 1
    if declared_count is not None:
        if count > declared_count:
            raise ConflictError(
                f"The server holds {count} chunks of this recording, but the "
                f"recorder says it sent {declared_count}.",
                details={"chunk_count": declared_count, "chunks_received": count})
        count = declared_count

    missing = [i for i in range(count) if i not in sizes]
    if missing:
        raise ConflictError(
            f"{len(missing)} of {count} chunks have not arrived yet.",
            details={"missing": missing[:100]})

    short = [i for i in range(count - 1) if sizes[i] != chunk_bytes]
    if short:
        first = short[0]
        raise ValidationError(
            f"Chunk {first} is {sizes[first]:,} bytes; every chunk but the last "
            f"must be exactly {chunk_bytes:,}.",
            details={"short": short[:100], "chunk_bytes": chunk_bytes})

    last = sizes[count - 1]
    if not 0 < last <= chunk_bytes:
        raise ValidationError(
            f"The last chunk is {last:,} bytes; it must be 1 to {chunk_bytes:,}.",
            details={"index": count - 1, "size_bytes": last})
    return chunk_bytes * (count - 1) + last, count
