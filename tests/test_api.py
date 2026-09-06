"""(2, 3, 4, 19, 20, 21) HTTP layer: health, discovery, workflow, errors, OpenAPI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.fake_discord import (
    BOT_ID,
    GUILD_ID,
    MISSING_CHANNEL_ID,
    NO_HISTORY_CHANNEL_ID,
    PRIVATE_CHANNEL_ID,
    PUBLIC_CHANNEL_ID,
    FakeDiscord,
)


# ============================================================== health / identity ==
def test_health_reports_ok(app_client: TestClient):
    response = app_client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == "discord-service-test"
    assert body["database"] == "connected"
    assert body["discord"] == "connected"


def test_health_never_leaks_the_token(app_client: TestClient, settings):
    response = app_client.get("/health")

    assert settings.bot_token not in response.text
    assert "token" not in response.text.lower()


def test_health_degrades_when_discord_rejects_the_token(
    app_client: TestClient, fake_discord: FakeDiscord
):
    fake_discord.unauthorized = True

    body = app_client.get("/health").json()

    assert body["database"] == "connected"
    assert body["discord"] in ("error", "disconnected")
    assert body["status"] == "degraded"


def test_discord_health_reports_subsystems(app_client: TestClient, settings):
    body = app_client.get("/discord/health").json()

    assert body["discord_configured"] is True
    assert body["access_recheck_hours"] == 12
    assert body["bot"]["id"] == BOT_ID
    assert "counts" in body and "gateway" in body and "worker" in body
    assert settings.bot_token not in str(body)


def test_bot_endpoint_returns_identity_without_the_token(
    app_client: TestClient, settings
):
    response = app_client.get("/discord/bot")
    body = response.json()

    assert response.status_code == 200
    assert body["id"] == BOT_ID
    assert body["username"] == "collector-bot"
    assert settings.bot_token not in response.text


def test_unauthorized_bot_call_is_sanitized(
    app_client: TestClient, fake_discord: FakeDiscord
):
    fake_discord.unauthorized = True

    response = app_client.get("/discord/bot")
    body = response.json()

    assert response.status_code == 502
    assert body["code"] == "DISCORD_UNAUTHORIZED"
    assert "Traceback" not in response.text


def test_endpoints_return_503_when_discord_is_unconfigured(
    settings, fake_discord: FakeDiscord
):
    from app.main import create_app

    unconfigured = settings.model_copy(update={"discord_bot_token": ""})
    with TestClient(
        create_app(unconfigured, rest_transport=fake_discord.transport())
    ) as client:
        health = client.get("/health").json()
        guilds = client.get("/discord/guilds")

    assert health["discord"] == "unconfigured"
    assert guilds.status_code == 503
    assert guilds.json()["code"] == "DISCORD_NOT_CONFIGURED"


# ==================================================================== discovery ==
def test_list_guilds_discovers_from_discord(app_client: TestClient):
    response = app_client.get("/discord/guilds", params={"refresh": True})
    body = response.json()

    assert response.status_code == 200
    assert body["pagination"]["total"] == 1
    assert body["items"][0]["guild_id"] == GUILD_ID
    assert body["items"][0]["name"] == "Threat Intel Server"


def test_get_guild(app_client: TestClient):
    body = app_client.get(f"/discord/guilds/{GUILD_ID}").json()

    assert body["guild_id"] == GUILD_ID
    assert body["icon_url"].startswith("https://cdn.discordapp.com/icons/")


def test_get_unknown_guild_returns_404(app_client: TestClient):
    response = app_client.get("/discord/guilds/999999999999999999")

    assert response.status_code == 404


def test_list_guild_channels(app_client: TestClient):
    body = app_client.get(f"/discord/guilds/{GUILD_ID}/channels").json()
    names = {item["name"] for item in body["items"]}

    assert names == {"general", "incident-response", "announcements"}


def test_channel_discovery_flags_private_channels(app_client: TestClient):
    body = app_client.get(
        f"/discord/guilds/{GUILD_ID}/channels", params={"only_private": True}
    ).json()

    assert [item["name"] for item in body["items"]] == ["incident-response"]


def test_get_channel_returns_access_evaluation(app_client: TestClient):
    body = app_client.get(f"/discord/channels/{PUBLIC_CHANNEL_ID}").json()

    assert body["channel"]["name"] == "general"
    assert body["access"]["collection_allowed"] is True
    assert body["access"]["reason"] == "FULLY_ACCESSIBLE"
    assert body["open_access_request_id"] is None


def test_get_private_channel_reports_the_reason(app_client: TestClient):
    body = app_client.get(f"/discord/channels/{PRIVATE_CHANNEL_ID}").json()

    assert body["access"]["is_private"] is True
    assert body["access"]["bot_can_view"] is False
    assert body["access"]["access_status"] == "PRIVATE"
    assert body["access"]["reason"] == "BOT_CANNOT_VIEW_CHANNEL"


# ============================================================== access requests ==
def test_access_request_for_private_channel_returns_pending(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={}
    )
    body = response.json()

    assert response.status_code == 201
    assert body["channel_status"] == "PRIVATE"
    assert body["access_request_status"] == "PENDING"
    assert body["created"] is True
    assert body["next_check_at"] is not None
    assert "administrator" in body["message"].lower()


def test_access_request_for_accessible_channel_creates_nothing(
    app_client: TestClient,
):
    body = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/access-request", json={}
    ).json()

    assert body["already_accessible"] is True
    assert body["access_request"] is None
    assert app_client.get("/discord/access-requests").json()["pagination"]["total"] == 0


def test_duplicate_access_request_over_http_is_not_created(app_client: TestClient):
    first = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={}
    ).json()
    second = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={}
    ).json()

    assert first["created"] is True
    assert second["created"] is False
    assert second["access_request"]["id"] == first["access_request"]["id"]


def test_access_request_for_unknown_channel_returns_404(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{MISSING_CHANNEL_ID}/access-request", json={}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_list_and_filter_access_requests(app_client: TestClient):
    app_client.post(f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={})

    listed = app_client.get("/discord/access-requests").json()
    pending = app_client.get(
        "/discord/access-requests", params={"status": "PENDING"}
    ).json()
    accepted = app_client.get(
        "/discord/access-requests", params={"status": "ACCEPTED"}
    ).json()

    assert listed["pagination"]["total"] == 1
    assert pending["pagination"]["total"] == 1
    assert accepted["pagination"]["total"] == 0


def test_get_access_request_by_id(app_client: TestClient):
    created = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={}
    ).json()
    request_id = created["access_request"]["id"]

    body = app_client.get(f"/discord/access-requests/{request_id}").json()

    assert body["id"] == request_id
    assert body["status"] == "PENDING"


def test_get_missing_access_request_returns_404(app_client: TestClient):
    assert app_client.get("/discord/access-requests/4242").status_code == 404


def test_manual_recheck_grants_access_and_collects(
    app_client: TestClient, fake_discord: FakeDiscord
):
    created = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request",
        json={"collect_history_on_grant": True, "monitor_on_grant": True},
    ).json()
    request_id = created["access_request"]["id"]
    fake_discord.add_messages(PRIVATE_CHANNEL_ID, 6)
    fake_discord.grant_access(PRIVATE_CHANNEL_ID)

    body = app_client.post(f"/discord/access-requests/{request_id}/recheck").json()

    assert body["access_request_status"] == "ACCEPTED"
    assert body["transitioned"] is True
    assert body["scrape"]["stored"] == 6

    monitor = app_client.get(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/monitor/status"
    ).json()
    assert monitor["monitor"]["status"] == "RUNNING"
    assert monitor["gateway_active"] is True


def test_cancel_access_request(app_client: TestClient):
    created = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={}
    ).json()
    request_id = created["access_request"]["id"]

    body = app_client.post(f"/discord/access-requests/{request_id}/cancel").json()

    assert body["status"] == "CANCELLED"


# ================================================= collection, search, monitors ==
def test_scrape_collects_and_deduplicates(app_client: TestClient):
    first = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape", json={"limit": 100}
    ).json()
    second = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape", json={"limit": 100}
    ).json()

    assert first["stored"] == 10
    assert second["stored"] == 0
    assert second["duplicates"] == 10


def test_scrape_without_access_returns_409(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/scrape", json={}
    )
    body = response.json()

    assert response.status_code == 409
    assert body["code"] == "ACCESS_NOT_GRANTED"
    assert body["details"]["reason"] == "BOT_CANNOT_VIEW_CHANNEL"
    assert "administrator" in body["details"]["hint"]


def test_scrape_without_read_history_returns_409(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{NO_HISTORY_CHANNEL_ID}/scrape", json={}
    )

    assert response.status_code == 409
    assert response.json()["details"]["reason"] == "MISSING_READ_MESSAGE_HISTORY"


def test_list_stored_messages(app_client: TestClient):
    app_client.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape", json={})

    body = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/messages", params={"limit": 5}
    ).json()

    assert body["pagination"]["total"] == 10
    assert len(body["items"]) == 5
    assert body["items"][0]["platform"] == "discord"
    assert body["items"][0]["message_url"].startswith("https://discord.com/channels/")


def test_search_endpoint_matches_documented_request_shape(app_client: TestClient):
    app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape",
        json={"keywords": {"keywords": ["message"]}},
    )

    body = app_client.post(
        "/discord/search",
        json={
            "channel_id": PUBLIC_CHANNEL_ID,
            "keywords": ["message 1", "message 2"],
            "limit": 100,
        },
    ).json()

    assert body["pagination"]["total"] >= 2
    assert all(hit["matched_keywords"] for hit in body["items"])


def test_search_rejects_a_bad_snowflake(app_client: TestClient):
    response = app_client.post(
        "/discord/search", json={"channel_id": "not-a-snowflake", "keywords": []}
    )

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_monitor_start_stop_status_cycle(app_client: TestClient):
    started = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/start",
        json={"keywords": {"keywords": ["ransomware"]}, "collect_history": True},
    ).json()

    assert started["monitor"]["status"] == "RUNNING"
    assert started["waiting_for_access"] is False
    assert started["scrape"]["stored"] == 10

    status = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/status"
    ).json()
    assert status["keywords"]["keywords"] == ["ransomware"]
    assert status["gateway_active"] is True

    stopped = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/stop",
        json={"reason": "finished"},
    ).json()
    assert stopped["status"] == "STOPPED"


def test_monitor_on_private_channel_waits_and_opens_a_request(
    app_client: TestClient,
):
    body = app_client.post(
        f"/discord/channels/{PRIVATE_CHANNEL_ID}/monitor/start", json={}
    ).json()

    assert body["monitor"]["status"] == "WAITING_FOR_ACCESS"
    assert body["waiting_for_access"] is True
    assert body["access_request"]["status"] == "PENDING"
    assert body["scrape"] is None


def test_monitor_status_for_unknown_channel_returns_404(app_client: TestClient):
    response = app_client.get(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/monitor/status"
    )

    assert response.status_code == 404


# ================================================================= notifications ==
def test_notifications_are_listed_and_filterable(app_client: TestClient):
    app_client.post(f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={})

    all_events = app_client.get("/discord/notifications").json()
    filtered = app_client.get(
        "/discord/notifications", params={"event_type": "ACCESS_PENDING"}
    ).json()
    unread = app_client.get(
        "/discord/notifications", params={"unread": True}
    ).json()

    assert all_events["pagination"]["total"] == 2
    assert filtered["pagination"]["total"] == 1
    assert unread["pagination"]["total"] == 2
    assert filtered["items"][0]["channel_id"] == PRIVATE_CHANNEL_ID


def test_notification_can_be_marked_read(app_client: TestClient):
    app_client.post(f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={})
    notification_id = app_client.get("/discord/notifications").json()["items"][0]["id"]

    body = app_client.post(f"/discord/notifications/{notification_id}/read").json()

    assert body["read_at"] is not None
    unread = app_client.get("/discord/notifications", params={"unread": True}).json()
    assert unread["pagination"]["total"] == 1


# ==================================================================== validation ==
@pytest.mark.parametrize(
    "path",
    [
        "/discord/channels/not-a-number",
        "/discord/channels/../../etc/passwd",
        "/discord/guilds/abc/channels",
    ],
)
def test_invalid_path_ids_are_rejected(app_client: TestClient, path: str):
    assert app_client.get(path).status_code in (404, 422)


def test_pagination_bounds_are_enforced(app_client: TestClient):
    too_large = app_client.get("/discord/guilds", params={"limit": 10000})
    negative = app_client.get("/discord/guilds", params={"offset": -1})

    assert too_large.status_code == 422
    assert negative.status_code == 422


def test_scrape_rejects_contradictory_cursors(app_client: TestClient):
    response = app_client.post(
        f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape",
        json={"incremental": True, "after": "500000000000000001"},
    )

    assert response.status_code == 422


def test_validation_errors_do_not_echo_raw_input(app_client: TestClient):
    response = app_client.post(
        "/discord/search", json={"channel_id": "'; DROP TABLE discord_messages; --"}
    )

    assert response.status_code == 422
    assert "DROP TABLE" not in response.text


# ======================================================================= openapi ==
def test_openapi_document_is_generated(app_client: TestClient):
    response = app_client.get("/openapi.json")
    schema = response.json()

    assert response.status_code == 200
    assert schema["info"]["title"] == "Discord Collection Service"
    assert len(schema["paths"]) >= 21


def test_openapi_documents_every_required_endpoint(app_client: TestClient):
    paths = app_client.get("/openapi.json").json()["paths"]

    for required in [
        "/health",
        "/discord/health",
        "/discord/bot",
        "/discord/guilds",
        "/discord/guilds/{guild_id}",
        "/discord/guilds/{guild_id}/channels",
        "/discord/channels/{channel_id}",
        "/discord/channels/{channel_id}/access-request",
        "/discord/access-requests",
        "/discord/access-requests/{request_id}",
        "/discord/access-requests/{request_id}/recheck",
        "/discord/access-requests/{request_id}/cancel",
        "/discord/channels/{channel_id}/messages",
        "/discord/channels/{channel_id}/scrape",
        "/discord/search",
        "/discord/channels/{channel_id}/monitor/start",
        "/discord/channels/{channel_id}/monitor/stop",
        "/discord/channels/{channel_id}/monitor/status",
        "/discord/notifications",
    ]:
        assert required in paths, f"missing documented endpoint: {required}"


def test_openapi_exposes_status_enums(app_client: TestClient):
    schemas = app_client.get("/openapi.json").json()["components"]["schemas"]

    assert set(schemas["AccessRequestStatus"]["enum"]) >= {
        "PENDING",
        "ACCEPTED",
        "DENIED",
        "ERROR",
        "EXPIRED",
        "CANCELLED",
    }
    assert set(schemas["MonitorStatus"]["enum"]) >= {
        "WAITING_FOR_ACCESS",
        "STARTING",
        "RUNNING",
        "STOPPED",
        "PAUSED",
        "ERROR",
    }
    assert set(schemas["ChannelAccessStatus"]["enum"]) >= {
        "UNKNOWN",
        "PUBLIC_ACCESSIBLE",
        "PRIVATE",
        "ACCESSIBLE",
        "ERROR",
    }


def test_openapi_never_exposes_the_token(app_client: TestClient, settings):
    """No schema field carries the token, and the value never appears.

    ``INVALID_BOT_TOKEN`` is a legitimate error-reason enum value, so this checks for
    a real secret-bearing field rather than the substring.
    """

    document = app_client.get("/openapi.json")
    schemas = document.json()["components"]["schemas"]

    assert settings.bot_token not in document.text
    for name, schema in schemas.items():
        for field in (schema.get("properties") or {}):
            assert "token" not in field.lower(), f"{name}.{field} exposes a token field"


def test_docs_pages_render(app_client: TestClient):
    assert app_client.get("/docs").status_code == 200
    assert app_client.get("/redoc").status_code == 200


# ============================================================ startup / shutdown ==
def test_startup_creates_the_sqlite_schema(app_client: TestClient, settings):
    import sqlite3

    path = settings.sqlite_path
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert {
        "discord_guilds",
        "discord_channels",
        "discord_access_requests",
        "discord_messages",
        "discord_monitors",
        "discord_notifications",
    } <= tables


def test_startup_creates_the_documented_indexes(app_client: TestClient, settings):
    import sqlite3

    with sqlite3.connect(settings.sqlite_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }

    assert "ux_discord_messages_identity" in indexes
    assert "ux_access_requests_open_channel" in indexes
    assert "ix_access_requests_status_next_check" in indexes


def test_data_persists_across_a_restart(settings, fake_discord: FakeDiscord):
    """(20) SQLite persistence: a second app instance sees the first one's data."""

    from app.main import create_app

    with TestClient(
        create_app(settings, rest_transport=fake_discord.transport())
    ) as first:
        first.post(f"/discord/channels/{PUBLIC_CHANNEL_ID}/scrape", json={})
        first.post(f"/discord/channels/{PRIVATE_CHANNEL_ID}/access-request", json={})

    with TestClient(
        create_app(settings, rest_transport=fake_discord.transport())
    ) as second:
        messages = second.get(
            f"/discord/channels/{PUBLIC_CHANNEL_ID}/messages"
        ).json()
        requests = second.get("/discord/access-requests").json()

    assert messages["pagination"]["total"] == 10
    assert requests["pagination"]["total"] == 1
    assert requests["items"][0]["status"] == "PENDING"


