"""
Google Gemini, through LangChain's ChatGoogleGenerativeAI.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import LLMConfig
from app.core.errors import ModelNotConfiguredError

NAME = "gemini"
KEY_VARIABLE = "GOOGLE_API_KEY"


def build(config: LLMConfig, model: Optional[str] = None,
          max_tokens: Optional[int] = None) -> ChatGoogleGenerativeAI:
    """A Gemini chat model from the `llm` and `llm.gemini` config sections.

    `model` overrides llm.gemini.model; `max_tokens` caps the reply (used by
    the key check to keep its one request cheap).
    """
    section = config.gemini
    api_key = (section.api_key or "").strip()
    if not api_key:
        raise ModelNotConfiguredError(
            f"No Gemini API key configured. Set {KEY_VARIABLE} in backend/.env, "
            f"or switch llm.provider in config.yaml.")

    kwargs: Dict[str, Any] = {
        "model": model or section.model,
        "google_api_key": api_key,
        "temperature": config.temperature,
        "max_retries": config.max_retries,
        "timeout": config.timeout_seconds,
    }
    # Only passed when wanted, so "off" means the API's own default rather
    # than a value invented here.
    if section.json_mode:
        kwargs["response_mime_type"] = "application/json"
    if max_tokens is not None:
        kwargs["max_output_tokens"] = max_tokens
    return ChatGoogleGenerativeAI(**kwargs)
