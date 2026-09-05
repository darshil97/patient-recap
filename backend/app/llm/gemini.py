"""
Gemini client that rotates across several API keys.

Free-tier keys are capped per day per model, so a full backfill exhausts one key
partway through. This holds several keys and moves to the next when a key is
rate limited or rejected, presenting the `.invoke()` surface the pipeline wants.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from langchain_google_genai import ChatGoogleGenerativeAI

from app.llm.errors import BAD_KEY, QUOTA, REQUEST, AllKeysExhausted, classify_error

logger = logging.getLogger(__name__)


class ChatClient(Protocol):
    """The bit of a chat model the pipeline actually uses.

    A protocol so the multi-key wrapper, a bare ChatGoogleGenerativeAI, or a
    test stub are all acceptable.
    """

    model: str

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...


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


class MultiKeyGemini:
    """ChatGoogleGenerativeAI that falls through to other API keys on failure.

    Rotation is sticky: once a key works it keeps being used until it fails,
    rather than restarting from key 1 on every call. Without that, every request
    after key 1 is exhausted wastes a round trip rediscovering it.
    """

    def __init__(
        self,
        model: str,
        api_keys: List[str],
        temperature: float = 0.0,
        response_mime_type: Optional[str] = None,
        max_output_tokens: Optional[int] = None,
        thinking_budget: Optional[int] = None,
        max_retries: int = 2,
        timeout: Optional[float] = 120.0,
    ) -> None:
        if not api_keys:
            raise ValueError(
                "No Google API keys configured. Set GOOGLE_API_KEY_1 (and "
                "optionally _2, _3) or a single GOOGLE_API_KEY."
            )

        self.model = model
        self.temperature = temperature
        self.response_mime_type = response_mime_type
        self.max_output_tokens = max_output_tokens
        self.thinking_budget = thinking_budget
        self.max_retries = max_retries
        self.timeout = timeout

        self._states = [_KeyState(index=i, key=k) for i, k in enumerate(api_keys, 1)]
        self._clients: Dict[int, ChatGoogleGenerativeAI] = {}
        self._cursor = 0

    # -- introspection ----------------------------------------------------- #

    @property
    def key_count(self) -> int:
        return len(self._states)

    @property
    def active_key(self) -> int:
        return self._states[self._cursor].index

    def status(self) -> Dict[str, str]:
        """Per-key summary for logs and end-of-run reporting."""
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
        """Build once and cache the client for a key."""
        if state.index not in self._clients:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "temperature": self.temperature,
                "google_api_key": state.key,
                "max_retries": self.max_retries,
                "timeout": self.timeout,
            }
            # Optional knobs are only passed when set, so unset means the API's
            # own default rather than a value we invented.
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

        if kind == BAD_KEY:
            state.disabled = True
            state.reason = "rejected"
            logger.warning("Gemini %s rejected (disabling it): %s", state.label, snippet)
        elif kind == QUOTA:
            logger.warning("Gemini %s rate limited, switching key: %s",
                           state.label, snippet)
        return kind

    def _succeed(self, state: _KeyState, position: int) -> None:
        state.successes += 1
        self._cursor = self._states.index(state)  # stick with what worked
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
                result = self._client_for(state).invoke(input, **kwargs)
                self._succeed(state, position)
                return result
            except Exception as exc:
                last_error = exc
                if self._handle_failure(state, exc) == REQUEST:
                    raise  # a malformed request fails identically on every key

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
                result = await self._client_for(state).ainvoke(input, **kwargs)
                self._succeed(state, position)
                return result
            except Exception as exc:
                last_error = exc
                if self._handle_failure(state, exc) == REQUEST:
                    raise

        raise AllKeysExhausted(
            f"All {len(candidates)} usable Gemini API key(s) failed "
            f"(model {self.model}). Last error: {last_error}",
            last_error=last_error, status=self.status())


def build_llm(
    model: Optional[str] = None,
    api_keys: Optional[List[str]] = None,
    temperature: Optional[float] = None,
    json_mode: Optional[bool] = None,
) -> MultiKeyGemini:
    """Construct the client from settings, with per-call overrides.

    json_mode sets response_mime_type=application/json so the API itself
    constrains replies to JSON, which is what all three prompts ask for.
    """
    from app.config import get_settings

    settings = get_settings()
    keys = api_keys if api_keys is not None else settings.api_keys
    use_json = settings.json_mode if json_mode is None else json_mode

    return MultiKeyGemini(
        model=model or settings.gemini_model,
        api_keys=keys,
        temperature=settings.temperature if temperature is None else temperature,
        response_mime_type="application/json" if use_json else None,
        max_retries=settings.max_retries,
        timeout=settings.request_timeout,
    )


def check_keys(model: str, api_keys: List[str]) -> List[Dict[str, Any]]:
    """Send a minimal prompt on each key to see which are usable.

    One request per key: cheap, but it does count against a working key's quota.
    """
    results: List[Dict[str, Any]] = []
    for index, key in enumerate(api_keys, 1):
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