def test_worker_starts_exactly_once_for_the_lock_holder(
    settings, fake_discord: FakeDiscord
):
    """(21) Only the process holding the worker lock runs background work."""

    from app.main import create_app

    with_workers = settings.model_copy(
        update={"enable_background_workers": True, "enable_gateway": False}
    )

    app_one = create_app(with_workers, rest_transport=fake_discord.transport())
    app_two = create_app(with_workers, rest_transport=fake_discord.transport())

    with TestClient(app_one) as first:
        assert app_one.state.is_worker_leader is True
        assert app_one.state.worker is not None
        assert app_one.state.worker.is_running is True

        # A second application instance contending for the same lock file must not
        # start a duplicate worker.
        with TestClient(app_two) as second:
            assert app_two.state.worker is None or (
                app_two.state.is_worker_leader is False
            )
            assert second.get("/health").json()["status"] == "ok"

        assert first.get("/discord/health").json()["worker"]["running"] is True


def test_shutdown_stops_the_worker_and_closes_resources(
    settings, fake_discord: FakeDiscord
):
    from app.main import create_app

    with_workers = settings.model_copy(
        update={"enable_background_workers": True, "enable_gateway": False}
    )
    application = create_app(with_workers, rest_transport=fake_discord.transport())

    with TestClient(application) as client:
        client.get("/health")
        worker = application.state.worker
        assert worker.is_running is True

    assert worker.is_running is False
    assert application.state.worker_lock.acquired is False


def test_gateway_disabled_is_reported_not_an_error(app_client: TestClient):
    gateway = app_client.get("/discord/health").json()["gateway"]

    assert gateway["enabled"] is False
    assert gateway["status"] == "disabled"
