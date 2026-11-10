"""Derive an agent's current state from its event history.

Everything here is a pure function of the events (plus "now"), with no I/O,
so the rules are easy to read and to test in isolation.

Key rule: the *session* status only looks at main-agent events
(``agent_id is None``). While a subagent runs, the main agent is inside an
``Agent`` tool call, so it is still "running a tool"; letting subagent events
drive the session status would make it flicker.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel

from agent_monitor.models import StoredEvent


class AgentStatus(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    RUNNING_TOOL = "running_tool"
    WAITING_INPUT = "waiting_input"
    ENDED = "ended"
    STALE = "stale"
    UNKNOWN = "unknown"  # no event we know how to interpret yet


class SubagentStatus(StrEnum):
    RUNNING = "running"
    DONE = "done"


# Event name -> status it implies. Events not listed here are skipped.
_EVENT_STATUS: dict[str, AgentStatus] = {
    "SessionStart": AgentStatus.IDLE,
    "UserPromptSubmit": AgentStatus.WORKING,
    "PreToolUse": AgentStatus.RUNNING_TOOL,
    "PostToolUse": AgentStatus.WORKING,
    "PostToolUseFailure": AgentStatus.WORKING,
    "Stop": AgentStatus.IDLE,
    "SessionEnd": AgentStatus.ENDED,
}

# Notification events depend on their `notification_type`.
_NOTIFICATION_STATUS: dict[str, AgentStatus] = {
    "permission_prompt": AgentStatus.WAITING_INPUT,
    "idle_prompt": AgentStatus.IDLE,
}

# Only these can become stale: an idle or waiting agent is silent on purpose.
_ACTIVE = {AgentStatus.WORKING, AgentStatus.RUNNING_TOOL}


@dataclass(frozen=True)
class StatusResult:
    status: AgentStatus
    current_tool: str | None = None


def status_from_event(event: StoredEvent) -> AgentStatus | None:
    """The status a single event implies, or ``None`` if it says nothing."""
    if event.hook_event_name == "Notification":
        return _NOTIFICATION_STATUS.get(event.payload.get("notification_type", ""))
    return _EVENT_STATUS.get(event.hook_event_name)


def compute_status(
    events: Sequence[StoredEvent], now: datetime, stale_after: timedelta
) -> StatusResult:
    """Current status of a session, given its events oldest first."""
    if not events:
        return StatusResult(AgentStatus.UNKNOWN)

    for event in reversed(events):
        if event.agent_id is not None:
            continue  # subagent event, see module docstring
        status = status_from_event(event)
        if status is None:
            continue  # an event we don't interpret: look further back

        current_tool = event.tool_name if status is AgentStatus.RUNNING_TOOL else None
        # Any activity (subagents included) proves the session is alive.
        silent_for = now - events[-1].received_at
        if status in _ACTIVE and silent_for > stale_after:
            return StatusResult(AgentStatus.STALE, current_tool)
        return StatusResult(status, current_tool)

    return StatusResult(AgentStatus.UNKNOWN)


class SubagentSummary(BaseModel):
    agent_id: str
    agent_type: str | None
    status: SubagentStatus
    started_at: datetime


def compute_subagents(events: Sequence[StoredEvent]) -> list[SubagentSummary]:
    """Subagents seen in a session, in the order they first appeared.

    A subagent is ``running`` until its ``SubagentStop`` arrives.
    """
    subagents: dict[str, SubagentSummary] = {}
    for event in events:
        if event.agent_id is None:
            continue
        summary = subagents.get(event.agent_id)
        if summary is None:
            summary = SubagentSummary(
                agent_id=event.agent_id,
                agent_type=event.payload.get("agent_type"),
                status=SubagentStatus.RUNNING,
                started_at=event.received_at,
            )
            subagents[event.agent_id] = summary
        if event.hook_event_name == "SubagentStop":
            summary.status = SubagentStatus.DONE
    return list(subagents.values())


class AgentSummary(BaseModel):
    """One card on the dashboard: a session and its current state."""

    session_id: str
    status: AgentStatus
    current_tool: str | None
    cwd: str | None
    started_at: datetime
    last_event_at: datetime
    event_count: int
    subagents: list[SubagentSummary]


def build_summary(
    session_id: str,
    events: Sequence[StoredEvent],
    now: datetime,
    stale_after: timedelta,
) -> AgentSummary:
    """Summarise a session. ``events`` must be non-empty, oldest first."""
    result = compute_status(events, now, stale_after)
    # Most recent cwd wins: it can change during a session (CwdChanged).
    cwd = next((e.payload["cwd"] for e in reversed(events) if e.payload.get("cwd")), None)
    return AgentSummary(
        session_id=session_id,
        status=result.status,
        current_tool=result.current_tool,
        cwd=cwd,
        started_at=events[0].received_at,
        last_event_at=events[-1].received_at,
        event_count=len(events),
        subagents=compute_subagents(events),
    )
