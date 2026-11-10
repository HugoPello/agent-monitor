"""Tests for the dashboard's static files."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_monitor.api import STATIC_DIR, create_app
from agent_monitor.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(_env_file=None, db_path=tmp_path / "events.db")
    return TestClient(create_app(settings))


def test_root_serves_dashboard_html(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="agent-list"' in response.text
    assert "/static/app.js" in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/static/app.js", "javascript"), ("/static/styles.css", "text/css")],
)
def test_static_assets_are_served(client: TestClient, path: str, content_type: str) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]


def test_missing_static_file_is_404(client: TestClient) -> None:
    assert client.get("/static/nope.js").status_code == 404


def test_dashboard_is_not_part_of_the_api_schema(client: TestClient) -> None:
    assert "/" not in client.get("/openapi.json").json()["paths"]


def test_app_js_never_uses_innerhtml() -> None:
    # Guard against XSS: event payloads contain prompts and code we don't
    # control, so the dashboard must insert all text with textContent.
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe not in source, f"app.js uses {unsafe}"
