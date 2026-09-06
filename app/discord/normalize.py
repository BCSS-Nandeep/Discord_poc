"""Normalization of Discord payloads into this service's stable internal schema.

Both the REST scraper and the Gateway listener funnel through here, so a message row
looks identical regardless of how it arrived.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Keys kept from the raw payload for debugging/auditing. Storing the entire object
#: would duplicate the normalized columns for no benefit.
RAW_KEYS_KEPT = (
    "id",
    "type",
    "flags",
    "pinned",
    "tts",
    "mention_everyone",
    "mentions",
    "mention_roles",
    "message_reference",
    "sticker_items",
    "thread",
)

MAX_RAW_JSON_CHARS = 20000


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a Discord ISO-8601 timestamp into an aware UTC datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        logger.debug("Could not parse Discord timestamp", extra={"value": str(value)[:64]})
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def snowflake_to_datetime(snowflake: str) -> datetime | None:
    """Derive the creation time embedded in a Discord snowflake."""

    try:
        value = int(snowflake)
    except (TypeError, ValueError):
        return None
    # Discord epoch: 2015-01-01T00:00:00Z, in milliseconds.
    millis = (value >> 22) + 1420070400000
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def build_message_url(guild_id: str | None, channel_id: str, message_id: str) -> str:
    """A permalink to the message in the Discord client."""

    return f"https://discord.com/channels/{guild_id or '@me'}/{channel_id}/{message_id}"


def normalize_attachments(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "id": str(item.get("id")),
            "filename": item.get("filename"),
            "size": item.get("size"),
            "content_type": item.get("content_type"),
            "url": item.get("url"),
        }
        for item in payload or []
    ]


def normalize_embeds(payload: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [
        {
            "type": item.get("type"),
            "title": item.get("title"),
            "description": item.get("description"),
            "url": item.get("url"),
        }
        for item in payload or []
    ]


def _trim_raw(payload: dict[str, Any]) -> str | None:
    subset = {key: payload[key] for key in RAW_KEYS_KEPT if key in payload}
    if not subset:
        return None
    try:
        encoded = json.dumps(subset, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return encoded[:MAX_RAW_JSON_CHARS]


def normalize_message(
    payload: dict[str, Any],
    *,
    channel_id: str,
    guild_id: str | None,
    matched_keywords: list[str] | None = None,
    source: str = "rest",
    store_raw: bool = True,
) -> dict[str, Any]:
    """Convert a Discord message object into a ``discord_messages`` row dict.

    ``guild_id`` falls back to an empty string so the unique constraint over
    ``(guild_id, channel_id, message_id)`` always applies -- SQLite treats NULLs inside
    a UNIQUE tuple as distinct, which would silently defeat duplicate protection.
    """

    message_id = str(payload.get("id"))
    author = payload.get("author") or {}
    reference = payload.get("message_reference") or {}
    attachments = payload.get("attachments") or []

    timestamp = parse_timestamp(payload.get("timestamp")) or snowflake_to_datetime(
        message_id
    )
    if timestamp is None:  # pragma: no cover - Discord always supplies one of these
        timestamp = datetime.now(UTC)

    resolved_guild_id = guild_id or (
        str(payload["guild_id"]) if payload.get("guild_id") is not None else None
    )

    author_name = (
        author.get("global_name") or author.get("username") or None
    )

    return {
        "guild_id": resolved_guild_id or "",
        "channel_id": str(channel_id),
        "message_id": message_id,
        "author_id": str(author["id"]) if author.get("id") is not None else None,
        "author_name": author_name,
        "author_is_bot": bool(author.get("bot", False)),
        "content": payload.get("content") or "",
        "timestamp": timestamp,
        "edited_at": parse_timestamp(payload.get("edited_timestamp")),
        "message_url": build_message_url(resolved_guild_id, str(channel_id), message_id),
        "reply_to_message_id": (
            str(reference["message_id"]) if reference.get("message_id") else None
        ),
        "has_attachments": bool(attachments),
        "attachments_json": (
            json.dumps(normalize_attachments(attachments)) if attachments else None
        ),
        "embeds_json": (
            json.dumps(normalize_embeds(payload.get("embeds")))
            if payload.get("embeds")
            else None
        ),
        "raw_json": _trim_raw(payload) if store_raw else None,
        "matched_keywords_json": (
            json.dumps(matched_keywords) if matched_keywords else None
        ),
        "source": source,
    }


def normalize_guild(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Discord guild object."""

    guild_id = str(payload.get("id"))
    icon_hash = payload.get("icon")
    return {
        "guild_id": guild_id,
        "name": payload.get("name"),
        "icon_url": (
            f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png"
            if icon_hash
            else None
        ),
        "owner_id": str(payload["owner_id"]) if payload.get("owner_id") else None,
        "is_available": not bool(payload.get("unavailable", False)),
    }


def normalize_channel(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Discord channel object into channel metadata."""

    return {
        "channel_id": str(payload.get("id")),
        "guild_id": str(payload["guild_id"]) if payload.get("guild_id") else None,
        "parent_id": str(payload["parent_id"]) if payload.get("parent_id") else None,
        "name": payload.get("name"),
        "channel_type": int(payload.get("type", 0)),
        "position": payload.get("position"),
        "topic": payload.get("topic"),
    }
