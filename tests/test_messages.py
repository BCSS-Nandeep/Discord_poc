"""(13, 14, 15, 20) Historical pagination, duplicate prevention, keywords, storage."""

from __future__ import annotations

import json

import pytest

from app.core.exceptions import AccessNotGrantedError
from app.discord.keywords import KeywordConfig, KeywordMatcher, normalize_keywords
from app.discord.normalize import (
    build_message_url,
    normalize_message,
    parse_timestamp,
    snowflake_to_datetime,
)
from tests.fake_discord import (
    GUILD_ID,
    NO_HISTORY_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
    make_message,
    rate_limited,
)


# ------------------------------------------------------------- pagination (13) --
async def test_collect_history_stores_all_messages(services, fake_discord: FakeDiscord):
    result = await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert result.fetched == 10
    assert result.stored == 10
    assert result.completed is True


async def test_collect_history_pages_beyond_the_100_message_cap(
    services, fake_discord: FakeDiscord
):
    """Discord returns at most 100 per call, so 250 messages needs 3 pages."""

    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 250)

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=250
    )

    assert result.fetched == 250
    assert result.stored == 250
    assert result.pages == 3


async def test_pagination_uses_a_before_cursor(services, fake_discord: FakeDiscord):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 150)

    await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=150
    )

    history_calls = fake_discord.calls_to(f"/channels/{PUBLIC_CHANNEL_ID}/messages")
    assert "before" not in history_calls[0][2]
    assert "before" in history_calls[1][2]


async def test_limit_bounds_the_total_not_the_page(services, fake_discord: FakeDiscord):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 250)

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=120
    )

    assert result.fetched == 120
    assert result.stored == 120


async def test_after_cursor_makes_collection_incremental(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 20)
    all_ids = sorted(int(m["id"]) for m in fake_discord.channels[PUBLIC_CHANNEL_ID].messages)
    midpoint = str(all_ids[9])

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, after=midpoint
    )

    assert result.stored == 10


async def test_short_page_ends_pagination(services, fake_discord: FakeDiscord):
    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=1000
    )

    assert result.pages == 1
    assert result.fetched == 10


# ------------------------------------------------ duplicate prevention (14, 20) --
async def test_rescraping_stores_no_duplicates(services, fake_discord: FakeDiscord):
    first = await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)
    second = await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert first.stored == 10
    assert second.stored == 0
    assert second.duplicates == 10
    assert await services.messages.count() == 10


async def test_duplicate_within_a_single_batch_is_ignored(services):
    payload = make_message("555000000000000001", channel_id=PUBLIC_CHANNEL_ID)
    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    inserted_first = await services.messages._repo.bulk_insert_ignore_duplicates([row])
    inserted_again = await services.messages._repo.bulk_insert_ignore_duplicates([row])

    assert inserted_first == 1
    assert inserted_again == 0


async def test_messages_persist_with_normalized_fields(services):
    await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)
    rows, total = await services.messages.list_stored_messages(PUBLIC_CHANNEL_ID, limit=5)

    assert total == 10
    stored = rows[0]
    assert stored.guild_id == GUILD_ID
    assert stored.channel_id == PUBLIC_CHANNEL_ID
    assert stored.author_name
    assert stored.message_url.startswith("https://discord.com/channels/")
    assert stored.source == "rest"
    assert stored.timestamp is not None


# ------------------------------------------------------- error handling in runs --
async def test_scrape_is_refused_without_access(services):
    with pytest.raises(AccessNotGrantedError) as excinfo:
        await services.workflow.scrape_channel(PRIVATE_CHANNEL_ID)

    assert excinfo.value.details["reason"] == "BOT_CANNOT_VIEW_CHANNEL"


async def test_scrape_is_refused_without_read_history(services):
    with pytest.raises(AccessNotGrantedError):
        await services.workflow.scrape_channel(NO_HISTORY_CHANNEL_ID)


async def test_collection_stops_cleanly_when_access_is_revoked_midway(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 150)
    # An administrator revokes access after the first page is served.
    fake_discord.deny_history_after[PUBLIC_CHANNEL_ID] = 1

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=150
    )

    assert result.completed is False
    assert result.stopped_reason == "FORBIDDEN"
    assert result.stored == 100  # the first page was kept


async def test_rate_limit_stops_collection_without_losing_stored_pages(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 150)
    for _ in range(6):
        fake_discord.script(f"/channels/{PUBLIC_CHANNEL_ID}/messages", rate_limited(0.01))

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=150
    )

    assert result.completed is False
    assert result.stopped_reason == "RATE_LIMITED"


# ---------------------------------------------------------- keyword matching (15) --
def test_substring_matching_is_case_insensitive_by_default():
    matcher = KeywordMatcher(KeywordConfig(keywords=["Ransomware"]))

    assert matcher.match("New RANSOMWARE sample") == ["Ransomware"]
    assert matcher.match("nothing here") == []


def test_substring_matches_inside_a_word():
    matcher = KeywordMatcher(KeywordConfig(keywords=["cred"]))

    assert matcher.match("stolen credentials") == ["cred"]


