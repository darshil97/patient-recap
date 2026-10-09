"""
Classifying model failures, for Gemini and Bedrock alike.

Three kinds, because each needs a different response:

    QUOTA     throttled, out of quota, or the service is briefly unavailable.
              Worth retrying unchanged later, so it surfaces as HTTP 429.
    BAD_KEY   the credential is missing, wrong, expired, or not allowed to call
              this model. Retrying cannot help; the configuration has to change.
    REQUEST   anything else: a malformed request, an unknown model, a safety block.

Classification is by message text. Both providers' errors name their cause in
the message (Google's RESOURCE_EXHAUSTED, botocore's "(ThrottlingException)"),
and the providers' own exception types are not stable across library versions.
"""

from __future__ import annotations

QUOTA = "quota"
BAD_KEY = "bad_key"
REQUEST = "request"

# The key may work again shortly.
_QUOTA_MARKERS = (
    # Gemini
    "429", "resource_exhausted", "resourceexhausted", "quota",
    "rate limit", "rate-limit", "503", "unavailable",
    "deadline exceeded", "deadline_exceeded", "overloaded",
    # Bedrock (botocore puts the error code in the message)
    "throttlingexception", "too many requests", "serviceunavailableexception",
    "modelnotreadyexception", "modeltimeoutexception", "read timeout",
)

# The credential itself is the problem.
_BAD_KEY_MARKERS = (
    # Gemini
    "api key not valid", "api_key_invalid", "invalid api key",
    "permission_denied", "permissiondenied", "401", "unauthenticated",
    "api key expired", "consumer_suspended",
    # Bedrock
    "accessdeniedexception", "unrecognizedclientexception",
    "expiredtokenexception", "invalid api key format", "not authorized",
    "security token included in the request is invalid",
)


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
    """True if this failure is a rate limit or a transient outage."""
    return classify_error(error) == QUOTA
