"""
Classifying Gemini failures.

Three kinds, because each needs a different response. The middle one is the
reason this module exists: an invalid key is a problem with the *key*, not the
request, so it must not abort a run that other keys could finish.
"""

from __future__ import annotations

from typing import Dict, Optional

QUOTA = "quota"
BAD_KEY = "bad_key"
REQUEST = "request"

# 429 / 503 / transient. The key may work again later, so it is only skipped.
_QUOTA_MARKERS = (
    "429", "resource_exhausted", "resourceexhausted", "quota",
    "rate limit", "rate-limit", "503", "unavailable",
    "deadline exceeded", "deadline_exceeded", "overloaded",
)

# The key itself is bad: no point trying it again this run.
_BAD_KEY_MARKERS = (
    "api key not valid", "api_key_invalid", "invalid api key",
    "permission_denied", "permissiondenied", "401", "unauthenticated",
    "api key expired", "consumer_suspended",
)


class AllKeysExhausted(RuntimeError):
    """Every configured key failed. Carries the last underlying error."""

    def __init__(self, message: str, last_error: Optional[BaseException] = None,
                 status: Optional[Dict[str, str]] = None) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.status = status or {}


def classify_error(error: BaseException) -> str:
    """Return QUOTA, BAD_KEY, or REQUEST."""
    text = str(error).lower()
    # Checked first: a 400 naming an invalid key is a key problem, not a
    # malformed-request problem, and both mention 400.
    if any(marker in text for marker in _BAD_KEY_MARKERS):
        return BAD_KEY
    if any(marker in text for marker in _QUOTA_MARKERS):
        return QUOTA
    return REQUEST


def is_quota_failure(error: BaseException) -> bool:
    """True if this failure is a rate limit, including a wrapped AllKeysExhausted."""
    if isinstance(error, AllKeysExhausted):
        return True
    return classify_error(error) == QUOTA
