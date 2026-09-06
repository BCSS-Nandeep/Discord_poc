"""Channel and access-evaluation schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import AccessReason, ChannelAccessStatus
from app.schemas.common import ORMModel


class ChannelResponse(ORMModel):
    """A channel and the bot's last evaluated access to it."""

    id: int = Field(description="Local row id.")
    guild_id: str | None = None
    channel_id: str = Field(description="Discord channel snowflake.")
    parent_id: str | None = Field(default=None, description="Category or parent channel.")
    name: str | None = None
    channel_type: int = Field(description="Discord numeric channel type.")
    position: int | None = None
    topic: str | None = None
    is_private: bool = Field(
        description="True when @everyone is denied View Channel on this channel."
    )
    bot_can_view: bool
    bot_can_read_history: bool
    bot_can_read_message_content: bool
    access_status: ChannelAccessStatus
    access_reason: AccessReason | None = None
    last_permission_check_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 7,
                "guild_id": "123456789012345678",
                "channel_id": "223456789012345678",
                "parent_id": None,
                "name": "incident-response",
                "channel_type": 0,
                "position": 3,
                "topic": None,
                "is_private": True,
                "bot_can_view": False,
                "bot_can_read_history": False,
                "bot_can_read_message_content": True,
                "access_status": "PRIVATE",
                "access_reason": "BOT_CANNOT_VIEW_CHANNEL",
                "last_permission_check_at": "2026-09-05T12:00:00Z",
                "created_at": "2026-09-05T12:00:00Z",
                "updated_at": "2026-09-05T12:00:00Z",
            }
        },
    )


class AccessEvaluationResponse(BaseModel):
    """Structured result from the PermissionService."""

    channel_id: str
    guild_id: str | None = None
    channel_name: str | None = None
    channel_type: int | None = None
    channel_type_name: str | None = None
    exists: bool = Field(description="Whether the channel exists and is addressable.")
    is_private: bool
    bot_can_view: bool
    bot_can_read_history: bool
    bot_can_read_message_content: bool
    access_status: ChannelAccessStatus
    reason: AccessReason = Field(description="Machine-readable explanation.")
    detail: str | None = Field(default=None, description="Human readable explanation.")
    collection_allowed: bool = Field(
        description="Whether history collection and monitoring may run right now."
    )
    checked_at: datetime

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel_id": "223456789012345678",
                "guild_id": "123456789012345678",
                "channel_name": "incident-response",
                "channel_type": 0,
                "channel_type_name": "GUILD_TEXT",
                "exists": True,
                "is_private": True,
                "bot_can_view": False,
                "bot_can_read_history": False,
                "bot_can_read_message_content": True,
                "access_status": "PRIVATE",
                "reason": "BOT_CANNOT_VIEW_CHANNEL",
                "detail": "The bot does not have the View Channel permission for this channel.",
                "collection_allowed": False,
                "checked_at": "2026-09-05T12:00:00Z",
            }
        }
    )


class ChannelDetailResponse(BaseModel):
    """A channel plus a freshly computed access evaluation."""

    channel: ChannelResponse
    access: AccessEvaluationResponse
    open_access_request_id: int | None = Field(
        default=None, description="Id of the PENDING access request, if any."
    )
