"""
Model providers. Each module exposes the same three names:

    NAME          the value of llm.provider that selects it
    KEY_VARIABLE  the environment variable its API key comes from
    build(config, model=None, max_tokens=None) -> a LangChain chat model

Adding a provider is a new module here, an entry in PROVIDERS, a section in
config.yaml and its config model in app/config.py.
"""

from __future__ import annotations

from types import ModuleType
from typing import Dict

from app.llm.providers import bedrock, gemini

PROVIDERS: Dict[str, ModuleType] = {gemini.NAME: gemini, bedrock.NAME: bedrock}
