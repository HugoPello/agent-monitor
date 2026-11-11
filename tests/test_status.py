"""Tests for status computation."""

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from agent_monitor.models import StoredEvent
from agent_monitor.status import (
    TOOL_ACTIVITY,
    Activity,
    AgentStatus,
    AgentSummary,
    SubagentStatus,
    SubagentSummary,
    activity_for,
    build_summary,
    compute_failures,
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


class TestActivityFor:
    @pytest.mark.parametrize(
        ("tool", "expected"),
        [
            ("Read", Activity.RESEARCHING),
            ("Grep", Activity.RESEARCHING),
            ("Glob", Activity.RESEARCHING),
            ("WebSearch", Activity.RESEARCHING),
            ("WebFetch", Activity.RESEARCHING),
            ("LSP", Activity.RESEARCHING),
            ("Edit", Activity.CODING),
            ("Write", Activity.CODING),
            ("NotebookEdit", Activity.CODING),
            ("Bash", Activity.RUNNING),
            ("PowerShell", Activity.RUNNING),
            ("Agent", Activity.DELEGATING),
            ("Task", Activity.DELEGATING),
        ],
    )
    def test_running_tool_uses_the_table(self, tool: str, expected: Activity) -> None:
        assert activity_for(AgentStatus.RUNNING_TOOL, tool) == expected

    def test_every_table_entry_is_tested(self) -> None:
        # Forces a new table entry to come with a test case above.
        assert len(TOOL_ACTIVITY) == 13

    @pytest.mark.parametrize(
        "tool", ["mcp__github__create_issue", "SomeFutureTool", "read", "", None]
    )
    def test_unknown_tools_fall_back_to_working(self, tool: str | None) -> None:
        assert activity_for(AgentStatus.RUNNING_TOOL, tool) == Activity.WORKING

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (AgentStatus.IDLE, Activity.IDLE),
            (AgentStatus.WORKING, Activity.THINKING),
            (AgentStatus.WAITING_INPUT, Activity.WAITING_INPUT),
            (AgentStatus.ENDED, Activity.ENDED),
            (AgentStatus.STALE, Activity.STALE),
            (AgentStatus.UNKNOWN, Activity.IDLE),
        ],
    )
    def test_other_statuses_ignore_the_tool(self, status: AgentStatus, expected: Activity) -> None:
        assert activity_for(status, "Bash") == expected


class TestSessionActivity:
    def activity_of(self, *events: StoredEvent, now: datetime = T0) -> Activity:
        return compute_status(list(events), now, STALE_AFTER).activity

    def test_thinking_between_tools(self) -> None:
        events = [ev("PreToolUse", tool_name="Read"), ev("PostToolUse", tool_name="Read")]

        assert self.activity_of(*events) == Activity.THINKING

    def test_delegating_while_subagent_works(self) -> None:
        events = [
            ev("PreToolUse", tool_name="Agent"),
            ev("SubagentStart", agent_id="a1"),
            ev("PreToolUse", agent_id="a1", tool_name="Bash"),
        ]

        assert self.activity_of(*events) == Activity.DELEGATING

    def test_stale_tool_call_is_stale_not_its_tool(self) -> None:
        events = [ev("PreToolUse", tool_name="Bash")]
        now = T0 + STALE_AFTER + timedelta(seconds=1)

        assert self.activity_of(*events, now=now) == Activity.STALE

    def test_exactly_at_threshold_keeps_tool_activity(self) -> None:
        events = [ev("PreToolUse", tool_name="Bash")]

        assert self.activity_of(*events, now=T0 + STALE_AFTER) == Activity.RUNNING


def subagents_of(*events: StoredEvent, now: datetime = T0) -> list[SubagentSummary]:
    return compute_subagents(list(events), now, STALE_AFTER)


class TestFinishedTurn:
    def activity_of(self, *events: StoredEvent) -> Activity:
        return compute_status(list(events), T0, STALE_AFTER).activity

    def test_stop_means_the_turn_is_finished(self) -> None:
        events = [ev("SessionStart"), ev("UserPromptSubmit"), ev("Stop")]

        assert self.activity_of(*events) == Activity.FINISHED

    def test_fresh_session_is_idle(self) -> None:
        assert self.activity_of(ev("SessionStart")) == Activity.IDLE

    def test_idle_prompt_after_stop_stays_finished(self) -> None:
        events = [
            ev("UserPromptSubmit"),
            ev("Stop"),
            ev("Notification", notification_type="idle_prompt"),
        ]

        assert self.activity_of(*events) == Activity.FINISHED

    def test_idle_prompt_on_a_fresh_session_stays_idle(self) -> None:
        events = [ev("SessionStart"), ev("Notification", notification_type="idle_prompt")]

        assert self.activity_of(*events) == Activity.IDLE

    def test_new_prompt_starts_a_new_turn(self) -> None:
        events = [ev("Stop"), ev("UserPromptSubmit")]

        assert self.activity_of(*events) == Activity.THINKING

    def test_new_session_after_stop_is_idle(self) -> None:
        # e.g. /clear starts a new session id, but a resume can reuse one
        events = [ev("Stop"), ev("SessionStart", source="resume")]

        assert self.activity_of(*events) == Activity.IDLE

    def test_subagent_events_are_not_turn_boundaries(self) -> None:
        events = [ev("Stop"), ev("SubagentStop", agent_id="a1")]

        assert self.activity_of(*events) == Activity.FINISHED

    def test_activity_for_needs_both_idle_and_finished(self) -> None:
        assert activity_for(AgentStatus.IDLE, None, turn_finished=True) == Activity.FINISHED
        assert activity_for(AgentStatus.WORKING, None, turn_finished=True) == Activity.THINKING


