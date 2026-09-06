"""Finding guilds and channels by name rather than by snowflake id."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.fake_discord import GUILD_ID, PUBLIC_CHANNEL_ID, FakeChannel, FakeDiscord


# ============================================================== service level ==
async def test_channel_search_finds_a_discovered_channel(services):
    await services.channels.discover_guild_channels(GUILD_ID)

    rows, total = await services.channels.search_by_name("general")

    assert total == 1
    assert rows[0].channel_id == PUBLIC_CHANNEL_ID


async def test_channel_search_is_case_insensitive(services):
    await services.channels.discover_guild_channels(GUILD_ID)

    upper, _ = await services.channels.search_by_name("GENERAL")
    lower, _ = await services.channels.search_by_name("general")

    assert [r.channel_id for r in upper] == [r.channel_id for r in lower]


async def test_channel_search_matches_a_substring(services):
    await services.channels.discover_guild_channels(GUILD_ID)

    rows, total = await services.channels.search_by_name("incident")

    assert total == 1
    assert rows[0].name == "incident-response"


async def test_channel_search_can_discover_first(services):
    """With refresh=True the guild is discovered as part of the search."""

    rows, total = await services.channels.search_by_name(
        "general", guild_id=GUILD_ID, refresh=True
    )

    assert total == 1
    assert rows[0].name == "general"


async def test_channel_search_without_discovery_finds_nothing(services):
    rows, total = await services.channels.search_by_name("general")

    assert total == 0
    assert rows == []


async def test_channel_search_can_be_scoped_to_a_guild(services):
    await services.channels.discover_guild_channels(GUILD_ID)

    matching, total = await services.channels.search_by_name(
        "general", guild_id=GUILD_ID
    )
    other, other_total = await services.channels.search_by_name(
        "general", guild_id="999999999999999999"
    )

    assert total == 1 and matching[0].name == "general"
    assert other_total == 0


async def test_channel_search_escapes_like_wildcards(services, fake_discord: FakeDiscord):
    """A '%' in the query is a literal, not a match-everything wildcard."""

    fake_discord.channels["200000000000000077"] = FakeChannel(
        channel_id="200000000000000077", name="100%-uptime"
    )
    await services.channels.discover_guild_channels(GUILD_ID)

    literal, literal_total = await services.channels.search_by_name("100%")
    percent, percent_total = await services.channels.search_by_name("%")
    _all, all_total = await services.channels.search_by_name("")

    assert literal_total == 1 and literal[0].name == "100%-uptime"
    # Escaped, "%" matches only the name that really contains a percent sign --
    # not every channel, which is what an unescaped LIKE wildcard would return.
    assert percent_total == 1 and percent[0].name == "100%-uptime"
    assert all_total == 4 and all_total > percent_total


async def test_guild_search_finds_a_known_guild(services):
    await services.guilds.discover_guilds()

    rows, total = await services.guilds.search_by_name("threat")

    assert total == 1
    assert rows[0].guild_id == GUILD_ID


async def test_guild_search_can_refresh_first(services):
    rows, total = await services.guilds.search_by_name("Threat Intel", refresh=True)

    assert total == 1
    assert rows[0].name == "Threat Intel Server"


async def test_guild_search_miss_returns_empty(services):
    await services.guilds.discover_guilds()

    rows, total = await services.guilds.search_by_name("nonexistent")

    assert total == 0
    assert rows == []


# ================================================================== HTTP level ==
def test_channel_search_endpoint(app_client: TestClient):
    app_client.get(f"/discord/guilds/{GUILD_ID}/channels")

    body = app_client.get(
        "/discord/channels/search", params={"query": "general"}
    ).json()

    assert body["pagination"]["total"] == 1
    assert body["items"][0]["name"] == "general"
    assert body["items"][0]["channel_id"] == PUBLIC_CHANNEL_ID


def test_channel_search_endpoint_can_discover_inline(app_client: TestClient):
    body = app_client.get(
        "/discord/channels/search",
        params={"query": "incident", "guild_id": GUILD_ID, "refresh": True},
    ).json()

    assert body["pagination"]["total"] == 1
    assert body["items"][0]["name"] == "incident-response"


def test_guild_search_endpoint(app_client: TestClient):
    body = app_client.get(
        "/discord/guilds/search", params={"query": "threat", "refresh": True}
    ).json()

    assert body["pagination"]["total"] == 1
    assert body["items"][0]["guild_id"] == GUILD_ID


def test_search_routes_are_not_shadowed_by_the_id_routes(app_client: TestClient):
    """Route ordering guard.

    Starlette matches in declaration order and compiles ``{channel_id}`` to a generic
    path segment. If ``/channels/search`` were registered after ``/channels/{id}`` it
    would be captured as an id and rejected as a malformed snowflake, so assert these
    resolve to the search handlers rather than to a 422.
    """

    channels = app_client.get("/discord/channels/search", params={"query": "x"})
    guilds = app_client.get("/discord/guilds/search", params={"query": "x"})

    assert channels.status_code == 200, channels.text
    assert guilds.status_code == 200, guilds.text
    assert "pagination" in channels.json()
    assert "pagination" in guilds.json()


def test_search_requires_a_non_empty_query(app_client: TestClient):
    missing = app_client.get("/discord/channels/search")
    blank = app_client.get("/discord/channels/search", params={"query": ""})

    assert missing.status_code == 422
    assert blank.status_code == 422


def test_search_rejects_a_bad_guild_id(app_client: TestClient):
    response = app_client.get(
        "/discord/channels/search", params={"query": "general", "guild_id": "Python"}
    )

    assert response.status_code == 422


def test_numeric_channel_id_still_resolves_to_the_detail_route(app_client: TestClient):
    """The new route must not break lookup by id."""

    body = app_client.get(f"/discord/channels/{PUBLIC_CHANNEL_ID}").json()

    assert body["channel"]["channel_id"] == PUBLIC_CHANNEL_ID
    assert "access" in body


def test_channel_name_in_the_id_route_is_still_rejected(app_client: TestClient):
    """What the user hit in Swagger: a name is not a valid snowflake."""

    response = app_client.get("/discord/channels/Python")

    assert response.status_code == 422


def test_search_endpoints_are_documented(app_client: TestClient):
    paths = app_client.get("/openapi.json").json()["paths"]

    assert "/discord/channels/search" in paths
    assert "/discord/guilds/search" in paths
