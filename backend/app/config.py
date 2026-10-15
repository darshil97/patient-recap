"""
Application settings.

Two files, two jobs:

    backend/config.yaml   what to run: provider, models, limits, paths. Committed.
    backend/.env          secrets only. Gitignored, and referenced from the YAML
                          as ${NAME} so the YAML shows where every value comes from.

load_settings() reads the YAML, substitutes ${NAME} / ${NAME:-fallback} from the
environment and .env, and validates the result into the typed tree below.
get_settings() caches that for the process.

Every section rejects unknown keys, so a typo in config.yaml is a startup error
naming the key rather than a silent fall back to a default.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional

import yaml
from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.yaml"
ENV_FILE = BASE_DIR / ".env"

LLM_PROVIDERS = ("gemini", "bedrock")
STORAGE_BACKENDS = ("local", "neon_object_storage")


class _Section(BaseModel):
    """Base for every config section: unknown keys fail instead of being ignored."""

    model_config = ConfigDict(extra="forbid")


def _normalise_choice(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _under_base_dir(value: Path) -> Path:
    return value if value.is_absolute() else (BASE_DIR / value).resolve()


# --------------------------------------------------------------------------- #
# llm
# --------------------------------------------------------------------------- #

class GeminiConfig(_Section):
    model: str = "gemini-3.7-flash"
    api_key: Optional[str] = None
    # response_mime_type=application/json: the API constrains replies to JSON,
    # which is what all three prompts ask for.
    json_mode: bool = True


class BedrockConfig(_Section):
    # The Bedrock model id: gpt-oss-120b is published as openai.gpt-oss-120b-1:0.
    model: str = "openai.gpt-oss-120b-1:0"
    api_key: Optional[str] = None
    region: str = "us-west-2"
    # Upper bound on generated tokens, reasoning included. None = API default.
    max_tokens: Optional[int] = Field(default=None, gt=0)


class LLMConfig(_Section):
    provider: Literal["gemini", "bedrock"] = "gemini"
    temperature: float = 0.0
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    prompts_dir: Path = Path("prompts")

    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    bedrock: BedrockConfig = Field(default_factory=BedrockConfig)

    @field_validator("provider", mode="before")
    @classmethod
    def _normalise_provider(cls, value: Any) -> Any:
        return _normalise_choice(value)

    @field_validator("prompts_dir", mode="after")
    @classmethod
    def _resolve_prompts(cls, value: Path) -> Path:
        return _under_base_dir(value)

    @property
    def active(self) -> GeminiConfig | BedrockConfig:
        """The selected provider's own section."""
        return self.bedrock if self.provider == "bedrock" else self.gemini

    @property
    def model(self) -> str:
        """The model id the selected provider will be asked for."""
        return self.active.model

    @property
    def api_key_configured(self) -> bool:
        return bool((self.active.api_key or "").strip())


# --------------------------------------------------------------------------- #
# database, api
# --------------------------------------------------------------------------- #

class DatabaseConfig(_Section):
    url: Optional[str] = None
    pool_min: int = Field(default=1, ge=0)
    pool_max: int = Field(default=8, ge=1)


