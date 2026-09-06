"""Health and bot-identity schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthResponse(BaseModel):
    """Top-level service health. Never contains secrets."""

    status: Literal["ok", "degraded", "error"] = Field(
        description="Overall service health."
    )
    service: str = Field(description="Service name.")
    version: str = Field(description="Service version.")
    database: Literal["connected", "disconnected"] = Field(
        description="SQLite connectivity."
    )
    discord: Literal["connected", "unconfigured", "disconnected", "error"] = Field(
        description="Discord REST reachability using the configured bot token."
    )
    auth: Literal["enabled", "disabled"] = Field(
        description="Whether X-API-Key is required. 'disabled' means the API is open."
    )
    timestamp: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "ok",
                "service": "discord-service",
                "version": "1.0.0",
                "database": "connected",
                "discord": "connected",
                "auth": "enabled",
                "timestamp": "2026-09-05T12:00:00Z",
            }
        }
    )


class DiscordHealthResponse(BaseModel):
    """Detailed Discord subsystem health."""

    status: Literal["ok", "degraded", "error"]
    discord_configured: bool = Field(
        description="Whether a bot token is present. The token itself is never returned."
    )
    api_base_url: str
    bot: dict[str, Any] | None = Field(
        default=None, description="Bot identity from GET /users/@me."
    )
    gateway: dict[str, Any] = Field(description="Gateway connection status.")
    worker: dict[str, Any] = Field(description="Reconciliation worker status.")
    counts: dict[str, int] = Field(description="Row counts per table.")
    message_content_intent: bool
    access_recheck_hours: int
    error: str | None = None
    timestamp: datetime


class BotResponse(BaseModel):
    """The authenticated bot account. The token is never included."""

    id: str
    username: str | None = None
    global_name: str | None = None
    discriminator: str | None = None
    bot: bool = True
    avatar_url: str | None = None
    application_id: str | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "987654321098765432",
                "username": "collector-bot",
                "global_name": "Collector",
                "discriminator": "0",
                "bot": True,
                "avatar_url": None,
                "application_id": "987654321098765432",
            }
        }
    )
