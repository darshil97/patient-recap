"""
Transcript storage: where the .txt actually lives.

A new I/O edge, alongside `db` and `llm`. `services` is still the only place the
edges meet, and `pipeline` must never import this — it receives an already-loaded
`Transcript` and stays I/O-free (§7).

WHY THIS EXISTS AS AN ABSTRACTION RATHER THAN A REWRITE

`visits.transcript_provider` has been in the schema since 5_api_support.sql,
carrying the value 'local' on every row. It was put there in anticipation of
exactly this change, so the move to object storage is a second implementation
behind an existing seam rather than a replacement of the first one. That matters
for two reasons:

  * **Nothing has to migrate atomically.** Each visit is read back through the
    provider it names, so local and object rows coexist indefinitely. The
    migration can run in batches, be interrupted, and resume.
  * **Tests and a bare checkout keep working.** The local backend has no
    dependencies and no credentials, so `python -m unittest` stays offline (§10)
    and a fresh clone runs without a Neon storage credential.

WHAT IS DELIBERATELY THE SAME IN BOTH BACKENDS

The locator. A visit's `transcript_path` is `transcripts/{visit_id}.txt` whether
it is a filesystem path or an object key, so `transcript_provider` is the scheme
and `transcript_path` is the address. Keeping the string identical means the
migration flips one column and rewrites no paths, and `get_by_transcript_path`
(used by `cli.load_insights --transcript`) keeps working against both.

It also carries §8.19 across unchanged: the locator is derived from the visit id
and **never** from the client's filename, which is attacker controlled. The
doctor's own filename stays in `visits.transcript_filename` for display only.
"""

from __future__ import annotations

from app.storage.base import (
    TRANSCRIPT_KEY_PATTERN,
    StorageError,
    StoredTranscript,
    TranscriptStore,
    transcript_locator,
    validate_locator,
)
from app.storage.factory import (
    LOCAL,
    NEON_OBJECT_STORAGE,
    PROVIDERS,
    default_provider,
    get_store,
    store_for_visit,
)
from app.storage.local import LocalTranscriptStore
from app.storage.object_store import ObjectTranscriptStore

__all__ = [
    "LOCAL",
    "NEON_OBJECT_STORAGE",
    "PROVIDERS",
    "TRANSCRIPT_KEY_PATTERN",
    "LocalTranscriptStore",
    "ObjectTranscriptStore",
    "StorageError",
    "StoredTranscript",
    "TranscriptStore",
    "default_provider",
    "get_store",
    "store_for_visit",
    "transcript_locator",
    "validate_locator",
]
