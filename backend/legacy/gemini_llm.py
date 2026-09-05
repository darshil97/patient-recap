"""
Gemini client that rotates across several API keys.

Free-tier keys are capped per day per model, so a long backfill will exhaust
one key partway through. This wrapper holds several keys and moves to the next
one when a key is rate limited or rejected, presenting the same `.invoke()`
surface the rest of the pipeline expects.

Set the keys in .env:

    GOOGLE_API_KEY_1=...
    GOOGLE_API_KEY_2=...
    GOOGLE_API_KEY_3=...

A single GOOGLE_API_KEY is used as a fallback if none of the numbered ones exist.

Errors are sorted into three kinds, because they need different responses:

    quota / transient   429, 503, deadline exceeded -> try the next key
    key rejected        invalid or unauthorised key -> disable that key, try next
    request problem     bad prompt, unknown model   -> raise, another key won't help

The distinction matters: an invalid key is a problem with the key, not the
request, so it must not abort a run that other keys could complete.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

logger = logging.getLogger(__name__)

MAX_KEYS = 8

# 429 / 503 / transient: the key may work again later, so it is only skipped.
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


def load_api_keys(max_keys: int = MAX_KEYS) -> List[str]:
    """Collect GOOGLE_API_KEY_1..N, falling back to a single GOOGLE_API_KEY.

    Duplicates are dropped: the same key twice would just fail twice.
    """
    found: List[str] = []
    for index in range(1, max_keys + 1):
        value = (os.getenv(f"GOOGLE_API_KEY_{index}") or "").strip()
        if value and value not in found:
            found.append(value)

    if not found:
        for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
            value = (os.getenv(name) or "").strip()
            if value and value not in found:
                found.append(value)
    return found


def classify_error(error: BaseException) -> str:
    """Return 'quota', 'bad_key', or 'request'."""
    text = str(error).lower()
    # Checked first: a 400 naming an invalid key is a key problem, not a request one.
    if any(marker in text for marker in _BAD_KEY_MARKERS):
        return "bad_key"
    if any(marker in text for marker in _QUOTA_MARKERS):
        return "quota"
    return "request"


@dataclass
class _KeyState:
    """Bookkeeping for one API key."""

    index: int
    key: str
    disabled: bool = False
    reason: str = ""
    successes: int = 0
    failures: int = 0

    @property
    def label(self) -> str:
        return f"key {self.index}"


class ChatGoogleGenerativeAIWithRetry:
    """ChatGoogleGenerativeAI that falls through to other API keys on failure.

    Rotation is sticky: once a key works, it keeps being used until it fails,
    rather than restarting from key 1 on every call. Without that, every request
    after key 1 is exhausted would waste a round trip rediscovering it.
    """

    def __init__(
        self,
        model: str,
        temperature: float = 0.0,
        api_keys: Optional[List[str]] = None,
        response_mime_type: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        max_retries: int = 2,
        timeout: Optional[float] = 120.0,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.response_mime_type = response_mime_type
        self.max_output_tokens = max_output_tokens
        self.thinking_budget = thinking_budget
        self.max_retries = max_retries
        self.timeout = timeout

        keys = api_keys if api_keys is not None else load_api_keys()
        if not keys:
            raise ValueError(
                "No Google API keys found. Set GOOGLE_API_KEY_1 (and optionally "
                "_2, _3) or a single GOOGLE_API_KEY in your environment or .env."
            )

        self._states = [_KeyState(index=i, key=k) for i, k in enumerate(keys, 1)]
        self._clients: Dict[int, ChatGoogleGenerativeAI] = {}
        self._cursor = 0  # index into _states to try first

    # -- introspection ----------------------------------------------------- #

    def __len__(self) -> int:
        return len(self._states)

    @property
    def key_count(self) -> int:
        return len(self._states)

    @property
    def active_key(self) -> int:
        return self._states[self._cursor].index

    def status(self) -> Dict[str, str]:
        """Per-key summary, for logs and end-of-run reporting."""
        out: Dict[str, str] = {}
        for state in self._states:
            if state.disabled:
                out[state.label] = f"disabled ({state.reason})"
            else:
                out[state.label] = (f"ok, {state.successes} call(s)"
                                    if state.successes else "unused")
        return out

    # -- internals --------------------------------------------------------- #

    def _client_for(self, state: _KeyState) -> ChatGoogleGenerativeAI:
        """Build (once) and cache the client for a key."""
        if state.index not in self._clients:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "temperature": self.temperature,
                "google_api_key": state.key,
                "max_retries": self.max_retries,
                "timeout": self.timeout,
            }
            # Only pass optional knobs when set, so defaults stay the API's own.
            if self.response_mime_type:
                kwargs["response_mime_type"] = self.response_mime_type
            if self.max_output_tokens is not None:
                kwargs["max_output_tokens"] = self.max_output_tokens
            if self.thinking_budget is not None:
                kwargs["thinking_budget"] = self.thinking_budget
            self._clients[state.index] = ChatGoogleGenerativeAI(**kwargs)
        return self._clients[state.index]

    def _order(self) -> List[_KeyState]:
        """Enabled keys, starting at the cursor and wrapping around."""
        rotated = self._states[self._cursor:] + self._states[:self._cursor]
        return [s for s in rotated if not s.disabled]

    def _handle_failure(self, state: _KeyState, error: BaseException) -> str:
        kind = classify_error(error)
        state.failures += 1
        snippet = str(error).replace("\n", " ")[:160]

        if kind == "bad_key":
            state.disabled = True
            state.reason = "rejected"
            logger.warning("Gemini %s rejected (disabling it): %s",
                           state.label, snippet)
        elif kind == "quota":
            logger.warning("Gemini %s rate limited, switching key: %s",
                           state.label, snippet)
        return kind

    def _succeed(self, state: _KeyState, position: int) -> None:
        state.successes += 1
        # Stick with the key that just worked.
        self._cursor = self._states.index(state)
        if position > 0:
            logger.warning("Gemini now using %s", state.label)

    # -- the call ---------------------------------------------------------- #

    def invoke(self, input: Any, **kwargs: Any) -> Any:
        """Invoke the model, walking through keys until one succeeds."""
        candidates = self._order()
        if not candidates:
            raise AllKeysExhausted(
                f"All {self.key_count} Gemini API key(s) are disabled.",
                status=self.status())

        last_error: Optional[BaseException] = None
        for position, state in enumerate(candidates):
            try:
                logger.debug("Gemini attempt with %s", state.label)
                result = self._client_for(state).invoke(input, **kwargs)
                self._succeed(state, position)
                return result
            except Exception as exc:
                last_error = exc
                if self._handle_failure(state, exc) == "request":
                    # A malformed request fails identically on every key.
                    raise

        raise AllKeysExhausted(
            f"All {len(candidates)} usable Gemini API key(s) failed "
            f"(model {self.model}). Last error: {last_error}",
            last_error=last_error, status=self.status())

    async def ainvoke(self, input: Any, **kwargs: Any) -> Any:
        """Async counterpart of invoke()."""
        candidates = self._order()
        if not candidates:
            raise AllKeysExhausted(
                f"All {self.key_count} Gemini API key(s) are disabled.",
                status=self.status())

        last_error: Optional[BaseException] = None
        for position, state in enumerate(candidates):
            try:
                logger.debug("Gemini attempt with %s", state.label)
                result = await self._client_for(state).ainvoke(input, **kwargs)
                self._succeed(state, position)
                return result
            except Exception as exc:
                last_error = exc
                if self._handle_failure(state, exc) == "request":
                    raise

        raise AllKeysExhausted(
            f"All {len(candidates)} usable Gemini API key(s) failed "
            f"(model {self.model}). Last error: {last_error}",
            last_error=last_error, status=self.status())


def check_keys(model: str, api_keys: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Send a minimal prompt on each key to see which are usable.

    One request per key. Cheap, but it does count against a working key's quota.
    """
    keys = api_keys if api_keys is not None else load_api_keys()
    results: List[Dict[str, Any]] = []

    for index, key in enumerate(keys, 1):
        entry: Dict[str, Any] = {"index": index, "length": len(key),
                                 "looks_like_google_key": key.startswith("AIza")}
        try:
            client = ChatGoogleGenerativeAI(
                model=model, temperature=0.0, google_api_key=key,
                max_retries=0, timeout=30.0, max_output_tokens=8,
            )
            client.invoke("Reply with the single word: ok")
            entry.update(usable=True, kind="ok", detail="")
        except Exception as exc:
            entry.update(usable=False, kind=classify_error(exc),
                         detail=str(exc).replace("\n", " ")[:200])
        results.append(entry)
    return results


if __name__ == "__main__":  # pragma: no cover
    import argparse

    from dotenv import load_dotenv

    load_dotenv(override=True)
    parser = argparse.ArgumentParser(
        description="Check which Gemini API keys are usable (1 request each).")
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    found = load_api_keys()
    print(f"{len(found)} key(s) configured | model {args.model}\n")
    for row in check_keys(args.model, found):
        verdict = "USABLE" if row["usable"] else f"unusable ({row['kind']})"
        print(f"key {row['index']}: {verdict}  "
              f"[len={row['length']}, AIza-prefixed={row['looks_like_google_key']}]")
        if row["detail"]:
            print(f"         {row['detail']}")
