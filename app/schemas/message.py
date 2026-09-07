"""Message, scrape and search schemas."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.database.models import DiscordMessage
from app.schemas.common import ORMModel

MAX_KEYWORDS = 100


class KeywordFilter(BaseModel):
    """Keyword matching configuration.

    Default matching is case-insensitive substring.  ``word`` matches whole words and
    ``exact`` requires the whole message to equal the keyword.
    """

    keywords: list[str] = Field(
        default_factory=list,
        max_length=MAX_KEYWORDS,
        description="Keywords to match against message content.",
    )
    match_mode: Literal["substring", "exact", "word"] = Field(
        default="substring", description="How each keyword is compared."
    )
    case_sensitive: bool = Field(default=False)
    store_non_matching: bool = Field(
        default=True,
        description="When false, only messages matching a keyword are stored.",
    )

    @field_validator("keywords")
    @classmethod
    def _validate_keywords(cls, values: list[str]) -> list[str]:
        cleaned = [item.strip() for item in values if item and item.strip()]
        for item in cleaned:
            if len(item) > 200:
                raise ValueError("each keyword must be 200 characters or fewer")
        return cleaned

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "keywords": ["ransomware", "malware", "credential"],
                "match_mode": "substring",
                "case_sensitive": False,
                "store_non_matching": True,
            }
        }
    )


class MessageResponse(ORMModel):
    """A stored message in this service's normalized schema."""

    id: int
    platform: Literal["discord"] = Field(
        default="discord", description="Constant: identifies the source platform."
    )
    guild_id: str | None = None
    channel_id: str
    message_id: str
    author_id: str | None = None
    author_name: str | None = None
    author_is_bot: bool = False
    content: str | None = None
    timestamp: datetime
    edited_at: datetime | None = None
    message_url: str | None = None
    reply_to_message_id: str | None = None
    has_attachments: bool = False
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    embeds: list[dict[str, Any]] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    source: str = Field(description="How the message was collected: rest or gateway.")
    collected_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 1,
                "platform": "discord",
                "guild_id": "123456789012345678",
                "channel_id": "223456789012345678",
                "message_id": "333456789012345678",
                "author_id": "444456789012345678",
                "author_name": "analyst",
                "author_is_bot": False,
                "content": "New ransomware sample observed.",
                "timestamp": "2026-09-05T11:59:00Z",
                "edited_at": None,
                "message_url": (
                    "https://discord.com/channels/123456789012345678/"
                    "223456789012345678/333456789012345678"
                ),
                "reply_to_message_id": None,
                "has_attachments": False,
                "attachments": [],
                "embeds": [],
                "matched_keywords": ["ransomware"],
                "source": "rest",
                "collected_at": "2026-09-05T12:00:00Z",
            }
        },
    )

    @classmethod
    def from_entity(cls, entity: DiscordMessage) -> MessageResponse:
        """Build a response, decoding the JSON-encoded columns."""

        return cls(
            id=entity.id,
            guild_id=entity.guild_id or None,
            channel_id=entity.channel_id,
            message_id=entity.message_id,
            author_id=entity.author_id,
            author_name=entity.author_name,
            author_is_bot=entity.author_is_bot,
            content=entity.content,
            timestamp=entity.timestamp,
            edited_at=entity.edited_at,
            message_url=entity.message_url,
            reply_to_message_id=entity.reply_to_message_id,
            has_attachments=entity.has_attachments,
            attachments=_decode_json_list(entity.attachments_json),
            embeds=_decode_json_list(entity.embeds_json),
            matched_keywords=[
                str(item) for item in _decode_json_list(entity.matched_keywords_json)
            ],
            source=entity.source,
            collected_at=entity.collected_at,
        )


def _decode_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


class ScrapeRequest(BaseModel):
    """Body for ``POST /discord/channels/{channel_id}/scrape``."""

    limit: int | None = Field(
        default=None,
        ge=1,
        le=100000,
        description="Maximum messages to fetch. Defaults to HISTORY_MAX_MESSAGES.",
    )
    before: str | None = Field(
        default=None, description="Fetch messages older than this message id."
    )
    after: str | None = Field(
        default=None, description="Stop once this message id is reached."
    )
    incremental: bool = Field(
        default=False,
        description="Resume from the newest message already stored for the channel.",
    )
    keywords: KeywordFilter | None = None

    @field_validator("before", "after")
    @classmethod
    def _validate_snowflake(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) > 20:
            raise ValueError("must be a numeric Discord snowflake")
        return cleaned

    @model_validator(mode="after")
    def _validate_cursors(self) -> ScrapeRequest:
        if self.incremental and self.after is not None:
            raise ValueError("provide either 'after' or 'incremental', not both")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "limit": 500,
                "incremental": True,
                "keywords": {"keywords": ["ransomware", "malware"]},
            }
        }
    )


class SearchRequest(BaseModel):
    """Body for ``POST /discord/search`` -- searches messages already in SQLite."""

    keywords: list[str] = Field(
        default_factory=list,
        max_length=MAX_KEYWORDS,
        description="Keywords to search for. Empty returns recent messages.",
    )
    channel_id: str | None = None
    guild_id: str | None = None
    author_id: str | None = None
    since: datetime | None = Field(default=None, description="Only messages at/after this time.")
    until: datetime | None = Field(default=None, description="Only messages at/before this time.")
    match_mode: Literal["substring", "exact", "word"] = "substring"
    match_all: bool = Field(
        default=False, description="Require every keyword instead of any keyword."
    )
    case_sensitive: bool = False
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @field_validator("channel_id", "guild_id", "author_id")
    @classmethod
    def _validate_snowflake(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned.isdigit() or len(cleaned) > 20:
            raise ValueError("must be a numeric Discord snowflake")
        return cleaned

    @field_validator("keywords")
    @classmethod
    def _validate_keywords(cls, values: list[str]) -> list[str]:
        return [item.strip() for item in values if item and item.strip()]

    @model_validator(mode="after")
    def _validate_range(self) -> SearchRequest:
        if self.since and self.until and self.since > self.until:
            raise ValueError("'since' must be earlier than 'until'")
        return self

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "channel_id": "223456789012345678",
                "keywords": ["ransomware", "malware", "credential"],
                "limit": 100,
            }
        }
    )


class SearchHitResponse(BaseModel):
    """A stored message plus the keywords that matched this query."""

    message: MessageResponse
    matched_keywords: list[str] = Field(
        description="Keywords from this query that matched the message."
    )


class ScrapeJobResponse(BaseModel):
    """A background historical-collection job. Poll this instead of blocking on scrape."""

    id: int
    channel_id: str
    guild_id: str | None = None
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED"]
    requested_limit: int | None = None
    incremental: bool
    messages_fetched: int
    messages_stored: int
    messages_duplicate: int
    messages_matched: int
    pages_fetched: int
    checkpoint_message_id: str | None = Field(
        default=None, description="Oldest message id reached so far -- the resume point."
    )
    stopped_reason: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": 7,
                "channel_id": "223456789012345678",
                "guild_id": "123456789012345678",
                "status": "RUNNING",
                "requested_limit": 5000,
                "incremental": False,
                "messages_fetched": 800,
                "messages_stored": 800,
                "messages_duplicate": 0,
                "messages_matched": 12,
                "pages_fetched": 8,
                "checkpoint_message_id": "333456789012345000",
                "stopped_reason": None,
                "error_message": None,
                "created_at": "2026-09-08T12:00:00Z",
                "started_at": "2026-09-08T12:00:01Z",
                "finished_at": None,
            }
        },
    )
