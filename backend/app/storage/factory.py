"""
Choosing a backend.

Two entry points, and the distinction between them is the whole point:

    default_provider()      where a NEW upload goes      — from settings
    store_for_visit(visit)  where an EXISTING one lives   — from the visit row

Reads must never consult the setting. A visit stored on local disk has to stay
readable after `transcripts.storage` flips to object storage, or switching the
setting would silently break every brief generated before the switch. The row is
the authority for where its own bytes are; the setting only decides where the next
ones go.

`visits.transcript_provider` is NULL when no transcript is attached, and older
rows may carry the string 'local' — both resolve to the local backend, so nothing
needs backfilling before the object backend can be turned on.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Optional

from app.config import get_settings
from app.storage.base import StorageError, TranscriptStore
from app.storage.local import PROVIDER as LOCAL
from app.storage.local import LocalTranscriptStore
from app.storage.object_store import PROVIDER as NEON_OBJECT_STORAGE
from app.storage.object_store import ObjectTranscriptStore, build_s3_client

logger = logging.getLogger(__name__)

PROVIDERS = (LOCAL, NEON_OBJECT_STORAGE)


def default_provider() -> str:
    """The provider new uploads are written to."""
    return get_settings().transcripts.storage


@lru_cache(maxsize=None)
def _object_store() -> ObjectTranscriptStore:
    """The object-storage backend, built once per process.

    Cached because a boto3 client is relatively expensive to construct and is
    thread-safe to share. Clear with `get_store.cache_clear()` in tests.
    """
    config = get_settings().transcripts.object_storage
    if not config.configured:
        missing = [
            name for name, value in (
                ("AWS_ENDPOINT_URL_S3", config.endpoint_url),
                ("AWS_ACCESS_KEY_ID", config.access_key_id),
                ("AWS_SECRET_ACCESS_KEY", config.secret_access_key),
                ("transcripts.object_storage.bucket", config.bucket),
            ) if not value
        ]
        # Named explicitly, because the alternative is a NoCredentialsError from
        # inside botocore that says nothing about which variable is absent.
        raise StorageError(
            "Neon Object Storage is selected but not configured. Missing: "
            + ", ".join(missing)
            + ". Create a credential with storage:read and storage:write on this "
              "branch, or set transcripts.storage to local in config.yaml.",
            details={"missing": missing},
        )

    client = build_s3_client(
        endpoint_url=str(config.endpoint_url),
        access_key_id=str(config.access_key_id),
        secret_access_key=str(config.secret_access_key),
        region=config.region,
    )
    return ObjectTranscriptStore(bucket=config.bucket, client=client)


@lru_cache(maxsize=None)
def _local_store() -> LocalTranscriptStore:
    return LocalTranscriptStore(root=get_settings().transcripts.local_dir)


def get_store(provider: Optional[str] = None) -> TranscriptStore:
    """The backend for `provider`, defaulting to the configured one.

    An unknown provider raises rather than falling back. A row naming a backend
    this build does not have is a real problem — most likely a downgrade, or a
    database restored from a newer deployment — and quietly reading the local disk
    instead would either 404 confusingly or, if a stale file happened to be there,
    serve the wrong version of a clinical record.
    """
    name = (provider or default_provider() or LOCAL).strip().lower()

    if name == LOCAL:
        return _local_store()
    if name == NEON_OBJECT_STORAGE:
        return _object_store()

    raise StorageError(
        f"Unknown transcript provider {provider!r}. Known providers: "
        f"{', '.join(PROVIDERS)}.",
        details={"provider": provider, "known": list(PROVIDERS)},
    )


def store_for_visit(visit: Any) -> TranscriptStore:
    """The backend holding *this visit's* transcript.

    Reads the provider off the row, never from settings — see the module
    docstring. A visit with no transcript attached has provider NULL and gets the
    local backend, which is harmless: the caller is about to find there is nothing
    to read either way.
    """
    return get_store(getattr(visit, "transcript_provider", None) or LOCAL)


def reset_stores() -> None:
    """Drop the cached backends. For tests, and after a settings change."""
    _object_store.cache_clear()
    _local_store.cache_clear()
