"""Background historical-collection jobs: queue, poll, complete, fail.

Mirrors the fire-and-poll pattern (create returns QUEUED immediately; the caller polls
for progress) rather than blocking an HTTP connection for the whole scrape.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.enums import ScrapeJobStatus
from app.core.exceptions import NotFoundError
from tests.fake_discord import (
    GUILD_ID,
    NO_HISTORY_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
)


# ------------------------------------------------------------------- service level --
async def test_job_starts_queued(services):
    job = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=100,
        before=None, after=None, incremental=False, keyword_config=None,
    )

    assert job.status == ScrapeJobStatus.QUEUED.value
    assert job.messages_stored == 0
    assert job.started_at is None


async def test_running_job_updates_progress_after_each_page(
    services, fake_discord: FakeDiscord
):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 250)  # 3 pages at 100/page

    job = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=250,
        before=None, after=None, incremental=False, keyword_config=None,
    )
    await services.scrape_jobs.mark_running(job)

    seen_page_counts = []

    async def on_page(result):
        seen_page_counts.append(result.pages)
        await services.scrape_jobs.record_progress(job, result)

    result, _evaluation = await services.workflow.scrape_channel(
        PUBLIC_CHANNEL_ID, limit=250, on_page=on_page
    )
    await services.scrape_jobs.mark_completed(job, result)

    assert seen_page_counts == [1, 2, 3]  # progress fired once per page, in order
    assert job.status == ScrapeJobStatus.COMPLETED.value
    assert job.messages_stored == 250
    assert job.pages_fetched == 3
    assert job.checkpoint_message_id == result.oldest_message_id
    assert job.finished_at is not None


async def test_partial_job_when_the_run_stops_early(services, fake_discord: FakeDiscord):
    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 150)
    # scrape_channel's own access check makes one history probe call before the scrape
    # loop starts, so "revoke after page 1 of the real scrape" is the 2nd call overall.
    fake_discord.deny_history_after[PUBLIC_CHANNEL_ID] = 2

    job = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=150,
        before=None, after=None, incremental=False, keyword_config=None,
    )
    result, _evaluation = await services.workflow.scrape_channel(
        PUBLIC_CHANNEL_ID, limit=150
    )
    await services.scrape_jobs.mark_completed(job, result)

    assert job.status == ScrapeJobStatus.PARTIAL.value
    assert job.stopped_reason == "FORBIDDEN"
    assert job.messages_stored == 100


async def test_job_marked_failed_records_a_sanitized_message(services):
    job = await services.scrape_jobs.create(
        channel_id=PRIVATE_CHANNEL_ID, guild_id=GUILD_ID, limit=100,
        before=None, after=None, incremental=False, keyword_config=None,
    )

    failed = await services.scrape_jobs.mark_failed(
        job, error_message="ACCESS_NOT_GRANTED: blocked"
    )

    assert failed.status == ScrapeJobStatus.FAILED.value
    assert "ACCESS_NOT_GRANTED" in failed.error_message
    assert failed.finished_at is not None


async def test_get_unknown_job_raises(services):
    with pytest.raises(NotFoundError):
        await services.scrape_jobs.get(999999)


async def test_list_jobs_for_a_channel_newest_first(services):
    first = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=10,
        before=None, after=None, incremental=False, keyword_config=None,
    )
    second = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=20,
        before=None, after=None, incremental=False, keyword_config=None,
    )

    rows, total = await services.scrape_jobs.list_for_channel(PUBLIC_CHANNEL_ID)

    assert total == 2
    assert [r.id for r in rows] == [second.id, first.id]


async def test_keyword_config_round_trips_through_a_job(services, fake_discord: FakeDiscord):
    from app.discord.keywords import KeywordConfig

    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 3, prefix="ransomware alert")

    job = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=10,
        before=None, after=None, incremental=False,
        keyword_config=KeywordConfig(keywords=["ransomware"]),
    )
    from app.discord.keywords import KeywordConfig as KC

    restored = KC.from_json(job.keyword_config_json)
    result, _evaluation = await services.workflow.scrape_channel(
        PUBLIC_CHANNEL_ID, limit=10, keyword_config=restored
    )
    await services.scrape_jobs.mark_completed(job, result)

    assert restored.keywords == ["ransomware"]
    assert job.messages_matched == 3


# --------------------------------------------------------------------- runner level --
async def test_run_scrape_job_end_to_end(clients, database, services, fake_discord: FakeDiscord):
    """Exercises DiscordClientManager.run_scrape_job -- the real background-task path,
    with its own database sessions, not the request-scoped one."""

    fake_discord.channels[PUBLIC_CHANNEL_ID].messages.clear()
    fake_discord.add_messages(PUBLIC_CHANNEL_ID, 10)

    job = await services.scrape_jobs.create(
        channel_id=PUBLIC_CHANNEL_ID, guild_id=GUILD_ID, limit=10,
        before=None, after=None, incremental=False, keyword_config=None,
    )
    await services.session.commit()

    await clients.run_scrape_job(job.id)

    async with database.session() as session:
        graph = clients.build_services(session)
        finished = await graph.scrape_jobs.get(job.id)

    assert finished.status == ScrapeJobStatus.COMPLETED.value
    assert finished.messages_stored == 10
    assert finished.started_at is not None
    assert finished.finished_at is not None


async def test_run_scrape_job_marks_failed_on_access_error(
    clients, database, services, fake_discord: FakeDiscord
):
    job = await services.scrape_jobs.create(
        channel_id=PRIVATE_CHANNEL_ID, guild_id=GUILD_ID, limit=10,
        before=None, after=None, incremental=False, keyword_config=None,
    )
    await services.session.commit()

    await clients.run_scrape_job(job.id)

    async with database.session() as session:
        graph = clients.build_services(session)
        finished = await graph.scrape_jobs.get(job.id)

    assert finished.status == ScrapeJobStatus.FAILED.value
    assert finished.error_message is not None


# ---------------------------------------------------------------------------- HTTP ---
def test_queue_job_returns_202_and_a_queued_job(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 100}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] in ("QUEUED", "RUNNING", "COMPLETED")  # background task may
    assert body["channel_id"] == PUBLIC_CHANNEL_ID                # already have run


def test_queued_job_completes_via_the_background_task(app_client: TestClient):
    """FastAPI's TestClient awaits BackgroundTasks before returning, so by the time a
    poll happens the job should already be finished for a small channel."""

    created = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 100}
    ).json()

    polled = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs/{created['id']}"
    ).json()

    assert polled["status"] == "COMPLETED"
    assert polled["messages_stored"] == 10  # the seeded public channel has 10 messages


def test_queue_job_fails_fast_without_access(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/scrape/jobs", json={}
    )

    assert response.status_code == 409
    assert response.json()["code"] == "ACCESS_NOT_GRANTED"


def test_queue_job_fails_fast_without_read_history(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{NO_HISTORY_CHANNEL_ID}/scrape/jobs", json={}
    )

    assert response.status_code == 409


def test_no_job_row_is_created_when_access_is_refused(app_client: TestClient):
    app_client.post(f"/discord/channels/{PRIVATE_CHANNEL_ID}/scrape/jobs", json={})

    listed = app_client.get(f"/discord/channels/{PRIVATE_CHANNEL_ID}/scrape/jobs").json()

    assert listed["pagination"]["total"] == 0


def test_get_job_for_the_wrong_channel_is_not_found(app_client: TestClient):
    created = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 10}
    ).json()

    response = app_client.get(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/scrape/jobs/{created['id']}"
    )

    assert response.status_code == 404


def test_get_unknown_job_id_is_not_found(app_client: TestClient):
    response = app_client.get(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs/999999")

    assert response.status_code == 404


def test_list_jobs_endpoint(app_client: TestClient):
    app_client.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 10})
    app_client.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 20})

    body = app_client.get(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs").json()

    assert body["pagination"]["total"] == 2
    assert len(body["items"]) == 2


def test_job_endpoint_requires_the_api_key_when_configured(app_client: TestClient, settings):
    from fastapi.testclient import TestClient as TC

    from app.main import create_app

    secured = settings.model_copy(update={"api_keys": "dsk_test"})
    with TC(create_app(secured)) as client:
        response = client.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={})
        assert response.status_code == 401


def test_repeated_scraping_via_jobs_does_not_duplicate_messages(app_client: TestClient):
    """Duplicate protection (the unique index) applies to the job path exactly as it
    does to the synchronous scrape endpoint."""

    app_client.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 100})
    second = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs", json={"limit": 100}
    ).json()

    completed = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape/jobs/{second['id']}"
    ).json()

    assert completed["messages_stored"] == 0  # all 10 already stored by the first job
    assert completed["messages_fetched"] == 10


def test_scrape_job_endpoints_are_documented(app_client: TestClient):
    schema = app_client.get("/openapi.json").json()

    assert "/discord/channels/{channel_id}/scrape/jobs" in schema["paths"]
    assert "/discord/channels/{channel_id}/scrape/jobs/{job_id}" in schema["paths"]
