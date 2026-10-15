"""
The interface the pipeline needs from a chat model, independent of provider.
"""

from __future__ import annotations

from typing import Any, Protocol


class ChatClient(Protocol):
    """The bit of a chat model the pipeline actually uses.

    Any LangChain chat model satisfies it, and so does a test stub with an
    `invoke()` method.
    """

    def invoke(self, input: Any, **kwargs: Any) -> Any: ...


def model_name(llm: Any) -> str:
    """The model id a client was built for, for provenance on the stored row.

    LangChain providers disagree on the attribute: ChatGoogleGenerativeAI has
    `model`, ChatBedrockConverse has `model_id`.
    """
    for attribute in ("model_id", "model", "model_name"):
        value = getattr(llm, attribute, None)
        if isinstance(value, str) and value:
            return value.removeprefix("models/")
    return "unknown"