class TestComputeSubagents:
    def test_tracks_subagents_in_order_of_appearance(self) -> None:
        events = [
            ev("PreToolUse", tool_name="Agent"),
            ev("SubagentStart", agent_id="a1", agent_type="Explore"),
            ev("SubagentStart", at=timedelta(seconds=5), agent_id="a2", agent_type="Plan"),
            ev("SubagentStop", at=timedelta(seconds=7), agent_id="a1", agent_type="Explore"),
        ]

        subagents = subagents_of(*events, now=T0 + timedelta(seconds=8))

        assert [(s.agent_id, s.agent_type, s.status) for s in subagents] == [
            ("a1", "Explore", SubagentStatus.DONE),
            ("a2", "Plan", SubagentStatus.RUNNING),
        ]
        assert subagents[1].started_at == T0 + timedelta(seconds=5)
        assert subagents[0].last_event_at == T0 + timedelta(seconds=7)

    def test_no_subagents(self) -> None:
        assert subagents_of(ev("Stop")) == []

    def test_agent_type_from_first_event_that_has_it(self) -> None:
        events = [
            ev("PreToolUse", agent_id="a1", tool_name="Read"),
            ev("PostToolUse", agent_id="a1", tool_name="Read", agent_type="Explore"),
        ]

        [subagent] = subagents_of(*events)

        assert subagent.agent_type == "Explore"


class TestSubagentActivity:
    def activity_of(self, *events: StoredEvent, now: datetime = T0) -> Activity:
        [subagent] = subagents_of(*events, now=now)
        return subagent.activity

    def test_just_started_is_thinking(self) -> None:
        assert self.activity_of(ev("SubagentStart", agent_id="a1")) == Activity.THINKING

    @pytest.mark.parametrize(
        ("tool", "expected"),
        [("Grep", Activity.RESEARCHING), ("Write", Activity.CODING), ("Bash", Activity.RUNNING)],
    )
    def test_running_tool_uses_the_table(self, tool: str, expected: Activity) -> None:
        events = [
            ev("SubagentStart", agent_id="a1"),
            ev("PreToolUse", agent_id="a1", tool_name=tool),
        ]

        assert self.activity_of(*events) == expected

    def test_thinking_after_a_tool(self) -> None:
        events = [
            ev("PreToolUse", agent_id="a1", tool_name="Grep"),
            ev("PostToolUse", agent_id="a1", tool_name="Grep"),
        ]

        assert self.activity_of(*events) == Activity.THINKING

    def test_done_subagent_has_ended(self) -> None:
        events = [ev("SubagentStart", agent_id="a1"), ev("SubagentStop", agent_id="a1")]

        assert self.activity_of(*events, now=T0 + timedelta(days=1)) == Activity.ENDED

    def test_each_subagent_has_its_own_activity(self) -> None:
        events = [
            ev("PreToolUse", agent_id="a1", tool_name="Read"),
            ev("PreToolUse", agent_id="a2", tool_name="Edit"),
            ev("PreToolUse", tool_name="Agent"),
        ]

        assert [s.activity for s in subagents_of(*events)] == [
            Activity.RESEARCHING,
            Activity.CODING,
        ]

    def test_ignores_main_agent_events(self) -> None:
        events = [ev("PreToolUse", agent_id="a1", tool_name="Read"), ev("Stop")]

        assert self.activity_of(*events) == Activity.RESEARCHING

    def test_subagent_stop_does_not_end_the_session(self) -> None:
        events = [ev("PreToolUse", tool_name="Agent"), ev("SubagentStop", agent_id="a1")]

        assert status_of(*events) == AgentStatus.RUNNING_TOOL