class ApiConfig(_Section):
    prefix: str = "/api/v1"
    # Shared secret for a single practice. Unset leaves the API open.
    key: Optional[str] = None
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept "*", one origin, "a,b", a JSON list string, or a YAML list.

        A trailing slash is stripped because an origin is scheme+host+port only:
        `https://app.example.com/` would never match a browser's Origin header and
        the mismatch is invisible in a log. Blank falls back to "*" so an empty
        CORS_ORIGINS= cannot silently block every origin.
        """
        if value is None:
            return ["*"]
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "cors_origins (CORS_ORIGINS) looks like JSON but could not "
                        f"be parsed: {exc}") from exc
                items = parsed if isinstance(parsed, list) else [parsed]
            else:
                items = stripped.split(",")
        elif isinstance(value, (list, tuple)):
            items = list(value)
        else:
            return value

        origins = [str(item).strip().rstrip("/") for item in items]
        return [origin for origin in origins if origin] or ["*"]


# --------------------------------------------------------------------------- #
# transcripts
# --------------------------------------------------------------------------- #

class ObjectStorageConfig(_Section):
    bucket: str = "consultation-transcripts"
    endpoint_url: Optional[str] = None
    access_key_id: Optional[str] = None
    secret_access_key: Optional[str] = None
    region: str = "us-east-2"

    @property
    def configured(self) -> bool:
        """Whether there is enough to reach the bucket.

        Checked before use so a missing credential is a clear error naming the
        variable, instead of a boto3 NoCredentialsError from three frames deep.
        """
        return bool(self.endpoint_url and self.access_key_id
                    and self.secret_access_key and self.bucket)


class TranscriptsConfig(_Section):
    # Generous ceiling, still well inside the model context: without it a large
    # paste fails much later as an opaque pipeline error.
    max_upload_bytes: int = Field(default=256 * 1024, gt=0)

    # Where *new* uploads go. Existing rows record their own backend in
    # visits.transcript_provider and are read back through it. The code default
    # is local so Settings() with no file needs no credentials.
    storage: Literal["local", "neon_object_storage"] = "local"

    # Transcript paths arrive from API payloads, so local reads are confined to
    # this directory (see app/domain/transcripts.py for the traversal guard).
    local_dir: Path = Path("transcripts")

    object_storage: ObjectStorageConfig = Field(default_factory=ObjectStorageConfig)

    @field_validator("storage", mode="before")
    @classmethod
    def _normalise_storage(cls, value: Any) -> Any:
        return _normalise_choice(value)

    @field_validator("local_dir", mode="after")
    @classmethod
    def _resolve_local_dir(cls, value: Path) -> Path:
        return _under_base_dir(value)


# --------------------------------------------------------------------------- #
# speech (audio uploads -> transcript)
# --------------------------------------------------------------------------- #

class DeepgramConfig(_Section):
    api_key: Optional[str] = None
    # Not `url`: the region matters for clinical audio (api.eu.deepgram.com keeps
    # processing in the EU), so it is configurable, but it is not a secret.
    base_url: str = "https://api.deepgram.com"
    model: str = "nova-3"
    # `en`, or `multi` for code-switched speech (English/Hindi and others).
    language: str = "en"
    # Diarizer version. `latest` follows Deepgram's current batch diarizer; the
    # boolean `diarize=true` is deprecated and pinned to the old one.
    diarize_model: str = "latest"
    smart_format: bool = True
    # Deepgram itself gives up on a pre-recorded request after 10 minutes.
    timeout_seconds: float = Field(default=300.0, gt=0)

    @field_validator("base_url", mode="after")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def configured(self) -> bool:
        return bool((self.api_key or "").strip())


class SpeechConfig(_Section):
    # Ceiling on an uploaded recording, across all of its chunks.
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # Size of each piece the browser sends. Every request passes through the
    # Next.js BFF, and Vercel caps a function's request body at 4.5 MB, so this
    # stays under that with room for the multipart envelope.
    chunk_bytes: int = Field(default=4_000_000, ge=64 * 1024)
    # Size of each piece a live, in-browser recording is sent in while it is
    # still being recorded (§8.24). Small, so a closed tab loses at most this
    # much: 256 KiB is about a minute of speech at the recorder's 32 kbps.
    live_chunk_bytes: int = Field(default=256 * 1024, ge=64 * 1024)
    # Chunks and recordings for the local storage backend. Which backend is used
    # follows transcripts.storage, so audio sits next to its transcript.
    local_dir: Path = Path("audio")
    deepgram: DeepgramConfig = Field(default_factory=DeepgramConfig)

    @field_validator("local_dir", mode="after")
    @classmethod
    def _resolve_local_dir(cls, value: Path) -> Path:
        return _under_base_dir(value)

    @model_validator(mode="after")
    def _live_chunks_fit_a_request(self) -> "SpeechConfig":
        # Both kinds of chunk cross the same 4.5 MB BFF body cap, and the chunk
        # route reads at most chunk_bytes + 1 of a request.
        if self.live_chunk_bytes > self.chunk_bytes:
            raise ValueError(
                f"speech.live_chunk_bytes ({self.live_chunk_bytes}) must not exceed "
                f"speech.chunk_bytes ({self.chunk_bytes}).")
        return self


class Settings(_Section):
    """Everything configurable, in one typed tree mirroring config.yaml."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    transcripts: TranscriptsConfig = Field(default_factory=TranscriptsConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Variables the previous flat, environment-only configuration read. Each is now
