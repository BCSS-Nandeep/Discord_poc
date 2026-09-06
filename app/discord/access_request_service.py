"""The application-level private-channel access workflow.

**Discord has no API for a bot to request access to a private channel.**  There is no
endpoint to submit a request and no Discord-side approval to wait on.  What this module
implements is an *internal* record of intent:

1. A caller asks this service to collect from a channel the bot cannot read.
2. We store an access request with status ``PENDING`` and report the channel as
   ``PRIVATE`` / ``ACCESS_PENDING``.
3. A Discord **server administrator** independently grants the bot *View Channel* and
   *Read Message History* on that channel (in the Discord UI -- nothing we can trigger).
4. The reconciliation worker re-checks every ``ACCESS_RECHECK_HOURS`` (12 by default),
   and Gateway permission events provide a faster signal.
5. When the bot can actually read the channel, the request becomes ``ACCEPTED`` and the
   follow-up pipeline (history collection, monitor activation) runs.

Two classes live here:

``AccessRequestService``
    Owns the request record and its validated state transitions.

``AccessWorkflowService``
    The coordinator across permissions, channels, requests, collection and monitors.
    Keeping it here (rather than making services import each other) keeps the service
    dependency graph a DAG.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.config import Settings
from app.core.enums import (
    ACCESS_REQUEST_TRANSITIONS,
    AccessReason,
    AccessRequestStatus,
    ChannelAccessStatus,
    MonitorStatus,
    NotificationEvent,
    ensure_transition,
)
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database.models import DiscordAccessRequest, DiscordMonitor, utcnow
from app.database.repositories.access_request_repository import AccessRequestRepository
from app.discord.channel_service import ChannelService
from app.discord.keywords import KeywordConfig
from app.discord.message_service import MessageService, ScrapeResult
from app.discord.monitor_service import MonitorService
from app.discord.notification_service import NotificationService
from app.discord.permission_service import AccessEvaluation
from app.discord.rest_client import validate_snowflake

logger = get_logger(__name__)

PENDING_MESSAGE = (
    "Private channel. Waiting for a Discord server administrator to grant the bot "
    "access (View Channel + Read Message History)."
)


@dataclass(slots=True)
class AccessRequestOutcome:
    """What happened when a channel was requested or re-checked."""

    channel_id: str
    guild_id: str | None
    channel_name: str | None
    channel_status: ChannelAccessStatus
    request: DiscordAccessRequest | None
    evaluation: AccessEvaluation
    message: str
    created: bool = False
    already_accessible: bool = False
    transitioned: bool = False
    scrape: ScrapeResult | None = None
    monitor: DiscordMonitor | None = None

    @property
    def request_status(self) -> AccessRequestStatus | None:
        return AccessRequestStatus(self.request.status) if self.request else None


class AccessRequestService:
    """Owns access-request records and their state transitions."""

    def __init__(
        self,
        repository: AccessRequestRepository,
        notification_service: NotificationService,
        settings: Settings,
    ) -> None:
        self._repo = repository
        self._notifications = notification_service
        self._settings = settings

    # ---------------------------------------------------------------- scheduling --
    def next_check_at(self, *, from_time: datetime | None = None) -> datetime:
        """``now + ACCESS_RECHECK_HOURS`` (12 hours by default)."""

        base = from_time or utcnow()
        return base + timedelta(hours=self._settings.access_recheck_hours)

    def expiry_at(self, *, from_time: datetime | None = None) -> datetime:
        base = from_time or utcnow()
        return base + timedelta(days=self._settings.access_request_expiry_days)

    # -------------------------------------------------------------------- reading --
    async def get(self, request_id: int) -> DiscordAccessRequest:
        request = await self._repo.get(request_id)
        if request is None:
            raise NotFoundError(
                "Access request not found", details={"request_id": request_id}
            )
        return request

    async def get_open_for_channel(self, channel_id: str) -> DiscordAccessRequest | None:
        return await self._repo.get_open_for_channel(str(channel_id))

    async def list_requests(
        self,
        *,
        status: AccessRequestStatus | None = None,
        guild_id: str | None = None,
        channel_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[DiscordAccessRequest], int]:
        status_value = status.value if status else None
        rows = await self._repo.list_requests(
            status=status_value,
            guild_id=guild_id,
            channel_id=channel_id,
            limit=limit,
            offset=offset,
        )
        total = await self._repo.count_requests(
            status=status_value, guild_id=guild_id, channel_id=channel_id
        )
        return rows, total

    async def list_due(self, *, now: datetime, limit: int) -> Sequence[DiscordAccessRequest]:
        return await self._repo.list_due(now=now, limit=limit)

    # ------------------------------------------------------------------- creating --
    async def create_pending(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        channel_name: str | None,
        collect_history_on_grant: bool = True,
        monitor_on_grant: bool = False,
        keyword_config: KeywordConfig | None = None,
        requested_by: str | None = None,
        note: str | None = None,
    ) -> DiscordAccessRequest:
        """Create a PENDING request scheduled for its first re-check in 12 hours."""

        now = utcnow()
        request = await self._repo.create(
            channel_id=str(channel_id),
            guild_id=guild_id,
            channel_name=channel_name,
            requested_at=now,
            next_check_at=self.next_check_at(from_time=now),
            expires_at=self.expiry_at(from_time=now),
            collect_history_on_grant=collect_history_on_grant,
            monitor_on_grant=monitor_on_grant,
            keyword_config_json=(
                keyword_config.to_json()
                if keyword_config and keyword_config.enabled
                else None
            ),
            requested_by=requested_by,
            note=note,
        )
        logger.info(
            "Access request created",
            extra={
                "request_id": request.id,
                "channel_id": request.channel_id,
                "guild_id": request.guild_id,
                "next_check_at": request.next_check_at.isoformat()
                if request.next_check_at
                else None,
            },
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_REQUEST_CREATED,
            f"Access request created for channel {channel_name or channel_id}.",
            access_request_id=request.id,
            guild_id=guild_id,
            channel_id=str(channel_id),
            payload={"next_check_at": request.next_check_at, "status": request.status},
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_PENDING,
            PENDING_MESSAGE,
            access_request_id=request.id,
            guild_id=guild_id,
            channel_id=str(channel_id),
            payload={"next_check_at": request.next_check_at},
        )
        return request

    # ---------------------------------------------------------------- transitions --
    async def mark_accepted(self, request: DiscordAccessRequest) -> DiscordAccessRequest:
        ensure_transition(
            ACCESS_REQUEST_TRANSITIONS,
            AccessRequestStatus(request.status),
            AccessRequestStatus.ACCEPTED,
        )
        now = utcnow()
        await self._repo.apply_status(
            request, status=AccessRequestStatus.ACCEPTED, now=now, next_check_at=None
        )
        logger.info(
            "Access request accepted",
            extra={
                "request_id": request.id,
                "channel_id": request.channel_id,
                "check_count": request.check_count,
            },
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_GRANTED,
            (
                f"A Discord administrator granted the bot access to channel "
                f"{request.channel_name or request.channel_id}."
            ),
            access_request_id=request.id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            payload={"accepted_at": now, "check_count": request.check_count},
        )
        return request

    async def mark_denied(
        self, request: DiscordAccessRequest, *, reason: str
    ) -> DiscordAccessRequest:
        """Record an explicit, non-transient denial (for example the channel is gone)."""

        ensure_transition(
            ACCESS_REQUEST_TRANSITIONS,
            AccessRequestStatus(request.status),
            AccessRequestStatus.DENIED,
        )
        await self._repo.apply_status(
            request,
            status=AccessRequestStatus.DENIED,
            now=utcnow(),
            next_check_at=None,
            rejection_reason=reason,
        )
        logger.info(
            "Access request denied",
            extra={"request_id": request.id, "channel_id": request.channel_id, "reason": reason},
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_DENIED,
            f"Access request for channel {request.channel_id} was closed: {reason}",
            access_request_id=request.id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            payload={"reason": reason},
        )
        return request

    async def mark_expired(self, request: DiscordAccessRequest) -> DiscordAccessRequest:
        ensure_transition(
            ACCESS_REQUEST_TRANSITIONS,
            AccessRequestStatus(request.status),
            AccessRequestStatus.EXPIRED,
        )
        await self._repo.apply_status(
            request,
            status=AccessRequestStatus.EXPIRED,
            now=utcnow(),
            next_check_at=None,
            rejection_reason="No administrator granted access before the request expired.",
        )
        logger.info("Access request expired", extra={"request_id": request.id})
        await self._notifications.emit(
            NotificationEvent.ACCESS_DENIED,
            f"Access request for channel {request.channel_id} expired.",
            access_request_id=request.id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            payload={"expired_at": request.expires_at},
        )
        return request

    async def cancel(self, request: DiscordAccessRequest) -> DiscordAccessRequest:
        ensure_transition(
            ACCESS_REQUEST_TRANSITIONS,
            AccessRequestStatus(request.status),
            AccessRequestStatus.CANCELLED,
        )
        await self._repo.apply_status(
            request,
            status=AccessRequestStatus.CANCELLED,
            now=utcnow(),
            next_check_at=None,
            rejection_reason="Cancelled by an API caller.",
        )
        logger.info(
            "Access request cancelled",
            extra={"request_id": request.id, "channel_id": request.channel_id},
        )
        return request

    async def record_still_pending(
        self, request: DiscordAccessRequest, *, evaluation: AccessEvaluation
    ) -> DiscordAccessRequest:
        """The channel is still inaccessible; keep waiting and reschedule."""

        now = utcnow()
        await self._repo.record_check_result(
            request,
            now=now,
            next_check_at=self.next_check_at(from_time=now),
            last_error=evaluation.detail,
        )
        logger.info(
            "Access request still pending",
            extra={
                "request_id": request.id,
                "channel_id": request.channel_id,
                "reason": evaluation.reason.value,
                "check_count": request.check_count,
                "next_check_at": request.next_check_at.isoformat()
                if request.next_check_at
                else None,
            },
        )
        return request

    async def record_check_failure(
        self, request: DiscordAccessRequest, *, evaluation: AccessEvaluation
    ) -> DiscordAccessRequest:
        """A transient failure: stay PENDING, reschedule, and notify."""

        now = utcnow()
        await self._repo.record_check_result(
            request,
            now=now,
            next_check_at=self.next_check_at(from_time=now),
            last_error=evaluation.detail or evaluation.reason.value,
        )
        await self._notifications.emit(
            NotificationEvent.ACCESS_CHECK_FAILED,
            (
                f"Access check for channel {request.channel_id} could not complete: "
                f"{evaluation.reason.value}."
            ),
            access_request_id=request.id,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            payload={"reason": evaluation.reason.value, "detail": evaluation.detail},
        )
        logger.warning(
            "Access check failed transiently",
            extra={
                "request_id": request.id,
                "channel_id": request.channel_id,
                "reason": evaluation.reason.value,
            },
        )
        return request

    # ---------------------------------------------------------------- concurrency --
    async def claim(self, request_id: int, *, force: bool = False) -> bool:
        """Atomically claim a request so only one checker processes it."""

        now = utcnow()
        if force:
            return await self._repo.force_claim(request_id=request_id, now=now)
        return await self._repo.claim_for_check(
            request_id=request_id, now=now, next_check_at=self.next_check_at(from_time=now)
        )

    async def list_expired_candidates(
        self, *, now: datetime, limit: int = 100
    ) -> Sequence[DiscordAccessRequest]:
        return await self._repo.list_expired_candidates(now=now, limit=limit)


class AccessWorkflowService:
    """Coordinates evaluation, requests, collection and monitoring."""

    #: Guards against the scheduler and a manual re-check touching one request at once
    #: inside a single process. The database claim guards across processes.
    _channel_locks: dict[str, asyncio.Lock] = {}

    def __init__(
        self,
        *,
        settings: Settings,
        channel_service: ChannelService,
        access_request_service: AccessRequestService,
        message_service: MessageService,
        monitor_service: MonitorService,
        notification_service: NotificationService,
    ) -> None:
        self._settings = settings
        self._channels = channel_service
        self._requests = access_request_service
        self._messages = message_service
        self._monitors = monitor_service
        self._notifications = notification_service

    @classmethod
    def _lock_for(cls, channel_id: str) -> asyncio.Lock:
        lock = cls._channel_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            cls._channel_locks[channel_id] = lock
        return lock

    # ------------------------------------------------------------ request creation --
    async def request_access(
        self,
        channel_id: str,
        *,
        collect_history_on_grant: bool = True,
        monitor_on_grant: bool = False,
        keyword_config: KeywordConfig | None = None,
        requested_by: str | None = None,
        note: str | None = None,
        history_limit: int | None = None,
    ) -> AccessRequestOutcome:
        """Handle ``POST /discord/channels/{id}/access-request``.

        If the bot can already read the channel no pending request is created -- the
        outcome simply reports that access is available.
        """

        cid = validate_snowflake(channel_id, field="channel_id")

        async with self._lock_for(cid):
            channel, evaluation = await self._channels.refresh_access(cid)

            if evaluation.reason is AccessReason.CHANNEL_NOT_FOUND:
                raise NotFoundError(
                    "Channel does not exist or is not visible to this bot",
                    details={"channel_id": cid},
                )

            # Already accessible: do not create an unnecessary pending request.
            if evaluation.collection_allowed:
                existing = await self._requests.get_open_for_channel(cid)
                if existing is not None:
                    await self._accept_and_run_pipeline(
                        existing, evaluation, history_limit=history_limit
                    )
                    return AccessRequestOutcome(
                        channel_id=cid,
                        guild_id=evaluation.guild_id,
                        channel_name=evaluation.channel_name,
                        channel_status=evaluation.access_status,
                        request=existing,
                        evaluation=evaluation,
                        message="Access is already available; the pending request was accepted.",
                        already_accessible=True,
                        transitioned=True,
                    )
                return AccessRequestOutcome(
                    channel_id=cid,
                    guild_id=evaluation.guild_id,
                    channel_name=evaluation.channel_name,
                    channel_status=evaluation.access_status,
                    request=None,
                    evaluation=evaluation,
                    message="The bot already has access to this channel; no request needed.",
                    already_accessible=True,
                )

            if evaluation.transient:
                raise _transient_error(evaluation)

            # Inaccessible: reuse an open request rather than creating a duplicate.
            existing = await self._requests.get_open_for_channel(cid)
            if existing is not None:
                logger.info(
                    "Reusing existing pending access request",
                    extra={"request_id": existing.id, "channel_id": cid},
                )
                return AccessRequestOutcome(
                    channel_id=cid,
                    guild_id=existing.guild_id,
                    channel_name=existing.channel_name,
                    channel_status=ChannelAccessStatus.PRIVATE,
                    request=existing,
                    evaluation=evaluation,
                    message=PENDING_MESSAGE,
                    created=False,
                )

            request = await self._requests.create_pending(
                channel_id=cid,
                guild_id=evaluation.guild_id or channel.guild_id,
                channel_name=evaluation.channel_name or channel.name,
                collect_history_on_grant=collect_history_on_grant,
                monitor_on_grant=monitor_on_grant,
                keyword_config=keyword_config,
                requested_by=requested_by,
                note=note,
            )
            return AccessRequestOutcome(
                channel_id=cid,
                guild_id=request.guild_id,
                channel_name=request.channel_name,
                channel_status=ChannelAccessStatus.PRIVATE,
                request=request,
                evaluation=evaluation,
                message=PENDING_MESSAGE,
                created=True,
            )

    # ----------------------------------------------------------------- re-checking --
    async def recheck_request(
        self, request_id: int, *, force: bool = True, history_limit: int | None = None
    ) -> AccessRequestOutcome:
        """Re-evaluate one access request now (manual re-check or worker tick)."""

        request = await self._requests.get(request_id)
        status = AccessRequestStatus(request.status)

        if status is not AccessRequestStatus.PENDING:
            evaluation = await self._channels.evaluate_only(request.channel_id)
            return AccessRequestOutcome(
                channel_id=request.channel_id,
                guild_id=request.guild_id,
                channel_name=request.channel_name,
                channel_status=evaluation.access_status,
                request=request,
                evaluation=evaluation,
                message=f"Request is {status.value}; no re-check performed.",
            )

        async with self._lock_for(request.channel_id):
            claimed = await self._requests.claim(request_id, force=force)
            if not claimed:
                # Another checker holds this request. Report current stored state
                # rather than calling Discord a second time for the same work.
                logger.info(
                    "Access request is already being checked elsewhere; skipping",
                    extra={"request_id": request_id},
                )
                stored = await self._channels.find_stored_channel(request.channel_id)
                channel_status = (
                    ChannelAccessStatus(stored.access_status)
                    if stored is not None
                    else ChannelAccessStatus.UNKNOWN
                )
                return AccessRequestOutcome(
                    channel_id=request.channel_id,
                    guild_id=request.guild_id,
                    channel_name=request.channel_name,
                    channel_status=channel_status,
                    request=request,
                    evaluation=AccessEvaluation(channel_id=request.channel_id),
                    message="Another check for this request is already in progress.",
                )

            return await self._evaluate_pending_request(
                request, history_limit=history_limit
            )

    async def _evaluate_pending_request(
        self, request: DiscordAccessRequest, *, history_limit: int | None = None
    ) -> AccessRequestOutcome:
        """Core of the reconciliation: evaluate, transition, run follow-ups."""

        _channel, evaluation = await self._channels.refresh_access(request.channel_id)

        # 1. The check itself failed (rate limit, Discord outage, bad token) -> stay
        # PENDING and notify. This is never a state downgrade, and it must not be
        # mistaken for "an administrator has simply not granted access yet".
        if evaluation.transient or evaluation.access_status is ChannelAccessStatus.ERROR:
            await self._requests.record_check_failure(request, evaluation=evaluation)
            return self._outcome(
                request,
                evaluation,
                "Access could not be verified because the check failed "
                f"({evaluation.reason.value}); the request stays pending.",
            )

        # 2. Explicit, permanent denial -> close the request honestly.
        if evaluation.reason is AccessReason.CHANNEL_NOT_FOUND:
            await self._requests.mark_denied(
                request, reason="Channel no longer exists or is invisible to the bot."
            )
            return self._outcome(
                request, evaluation, "Channel no longer exists; request closed as DENIED."
            )
        if evaluation.reason is AccessReason.NOT_A_TEXT_CHANNEL:
            await self._requests.mark_denied(
                request, reason="Channel type cannot hold collectable messages."
            )
            return self._outcome(
                request, evaluation, "Channel cannot hold messages; request closed as DENIED."
            )

        # 3. Access granted -> accept and run the follow-up pipeline.
        if evaluation.collection_allowed:
            outcome = await self._accept_and_run_pipeline(
                request, evaluation, history_limit=history_limit
            )
            return outcome

        # 4. Still waiting for an administrator.
        if request.expires_at is not None and request.expires_at <= utcnow():
            await self._requests.mark_expired(request)
            return self._outcome(
                request, evaluation, "Request expired before access was granted."
            )

        await self._requests.record_still_pending(request, evaluation=evaluation)
        return self._outcome(request, evaluation, PENDING_MESSAGE)

    async def _accept_and_run_pipeline(
        self,
        request: DiscordAccessRequest,
        evaluation: AccessEvaluation,
        *,
        history_limit: int | None = None,
    ) -> AccessRequestOutcome:
        """PENDING -> ACCEPTED, then history collection and monitor activation."""

        await self._requests.mark_accepted(request)

        keyword_config = KeywordConfig.from_json(request.keyword_config_json)
        scrape: ScrapeResult | None = None
        monitor: DiscordMonitor | None = None

        if request.collect_history_on_grant:
            scrape = await self._messages.collect_history(
                request.channel_id,
                guild_id=evaluation.guild_id or request.guild_id,
                limit=history_limit,
                keyword_config=keyword_config,
            )

        if request.monitor_on_grant:
            monitor = await self._monitors.ensure_monitor(
                channel_id=request.channel_id,
                guild_id=evaluation.guild_id or request.guild_id,
                keyword_config=keyword_config,
            )
            await self._monitors.start(monitor)

        return AccessRequestOutcome(
            channel_id=request.channel_id,
            guild_id=request.guild_id,
            channel_name=request.channel_name,
            channel_status=evaluation.access_status,
            request=request,
            evaluation=evaluation,
            message="Access granted by a Discord administrator; collection has run.",
            transitioned=True,
            scrape=scrape,
            monitor=monitor,
        )

    # --------------------------------------------------------------- scraping API --
    async def scrape_channel(
        self,
        channel_id: str,
        *,
        limit: int | None = None,
        before: str | None = None,
        after: str | None = None,
        keyword_config: KeywordConfig | None = None,
        incremental: bool = False,
    ) -> tuple[ScrapeResult, AccessEvaluation]:
        """Run historical collection, refusing if access is not available."""

        cid = validate_snowflake(channel_id, field="channel_id")
        _channel, evaluation = await self._channels.refresh_access(cid)

        if not evaluation.collection_allowed:
            from app.core.exceptions import AccessNotGrantedError

            raise AccessNotGrantedError(
                "The bot cannot collect from this channel yet",
                details={
                    "channel_id": cid,
                    "access_status": evaluation.access_status.value,
                    "reason": evaluation.reason.value,
                    "hint": (
                        "Create an access request and ask a Discord server "
                        "administrator to grant the bot access."
                    ),
                },
            )

        if incremental and after is None:
            after = await self._messages.newest_stored_message_id(cid)

        result = await self._messages.collect_history(
            cid,
            guild_id=evaluation.guild_id,
            limit=limit,
            before=before,
            after=after,
            keyword_config=keyword_config,
        )
        return result, evaluation

    # -------------------------------------------------------------- monitoring API --
    async def start_monitor(
        self,
        channel_id: str,
        *,
        keyword_config: KeywordConfig | None = None,
        collect_history: bool = True,
        history_limit: int | None = None,
        store_all_messages: bool = True,
        requested_by: str | None = None,
    ) -> tuple[DiscordMonitor, AccessRequestOutcome | None, ScrapeResult | None]:
        """Start monitoring, parking the monitor if access is not available yet.

        Returns ``(monitor, access_outcome, scrape_result)``.  ``access_outcome`` is set
        when an access request was created or reused because the channel is private.
        """

        cid = validate_snowflake(channel_id, field="channel_id")

        async with self._lock_for(cid):
            channel, evaluation = await self._channels.refresh_access(cid)

            if evaluation.reason is AccessReason.CHANNEL_NOT_FOUND:
                raise NotFoundError(
                    "Channel does not exist or is not visible to this bot",
                    details={"channel_id": cid},
                )
            if evaluation.transient:
                raise _transient_error(evaluation)

            monitor = await self._monitors.ensure_monitor(
                channel_id=cid,
                guild_id=evaluation.guild_id or channel.guild_id,
                keyword_config=keyword_config,
                store_all_messages=store_all_messages,
            )

            if not evaluation.collection_allowed:
                # private channel -> monitor requested -> WAITING_FOR_ACCESS
                await self._monitors.mark_waiting_for_access(monitor)
                outcome = await self._ensure_pending_request(
                    cid,
                    evaluation=evaluation,
                    channel_guild_id=channel.guild_id,
                    channel_name=evaluation.channel_name or channel.name,
                    keyword_config=keyword_config,
                    collect_history_on_grant=collect_history,
                    requested_by=requested_by,
                )
                return monitor, outcome, None

            scrape: ScrapeResult | None = None
            if collect_history:
                scrape = await self._messages.collect_history(
                    cid,
                    guild_id=evaluation.guild_id,
                    limit=history_limit,
                    keyword_config=keyword_config,
                )

            await self._monitors.start(monitor)
            return monitor, None, scrape

    async def _ensure_pending_request(
        self,
        channel_id: str,
        *,
        evaluation: AccessEvaluation,
        channel_guild_id: str | None,
        channel_name: str | None,
        keyword_config: KeywordConfig | None,
        collect_history_on_grant: bool,
        requested_by: str | None,
    ) -> AccessRequestOutcome:
        """Create (or reuse) the pending request that a waiting monitor depends on."""

        existing = await self._requests.get_open_for_channel(channel_id)
        if existing is not None:
            existing.monitor_on_grant = True
            if keyword_config is not None and keyword_config.enabled:
                existing.keyword_config_json = keyword_config.to_json()
            return AccessRequestOutcome(
                channel_id=channel_id,
                guild_id=existing.guild_id,
                channel_name=existing.channel_name,
                channel_status=ChannelAccessStatus.PRIVATE,
                request=existing,
                evaluation=evaluation,
                message=PENDING_MESSAGE,
            )

        request = await self._requests.create_pending(
            channel_id=channel_id,
            guild_id=evaluation.guild_id or channel_guild_id,
            channel_name=channel_name,
            collect_history_on_grant=collect_history_on_grant,
            monitor_on_grant=True,
            keyword_config=keyword_config,
            requested_by=requested_by,
            note="Created automatically because monitoring was requested.",
        )
        return AccessRequestOutcome(
            channel_id=channel_id,
            guild_id=request.guild_id,
            channel_name=request.channel_name,
            channel_status=ChannelAccessStatus.PRIVATE,
            request=request,
            evaluation=evaluation,
            message=PENDING_MESSAGE,
            created=True,
        )

    async def stop_monitor(self, channel_id: str, *, reason: str | None = None) -> DiscordMonitor:
        monitor = await self._monitors.require_by_channel(
            validate_snowflake(channel_id, field="channel_id")
        )
        return await self._monitors.stop(monitor, reason=reason)

    # -------------------------------------------------------------- gateway signal --
    async def handle_permission_signal(self, channel_id: str) -> AccessRequestOutcome | None:
        """Fast-path re-check triggered by a Gateway permission/channel event.

        The 12-hour reconciler stays authoritative; this only shortens the wait.
        """

        request = await self._requests.get_open_for_channel(str(channel_id))
        if request is None:
            return None
        logger.info(
            "Gateway signalled a permission change; re-checking access early",
            extra={"channel_id": channel_id, "request_id": request.id},
        )
        return await self.recheck_request(request.id, force=True)

    # -------------------------------------------------------------------- helpers --
    def _outcome(
        self,
        request: DiscordAccessRequest,
        evaluation: AccessEvaluation,
        message: str,
    ) -> AccessRequestOutcome:
        return AccessRequestOutcome(
            channel_id=request.channel_id,
            guild_id=request.guild_id,
            channel_name=request.channel_name,
            channel_status=evaluation.access_status,
            request=request,
            evaluation=evaluation,
            message=message,
        )

    async def cancel_request(self, request_id: int) -> DiscordAccessRequest:
        request = await self._requests.get(request_id)
        cancelled = await self._requests.cancel(request)
        monitor = await self._monitors.get_by_channel(request.channel_id)
        if monitor is not None and monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value:
            await self._monitors.stop(
                monitor, reason="Associated access request was cancelled."
            )
        return cancelled


def _transient_error(evaluation: AccessEvaluation) -> Exception:
    """Translate a transient evaluation into the right API-facing exception."""

    from app.core.exceptions import DiscordRateLimitError, DiscordServerError

    if evaluation.reason is AccessReason.RATE_LIMITED:
        return DiscordRateLimitError(
            "Discord rate limited this request; try again shortly"
        )
    return DiscordServerError("Discord is temporarily unavailable; try again shortly")
