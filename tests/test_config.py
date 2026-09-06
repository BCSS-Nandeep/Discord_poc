"""(1) Configuration loading and secret handling."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings, reset_settings_cache
from app.core.logging import REDACTED, SecretRedactionFilter, register_secret, scrub


def test_defaults_match_documented_values():
    settings = Settings(_env_file=None, discord_bot_token="token-value-1234567890")

    assert settings.discord_api_base_url == "https://discord.com/api/v10"
    assert settings.access_recheck_hours == 12
    assert settings.history_page_size == 100
    assert settings.log_level == "INFO"


def test_environment_variables_are_read(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "123456789012345678")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token-abcdefghijklmnop")
    monkeypatch.setenv("ACCESS_RECHECK_HOURS", "6")
    monkeypatch.setenv("LOG_LEVEL", "debug")

    settings = Settings(_env_file=None)

    assert settings.discord_application_id == "123456789012345678"
    assert settings.bot_token == "env-token-abcdefghijklmnop"
    assert settings.access_recheck_hours == 6
    assert settings.log_level == "DEBUG"


def test_sync_sqlite_url_is_upgraded_to_aiosqlite():
    settings = Settings(_env_file=None, database_url="sqlite:///./discord_service.db")

    assert settings.database_url == "sqlite+aiosqlite:///./discord_service.db"
    assert settings.sqlite_path == "./discord_service.db"


def test_non_sqlite_database_url_is_rejected():
    with pytest.raises(ValidationError, match="Only SQLite"):
        Settings(_env_file=None, database_url="postgresql://localhost/db")


def test_api_base_url_is_restricted_to_discord():
    """A client-supplied host must never become the API base."""

    with pytest.raises(ValidationError, match="discord.com/api"):
        Settings(_env_file=None, discord_api_base_url="https://evil.example.com/api/v10")


def test_invalid_log_level_is_rejected():
    with pytest.raises(ValidationError, match="log_level"):
        Settings(_env_file=None, log_level="chatty")


def test_missing_token_is_reported_not_fatal():
    settings = Settings(_env_file=None, discord_bot_token="")

    assert settings.discord_configured is False
    assert settings.bot_token == ""


def test_token_never_appears_in_repr_or_public_summary():
    settings = Settings(_env_file=None, discord_bot_token="super-secret-token-value")

    assert "super-secret-token-value" not in repr(settings)
    assert "super-secret-token-value" not in str(settings.model_dump())
    assert "super-secret-token-value" not in str(settings.public_summary())
    assert "discord_bot_token" not in settings.public_summary()


def test_registered_secret_is_scrubbed_from_log_records():
    token = "hunter2-hunter2-hunter2"
    register_secret(token)
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "using %s now", (token,), None
    )

    assert SecretRedactionFilter().filter(record) is True
    assert token not in str(record.args)
    assert REDACTED in str(record.args)


def test_scrub_redacts_authorization_headers():
    scrubbed = scrub({"Authorization": "Bot abc", "User-Agent": "svc"})

    assert scrubbed["Authorization"] == REDACTED
    assert scrubbed["User-Agent"] == "svc"


def test_cors_origins_parse_into_a_list():
    settings = Settings(
        _env_file=None, cors_origins="https://a.example, https://b.example ,"
    )

    assert settings.cors_origin_list == ["https://a.example", "https://b.example"]


def test_settings_singleton_is_cached(monkeypatch: pytest.MonkeyPatch):
    reset_settings_cache()
    monkeypatch.setenv("APP_NAME", "cached-service")
    first = get_settings()
    second = get_settings()

    assert first is second
    reset_settings_cache()
