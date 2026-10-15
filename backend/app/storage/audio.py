"""
Binary storage for audio: upload chunks while they arrive, and the stitched
recording once they have.

A sibling of the transcript store rather than an extension of it. Transcripts
are text with one fixed address per visit; audio is bytes, streamed, and has two
kinds of key:

    audio-chunks/<upload_id>/<index:06d>.part        transient, deleted on stitch
    recordings/<visit_id>/<upload_id><extension>      the visit's recording

The recording key carries the upload id so a replacement is written beside the
current recording rather than over it. If the new upload then fails, the visit
still has the recording its transcript came from; the old one is removed only
once the new one has completed (see audio_upload_service).

Same rules as app/storage/base.py: every key is generated here from ids FastAPI
has already validated as UUIDs, and every read, write and delete re-checks it
against a strict pattern, because object storage has no parent directory to be
confined to. The client's filename never reaches a key.

Which backend holds an upload is recorded on its row (`storage_provider`), and
new uploads follow `transcripts.storage`, so a visit's audio sits next to its
transcript.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Optional, Protocol

from app.config import get_settings
from app.core.errors import AudioStorageError
from app.domain.audio import AUDIO_CONTENT_TYPES
from app.storage.factory import LOCAL, NEON_OBJECT_STORAGE, PROVIDERS, get_store

logger = logging.getLogger(__name__)

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_EXTENSIONS = "|".join(re.escape(ext.lstrip(".")) for ext in sorted(AUDIO_CONTENT_TYPES))

CHUNK_KEY_PATTERN = re.compile(rf"^audio-chunks/{_UUID}/\d{{6}}\.part$")
RECORDING_KEY_PATTERN = re.compile(rf"^recordings/{_UUID}/{_UUID}\.(?:{_EXTENSIONS})$")

CHUNK_CONTENT_TYPE = "application/octet-stream"
COPY_BUFFER_BYTES = 1024 * 1024
_MISSING_CODES = {"NoSuchKey", "NotFound", "404"}


def chunk_locator(upload_id: str, index: int) -> str:
    return f"audio-chunks/{upload_id}/{index:06d}.part"


def recording_locator(visit_id: str, upload_id: str, extension: str) -> str:
    return f"recordings/{visit_id}/{upload_id}{extension}"


def validate_audio_locator(locator: str) -> str:
    """Refuse anything that is not an audio key this module generated."""
    candidate = (locator or "").strip()
    if not (CHUNK_KEY_PATTERN.match(candidate) or RECORDING_KEY_PATTERN.match(candidate)):
        raise AudioStorageError(
            f"Refusing to use {locator!r} as an audio location.",
            details={"locator": locator})
    return candidate


class AudioStore(Protocol):
    """What an audio backend has to be able to do."""

    provider: str

    def write_bytes(self, locator: str, data: bytes, content_type: str) -> None: ...

    def write_file(self, locator: str, source: BinaryIO, size: int,
                   content_type: str) -> None: ...

    def open(self, locator: str) -> Any:
        """A context manager yielding a binary stream with `.read(n)`."""
        ...

    def size(self, locator: str) -> Optional[int]:
        """Stored size in bytes, or None when nothing is there."""
        ...

    def delete(self, locator: str) -> None: ...


# --------------------------------------------------------------------------- #
# Local disk
# --------------------------------------------------------------------------- #

class LocalAudioStore:
    """Audio under `root` (backend/audio by default)."""

    provider = LOCAL

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, locator: str) -> Path:
        key = validate_audio_locator(locator)
        root = self.root.resolve()
        path = (root / key).resolve()
        # The pattern proves the shape; this proves where it landed, which on a
        # filesystem a symlink could otherwise change.
        if root not in path.parents:
            raise AudioStorageError("Audio path resolves outside the audio directory.",
                                    details={"locator": locator})
        return path

    def _write(self, locator: str, writer: Any) -> None:
        target = self._path(locator)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write beside, then rename: a crash mid-write leaves a temp file, never a
        # truncated chunk or recording under the real name.
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                writer(handle)
            os.replace(tmp, target)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise AudioStorageError(f"Could not store audio: {exc}",
                                    details={"locator": locator}) from exc

    def write_bytes(self, locator: str, data: bytes, content_type: str) -> None:
        self._write(locator, lambda handle: handle.write(data))

    def write_file(self, locator: str, source: BinaryIO, size: int,
                   content_type: str) -> None:
        def copy(handle: BinaryIO) -> None:
            while True:
                block = source.read(COPY_BUFFER_BYTES)
                if not block:
                    return
                handle.write(block)
        self._write(locator, copy)

    @contextmanager
    def open(self, locator: str) -> Iterator[BinaryIO]:
        path = self._path(locator)
        try:
            handle = path.open("rb")
        except OSError as exc:
            raise AudioStorageError(f"Could not read audio: {exc}",
                                    details={"locator": locator}) from exc
        with handle:
            yield handle

    def size(self, locator: str) -> Optional[int]:
        path = self._path(locator)
        return path.stat().st_size if path.is_file() else None

    def delete(self, locator: str) -> None:
        self._path(locator).unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Neon Object Storage
# --------------------------------------------------------------------------- #

class ObjectAudioStore:
    """Audio as objects in the transcripts bucket, through the same S3 client."""

    provider = NEON_OBJECT_STORAGE

    def __init__(self, bucket: str, client: Any) -> None:
        self.bucket = bucket
        self.client = client

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error") or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return str(error.get("Code")) in _MISSING_CODES or status == 404

    def _fail(self, exc: Exception, action: str, locator: str) -> AudioStorageError:
        return AudioStorageError(
            f"Could not {action} audio in object storage: {exc}",
            details={"locator": locator, "bucket": self.bucket})

    def write_bytes(self, locator: str, data: bytes, content_type: str) -> None:
        key = validate_audio_locator(locator)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                   ContentType=content_type)
        except Exception as exc:
            raise self._fail(exc, "store", key) from exc

    def write_file(self, locator: str, source: BinaryIO, size: int,
                   content_type: str) -> None:
        # One PUT from a seekable file, rather than boto3's managed multipart
        # transfer: it does not depend on the S3-compatible endpoint supporting
        # multipart uploads, and a seekable body lets botocore retry it.
        key = validate_audio_locator(locator)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=source,
                                   ContentLength=size, ContentType=content_type)
        except Exception as exc:
            raise self._fail(exc, "store", key) from exc

    @contextmanager
    def open(self, locator: str) -> Iterator[BinaryIO]:
        key = validate_audio_locator(locator)
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as exc:
            raise self._fail(exc, "read", key) from exc
        try:
            yield body
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()

    def size(self, locator: str) -> Optional[int]:
        key = validate_audio_locator(locator)
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_missing(exc):
                return None
            raise self._fail(exc, "check", key) from exc
        length = head.get("ContentLength")
        return int(length) if length is not None else None

    def delete(self, locator: str) -> None:
        key = validate_audio_locator(locator)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._fail(exc, "delete", key) from exc


# --------------------------------------------------------------------------- #
# Choosing one
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=None)
def _local() -> LocalAudioStore:
    return LocalAudioStore(get_settings().speech.local_dir)


@lru_cache(maxsize=None)
def _object() -> ObjectAudioStore:
    # Reuses the transcript store's client, which is where the "not configured"
    # error naming the missing variables is raised.
    transcript_store = get_store(NEON_OBJECT_STORAGE)
    return ObjectAudioStore(bucket=get_settings().transcripts.object_storage.bucket,
                            client=transcript_store.client)  # type: ignore[attr-defined]


def get_audio_store(provider: str) -> AudioStore:
    """The audio backend for a row's `storage_provider`. Unknown raises (§8.22)."""
    name = (provider or "").strip().lower()
    if name == LOCAL:
        return _local()
    if name == NEON_OBJECT_STORAGE:
        return _object()
    raise AudioStorageError(
        f"Unknown audio storage provider {provider!r}. Known providers: "
        f"{', '.join(PROVIDERS)}.",
        details={"provider": provider})


def reset_audio_stores() -> None:
    """Drop the cached backends. For tests, and after a settings change."""
    _local.cache_clear()
    _object.cache_clear()