def test_word_mode_requires_whole_words():
    matcher = KeywordMatcher(KeywordConfig(keywords=["cred"], match_mode="word"))

    assert matcher.match("stolen credentials") == []
    assert matcher.match("the cred is here") == ["cred"]


def test_exact_mode_requires_the_whole_message():
    matcher = KeywordMatcher(KeywordConfig(keywords=["malware"], match_mode="exact"))

    assert matcher.match("malware") == ["malware"]
    assert matcher.match("some malware here") == []


def test_case_sensitive_matching():
    matcher = KeywordMatcher(
        KeywordConfig(keywords=["Ransomware"], case_sensitive=True)
    )

    assert matcher.match("Ransomware") == ["Ransomware"]
    assert matcher.match("ransomware") == []


def test_multiple_keywords_all_reported():
    matcher = KeywordMatcher(KeywordConfig(keywords=["ransomware", "malware", "credential"]))

    matched = matcher.match("ransomware and credential theft")

    assert set(matched) == {"ransomware", "credential"}


def test_keywords_are_deduplicated_and_bounded():
    cleaned = normalize_keywords(["  spam ", "SPAM", "spam", "", "x" * 500])

    assert cleaned == ["spam"]


def test_empty_config_matches_nothing_but_stores_everything():
    matcher = KeywordMatcher(KeywordConfig())

    assert matcher.enabled is False
    assert matcher.match("anything") == []
    assert matcher.should_store([]) is True


def test_store_non_matching_false_filters_out_misses():
    matcher = KeywordMatcher(
        KeywordConfig(keywords=["ransomware"], store_non_matching=False)
    )

    assert matcher.should_store([]) is False
    assert matcher.should_store(["ransomware"]) is True


async def test_matched_keywords_are_persisted(services, fake_discord: FakeDiscord):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages = [
        make_message(
            "777000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="ransomware found"
        ),
        make_message(
            "777000000000000002", channel_id=PUBLIC_CHANNEL_ID, content="normal chatter"
        ),
    ]

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID,
        guild_id=GUILD_ID,
        keyword_config=KeywordConfig(keywords=["ransomware"]),
    )
    rows, _total = await services.messages.list_stored_messages(PUBLIC_CHANNEL_ID)
    by_id = {row.message_id: row for row in rows}

    assert result.matched == 1
    assert json.loads(by_id["777000000000000001"].matched_keywords_json) == ["ransomware"]
    assert by_id["777000000000000002"].matched_keywords_json is None


async def test_non_matching_messages_can_be_discarded(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages = [
        make_message("778000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="ransomware"),
        make_message("778000000000000002", channel_id=PUBLIC_CHANNEL_ID, content="chatter"),
    ]

    result = await services.messages.collect_history(
        PUBLIC_CHANNEL_ID,
        guild_id=GUILD_ID,
        keyword_config=KeywordConfig(keywords=["ransomware"], store_non_matching=False),
    )

    assert result.stored == 1
    assert result.skipped_non_matching == 1


def test_keyword_config_round_trips_through_json():
    config = KeywordConfig(keywords=["a", "b"], match_mode="word", case_sensitive=True)

    restored = KeywordConfig.from_json(config.to_json())

    assert restored.keywords == ["a", "b"]
    assert restored.match_mode == "word"
    assert restored.case_sensitive is True


def test_corrupt_keyword_json_degrades_safely():
    assert KeywordConfig.from_json("{not json").keywords == []
    assert KeywordConfig.from_json(None).keywords == []


# ------------------------------------------------------------- normalization ---
def test_normalize_message_produces_the_internal_schema():
    payload = make_message("999000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="hi")

    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert row["message_id"] == "999000000000000001"
    assert row["channel_id"] == PUBLIC_CHANNEL_ID
    assert row["guild_id"] == GUILD_ID
    assert row["content"] == "hi"
    assert row["author_name"] == "Analyst"
    assert row["message_url"] == build_message_url(
        GUILD_ID, PUBLIC_CHANNEL_ID, "999000000000000001"
    )


def test_normalize_defaults_guild_id_to_empty_string_for_dedup():
    """NULL guild_id would defeat the UNIQUE tuple in SQLite."""

    payload = make_message("999000000000000002", channel_id=PUBLIC_CHANNEL_ID)
    payload.pop("guild_id")

    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=None)

    assert row["guild_id"] == ""


def test_attachments_are_normalized():
    payload = make_message(
        "999000000000000003",
        channel_id=PUBLIC_CHANNEL_ID,
        attachments=[
            {
                "id": "1",
                "filename": "sample.bin",
                "size": 10,
                "url": "https://x",
                "content_type": "application/octet-stream",
            }
        ],
    )

    row = normalize_message(payload, channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    assert row["has_attachments"] is True
    assert json.loads(row["attachments_json"])[0]["filename"] == "sample.bin"


def test_timestamp_parsing_handles_z_suffix_and_offsets():
    assert parse_timestamp("2026-09-01T12:00:00Z").tzinfo is not None
    assert parse_timestamp("2026-09-01T12:00:00+00:00").hour == 12
    assert parse_timestamp("not-a-date") is None
    assert parse_timestamp(None) is None


def test_snowflake_carries_its_creation_time():
    created = snowflake_to_datetime("175928847299117063")

    assert created is not None
    assert created.year == 2016
