"""The API explorer at /explorer: a generic tester driven by the live OpenAPI schema."""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from app.api.routes_ui import EXPLORER_FILE


def _explorer_javascript(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def test_explorer_asset_ships_with_the_package():
    assert EXPLORER_FILE.is_file(), f"explorer asset missing at {EXPLORER_FILE}"


def test_explorer_is_served(app_client: TestClient):
    response = app_client.get("/explorer")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "API Explorer" in response.text


def test_explorer_javascript_parses(app_client: TestClient, tmp_path):
    """A broken script here would silently show an empty endpoint list. See the same
    class of bug that shipped in the console (fixed in a prior commit) -- only a real
    parse catches it, not a substring assertion.
    """

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed; cannot syntax-check the explorer JS")

    script = tmp_path / "explorer.js"
    script.write_text(_explorer_javascript(app_client.get("/explorer").text), encoding="utf-8")

    result = subprocess.run(
        [node, "--check", str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"explorer JS has a syntax error:\n{result.stderr}"


def test_explorer_is_self_contained(app_client: TestClient):
    """No external CSS/JS/font: must work offline, same standard as the console."""

    html = app_client.get("/explorer").text
    external = re.findall(r'(?:src|href)\s*=\s*["\'](https?://[^"\']+)', html)

    assert external == [], f"explorer pulls external assets: {external}"


def test_explorer_reads_the_live_schema_not_a_hardcoded_list(app_client: TestClient):
    """The whole point: it must never hand-list endpoints, or it goes stale."""

    html = app_client.get("/explorer").text

    assert 'fetch("/openapi.json")' in html
    assert "/discord/access-requests" not in html  # no endpoint path is hardcoded


def test_explorer_is_excluded_from_the_openapi_schema(app_client: TestClient):
    paths = app_client.get("/openapi.json").json()["paths"]

    assert "/explorer" not in paths


def test_explorer_does_not_embed_secrets(app_client: TestClient, settings):
    html = app_client.get("/explorer").text

    assert settings.bot_token not in html
    assert "DISCORD_BOT_TOKEN=" not in html


def test_explorer_masks_the_api_key_in_the_displayed_curl_command(app_client: TestClient):
    """The key is sent as a real header but must never appear in the copyable curl text."""

    html = app_client.get("/explorer").text

    assert 'name === "X-API-Key" ? "***" : value' in html
    # And confirm the function that builds the curl text no longer takes the raw key
    # as an argument at all -- it cannot leak what it never receives.
    assert re.search(r"function buildCurl\([^)]*\)", html).group(0) == (
        "function buildCurl(method, url, headers, bodyText)"
    )


def test_console_links_to_the_explorer_and_vice_versa(app_client: TestClient):
    console_html = app_client.get("/ui").text
    explorer_html = app_client.get("/explorer").text

    assert '/explorer"' in console_html
    assert '/ui"' in explorer_html or 'href="/ui"' in explorer_html


def test_explorer_endpoint_grouping_covers_every_tag(app_client: TestClient):
    """Every operation in the schema must land in some group -- none silently dropped.

    This mirrors the explorer's own `collectEndpoints`: group by first tag, else
    "other". If a route were ever added without a tag it must still show up.
    """

    schema = app_client.get("/openapi.json").json()
    all_paths = {
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        for method in methods
        if method in ("get", "post", "put", "patch", "delete")
    }

    assert len(all_paths) >= 24  # every /discord, /auth and /health endpoint
    # Every operation must be tagged (or the explorer's "other" bucket catches it,
    # but we expect the project's convention of tagging everything to hold).
    untagged = [
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        for method, op in methods.items()
        if method in ("get", "post", "put", "patch", "delete") and not op.get("tags")
    ]
    assert untagged == [], f"untagged operations would land in 'other': {untagged}"
