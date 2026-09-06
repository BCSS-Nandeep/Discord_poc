"""Application configuration, loaded from environment variables / ``.env``.

Secrets are held in :class:`pydantic.SecretStr` so they never appear in ``repr()``,
log records, tracebacks or API responses.
"""

from __future__ import annotations

import functools
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the standalone Discord data-collection service."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------------------------------------------- application --
    app_name: str = Field(default="discord-service", description="Service identifier.")
    environment: Literal["development", "staging", "production", "test"] = "development"
    host: str = "0.0.0.0"
    port: int = Field(default=8100, ge=1, le=65535)
    log_level: str = Field(default="INFO")
    log_json: bool = Field(
        default=False, description="Emit newline-delimited JSON logs instead of text."
    )
    cors_origins: str = Field(
        default="",
        description="Comma-separated allowed CORS origins. Empty disables CORS.",
    )
    api_keys: str = Field(
        default="",
        description=(
            "Comma-separated API keys accepted in the X-API-Key header. Empty "
            "disables authentication (development only)."
        ),
    )

    # --------------------------------------------------------------------- discord --
    discord_application_id: str = Field(default="", description="Discord application id.")
    discord_bot_token: SecretStr = Field(
        default=SecretStr(""),
        description="Discord bot token. Never logged and never returned by the API.",
    )
    discord_api_base_url: str = Field(
        default="https://discord.com/api/v10",
        description="Base URL for the Discord REST API (pinned API version).",
    )
    discord_message_content_intent: bool = Field(
        default=True,
        description=(
            "Whether the privileged Message Content intent is enabled for this "
            "application in the Discord Developer Portal."
        ),
    )
    discord_guild_members_intent: bool = Field(
        default=False,
        description="Whether the privileged Server Members intent is enabled.",
    )
    discord_request_timeout_seconds: float = Field(default=20.0, gt=0, le=300)
    discord_max_retries: int = Field(
        default=3, ge=0, le=10, description="Bounded retries for transient failures."
    )
    discord_retry_base_delay_seconds: float = Field(default=0.5, gt=0, le=30)
    discord_max_retry_delay_seconds: float = Field(default=30.0, gt=0, le=300)
    discord_max_rate_limit_wait_seconds: float = Field(
        default=60.0,
        gt=0,
        description="Refuse to sleep longer than this for a 429; raise instead.",
    )
    discord_user_agent: str = Field(
        default="DiscordCollectionService (https://example.invalid/discord-service, 1.0.0)"
    )

    # -------------------------------------------------------------------- database --
    database_url: str = Field(
        default="sqlite:///./discord_service.db",
        description="SQLite URL. A sync scheme is upgraded to aiosqlite automatically.",
    )
    database_echo: bool = False
    database_busy_timeout_ms: int = Field(default=10000, ge=0)

    # --------------------------------------------------------------------- workers --
    access_recheck_hours: int = Field(
        default=12,
        ge=1,
        le=168,
        description="Interval between automatic re-checks of PENDING access requests.",
    )
    access_request_expiry_days: int = Field(
        default=30,
        ge=1,
        description="A PENDING request older than this becomes EXPIRED.",
    )
    worker_tick_seconds: int = Field(
        default=300,
        ge=5,
        description="How often the reconciler wakes up to look for due requests.",
    )
    worker_batch_size: int = Field(default=25, ge=1, le=500)
    enable_background_workers: bool = Field(
        default=True, description="Master switch for the reconciliation worker."
    )
    enable_gateway: bool = Field(
        default=True, description="Master switch for the Discord Gateway listener."
    )
    worker_lock_file: str = Field(
        default=".discord_service.worker.lock",
        description=(
            "OS-level lock file guaranteeing a single worker/Gateway owner even when "
            "uvicorn reload or multiple workers spawn extra processes."
        ),
    )

    # ------------------------------------------------------------------ collection --
    history_page_size: int = Field(
        default=100, ge=1, le=100, description="Discord caps this endpoint at 100."
    )
    history_max_messages: int = Field(
        default=1000, ge=1, le=1000000, description="Default scrape ceiling."
    )
    keyword_match_mode: Literal["substring", "exact", "word"] = "substring"
    keyword_case_sensitive: bool = False

    # ------------------------------------------------------------------ validators --
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if normalized not in allowed:
            raise ValueError("log_level must be one of " + ", ".join(sorted(allowed)))
        return normalized

    @field_validator("discord_api_base_url")
    @classmethod
    def _validate_api_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith("https://discord.com/api"):
            # Hard allow-list: this service only ever talks to the official API.
            raise ValueError("discord_api_base_url must start with https://discord.com/api")
        return cleaned

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("database_url must not be empty")
        if cleaned.startswith("sqlite+aiosqlite:///"):
            return cleaned
        if cleaned.startswith("sqlite:///"):
            return cleaned.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
        raise ValueError("Only SQLite URLs are supported by this service")

    @model_validator(mode="after")
    def _validate_retry_window(self) -> Settings:
        if self.discord_retry_base_delay_seconds > self.discord_max_retry_delay_seconds:
            raise ValueError(
                "discord_retry_base_delay_seconds must be <= "
                "discord_max_retry_delay_seconds"
            )
        return self

    # --------------------------------------------------------------------- helpers --
    @property
    def bot_token(self) -> str:
        """The raw bot token. Only ever passed to the Authorization header.

        Tolerates a plain string because ``model_copy(update=...)`` bypasses
        validation and would otherwise leave a bare ``str`` in this field.
        """

        token = self.discord_bot_token
        if isinstance(token, SecretStr):
            return token.get_secret_value().strip()
        return str(token or "").strip()

    @property
    def discord_configured(self) -> bool:
        """True when a bot token is present."""

        return bool(self.bot_token)

    @property
    def api_key_list(self) -> list[str]:
        """Configured API keys. Empty means authentication is disabled."""

        return [key.strip() for key in self.api_keys.split(",") if key.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key_list)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def sqlite_path(self) -> str | None:
        """Filesystem path backing the SQLite database, if it is file based."""

        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        path = self.database_url[len(prefix) :]
        return path or None

    @property
    def is_in_memory_database(self) -> bool:
        return ":memory:" in self.database_url

    def public_summary(self) -> dict[str, Any]:
        """Configuration snapshot that is safe to expose. Never includes secrets."""

        return {
            "app_name": self.app_name,
            "environment": self.environment,
            "discord_api_base_url": self.discord_api_base_url,
            "discord_application_id": self.discord_application_id or None,
            "discord_configured": self.discord_configured,
            "message_content_intent": self.discord_message_content_intent,
            "access_recheck_hours": self.access_recheck_hours,
            "gateway_enabled": self.enable_gateway,
            "background_workers_enabled": self.enable_background_workers,
            "auth_enabled": self.auth_enabled,
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton used by the app and by FastAPI dependencies."""

    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that patch the environment)."""

    get_settings.cache_clear()
