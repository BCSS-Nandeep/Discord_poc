"""Schemas for the application-level private-channel access workflow."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.enums import AccessRequestStatus, ChannelAccessStatus
from app.schemas.channel import AccessEvaluationResponse
from app.schemas.common import ORMModel
from app.schemas.message import KeywordFilter


class AccessRequestCreate(BaseModel):
    """Body for ``POST /discord/channels/{channel_id}/access-request``.

    This creates an **internal** record that we are waiting for a Discord server
    administrator to grant the bot access. Discord has no bot-facing endpoint to
    request or approve channel access, so nothing is sent to Discord here.
    """

    collect_history_on_grant: bool = Field(
        default=True,
        description="Collect historical messages automatically once access is granted.",
    )
    monitor_on_grant: bool = Field(
        default=False,
        description="Start live monitoring automatically once access is granted.",
    )
    keywords: KeywordFilter | None = Field(
        default=None, description="Keyword filter applied to collected messages."
    )
    requested_by: str | None = Field(
        default=None, max_length=120, description="Free-form requester label."
    )
    note: str | None = Field(default=None, max_length=1000)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "collect_history_on_grant": True,
                "monitor_on_grant": True,
                "keywords": {"keywords": ["ransomware", "malware", "credential"]},
                "requested_by": "threat-intel-team",
                "note": "Needed for incident 2451.",
            }
        }
    )


class AccessRequestResponse(ORMModel):
    """A stored access request."""

    id: int
    guild_id: str | None = None
    channel_id: str
    channel_name: str | None = None
    requested_at: datetime
    status: AccessRequestStatus
    last_checked_at: datetime | None = None
    next_check_at: datetime | None = Field(
        default=None, description="When the reconciliation worker will look again."
    )
    accepted_at: datetime | None = None
    denied_at: datetime | None = None
    expires_at: datetime | None = None
    rejection_reason: str | None = None
    last_error: str | None = None
    check_count: int = Field(description="How many times access has been re-checked.")
    requested_by: str | None = None
    note: str | None = None
    collect_history_on_grant: bool
    monitor_on_grant: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 12,
                "guild_id": "123456789012345678",
                "channel_id": "223456789012345678",
                "channel_name": "incident-response",
                "requested_at": "2026-09-05T12:00:00Z",
                "status": "PENDING",
                "last_checked_at": None,
                "next_check_at": "2026-09-06T00:00:00Z",
                "accepted_at": None,
                "denied_at": None,
                "expires_at": "2026-10-05T12:00:00Z",
                "rejection_reason": None,
                "last_error": None,
                "check_count": 0,
                "requested_by": "threat-intel-team",
                "note": None,
                "collect_history_on_grant": True,
                "monitor_on_grant": True,
                "created_at": "2026-09-05T12:00:00Z",
                "updated_at": "2026-09-05T12:00:00Z",
            }
        },
    )


class ScrapeSummary(BaseModel):
    """Counters from a historical collection run."""

    channel_id: str
    fetched: int = Field(description="Messages returned by Discord.")
    stored: int = Field(description="New rows written to SQLite.")
    duplicates: int = Field(description="Messages already stored, skipped.")
    matched: int = Field(description="Messages matching the keyword filter.")
    skipped_non_matching: int = Field(
        description="Messages discarded because they matched no keyword."
    )
    pages: int = Field(description="Discord API pages requested.")
    oldest_message_id: str | None = None
    newest_message_id: str | None = None
    completed: bool = Field(description="False when the run stopped early.")
    stopped_reason: str | None = None
    errors: list[str] = Field(default_factory=list)


class AccessRequestOutcomeResponse(BaseModel):
    """Response for the access-request and re-check endpoints."""

    channel_id: str
    channel_name: str | None = None
    guild_id: str | None = None
    channel_status: ChannelAccessStatus = Field(
        description="The channel's evaluated access status."
    )
    access_request_status: AccessRequestStatus | None = Field(
        default=None, description="Status of the internal access request, if one exists."
    )
    access_request: AccessRequestResponse | None = None
    requested_at: datetime | None = None
    next_check_at: datetime | None = None
    created: bool = Field(
        default=False, description="True when a new request was created by this call."
    )
    already_accessible: bool = Field(
        default=False, description="True when the bot could already read the channel."
    )
    transitioned: bool = Field(
        default=False, description="True when this call changed the request status."
    )
    message: str = Field(description="Human readable explanation of the outcome.")
    access: AccessEvaluationResponse
    scrape: ScrapeSummary | None = Field(
        default=None, description="Set when collection ran as part of this call."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel_id": "223456789012345678",
                "channel_name": "incident-response",
                "guild_id": "123456789012345678",
                "channel_status": "PRIVATE",
                "access_request_status": "PENDING",
                "requested_at": "2026-09-05T12:00:00Z",
                "next_check_at": "2026-09-06T00:00:00Z",
                "created": True,
                "already_accessible": False,
                "transitioned": False,
                "message": (
                    "Private channel. Waiting for a Discord server administrator to "
                    "grant the bot access (View Channel + Read Message History)."
                ),
            }
        }
    )


class AccessRequestListQuery(BaseModel):
    """Filters accepted by ``GET /discord/access-requests``."""

    status: AccessRequestStatus | None = None
    guild_id: str | None = None
    channel_id: str | None = None

    @field_validator("guild_id", "channel_id")
    @classmethod
    def _validate_snowflake(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) > 20:
            raise ValueError("must be a numeric Discord snowflake")
        return cleaned
