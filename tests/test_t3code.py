"""Tests for the T3 Code read-only SQLite source and translator."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_monitor.api import create_app
from agent_monitor.config import Settings
from agent_monitor.server import build_server
from agent_monitor.status import Activity, AgentStatus, SubagentStatus, build_summary
from agent_monitor.storage import EventStore
from agent_monitor.t3code import T3CodeReader, _bounded_add

T3_SCHEMA = """
CREATE TABLE projection_projects (
    project_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE orchestration_v2_projection_threads (
    thread_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    default_provider TEXT NOT NULL,
    provider_instance_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE orchestration_v2_projection_runs (
    run_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    provider TEXT NOT NULL,
    provider_instance_id TEXT,
    status TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE orchestration_v2_projection_subagents (
    subagent_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    run_id TEXT,
    parent_node_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_instance_id TEXT,
    driver TEXT,
    provider_thread_id TEXT,
    child_thread_id TEXT,
    origin TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE orchestration_v2_projection_runtime_requests (
    runtime_request_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE orchestration_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    aggregate_kind TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def create_t3_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(T3_SCHEMA)
    return conn


def insert_project(
    conn: sqlite3.Connection, project_id: str, workspace_root: str, title: str = "demo"
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO projection_projects (project_id, title, workspace_root) VALUES (?, ?, ?)",
            (project_id, title, workspace_root),
        )


def insert_thread(
    conn: sqlite3.Connection,
    *,
    thread_id: str,
    project_id: str,
    title: str,
    provider: str,
    model: str,
    created_at: str,
    parent_thread_id: str | None = None,
    root_thread_id: str | None = None,
    worktree_path: str | None = None,
) -> dict:
    payload = {
        "id": thread_id,
        "projectId": project_id,
        "title": title,
        "providerInstanceId": provider,
        "modelSelection": {"instanceId": provider, "model": model},
        "worktreePath": worktree_path,
        "lineage": {
            "parentThreadId": parent_thread_id,
            "relationshipToParent": "subagent" if parent_thread_id else None,
            "rootThreadId": root_thread_id or thread_id,
        },
        "createdAt": created_at,
        "updatedAt": created_at,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_threads
                (thread_id, project_id, title, default_provider, provider_instance_id,
                 created_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                thread_id,
                project_id,
                title,
                provider,
                provider,
                created_at,
                created_at,
                json.dumps(payload),
            ),
        )
    return payload


def append_event(
    conn: sqlite3.Connection,
    *,
    seq_hint: int,
    aggregate_kind: str,
    stream_id: str,
    event_type: str,
    occurred_at: str,
    payload: dict,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_events
                (event_id, aggregate_kind, stream_id, stream_version,
                 event_type, occurred_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ev-{seq_hint}-{stream_id}-{event_type}",
                aggregate_kind,
                stream_id,
                seq_hint,
                event_type,
                occurred_at,
                json.dumps(payload),
            ),
        )


def test_missing_t3_database_returns_empty(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(tmp_path / "does-not-exist.sqlite")

    assert reader.poll_once(store) == []
    assert store.session_ids() == []


def test_imports_multi_provider_threads_with_timestamps_and_deduplication(
    tmp_path: Path,
) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/alpha")

    t0 = "2026-11-20T10:00:00Z"
    t1 = "2026-11-20T10:00:02Z"
    t2 = "2026-11-20T10:00:05Z"
    t3 = "2026-11-20T10:00:08Z"

    gemini_thread = insert_thread(
        conn,
        thread_id="t-gemini",
        project_id="p1",
        title="Refactor parser",
        provider="antigravity",
        model="gemini-4-neon",
        created_at=t0,
    )
    claude_thread = insert_thread(
        conn,
        thread_id="t-claude",
        project_id="p1",
        title="Write docs",
        provider="claudeAgent",
        model="claude-sonnet-5-5",
        created_at=t0,
    )

    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t-gemini",
        event_type="thread.created",
        occurred_at=t0,
        payload=gemini_thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t-gemini",
        event_type="turn-item.updated",
        occurred_at=t1,
        payload={"id": "msg-1", "threadId": "t-gemini", "type": "user_message", "text": "Go"},
    )
    # Streaming tool updates: two running updates must emit only one PreToolUse.
    for idx, ts in enumerate((t1, t2), start=3):
        append_event(
            conn,
            seq_hint=idx,
            aggregate_kind="thread",
            stream_id="t-gemini",
            event_type="turn-item.updated",
            occurred_at=ts,
            payload={
                "id": "item-read",
                "threadId": "t-gemini",
                "type": "dynamic_tool",
                "toolName": "read_file",
                "status": "running",
                "input": {"Path": "/repos/alpha/app.py"},
            },
        )
    # Ignored reasoning item should produce no event.
    append_event(
        conn,
        seq_hint=5,
        aggregate_kind="thread",
        stream_id="t-gemini",
        event_type="turn-item.updated",
        occurred_at=t2,
        payload={
            "id": "reason-1",
            "threadId": "t-gemini",
            "type": "reasoning",
            "status": "completed",
        },
    )
    # Claude thread finishes a run after transient "waiting" status.
    append_event(
        conn,
        seq_hint=6,
        aggregate_kind="thread",
        stream_id="t-claude",
        event_type="thread.created",
        occurred_at=t0,
        payload=claude_thread,
    )
    append_event(
        conn,
        seq_hint=7,
        aggregate_kind="thread",
        stream_id="t-claude",
        event_type="turn-item.updated",
        occurred_at=t1,
        payload={"id": "msg-2", "threadId": "t-claude", "type": "user_message", "text": "Docs"},
    )
    append_event(
        conn,
        seq_hint=8,
        aggregate_kind="thread",
        stream_id="t-claude",
        event_type="turn-item.updated",
        occurred_at=t2,
        payload={
            "id": "asst-2",
            "threadId": "t-claude",
            "type": "assistant_message",
            "status": "completed",
            "text": "Updated README.",
        },
    )
    append_event(
        conn,
        seq_hint=9,
        aggregate_kind="thread",
        stream_id="t-claude",
        event_type="run.updated",
        occurred_at=t2,
        payload={"id": "run-c1", "threadId": "t-claude", "status": "waiting"},
    )
    append_event(
        conn,
        seq_hint=10,
        aggregate_kind="thread",
        stream_id="t-claude",
        event_type="run.updated",
        occurred_at=t3,
        payload={"id": "run-c1", "threadId": "t-claude", "status": "completed"},
    )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)

    stored = reader.poll_once(store)
    assert len(stored) == 6
    # Polling again with a fresh reader must be idempotent.
    assert T3CodeReader(t3_path).poll_once(store) == []

    now = datetime(2026, 11, 20, 10, 0, 10, tzinfo=UTC)
    gemini_summary = build_summary(
        "t-gemini", store.events_for_session("t-gemini"), now, timedelta(minutes=10)
    )
    assert gemini_summary.provider == "antigravity"
    assert gemini_summary.model == "gemini-4-neon"
    assert gemini_summary.title == "Refactor parser"
    assert gemini_summary.cwd == "/repos/alpha"
    assert gemini_summary.status == AgentStatus.RUNNING_TOOL
    assert gemini_summary.activity == Activity.RESEARCHING
    assert gemini_summary.current_tool == "Read"

    claude_summary = build_summary(
        "t-claude", store.events_for_session("t-claude"), now, timedelta(minutes=10)
    )
    assert claude_summary.provider == "claude"
    assert claude_summary.model == "claude-sonnet-5-5"
    assert claude_summary.activity == Activity.FINISHED
    claude_events = store.events_for_session("t-claude")
    assert claude_events[-1].payload["last_assistant_message"] == "Updated README."


def test_command_exit_code_nonzero_maps_to_post_tool_use_failure(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/alpha")
    t0 = "2026-11-20T10:00:00Z"
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Run tests",
        provider="opencode",
        model="opencode/big-pickle",
        created_at=t0,
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at=t0,
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={"id": "u1", "threadId": "t1", "type": "user_message", "text": "test"},
    )
    append_event(
        conn,
        seq_hint=3,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:03Z",
        payload={
            "id": "cmd-1",
            "threadId": "t1",
            "type": "command_execution",
            "status": "completed",
            "input": "pytest -q",
            "exitCode": 1,
            "startedAt": "2026-11-20T10:00:02Z",
        },
    )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    T3CodeReader(t3_path).poll_once(store)

    events = store.events_for_session("t1")
    assert [e.hook_event_name for e in events] == [
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUseFailure",
    ]
    assert events[-1].tool_name == "Command"
    assert events[-1].payload["error"] == "exit code 1"

    summary = build_summary(
        "t1", events, datetime(2026, 11, 20, 10, 0, 5, tzinfo=UTC), timedelta(minutes=10)
    )
    assert summary.failed_steps == 1
    assert summary.last_failure_tool == "Command"


def test_cross_provider_subagent_child_thread_lifecycle(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/alpha")
    t0 = "2026-11-20T10:00:00Z"

    parent = insert_thread(
        conn,
        thread_id="root-1",
        project_id="p1",
        title="Build feature",
        provider="antigravity",
        model="gemini-pro-orchestrator-neon",
        created_at=t0,
    )
    child = insert_thread(
        conn,
        thread_id="child-opus",
        project_id="p1",
        title="Opus review",
        provider="claudeAgent",
        model="claude-opus-5-5",
        created_at="2026-11-20T10:00:02Z",
        parent_thread_id="root-1",
        root_thread_id="root-1",
        worktree_path="/repos/alpha-wt",
    )

    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="root-1",
        event_type="thread.created",
        occurred_at=t0,
        payload=parent,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="root-1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={
            "id": "del-1",
            "threadId": "root-1",
            "type": "dynamic_tool",
            "toolName": "t3-code.delegate_task",
            "status": "running",
            "input": {"task": "review"},
        },
    )
    append_event(
        conn,
        seq_hint=3,
        aggregate_kind="thread",
        stream_id="child-opus",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:02Z",
        payload=child,
    )
    append_event(
        conn,
        seq_hint=4,
        aggregate_kind="thread",
        stream_id="child-opus",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:03Z",
        payload={
            "id": "sub-edit",
            "threadId": "child-opus",
            "type": "dynamic_tool",
            "toolName": "Edit",
            "status": "running",
            "input": {"file_path": "/repos/alpha-wt/main.py"},
        },
    )

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    now = datetime(2026, 11, 20, 10, 0, 4, tzinfo=UTC)
    summary = build_summary(
        "root-1", store.events_for_session("root-1"), now, timedelta(minutes=10)
    )
    assert summary.provider == "antigravity"
    assert summary.model == "gemini-pro-orchestrator-neon"
    assert summary.activity == Activity.DELEGATING
    assert len(summary.subagents) == 1
    [sub] = summary.subagents
    assert sub.agent_id == "child-opus"
    assert sub.agent_type == "Opus review"
    assert sub.provider == "claude"
    assert sub.model == "claude-opus-5-5"
    assert sub.status == SubagentStatus.RUNNING
    assert sub.activity == Activity.CODING

    # Now the child completes its run; parent remains delegating until parent finishes.
    append_event(
        conn,
        seq_hint=5,
        aggregate_kind="thread",
        stream_id="child-opus",
        event_type="run.updated",
        occurred_at="2026-11-20T10:00:06Z",
        payload={"id": "run-child", "threadId": "child-opus", "status": "completed"},
    )
    conn.close()

    reader.poll_once(store)
    summary2 = build_summary(
        "root-1",
        store.events_for_session("root-1"),
        datetime(2026, 11, 20, 10, 0, 7, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary2.activity == Activity.DELEGATING
    assert summary2.subagents[0].status == SubagentStatus.DONE
    assert summary2.subagents[0].activity == Activity.ENDED


def test_provider_native_subagents_and_runtime_requests(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/alpha")
    t0 = "2026-11-20T10:00:00Z"
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Native subagent test",
        provider="claudeAgent",
        model="claude-opus-5-5",
        created_at=t0,
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at=t0,
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={"id": "u1", "threadId": "t1", "type": "user_message", "text": "go"},
    )
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_subagents
                (subagent_id, thread_id, run_id, parent_node_id, provider,
                 provider_instance_id, driver, provider_thread_id, child_thread_id,
                 origin, status, started_at, completed_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "native-sub-1",
                "t1",
                "r1",
                "n1",
                "claudeAgent",
                "claudeAgent",
                "claudeAgent",
                "pt-native-1",
                None,
                "provider_native",
                "running",
                "2026-11-20T10:00:02Z",
                None,
                "2026-11-20T10:00:02Z",
                json.dumps({"agentType": "Explore"}),
            ),
        )
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_runtime_requests
                (runtime_request_id, thread_id, node_id, kind, status,
                 created_at, resolved_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "req-1",
                "t1",
                "n2",
                "command_approval",
                "pending",
                "2026-11-20T10:00:03Z",
                None,
                json.dumps({"message": "Allow running pytest?"}),
            ),
        )

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    summary = build_summary(
        "t1",
        store.events_for_session("t1"),
        datetime(2026, 11, 20, 10, 0, 4, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary.status == AgentStatus.WAITING_INPUT
    assert summary.waiting_reason == "Allow running pytest?"
    assert len(summary.subagents) == 1
    assert summary.subagents[0].agent_id == "native-sub-1"
    assert summary.subagents[0].agent_type == "Explore"
    assert summary.subagents[0].status == SubagentStatus.RUNNING

    # Resolve the request and mark native subagent completed.
    with conn:
        conn.execute(
            """
            UPDATE orchestration_v2_projection_runtime_requests
            SET status = 'resolved', resolved_at = '2026-11-20T10:00:05Z'
            WHERE runtime_request_id = 'req-1'
            """
        )
        conn.execute(
            """
            UPDATE orchestration_v2_projection_subagents
            SET status = 'completed', completed_at = '2026-11-20T10:00:06Z',
                updated_at = '2026-11-20T10:00:06Z'
            WHERE subagent_id = 'native-sub-1'
            """
        )
    conn.close()

    reader.poll_once(store)
    summary2 = build_summary(
        "t1",
        store.events_for_session("t1"),
        datetime(2026, 11, 20, 10, 0, 7, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary2.status == AgentStatus.WORKING
    assert summary2.waiting_reason is None
    assert summary2.subagents[0].status == SubagentStatus.DONE


def test_backfills_projection_threads_without_events_and_handles_db_reset(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/backfill")
    insert_thread(
        conn,
        thread_id="old-thread",
        project_id="p1",
        title="Compacted thread",
        provider="antigravity",
        model="gemini-3.8-flash",
        created_at="2026-11-19T08:00:00Z",
    )
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_runs
                (run_id, thread_id, ordinal, provider, provider_instance_id,
                 status, requested_at, completed_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run-old",
                "old-thread",
                1,
                "antigravity",
                "antigravity",
                "completed",
                "2026-11-19T08:00:00Z",
                "2026-11-19T08:05:00Z",
                "{}",
            ),
        )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    events = store.events_for_session("old-thread")
    assert [e.hook_event_name for e in events] == ["SessionStart", "Stop"]
    summary = build_summary(
        "old-thread",
        events,
        datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary.activity == Activity.FINISHED
    assert summary.cwd == "/repos/backfill"

    # Simulate DB reset where cursor > max_seq.
    store.set_cursor("t3code", 999, {"started_threads": ["ghost"]})
    t3_path.unlink()
    conn2 = create_t3_db(t3_path)
    insert_project(conn2, "p2", "/repos/fresh")
    fresh = insert_thread(
        conn2,
        thread_id="fresh-thread",
        project_id="p2",
        title="Fresh",
        provider="claudeAgent",
        model="claude-haiku-5-5",
        created_at="2026-11-20T11:00:00Z",
    )
    append_event(
        conn2,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="fresh-thread",
        event_type="thread.created",
        occurred_at="2026-11-20T11:00:00Z",
        payload=fresh,
    )
    conn2.close()

    reader.poll_once(store)
    cursor, _ = store.get_cursor("t3code")
    assert cursor == 1
    assert len(store.events_for_session("fresh-thread")) == 1


def test_app_lifespan_polls_t3_db_and_publishes_sse(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    t0 = now.isoformat()
    t1 = (now + timedelta(seconds=1)).isoformat()

    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/live")
    thread = insert_thread(
        conn,
        thread_id="live-1",
        project_id="p1",
        title="Live thread",
        provider="antigravity",
        model="gemini-4-neon",
        created_at=t0,
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="live-1",
        event_type="thread.created",
        occurred_at=t0,
        payload=thread,
    )

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "monitor.db",
        t3_enabled=True,
        t3_db_path=t3_path,
        t3_poll_interval_seconds=0.05,
    )
    # Test startup import first with TestClient, then live polling over real SSE.
    with TestClient(create_app(settings)) as client:
        agents = client.get("/agents").json()
        assert len(agents) == 1
        assert agents[0]["session_id"] == "live-1"
        assert agents[0]["provider"] == "antigravity"

    server = build_server(settings, port=0, log_level="warning")
    thread_srv = threading.Thread(target=server.run, daemon=True)
    thread_srv.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        lines: list[str] = []

        def read_stream() -> None:
            with urllib.request.urlopen(f"{base_url}/stream") as response:
                for raw in response:
                    lines.append(raw.decode())

        threading.Thread(target=read_stream, daemon=True).start()
        while not lines:
            time.sleep(0.02)

        append_event(
            conn,
            seq_hint=2,
            aggregate_kind="thread",
            stream_id="live-1",
            event_type="turn-item.updated",
            occurred_at=t1,
            payload={"id": "u1", "threadId": "live-1", "type": "user_message", "text": "hi"},
        )
        conn.close()

        deadline = time.monotonic() + 5
        while not any(line.startswith("data:") for line in lines):
            assert time.monotonic() < deadline, "did not receive SSE message"
            time.sleep(0.02)

        data_line = next(line for line in lines if line.startswith("data:"))
        data = json.loads(data_line.removeprefix("data: "))
        assert data["event"]["hook_event_name"] == "UserPromptSubmit"
        assert data["agent"]["activity"] == "thinking"
    finally:
        server.should_exit = True
        thread_srv.join(timeout=10)


def test_transient_orchestration_events_error_does_not_reset_cursor(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/err")
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Err test",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=thread,
    )

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    assert len(reader.poll_once(store)) == 1
    saved_cursor, _ = store.get_cursor("t3code")
    assert saved_cursor == 1

    # Temporarily rename orchestration_events to simulate transient read error / migration.
    with conn:
        conn.execute("ALTER TABLE orchestration_events RENAME TO orchestration_events_tmp")
    assert reader.poll_once(store) == []
    assert store.get_cursor("t3code")[0] == 1

    with conn:
        conn.execute("ALTER TABLE orchestration_events_tmp RENAME TO orchestration_events")
    conn.close()

    assert reader.poll_once(store) == []
    assert len(store.events_for_session("t1")) == 1


def test_recreated_db_with_higher_sequence_resets_via_fingerprint(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/fp")
    t_old = insert_thread(
        conn,
        thread_id="old-1",
        project_id="p1",
        title="Old",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="old-1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=t_old,
    )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)
    assert store.get_cursor("t3code")[0] == 1

    # Recreate DB with MORE events than the old cursor (max_seq=2 >= cursor=1), different event_id.
    t3_path.unlink()
    conn2 = create_t3_db(t3_path)
    insert_project(conn2, "p2", "/repos/fp2")
    t_new = insert_thread(
        conn2,
        thread_id="new-1",
        project_id="p2",
        title="New",
        provider="claudeAgent",
        model="claude-opus-5-5",
        created_at="2026-11-20T11:00:00Z",
    )
    append_event(
        conn2,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="new-1",
        event_type="thread.created",
        occurred_at="2026-11-20T11:00:00Z",
        payload=t_new,
    )
    append_event(
        conn2,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="new-1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T11:00:01Z",
        payload={"id": "u-new", "threadId": "new-1", "type": "user_message", "text": "go"},
    )
    conn2.close()

    stored = reader.poll_once(store)
    assert [e.hook_event_name for e in stored] == ["SessionStart", "UserPromptSubmit"]
    assert store.get_cursor("t3code")[0] == 2


def test_provider_native_subagent_turn_items_attributed_before_stop(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/native")
    parent = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Parent",
        provider="claudeAgent",
        model="claude-opus-5-5",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=parent,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={"id": "u1", "threadId": "t1", "type": "user_message", "text": "search"},
    )
    append_event(
        conn,
        seq_hint=3,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:03Z",
        payload={
            "id": "sub-read",
            "threadId": "t1",
            "providerThreadId": "pt-native-1",
            "type": "dynamic_tool",
            "toolName": "Read",
            "status": "completed",
            "startedAt": "2026-11-20T10:00:02Z",
            "input": {"file_path": "/repos/native/a.py"},
        },
    )
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_subagents
                (subagent_id, thread_id, run_id, parent_node_id, provider,
                 provider_instance_id, driver, provider_thread_id, child_thread_id,
                 origin, status, started_at, completed_at, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "native-sub-1",
                "t1",
                "r1",
                "n1",
                "claudeAgent",
                "claudeAgent",
                "claudeAgent",
                "pt-native-1",
                None,
                "provider_native",
                "completed",
                "2026-11-20T10:00:02Z",
                "2026-11-20T10:00:04Z",
                "2026-11-20T10:00:04Z",
                json.dumps({"agentType": "Explore"}),
            ),
        )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    T3CodeReader(t3_path).poll_once(store)

    events = store.events_for_session("t1")
    assert [(e.hook_event_name, e.agent_id) for e in events] == [
        ("SessionStart", None),
        ("UserPromptSubmit", None),
        ("SubagentStart", "native-sub-1"),
        ("PreToolUse", "native-sub-1"),
        ("PostToolUse", "native-sub-1"),
        ("SubagentStop", "native-sub-1"),
    ]
    summary = build_summary(
        "t1",
        events,
        datetime(2026, 11, 20, 10, 0, 5, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary.status == AgentStatus.WORKING
    assert summary.subagents[0].status == SubagentStatus.DONE


def test_lifespan_survives_startup_and_poll_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    real_poll = T3CodeReader.poll_once

    def flaky_poll(self: T3CodeReader, store: EventStore) -> list:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("boom")
        return real_poll(self, store)

    monkeypatch.setattr(T3CodeReader, "poll_once", flaky_poll)

    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/recover")
    thread = insert_thread(
        conn,
        thread_id="rec-1",
        project_id="p1",
        title="Recover",
        provider="antigravity",
        model="gemini-4-neon",
        created_at=datetime.now(UTC).isoformat(),
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="rec-1",
        event_type="thread.created",
        occurred_at=datetime.now(UTC).isoformat(),
        payload=thread,
    )
    conn.close()

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "monitor.db",
        t3_enabled=True,
        t3_db_path=t3_path,
        t3_poll_interval_seconds=0.05,
    )
    with TestClient(create_app(settings)) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            agents = client.get("/agents").json()
            if agents:
                break
            time.sleep(0.05)
        assert len(agents) == 1
        assert agents[0]["session_id"] == "rec-1"


def test_missing_projection_table_does_not_flap_waiting_input(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/req")
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Wait",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={"id": "u1", "threadId": "t1", "type": "user_message", "text": "go"},
    )
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_runtime_requests
                (runtime_request_id, thread_id, node_id, kind, status,
                 created_at, resolved_at, payload_json)
            VALUES ('req-1', 't1', 'n1', 'approval', 'pending', '2026-11-20T10:00:02Z', NULL, '{}')
            """
        )

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    with conn:
        conn.execute(
            "ALTER TABLE orchestration_v2_projection_runtime_requests "
            "RENAME TO runtime_requests_tmp"
        )
    assert reader.poll_once(store) == []

    with conn:
        conn.execute(
            "ALTER TABLE runtime_requests_tmp "
            "RENAME TO orchestration_v2_projection_runtime_requests"
        )
    conn.close()

    assert reader.poll_once(store) == []
    summary = build_summary(
        "t1",
        store.events_for_session("t1"),
        datetime(2026, 11, 20, 10, 0, 5, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary.status == AgentStatus.WAITING_INPUT


def test_resolved_request_skipped_when_real_event_arrives_in_same_poll(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/req2")
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Resume",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={"id": "u1", "threadId": "t1", "type": "user_message", "text": "go"},
    )
    with conn:
        conn.execute(
            """
            INSERT INTO orchestration_v2_projection_runtime_requests
                (runtime_request_id, thread_id, node_id, kind, status,
                 created_at, resolved_at, payload_json)
            VALUES ('req-1', 't1', 'n1', 'approval', 'pending', '2026-11-20T10:00:02Z', NULL, '{}')
            """
        )

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    with conn:
        conn.execute(
            "UPDATE orchestration_v2_projection_runtime_requests "
            "SET status = 'resolved', resolved_at = '2026-11-20T10:00:03Z'"
        )
    append_event(
        conn,
        seq_hint=3,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:04Z",
        payload={
            "id": "edit-1",
            "threadId": "t1",
            "type": "dynamic_tool",
            "toolName": "Edit",
            "status": "running",
            "input": {"file_path": "/repos/req2/x.py"},
        },
    )
    conn.close()

    stored = reader.poll_once(store)
    assert [e.hook_event_name for e in stored] == ["PreToolUse"]
    summary = build_summary(
        "t1",
        store.events_for_session("t1"),
        datetime(2026, 11, 20, 10, 0, 5, tzinfo=UTC),
        timedelta(minutes=10),
    )
    assert summary.status == AgentStatus.RUNNING_TOOL
    assert summary.activity == Activity.CODING


def test_idle_poll_does_not_rewrite_source_cursors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/idle")
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Idle",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="run.updated",
        occurred_at="2026-11-20T10:00:02Z",
        payload={
            "id": "r1",
            "threadId": "t1",
            "status": "completed",
            "providerInstanceId": "antigravity",
            "modelSelection": {"instanceId": "antigravity", "model": "gemini-4-hydrogen"},
        },
    )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    reader = T3CodeReader(t3_path)
    reader.poll_once(store)

    writes = 0
    real_add_batch = store.add_batch_with_cursor

    def counting_add_batch(*args, **kwargs):
        nonlocal writes
        writes += 1
        return real_add_batch(*args, **kwargs)

    monkeypatch.setattr(store, "add_batch_with_cursor", counting_add_batch)
    assert reader.poll_once(store) == []
    assert writes == 0


def test_non_command_tool_failures_and_string_exit_code(tmp_path: Path) -> None:
    t3_path = tmp_path / "statev2.sqlite"
    conn = create_t3_db(t3_path)
    insert_project(conn, "p1", "/repos/fail")
    thread = insert_thread(
        conn,
        thread_id="t1",
        project_id="p1",
        title="Tools",
        provider="antigravity",
        model="gemini-4-neon",
        created_at="2026-11-20T10:00:00Z",
    )
    append_event(
        conn,
        seq_hint=1,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="thread.created",
        occurred_at="2026-11-20T10:00:00Z",
        payload=thread,
    )
    append_event(
        conn,
        seq_hint=2,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:01Z",
        payload={
            "id": "cmd-str-zero",
            "threadId": "t1",
            "type": "command_execution",
            "status": "completed",
            "input": "echo ok",
            "exitCode": "0",
        },
    )
    append_event(
        conn,
        seq_hint=3,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:02Z",
        payload={
            "id": "read-err",
            "threadId": "t1",
            "type": "dynamic_tool",
            "toolName": "Read",
            "status": "failed",
            "error": "ENOENT",
            "input": {"file_path": "/missing"},
        },
    )
    append_event(
        conn,
        seq_hint=4,
        aggregate_kind="thread",
        stream_id="t1",
        event_type="turn-item.updated",
        occurred_at="2026-11-20T10:00:03Z",
        payload={
            "id": "edit-int",
            "threadId": "t1",
            "type": "dynamic_tool",
            "toolName": "Edit",
            "status": "interrupted",
            "input": {"file_path": "/x.py"},
        },
    )
    conn.close()

    store = EventStore(tmp_path / "monitor.db")
    T3CodeReader(t3_path).poll_once(store)

    events = store.events_for_session("t1")
    assert [(e.hook_event_name, e.tool_name) for e in events] == [
        ("SessionStart", None),
        ("PreToolUse", "Command"),
        ("PostToolUse", "Command"),
        ("PreToolUse", "Read"),
        ("PostToolUseFailure", "Read"),
        ("PreToolUse", "Edit"),
        ("PostToolUseFailure", "Edit"),
    ]
    assert events[4].payload["error"] == "ENOENT"
    assert events[6].payload["error"] == "interrupted"


def test_bounded_add_evicts_oldest_entries_in_order() -> None:
    bucket: dict[str, None] = {}
    for idx in range(5):
        _bounded_add(bucket, f"id-{idx}", max_size=3)
    _bounded_add(bucket, "id-3", max_size=3)  # duplicate is a no-op

    assert list(bucket) == ["id-2", "id-3", "id-4"]
