"""Discord OAuth2 login: session signing, the CSRF state check, and the callback."""

from __future__ import annotations

import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.sessions import SESSION_COOKIE, sign, verify
from app.discord.oauth_service import OAuthFailedError, OAuthNotConfiguredError
from tests.fake_discord import FakeDiscord

APP_ID = "900000000000000001"
CLIENT_SECRET = "test-client-secret-value"
SESSION_KEY = "test-session-signing-key-0123456789"
DISCORD_USER = {
    "id": "444456789012345678",
    "username": "nandeep",
    "global_name": "Nandeep",
    "avatar": "abc123",
}


@pytest.fixture
def oauth_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "discord_application_id": APP_ID,
            "discord_client_secret": CLIENT_SECRET,
            "session_secret": SESSION_KEY,
            "discord_oauth_redirect_uri": "http://testserver/auth/discord/callback",
        }
    )


@pytest.fixture
def oauth_discord(fake_discord: FakeDiscord) -> FakeDiscord:
    """Fake that also answers the OAuth2 token exchange and bearer profile lookup."""

    base = fake_discord.handler

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/oauth2/token/revoke"):
            return httpx.Response(200, json={})
        if path.endswith("/oauth2/token"):
            body = request.content.decode()
            if "code=good-code" not in body:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(
                200, json={"access_token": "user-access-token", "token_type": "Bearer"}
            )
        if path.endswith("/users/@me") and request.headers.get(
            "Authorization", ""
        ).startswith("Bearer "):
            return httpx.Response(200, json=DISCORD_USER)
        return base(request)

    fake_discord.transport = lambda: httpx.MockTransport(handler)  # type: ignore[method-assign]
    return fake_discord


@pytest.fixture
def oauth_client(oauth_settings: Settings, oauth_discord: FakeDiscord):
    from app.main import create_app

    app = create_app(oauth_settings, rest_transport=oauth_discord.transport())
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ session cookies --
def test_signed_payload_round_trips():
    token = sign({"sub": "123", "username": "n"}, SESSION_KEY, ttl_seconds=60)
    payload = verify(token, SESSION_KEY)

    assert payload["sub"] == "123"
    assert payload["username"] == "n"
    assert payload["exp"] > time.time()


def test_tampered_payload_is_rejected():
    token = sign({"sub": "123"}, SESSION_KEY, ttl_seconds=60)
    encoded, signature = token.split(".", 1)
    forged = sign({"sub": "999"}, "another-key-entirely", ttl_seconds=60).split(".", 1)[0]

    assert verify(f"{forged}.{signature}", SESSION_KEY) is None


def test_wrong_secret_is_rejected():
    assert verify(sign({"sub": "1"}, SESSION_KEY, ttl_seconds=60), "different") is None


def test_expired_session_is_rejected():
    assert verify(sign({"sub": "1"}, SESSION_KEY, ttl_seconds=-1), SESSION_KEY) is None


@pytest.mark.parametrize("bad", ["", None, "garbage", "a.b.c", "only-one-part", "..."])
def test_malformed_tokens_never_raise(bad):
    assert verify(bad, SESSION_KEY) is None


def test_session_payload_carries_no_token():
    token = sign({"sub": "1", "username": "n"}, SESSION_KEY, ttl_seconds=60)
    decoded = json.loads(
        __import__("base64").urlsafe_b64decode(token.split(".")[0] + "===")
    )

    assert "access_token" not in decoded
    assert "refresh_token" not in decoded


# ----------------------------------------------------------------- configuration --
def test_oauth_disabled_without_a_secret(settings: Settings):
    assert settings.oauth_enabled is False


def test_oauth_enabled_when_fully_configured(oauth_settings: Settings):
    assert oauth_settings.oauth_enabled is True
    assert oauth_settings.oauth_scope_list == ["identify"]


def test_client_secret_never_appears_in_public_summary(oauth_settings: Settings):
    assert CLIENT_SECRET not in str(oauth_settings.public_summary())
    assert oauth_settings.public_summary()["oauth_login_enabled"] is True


async def test_login_without_configuration_is_rejected(services):
    with pytest.raises(OAuthNotConfiguredError):
        services.oauth.build_login_redirect()


# ------------------------------------------------------------------- login redirect --
def test_login_redirects_to_discord(oauth_client: TestClient):
    response = oauth_client.get("/auth/discord/login", follow_redirects=False)

    assert response.status_code == 307
    target = urlparse(response.headers["location"])
    params = parse_qs(target.query)
    assert target.netloc == "discord.com"
    assert params["client_id"] == [APP_ID]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["identify"]
    assert params["state"][0]


def test_login_sets_a_state_cookie(oauth_client: TestClient):
    response = oauth_client.get("/auth/discord/login", follow_redirects=False)

    assert "discord_service_oauth_state" in response.cookies
    assert "httponly" in response.headers["set-cookie"].lower()


def test_each_login_uses_a_fresh_state(oauth_client: TestClient):
    first = oauth_client.get("/auth/discord/login", follow_redirects=False)
    second = oauth_client.get("/auth/discord/login", follow_redirects=False)

    state_of = lambda r: parse_qs(urlparse(r.headers["location"]).query)["state"][0]  # noqa: E731
    assert state_of(first) != state_of(second)


# ------------------------------------------------------------------- CSRF defence --
def test_callback_without_state_is_rejected(oauth_client: TestClient):
    response = oauth_client.get(
        "/auth/discord/callback?code=good-code", follow_redirects=False
    )

    assert response.status_code == 400
    assert response.json()["code"] == "OAUTH_FAILED"


