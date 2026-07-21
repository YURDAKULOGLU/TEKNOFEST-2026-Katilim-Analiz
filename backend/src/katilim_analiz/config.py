"""Validated environment configuration shared by API and worker roles."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnvironment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    LOCAL_KUBERNETES = "local-kubernetes"
    PRODUCTION = "production"
    INSTITUTION = "institution"


class ModelProfile(StrEnum):
    RULES_ONLY = "rules_only"
    LAPTOP = "laptop"
    WORKSTATION = "workstation"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """Application settings; secrets are masked in repr and never logged wholesale."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Katılım Analiz"
    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65_535)
    app_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )
    app_cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://katilim:change-me@127.0.0.1:5432/katilim_analiz"
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=100)

    private_raw_dir: Path = Path("data/private/raw")
    public_derived_dir: Path = Path("datasets/derived")
    log_level: str = "INFO"
    log_format: LogFormat = LogFormat.JSON

    model_profile: ModelProfile = ModelProfile.RULES_ONLY
    model_provider: str = "ollama"
    ollama_base_url: HttpUrl = HttpUrl("http://127.0.0.1:11434")
    ollama_model: str = "qwen3.5:4b"
    ollama_model_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] = (
        "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    )
    model_timeout_seconds: int = Field(default=120, ge=1, le=600)
    model_max_context: int = Field(default=4096, ge=512, le=32_768)
    model_concurrency: int = Field(default=1, ge=1, le=4)
    model_keep_alive: str = "-1"
    #: Optional private directory that persists validated model answers across
    #: worker and scan runs. The in-process LRU cache is always active; this
    #: only adds durability so a restarted run does not re-ask answered
    #: questions. Replayed answers still pass the full grounding validation.
    model_response_cache_dir: Path | None = None

    ingest_network_enabled: bool = False
    ingest_user_agent: str = (
        "KatilimAnalizBot/0.1 (+local-competition-research; contact=configure-me)"
    )
    ingest_per_host_delay_seconds: float = Field(default=3.0, ge=1.0, le=120.0)
    ingest_max_bytes: int = Field(default=5_000_000, ge=1_024, le=25_000_000)

    @field_validator("app_allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("app_cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("app_cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for origin in value:
            if origin == "*":
                raise ValueError("wildcard CORS origins are forbidden")
            try:
                parsed = urlsplit(origin)
                port = parsed.port
            except ValueError as exc:
                raise ValueError("CORS origin contains an invalid port") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("CORS origins must be absolute HTTP(S) origins")
            host = parsed.hostname.casefold()
            default_port = 80 if parsed.scheme == "http" else 443
            authority = host if port in {None, default_port} else f"{host}:{port}"
            canonical = f"{parsed.scheme}://{authority}"
            if canonical not in normalized:
                normalized.append(canonical)
        return normalized

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.upper().strip()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("ingest_user_agent")
    @classmethod
    def require_identifiable_user_agent(cls, value: str) -> str:
        if len(value.strip()) < 10 or "contact=" not in value:
            raise ValueError("ingest user agent must identify the collector and a contact field")
        return value.strip()

    @field_validator("model_keep_alive")
    @classmethod
    def validate_keep_alive(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized == "-1":
            return normalized
        suffixes = ("s", "m", "h")
        if not normalized.endswith(suffixes) or not normalized[:-1].isdigit():
            raise ValueError("model_keep_alive must be -1 or a positive duration such as 30m")
        if int(normalized[:-1]) <= 0:
            raise ValueError("model_keep_alive duration must be positive")
        return normalized

    @field_validator("ollama_base_url")
    @classmethod
    def require_ollama_origin(cls, value: HttpUrl) -> HttpUrl:
        if (
            value.username is not None
            or value.password is not None
            or value.path not in {None, "", "/"}
            or value.query is not None
            or value.fragment is not None
        ):
            raise ValueError("ollama_base_url must be an HTTP(S) origin without credentials")
        return value

    def sqlalchemy_url(self) -> str:
        return self.database_url.get_secret_value()

    def safe_summary(self) -> dict[str, object]:
        """Return diagnostics that deliberately exclude credentials and sensitive paths."""

        return {
            "app_name": self.app_name,
            "app_env": self.app_env.value,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "cors_enabled": bool(self.app_cors_origins),
            "log_level": self.log_level,
            "model_profile": self.model_profile.value,
            "model_provider": self.model_provider,
            "ollama_origin": str(self.ollama_base_url).rstrip("/"),
            "ollama_model": self.ollama_model,
            "ollama_model_digest": self.ollama_model_digest,
            "model_keep_alive": self.model_keep_alive,
            "ingest_network_enabled": self.ingest_network_enabled,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
