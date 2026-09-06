"""Guild schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field

from app.schemas.common import ORMModel


class GuildResponse(ORMModel):
    """A Discord server the bot belongs to."""

    id: int = Field(description="Local row id.")
    guild_id: str = Field(description="Discord guild snowflake.")
    name: str | None = Field(default=None, description="Guild name.")
    icon_url: str | None = Field(default=None, description="Guild icon CDN URL.")
    owner_id: str | None = Field(default=None, description="Guild owner snowflake.")
    is_available: bool = Field(description="False during a Discord outage.")
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "guild_id": "123456789012345678",
                "name": "Threat Intel Server",
                "icon_url": None,
                "owner_id": "111111111111111111",
                "is_available": True,
                "created_at": "2026-09-05T12:00:00Z",
                "updated_at": "2026-09-05T12:00:00Z",
            }
        },
    )