def test_callback_with_a_forged_state_is_rejected(oauth_client: TestClient):
    """The core CSRF check: an attacker-supplied state must not authenticate."""

    oauth_client.get("/auth/discord/login", follow_redirects=False)

    response = oauth_client.get(
        "/auth/discord/callback?code=good-code&state=attacker-chosen",
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert response.json()["details"]["reason"] == "STATE_MISMATCH"
    assert SESSION_COOKIE not in response.cookies


def test_callback_without_a_state_cookie_is_rejected(oauth_client: TestClient):
    """Someone replaying a callback URL has no matching cookie."""

    login = oauth_client.get("/auth/discord/login", follow_redirects=False)
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    oauth_client.cookies.clear()

    response = oauth_client.get(
        f"/auth/discord/callback?code=good-code&state={state}", follow_redirects=False
    )

    assert response.status_code == 400
    assert response.json()["details"]["reason"] == "MISSING_STATE"


# ------------------------------------------------------------------ successful login --
def _login(client: TestClient) -> httpx.Response:
    redirect = client.get("/auth/discord/login", follow_redirects=False)
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]
    return client.get(
        f"/auth/discord/callback?code=good-code&state={state}", follow_redirects=False
    )


def test_successful_login_issues_a_session(oauth_client: TestClient):
    response = _login(oauth_client)

    assert response.status_code == 307
    assert response.headers["location"] == "/ui?login=ok"
    assert SESSION_COOKIE in response.cookies


def test_whoami_reports_the_logged_in_user(oauth_client: TestClient):
    _login(oauth_client)

    body = oauth_client.get("/auth/me").json()

    assert body["authenticated"] is True
    assert body["user"]["discord_user_id"] == DISCORD_USER["id"]
    assert body["user"]["username"] == "nandeep"
    assert body["oauth_enabled"] is True


def test_login_is_recorded_and_counted(oauth_client: TestClient, oauth_settings: Settings):
    """Repeat logins update one row rather than creating duplicates."""

    import sqlite3

    _login(oauth_client)
    _login(oauth_client)

    with sqlite3.connect(oauth_settings.sqlite_path) as connection:
        rows = list(
            connection.execute(
                "SELECT discord_user_id, username, login_count FROM discord_users"
            )
        )

    assert len(rows) == 1, "a repeat login must not create a second user row"
    user_id, username, login_count = rows[0]
    assert user_id == DISCORD_USER["id"]
    assert username == "nandeep"
    assert login_count == 2


def test_no_discord_token_is_persisted(oauth_client: TestClient, oauth_settings: Settings):
    """The access token is used once and discarded, never written to storage."""

    import sqlite3

    _login(oauth_client)

    with sqlite3.connect(oauth_settings.sqlite_path) as connection:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(discord_users)")
        ]
        contents = str(list(connection.execute("SELECT * FROM discord_users")))

    assert not [c for c in columns if "token" in c.lower()], columns
    assert "user-access-token" not in contents


def test_bad_code_is_reported_cleanly(oauth_client: TestClient):
    redirect = oauth_client.get("/auth/discord/login", follow_redirects=False)
    state = parse_qs(urlparse(redirect.headers["location"]).query)["state"][0]

    response = oauth_client.get(
        f"/auth/discord/callback?code=expired&state={state}", follow_redirects=False
    )

    assert response.status_code == 400
    assert response.json()["details"]["reason"] == "CODE_EXCHANGE_FAILED"


def test_user_declining_is_not_an_error(oauth_client: TestClient):
    response = oauth_client.get(
        "/auth/discord/callback?error=access_denied", follow_redirects=False
    )

    assert response.status_code == 307
    assert response.headers["location"] == "/ui?login=denied"


def test_logout_clears_the_session(oauth_client: TestClient):
    _login(oauth_client)
    assert oauth_client.get("/auth/me").json()["authenticated"] is True

    assert oauth_client.post("/auth/logout").status_code == 204
    assert oauth_client.get("/auth/me").json()["authenticated"] is False


# ---------------------------------------------------- session as API authentication --
def test_session_authenticates_discord_endpoints(oauth_client: TestClient):
    """A logged-in human needs no API key."""

    assert oauth_client.get("/discord/guilds").status_code == 401

    _login(oauth_client)

    assert oauth_client.get("/discord/guilds").status_code == 200


def test_api_key_still_works_alongside_oauth(oauth_settings: Settings, oauth_discord):
    """Server-to-server integration keeps using a key while humans log in."""

    from app.main import create_app

    both = oauth_settings.model_copy(update={"api_keys": "dsk_service_key"})
    with TestClient(create_app(both, rest_transport=oauth_discord.transport())) as client:
        assert client.get("/discord/guilds").status_code == 401
        assert (
            client.get("/discord/guilds", headers={"X-API-Key": "dsk_service_key"}).status_code
            == 200
        )


def test_unauthenticated_error_points_at_the_login_url(oauth_client: TestClient):
    body = oauth_client.get("/discord/guilds").json()

    assert body["code"] == "UNAUTHORIZED"
    assert body["details"]["login_url"] == "/auth/discord/login"


def test_health_stays_open_with_oauth_enabled(oauth_client: TestClient):
    assert oauth_client.get("/health").status_code == 200


def test_oauth_never_leaks_the_client_secret(oauth_client: TestClient):
    combined = (
        oauth_client.get("/openapi.json").text
        + oauth_client.get("/auth/me").text
        + oauth_client.get("/health").text
    )

    assert CLIENT_SECRET not in combined
    assert SESSION_KEY not in combined


def test_state_check_rejects_mismatch_directly():
    from app.discord.oauth_service import DiscordOAuthService

    DiscordOAuthService.verify_state("same", "same")  # no raise

    with pytest.raises(OAuthFailedError):
        DiscordOAuthService.verify_state("a", "b")
    with pytest.raises(OAuthFailedError):
        DiscordOAuthService.verify_state(None, "b")
