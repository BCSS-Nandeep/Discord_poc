"""Application-level notifications.

These are *our* status updates, written to SQLite and exposed over the API.  The
service never sends Discord DMs and never invents a Discord-native approval
notification -- Discord has no such mechanism for bot channel access.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.core.enums import NotificationEvent
from app.core.logging import get_logger
from app.database.models import DiscordNotification
from app.database.repositories.notification_repository import NotificationRepository

logger = get_logger(__name__)


class NotificationService:
    """Creates and reads application notifications."""

    def __init__(self, repository: NotificationRepository) -> None:
        self._repo = repository

    async def emit(
        self,
        event_type: NotificationEvent,
        message: str,
        *,
        access_request_id: int | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> DiscordNotification:
        """Record a notification for a status transition or noteworthy event."""

        payload_json: str | None = None
        if payload:
            try:
                payload_json = json.dumps(payload, default=str)
            except (TypeError, ValueError):  # pragma: no cover - defensive
                payload_json = None

        notification = await self._repo.create(
            event_type=event_type.value,
            message=message,
            access_request_id=access_request_id,
            guild_id=guild_id,
            channel_id=channel_id,
            payload_json=payload_json,
        )
        logger.info(
            "Notification created",
            extra={
                "event_type": event_type.value,
                "channel_id": channel_id,
                "guild_id": guild_id,
                "access_request_id": access_request_id,
            },
        )
        return notification

    async def list_notifications(
        self,
        *,
        channel_id: str | None = None,
        guild_id: str | None = None,
        event_type: NotificationEvent | None = None,
        access_request_id: int | None = None,
        unread: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[DiscordNotification], int]:
        return await self._repo.list_notifications(
            channel_id=channel_id,
            guild_id=guild_id,
            event_type=event_type.value if event_type else None,
            access_request_id=access_request_id,
            unread=unread,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

    async def mark_read(self, notification_id: int) -> DiscordNotification | None:
        notification = await self._repo.get(notification_id)
        if notification is None:
            return None
        return await self._repo.mark_read(notification)

    async def unread_count(self) -> int:
        return await self._repo.count_unread()
