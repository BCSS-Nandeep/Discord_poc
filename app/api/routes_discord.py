"""Discord API routes.

Routes validate input, delegate to a service, and shape the response.  All business
rules -- permission evaluation, the access workflow, collection, monitoring -- live in
``app/discord/*_service.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Path, Query, status

from app.api.deps import (
    ClientsDep,
    DiscordConfiguredDep,
    PaginationDep,
    ServicesDep,
    SettingsDep,
    get_worker,
)
from app.core.enums import AccessRequestStatus, MonitorStatus, NotificationEvent
from app.core.exceptions import AccessNotGrantedError, NotFoundError
from app.core.logging import get_logger
from app.core.security import api_key_scheme, require_api_key
from app.discord.access_request_service import AccessRequestOutcome
from app.discord.keywords import KeywordConfig
from app.discord.message_service import ScrapeResult
from app.schemas.access_request import (
    AccessRequestCreate,
    AccessRequestOutcomeResponse,
    AccessRequestResponse,
    ScrapeSummary,
)
from app.schemas.channel import (
    AccessEvaluationResponse,
    ChannelDetailResponse,
    ChannelResponse,
)
from app.schemas.common import ErrorResponse, Page, Pagination
from app.schemas.guild import GuildMonitoringToggleRequest, GuildResponse
from app.schemas.health import BotResponse, DiscordHealthResponse
from app.schemas.message import (
    KeywordFilter,
    MessageResponse,
    ScrapeJobResponse,
    ScrapeRequest,
    SearchHitResponse,
    SearchRequest,
)
from app.schemas.monitor import (
    MonitorResponse,
    MonitorStartRequest,
    MonitorStartResponse,
    MonitorStatusResponse,
    MonitorStopRequest,
)
from app.schemas.notification import NotificationResponse

logger = get_logger(__name__)

# Authentication is enforced for the whole Discord surface. `/health` and `/ui` stay
# open so probes and the console page itself keep working without a key.
router = APIRouter(
    prefix="/discord",
    tags=["discord"],
    dependencies=[Depends(require_api_key), Depends(api_key_scheme)],
)

ChannelIdPath = Annotated[
    str,
    Path(
        pattern=r"^\d{1,20}$",
        description="Discord channel snowflake.",
        examples=["223456789012345678"],
    ),
]
GuildIdPath = Annotated[
    str,
    Path(
        pattern=r"^\d{1,20}$",
        description="Discord guild snowflake.",
        examples=["123456789012345678"],
    ),
]
RequestIdPath = Annotated[int, Path(ge=1, description="Access request id.")]

COMMON_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or invalid X-API-Key."},
    404: {"model": ErrorResponse, "description": "Resource not found."},
    409: {"model": ErrorResponse, "description": "Conflicting state."},
    429: {"model": ErrorResponse, "description": "Discord rate limit reached."},
    502: {"model": ErrorResponse, "description": "Discord API error."},
    503: {"model": ErrorResponse, "description": "Discord is not configured."},
}


# ======================================================================== helpers ==
def _keyword_config(filter_: KeywordFilter | None) -> KeywordConfig | None:
    if filter_ is None:
        return None
    return KeywordConfig.from_dict(filter_.model_dump())


def _keyword_filter(config: KeywordConfig) -> KeywordFilter:
    return KeywordFilter(
        keywords=config.keywords,
        match_mode=config.match_mode,
        case_sensitive=config.case_sensitive,
        store_non_matching=config.store_non_matching,
    )


def _scrape_summary(result: ScrapeResult | None) -> ScrapeSummary | None:
    if result is None:
        return None
    return ScrapeSummary(
        channel_id=result.channel_id,
        fetched=result.fetched,
        stored=result.stored,
        duplicates=result.duplicates,
        matched=result.matched,
        skipped_non_matching=result.skipped_non_matching,
        pages=result.pages,
        oldest_message_id=result.oldest_message_id,
        newest_message_id=result.newest_message_id,
        completed=result.completed,
        stopped_reason=result.stopped_reason,
        errors=result.errors,
    )


def _outcome_response(outcome: AccessRequestOutcome) -> AccessRequestOutcomeResponse:
    request = outcome.request
    stored = AccessRequestResponse.model_validate(request) if request else None
    return AccessRequestOutcomeResponse(
        channel_id=outcome.channel_id,
        channel_name=outcome.channel_name,
        guild_id=outcome.guild_id,
        channel_status=outcome.channel_status,
        access_request_status=outcome.request_status,
        access_request=stored,
        requested_at=request.requested_at if request else None,
        next_check_at=request.next_check_at if request else None,
        created=outcome.created,
        already_accessible=outcome.already_accessible,
        transitioned=outcome.transitioned,
        message=outcome.message,
        access=AccessEvaluationResponse(**outcome.evaluation.to_dict()),
        scrape=_scrape_summary(outcome.scrape),
    )


def _pagination(total: int, limit: int, offset: int, returned: int) -> Pagination:
    return Pagination(total=total, limit=limit, offset=offset, returned=returned)


# =========================================================================== health ==
@router.get(
    "/health",
    response_model=DiscordHealthResponse,
    summary="Discord subsystem health",
    description=(
        "Reports bot authentication, Gateway status, reconciliation worker status and "
        "stored row counts. The bot token is never included."
    ),
)
async def discord_health(
    settings: SettingsDep,
    clients: ClientsDep,
    services: ServicesDep,
    worker=Depends(get_worker),
) -> DiscordHealthResponse:
    """Detailed status of every Discord-facing component."""

    bot: dict[str, Any] | None = None
    error: str | None = None
    subsystem_status = "ok"

    if not settings.discord_configured:
        subsystem_status = "error"
        error = "DISCORD_BOT_TOKEN is not configured."
    else:
        try:
            bot = await clients.verify_bot(refresh=True)
        except Exception as exc:  # noqa: BLE001 - health must never raise
            subsystem_status = "error"
            error = f"{type(exc).__name__}: could not authenticate with Discord."
            logger.warning("Discord health probe failed", extra={"error": type(exc).__name__})

    gateway = clients.gateway.snapshot()
    if subsystem_status == "ok" and gateway["status"] not in ("connected", "disabled"):
        subsystem_status = "degraded"

    counts = {
        "guilds": await services.guilds.count(),
        "channels": await services.channels.count(),
        "messages": await services.messages.count(),
        "monitors": await services.monitors.count(),
        "running_monitors": await services.monitors.count(status=MonitorStatus.RUNNING),
        "pending_access_requests": (
            await services.access_requests.list_requests(
                status=AccessRequestStatus.PENDING, limit=1
            )
        )[1],
        "unread_notifications": await services.notifications.unread_count(),
    }

    return DiscordHealthResponse(
        status=subsystem_status,  # type: ignore[arg-type]
        discord_configured=settings.discord_configured,
        api_base_url=settings.discord_api_base_url,
        bot=bot,
        gateway=gateway,
        worker=worker.snapshot() if worker else {"enabled": False, "running": False},
        counts=counts,
        message_content_intent=settings.discord_message_content_intent,
        access_recheck_hours=settings.access_recheck_hours,
        error=error,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/bot",
    response_model=BotResponse,
    responses=COMMON_ERRORS,
    summary="Authenticated bot account",
    description="Identity of the bot behind the configured token. The token is never returned.",
)
async def get_bot(
    settings: DiscordConfiguredDep, clients: ClientsDep
) -> BotResponse:
    """Return the bot's Discord identity."""

    identity = await clients.verify_bot(refresh=True)
    return BotResponse(
        **identity, application_id=settings.discord_application_id or identity["id"]
    )


