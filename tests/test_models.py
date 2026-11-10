"""Tests for the HookEvent model and payload truncation."""

import pytest
from pydantic import ValidationError

from agent_monitor.models import (
    MAX_STRING_LENGTH,
    TRUNCATION_MARKER,
    HookEvent,
    truncate_strings,
)

# Shape taken from the SubagentStop example in the Claude Code hooks reference.
SUBAGENT_STOP = {
    "session_id": "abc123",
    "transcript_path": "~/.claude/projects/.../abc123.jsonl",
    "cwd": "/Users/me/project",
    "permission_mode": "default",
    "hook_event_name": "SubagentStop",
    "stop_hook_active": False,
    "agent_id": "def456",
    "agent_type": "Explore",
    "agent_transcript_path": "~/.claude/projects/.../abc123/subagents/agent-def456.jsonl",
    "last_assistant_message": "Analysis complete.",
    "background_tasks": [],
    "session_crons": [],
}


class TestHookEvent:
    def test_accepts_minimal_event(self) -> None:
        event = HookEvent(session_id="s1", hook_event_name="Stop")

        assert event.session_id == "s1"
        assert event.agent_id is None
        assert event.tool_name is None

    @pytest.mark.parametrize("missing", ["session_id", "hook_event_name"])
    def test_rejects_missing_required_field(self, missing: str) -> None:
        data = {"session_id": "s1", "hook_event_name": "Stop"}
        del data[missing]

        with pytest.raises(ValidationError):
            HookEvent.model_validate(data)

    @pytest.mark.parametrize("field", ["session_id", "hook_event_name"])
    def test_rejects_empty_required_field(self, field: str) -> None:
        data = {"session_id": "s1", "hook_event_name": "Stop", field: ""}

        with pytest.raises(ValidationError):
            HookEvent.model_validate(data)

    def test_accepts_unknown_event_names(self) -> None:
        # Claude Code adds events over time; the server must not reject them.
        event = HookEvent(session_id="s1", hook_event_name="SomeFutureEvent")

        assert event.hook_event_name == "SomeFutureEvent"

    def test_reads_subagent_fields(self) -> None:
        event = HookEvent.model_validate(SUBAGENT_STOP)

        assert event.agent_id == "def456"
        assert event.agent_type == "Explore"
        assert event.cwd == "/Users/me/project"

    def test_payload_keeps_unknown_fields(self) -> None:
        payload = HookEvent.model_validate(SUBAGENT_STOP).payload()

        assert payload["last_assistant_message"] == "Analysis complete."
        assert payload["stop_hook_active"] is False
        assert payload["background_tasks"] == []

    def test_truncates_long_nested_strings(self) -> None:
        event = HookEvent.model_validate(
            {
                "session_id": "s1",
                "hook_event_name": "PostToolUse",
                "tool_name": "Read",
                "tool_response": {"content": "x" * (MAX_STRING_LENGTH + 500)},
            }
        )

        content = event.payload()["tool_response"]["content"]
        assert content.endswith(TRUNCATION_MARKER)
        assert len(content) == MAX_STRING_LENGTH + len(TRUNCATION_MARKER)


class TestTruncateStrings:
    def test_leaves_short_values_untouched(self) -> None:
        value = {"a": "short", "b": [1, "two", None], "c": True}

        assert truncate_strings(value, max_length=10) == value

    def test_cuts_strings_inside_lists(self) -> None:
        assert truncate_strings(["abcdef"], max_length=3) == ["abc" + TRUNCATION_MARKER]

    def test_string_at_exact_limit_is_kept(self) -> None:
        assert truncate_strings("abc", max_length=3) == "abc"
