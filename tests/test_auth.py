"""API key authentication for consuming applications."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security import generate_api_key
from tests.fake_discord import PUBLIC_CHANNEL_ID, FakeDiscord

VALID_KEY = "dsk_test_valid_key_value_0123456789"
SECOND_KEY = "dsk_test_second_key_value_9876543"


@pytest.fixture
def secured_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"api_keys": f"{VALID_KEY},{SECOND_KEY}"})


@pytest.fixture
def secured_client(secured_settings: Settings, fake_discord: FakeDiscord):
    from app.main import create_app

    app = create_app(secured_settings, rest_transport=fake_discord.transport())
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------- configuration --
def test_auth_is_disabled_by_default(settings: Settings):
    assert settings.auth_enabled is False
    assert settings.api_key_list == []


def test_keys_are_parsed_and_trimmed():
    parsed = Settings(_env_file=None, api_keys=" a , b ,, c ")

    assert parsed.api_key_list == ["a", "b", "c"]
    assert parsed.auth_enabled is True


def test_generated_keys_are_unique_and_prefixed():
    first, second = generate_api_key(), generate_api_key()

    assert first.startswith("dsk_") and len(first) > 20
    assert first != second


def test_public_summary_reports_auth_without_exposing_keys(secured_settings: Settings):
    summary = secured_settings.public_summary()

    assert summary["auth_enabled"] is True
    assert VALID_KEY not in str(summary)


# ----------------------------------------------------------------------- enforcement --
def test_request_without_a_key_is_rejected(secured_client: TestClient):
    response = secured_client.get("/discord/guilds")

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_request_with_a_wrong_key_is_rejected(secured_client: TestClient):
    response = secured_client.get("/discord/guilds", headers={"X-API-Key": "nope"})

    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_request_with_a_valid_key_succeeds(secured_client: TestClient):
    response = secured_client.get("/discord/guilds", headers={"X-API-Key": VALID_KEY})

    assert response.status_code == 200


def test_every_configured_key_works(secured_client: TestClient):
    """Several consumers can be issued distinct keys."""

    for key in (VALID_KEY, SECOND_KEY):
        assert secured_client.get("/discord/guilds", headers={"X-API-Key": key}).status_code == 200


def test_post_endpoints_are_protected_too(secured_client: TestClient):
    unauthenticated = secured_client.post("/discord/search", json={"keywords": []})
    authenticated = secured_client.post(
        "/discord/search", json={"keywords": []}, headers={"X-API-Key": VALID_KEY}
    )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200


def test_all_discord_routes_require_the_key(secured_client: TestClient):
    """No endpoint may be accidentally left off the protected router."""

    schema = secured_client.get("/openapi.json").json()
    discord_paths = [p for p in schema["paths"] if p.startswith("/discord/")]
    assert discord_paths, "expected discord endpoints in the schema"

    for path in discord_paths:
        probe = path.replace("{channel_id}", PUBLIC_CHANNEL_ID)
        probe = probe.replace("{guild_id}", "100000000000000001")
        probe = probe.replace("{request_id}", "1").replace("{notification_id}", "1")
        for method in schema["paths"][path]:
            response = secured_client.request(method.upper(), probe)
            assert response.status_code == 401, f"{method.upper()} {probe} was not protected"


# ------------------------------------------------------------------- open endpoints --
def test_health_stays_open_for_probes(secured_client: TestClient):
    """Load balancers must not need a secret to check liveness."""

    response = secured_client.get("/health")

    assert response.status_code == 200
    assert response.json()["auth"] == "enabled"


def test_health_reports_when_auth_is_disabled(app_client: TestClient):
    assert app_client.get("/health").json()["auth"] == "disabled"


def test_console_page_stays_open(secured_client: TestClient):
    """The page loads; its own fetches carry the key from browser storage."""

    assert secured_client.get("/ui").status_code == 200


def test_health_never_leaks_a_key(secured_client: TestClient):
    body = secured_client.get("/health").text + secured_client.get("/openapi.json").text

    assert VALID_KEY not in body
    assert SECOND_KEY not in body


# ---------------------------------------------------------------------- disabled mode --
def test_no_key_needed_when_auth_is_disabled(app_client: TestClient):
    """Default local development stays frictionless."""

    assert app_client.get("/discord/guilds").status_code == 200


# ---------------------------------------------------------------------------- console --
def test_console_sends_the_api_key_header(secured_client: TestClient):
    html = secured_client.get("/ui").text

    assert 'headers["X-API-Key"] = key' in html
    assert "function setApiKey" in html
    assert "UNAUTHORIZED" in html
