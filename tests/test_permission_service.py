"""(5, 6, 7) Permission evaluation: public access, private access, failure modes."""

from __future__ import annotations

from app.core.enums import AccessReason, ChannelAccessStatus
from app.discord.permission_service import (
    ADMINISTRATOR,
    READ_MESSAGE_HISTORY,
    VIEW_CHANNEL,
    compute_permissions,
    everyone_denied_view,
)
from tests.fake_discord import (
    GUILD_ID,
    MISSING_CHANNEL_ID,
    NO_HISTORY_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeChannel,
    FakeDiscord,
    rate_limited,
    server_error,
)


# ------------------------------------------------------- public channel (case 6) --
async def test_public_channel_is_fully_accessible(services):
    result = await services.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)

    assert result.exists is True
    assert result.is_private is False
    assert result.bot_can_view is True
    assert result.bot_can_read_history is True
    assert result.access_status is ChannelAccessStatus.PUBLIC_ACCESSIBLE
    assert result.reason is AccessReason.FULLY_ACCESSIBLE
    assert result.collection_allowed is True


async def test_public_channel_metadata_is_captured(services):
    result = await services.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)

    assert result.channel_name == "general"
    assert result.guild_id == GUILD_ID
    assert result.channel_type == 0


# ------------------------------------------------------ private channel (case 7) --
async def test_private_channel_reports_cannot_view(services):
    result = await services.permissions.evaluate_channel_access(PRIVATE_CHANNEL_ID)

    assert result.is_private is True
    assert result.bot_can_view is False
    assert result.bot_can_read_history is False
    assert result.access_status is ChannelAccessStatus.PRIVATE
    assert result.reason is AccessReason.BOT_CANNOT_VIEW_CHANNEL
    assert result.collection_allowed is False


async def test_structured_result_matches_documented_shape(services):
    payload = (
        await services.permissions.evaluate_channel_access(PRIVATE_CHANNEL_ID)
    ).to_dict()

    assert payload["channel_id"] == PRIVATE_CHANNEL_ID
    assert payload["is_private"] is True
    assert payload["bot_can_view"] is False
    assert payload["bot_can_read_history"] is False
    assert payload["access_status"] == "PRIVATE"
    assert payload["reason"] == "BOT_CANNOT_VIEW_CHANNEL"


async def test_private_channel_becomes_accessible_after_admin_grants_access(
    services, fake_discord: FakeDiscord
):
    before = await services.permissions.evaluate_channel_access(PRIVATE_CHANNEL_ID)
    assert before.collection_allowed is False

    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    after = await services.permissions.evaluate_channel_access(PRIVATE_CHANNEL_ID)

    # Still marked private (@everyone is denied) but now readable by the bot.
    assert after.is_private is True
    assert after.access_status is ChannelAccessStatus.ACCESSIBLE
    assert after.reason is AccessReason.FULLY_ACCESSIBLE
    assert after.collection_allowed is True


# ------------------------------------------------------------ distinct failures --
async def test_unknown_channel_is_distinguished(services):
    result = await services.permissions.evaluate_channel_access(MISSING_CHANNEL_ID)

    assert result.exists is False
    assert result.reason is AccessReason.CHANNEL_NOT_FOUND
    assert result.collection_allowed is False


async def test_missing_read_history_is_distinguished(services):
    """Viewable but unreadable is a different state from invisible."""

    result = await services.permissions.evaluate_channel_access(NO_HISTORY_CHANNEL_ID)

    assert result.bot_can_view is True
    assert result.bot_can_read_history is False
    assert result.reason is AccessReason.MISSING_READ_MESSAGE_HISTORY
    assert result.access_status is ChannelAccessStatus.PRIVATE
    assert result.collection_allowed is False


async def test_rate_limit_is_transient_not_a_permission_verdict(
    services, fake_discord: FakeDiscord
):
    for _ in range(5):
        fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", rate_limited(0.01))

    result = await services.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)

    assert result.reason is AccessReason.RATE_LIMITED
    assert result.transient is True
    assert result.access_status is ChannelAccessStatus.UNKNOWN


async def test_discord_server_error_is_transient(services, fake_discord: FakeDiscord):
    for _ in range(5):
        fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", server_error())

    result = await services.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)

    assert result.reason is AccessReason.DISCORD_ERROR
    assert result.transient is True


async def test_invalid_token_is_reported_as_error(services, fake_discord: FakeDiscord):
    fake_discord.unauthorized = True

    result = await services.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)

    assert result.reason is AccessReason.INVALID_BOT_TOKEN
    assert result.access_status is ChannelAccessStatus.ERROR


async def test_voice_channel_is_not_collectable(services, fake_discord: FakeDiscord):
    fake_discord.channels["200000000000000055"] = FakeChannel(
        channel_id="200000000000000055", name="voice-chat", channel_type=2
    )

    result = await services.permissions.evaluate_channel_access("200000000000000055")

    assert result.reason is AccessReason.NOT_A_TEXT_CHANNEL
    assert result.collection_allowed is False


async def test_message_content_intent_disabled_blocks_collection(
    settings, database, fake_discord: FakeDiscord
):
    from app.discord.client import DiscordClientManager

    no_content = settings.model_copy(
        update={"discord_message_content_intent": False}
    )
    manager = DiscordClientManager(
        no_content, database, rest_transport=fake_discord.transport()
    )
    await manager.start_rest()
    async with database.session() as session:
        graph = manager.build_services(session)
        result = await graph.permissions.evaluate_channel_access(PUBLIC_CHANNEL_ID)
    await manager.close()

    assert result.bot_can_read_message_content is False
    assert result.reason is AccessReason.MESSAGE_CONTENT_NOT_ENABLED
    assert result.collection_allowed is False


# ------------------------------------------------------ permission bit arithmetic --
def test_everyone_denied_view_detects_private_channels():
    overwrites = [{"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}]

    assert everyone_denied_view(GUILD_ID, overwrites) is True
    assert everyone_denied_view(GUILD_ID, []) is False


def test_compute_permissions_applies_everyone_then_role_then_member():
    overwrites = [
        {"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)},
        {"id": "role-1", "type": 0, "allow": str(VIEW_CHANNEL), "deny": "0"},
    ]

    result = compute_permissions(
        base_permissions=READ_MESSAGE_HISTORY,
        member_role_ids=["role-1"],
        member_id="bot-1",
        guild_id=GUILD_ID,
        overwrites=overwrites,
    )

    # @everyone removed VIEW_CHANNEL, the role granted it back.
    assert result & VIEW_CHANNEL
    assert result & READ_MESSAGE_HISTORY


def test_member_overwrite_wins_over_role_overwrite():
    overwrites = [
        {"id": "role-1", "type": 0, "allow": str(VIEW_CHANNEL), "deny": "0"},
        {"id": "bot-1", "type": 1, "allow": "0", "deny": str(VIEW_CHANNEL)},
    ]

    result = compute_permissions(
        base_permissions=0,
        member_role_ids=["role-1"],
        member_id="bot-1",
        guild_id=GUILD_ID,
        overwrites=overwrites,
    )

    assert not result & VIEW_CHANNEL


def test_administrator_bypasses_all_overwrites():
    overwrites = [{"id": GUILD_ID, "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)}]

    result = compute_permissions(
        base_permissions=ADMINISTRATOR,
        member_role_ids=[],
        member_id="bot-1",
        guild_id=GUILD_ID,
        overwrites=overwrites,
    )

    assert result & VIEW_CHANNEL
