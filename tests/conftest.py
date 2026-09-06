"""Shared pytest fixtures.

Every test runs against a temporary SQLite file and the :mod:`tests.fake_discord`
stand-in.  No real Discord credentials, server or network access are required.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from app.core.config import Settings
from app.database.database import Database
from app.discord.client import DiscordClientManager, ServiceGraph
from tests.fake_discord import FakeDiscord

TEST_TOKEN = "test.bot.token.aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a developer's real .env from leaking into the test run."""

    for key in list(os.environ):
        if key.upper().startswith(("DISCORD_", "DATABASE_", "ACCESS_", "WORKER_")):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings pointed at a throwaway database, with background work disabled."""

    return Settings(
        _env_file=None,
        app_name="discord-service-test",
        environment="test",
        discord_application_id="900000000000000001",
        discord_bot_token=TEST_TOKEN,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        enable_background_workers=False,
        enable_gateway=False,
        worker_lock_file=str(tmp_path / "worker.lock"),
        access_recheck_hours=12,
        discord_max_retries=2,
        discord_retry_base_delay_seconds=0.01,
        discord_max_retry_delay_seconds=0.05,
        log_level="WARNING",
    )


@pytest.fixture
def fake_discord() -> FakeDiscord:
    return FakeDiscord()


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    db = Database(settings)
    await db.create_all()
    try:
        yield db
    finally:
        await db.disconnect()


@pytest_asyncio.fixture
async def clients(
    settings: Settings, database: Database, fake_discord: FakeDiscord
) -> AsyncIterator[DiscordClientManager]:
    manager = DiscordClientManager(
        settings, database, rest_transport=fake_discord.transport()
    )
    await manager.start_rest()
    try:
        yield manager
    finally:
        await manager.close()


@pytest_asyncio.fixture
async def services(
    database: Database, clients: DiscordClientManager
) -> AsyncIterator[ServiceGraph]:
    """A service graph on a session that commits at the end of the test."""

    async with database.session() as session:
        yield clients.build_services(session)


@pytest.fixture
def make_services(database: Database, clients: DiscordClientManager):
    """Factory for a fresh session/service graph, for tests needing several."""

    def _factory():
        return database.session()

    def _build(session) -> ServiceGraph:
        return clients.build_services(session)

    _factory.build = _build  # type: ignore[attr-defined]
    return _factory


@pytest.fixture
def app_client(settings: Settings, fake_discord: FakeDiscord) -> Iterator:
    """A TestClient whose lifespan (startup/shutdown) actually runs."""

    from fastapi.testclient import TestClient

    from app.main import create_app

    application = create_app(settings, rest_transport=fake_discord.transport())
    with TestClient(application) as client:
        yield client
