"""(2, 18, 19) Discord REST client: auth, rate limits, retries, error mapping."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import Settings
from app.core.exceptions import (
    DiscordForbiddenError,
    DiscordNotConfiguredError,
    DiscordNotFoundError,
    DiscordRateLimitError,
    DiscordServerError,
    DiscordTransportError,
    DiscordUnauthorizedError,
    ValidationError,
)
from app.discord.rest_client import DiscordRestClient, validate_snowflake
from tests.fake_discord import (
    BOT_ID,
    GUILD_ID,
    MISSING_CHANNEL_ID,
    NO_HISTORY_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
    rate_limited,
    server_error,
)


@pytest.fixture
async def client(settings: Settings, fake_discord: FakeDiscord):
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()
    try:
        yield rest
    finally:
        await rest.close()


# ---------------------------------------------------------------- authentication --
async def test_get_current_bot_authenticates(client: DiscordRestClient):
    bot = await client.get_current_bot()

    assert bot["id"] == BOT_ID
    assert bot["bot"] is True


async def test_authorization_header_uses_bot_scheme(
    settings: Settings, fake_discord: FakeDiscord
):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization", "")
        return fake_discord.handler(request)

    rest = DiscordRestClient(settings, transport=httpx.MockTransport(handler))
    await rest.start()
    await client_get_bot(rest)
    await rest.close()

    assert seen["auth"].startswith("Bot ")
    assert settings.bot_token in seen["auth"]


async def client_get_bot(rest: DiscordRestClient):
    return await rest.get_current_bot()


async def test_unauthorized_raises_and_does_not_retry(
    settings: Settings, fake_discord: FakeDiscord
):
    fake_discord.unauthorized = True
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    with pytest.raises(DiscordUnauthorizedError):
        await rest.get_current_bot()

    await rest.close()
    # Exactly one attempt: a bad token cannot be fixed by retrying.
    assert len(fake_discord.calls_to("/users/@me")) == 1


async def test_missing_token_raises_configuration_error(
    settings: Settings, fake_discord: FakeDiscord
):
    unconfigured = settings.model_copy(update={"discord_bot_token": ""})
    rest = DiscordRestClient(unconfigured, transport=fake_discord.transport())
    await rest.start()

    with pytest.raises(DiscordNotConfiguredError):
        await rest.get_current_bot()

    await rest.close()


# ------------------------------------------------------------------- error codes --
async def test_forbidden_maps_to_forbidden_error(client: DiscordRestClient):
    with pytest.raises(DiscordForbiddenError) as excinfo:
        await client.get_channel(PRIVATE_CHANNEL_ID)

    assert excinfo.value.discord_code == 50001
    assert excinfo.value.retryable is False


async def test_forbidden_is_not_retried(
    client: DiscordRestClient, fake_discord: FakeDiscord
):
    with pytest.raises(DiscordForbiddenError):
        await client.get_channel(PRIVATE_CHANNEL_ID)

    assert len(fake_discord.calls_to(f"/channels/{PRIVATE_CHANNEL_ID}")) == 1


async def test_not_found_maps_to_not_found_error(client: DiscordRestClient):
    with pytest.raises(DiscordNotFoundError):
        await client.get_channel(MISSING_CHANNEL_ID)


async def test_history_forbidden_when_read_history_missing(client: DiscordRestClient):
    with pytest.raises(DiscordForbiddenError):
        await client.get_channel_messages(NO_HISTORY_CHANNEL_ID, limit=1)


# ------------------------------------------------------------------- rate limits --
async def test_rate_limit_is_retried_after_retry_after(
    settings: Settings, fake_discord: FakeDiscord
):
    fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", rate_limited(0.01))
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    channel = await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert channel["id"] == PUBLIC_CHANNEL_ID
    # One 429 plus one successful retry.
    assert len(fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}")) == 2


async def test_rate_limit_raises_after_bounded_retries(
    settings: Settings, fake_discord: FakeDiscord
):
    for _ in range(5):
        fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", rate_limited(0.01))
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    with pytest.raises(DiscordRateLimitError) as excinfo:
        await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert excinfo.value.retry_after == 0.01
    # max_retries=2 in the test settings -> 3 attempts total, never unbounded.
    assert len(fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}")) == 3


async def test_excessive_retry_after_is_refused_not_slept(
    settings: Settings, fake_discord: FakeDiscord
):
    """A multi-minute Retry-After must fail fast rather than block a request."""

    fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", rate_limited(9999.0))
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    with pytest.raises(DiscordRateLimitError) as excinfo:
        await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert excinfo.value.retry_after == 9999.0
    assert len(fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}")) == 1


async def test_global_rate_limit_is_tracked(
    settings: Settings, fake_discord: FakeDiscord
):
    fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", rate_limited(0.01, is_global=True))
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    channel = await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert channel["id"] == PUBLIC_CHANNEL_ID


# ---------------------------------------------------------------------- retries --
async def test_server_errors_are_retried_then_raise(
    settings: Settings, fake_discord: FakeDiscord
):
    for _ in range(5):
        fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", server_error())
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    with pytest.raises(DiscordServerError):
        await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert len(fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}")) == 3


async def test_server_error_recovers_on_retry(
    settings: Settings, fake_discord: FakeDiscord
):
    fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}", server_error())
    rest = DiscordRestClient(settings, transport=fake_discord.transport())
    await rest.start()

    channel = await rest.get_channel(PUBLIC_CHANNEL_ID)

    await rest.close()
    assert channel["name"] == "general"


async def test_network_errors_become_transport_errors(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    rest = DiscordRestClient(settings, transport=httpx.MockTransport(handler))
    await rest.start()

    with pytest.raises(DiscordTransportError):
        await rest.get_current_bot()

    await rest.close()


async def test_timeouts_become_transport_errors(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    rest = DiscordRestClient(settings, transport=httpx.MockTransport(handler))
    await rest.start()

    with pytest.raises(DiscordTransportError):
        await rest.get_current_bot()

    await rest.close()


# ------------------------------------------------------------------- validation --
@pytest.mark.parametrize(
    "bad_value",
    ["../../users/@me", "abc", "", "12345678901234567890123", "12 34", "1;DROP"],
)
def test_invalid_snowflakes_are_rejected(bad_value: str):
    with pytest.raises(ValidationError):
        validate_snowflake(bad_value, field="channel_id")


def test_valid_snowflake_is_normalized_to_string():
    assert validate_snowflake(123456789012345678) == "123456789012345678"


async def test_client_cannot_be_pointed_at_another_host(client: DiscordRestClient):
    """Path traversal in an id must not escape the Discord base URL."""

    with pytest.raises(ValidationError):
        await client.get_channel("../../../evil")


async def test_message_page_size_is_capped_at_100(
    client: DiscordRestClient, fake_discord: FakeDiscord
):
    await client.get_channel_messages(PUBLIC_CHANNEL_ID, limit=5000)

    call = fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}/messages")[-1]
    assert call[2]["limit"] == "100"


async def test_guild_endpoints_work(client: DiscordRestClient):
    guild = await client.get_guild(GUILD_ID)
    channels = await client.get_guild_channels(GUILD_ID)
    guilds = await client.get_guilds()

    assert guild["id"] == GUILD_ID
    assert len(channels) == 3
    assert guilds[0]["id"] == GUILD_ID
