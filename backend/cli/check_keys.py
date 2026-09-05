"""
Report which Gemini API keys are usable.

    python -m cli.check_keys

One small request per key: cheap, but it does consume a request from each
working key's daily quota, so don't run it in a loop.
"""

from __future__ import annotations

import argparse
import logging
from typing import List, Optional

from app.config import get_settings
from app.core.logging import configure_logging
from app.llm.gemini import check_keys


def main(argv: Optional[List[str]] = None) -> int:
    configure_logging(level=logging.WARNING)
    settings = get_settings()

    parser = argparse.ArgumentParser(description="Check Gemini API key health.")
    parser.add_argument("--model", default=settings.gemini_model)
    args = parser.parse_args(argv)

    keys = settings.api_keys
    if not keys:
        print("No API keys configured. Set GOOGLE_API_KEY_1 (and optionally _2, _3) "
              "or a single GOOGLE_API_KEY in .env.")
        return 1

    print(f"{len(keys)} key(s) configured | model {args.model}\n")
    usable = 0
    for row in check_keys(args.model, keys):
        verdict = "USABLE" if row["usable"] else f"unusable ({row['kind']})"
        usable += int(row["usable"])
        print(f"key {row['index']}: {verdict}  "
              f"[len={row['length']}, AIza-prefixed={row['looks_like_google_key']}]")
        if row["detail"]:
            print(f"         {row['detail']}")

    print(f"\n{usable}/{len(keys)} usable. Each visit costs 3 requests; "
          f"the free tier allows 20 per day per model per project.")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