class TestSubagentStale:
    def activity_of(self, *events: StoredEvent, now: datetime) -> Activity:
        [subagent] = subagents_of(*events, now=now)
        return subagent.activity

    def test_silent_running_subagent_becomes_stale(self) -> None:
        # e.g. its SubagentStop was never sent
        events = [ev("PreToolUse", agent_id="a1", tool_name="Bash")]
        now = T0 + STALE_AFTER + timedelta(seconds=1)

        assert self.activity_of(*events, now=now) == Activity.STALE

    def test_exactly_at_threshold_is_not_stale(self) -> None:
        events = [ev("PreToolUse", agent_id="a1", tool_name="Bash")]

        assert self.activity_of(*events, now=T0 + STALE_AFTER) == Activity.RUNNING

    def test_staleness_uses_the_subagents_own_events(self) -> None:
        # The session is busy, but this subagent has been silent too long.
        events = [
            ev("PreToolUse", agent_id="a1", tool_name="Read"),
            ev("PreToolUse", at=timedelta(minutes=15), tool_name="Bash"),
        ]

        assert self.activity_of(*events, now=T0 + timedelta(minutes=15)) == Activity.STALE

    def test_stale_subagent_is_still_running(self) -> None:
        events = [ev("SubagentStart", agent_id="a1")]

        [subagent] = subagents_of(*events, now=T0 + timedelta(hours=1))

        assert subagent.status == SubagentStatus.RUNNING
        assert subagent.activity == Activity.STALE


class TestFailures:
    def test_counts_failures_in_the_current_turn(self) -> None:
        events = [
            ev("UserPromptSubmit"),
            ev("PostToolUseFailure", at=timedelta(seconds=1), tool_name="Bash"),
            ev("PostToolUseFailure", at=timedelta(seconds=2), agent_id="a1", tool_name="Edit"),
            ev("PostToolUse", at=timedelta(seconds=3), tool_name="Bash"),
        ]

        failures = compute_failures(events)

        assert failures.count == 2
        assert failures.last_at == T0 + timedelta(seconds=2)

    def test_a_new_prompt_starts_from_zero(self) -> None:
        events = [
            ev("UserPromptSubmit"),
            ev("PostToolUseFailure", tool_name="Bash"),
            ev("Stop"),
            ev("UserPromptSubmit", at=timedelta(minutes=1)),
        ]

        assert compute_failures(events).count == 0
        assert compute_failures(events).last_at is None

    def test_failures_before_any_prompt_count(self) -> None:
        assert compute_failures([ev("PostToolUseFailure", tool_name="Read")]).count == 1

    def test_subagent_prompts_do_not_reset_the_turn(self) -> None:
        events = [
            ev("PostToolUseFailure", tool_name="Bash"),
            ev("UserPromptSubmit", agent_id="a1"),
        ]

        assert compute_failures(events).count == 1


class TestWaitingReason:
    def summary_of(self, *events: StoredEvent) -> AgentSummary:
        return build_summary("s1", list(events), T0, STALE_AFTER)

    def test_permission_prompt_message_is_the_reason(self) -> None:
        summary = self.summary_of(
            ev("PreToolUse", tool_name="Bash"),
            ev(
                "Notification",
                notification_type="permission_prompt",
                message="Claude needs your permission to use Bash",
            ),
        )

        assert summary.waiting_reason == "Claude needs your permission to use Bash"

    def test_no_reason_once_no_longer_waiting(self) -> None:
        summary = self.summary_of(
            ev("Notification", notification_type="permission_prompt", message="perm"),
            ev("PostToolUse", tool_name="Bash"),
        )

        assert summary.waiting_reason is None

    def test_missing_message_gives_no_reason(self) -> None:
        summary = self.summary_of(ev("Notification", notification_type="permission_prompt"))

        assert summary.activity == Activity.WAITING_INPUT
        assert summary.waiting_reason is None

    def test_non_text_message_is_ignored(self) -> None:
        summary = self.summary_of(
            ev("Notification", notification_type="permission_prompt", message=42)
        )

        assert summary.waiting_reason is None


def test_build_summary() -> None:
    events = [
        ev("SessionStart", cwd="/old"),
        ev("UserPromptSubmit", at=timedelta(seconds=1), cwd="/project"),
        ev("PreToolUse", at=timedelta(seconds=2), tool_name="Bash", cwd="/project"),
    ]

    summary = build_summary("s1", events, T0 + timedelta(seconds=3), STALE_AFTER)

    assert summary.session_id == "s1"
    assert summary.status == AgentStatus.RUNNING_TOOL
    assert summary.activity == Activity.RUNNING
    assert summary.current_tool == "Bash"
    assert summary.cwd == "/project"
    assert summary.started_at == T0
    assert summary.last_event_at == T0 + timedelta(seconds=2)
    assert summary.event_count == 3
    assert summary.subagents == []
    assert summary.waiting_reason is None
    assert summary.failed_steps == 0
    assert summary.last_failure_at is None
