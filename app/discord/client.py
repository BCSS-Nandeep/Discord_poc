"""Composition root for the Discord integration.

Owns the long-lived objects (REST client, Gateway) and builds per-session service
graphs.  Gateway callbacks land here, where each one opens its own database session --
Gateway events run on discord.py's loop and must never borrow an HTTP request's session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.enums import AccessRequestStatus, MonitorStatus, NotificationEvent
from app.core.logging import get_logger
from app.database.database import Database
from app.database.repositories.access_request_repository import AccessRequestRepository
from app.database.repositories.channel_repository import ChannelRepository
from app.database.repositories.guild_repository import GuildRepository
from app.database.repositories.message_repository import MessageRepository
from app.database.repositories.monitor_repository import MonitorRepository
from app.database.repositories.notification_repository import NotificationRepository
from app.database.repositories.scrape_job_repository import ScrapeJobRepository
from app.database.repositories.user_repository import UserRepository
from app.discord.access_request_service import (
    AccessRequestService,
    AccessWorkflowService,
)
from app.discord.channel_service import ChannelService
from app.discord.gateway import RECHECK_ALL_PENDING, GatewayService
from app.discord.guild_service import GuildService
from app.discord.keywords import KeywordConfig, KeywordMatcher
from app.discord.message_service import MessageService
from app.discord.monitor_service import MonitorService
from app.discord.normalize import parse_timestamp
from app.discord.notification_service import NotificationService
from app.discord.oauth_service import DiscordOAuthService
from app.discord.permission_service import PermissionService
from app.discord.rest_client import DiscordRestClient
from app.discord.scrape_job_service import ScrapeJobService
from app.discord.search_service import SearchService

logger = get_logger(__name__)


@dataclass(slots=True)
class ServiceGraph:
    """Every service, bound to one database session."""

    session: AsyncSession
    notifications: NotificationService
    permissions: PermissionService
    guilds: GuildService
    channels: ChannelService
    messages: MessageService
    search: SearchService
    monitors: MonitorService
    access_requests: AccessRequestService
    workflow: AccessWorkflowService
    oauth: DiscordOAuthService
    scrape_jobs: ScrapeJobService


class DiscordClientManager:
    """Long-lived Discord integration owned by the FastAPI application."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        rest_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        # ``rest_transport`` is the seam tests use to stand in for the Discord API.
        self.rest_client = DiscordRestClient(settings, transport=rest_transport)
        self.gateway = GatewayService(
            settings,
            on_monitored_message=self._handle_gateway_message,
            on_permission_signal=self._handle_permission_signal,
        )
        self._bot_identity: dict[str, Any] | None = None

    # ---------------------------------------------------------------- composition --
    def build_services(self, session: AsyncSession) -> ServiceGraph:
        """Build the service graph for one unit of work."""

        notifications = NotificationService(NotificationRepository(session))
        permissions = PermissionService(self.rest_client, self._settings)
        guilds = GuildService(self.rest_client, GuildRepository(session))
        channels = ChannelService(
            self.rest_client, ChannelRepository(session), permissions
        )
        messages = MessageService(
            self.rest_client, MessageRepository(session), self._settings
        )
        search = SearchService(MessageRepository(session))
        monitors = MonitorService(MonitorRepository(session), notifications)
        access_requests = AccessRequestService(
            AccessRequestRepository(session), notifications, self._settings
        )
        workflow = AccessWorkflowService(
            settings=self._settings,
            channel_service=channels,
            guild_service=guilds,
            access_request_service=access_requests,
            message_service=messages,
            monitor_service=monitors,
            notification_service=notifications,
        )
        oauth = DiscordOAuthService(
            self._settings, self.rest_client, UserRepository(session)
        )
        scrape_jobs = ScrapeJobService(ScrapeJobRepository(session))
        return ServiceGraph(
            session=session,
            notifications=notifications,
            permissions=permissions,
            guilds=guilds,
            channels=channels,
            messages=messages,
            search=search,
            monitors=monitors,
            access_requests=access_requests,
            workflow=workflow,
            oauth=oauth,
            scrape_jobs=scrape_jobs,
        )

    # ------------------------------------------------------------------ lifecycle --
    async def start_rest(self) -> None:
        await self.rest_client.start()

    async def start_gateway(self) -> None:
        """Start the Gateway after seeding the monitored-channel set from SQLite."""

        await self.refresh_monitored_channels()
        await self.gateway.start()

    async def close(self) -> None:
        await self.gateway.stop()
        await self.rest_client.close()

    async def refresh_monitored_channels(self) -> set[str]:
        """Reload the RUNNING monitors so the Gateway knows what to collect."""

        async with self._database.session() as session:
            services = self.build_services(session)
            channel_ids = set(await services.monitors.running_channel_ids())
        self.gateway.set_monitored_channels(channel_ids)
        return channel_ids

    # ------------------------------------------------------------------- identity --
    async def verify_bot(self, *, refresh: bool = False) -> dict[str, Any]:
        """Fetch and cache the bot identity. Never returns or logs the token."""

        if self._bot_identity is not None and not refresh:
            return self._bot_identity
        payload = await self.rest_client.get_current_bot()
        self._bot_identity = {
            "id": str(payload.get("id")),
            "username": payload.get("username"),
            "global_name": payload.get("global_name"),
            "discriminator": payload.get("discriminator"),
            "bot": bool(payload.get("bot", True)),
            "avatar_url": (
                f"https://cdn.discordapp.com/avatars/{payload.get('id')}/"
                f"{payload.get('avatar')}.png"
                if payload.get("avatar")
                else None
            ),
        }
        logger.info(
            "Discord bot authenticated",
            extra={
                "bot_id": self._bot_identity["id"],
                "bot_username": self._bot_identity["username"],
            },
        )
        return self._bot_identity

    @property
    def bot_identity(self) -> dict[str, Any] | None:
        return self._bot_identity

    # ------------------------------------------------------------ gateway handlers --
    async def _handle_gateway_message(
        self, payload: dict[str, Any], channel_id: str, guild_id: str | None
    ) -> None:
        """Persist a live message and update the monitor's counters."""

        async with self._database.session() as session:
            services = self.build_services(session)
            monitor = await services.monitors.get_by_channel(channel_id)
            if monitor is None or monitor.status != MonitorStatus.RUNNING.value:
                # The monitor was stopped between the event and this handler.
                self.gateway.remove_monitored_channel(channel_id)
                return

            matcher = KeywordMatcher(services.monitors.keyword_config_for(monitor))
            matched = matcher.match(payload.get("content"))

            stored = False
            if matcher.should_store(matched):
                stored = await services.messages.store_live_message(
                    payload,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    matched_keywords=matched,
                )

            occurred_at = parse_timestamp(payload.get("timestamp"))
            await services.monitors.record_message_event(
                monitor,
                message_id=str(payload.get("id")),
                occurred_at=occurred_at or monitor.updated_at,
                matched=bool(matched),
            )

            if matched:
                await services.notifications.emit(
                    NotificationEvent.MESSAGE_MATCHED,
                    (
                        f"Message {payload.get('id')} in channel {channel_id} matched "
                        f"{len(matched)} keyword(s)."
                    ),
                    guild_id=guild_id,
                    channel_id=channel_id,
                    payload={
                        "message_id": str(payload.get("id")),
                        "matched_keywords": matched,
                        "stored": stored,
                    },
                )

            logger.debug(
                "Live message processed",
                extra={
                    "channel_id": channel_id,
                    "message_id": str(payload.get("id")),
                    "matched": bool(matched),
                    "stored": stored,
                },
            )

    async def run_scrape_job(self, job_id: int) -> None:
        """Run a queued background scrape job to completion.

        Scheduled via FastAPI's ``BackgroundTasks`` from the endpoint that creates the
        job, so this runs *after* that request's own session has already been
        committed and closed -- it must open its own, exactly like the Gateway
        handlers above, never reuse a session tied to a request that has finished.
        """

        async with self._database.session() as session:
            services = self.build_services(session)
            job = await services.scrape_jobs.get(job_id)
            await services.scrape_jobs.mark_running(job)
            keyword_config = KeywordConfig.from_json(job.keyword_config_json)

        async def on_page(result):  # noqa: ANN001 - local closure, type is ScrapeResult
            async with self._database.session() as page_session:
                page_services = self.build_services(page_session)
                fresh_job = await page_services.scrape_jobs.get(job_id)
                await page_services.scrape_jobs.record_progress(fresh_job, result)

        try:
            async with self._database.session() as session:
                services = self.build_services(session)
                job = await services.scrape_jobs.get(job_id)
                result, _evaluation = await services.workflow.scrape_channel(
                    job.channel_id,
                    limit=job.requested_limit,
                    before=job.before_cursor,
                    after=job.after_cursor,
                    incremental=job.incremental,
                    keyword_config=keyword_config,
                    on_page=on_page,
                )
                await services.scrape_jobs.mark_completed(job, result)
            logger.info(
                "Scrape job finished",
                extra={"job_id": job_id, "stored": result.stored, "completed": result.completed},
            )
        except Exception as exc:  # noqa: BLE001 - a job failure must not crash the app
            logger.exception("Scrape job failed", extra={"job_id": job_id})
            async with self._database.session() as session:
                services = self.build_services(session)
                job = await services.scrape_jobs.get(job_id)
                # Sanitized: never leak internals through a job's stored error message.
                message = (
                    exc.message  # type: ignore[attr-defined]
                    if hasattr(exc, "message")
                    else f"{type(exc).__name__} while scraping"
                )
                await services.scrape_jobs.mark_failed(job, error_message=str(message))

    async def _handle_permission_signal(self, channel_id: str) -> None:
        """Fast-path access re-check driven by a Gateway permission event."""

        if channel_id == RECHECK_ALL_PENDING:
            async with self._database.session() as session:
                services = self.build_services(session)
                pending, _total = await services.access_requests.list_requests(
                    status=AccessRequestStatus.PENDING, limit=50
                )
                targets = [request.channel_id for request in pending]
        else:
            targets = [channel_id]

        granted = False
        for target in targets:
            try:
                async with self._database.session() as session:
                    services = self.build_services(session)
                    outcome = await services.workflow.handle_permission_signal(target)
                if outcome is not None and outcome.transitioned:
                    granted = True
            except Exception:  # noqa: BLE001 - a signal must never kill the Gateway
                logger.exception(
                    "Fast-path access re-check failed", extra={"channel_id": target}
                )

        if granted:
            await self.refresh_monitored_channels()
