"""
Amazon Bedrock, through LangChain's ChatBedrockConverse.

Uses the Converse API with a Bedrock API key (bearer token). gpt-oss returns its
reasoning as separate `reasoning_content` blocks, which `AIMessage.text` leaves
out, so the pipeline's JSON parsing sees only the answer.

No JSON mode is requested: Converse has no OpenAI-style `response_format`. The
prompts already demand JSON, and extract_json copes with fences or a preamble.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from botocore.config import Config
from langchain_aws import ChatBedrockConverse

from app.config import LLMConfig
from app.core.errors import ModelNotConfiguredError

NAME = "bedrock"
KEY_VARIABLE = "AWS_BEARER_TOKEN_BEDROCK"


def build(config: LLMConfig, model: Optional[str] = None,
          max_tokens: Optional[int] = None) -> ChatBedrockConverse:
    """A Bedrock chat model from the `llm` and `llm.bedrock` config sections.

    The API key is required rather than left to boto3's credential chain. On
    the server that chain would find the Neon Object Storage AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY and sign Bedrock calls with them.

    Note that langchain-aws hands the key to botocore by setting the
    AWS_BEARER_TOKEN_BEDROCK environment variable for this process. Only
    Bedrock clients read it; the S3 client for transcripts is unaffected.
    """
    section = config.bedrock
    api_key = (section.api_key or "").strip()
    if not api_key:
        raise ModelNotConfiguredError(
            f"No Bedrock API key configured. Set {KEY_VARIABLE} in backend/.env, "
            f"or switch llm.provider in config.yaml.")

    # Retries and timeouts belong to botocore here. In "standard" mode
    # max_attempts counts retries after the first call, matching llm.max_retries,
    # and covers throttling and 5xx responses.
    client_config = Config(
        read_timeout=config.timeout_seconds,
        retries={"max_attempts": config.max_retries, "mode": "standard"},
    )

    kwargs: Dict[str, Any] = {
        "model": model or section.model,
        "region_name": section.region,
        "bedrock_api_key": api_key,
        "temperature": config.temperature,
        "config": client_config,
    }
    tokens = max_tokens if max_tokens is not None else section.max_tokens
    if tokens is not None:
        kwargs["max_tokens"] = tokens
    return ChatBedrockConverse(**kwargs)
