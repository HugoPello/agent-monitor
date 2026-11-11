"""Derive an agent's current state from its event history.

Everything here is a pure function of the events (plus "now"), with no I/O,
so the rules are easy to read and to test in isolation.

Key rule: the *session* status only looks at main-agent events
(``agent_id is None``). While a subagent runs, the main agent is inside an
``Agent`` tool call, so it is still "running a tool"; letting subagent events
drive the session status would make it flicker.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
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


class Activity(StrEnum):
    """What an agent is doing, coarse enough to read at a glance.

    A heuristic layered on top of ``AgentStatus``: hooks tell us *which* tool
    runs, never *why*, so "researching" really means "running a tool that
    usually reads". Keeping it derived from the status reuses the status
    rules (stale, waiting, subagents not driving the session) unchanged.
    """

    IDLE = "idle"  # nothing asked yet (fresh session)
    FINISHED = "finished"  # finished its turn: the next move is the user's
    THINKING = "thinking"  # working, no tool running
    RESEARCHING = "researching"
    CODING = "coding"
    RUNNING = "running"  # shell commands
    DELEGATING = "delegating"  # inside an Agent tool call
    WORKING = "working"  # a tool we don't classify (MCP tools included)
    WAITING_INPUT = "waiting_input"
    STALE = "stale"
    ENDED = "ended"


# Tool name -> activity while that tool runs. Names are the exact strings
# from the Claude Code tools reference (code.claude.com/docs/en/tools-reference).
# Anything not listed, including MCP tools (``mcp__<server>__<tool>``), falls
# back to Activity.WORKING. Extend this table to classify more tools.
TOOL_ACTIVITY: dict[str, Activity] = {
    "Read": Activity.RESEARCHING,
    "Grep": Activity.RESEARCHING,
    "Glob": Activity.RESEARCHING,
    "WebSearch": Activity.RESEARCHING,
    "WebFetch": Activity.RESEARCHING,
    "LSP": Activity.RESEARCHING,
    "Edit": Activity.CODING,
    "Write": Activity.CODING,
    "NotebookEdit": Activity.CODING,
    "Bash": Activity.RUNNING,
    "PowerShell": Activity.RUNNING,
    "Agent": Activity.DELEGATING,
    "Task": Activity.DELEGATING,  # the Agent tool's name before Claude Code 2.1.63
}

# Activity for every status that is not "running a tool".
_STATUS_ACTIVITY: dict[AgentStatus, Activity] = {
    AgentStatus.IDLE: Activity.IDLE,
    AgentStatus.WORKING: Activity.THINKING,
    AgentStatus.WAITING_INPUT: Activity.WAITING_INPUT,
    AgentStatus.ENDED: Activity.ENDED,
    AgentStatus.STALE: Activity.STALE,
    AgentStatus.UNKNOWN: Activity.IDLE,
}


def activity_for(
    status: AgentStatus, tool_name: str | None, *, turn_finished: bool = False
) -> Activity:
    """The activity implied by a status and the tool running, if any.

    ``turn_finished`` tells an idle agent that has answered (FINISHED) from
    one that was never asked anything (IDLE).
    """
    if status is AgentStatus.RUNNING_TOOL:
        return TOOL_ACTIVITY.get(tool_name or "", Activity.WORKING)
    if status is AgentStatus.IDLE and turn_finished:
        return Activity.FINISHED
    return _STATUS_ACTIVITY[status]


# Events that open or close a turn of the conversation.
_TURN_BOUNDARIES = {"SessionStart", "UserPromptSubmit", "Stop"}


def turn_finished(main_events: Sequence[StoredEvent]) -> bool:
    """Whether the main agent's latest turn ended with ``Stop``.

    Looks at the last turn boundary rather than the last event, so an
    ``idle_prompt`` notification arriving after the ``Stop`` keeps the turn
    finished, while a fresh ``SessionStart`` does not count as one.
    """
    for event in reversed(main_events):
        if event.hook_event_name in _TURN_BOUNDARIES:
            return event.hook_event_name == "Stop"
    return False


@dataclass(frozen=True)
class StatusResult:
    status: AgentStatus
    current_tool: str | None = None
    turn_finished: bool = False

    @property
    def activity(self) -> Activity:
        return activity_for(self.status, self.current_tool, turn_finished=self.turn_finished)


def status_from_event(event: StoredEvent) -> AgentStatus | None:
    """The status a single event implies, or ``None`` if it says nothing."""
    if event.hook_event_name == "Notification":
        return _NOTIFICATION_STATUS.get(event.payload.get("notification_type", ""))
    return _EVENT_STATUS.get(event.hook_event_name)


def subagent_status_from_event(event: StoredEvent) -> AgentStatus | None:
    """Like ``status_from_event``, plus the events that bracket a subagent.

    Kept separate on purpose: ``SubagentStop`` must never end the *session*.
    """
    if event.hook_event_name == "SubagentStart":
        return AgentStatus.WORKING
    if event.hook_event_name == "SubagentStop":
        return AgentStatus.ENDED
    return status_from_event(event)


def _latest_status(
    events: Sequence[StoredEvent],
    interpret: Callable[[StoredEvent], AgentStatus | None],
    last_seen: datetime,
    now: datetime,
    stale_after: timedelta,
) -> StatusResult:
    """Status from the newest event ``interpret`` understands.

    ``last_seen`` is when the agent last showed any sign of life; an active
    agent silent for longer than ``stale_after`` is reported as stale.
    """
    for event in reversed(events):
        status = interpret(event)
        if status is None:
            continue  # an event we don't interpret: look further back

        current_tool = event.tool_name if status is AgentStatus.RUNNING_TOOL else None
        if status in _ACTIVE and now - last_seen > stale_after:
            return StatusResult(AgentStatus.STALE, current_tool)
        return StatusResult(status, current_tool)

    return StatusResult(AgentStatus.UNKNOWN)


def compute_status(
    events: Sequence[StoredEvent], now: datetime, stale_after: timedelta
) -> StatusResult:
    """Current status of a session, given its events oldest first."""
    if not events:
        return StatusResult(AgentStatus.UNKNOWN)

    main_events = [e for e in events if e.agent_id is None]  # see module docstring
    # Any activity (subagents included) proves the session is alive.
    last_seen = events[-1].received_at
    result = _latest_status(main_events, status_from_event, last_seen, now, stale_after)
    return replace(result, turn_finished=turn_finished(main_events))


class SubagentSummary(BaseModel):
    agent_id: str
    agent_type: str | None
    status: SubagentStatus
    activity: Activity
    started_at: datetime
    last_event_at: datetime


def _summarize_subagent(
    agent_id: str, own_events: Sequence[StoredEvent], now: datetime, stale_after: timedelta
) -> SubagentSummary:
    """Summary of one subagent from its own events (non-empty, oldest first)."""
    last_seen = own_events[-1].received_at
    done = any(e.hook_event_name == "SubagentStop" for e in own_events)
    if done:
        activity = Activity.ENDED
    else:
        result = _latest_status(own_events, subagent_status_from_event, last_seen, now, stale_after)
        activity = result.activity
    agent_type = next(
        (e.payload["agent_type"] for e in own_events if e.payload.get("agent_type")), None
    )
    return SubagentSummary(
        agent_id=agent_id,
        agent_type=agent_type,
        status=SubagentStatus.DONE if done else SubagentStatus.RUNNING,
        activity=activity,
        started_at=own_events[0].received_at,
        last_event_at=last_seen,
    )


def compute_subagents(
    events: Sequence[StoredEvent], now: datetime, stale_after: timedelta
) -> list[SubagentSummary]:
    """Subagents seen in a session, in the order they first appeared.

    A subagent is ``running`` until its ``SubagentStop`` arrives. Its activity
    comes from its own latest event; a running subagent silent for longer
    than ``stale_after`` is stale (e.g. its ``SubagentStop`` was never sent).
    """
    by_agent: dict[str, list[StoredEvent]] = {}
    for event in events:
        if event.agent_id is not None:
            by_agent.setdefault(event.agent_id, []).append(event)
    return [
        _summarize_subagent(agent_id, own, now, stale_after) for agent_id, own in by_agent.items()
    ]


class AgentSummary(BaseModel):
    """One card on the dashboard: a session and its current state."""

    session_id: str
    status: AgentStatus
    activity: Activity
    current_tool: str | None  # kept for debugging; the dashboard shows activity
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
        activity=result.activity,
        current_tool=result.current_tool,
        cwd=cwd,
        started_at=events[0].received_at,
        last_event_at=events[-1].received_at,
        event_count=len(events),
        subagents=compute_subagents(events, now, stale_after),
    )
