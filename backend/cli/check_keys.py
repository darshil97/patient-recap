"""
Check whether the model provider's API key works.

    python -m cli.check_keys                     # the provider in config.yaml
    python -m cli.check_keys --provider bedrock  # another one

One small request: cheap, but on Gemini's free tier it is 1 of 20 a day, and on
Bedrock it is a billed call, so don't run it in a loop.
"""

from __future__ import annotations

import argparse
import logging
from typing import List, Optional

from app.config import LLM_PROVIDERS, get_settings
from app.core.logging import configure_logging
from app.llm.factory import check_llm


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging(level=logging.WARNING)
    configured = get_settings().llm.provider

    parser = argparse.ArgumentParser(description="Check the model provider's API key.")
    parser.add_argument("--provider", choices=LLM_PROVIDERS, default=configured,
                        help=f"default: llm.provider in config.yaml ({configured})")
    parser.add_argument("--model", default=None,
                        help="model id within the provider (default: its configured model)")
    args = parser.parse_args(argv)

    row = check_llm(provider=args.provider, model=args.model)
    print(f"{row['provider']} | model {row['model']}")

    if not row["configured"]:
        print(f"No API key: set {row['key_variable']} in backend/.env.")
        return 1

    print("USABLE" if row["usable"] else f"unusable ({row['kind']})")
    if row["detail"]:
        print(f"  {row['detail']}")
    return 0 if row["usable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
