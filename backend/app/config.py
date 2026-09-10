"""
Application settings, loaded once from the environment / .env.

Replaces the scattered os.getenv calls of the original scripts so every
configurable value has one declared home, a type, and a default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

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
    #
    # Still used when TRANSCRIPT_STORAGE=local, and still the home of the
    # already-migrated originals, so it does not go away when object storage is
    # switched on.
    transcripts_dir: Path = BASE_DIR / "transcripts"

    # -- transcript storage ------------------------------------------------ #
    # Which backend *new* uploads go to. Existing rows are unaffected: each visit
    # records its own provider in visits.transcript_provider and is read back
    # through whichever one it names, so flipping this does not orphan anything
    # already stored. Moving old rows across is an explicit, verified step
    # (cli/migrate_transcripts.py), never a side effect of changing a setting.
    #
    # Defaults to local so a fresh checkout with no credentials still works and
    # the test suite stays offline.
    transcript_storage: str = Field(default="local", alias="TRANSCRIPT_STORAGE")

    transcript_bucket: str = Field(default="consultation-transcripts",
                                   alias="TRANSCRIPT_BUCKET")

    # AWS-standard names on purpose: `neon env pull` writes exactly these, and
    # boto3 would pick them up from the environment anyway, so there is one set of
    # names rather than a Neon-specific alias layer to keep in sync.
    s3_endpoint_url: Optional[str] = Field(default=None, alias="AWS_ENDPOINT_URL_S3")
    s3_access_key_id: Optional[str] = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    s3_secret_access_key: Optional[str] = Field(default=None,
                                                alias="AWS_SECRET_ACCESS_KEY")
    s3_region: str = Field(default="us-east-2", alias="AWS_REGION")

    @field_validator("transcript_storage", mode="after")
    @classmethod
    def _known_storage(cls, value: str) -> str:
        """Fail at startup on a typo rather than at the first upload.

        A mistyped provider would otherwise fall through to whatever the store
        factory treats as a default, and the first sign of trouble would be a
        transcript written somewhere nobody expected.
        """
        allowed = ("local", "neon_object_storage")
        cleaned = (value or "").strip().lower()
        if cleaned not in allowed:
            raise ValueError(
                f"TRANSCRIPT_STORAGE must be one of {allowed}, got {value!r}")
        return cleaned

    @property
    def object_storage_configured(self) -> bool:
        """Whether there is enough configuration to reach the bucket.

        Checked before use so a missing credential is a clear error naming the
        variable, instead of a boto3 NoCredentialsError from three frames deep.
        """
        return bool(self.s3_endpoint_url and self.s3_access_key_id
                    and self.s3_secret_access_key and self.transcript_bucket)

    # -- api --------------------------------------------------------------- #
    api_prefix: str = "/api/v1"

    # Permissive by default so local UI development needs no setup. Narrow this
    # before deployment: "*" cannot be combined with credentialed requests, so it
    # has to be fixed before enabling api_key rather than after.
    #
    # NoDecode is load-bearing. pydantic-settings treats List[str] as a complex
    # field and runs json.loads() on the raw env value inside the settings
    # source, which happens *before* field validators. Without NoDecode,
    # CORS_ORIGINS=https://app.example.com raises SettingsError and the process
    # will not start -- only JSON syntax would work. NoDecode hands the raw
    # string to _split_origins below instead.
    cors_origins: Annotated[List[str], NoDecode] = Field(
        default=["*"], alias="CORS_ORIGINS")

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
        """Parse CORS_ORIGINS, which arrives as a raw string thanks to NoDecode.

        Accepts a comma-separated list (`a,b`), a bare single origin (`*` or one
        URL), or JSON (`["a","b"]`). A trailing slash is stripped because an
        origin is scheme+host+port only -- `https://app.example.com/` would never
        match a browser's Origin header and the mismatch is invisible in a log.

        Falls back to the default when the value is blank, so an empty
        CORS_ORIGINS= in a .env cannot silently block every origin.
        """
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return ["*"]

        if stripped.startswith("["):
            import json

            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"CORS_ORIGINS looks like JSON but could not be parsed: {exc}"
                ) from exc
            items = parsed if isinstance(parsed, list) else [parsed]
        else:
            items = stripped.split(",")

        origins = [str(item).strip().rstrip("/") for item in items]
        return [origin for origin in origins if origin] or ["*"]

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
