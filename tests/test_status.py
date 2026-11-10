"""Tests for status computation."""

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from agent_monitor.models import StoredEvent
from agent_monitor.status import (
    AgentStatus,
    SubagentStatus,
    build_summary,
    compute_status,
    compute_subagents,
)

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=10)
_ids = count(1)


def ev(
    name: str,
    *,
    at: timedelta = timedelta(0),
    agent_id: str | None = None,
    tool_name: str | None = None,
    **payload: object,
) -> StoredEvent:
    """Build a StoredEvent at T0 + ``at``."""
    full_payload = {"session_id": "s1", "hook_event_name": name, **payload}
    if agent_id:
        full_payload["agent_id"] = agent_id
    return StoredEvent(
        id=next(_ids),
        received_at=T0 + at,
        session_id="s1",
        agent_id=agent_id,
        hook_event_name=name,
        tool_name=tool_name,
        payload=full_payload,
    )


def status_of(*events: StoredEvent, now: datetime = T0) -> AgentStatus:
    return compute_status(list(events), now, STALE_AFTER).status


class TestComputeStatus:
    @pytest.mark.parametrize(
        ("event", "expected"),
        [
            (ev("SessionStart", source="startup"), AgentStatus.IDLE),
            (ev("UserPromptSubmit", prompt="hi"), AgentStatus.WORKING),
            (ev("PreToolUse", tool_name="Bash"), AgentStatus.RUNNING_TOOL),
            (ev("PostToolUse", tool_name="Bash"), AgentStatus.WORKING),
            (ev("PostToolUseFailure", tool_name="Bash"), AgentStatus.WORKING),
            (ev("Notification", notification_type="permission_prompt"), AgentStatus.WAITING_INPUT),
            (ev("Notification", notification_type="idle_prompt"), AgentStatus.IDLE),
            (ev("Stop"), AgentStatus.IDLE),
            (ev("SessionEnd", reason="logout"), AgentStatus.ENDED),
        ],
    )
    def test_last_event_decides_status(self, event: StoredEvent, expected: AgentStatus) -> None:
        assert status_of(ev("SessionStart"), event) == expected

    def test_no_events_is_unknown(self) -> None:
        assert status_of() == AgentStatus.UNKNOWN

    def test_only_uninterpreted_events_is_unknown(self) -> None:
        assert status_of(ev("InstructionsLoaded")) == AgentStatus.UNKNOWN

    def test_uninterpreted_events_are_skipped(self) -> None:
        assert status_of(ev("UserPromptSubmit"), ev("FileChanged")) == AgentStatus.WORKING

    def test_other_notification_types_are_skipped(self) -> None:
        events = [ev("Stop"), ev("Notification", notification_type="auth_success")]

        assert status_of(*events) == AgentStatus.IDLE

    def test_reports_current_tool_while_running(self) -> None:
        result = compute_status([ev("PreToolUse", tool_name="Edit")], T0, STALE_AFTER)

        assert result.current_tool == "Edit"

    def test_no_current_tool_once_finished(self) -> None:
        events = [ev("PreToolUse", tool_name="Edit"), ev("PostToolUse", tool_name="Edit")]

        assert compute_status(events, T0, STALE_AFTER).current_tool is None

    def test_subagent_events_do_not_change_session_status(self) -> None:
        events = [
            ev("PreToolUse", tool_name="Agent"),
            ev("SubagentStart", agent_id="a1"),
            ev("PreToolUse", agent_id="a1", tool_name="Grep"),
            ev("PostToolUse", agent_id="a1", tool_name="Grep"),
        ]
        result = compute_status(events, T0, STALE_AFTER)

        assert result.status == AgentStatus.RUNNING_TOOL
        assert result.current_tool == "Agent"


class TestStale:
    def test_silent_working_agent_becomes_stale(self) -> None:
        events = [ev("UserPromptSubmit")]

        assert status_of(*events, now=T0 + STALE_AFTER + timedelta(seconds=1)) == AgentStatus.STALE

    def test_exactly_at_threshold_is_not_stale(self) -> None:
        assert status_of(ev("UserPromptSubmit"), now=T0 + STALE_AFTER) == AgentStatus.WORKING

    @pytest.mark.parametrize(
        "event",
        [ev("Stop"), ev("SessionEnd"), ev("Notification", notification_type="permission_prompt")],
    )
    def test_quiet_states_never_become_stale(self, event: StoredEvent) -> None:
        assert status_of(event, now=T0 + timedelta(days=1)) != AgentStatus.STALE

    def test_recent_subagent_activity_keeps_session_alive(self) -> None:
        events = [
            ev("PreToolUse", tool_name="Agent"),
            ev("PreToolUse", at=timedelta(minutes=15), agent_id="a1", tool_name="Read"),
        ]

        now = T0 + timedelta(minutes=16)
        assert status_of(*events, now=now) == AgentStatus.RUNNING_TOOL


class TestComputeSubagents:
    def test_tracks_subagents_in_order_of_appearance(self) -> None:
        events = [
            ev("PreToolUse", tool_name="Agent"),
            ev("SubagentStart", agent_id="a1", agent_type="Explore"),
            ev("SubagentStart", at=timedelta(seconds=5), agent_id="a2", agent_type="Plan"),
            ev("SubagentStop", agent_id="a1", agent_type="Explore"),
        ]

        subagents = compute_subagents(events)

        assert [(s.agent_id, s.agent_type, s.status) for s in subagents] == [
            ("a1", "Explore", SubagentStatus.DONE),
            ("a2", "Plan", SubagentStatus.RUNNING),
        ]
        assert subagents[1].started_at == T0 + timedelta(seconds=5)

    def test_no_subagents(self) -> None:
        assert compute_subagents([ev("Stop")]) == []


def test_build_summary() -> None:
    events = [
        ev("SessionStart", cwd="/old"),
        ev("UserPromptSubmit", at=timedelta(seconds=1), cwd="/project"),
        ev("PreToolUse", at=timedelta(seconds=2), tool_name="Bash", cwd="/project"),
    ]

    summary = build_summary("s1", events, T0 + timedelta(seconds=3), STALE_AFTER)

    assert summary.session_id == "s1"
    assert summary.status == AgentStatus.RUNNING_TOOL
    assert summary.current_tool == "Bash"
    assert summary.cwd == "/project"
    assert summary.started_at == T0
    assert summary.last_event_at == T0 + timedelta(seconds=2)
    assert summary.event_count == 3
    assert summary.subagents == []
