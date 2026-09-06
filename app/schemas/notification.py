"""Notification schemas."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import NotificationEvent
from app.database.models import DiscordNotification
from app.schemas.common import ORMModel


class NotificationResponse(ORMModel):
    """An application-level status event recorded by this service."""

    id: int
    access_request_id: int | None = None
    guild_id: str | None = None
    channel_id: str | None = None
    event_type: NotificationEvent
    message: str
    payload: dict[str, Any] | None = Field(
        default=None, description="Event-specific structured context."
    )
    created_at: datetime
    delivered_at: datetime | None = None
    read_at: datetime | None = None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 42,
                "access_request_id": 12,
                "guild_id": "123456789012345678",
                "channel_id": "223456789012345678",
                "event_type": "ACCESS_GRANTED",
                "message": (
                    "A Discord administrator granted the bot access to channel "
                    "incident-response."
                ),
                "payload": {"check_count": 3},
                "created_at": "2026-09-06T00:00:00Z",
                "delivered_at": None,
                "read_at": None,
            }
        },
    )

    @classmethod
    def from_entity(cls, entity: DiscordNotification) -> NotificationResponse:
        payload: dict[str, Any] | None = None
        if entity.payload_json:
            try:
                decoded = json.loads(entity.payload_json)
                payload = decoded if isinstance(decoded, dict) else {"value": decoded}
            except (ValueError, TypeError):
                payload = None
        return cls(
            id=entity.id,
            access_request_id=entity.access_request_id,
            guild_id=entity.guild_id,
            channel_id=entity.channel_id,
            event_type=NotificationEvent(entity.event_type),
            message=entity.message,
            payload=payload,
            created_at=entity.created_at,
            delivered_at=entity.delivered_at,
            read_at=entity.read_at,
        )


class NotificationSummary(BaseModel):
    """Counts returned alongside a notification listing."""

    unread: int = Field(description="Notifications never marked read.")
