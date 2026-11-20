"""Tests for the HTTP API."""

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_monitor.api import create_app
from agent_monitor.config import Settings


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(_env_file=None, db_path=tmp_path / "events.db", t3_enabled=False)
    return TestClient(create_app(settings))


def post(client: TestClient, session_id: str, name: str, **extra: object) -> dict:
    response = client.post(
        "/events", json={"session_id": session_id, "hook_event_name": name, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


class TestPostEvents:
    def test_stores_event_and_returns_it(self, client: TestClient) -> None:
        body = post(client, "s1", "PreToolUse", tool_name="Bash", tool_input={"command": "ls"})

        assert body["id"] > 0
        assert body["session_id"] == "s1"
        assert body["tool_name"] == "Bash"
        assert body["payload"]["tool_input"] == {"command": "ls"}
        assert "received_at" in body

    @pytest.mark.parametrize(
        "body",
        [
            {"hook_event_name": "Stop"},
            {"session_id": "s1"},
            {"session_id": "", "hook_event_name": "Stop"},
        ],
    )
    def test_rejects_invalid_event(self, client: TestClient, body: dict) -> None:
        assert client.post("/events", json=body).status_code == 422

    def test_rejects_non_json_body(self, client: TestClient) -> None:
        response = client.post(
            "/events", content=b"not json", headers={"Content-Type": "application/json"}
        )

        assert response.status_code == 422

    def test_enriches_claude_code_hook_with_provider(self, client: TestClient) -> None:
        response = client.post(
            "/events",
            json={"session_id": "s1", "hook_event_name": "SessionStart"},
            headers={"X-Agent-Monitor-Source": "claude-code"},
        )

        assert response.status_code == 201
        assert response.json()["payload"]["provider"] == "claude"

    def test_deduplicates_hook_inside_t3code_when_t3_source_active(self, tmp_path: Path) -> None:
        t3_db = tmp_path / "statev2.sqlite"
        with sqlite3.connect(t3_db) as conn:
            conn.execute(
                "CREATE TABLE orchestration_v2_projection_threads "
                "(thread_id TEXT PRIMARY KEY, deleted_at TEXT)"
            )
            conn.execute(
                "INSERT INTO orchestration_v2_projection_threads (thread_id) VALUES ('t-1')"
            )
        settings = Settings(
            _env_file=None,
            db_path=tmp_path / "events.db",
            t3_enabled=True,
            t3_db_path=t3_db,
        )
        t3_client = TestClient(create_app(settings))
        queue = t3_client.app.state.broadcaster.subscribe()

        response = t3_client.post(
            "/events",
            json={"session_id": "s1", "hook_event_name": "Stop"},
            headers={"X-Agent-Monitor-Source": "claude-code", "X-T3Code-Thread-Id": "t-1"},
        )

        assert response.status_code == 204
        assert t3_client.get("/agents").json() == []
        assert queue.empty()

    def test_keeps_hook_when_t3code_thread_not_in_t3_db(self, tmp_path: Path) -> None:
        t3_db = tmp_path / "statev2.sqlite"
        with sqlite3.connect(t3_db) as conn:
            conn.execute(
                "CREATE TABLE orchestration_v2_projection_threads "
                "(thread_id TEXT PRIMARY KEY, deleted_at TEXT)"
            )
            conn.execute(
                "INSERT INTO orchestration_v2_projection_threads (thread_id) VALUES ('other-t')"
            )
        settings = Settings(
            _env_file=None,
            db_path=tmp_path / "events.db",
            t3_enabled=True,
            t3_db_path=t3_db,
        )
        t3_client = TestClient(create_app(settings))

        response = t3_client.post(
            "/events",
            json={"session_id": "s1", "hook_event_name": "Stop"},
            headers={"X-Agent-Monitor-Source": "claude-code", "X-T3Code-Thread-Id": "missing-t"},
        )

        assert response.status_code == 201
        assert len(t3_client.get("/agents").json()) == 1

    def test_keeps_hook_inside_t3code_when_t3_source_disabled(self, client: TestClient) -> None:
        response = client.post(
            "/events",
            json={"session_id": "s1", "hook_event_name": "Stop"},
            headers={"X-Agent-Monitor-Source": "claude-code", "X-T3Code-Thread-Id": "t-1"},
        )

        assert response.status_code == 201
        assert len(client.get("/agents").json()) == 1


class TestListAgents:
    def test_empty(self, client: TestClient) -> None:
        assert client.get("/agents").json() == []

    def test_summary_per_session(self, client: TestClient) -> None:
        post(client, "s1", "SessionStart", cwd="/project", source="startup")
        post(client, "s1", "UserPromptSubmit", cwd="/project", prompt="hi")
        post(client, "s1", "PreToolUse", cwd="/project", tool_name="Edit")

        [agent] = client.get("/agents").json()

        assert agent["session_id"] == "s1"
        assert agent["status"] == "running_tool"
        assert agent["activity"] == "coding"
        assert agent["current_tool"] == "Edit"
        assert agent["cwd"] == "/project"
        assert agent["event_count"] == 3
        assert agent["subagents"] == []
        assert agent["waiting_reason"] is None
        assert agent["failed_steps"] == 0

    def test_most_recently_active_first(self, client: TestClient) -> None:
        post(client, "old", "SessionStart")
        post(client, "new", "SessionStart")

        ids = [a["session_id"] for a in client.get("/agents").json()]

        assert ids == ["new", "old"]

    def test_includes_subagents(self, client: TestClient) -> None:
        post(client, "s1", "PreToolUse", tool_name="Agent")
        post(client, "s1", "SubagentStart", agent_id="a1", agent_type="Explore")
        post(client, "s1", "SubagentStop", agent_id="a1", agent_type="Explore")

        [agent] = client.get("/agents").json()

        assert agent["status"] == "running_tool"
        assert agent["activity"] == "delegating"
        assert agent["subagents"] == [
            {
                "agent_id": "a1",
                "agent_type": "Explore",
                "status": "done",
                "activity": "ended",
                "started_at": agent["subagents"][0]["started_at"],
                "last_event_at": agent["subagents"][0]["last_event_at"],
                "failed_steps": 0,
                "provider": None,
                "model": None,
            }
        ]
        assert agent["provider"] is None
        assert agent["model"] is None
        assert agent["title"] is None


class TestAgentEvents:
    def test_timeline_in_order(self, client: TestClient) -> None:
        for name in ["SessionStart", "UserPromptSubmit", "Stop"]:
            post(client, "s1", name)
        post(client, "s2", "SessionStart")

        events = client.get("/agents/s1/events").json()

        assert [e["hook_event_name"] for e in events] == [
            "SessionStart",
            "UserPromptSubmit",
            "Stop",
        ]

    def test_unknown_session_is_404(self, client: TestClient) -> None:
        assert client.get("/agents/nope/events").status_code == 404


class TestStreamPublishing:
    # The /stream endpoint itself never ends, so instead of reading it through
    # TestClient we subscribe to the app's broadcaster directly. The stream
    # generator is covered in test_sse.py.

    def test_post_publishes_event_with_updated_summary(self, client: TestClient) -> None:
        queue = client.app.state.broadcaster.subscribe()

        post(client, "s1", "UserPromptSubmit", prompt="hi")
        stored = post(client, "s1", "PreToolUse", tool_name="Bash")

        queue.get_nowait()  # first event
        message = queue.get_nowait()
        assert message.event == "agent_event"
        assert message.id == str(stored["id"])

        data = json.loads(message.data)
        assert data["event"]["hook_event_name"] == "PreToolUse"
        assert data["agent"]["session_id"] == "s1"
        assert data["agent"]["status"] == "running_tool"
        assert data["agent"]["activity"] == "running"
        assert data["agent"]["event_count"] == 2

    def test_stream_carries_subagent_activity(self, client: TestClient) -> None:
        queue = client.app.state.broadcaster.subscribe()

        post(client, "s1", "PreToolUse", tool_name="Agent")
        post(client, "s1", "PreToolUse", agent_id="a1", agent_type="Explore", tool_name="Grep")

        queue.get_nowait()  # first event
        data = json.loads(queue.get_nowait().data)
        assert data["agent"]["activity"] == "delegating"
        [subagent] = data["agent"]["subagents"]
        assert subagent["agent_type"] == "Explore"
        assert subagent["status"] == "running"
        assert subagent["activity"] == "researching"

    def test_invalid_event_is_not_published(self, client: TestClient) -> None:
        queue = client.app.state.broadcaster.subscribe()

        client.post("/events", json={"hook_event_name": "Stop"})

        assert queue.empty()
