"""Logging setup shared by the API and the CLIs."""

from __future__ import annotations

import logging

# The provider clients and their HTTP layers narrate every request. The CLI
# renders its own progress and the API has its own access log, so both are
# noise here.
NOISY_LOGGERS = (
    "httpx", "httpcore", "google_genai", "google.genai",
    "google_genai.models", "langchain_google_genai",
    "botocore", "boto3", "urllib3", "langchain_aws",
)


def configure_logging(level: int = logging.INFO, quiet_third_party: bool = True) -> None:
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    if quiet_third_party:
        for name in NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.ERROR)