# =========================================================================== guilds ==
@router.get(
    "/guilds",
    response_model=Page[GuildResponse],
    responses=COMMON_ERRORS,
    summary="List guilds",
    description=(
        "Guilds the bot has been invited to. Set `refresh=true` to re-discover "
        "them from Discord before listing."
    ),
)
async def list_guilds(
    services: ServicesDep,
    pagination: PaginationDep,
    _settings: DiscordConfiguredDep,
    refresh: Annotated[
        bool, Query(description="Re-discover guilds from Discord before listing.")
    ] = False,
) -> Page[GuildResponse]:
    """List known guilds."""

    guilds = await services.guilds.list_guilds(
        limit=pagination.limit, offset=pagination.offset, refresh=refresh
    )
    total = await services.guilds.count()
    return Page[GuildResponse](
        items=[GuildResponse.model_validate(guild) for guild in guilds],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(guilds)),
    )


# Registered before "/guilds/{guild_id}" for the same route-ordering reason as
# "/channels/search" above.
@router.get(
    "/guilds/search",
    response_model=Page[GuildResponse],
    responses=COMMON_ERRORS,
    summary="Find guilds by name",
    description=(
        "Search guilds by **name** instead of snowflake id.\n\n"
        "Only servers the bot has been invited to can be found — Discord provides no "
        "way for a bot to discover or search servers it is not a member of."
    ),
)
async def search_guilds(
    services: ServicesDep,
    pagination: PaginationDep,
    query: Annotated[
        str,
        Query(
            min_length=1,
            max_length=200,
            description="Text to look for in the guild name.",
            examples=["Threat Intel"],
        ),
    ],
    refresh: Annotated[
        bool, Query(description="Re-discover guilds from Discord before searching.")
    ] = False,
) -> Page[GuildResponse]:
    """Search known guilds by name."""

    rows, total = await services.guilds.search_by_name(
        query, limit=pagination.limit, offset=pagination.offset, refresh=refresh
    )
    return Page[GuildResponse](
        items=[GuildResponse.model_validate(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


@router.get(
    "/guilds/{guild_id}",
    response_model=GuildResponse,
    responses=COMMON_ERRORS,
    summary="Get a guild",
)
async def get_guild(
    guild_id: GuildIdPath, services: ServicesDep, _settings: DiscordConfiguredDep
) -> GuildResponse:
    """Fetch one guild from Discord and refresh the stored copy."""

    guild = await services.guilds.get_guild(guild_id, refresh=True)
    return GuildResponse.model_validate(guild)


@router.get(
    "/guilds/{guild_id}/channels",
    response_model=Page[ChannelResponse],
    responses=COMMON_ERRORS,
    summary="List a guild's channels",
    description=(
        "Discovers channels and flags private ones from their permission overwrites. "
        "Set `evaluate_access=true` to also probe the bot's effective access to every "
        "text channel (slower: extra Discord calls per channel)."
    ),
)
async def list_guild_channels(
    guild_id: GuildIdPath,
    services: ServicesDep,
    pagination: PaginationDep,
    _settings: DiscordConfiguredDep,
    refresh: Annotated[bool, Query(description="Re-discover from Discord.")] = True,
    evaluate_access: Annotated[
        bool, Query(description="Run a full access evaluation per text channel.")
    ] = False,
    only_private: Annotated[
        bool | None, Query(description="Filter by private/public channels.")
    ] = None,
) -> Page[ChannelResponse]:
    """List the channels of a guild."""

    if refresh:
        await services.channels.discover_guild_channels(
            guild_id, evaluate_access=evaluate_access
        )
    channels = await services.channels.list_channels(
        guild_id,
        limit=pagination.limit,
        offset=pagination.offset,
        is_private=only_private,
        refresh=False,
    )
    return Page[ChannelResponse](
        items=[ChannelResponse.model_validate(channel) for channel in channels],
        pagination=_pagination(
            len(channels), pagination.limit, pagination.offset, len(channels)
        ),
    )


@router.post(
    "/guilds/{guild_id}/monitoring",
    response_model=GuildResponse,
    responses=COMMON_ERRORS,
    summary="Enable or disable monitoring for a guild",
    description=(
        "A guild-level master switch, independent of per-channel access. Disabling it "
        "immediately **stops every RUNNING monitor in the guild** and refuses to start "
        "new ones there until re-enabled. Re-enabling only lifts the block -- it does "
        "not restart monitors that were stopped; start those explicitly via "
        "`POST /discord/channels/{channel_id}/monitor/start`. "
        "The guild must already be known to this service "
        "(`GET /discord/guilds?refresh=true` first)."
    ),
)
async def set_guild_monitoring(
    guild_id: GuildIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    payload: Annotated[GuildMonitoringToggleRequest, Body()],
) -> GuildResponse:
    """Flip the guild-level monitoring master switch."""

    guild = await services.workflow.set_guild_monitoring(guild_id, enabled=payload.enabled)
    await services.session.commit()
    await clients.refresh_monitored_channels()
    return GuildResponse.model_validate(guild)


# ========================================================================= channels ==
# NOTE: this route MUST stay registered before "/channels/{channel_id}". Starlette
# matches routes in declaration order and compiles the path parameter to a generic
# segment, so a later "/channels/search" would be captured as a channel id and
# rejected as a malformed snowflake.
@router.get(
    "/channels/search",
    response_model=Page[ChannelResponse],
    summary="Find channels by name",
    description=(
        "Search discovered channels by **name** instead of snowflake id, for when you "
        "know a channel is called `general` but not its numeric id.\n\n"
        "Matching is case-insensitive substring. This reads channels already "
        "discovered into SQLite — Discord has no channel-name lookup endpoint — so "
        "call `GET /discord/guilds/{guild_id}/channels` first, or pass `guild_id` "
        "together with `refresh=true` to discover as part of this call."
    ),
)
async def search_channels(
    services: ServicesDep,
    pagination: PaginationDep,
    query: Annotated[
        str,
        Query(
            min_length=1,
            max_length=200,
            description="Text to look for in the channel name.",
            examples=["general"],
        ),
    ],
    guild_id: Annotated[
        str | None,
        Query(pattern=r"^\d{1,20}$", description="Restrict the search to one guild."),
    ] = None,
    refresh: Annotated[
        bool, Query(description="Discover the guild's channels first. Needs guild_id.")
    ] = False,
) -> Page[ChannelResponse]:
    """Search stored channels by name."""

    rows, total = await services.channels.search_by_name(
        query,
        guild_id=guild_id,
        limit=pagination.limit,
        offset=pagination.offset,
        refresh=refresh,
    )
    return Page[ChannelResponse](
        items=[ChannelResponse.model_validate(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


@router.get(
    "/channels/{channel_id}",
    response_model=ChannelDetailResponse,
    responses=COMMON_ERRORS,
    summary="Get a channel and its access evaluation",
    description=(
        "Evaluates whether the bot can view the channel and read its history, and "
        "reports the reason when it cannot."
    ),
)
async def get_channel(
    channel_id: ChannelIdPath, services: ServicesDep, _settings: DiscordConfiguredDep
) -> ChannelDetailResponse:
    """Return channel metadata plus a fresh access evaluation."""

    channel, evaluation = await services.channels.refresh_access(channel_id)
    open_request = await services.access_requests.get_open_for_channel(channel_id)
    return ChannelDetailResponse(
        channel=ChannelResponse.model_validate(channel),
        access=AccessEvaluationResponse(**evaluation.to_dict()),
        open_access_request_id=open_request.id if open_request else None,
    )


@router.post(
    "/channels/{channel_id}/access-request",
    response_model=AccessRequestOutcomeResponse,
    status_code=status.HTTP_201_CREATED,
    responses=COMMON_ERRORS,
    summary="Request access to a channel",
    description=(
        "Creates an **application-level** access request for a private channel.\n\n"
        "Discord provides no API for a bot to request channel access and no "
        "Discord-side approval flow. This endpoint records that we are waiting for a "
        "Discord **server administrator** to grant the bot *View Channel* and *Read "
        "Message History*. The request is re-checked automatically every "
        "`ACCESS_RECHECK_HOURS` (12 by default), and Gateway permission events "
        "trigger an earlier check.\n\n"
        "If the bot can already read the channel, no pending request is created."
    ),
)
async def create_access_request(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    _settings: DiscordConfiguredDep,
    payload: Annotated[AccessRequestCreate, Body()] = AccessRequestCreate(),
) -> AccessRequestOutcomeResponse:
    """Create or reuse an access request for a channel."""

    outcome = await services.workflow.request_access(
        channel_id,
        collect_history_on_grant=payload.collect_history_on_grant,
        monitor_on_grant=payload.monitor_on_grant,
        keyword_config=_keyword_config(payload.keywords),
        requested_by=payload.requested_by,
        note=payload.note,
    )
    if outcome.transitioned:
        # Commit before touching the Gateway so it sees the new monitor state.
        await services.session.commit()
        await clients.refresh_monitored_channels()
    return _outcome_response(outcome)


@router.get(
    "/channels/{channel_id}/messages",
    response_model=Page[MessageResponse],
    responses=COMMON_ERRORS,
    summary="List stored messages for a channel",
    description="Reads from SQLite. Use the scrape endpoint to collect from Discord.",
)
async def list_channel_messages(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    pagination: PaginationDep,
    before: Annotated[
        datetime | None, Query(description="Only messages before this timestamp.")
    ] = None,
    after: Annotated[
        datetime | None, Query(description="Only messages after this timestamp.")
    ] = None,
) -> Page[MessageResponse]:
    """List messages already collected for a channel."""

    rows, total = await services.messages.list_stored_messages(
        channel_id,
        limit=pagination.limit,
        offset=pagination.offset,
        before=before,
        after=after,
    )
    return Page[MessageResponse](
        items=[MessageResponse.from_entity(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


@router.post(
    "/channels/{channel_id}/scrape",
    response_model=ScrapeSummary,
    responses=COMMON_ERRORS,
    summary="Collect historical messages",
    description=(
        "Pages backwards through `GET /channels/{id}/messages` (100 per page, Discord's "
        "maximum), normalizes each message and stores it. Duplicates are skipped by the "
        "database's unique constraint, so re-running is safe.\n\n"
        "Returns **409** when the bot does not have access yet -- create an access "
        "request first."
    ),
)
async def scrape_channel(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    _settings: DiscordConfiguredDep,
    payload: Annotated[ScrapeRequest, Body()] = ScrapeRequest(),
) -> ScrapeSummary:
    """Run historical collection for a channel."""

    result, _evaluation = await services.workflow.scrape_channel(
        channel_id,
        limit=payload.limit,
        before=payload.before,
        after=payload.after,
        incremental=payload.incremental,
        keyword_config=_keyword_config(payload.keywords),
    )
    summary = _scrape_summary(result)
    assert summary is not None
    return summary


@router.post(
    "/channels/{channel_id}/scrape/jobs",
    response_model=ScrapeJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=COMMON_ERRORS,
    summary="Queue a background historical collection",
    description=(
        "Returns immediately with status `QUEUED` instead of blocking until the whole "
        "channel has been paged through -- poll `GET .../scrape/jobs/{job_id}` for "
        "progress. Access is checked before the job is queued, so a channel the bot "
        "cannot read still fails fast with **409** rather than queuing a job doomed to "
        "fail. Use the plain `POST .../scrape` instead for a small, quick collection "
        "where blocking for the response is acceptable."
    ),
)
async def queue_scrape_job(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    background_tasks: BackgroundTasks,
    _settings: DiscordConfiguredDep,
    payload: Annotated[ScrapeRequest, Body()] = ScrapeRequest(),
) -> ScrapeJobResponse:
    """Create a scrape job and schedule it to run after this response is sent."""

    # Fail fast on the same access check the synchronous endpoint uses, so a
    # channel the bot cannot read never gets a job queued for it in the first place.
    _channel, evaluation = await services.channels.refresh_access(channel_id)
    if not evaluation.collection_allowed:
        raise AccessNotGrantedError(
            "The bot cannot collect from this channel yet",
            details={
                "channel_id": channel_id,
                "access_status": evaluation.access_status.value,
                "reason": evaluation.reason.value,
                "hint": (
                    "Create an access request and ask a Discord server "
                    "administrator to grant the bot access."
                ),
            },
        )

    job = await services.scrape_jobs.create(
        channel_id=channel_id,
        guild_id=evaluation.guild_id,
        limit=payload.limit,
        before=payload.before,
        after=payload.after,
        incremental=payload.incremental,
        keyword_config=_keyword_config(payload.keywords),
    )
    await services.session.commit()
    background_tasks.add_task(clients.run_scrape_job, job.id)
    return ScrapeJobResponse.model_validate(job)


@router.get(
    "/channels/{channel_id}/scrape/jobs/{job_id}",
    response_model=ScrapeJobResponse,
    responses=COMMON_ERRORS,
    summary="Get a scrape job's progress",
)
async def get_scrape_job(
    channel_id: ChannelIdPath,
    job_id: Annotated[int, Path(ge=1, description="Scrape job id.")],
    services: ServicesDep,
) -> ScrapeJobResponse:
    """Poll one scrape job."""

    job = await services.scrape_jobs.get(job_id)
    if job.channel_id != channel_id:
        raise NotFoundError(
            "Scrape job not found for this channel",
            details={"channel_id": channel_id, "job_id": job_id},
        )
    return ScrapeJobResponse.model_validate(job)


@router.get(
    "/channels/{channel_id}/scrape/jobs",
    response_model=Page[ScrapeJobResponse],
    summary="List scrape jobs for a channel",
)
async def list_scrape_jobs(
    channel_id: ChannelIdPath, services: ServicesDep, pagination: PaginationDep
) -> Page[ScrapeJobResponse]:
    """List background scrape jobs for a channel, newest first."""

    rows, total = await services.scrape_jobs.list_for_channel(
        channel_id, limit=pagination.limit, offset=pagination.offset
    )
    return Page[ScrapeJobResponse](
        items=[ScrapeJobResponse.model_validate(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


# ================================================================= access requests ==
@router.get(
    "/access-requests",
    response_model=Page[AccessRequestResponse],
    summary="List access requests",
)
async def list_access_requests(
    services: ServicesDep,
    pagination: PaginationDep,
    request_status: Annotated[
        AccessRequestStatus | None,
        Query(alias="status", description="Filter by request status."),
    ] = None,
    guild_id: Annotated[
        str | None, Query(pattern=r"^\d{1,20}$", description="Filter by guild.")
    ] = None,
    channel_id: Annotated[
        str | None, Query(pattern=r"^\d{1,20}$", description="Filter by channel.")
    ] = None,
) -> Page[AccessRequestResponse]:
    """List stored access requests."""

    rows, total = await services.access_requests.list_requests(
        status=request_status,
        guild_id=guild_id,
        channel_id=channel_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[AccessRequestResponse](
        items=[AccessRequestResponse.model_validate(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


@router.get(
    "/access-requests/{request_id}",
    response_model=AccessRequestResponse,
    responses=COMMON_ERRORS,
    summary="Get an access request",
)
async def get_access_request(
    request_id: RequestIdPath, services: ServicesDep
) -> AccessRequestResponse:
    """Fetch one access request."""

    request = await services.access_requests.get(request_id)
    return AccessRequestResponse.model_validate(request)


@router.post(
    "/access-requests/{request_id}/recheck",
    response_model=AccessRequestOutcomeResponse,
    responses=COMMON_ERRORS,
    summary="Re-check an access request now",
    description=(
        "Immediately re-evaluates whether an administrator has granted the bot access, "
        "without waiting for the 12-hour cycle. If access is now available the request "
        "becomes ACCEPTED, historical collection runs when it was requested, and any "
        "waiting monitor starts."
    ),
)
async def recheck_access_request(
    request_id: RequestIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    _settings: DiscordConfiguredDep,
) -> AccessRequestOutcomeResponse:
    """Force an immediate access re-check."""

    outcome = await services.workflow.recheck_request(request_id, force=True)
    if outcome.transitioned:
        await services.session.commit()
        await clients.refresh_monitored_channels()
    return _outcome_response(outcome)


@router.post(
    "/access-requests/{request_id}/cancel",
    response_model=AccessRequestResponse,
    responses=COMMON_ERRORS,
    summary="Cancel an access request",
    description=(
        "Stops re-checking a pending request. Any monitor waiting on it is stopped too."
    ),
)
async def cancel_access_request(
    request_id: RequestIdPath, services: ServicesDep, clients: ClientsDep
) -> AccessRequestResponse:
    """Cancel a pending access request."""

    request = await services.workflow.cancel_request(request_id)
    await services.session.commit()
    await clients.refresh_monitored_channels()
    return AccessRequestResponse.model_validate(request)


# =========================================================================== search ==
@router.post(
    "/search",
    response_model=Page[SearchHitResponse],
    summary="Search stored messages",
    description=(
        "Keyword search over messages already collected into SQLite. Matching is "
        "case-insensitive substring by default; `word` and `exact` modes are also "
        "available. This endpoint never calls Discord."
    ),
)
async def search_messages(
    services: ServicesDep, payload: Annotated[SearchRequest, Body()]
) -> Page[SearchHitResponse]:
    """Search collected messages."""

    hits, total = await services.search.search(
        keywords=payload.keywords,
        channel_id=payload.channel_id,
        guild_id=payload.guild_id,
        author_id=payload.author_id,
        since=payload.since,
        until=payload.until,
        match_all=payload.match_all,
        case_sensitive=payload.case_sensitive,
        match_mode=payload.match_mode,
        limit=payload.limit,
        offset=payload.offset,
    )
    return Page[SearchHitResponse](
        items=[
            SearchHitResponse(
                message=MessageResponse.from_entity(hit.message),
                matched_keywords=hit.matched_keywords,
            )
            for hit in hits
        ],
        pagination=_pagination(total, payload.limit, payload.offset, len(hits)),
    )


# ========================================================================= monitors ==
@router.post(
    "/channels/{channel_id}/monitor/start",
    response_model=MonitorStartResponse,
    responses=COMMON_ERRORS,
    summary="Start live monitoring for a channel",
    description=(
        "Starts Gateway-driven live collection.\n\n"
        "If the bot cannot read the channel yet, the monitor is created in "
        "`WAITING_FOR_ACCESS` and an access request is opened automatically. Once an "
        "administrator grants access, the reconciliation worker runs the historical "
        "scrape and promotes the monitor to `RUNNING`."
    ),
)
async def start_monitor(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    _settings: DiscordConfiguredDep,
    payload: Annotated[MonitorStartRequest, Body()] = MonitorStartRequest(),
) -> MonitorStartResponse:
    """Start (or park) a monitor for a channel."""

    monitor, access_outcome, scrape = await services.workflow.start_monitor(
        channel_id,
        keyword_config=_keyword_config(payload.keywords),
        collect_history=payload.collect_history,
        history_limit=payload.history_limit,
        store_all_messages=payload.store_all_messages,
        requested_by=payload.requested_by,
    )

    waiting = monitor.status == MonitorStatus.WAITING_FOR_ACCESS.value
    # Commit first so the Gateway refresh below observes the new monitor state.
    await services.session.commit()
    await clients.refresh_monitored_channels()

    message = (
        "Channel is private and the bot has no access yet. The monitor is waiting and "
        "an access request is pending an administrator's action."
        if waiting
        else "Live monitoring is running for this channel."
    )
    return MonitorStartResponse(
        monitor=MonitorResponse.model_validate(monitor),
        message=message,
        access_request=(
            AccessRequestResponse.model_validate(access_outcome.request)
            if access_outcome and access_outcome.request
            else None
        ),
        scrape=_scrape_summary(scrape),
        waiting_for_access=waiting,
    )


@router.post(
    "/channels/{channel_id}/monitor/stop",
    response_model=MonitorResponse,
    responses=COMMON_ERRORS,
    summary="Stop live monitoring for a channel",
)
async def stop_monitor(
    channel_id: ChannelIdPath,
    services: ServicesDep,
    clients: ClientsDep,
    payload: Annotated[MonitorStopRequest, Body()] = MonitorStopRequest(),
) -> MonitorResponse:
    """Stop a channel monitor."""

    monitor = await services.workflow.stop_monitor(channel_id, reason=payload.reason)
    await services.session.commit()
    await clients.refresh_monitored_channels()
    return MonitorResponse.model_validate(monitor)


@router.get(
    "/channels/{channel_id}/monitor/status",
    response_model=MonitorStatusResponse,
    responses=COMMON_ERRORS,
    summary="Get monitor status for a channel",
)
async def monitor_status(
    channel_id: ChannelIdPath, services: ServicesDep, clients: ClientsDep
) -> MonitorStatusResponse:
    """Report a monitor's state, keyword filter and Gateway activity."""

    monitor = await services.monitors.require_by_channel(channel_id)
    access_request = await services.access_requests.get_open_for_channel(channel_id)
    return MonitorStatusResponse(
        monitor=MonitorResponse.model_validate(monitor),
        keywords=_keyword_filter(services.monitors.keyword_config_for(monitor)),
        gateway_active=channel_id in clients.gateway.monitored_channels(),
        access_request=(
            AccessRequestResponse.model_validate(access_request)
            if access_request
            else None
        ),
    )


@router.get(
    "/monitors",
    response_model=Page[MonitorResponse],
    summary="List monitors",
)
async def list_monitors(
    services: ServicesDep,
    pagination: PaginationDep,
    monitor_status_filter: Annotated[
        MonitorStatus | None, Query(alias="status", description="Filter by status.")
    ] = None,
) -> Page[MonitorResponse]:
    """List all monitors."""

    monitors = await services.monitors.list_monitors(
        status=monitor_status_filter, limit=pagination.limit, offset=pagination.offset
    )
    total = await services.monitors.count(status=monitor_status_filter)
    return Page[MonitorResponse](
        items=[MonitorResponse.model_validate(monitor) for monitor in monitors],
        pagination=_pagination(
            total, pagination.limit, pagination.offset, len(monitors)
        ),
    )


# ==================================================================== notifications ==
@router.get(
    "/notifications",
    response_model=Page[NotificationResponse],
    summary="List notifications",
    description=(
        "Application-level status events recorded by this service. These are our own "
        "notifications -- the service never sends Discord DMs."
    ),
)
async def list_notifications(
    services: ServicesDep,
    pagination: PaginationDep,
    channel_id: Annotated[
        str | None, Query(pattern=r"^\d{1,20}$", description="Filter by channel.")
    ] = None,
    guild_id: Annotated[
        str | None, Query(pattern=r"^\d{1,20}$", description="Filter by guild.")
    ] = None,
    event_type: Annotated[
        NotificationEvent | None, Query(description="Filter by event type.")
    ] = None,
    unread: Annotated[
        bool | None, Query(description="True for unread only, false for read only.")
    ] = None,
    since: Annotated[
        datetime | None, Query(description="Only events at/after this time.")
    ] = None,
    until: Annotated[
        datetime | None, Query(description="Only events at/before this time.")
    ] = None,
) -> Page[NotificationResponse]:
    """List application notifications."""

    rows, total = await services.notifications.list_notifications(
        channel_id=channel_id,
        guild_id=guild_id,
        event_type=event_type,
        unread=unread,
        since=since,
        until=until,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[NotificationResponse](
        items=[NotificationResponse.from_entity(row) for row in rows],
        pagination=_pagination(total, pagination.limit, pagination.offset, len(rows)),
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=NotificationResponse,
    responses=COMMON_ERRORS,
    summary="Mark a notification read",
)
async def mark_notification_read(
    notification_id: Annotated[int, Path(ge=1)], services: ServicesDep
) -> NotificationResponse:
    """Mark one notification as read."""

    notification = await services.notifications.mark_read(notification_id)
    if notification is None:
        raise NotFoundError(
            "Notification not found", details={"notification_id": notification_id}
        )
    return NotificationResponse.from_entity(notification)