# a config.yaml key (or gone), so one left behind in .env or on the host would
# otherwise be ignored in silence.
_RETIRED_VARIABLES: Dict[str, str] = {
    "LLM_PROVIDER": "set llm.provider in config.yaml",
    "GEMINI_MODEL": "set llm.gemini.model in config.yaml",
    "GEMINI_API_KEY": "use GOOGLE_API_KEY",
    "BEDROCK_MODEL": "set llm.bedrock.model in config.yaml",
    "BEDROCK_REGION": "set llm.bedrock.region in config.yaml",
    "BEDROCK_MAX_TOKENS": "set llm.bedrock.max_tokens in config.yaml",
    "TRANSCRIPT_STORAGE": "set transcripts.storage in config.yaml",
    "TRANSCRIPT_BUCKET": "set transcripts.object_storage.bucket in config.yaml",
    "MAX_TRANSCRIPT_BYTES": "set transcripts.max_upload_bytes in config.yaml",
    **{f"GOOGLE_API_KEY_{i}": "only GOOGLE_API_KEY is read now" for i in range(1, 9)},
}


def _environment(env_file: Optional[Path],
                 environ: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """The variables ${...} resolves against.

    backend/.env wins over the process environment: a stale GOOGLE_API_KEY
    exported in a shell once shadowed the real one and every request failed with
    "API key not valid". An empty value in .env does not blank out a real one.
    """
    env = dict(os.environ if environ is None else environ)
    if env_file is not None and Path(env_file).is_file():
        for name, value in dotenv_values(env_file).items():
            if value:
                env[name] = value
    return env


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _interpolate(node: Any, env: Mapping[str, str]) -> Any:
    """Substitute ${NAME} and ${NAME:-fallback}, recursively.

    A mapping key whose value comes out blank is dropped, so the field's own
    default applies: `key: ${API_KEY}` with API_KEY unset means "no key", not
    an empty-string key.
    """
    if isinstance(node, dict):
        resolved = {key: _interpolate(value, env) for key, value in node.items()}
        return {key: value for key, value in resolved.items() if not _is_blank(value)}
    if isinstance(node, list):
        return [_interpolate(item, env) for item in node]
    if isinstance(node, str):
        def substitute(match: "re.Match[str]") -> str:
            value = env.get(match.group(1))
            if _is_blank(value):
                return match.group(2) or ""
            return str(value)

        return _REFERENCE.sub(substitute, node)
    return node


def retired_variables(env: Mapping[str, str]) -> Dict[str, str]:
    """Retired variable names that are still set, with what to do instead."""
    return {name: hint for name, hint in _RETIRED_VARIABLES.items()
            if not _is_blank(env.get(name))}


def load_settings(
    config_file: Optional[Path] = CONFIG_FILE,
    env_file: Optional[Path] = ENV_FILE,
    environ: Optional[Mapping[str, str]] = None,
) -> Settings:
    """Read config.yaml, resolve ${...} references, and validate.

    Pass config_file=None for code defaults only, env_file=None to leave .env
    out, and `environ` to replace os.environ. Tests use all three so results
    cannot depend on the developer's machine.
    """
    env = _environment(env_file, environ)

    raw: Any = {}
    if config_file is not None:
        path = Path(config_file)
        if not path.is_file():
            # No silent fallback: code defaults differ from the real deployment
            # (local storage, for one), so running on them would be a surprise.
            raise FileNotFoundError(f"Config file not found: {path}")
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{path.name} must contain a mapping at the top level.")

    for name, hint in retired_variables(env).items():
        logger.warning("%s is set but no longer read: %s.", name, hint)

    try:
        return Settings.model_validate(_interpolate(raw, env))
    except ValidationError as exc:
        source = Path(config_file).name if config_file is not None else "settings"
        raise ValueError(f"Invalid {source}:\n{exc}") from exc


@lru_cache
def get_settings() -> Settings:
    """Cached settings. Call get_settings.cache_clear() after changing config."""
    return load_settings()
