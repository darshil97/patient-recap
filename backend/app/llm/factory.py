"""
Building the configured model.

The one place that reads llm.provider. Everything that needs a model (the
insight service, the batch CLI, the health routes) comes through here, so
switching provider is a one-line change in config.yaml.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Dict, Optional

from langchain_core.language_models import BaseChatModel

from app.config import LLM_PROVIDERS, get_settings
from app.llm.errors import classify_error
from app.llm.providers import PROVIDERS

# Enough for a reasoning model to finish thinking and say "ok". A reply cut off
# at the limit still proves the key works.
PROBE_MAX_TOKENS = 64


def _provider(name: str) -> ModuleType:
    module = PROVIDERS.get((name or "").strip().lower())
    if module is None:
        raise ValueError(f"Unknown LLM provider {name!r}; expected one of {LLM_PROVIDERS}.")
    return module


def build_llm(model: Optional[str] = None, provider: Optional[str] = None) -> BaseChatModel:
    """The configured provider's LangChain chat model.

    `model` overrides the model id *within* that provider, so it must be an id
    the provider recognises (a Gemini name for gemini, a Bedrock model id for
    bedrock). `provider` overrides llm.provider, for the key check CLI.
    """
    config = get_settings().llm
    return _provider(provider or config.provider).build(config, model=model)


def check_llm(provider: Optional[str] = None,
              model: Optional[str] = None) -> Dict[str, Any]:
    """Send one minimal request to see whether the provider's key works.

    Costs one request: on Gemini's free tier that is 1 of 20 per day.
    """
    config = get_settings().llm
    module = _provider(provider or config.provider)
    section = getattr(config, module.NAME)
    target = model or section.model

    result: Dict[str, Any] = {
        "provider": module.NAME,
        "model": target,
        "key_variable": module.KEY_VARIABLE,
        "configured": bool((section.api_key or "").strip()),
        "usable": False,
        "kind": "",
        "detail": "",
    }
    if not result["configured"]:
        result.update(kind="not_configured",
                      detail=f"{module.KEY_VARIABLE} is not set.")
        return result

    # No retries and a short timeout: this is a yes/no question.
    probe = config.model_copy(update={"max_retries": 0, "timeout_seconds": 30.0})
    try:
        module.build(probe, model=target, max_tokens=PROBE_MAX_TOKENS).invoke(
            "Reply with the single word: ok")
        result.update(usable=True, kind="ok")
    except Exception as exc:
        result.update(kind=classify_error(exc),
                      detail=str(exc).replace("\n", " ")[:300])
    return result
