"""Monitor lifecycle management.

A monitor only reaches ``RUNNING`` once the bot's access has been confirmed.  For an
inaccessible private channel the monitor is parked in ``WAITING_FOR_ACCESS`` and the
access-request workflow promotes it after an administrator grants permission.

This service owns monitor state only.  Orchestration across access requests, history
collection and monitors lives in
:class:`app.discord.access_request_service.AccessWorkflowService`, which keeps the
dependency graph acyclic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.core.enums import (
    MONITOR_TRANSITIONS,
    MonitorStatus,
    NotificationEvent,
    ensure_transition,
    is_valid_transition,
)
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import DiscordMonitor, utcnow
from app.database.repositories.monitor_repository import MonitorRepository
from app.discord.keywords import KeywordConfig
from app.discord.notification_service import NotificationService
from app.discord.rest_client import validate_snowflake

logger = get_logger(__name__)


class MonitorService:
    """Creates, transitions and reports on channel monitors."""

    def __init__(
        self,
        repository: MonitorRepository,
        notification_service: NotificationService,
    ) -> None:
        self._repo = repository
        self._notifications = notification_service

    # ------------------------------------------------------------------- accessors --
    async def get_by_channel(self, channel_id: str) -> DiscordMonitor | None:
        return await self._repo.get_by_channel_id(str(channel_id))

    async def require_by_channel(self, channel_id: str) -> DiscordMonitor:
        monitor = await self.get_by_channel(channel_id)
        if monitor is None:
            raise NotFoundError(
                "No monitor exists for this channel",
                details={"channel_id": str(channel_id)},
            )
        return monitor

    async def list_monitors(
        self,
        *,
        status: MonitorStatus | None = None,
        guild_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[DiscordMonitor]:
        return await self._repo.list_monitors(
            status=status.value if status else None,
            guild_id=guild_id,
            limit=limit,
            offset=offset,
        )

    async def list_active(self) -> Sequence[DiscordMonitor]:
        return await self._repo.list_active()

    async def running_channel_ids(self) -> list[str]:
        return await self._repo.list_running_channel_ids()

    # ------------------------------------------------------------------ lifecycle --
    async def ensure_monitor(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        keyword_config: KeywordConfig | None = None,
        store_all_messages: bool = True,
    ) -> DiscordMonitor:
        """Create or update the monitor row for a channel without starting it."""

        cid = validate_snowflake(channel_id, field="channel_id")
        monitor, created = await self._repo.get_or_create(channel_id=cid, guild_id=guild_id)
        if keyword_config is not None:
            monitor.keyword_config_json = (
                keyword_config.to_json() if keyword_config.enabled else None
            )
        monitor.store_all_messages = store_all_messages
        monitor.updated_at = utcnow()
        await self._repo.flush()
        if created:
            logger.info("Monitor record created", extra={"channel_id": cid})
        return monitor

    async def mark_waiting_for_access(self, monitor: DiscordMonitor) -> DiscordMonitor:
        """Park a monitor until an administrator grants the bot access."""

        if monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value:
            return monitor
        # A freshly created monitor is STOPPED, which is allowed to start waiting.
        ensure_transition(
            MONITOR_TRANSITIONS,
            MonitorStatus(monitor.status),
            MonitorStatus.WAITING_FOR_ACCESS,
        )
        await self._repo.set_status(monitor, status=MonitorStatus.WAITING_FOR_ACCESS)
        logger.info(
            "Monitor is waiting for channel access",
            extra={"channel_id": monitor.channel_id, "monitor_id": monitor.id},
        )
        return monitor

    async def start(self, monitor: DiscordMonitor) -> DiscordMonitor:
        """Transition a monitor to RUNNING. Access must already be confirmed."""

        current = MonitorStatus(monitor.status)
        if current is MonitorStatus.RUNNING:
            return monitor

        if is_valid_transition(MONITOR_TRANSITIONS, current, MonitorStatus.STARTING):
            await self._repo.set_status(monitor, status=MonitorStatus.STARTING)
            current = MonitorStatus.STARTING

        ensure_transition(MONITOR_TRANSITIONS, current, MonitorStatus.RUNNING)
        await self._repo.set_status(monitor, status=MonitorStatus.RUNNING)

        await self._notifications.emit(
            NotificationEvent.MONITOR_STARTED,
            f"Live monitoring started for channel {monitor.channel_id}.",
            guild_id=monitor.guild_id,
            channel_id=monitor.channel_id,
            payload={"monitor_id": monitor.id},
        )
        logger.info(
            "Monitor started",
            extra={"channel_id": monitor.channel_id, "monitor_id": monitor.id},
        )
        return monitor

    async def stop(self, monitor: DiscordMonitor, *, reason: str | None = None) -> DiscordMonitor:
        """Transition a monitor to STOPPED."""

        current = MonitorStatus(monitor.status)
        if current is MonitorStatus.STOPPED:
            return monitor
        ensure_transition(MONITOR_TRANSITIONS, current, MonitorStatus.STOPPED)
        await self._repo.set_status(
            monitor, status=MonitorStatus.STOPPED, last_error=reason
        )
        await self._notifications.emit(
            NotificationEvent.MONITOR_STOPPED,
            f"Live monitoring stopped for channel {monitor.channel_id}.",
            guild_id=monitor.guild_id,
            channel_id=monitor.channel_id,
            payload={"monitor_id": monitor.id, "reason": reason},
        )
        logger.info(
            "Monitor stopped",
            extra={"channel_id": monitor.channel_id, "reason": reason},
        )
        return monitor

    async def pause(self, monitor: DiscordMonitor) -> DiscordMonitor:
        current = MonitorStatus(monitor.status)
        ensure_transition(MONITOR_TRANSITIONS, current, MonitorStatus.PAUSED)
        return await self._repo.set_status(monitor, status=MonitorStatus.PAUSED)

    async def mark_error(self, monitor: DiscordMonitor, message: str) -> DiscordMonitor:
        current = MonitorStatus(monitor.status)
        if not is_valid_transition(MONITOR_TRANSITIONS, current, MonitorStatus.ERROR):
            return monitor
        logger.warning(
            "Monitor entered error state",
            extra={"channel_id": monitor.channel_id, "error": message},
        )
        return await self._repo.set_status(
            monitor, status=MonitorStatus.ERROR, last_error=message
        )

    async def handle_access_revoked(self, monitor: DiscordMonitor) -> DiscordMonitor:
        """Access that previously worked has disappeared.

        The monitor moves back to ``WAITING_FOR_ACCESS`` explicitly -- this is a real
        state change recorded for operators, not a silent failure.
        """

        current = MonitorStatus(monitor.status)
        if not is_valid_transition(
            MONITOR_TRANSITIONS, current, MonitorStatus.WAITING_FOR_ACCESS
        ):
            return monitor
        await self._repo.set_status(
            monitor,
            status=MonitorStatus.WAITING_FOR_ACCESS,
            last_error="Bot access to this channel was revoked.",
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_REVOKED,
            f"Bot access to channel {monitor.channel_id} was revoked; monitoring paused.",
            guild_id=monitor.guild_id,
            channel_id=monitor.channel_id,
            payload={"monitor_id": monitor.id},
        )
        return monitor

    # ----------------------------------------------------------------- live events --
    async def record_message_event(
        self,
        monitor: DiscordMonitor,
        *,
        message_id: str,
        occurred_at: datetime,
        matched: bool,
    ) -> DiscordMonitor:
        return await self._repo.record_event(
            monitor,
            message_id=message_id,
            occurred_at=occurred_at,
            matched=matched,
        )

    @staticmethod
    def keyword_config_for(monitor: DiscordMonitor) -> KeywordConfig:
        return KeywordConfig.from_json(monitor.keyword_config_json)

    async def count(self, *, status: MonitorStatus | None = None) -> int:
        return await self._repo.count(status=status.value if status else None)
