from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from katilim_analiz.config import AppEnvironment, ModelProfile, Settings
from katilim_analiz.logging import (
    JsonFormatter,
    bind_correlation_id,
    reset_correlation_id,
)


def test_settings_parse_kubernetes_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local-kubernetes")
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "localhost,api.katilim.test")
    monkeypatch.setenv("MODEL_PROFILE", "laptop")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:secret@postgres/db")

    settings = Settings(_env_file=None)

    assert settings.app_env is AppEnvironment.LOCAL_KUBERNETES
    assert settings.app_allowed_hosts == ["localhost", "api.katilim.test"]
    assert settings.model_profile is ModelProfile.LAPTOP
    assert "secret" not in repr(settings.database_url)
    assert "database_url" not in settings.safe_summary()


def test_collector_user_agent_requires_contact() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ingest_user_agent="anonymous bot")


@pytest.mark.parametrize("value", ["0m", "forever", "-2"])
def test_model_keep_alive_rejects_ambiguous_values(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, model_keep_alive=value)


def test_model_keep_alive_can_pin_model_in_memory() -> None:
    assert Settings(_env_file=None, model_keep_alive="-1").model_keep_alive == "-1"


def test_model_deadline_defaults_to_quality_profile_hard_limit() -> None:
    assert Settings(_env_file=None).model_timeout_seconds == 120


def test_model_identity_defaults_to_the_verified_release_digest() -> None:
    settings = Settings(_env_file=None)
    assert settings.ollama_model == "qwen3.5:4b"
    assert settings.ollama_model_digest == (
        "2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://user:secret@ollama:11434",
        "http://ollama:11434/api",
        "http://ollama:11434/?token=secret",
        "http://ollama:11434/#fragment",
    ],
)
def test_ollama_endpoint_rejects_non_origin_or_credentialed_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ollama_base_url=url)


def test_safe_summary_contains_only_sanitized_model_origin() -> None:
    summary = Settings(_env_file=None, ollama_base_url="http://ollama:11434").safe_summary()
    assert summary["ollama_origin"] == "http://ollama:11434"
    assert "secret" not in repr(summary)


def test_cors_is_closed_by_default_and_normalizes_local_origins() -> None:
    assert Settings(_env_file=None).app_cors_origins == []
    settings = Settings(
        _env_file=None,
        app_cors_origins="http://LOCALHOST:5173/,http://localhost:5173",
    )
    assert settings.app_cors_origins == ["http://localhost:5173"]


@pytest.mark.parametrize(
    "origin",
    ["*", "file:///tmp/app", "https://user:secret@example.test", "https://example.test/x"],
)
def test_cors_rejects_unsafe_or_non_origin_values(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_cors_origins=[origin])


def test_json_log_includes_correlation_id_without_extra_secrets() -> None:
    token = bind_correlation_id("request-123")
    try:
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="ready",
            args=(),
            exc_info=None,
        )
        rendered = JsonFormatter().format(record)
    finally:
        reset_correlation_id(token)

    assert '"correlation_id":"request-123"' in rendered
    assert '"message":"ready"' in rendered
