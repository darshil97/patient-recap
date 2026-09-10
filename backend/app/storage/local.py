"""
The filesystem backend: backend/transcripts/{visit_id}.txt.

Unchanged behaviour from before object storage existed, moved behind the store
interface. It keeps the traversal guard from app/domain/transcripts.py in addition
to validate_locator: the pattern check proves the locator has the right *shape*,
`resolve_within` proves the resolved path is actually inside the transcripts
directory. Two different questions, and on a filesystem both are worth asking —
symlinks and case-insensitive volumes can make a well-shaped relative path land
somewhere unexpected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.errors import NotFoundError
from app.domain.transcripts import decode_transcript_bytes, resolve_within
from app.storage.base import (
    StorageError,
    StoredTranscript,
    validate_locator,
)

logger = logging.getLogger(__name__)

PROVIDER = "local"


class LocalTranscriptStore:
    """Transcripts on the local disk, under `root`."""

    provider = PROVIDER

    def __init__(self, root: Path) -> None:
        #: The transcripts directory, e.g. backend/transcripts.
        self.root = Path(root)

    # -- helpers ----------------------------------------------------------- #

    def _path(self, locator: str) -> Path:
        """Resolve a locator to an absolute path, confined to the root.

        The locator is `transcripts/<uuid>.txt` and `root` is already the
        transcripts directory, so only the file name is joined on — otherwise the
        directory name would appear twice.
        """
        validate_locator(locator)
        filename = locator.split("/", 1)[1]
        return resolve_within(filename, self.root)

    # -- the contract ------------------------------------------------------ #

    def write(self, locator: str, text: str) -> StoredTranscript:
        target = self._path(locator)
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" so a Windows upload does not become CRLF on disk.
        target.write_text(text, encoding="utf-8", newline="\n")
        stat = target.stat()
        logger.info("Wrote transcript %s (%d chars, local)", locator, len(text))
        return StoredTranscript(
            locator=locator,
            text=text,
            provider=self.provider,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def read(self, locator: str) -> StoredTranscript:
        target = self._path(locator)
        if not target.is_file():
            raise NotFoundError(
                f"No transcript stored at {locator}.",
                details={"locator": locator, "provider": self.provider},
            )
        raw = target.read_bytes()
        # Same decoder the upload path uses, so a BOM is stripped identically on
        # the way out as on the way in (§8.19).
        text = decode_transcript_bytes(raw, target.name)
        stat = target.stat()
        return StoredTranscript(
            locator=locator,
            text=text,
            provider=self.provider,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        )

    def delete(self, locator: str) -> bool:
        target = self._path(locator)
        if not target.is_file():
            return False
        target.unlink()
        logger.info("Deleted transcript %s (local)", locator)
        return True

    def exists(self, locator: str) -> bool:
        try:
            return self._path(locator).is_file()
        except StorageError:
            # A malformed locator does not exist, and saying so is more useful to
            # a caller doing an existence check than raising at it.
            return False

    def describe(self) -> str:  # pragma: no cover - diagnostics
        return f"local:{self.root}"
