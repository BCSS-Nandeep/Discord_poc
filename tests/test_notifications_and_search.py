"""(15, 17) Notification creation/filtering and stored-message keyword search."""

from __future__ import annotations

import json
from datetime import timedelta

from app.core.enums import NotificationEvent
from app.database.models import utcnow
from tests.fake_discord import (
    GUILD_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
    make_message,
)


# ==================================================================== notifications ==
async def test_notification_is_created_with_payload(services):
    notification = await services.notifications.emit(
        NotificationEvent.SYSTEM_ERROR,
        "something happened",
        channel_id=PUBLIC_CHANNEL_ID,
        guild_id=GUILD_ID,
        payload={"detail": "context"},
    )

    assert notification.id is not None
    assert notification.event_type == "SYSTEM_ERROR"
    assert json.loads(notification.payload_json)["detail"] == "context"
    assert notification.read_at is None
    assert notification.created_at is not None


async def test_notifications_filter_by_event_type(services):
    await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "a")
    await services.notifications.emit(NotificationEvent.ACCESS_GRANTED, "b")

    rows, total = await services.notifications.list_notifications(
        event_type=NotificationEvent.ACCESS_GRANTED
    )

    assert total == 1
    assert rows[0].message == "b"


async def test_notifications_filter_by_channel(services):
    await services.notifications.emit(
        NotificationEvent.ACCESS_PENDING, "a", channel_id=PUBLIC_CHANNEL_ID
    )
    await services.notifications.emit(
        NotificationEvent.ACCESS_PENDING, "b", channel_id=PRIVATE_CHANNEL_ID
    )

    rows, total = await services.notifications.list_notifications(
        channel_id=PRIVATE_CHANNEL_ID
    )

    assert total == 1
    assert rows[0].message == "b"


async def test_notifications_filter_by_unread(services):
    first = await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "a")
    await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "b")
    await services.notifications.mark_read(first.id)

    unread, unread_total = await services.notifications.list_notifications(unread=True)
    read, read_total = await services.notifications.list_notifications(unread=False)

    assert unread_total == 1 and unread[0].message == "b"
    assert read_total == 1 and read[0].message == "a"
    assert await services.notifications.unread_count() == 1


async def test_notifications_filter_by_time_range(services):
    await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "recent")

    rows, total = await services.notifications.list_notifications(
        since=utcnow() - timedelta(minutes=5)
    )
    _none, none_total = await services.notifications.list_notifications(
        since=utcnow() + timedelta(minutes=5)
    )

    assert total == 1 and rows[0].message == "recent"
    assert none_total == 0


async def test_notifications_are_newest_first(services):
    await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "first")
    await services.notifications.emit(NotificationEvent.ACCESS_PENDING, "second")

    rows, _total = await services.notifications.list_notifications()

    assert rows[0].message == "second"


async def test_workflow_records_the_documented_event_sequence(
    services, fake_discord: FakeDiscord
):
    """Request -> pending -> granted, all recorded as notifications."""

    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)
    await services.workflow.recheck_request(created.request.id)

    rows, _total = await services.notifications.list_notifications(
        channel_id=PRIVATE_CHANNEL_ID
    )
    events = [row.event_type for row in rows]

    assert events.index("ACCESS_GRANTED") < events.index("ACCESS_PENDING")
    assert set(events) >= {
        "ACCESS_REQUEST_CREATED",
        "ACCESS_PENDING",
        "ACCESS_GRANTED",
    }


async def test_notifications_link_back_to_their_access_request(services):
    created = await services.workflow.request_access(PRIVATE_CHANNEL_ID)

    rows, _total = await services.notifications.list_notifications(
        access_request_id=created.request.id
    )

    assert len(rows) == 2
    assert all(row.access_request_id == created.request.id for row in rows)


# ========================================================================== search ==
async def _seed_messages(services, fake_discord: FakeDiscord) -> None:
    cid = PUBLIC_CHANNEL_ID
    fake_discord.channels[cid].messages = [
        make_message("910000000000000001", channel_id=cid, content="Ransomware sample"),
        make_message("910000000000000002", channel_id=cid, content="credential dump"),
        make_message("910000000000000003", channel_id=cid, content="lunch plans"),
        make_message("910000000000000004", channel_id=cid, content="malware+ransomware"),
    ]
    await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)


