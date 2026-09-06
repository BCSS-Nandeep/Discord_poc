"""Monitor schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import MonitorStatus
from app.schemas.access_request import AccessRequestResponse, ScrapeSummary
from app.schemas.common import ORMModel
from app.schemas.message import KeywordFilter


class MonitorStartRequest(BaseModel):
    """Body for ``POST /discord/channels/{channel_id}/monitor/start``.

    If the bot cannot read the channel yet the monitor is created in
    ``WAITING_FOR_ACCESS`` and an access request is opened automatically.
    """

    keywords: KeywordFilter | None = Field(
        default=None, description="Keyword filter applied to live messages."
    )
    collect_history: bool = Field(
        default=True,
        description="Run a historical collection before live monitoring starts.",
    )
    history_limit: int | None = Field(
        default=None, ge=1, le=100000, description="Cap for the initial history run."
    )
    store_all_messages: bool = Field(
        default=True,
        description="Store every live message, not only keyword matches.",
    )
    requested_by: str | None = Field(default=None, max_length=120)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "keywords": {"keywords": ["ransomware", "credential"]},
                "collect_history": True,
                "history_limit": 500,
                "store_all_messages": True,
            }
        }
    )


class MonitorStopRequest(BaseModel):
    """Body for ``POST /discord/channels/{channel_id}/monitor/stop``."""

    reason: str | None = Field(default=None, max_length=500)


class MonitorResponse(ORMModel):
    """A channel monitor."""

    id: int
    guild_id: str | None = None
    channel_id: str
    status: MonitorStatus
    store_all_messages: bool
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    last_event_at: datetime | None = None
    last_message_id: str | None = None
    last_error: str | None = None
    messages_seen: int
    messages_matched: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 3,
                "guild_id": "123456789012345678",
                "channel_id": "223456789012345678",
                "status": "WAITING_FOR_ACCESS",
                "store_all_messages": True,
                "started_at": None,
                "stopped_at": None,
                "last_event_at": None,
                "last_message_id": None,
                "last_error": None,
                "messages_seen": 0,
                "messages_matched": 0,
                "created_at": "2026-09-05T12:00:00Z",
                "updated_at": "2026-09-05T12:00:00Z",
            }
        },
    )


class MonitorStatusResponse(BaseModel):
    """Monitor status plus the keyword filter and Gateway view."""

    monitor: MonitorResponse
    keywords: KeywordFilter
    gateway_active: bool = Field(
        description="Whether the Gateway is currently collecting this channel."
    )
    access_request: AccessRequestResponse | None = Field(
        default=None, description="The pending access request this monitor waits on."
    )


class MonitorStartResponse(BaseModel):
    """Result of a monitor start request."""

    monitor: MonitorResponse
    message: str
    access_request: AccessRequestResponse | None = None
    scrape: ScrapeSummary | None = None
    waiting_for_access: bool = Field(
        default=False,
        description="True when the channel is private and access is not granted yet.",
    )
