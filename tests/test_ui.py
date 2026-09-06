"""The browser console served at /ui."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.api.routes_ui import UI_FILE


def test_console_asset_ships_with_the_package():
    assert UI_FILE.is_file(), f"console asset missing at {UI_FILE}"


def test_console_is_served(app_client: TestClient):
    response = app_client.get("/ui")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Discord Collection Service" in response.text


def test_root_redirects_to_the_console(app_client: TestClient):
    response = app_client.get("/", follow_redirects=False)

    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/ui"


def test_console_is_self_contained(app_client: TestClient):
    """No external CSS/JS/font: the console must work offline and air-gapped."""

    html = app_client.get("/ui").text
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)

    assert external == [], f"console pulls external assets: {external}"


def test_console_only_calls_documented_endpoints(app_client: TestClient):
    """Every path the console fetches must exist in the OpenAPI document."""

    html = app_client.get("/ui").text
    documented = set(app_client.get("/openapi.json").json()["paths"])

    # Collect fetch targets, normalising `${var}` interpolations to {param}.
    called = set()
    for raw in re.findall(r'api\(\s*"[A-Z]+"\s*,\s*[`"\']([^`"\']+)', html):
        path = raw.split("?")[0]
        path = re.sub(r"\$\{[^}]+\}", "{id}", path)
        called.add(path)

    def matches(path: str) -> bool:
        for candidate in documented:
            pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", candidate) + "$"
            if re.match(pattern, path):
                return True
        return False

    unknown = sorted(p for p in called if not matches(p))
    assert unknown == [], f"console calls undocumented endpoints: {unknown}"
    assert len(called) >= 10, f"expected the console to exercise the API, saw {called}"


def test_console_is_excluded_from_the_openapi_schema(app_client: TestClient):
    """The console is a page, not part of the machine-readable API surface."""

    paths = app_client.get("/openapi.json").json()["paths"]

    assert "/ui" not in paths
    assert "/" not in paths


def test_console_does_not_embed_secrets(app_client: TestClient, settings):
    html = app_client.get("/ui").text

    assert settings.bot_token not in html
    assert "DISCORD_BOT_TOKEN=" not in html


def test_console_edits_are_picked_up_without_restart(app_client: TestClient):
    """The handler reads the file per request, so a refresh reflects an edit."""

    original = UI_FILE.read_text(encoding="utf-8")
    try:
        UI_FILE.write_text(original.replace("</body>", "<!--probe--></body>"), encoding="utf-8")
        assert "<!--probe-->" in app_client.get("/ui").text
    finally:
        UI_FILE.write_text(original, encoding="utf-8")

    assert "<!--probe-->" not in app_client.get("/ui").text


def test_guild_field_is_a_dropdown_not_a_free_text_id(app_client: TestClient):
    """A server name typed into an id field was a real 422; a select removes it."""

    html = app_client.get("/ui").text

    assert '<select id="channelGuildId">' in html
    assert '<input id="channelGuildId"' not in html


def test_channel_fields_resolve_names_instead_of_regex_rejecting(app_client: TestClient):
    """Every channel input goes through resolveChannelId, not a raw snowflake guard."""

    html = app_client.get("/ui").text

    assert "async function resolveChannelId" in html
    # The old guard rejected anything non-numeric outright.
    assert "must be a numeric snowflake." not in html
    for field in ("evalChannelId", "reqChannelId", "scrapeChannelId", "monChannelId"):
        assert f'resolveChannelId($("{field}").value' in html, f"{field} not resolved"


def test_console_explains_the_zero_server_state(app_client: TestClient):
    """With no guilds, every tab is empty; the console must say why and how to fix it."""

    html = app_client.get("/ui").text

    assert "showInviteBanner" in html
    assert "oauth2/authorize" in html
    assert "permissions=66560" in html


def test_console_translates_validation_errors(app_client: TestClient):
    """Raw pydantic pattern errors are unreadable; the console rewrites them."""

    html = app_client.get("/ui").text

    assert "function friendlyError" in html
    assert "VALIDATION_ERROR" in html
    assert "ACCESS_NOT_GRANTED" in html
    assert "DISCORD_NOT_CONFIGURED" in html


def test_status_bar_surfaces_the_server_count(app_client: TestClient):
    """A bot in zero servers sees nothing, so that number belongs in the status bar."""

    html = app_client.get("/ui").text

    assert 'health.counts && health.counts.guilds' in html
    assert 'server${guilds === 1 ? "" : "s"}' in html
    assert "if (!guilds) showInviteBanner();" in html