async def test_search_finds_messages_by_keyword(services, fake_discord: FakeDiscord):
    await _seed_messages(services, fake_discord)

    hits, _total = await services.search.search(keywords=["ransomware"])

    assert len(hits) == 2
    assert all("ransomware" in hit.matched_keywords for hit in hits)


async def test_search_is_case_insensitive_by_default(services, fake_discord: FakeDiscord):
    await _seed_messages(services, fake_discord)

    hits, _total = await services.search.search(keywords=["RANSOMWARE"])

    assert len(hits) == 2


async def test_search_any_keyword_by_default(services, fake_discord: FakeDiscord):
    await _seed_messages(services, fake_discord)

    hits, _total = await services.search.search(keywords=["ransomware", "credential"])

    assert len(hits) == 3


async def test_search_match_all_requires_every_keyword(
    services, fake_discord: FakeDiscord
):
    await _seed_messages(services, fake_discord)

    hits, _total = await services.search.search(
        keywords=["malware", "ransomware"], match_all=True
    )

    assert len(hits) == 1
    assert set(hits[0].matched_keywords) == {"malware", "ransomware"}


async def test_search_filters_by_channel(services, fake_discord: FakeDiscord):
    await _seed_messages(services, fake_discord)

    hits, _total = await services.search.search(
        keywords=["ransomware"], channel_id=PRIVATE_CHANNEL_ID
    )

    assert hits == []


async def test_search_word_mode_excludes_partial_matches(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages = [
        make_message("911000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="creds leaked"),
        make_message("911000000000000002", channel_id=PUBLIC_CHANNEL_ID, content="a cred here"),
    ]
    await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    substring, _t1 = await services.search.search(keywords=["cred"])
    whole_word, _t2 = await services.search.search(keywords=["cred"], match_mode="word")

    assert len(substring) == 2
    assert len(whole_word) == 1


async def test_search_with_no_keywords_returns_recent_messages(
    services, fake_discord: FakeDiscord
):
    await _seed_messages(services, fake_discord)

    hits, total = await services.search.search(keywords=[])

    assert total == 4
    assert len(hits) == 4


async def test_search_special_characters_are_escaped_not_wildcards(
    services, fake_discord: FakeDiscord
):
    """A '%' in a keyword must be a literal, not a SQL LIKE wildcard."""

    fake_discord.channels[PUBLIC_CHANNEL_ID].messages = [
        make_message("912000000000000001", channel_id=PUBLIC_CHANNEL_ID, content="100% verified"),
        make_message("912000000000000002", channel_id=PUBLIC_CHANNEL_ID, content="nothing here"),
    ]
    await services.messages.collect_history(PUBLIC_CHANNEL_ID, guild_id=GUILD_ID)

    hits, _total = await services.search.search(keywords=["100%"])

    assert len(hits) == 1


async def test_search_pagination(services, fake_discord: FakeDiscord):
    await _seed_messages(services, fake_discord)

    page_one, total = await services.search.search(keywords=[], limit=2, offset=0)
    page_two, _total = await services.search.search(keywords=[], limit=2, offset=2)

    assert total == 4
    assert len(page_one) == 2 and len(page_two) == 2
    ids = {hit.message.message_id for hit in page_one} | {
        hit.message.message_id for hit in page_two
    }
    assert len(ids) == 4


async def test_stored_keywords_are_readable(services, fake_discord: FakeDiscord):
    from app.discord.keywords import KeywordConfig

    await services.messages.collect_history(
        PUBLIC_CHANNEL_ID,
        guild_id=GUILD_ID,
        keyword_config=KeywordConfig(keywords=["message 1"]),
    )
    rows, _total = await services.messages.list_stored_messages(PUBLIC_CHANNEL_ID)
    with_keywords = [row for row in rows if row.matched_keywords_json]

    assert services.search.stored_keywords(with_keywords[0]) == ["message 1"]
