"""Data models for Claude Code hook events.

Claude Code sends a JSON object to every hook. A few fields are common to
all events (``session_id``, ``hook_event_name``, ``cwd``...) and the rest
depend on the event. New events and fields appear regularly, so the model
is deliberately permissive: it requires only what we cannot work without
and keeps everything else as-is.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_STRING_LENGTH = 10_000
TRUNCATION_MARKER = "…[truncated]"


def truncate_strings(value: Any, max_length: int = MAX_STRING_LENGTH) -> Any:
    """Return ``value`` with every string longer than ``max_length`` cut short.

    Walks nested dicts and lists. Fields like ``tool_response`` can contain
    whole files; we only need enough to show them in a timeline.
    """
    if isinstance(value, str):
        if len(value) > max_length:
            return value[:max_length] + TRUNCATION_MARKER
        return value
    if isinstance(value, dict):
        return {key: truncate_strings(item, max_length) for key, item in value.items()}
    if isinstance(value, list):
        return [truncate_strings(item, max_length) for item in value]
    return value


class HookEvent(BaseModel):
    """An event as received from a Claude Code hook.

    Only ``session_id`` and ``hook_event_name`` are required. The other typed
    fields are the ones the monitor reads; any extra field is kept
    (``extra="allow"``) and stored in the raw payload.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str = Field(min_length=1)
    hook_event_name: str = Field(min_length=1)

    # Present when the hook fires inside a subagent (or with `claude --agent`).
    agent_id: str | None = None
    agent_type: str | None = None

    cwd: str | None = None
    tool_name: str | None = None
    tool_use_id: str | None = None
    notification_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _truncate_long_strings(cls, data: Any) -> Any:
        # Runs before field validation, so the model never holds huge strings.
        return truncate_strings(data)

    def payload(self) -> dict[str, Any]:
        """The full event as a JSON-serialisable dict, extra fields included."""
        return self.model_dump(mode="json")


class StoredEvent(BaseModel):
    """An event as persisted, with the fields the server adds."""

    id: int
    received_at: datetime
    session_id: str
    agent_id: str | None
    hook_event_name: str
    tool_name: str | None
    payload: dict[str, Any]
