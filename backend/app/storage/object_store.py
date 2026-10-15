"""
The Neon Object Storage backend.

Neon Object Storage is S3-compatible, so this is boto3 pointed at a Neon *branch*
endpoint. No AWS account is involved; the credential is a Neon credential whose
`token_id` and `s3_secret_access_key` are handed to the SDK as an access key pair.

TWO THINGS ABOUT THIS THAT ARE NOT OBVIOUS

**Buckets branch with the database.** A child branch inherits the parent's buckets
*and the objects in them at fork time*, copy-on-write, then diverges. That is the
real reason to prefer this over a filesystem: a preview branch of this project now
gets a consistent pair — the visit rows and the transcripts they point at — and
uploading or deleting on the branch cannot affect production. With transcripts on
local disk, branching the database produced visit rows whose `transcript_path`
pointed at files that either did not exist or, worse, were the *live* ones shared
by every branch.

**The credential is branch-scoped, and valid for descendants only.** A credential
minted on `production` works on `production` and anything forked from it, and is
rejected on a branch outside that lineage. So this is not a global secret, and a
sibling branch needs its own.

boto3 is imported lazily, inside the constructor, so the local backend and the
whole test suite keep working in a checkout that never installed it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from app.core.errors import NotFoundError
from app.domain.transcripts import decode_transcript_bytes
from app.storage.base import (
    StorageError,
    StoredTranscript,
    validate_locator,
)

logger = logging.getLogger(__name__)

PROVIDER = "neon_object_storage"

# S3 error codes that mean "there is nothing there", as opposed to a real failure.
_MISSING_CODES = {"NoSuchKey", "NotFound", "404"}


def build_s3_client(
    endpoint_url: str,
    access_key_id: str,
    secret_access_key: str,
    region: str = "us-east-2",
) -> Any:
    """A boto3 S3 client aimed at a Neon branch endpoint.

    `addressing_style="path"` is required: Neon's endpoint does not serve
    virtual-host-style bucket addressing, so the default would build
    `https://bucket.br-....storage...` and fail to resolve.
    """
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise StorageError(
            "boto3 is required for Neon Object Storage but is not installed. "
            "Run `pip install -r requirements.txt`, or set transcripts.storage to "
            "local in config.yaml.",
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
        config=Config(
            s3={"addressing_style": "path"},
            signature_version="s3v4",
            # Transcripts are 3-5 KB; a hung request should fail fast and let the
            # caller see a real error rather than stall an upload or a generation.
            connect_timeout=10,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class ObjectTranscriptStore:
    """Transcripts as objects in a Neon Object Storage bucket."""

    provider = PROVIDER

    def __init__(self, bucket: str, client: Any) -> None:
        """`client` is injected rather than built here.

        That is what makes this class testable offline: the suite passes a fake
        implementing the four boto3 calls used below, so there is coverage of the
        error mapping and the key handling without a network or a credential.
        """
        self.bucket = bucket
        self.client = client

    # -- helpers ----------------------------------------------------------- #

    @staticmethod
    def _is_missing(exc: Exception) -> bool:
        """Whether a botocore ClientError means 'no such object'.

        Read defensively off the response dict rather than importing ClientError,
        so a stubbed client in the tests can raise a lookalike.
        """
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return False
        error = response.get("Error") or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        return str(error.get("Code")) in _MISSING_CODES or status == 404

    def _wrap(self, exc: Exception, action: str, locator: str) -> StorageError:
        response = getattr(exc, "response", None)
        code = None
        if isinstance(response, dict):
            code = (response.get("Error") or {}).get("Code")
        return StorageError(
            f"Could not {action} the transcript in object storage: {exc}",
            details={"locator": locator, "bucket": self.bucket,
                     "provider": self.provider, "s3_code": code},
        )

    # -- the contract ------------------------------------------------------ #

    def write(self, locator: str, text: str) -> StoredTranscript:
        key = validate_locator(locator)
        body = text.encode("utf-8")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="text/plain; charset=utf-8",
            )
        except Exception as exc:
            raise self._wrap(exc, "store", locator) from exc

        logger.info("Wrote transcript %s (%d chars, %s/%s)",
                    key, len(text), self.provider, self.bucket)
        return StoredTranscript(locator=key, text=text, provider=self.provider,
                                size_bytes=len(body))

    def read(self, locator: str) -> StoredTranscript:
        key = validate_locator(locator)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            raw = response["Body"].read()
        except Exception as exc:
            if self._is_missing(exc):
                raise NotFoundError(
                    f"No transcript stored at {locator}.",
                    details={"locator": locator, "provider": self.provider},
                ) from exc
            raise self._wrap(exc, "read", locator) from exc

        # Same decoder as the upload path and the local backend, so a
        # byte-order mark is handled identically wherever the bytes came from.
        text = decode_transcript_bytes(raw, key)
        modified = response.get("LastModified")
        return StoredTranscript(
            locator=key,
            text=text,
            provider=self.provider,
            size_bytes=len(raw),
            modified_at=modified if isinstance(modified, datetime) else None,
        )

    def delete(self, locator: str) -> bool:
        key = validate_locator(locator)
        # Checked first because S3 DeleteObject is idempotent and reports success
        # for a key that never existed. The caller uses the return value to tell
        # "removed" from "was already gone", which matters when the migration is
        # reconciling the two backends.
        if not self.exists(key):
            return False
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise self._wrap(exc, "delete", locator) from exc
        logger.info("Deleted transcript %s (%s)", key, self.provider)
        return True

    def exists(self, locator: str) -> bool:
        try:
            key = validate_locator(locator)
        except StorageError:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            if self._is_missing(exc):
                return False
            raise self._wrap(exc, "check", locator) from exc

    def describe(self) -> str:  # pragma: no cover - diagnostics
        return f"{self.provider}:{self.bucket}"
