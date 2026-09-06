"""FastAPI dependencies.

Routes receive a fully-wired :class:`ServiceGraph` bound to a request-scoped database
session.  Routes never construct repositories or call Discord directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Query, Request

from app.core.config import Settings, get_settings
from app.core.exceptions import DiscordNotConfiguredError
from app.database.database import Database
from app.discord.client import DiscordClientManager, ServiceGraph


def get_app_settings(request: Request) -> Settings:
    """The settings instance created at startup."""

    return getattr(request.app.state, "settings", None) or get_settings()


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_client_manager(request: Request) -> DiscordClientManager:
    return request.app.state.clients


def get_worker(request: Request):
    """The reconciliation worker, or ``None`` when workers are disabled here."""

    return getattr(request.app.state, "worker", None)


async def get_services(request: Request) -> AsyncIterator[ServiceGraph]:
    """Open a request-scoped session and yield the service graph.

    The session commits when the route returns normally and rolls back on any
    exception, so a failed request never leaves partial state behind.
    """

    database: Database = request.app.state.database
    clients: DiscordClientManager = request.app.state.clients
    async with database.session() as session:
        yield clients.build_services(session)


def require_discord_configured(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Settings:
    """Reject Discord-dependent routes when no bot token is configured."""

    if not settings.discord_configured:
        raise DiscordNotConfiguredError(
            "Discord is not configured. Set DISCORD_BOT_TOKEN in the environment."
        )
    return settings


class PaginationParams:
    """Validated ``limit``/``offset`` query parameters."""

    def __init__(
        self,
        limit: Annotated[
            int, Query(ge=1, le=500, description="Maximum rows to return.")
        ] = 50,
        offset: Annotated[int, Query(ge=0, le=1_000_000, description="Rows to skip.")] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


ServicesDep = Annotated[ServiceGraph, Depends(get_services)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
ClientsDep = Annotated[DiscordClientManager, Depends(get_client_manager)]
DatabaseDep = Annotated[Database, Depends(get_database)]
PaginationDep = Annotated[PaginationParams, Depends()]
DiscordConfiguredDep = Annotated[Settings, Depends(require_discord_configured)]
