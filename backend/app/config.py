"""
Application settings, loaded once from the environment / .env.

Replaces the scattered os.getenv calls of the original scripts so every
configurable value has one declared home, a type, and a default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

MAX_API_KEYS = 8


class Settings(BaseSettings):
    """Everything configurable, in one place."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # The project's .env wins over anything already exported in the shell.
        # A stale GOOGLE_API_KEY in the environment previously shadowed the real
        # one and every request failed with "API key not valid".
        env_ignore_empty=True,
    )

    # -- database ---------------------------------------------------------- #
    connstr: str = Field(default="", alias="CONNSTR")
    db_pool_min: int = 1
    db_pool_max: int = 8

    # -- model ------------------------------------------------------------- #
    gemini_model: str = Field(default="gemini-3.7-flash", alias="GEMINI_MODEL")
    temperature: float = 0.0
    request_timeout: float = 120.0
    max_retries: int = 2
    json_mode: bool = True

    # Numbered keys are read explicitly so their order is stable.
    google_api_key: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY")
    google_api_key_1: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY_1")
    google_api_key_2: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY_2")
    google_api_key_3: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY_3")
    google_api_key_4: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY_4")
    google_api_key_5: Optional[str] = Field(default=None, alias="GOOGLE_API_KEY_5")
    gemini_api_key: Optional[str] = Field(default=None, alias="GEMINI_API_KEY")

    # -- paths ------------------------------------------------------------- #
    prompts_dir: Path = BASE_DIR / "prompts"

    # Transcript paths arrive from API payloads, so reads are confined to this
    # directory. See app/domain/transcripts.py for the traversal guard.
    transcripts_dir: Path = BASE_DIR / "transcripts"

    # -- api --------------------------------------------------------------- #
    api_prefix: str = "/api/v1"

    # Permissive by default so local UI development needs no setup. Narrow this
    # before deployment: "*" cannot be combined with credentialed requests, so it
    # has to be fixed before enabling api_key rather than after.
    cors_origins: List[str] = Field(default=["*"], alias="CORS_ORIGINS")

    # Shared secret for a single practice. When set, every route except
    # GET /health requires an X-API-Key header. When unset the API stays open,
    # so local development is unchanged.
    api_key: Optional[str] = Field(default=None, alias="API_KEY")

    # Real transcripts are 3-5 KB. Generous ceiling, still well inside the model
    # context: without it a large paste fails much later as an opaque pipeline
    # error instead of a clear rejection.
    max_transcript_bytes: int = Field(default=256 * 1024, alias="MAX_TRANSCRIPT_BYTES")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept a comma-separated CORS_ORIGINS, which is how env vars carry lists."""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value  # let pydantic parse JSON form
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("prompts_dir", "transcripts_dir", mode="after")
    @classmethod
    def _resolve(cls, value: Path) -> Path:
        return value if value.is_absolute() else (BASE_DIR / value).resolve()

    @property
    def api_keys(self) -> List[str]:
        """Ordered, de-duplicated API keys.

        Numbered keys take precedence; a single GOOGLE_API_KEY (or
        GEMINI_API_KEY) is only used when no numbered key is set, so the same
        credential is never counted twice against its quota.
        """
        numbered: List[str] = []
        for index in range(1, MAX_API_KEYS + 1):
            value = (getattr(self, f"google_api_key_{index}", None) or "").strip()
            if value and value not in numbered:
                numbered.append(value)
        if numbered:
            return numbered

        fallback: List[str] = []
        for value in (self.google_api_key, self.gemini_api_key):
            cleaned = (value or "").strip()
            if cleaned and cleaned not in fallback:
                fallback.append(cleaned)
        return fallback


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Clear the cache in tests to override."""
    return Settings()
