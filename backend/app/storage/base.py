"""
The transcript storage contract, and the one rule both backends must obey.

`validate_locator` is the load-bearing part of this module. Every read, write and
delete in both backends runs its locator through it first, so there is a single
place that decides what a transcript address may look like — rather than a
filesystem guard in one backend and an implicit assumption in the other.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol, runtime_checkable

from app.core.errors import TranscriptError
from app.domain.transcripts import TRANSCRIPT_SUFFIX

# transcripts/<uuid>.txt and nothing else.
#
# Both backends derive the locator from a visit id that FastAPI has already
# validated as a UUID (§8.15), so this is belt-and-braces — but it is cheap
# belt-and-braces guarding the one place attacker-controlled text has historically
# tried to get in. For the local backend a stray '../' would escape the
# transcripts directory; for object storage it would write to an unexpected key,
# and S3 has no notion of a parent directory to be confined to, so there is no
# equivalent of resolve_within() to catch it later. This pattern is that catch.
TRANSCRIPT_KEY_PATTERN = re.compile(
    r"^transcripts/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"\.txt$"
)

TRANSCRIPT_PREFIX = "transcripts"

# TRANSCRIPT_SUFFIX is imported from app.domain.transcripts rather than declared
# again here. It was defined in both, which is one definition too many for a
# value that decides what the upload endpoint accepts *and* what both storage
# backends will write: the two could disagree, and the failure would be a
# transcript accepted by validation and then refused by the store. The direction
# of the import is the one the layering allows — storage may depend on domain,
# never the reverse (§7). Re-exported by the import above, so callers that
# already read it from here are unaffected.


class StorageError(TranscriptError):
    """The transcript backend could not be reached, or refused the operation.

    Subclasses TranscriptError so app/api/errors.py already maps it, and so the
    CLI's existing `except AppError` handling catches it unchanged.
    """

    code = "storage_error"


@dataclass
class StoredTranscript:
    """A transcript read back out of a store."""

    locator: str
    text: str
    provider: str
    size_bytes: Optional[int] = None
    modified_at: Optional[datetime] = None

    @property
    def character_count(self) -> int:
        return len(self.text)


def transcript_locator(visit_id: str) -> str:
    """The one and only address for a visit's transcript.

    Identical for both backends, which is what lets the migration flip
    `transcript_provider` without rewriting `transcript_path`.
    """
    return f"{TRANSCRIPT_PREFIX}/{visit_id}{TRANSCRIPT_SUFFIX}"


def validate_locator(locator: str) -> str:
    """Refuse anything that is not a transcript address we generated.

    Raises rather than sanitising. Sanitising invites a follow-up question about
    whether the sanitiser is complete; refusing does not.
    """
    candidate = (locator or "").strip()
    if not TRANSCRIPT_KEY_PATTERN.match(candidate):
        raise StorageError(
            f"Refusing to use {locator!r} as a transcript location. Expected "
            f"{TRANSCRIPT_PREFIX}/<visit-uuid>{TRANSCRIPT_SUFFIX}.",
            details={"locator": locator},
        )
    return candidate


@runtime_checkable
class TranscriptStore(Protocol):
    """What a transcript backend has to be able to do.

    Deliberately small. Everything above this layer — validation, the
    completed-visit rule, the insights-exist rule — is provider-independent and
    stays in transcript_service, so adding a third backend later means
    implementing four methods and nothing else.
    """

    #: Value written to visits.transcript_provider by this backend.
    provider: str

    def write(self, locator: str, text: str) -> StoredTranscript:
        """Store `text`, overwriting any existing object at `locator`."""
        ...

    def read(self, locator: str) -> StoredTranscript:
        """Fetch the text. Raises NotFoundError when the locator holds nothing."""
        ...

    def delete(self, locator: str) -> bool:
        """Remove it. True if something was removed, False if it was already gone."""
        ...

    def exists(self, locator: str) -> bool:
        """Whether anything is stored at `locator`."""
        ...
