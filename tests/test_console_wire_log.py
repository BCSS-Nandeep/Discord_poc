"""The wire-log console at /console: a denser alternative to /ui with an editable
API base URL, an inline detail panel and a live request/response log."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.api.routes_ui import CONSOLE_FILE


def _console_javascript(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def test_console_asset_ships_with_the_package():
    assert CONSOLE_FILE.is_file(), f"console asset missing at {CONSOLE_FILE}"


def test_console_is_served(app_client: TestClient):
    response = app_client.get("/console")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Discord Service Console" in response.text


def test_console_javascript_parses(app_client: TestClient, tmp_path):
    """A broken inline script here would render the page but leave every tab
    inert -- only a real parse catches that, not a substring assertion."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot syntax-check the console JS")

    script = tmp_path / "console.js"
    script.write_text(_console_javascript(app_client.get("/console").text), encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"console JS has a syntax error:\n{result.stderr}"


def test_console_is_self_contained(app_client: TestClient):
    """No external CSS/JS/font: must work fully offline, same as /ui and /explorer."""

    html = app_client.get("/console").text
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)

    assert external == [], f"console pulls external assets: {external}"


def test_console_is_excluded_from_the_openapi_schema(app_client: TestClient):
    paths = app_client.get("/openapi.json").json()["paths"]

    assert "/console" not in paths


def test_console_does_not_embed_secrets(app_client: TestClient, settings):
    html = app_client.get("/console").text

    assert settings.bot_token not in html
    assert "DISCORD_BOT_TOKEN=" not in html


def test_console_sends_the_shared_api_key_header(app_client: TestClient):
    """The key box must use the same localStorage key as /ui and /explorer, so
    setting it once on any page carries over to the others."""

    html = app_client.get("/console").text

    assert '"discord-service-api-key"' in html
    assert 'headers["X-API-Key"] = key' in html


def test_console_covers_every_discord_api_path(app_client: TestClient):
    """Every /discord/* path segment used in fetch calls must be a real, current
    endpoint -- this is what would break silently if an endpoint were ever renamed."""

    html = app_client.get("/console").text
    schema_paths = set(app_client.get("/openapi.json").json()["paths"])

    used = set(re.findall(r'"(/discord/[a-zA-Z0-9_\-{}/]*)', html))
    # Template segments in the JS are string-concatenated (e.g. "/discord/guilds/"+id),
    # so compare prefixes against the schema rather than exact matches.
    for path in used:
        assert any(
            schema_path.startswith(path) or path.startswith(schema_path.split("{")[0])
            for schema_path in schema_paths
        ), f"console references a path with no matching schema entry: {path}"


def test_console_explains_the_application_level_access_workflow(app_client: TestClient):
    """The one fact that must never be misrepresented in any UI this service ships."""

    html = app_client.get("/console").text

    assert "not a Discord approval" in html
    assert "Discord has no API for a bot to request private-channel access" in html


def test_console_cross_links_the_other_two_pages(app_client: TestClient):
    console_html = app_client.get("/console").text
    ui_html = app_client.get("/ui").text
    explorer_html = app_client.get("/explorer").text

    assert 'href="/ui"' in console_html
    assert 'href="/explorer"' in console_html
    assert 'href="/console"' in ui_html
    assert 'href="/console"' in explorer_html


def test_console_origin_guard_matches_this_services_auth_model(app_client: TestClient):
    """Adapted from the reference design: this service supports optional auth
    (API key / session), so the CORS explanation must not claim there is none."""

    html = app_client.get("/console").text

    assert "This API can require an" in html
    assert "X-API-Key" in html


def test_console_never_claims_it_can_add_the_bot_to_a_server(app_client: TestClient):
    """A human being a member of a server must never be conflated with bot access."""

    html = app_client.get("/console").text

    assert "no API call can do it" in html
    assert "does not grant the bot access" in html
