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


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/styles.css"])
def test_dashboard_files_are_always_revalidated(client: TestClient, path: str) -> None:
    # Otherwise a browser can mix a cached index.html with a newer app.js.
    response = client.get(path)

    assert response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers  # so revalidation can answer 304


def test_missing_static_file_is_404(client: TestClient) -> None:
    assert client.get("/static/nope.js").status_code == 404


def test_dashboard_is_not_part_of_the_api_schema(client: TestClient) -> None:
    assert "/" not in client.get("/openapi.json").json()["paths"]


# Every API that turns a string into markup, plus inline event handlers set
# through setAttribute (SVG elements accept them too).
UNSAFE_JS_APIS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "DOMParser",
    "createContextualFragment",
    "setHTMLUnsafe",
    "parseHTMLUnsafe",
    'setAttribute("on',
    "setAttribute('on",
)


@pytest.mark.parametrize("unsafe", UNSAFE_JS_APIS)
def test_app_js_never_parses_html(unsafe: str) -> None:
    # Guard against XSS: event payloads contain prompts and code we don't
    # control, so the dashboard builds HTML and SVG with createElement /
    # createElementNS and inserts all text with textContent.
    source = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert unsafe not in source, f"app.js uses {unsafe}"


def test_styles_respect_reduced_motion() -> None:
    # The flow dashboard animates; people who ask for less motion get none.
    source = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    assert "@media (prefers-reduced-motion: reduce)" in source
