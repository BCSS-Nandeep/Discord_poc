"""Gateway listener: intents, lifecycle, monitored-channel set, event serialization.

These tests never open a real Gateway connection. discord.py's client is exercised
through its public surface with a stubbed login, which keeps the suite offline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from app.discord.gateway import (
    RECHECK_ALL_PENDING,
    DiscordGatewayClient,
    GatewayService,
    build_intents,
)
from app.discord.normalize import normalize_message
from tests.fake_discord import GUILD_ID, PUBLIC_CHANNEL_ID


# ------------------------------------------------------------------------ intents --
def test_intents_cover_guilds_and_messages(settings):
    intents = build_intents(settings)

    assert intents.guilds is True
    assert intents.guild_messages is True
    assert intents.message_content is True


def test_message_content_intent_follows_configuration(settings):
    disabled = settings.model_copy(update={"discord_message_content_intent": False})

    assert build_intents(disabled).message_content is False


def test_members_intent_is_off_unless_enabled(settings):
    assert build_intents(settings).members is False
    enabled = settings.model_copy(update={"discord_guild_members_intent": True})
    assert build_intents(enabled).members is True


def test_privileged_intents_are_not_requested_wholesale(settings):
    """Only the intents this service actually needs are requested."""

    intents = build_intents(settings)

    assert intents.presences is False
    assert intents.typing is False
    assert intents.guild_reactions is False


# ---------------------------------------------------------------------- lifecycle --
async def test_gateway_disabled_does_not_start(settings):
    service = GatewayService(
        settings,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    await service.start()

    assert service.status == "disabled"
    assert service.is_running is False


async def test_gateway_without_a_token_reports_an_error(settings):
    unconfigured = settings.model_copy(
        update={"enable_gateway": True, "discord_bot_token": ""}
    )
    service = GatewayService(
        unconfigured,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    await service.start()

    assert service.status == "error"
    assert "DISCORD_BOT_TOKEN" in service.last_error


async def test_gateway_reports_a_rejected_token(settings, monkeypatch):
    enabled = settings.model_copy(update={"enable_gateway": True})
    service = GatewayService(
        enabled,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    async def fake_start(self, token, **kwargs):
        raise discord.LoginFailure("bad token")

    monkeypatch.setattr(DiscordGatewayClient, "start", fake_start, raising=False)

    await service.start()
    await asyncio.sleep(0.05)

    assert service.status == "error"
    assert "token" in service.last_error.lower()
    await service.stop()


async def test_gateway_reports_missing_privileged_intents(settings, monkeypatch):
    """A missing Message Content intent must not crash the application."""

    enabled = settings.model_copy(update={"enable_gateway": True})
    service = GatewayService(
        enabled,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    async def fake_start(self, token, **kwargs):
        raise discord.PrivilegedIntentsRequired(shard_id=None)

    monkeypatch.setattr(DiscordGatewayClient, "start", fake_start, raising=False)

    await service.start()
    await asyncio.sleep(0.05)

    assert service.status == "error"
    assert "Message Content" in service.last_error
    await service.stop()


async def test_start_is_idempotent(settings, monkeypatch):
    enabled = settings.model_copy(update={"enable_gateway": True})
    service = GatewayService(
        enabled,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    async def fake_start(self, token, **kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(DiscordGatewayClient, "start", fake_start, raising=False)

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task
    await service.stop()
    assert service.is_running is False


async def test_gateway_never_exposes_the_token_in_its_snapshot(settings):
    service = GatewayService(
        settings,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    snapshot = service.snapshot()

    assert settings.bot_token not in str(snapshot)
    assert set(snapshot) == {
        "enabled",
        "status",
        "monitored_channels",
        "last_error",
        "message_content_intent",
    }


# --------------------------------------------------------------- monitored set --
def test_monitored_channel_set_is_replaceable(settings):
    service = GatewayService(
        settings,
        on_monitored_message=_unused_message,
        on_permission_signal=_unused_signal,
    )

    service.set_monitored_channels({PUBLIC_CHANNEL_ID})
    assert service.monitored_channels() == {PUBLIC_CHANNEL_ID}

    service.add_monitored_channel("123")
    assert "123" in service.monitored_channels()

    service.remove_monitored_channel("123")
    assert "123" not in service.monitored_channels()

    service.set_monitored_channels(set())
    assert service.monitored_channels() == set()


async def test_client_manager_seeds_the_monitored_set_from_sqlite(
    clients, database, services
):
    await services.workflow.start_monitor(PUBLIC_CHANNEL_ID, collect_history=False)
    await services.session.commit()

    monitored = await clients.refresh_monitored_channels()

    assert monitored == {PUBLIC_CHANNEL_ID}
    assert clients.gateway.monitored_channels() == {PUBLIC_CHANNEL_ID}


# ------------------------------------------------------------------ event shapes --
def test_serialized_gateway_message_normalizes_like_a_rest_message():
    """Both collection paths must produce the same stored row shape."""

    message = _fake_discord_message()

    payload = DiscordGatewayClient._serialize_message(message)
    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert row["message_id"] == "970000000000000001"
    assert row["channel_id"] == PUBLIC_CHANNEL_ID
    assert row["guild_id"] == GUILD_ID
    assert row["content"] == "ransomware detected"
    assert row["author_name"] == "Analyst"
    assert row["author_is_bot"] is False
    assert row["message_url"].endswith("970000000000000001")


def test_serialized_message_carries_attachments():
    message = _fake_discord_message(
        attachments=[
            SimpleNamespace(
                id=5,
                filename="sample.bin",
                size=12,
                content_type="application/octet-stream",
                url="https://cdn.example/sample.bin",
            )
        ]
    )

    payload = DiscordGatewayClient._serialize_message(message)
    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert row["has_attachments"] is True
    assert "sample.bin" in row["attachments_json"]


async def test_permission_signal_sentinel_rechecks_every_pending_request(
    clients, database, services
):
    from tests.fake_discord import PRIVATE_CHANNEL_ID

    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    await services.session.commit()

    await clients._handle_permission_signal(RECHECK_ALL_PENDING)

    async with database.session() as session:
        graph = clients.build_services(session)
        request = await graph.access_requests.get(created.request.id)

    assert request.check_count == 1  # the fast path ran a real check


async def test_permission_signal_promotes_a_granted_channel(
    clients, database, services, fake_discord
):
    from tests.fake_discord import PRIVATE_CHANNEL_ID

    await services.workflow.start_monitor(PRIVATE_CHANNEL_ID)
    await services.session.commit()
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    await clients._handle_permission_signal(PRIVATE_CHANNEL_ID)

    async with database.session() as session:
        graph = clients.build_services(session)
        monitor = await graph.monitors.get_by_channel(PRIVATE_CHANNEL_ID)

    assert monitor.status == "RUNNING"
    assert PRIVATE_CHANNEL_ID in clients.gateway.monitored_channels()


async def test_permission_signal_for_a_channel_without_a_request_is_a_no_op(
    clients, services
):
    result = await services.workflow.handle_permission_signal(PUBLIC_CHANNEL_ID)

    assert result is None


# ------------------------------------------------------------------------ helpers --
async def _unused_message(payload, channel_id, guild_id):  # pragma: no cover
    raise AssertionError("message handler should not run in this test")


async def _unused_signal(channel_id):  # pragma: no cover
    raise AssertionError("permission handler should not run in this test")


def _fake_discord_message(*, attachments=None):
    """A duck-typed stand-in for discord.Message, sufficient for serialization."""

    return SimpleNamespace(
        id=970000000000000001,
        type=SimpleNamespace(value=0),
        channel=SimpleNamespace(id=int(PUBLIC_CHANNEL_ID)),
        guild=SimpleNamespace(id=int(GUILD_ID)),
        author=SimpleNamespace(
            id=800000000000000001, name="analyst", global_name="Analyst", bot=False
        ),
        content="ransomware detected",
        created_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        edited_at=None,
        pinned=False,
        tts=False,
        mention_everyone=False,
        attachments=attachments or [],
        embeds=[],
        reference=None,
    )
