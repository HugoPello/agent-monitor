"""Read-only SQLite source that imports and streams T3 Code sessions.

T3 Code stores all orchestrated agent threads (Claude, Gemini/Antigravity,
OpenCode, Codex, etc.) in ``~/.t3/userdata/statev2.sqlite`` using SQLite WAL
mode. This module reads that database strictly in read-only URI mode
(``?mode=ro``), translates T3 Code's ``orchestration_events`` stream and
v2 projection tables into standard :class:`HookEvent` records, and checkpoints
its progress atomically in :class:`EventStore`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from agent_monitor.models import HookEvent, StoredEvent
from agent_monitor.storage import EventStore

logger = logging.getLogger(__name__)

SOURCE_NAME = "t3code"
BATCH_LIMIT = 1000
MAX_TRACKED_IDS = 5000
ASSISTANT_PREVIEW_MAX = 500

_ACTIVE_ITEM_STATUSES = frozenset({"pending", "running", "in_progress", "starting"})
_TERMINAL_ITEM_STATUSES = frozenset({"completed", "failed", "error", "interrupted", "cancelled"})
_FAILED_ITEM_STATUSES = frozenset({"failed", "error", "interrupted", "cancelled"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "interrupted", "cancelled"})
_IGNORED_ITEM_TYPES = frozenset({"reasoning", "checkpoint", "plan", "todo_list"})
_TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "web_search", "dynamic_tool", "mcp_tool_call"}
)
_SESSION_PROGRESS_HOOKS = frozenset(
    {"UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"}
)

_PROVIDER_ALIASES: dict[str, str] = {
    "claudeagent": "claude",
    "claude-agent": "claude",
    "claude": "claude",
}

_TOOL_NAME_ALIASES: dict[str, str] = {
    "read_file": "Read",
    "view_file": "Read",
    "read": "Read",
    "edit_file": "Edit",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "edit": "Edit",
    "write_file": "Write",
    "write_to_file": "Write",
    "write": "Write",
    "run_command": "Command",
    "bash": "Bash",
    "powershell": "PowerShell",
    "search_web": "WebSearch",
    "web_search": "WebSearch",
    "web_fetch": "WebFetch",
    "grep_search": "Grep",
    "grep": "Grep",
    "find_by_name": "Glob",
    "list_dir": "Glob",
    "glob": "Glob",
    "t3-code.delegate_task": "delegate_task",
    "delegate_task": "delegate_task",
    "invoke_subagent": "invoke_subagent",
    "spawn_agent": "invoke_subagent",
}


def _parse_ts(value: str | None) -> datetime:
    """Parse an ISO-8601 timestamp string into a UTC-aware ``datetime``."""
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _clean_str(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _safe_int(value: Any) -> int | None:
    """Parse ``value`` as an int, rejecting booleans and invalid strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                return int(stripped)
            except ValueError:
                return None
    return None


def _truncate(text: str | None, limit: int = ASSISTANT_PREVIEW_MAX) -> str | None:
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _concise_agent_type(title: str | None, provider: str | None) -> str:
    label = _clean_str(title)
    if label:
        return label if len(label) <= 40 else label[:39] + "…"
    return provider or "subagent"


def normalize_provider(*candidates: Any) -> str | None:
    """Normalise T3 Code provider/driver identifiers into short display names."""
    for raw in candidates:
        cleaned = _clean_str(raw)
        if not cleaned:
            continue
        return _PROVIDER_ALIASES.get(cleaned.lower(), cleaned.lower())
    return None


def normalize_tool_name(item_type: str | None, raw_tool_name: str | None) -> str | None:
    """Map T3 Code turn-item types and tool aliases onto canonical names."""
    cleaned = _clean_str(raw_tool_name)
    if cleaned:
        return _TOOL_NAME_ALIASES.get(cleaned.lower(), cleaned)
    if item_type == "command_execution":
        return "Command"
    if item_type == "file_change":
        return "FileChange"
    if item_type == "web_search":
        return "WebSearch"
    return None


def normalize_tool_input(item_type: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """Extract tool input into a shape understood by the dashboard timeline."""
    raw_input = payload.get("input")
    title = _clean_str(payload.get("title"))

    if item_type == "command_execution":
        if isinstance(raw_input, str) and raw_input:
            return {"command": raw_input}
        if isinstance(raw_input, dict):
            return dict(raw_input)
        return {"description": title} if title else {}

    if item_type == "file_change":
        if isinstance(raw_input, dict):
            result = dict(raw_input)
            if "file_path" not in result:
                for key in ("path", "Path", "filePath"):
                    if isinstance(result.get(key), str) and result[key]:
                        result["file_path"] = result[key]
                        break
            return result
        files = payload.get("files")
        if isinstance(files, list) and files:
            first = files[0]
            if isinstance(first, str) and first:
                return {"file_path": first}
            if isinstance(first, dict):
                path = _clean_str(first.get("path") or first.get("filePath"))
                if path:
                    return {"file_path": path}
        return {"description": title} if title else {}

    if isinstance(raw_input, dict):
        result = dict(raw_input)
        if "file_path" not in result:
            for key in ("Path", "path", "filePath"):
                val = result.get(key)
                if isinstance(val, str) and val:
                    result["file_path"] = val
                    break
        if "command" not in result and isinstance(result.get("CommandLine"), str):
            result["command"] = result["CommandLine"]
        if "prompt" not in result:
            for key in ("Prompt", "task"):
                val = result.get(key)
                if isinstance(val, str) and val:
                    result["prompt"] = val
                    break
        if not result and title:
            result["description"] = title
        return result

    if isinstance(raw_input, str) and raw_input:
        return {"description": raw_input}
    return {"description": title} if title else {}


def _is_workspace_prep_item(payload: dict[str, Any]) -> bool:
    item_id = str(payload.get("id") or "")
    decoded = unquote(item_id)
    return "workspace-preparation:" in item_id or "workspace-preparation:" in decoded


def _bounded_add(bucket: dict[str, None], key: str, max_size: int = MAX_TRACKED_IDS) -> None:
    """Insert ``key`` into an insertion-ordered set-dict, evicting oldest past ``max_size``."""
    if key in bucket:
        return
    bucket[key] = None
    if len(bucket) > max_size:
        del bucket[next(iter(bucket))]


def _load_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _clamp_ts(
    ts: datetime, floor_by_session: dict[str, datetime] | None, session_id: str
) -> datetime:
    if not floor_by_session or session_id not in floor_by_session:
        return ts
    floor = floor_by_session[session_id]
    return ts if ts >= floor else floor


class T3CodeTranslator:
    """Stateful translator from T3 Code records to ``(HookEvent, timestamp)``."""

    def __init__(self) -> None:
        self.db_fingerprint: str | None = None
        self.projects: dict[str, str] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.started_threads: set[str] = set()
        self.active_sessions: set[str] = set()
        self.last_assistant_message: dict[str, str] = {}

        self._started_items: dict[str, None] = {}
        self._finished_items: dict[str, None] = {}
        self._seen_prompts: dict[str, None] = {}
        self._finished_runs: dict[str, None] = {}

        self.subagent_statuses: dict[str, str] = {}
        self.provider_thread_subagents: dict[str, dict[str, str | None]] = {}
        self.pending_requests: dict[str, str] = {}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> T3CodeTranslator:
        translator = cls()
        if not state:
            return translator
        fp = state.get("db_fingerprint")
        translator.db_fingerprint = str(fp) if isinstance(fp, str) else None
        projects = state.get("projects")
        if isinstance(projects, dict):
            translator.projects = {str(k): str(v) for k, v in projects.items()}
        threads = state.get("threads")
        if isinstance(threads, dict):
            translator.threads = {
                str(k): dict(v) for k, v in threads.items() if isinstance(v, dict)
            }
        translator.started_threads = set(state.get("started_threads") or [])
        translator.active_sessions = set(state.get("active_sessions") or [])
        last_msgs = state.get("last_assistant_message")
        if isinstance(last_msgs, dict):
            translator.last_assistant_message = {str(k): str(v) for k, v in last_msgs.items()}
        for item_id in state.get("started_items") or []:
            _bounded_add(translator._started_items, str(item_id))
        for item_id in state.get("finished_items") or []:
            _bounded_add(translator._finished_items, str(item_id))
        for item_id in state.get("seen_prompts") or []:
            _bounded_add(translator._seen_prompts, str(item_id))
        for run_id in state.get("finished_runs") or []:
            _bounded_add(translator._finished_runs, str(run_id))
        sub_statuses = state.get("subagent_statuses")
        if isinstance(sub_statuses, dict):
            translator.subagent_statuses = {str(k): str(v) for k, v in sub_statuses.items()}
        pt_subs = state.get("provider_thread_subagents")
        if isinstance(pt_subs, dict):
            translator.provider_thread_subagents = {
                str(k): dict(v) for k, v in pt_subs.items() if isinstance(v, dict)
            }
        pending = state.get("pending_requests")
        if isinstance(pending, dict):
            translator.pending_requests = {str(k): str(v) for k, v in pending.items()}
        return translator

    def to_state(self) -> dict[str, Any]:
        return {
            "db_fingerprint": self.db_fingerprint,
            "projects": self.projects,
            "threads": self.threads,
            "started_threads": sorted(self.started_threads),
            "active_sessions": sorted(self.active_sessions),
            "last_assistant_message": self.last_assistant_message,
            "started_items": list(self._started_items)[-MAX_TRACKED_IDS:],
            "finished_items": list(self._finished_items)[-MAX_TRACKED_IDS:],
            "seen_prompts": list(self._seen_prompts)[-MAX_TRACKED_IDS:],
            "finished_runs": list(self._finished_runs)[-MAX_TRACKED_IDS:],
            "subagent_statuses": self.subagent_statuses,
            "provider_thread_subagents": self.provider_thread_subagents,
            "pending_requests": self.pending_requests,
        }

    def seed_context(
        self,
        project_rows: Sequence[dict[str, Any]] | None,
        thread_rows: Sequence[dict[str, Any]] | None,
    ) -> None:
        """Refresh project and thread metadata caches without emitting events."""
        if project_rows is not None:
            for row in project_rows:
                project_id = _clean_str(row.get("project_id"))
                workspace_root = _clean_str(row.get("workspace_root"))
                if project_id and workspace_root:
                    self.projects[project_id] = workspace_root

        if thread_rows is not None:
            for row in thread_rows:
                thread_id = _clean_str(row.get("thread_id"))
                if not thread_id:
                    continue
                row_updated = _clean_str(row.get("updated_at"))
                cached = self.threads.get(thread_id)
                if (
                    cached is not None
                    and row_updated is not None
                    and cached.get("updated_at") == row_updated
                    and cached.get("cwd") is not None
                ):
                    continue
                payload = _load_json(row.get("payload_json"))
                self._upsert_thread_meta(thread_id, payload, fallback_row=row)

    def _parse_subagent_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        subagent_id = _clean_str(row.get("subagent_id"))
        thread_id = _clean_str(row.get("thread_id"))
        if not subagent_id or not thread_id:
            return None

        child_thread_id = _clean_str(row.get("child_thread_id"))
        if child_thread_id and (
            child_thread_id in self.threads or child_thread_id in self.started_threads
        ):
            return None

        payload = _load_json(row.get("payload_json"))
        model_sel = payload.get("modelSelection")
        model = (
            _clean_str(model_sel.get("model")) if isinstance(model_sel, dict) else None
        ) or _clean_str(payload.get("model"))
        provider = normalize_provider(
            row.get("provider_instance_id"),
            row.get("driver"),
            row.get("provider"),
        )
        raw_type = _clean_str(
            payload.get("agentType")
            or payload.get("role")
            or payload.get("name")
            or payload.get("title")
        )
        agent_type = _concise_agent_type(raw_type, provider or _clean_str(row.get("origin")))
        parent_meta = self.threads.get(thread_id, {})
        session_id = parent_meta.get("root_thread_id") or thread_id
        return {
            "subagent_id": subagent_id,
            "thread_id": thread_id,
            "session_id": session_id,
            "cwd": parent_meta.get("cwd"),
            "agent_type": agent_type,
            "provider": provider,
            "model": model,
            "provider_thread_id": _clean_str(row.get("provider_thread_id")),
            "status": _clean_str(row.get("status")) or "running",
            "started_at": _clean_str(row.get("started_at") or row.get("updated_at")),
            "completed_at": _clean_str(row.get("completed_at") or row.get("updated_at")),
        }

    def seed_subagents(self, rows: Sequence[dict[str, Any]] | None) -> None:
        """Index provider-native subagents before translating ``orchestration_events``."""
        if rows is None:
            return
        for row in rows:
            info = self._parse_subagent_row(row)
            if info is None or not info["provider_thread_id"]:
                continue
            self.provider_thread_subagents[info["provider_thread_id"]] = {
                "agent_id": info["subagent_id"],
                "agent_type": info["agent_type"],
                "provider": info["provider"],
                "model": info["model"],
                "session_id": info["session_id"],
                "cwd": info["cwd"],
                "started_at": info["started_at"],
            }

    def _upsert_thread_meta(
        self,
        thread_id: str,
        payload: dict[str, Any],
        fallback_row: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.threads.get(thread_id, {})
        row = fallback_row or {}

        project_id = (
            _clean_str(payload.get("projectId"))
            or _clean_str(row.get("project_id"))
            or existing.get("project_id")
        )
        worktree_path = _clean_str(payload.get("worktreePath")) or existing.get("worktree_path")
        cwd = (
            worktree_path
            or (self.projects.get(project_id) if project_id else None)
            or existing.get("cwd")
        )
        model_sel = payload.get("modelSelection")
        model = (
            _clean_str(model_sel.get("model")) if isinstance(model_sel, dict) else None
        ) or existing.get("model")
        provider = normalize_provider(
            payload.get("providerInstanceId"),
            model_sel.get("instanceId") if isinstance(model_sel, dict) else None,
            row.get("provider_instance_id"),
            row.get("default_provider"),
            payload.get("provider"),
        ) or existing.get("provider")
        title = (
            _clean_str(payload.get("title"))
            or _clean_str(row.get("title"))
            or existing.get("title")
        )

        lineage = payload.get("lineage")
        parent_id = existing.get("parent_thread_id")
        root_id = existing.get("root_thread_id")
        relationship = existing.get("relationship")
        if isinstance(lineage, dict):
            parent_id = _clean_str(lineage.get("parentThreadId")) or parent_id
            root_id = _clean_str(lineage.get("rootThreadId")) or root_id
            relationship = _clean_str(lineage.get("relationshipToParent")) or relationship

        is_subagent = bool(relationship == "subagent" and parent_id)
        resolved_root = (root_id or parent_id) if is_subagent else thread_id
        updated_at = (
            _clean_str(row.get("updated_at"))
            or _clean_str(payload.get("updatedAt"))
            or existing.get("updated_at")
        )

        meta = {
            "project_id": project_id,
            "worktree_path": worktree_path,
            "cwd": cwd,
            "provider": provider,
            "model": model,
            "title": title,
            "parent_thread_id": parent_id,
            "root_thread_id": resolved_root,
            "relationship": relationship,
            "is_subagent": is_subagent,
            "updated_at": updated_at,
        }
        self.threads[thread_id] = meta
        return meta

    def _build_event(self, payload: dict[str, Any], ts: datetime) -> tuple[HookEvent, datetime]:
        return HookEvent.model_validate(payload), ts

    def _emit_thread_start(
        self, thread_id: str, meta: dict[str, Any], ts: datetime
    ) -> list[tuple[HookEvent, datetime]]:
        if thread_id in self.started_threads:
            return []
        self.started_threads.add(thread_id)
        if meta["is_subagent"] and meta["root_thread_id"]:
            payload: dict[str, Any] = {
                "session_id": meta["root_thread_id"],
                "hook_event_name": "SubagentStart",
                "agent_id": thread_id,
                "agent_type": _concise_agent_type(meta.get("title"), meta.get("provider")),
                "source": SOURCE_NAME,
            }
            for key in ("cwd", "provider", "model", "title"):
                if meta.get(key):
                    payload[key] = meta[key]
            return [self._build_event(payload, ts)]

        self.active_sessions.discard(thread_id)
        payload = {
            "session_id": thread_id,
            "hook_event_name": "SessionStart",
            "source": SOURCE_NAME,
        }
        for key in ("cwd", "provider", "model", "title"):
            if meta.get(key):
                payload[key] = meta[key]
        return [self._build_event(payload, ts)]

    def _resolve_target(self, thread_id: str, provider_thread_id: str | None) -> dict[str, Any]:
        meta = self.threads.get(thread_id) or {
            "cwd": None,
            "provider": None,
            "model": None,
            "title": None,
            "root_thread_id": thread_id,
            "is_subagent": False,
        }
        if meta["is_subagent"] and meta.get("root_thread_id"):
            return {
                "session_id": meta["root_thread_id"],
                "agent_id": thread_id,
                "agent_type": _concise_agent_type(meta.get("title"), meta.get("provider")),
                "provider": meta.get("provider"),
                "model": meta.get("model"),
                "cwd": meta.get("cwd"),
                "title": meta.get("title"),
                "native_subagent": False,
            }
        if provider_thread_id and provider_thread_id in self.provider_thread_subagents:
            sub = self.provider_thread_subagents[provider_thread_id]
            return {
                "session_id": sub.get("session_id") or thread_id,
                "agent_id": sub["agent_id"],
                "agent_type": sub.get("agent_type"),
                "provider": sub.get("provider") or meta.get("provider"),
                "model": sub.get("model") or meta.get("model"),
                "cwd": sub.get("cwd") or meta.get("cwd"),
                "title": meta.get("title"),
                "native_subagent": True,
                "native_started_at": sub.get("started_at"),
            }
        return {
            "session_id": thread_id,
            "agent_id": None,
            "agent_type": None,
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            "cwd": meta.get("cwd"),
            "title": meta.get("title"),
            "native_subagent": False,
        }

    def _attach_target_fields(
        self, payload: dict[str, Any], target: dict[str, Any]
    ) -> dict[str, Any]:
        for key in ("agent_id", "agent_type", "provider", "model", "cwd", "title"):
            if target.get(key):
                payload[key] = target[key]
        return payload

    def _ensure_native_subagent_started(
        self, target: dict[str, Any], item_ts: datetime
    ) -> list[tuple[HookEvent, datetime]]:
        if not target.get("native_subagent") or not target.get("agent_id"):
            return []
        sub_id = str(target["agent_id"])
        if sub_id in self.subagent_statuses:
            return []
        self.subagent_statuses[sub_id] = "running"
        raw_started = target.get("native_started_at")
        start_ts = min(_parse_ts(raw_started), item_ts) if raw_started else item_ts
        start_payload = self._attach_target_fields(
            {
                "session_id": target["session_id"],
                "hook_event_name": "SubagentStart",
                "source": SOURCE_NAME,
            },
            target,
        )
        return [self._build_event(start_payload, start_ts)]

    def handle_event(
        self,
        event_type: str,
        stream_id: str,
        occurred_at: str,
        payload: dict[str, Any],
    ) -> list[tuple[HookEvent, datetime]]:
        """Translate one row from ``orchestration_events`` into zero or more ``HookEvent``s."""
        ts = _parse_ts(occurred_at)

        if event_type in ("project.created", "project.updated"):
            project_id = _clean_str(payload.get("projectId") or payload.get("id")) or stream_id
            workspace_root = _clean_str(
                payload.get("workspaceRoot") or payload.get("workspace_root")
            )
            if project_id and workspace_root:
                self.projects[project_id] = workspace_root
            return []

        if event_type in ("thread.created", "thread.metadata-updated"):
            thread_id = _clean_str(payload.get("id") or payload.get("threadId")) or stream_id
            prev = self.threads.get(thread_id, {})
            prev_sig = (
                prev.get("title"),
                prev.get("provider"),
                prev.get("model"),
                prev.get("cwd"),
            )
            meta = self._upsert_thread_meta(thread_id, payload)
            if thread_id not in self.started_threads:
                return self._emit_thread_start(thread_id, meta, ts)
            new_sig = (
                meta.get("title"),
                meta.get("provider"),
                meta.get("model"),
                meta.get("cwd"),
            )
            if event_type == "thread.metadata-updated" and new_sig != prev_sig:
                out_payload: dict[str, Any] = {
                    "session_id": meta["root_thread_id"] or thread_id,
                    "hook_event_name": "ThreadMetadataUpdated",
                    "source": SOURCE_NAME,
                }
                if meta["is_subagent"]:
                    out_payload["agent_id"] = thread_id
                    out_payload["agent_type"] = _concise_agent_type(
                        meta.get("title"), meta.get("provider")
                    )
                for key in ("cwd", "provider", "model", "title"):
                    if meta.get(key):
                        out_payload[key] = meta[key]
                return [self._build_event(out_payload, ts)]
            return []

        if event_type == "run.updated":
            return self._handle_run_updated(stream_id, ts, payload)

        if event_type == "turn-item.updated":
            return self._handle_turn_item_updated(stream_id, ts, payload)

        return []

    def _handle_run_updated(
        self, stream_id: str, ts: datetime, payload: dict[str, Any]
    ) -> list[tuple[HookEvent, datetime]]:
        thread_id = _clean_str(payload.get("threadId")) or stream_id
        run_id = _clean_str(payload.get("id"))
        status_val = _clean_str(payload.get("status"))
        meta = self.threads.get(thread_id)
        if meta is not None:
            model_sel = payload.get("modelSelection")
            if isinstance(model_sel, dict):
                model = _clean_str(model_sel.get("model"))
                if model:
                    meta["model"] = model
                prov = normalize_provider(
                    payload.get("providerInstanceId"), model_sel.get("instanceId")
                )
                if prov:
                    meta["provider"] = prov

        if not run_id or status_val not in _TERMINAL_RUN_STATUSES:
            return []
        if run_id in self._finished_runs:
            return []
        _bounded_add(self._finished_runs, run_id)

        out: list[tuple[HookEvent, datetime]] = []
        if thread_id not in self.started_threads and thread_id in self.threads:
            out.extend(self._emit_thread_start(thread_id, self.threads[thread_id], ts))

        target = self._resolve_target(thread_id, None)
        last_msg = self.last_assistant_message.pop(thread_id, None)
        is_subagent = target["agent_id"] is not None
        stop_payload: dict[str, Any] = {
            "session_id": target["session_id"],
            "hook_event_name": "SubagentStop" if is_subagent else "Stop",
            "source": SOURCE_NAME,
            "run_status": status_val,
        }
        if last_msg:
            stop_payload["last_assistant_message"] = last_msg
        self._attach_target_fields(stop_payload, target)
        if not is_subagent:
            self.active_sessions.discard(target["session_id"])
        out.append(self._build_event(stop_payload, ts))
        return out

    def _handle_turn_item_updated(
        self, stream_id: str, ts: datetime, payload: dict[str, Any]
    ) -> list[tuple[HookEvent, datetime]]:
        item_type = _clean_str(payload.get("type"))
        if item_type in _IGNORED_ITEM_TYPES or _is_workspace_prep_item(payload):
            return []

        thread_id = _clean_str(payload.get("threadId")) or stream_id
        item_id = _clean_str(payload.get("id"))
        if not item_id:
            return []

        out: list[tuple[HookEvent, datetime]] = []
        if thread_id not in self.started_threads and thread_id in self.threads:
            out.extend(self._emit_thread_start(thread_id, self.threads[thread_id], ts))

        provider_thread_id = _clean_str(payload.get("providerThreadId"))
        target = self._resolve_target(thread_id, provider_thread_id)
        status_val = _clean_str(payload.get("status")) or ""

        if item_type == "user_message":
            if target["agent_id"] is not None or item_id in self._seen_prompts:
                return out
            _bounded_add(self._seen_prompts, item_id)
            prompt_payload = self._attach_target_fields(
                {
                    "session_id": target["session_id"],
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": payload.get("text") or "",
                    "source": SOURCE_NAME,
                },
                target,
            )
            self.active_sessions.add(target["session_id"])
            out.append(self._build_event(prompt_payload, ts))
            return out

        if item_type == "assistant_message":
            if status_val == "completed":
                preview = _truncate(_clean_str(payload.get("text")))
                if preview:
                    self.last_assistant_message[thread_id] = preview
            return out

        raw_tool_name = _clean_str(payload.get("toolName"))
        if item_type not in _TOOL_ITEM_TYPES and not raw_tool_name:
            return out

        tool_name = normalize_tool_name(item_type, raw_tool_name)
        if not tool_name:
            return out
        tool_input = normalize_tool_input(item_type, payload)

        if status_val in _ACTIVE_ITEM_STATUSES:
            if item_id not in self._started_items:
                out.extend(self._ensure_native_subagent_started(target, ts))
                _bounded_add(self._started_items, item_id)
                pre_payload = self._attach_target_fields(
                    {
                        "session_id": target["session_id"],
                        "hook_event_name": "PreToolUse",
                        "tool_name": tool_name,
                        "tool_use_id": item_id,
                        "tool_input": tool_input,
                        "source": SOURCE_NAME,
                    },
                    target,
                )
                if target["agent_id"] is None:
                    self.active_sessions.add(target["session_id"])
                out.append(self._build_event(pre_payload, ts))
            return out

        if status_val in _TERMINAL_ITEM_STATUSES:
            if item_id in self._finished_items:
                return out
            _bounded_add(self._finished_items, item_id)

            started_ts = _parse_ts(payload.get("startedAt")) if payload.get("startedAt") else ts
            if item_id not in self._started_items:
                out.extend(self._ensure_native_subagent_started(target, started_ts))
                _bounded_add(self._started_items, item_id)
                pre_payload = self._attach_target_fields(
                    {
                        "session_id": target["session_id"],
                        "hook_event_name": "PreToolUse",
                        "tool_name": tool_name,
                        "tool_use_id": item_id,
                        "tool_input": tool_input,
                        "source": SOURCE_NAME,
                    },
                    target,
                )
                out.append(self._build_event(pre_payload, started_ts))

            parsed_code = _safe_int(payload.get("exitCode"))
            failed = status_val in _FAILED_ITEM_STATUSES or (
                item_type == "command_execution" and parsed_code is not None and parsed_code != 0
            )
            post_payload: dict[str, Any] = {
                "session_id": target["session_id"],
                "hook_event_name": "PostToolUseFailure" if failed else "PostToolUse",
                "tool_name": tool_name,
                "tool_use_id": item_id,
                "tool_input": tool_input,
                "source": SOURCE_NAME,
            }
            if failed:
                error_text = _clean_str(payload.get("error")) or (
                    f"exit code {parsed_code}" if parsed_code is not None else status_val
                )
                post_payload["error"] = error_text
            self._attach_target_fields(post_payload, target)
            if target["agent_id"] is None:
                self.active_sessions.add(target["session_id"])
            out.append(self._build_event(post_payload, ts))

        return out

    def sync_subagents(
        self,
        rows: Sequence[dict[str, Any]] | None,
        *,
        session_latest_ts: dict[str, datetime] | None = None,
    ) -> list[tuple[HookEvent, datetime]]:
        """Track ``orchestration_v2_projection_subagents`` rows not backed by a child thread."""
        if rows is None:
            return []
        out: list[tuple[HookEvent, datetime]] = []
        for row in rows:
            info = self._parse_subagent_row(row)
            if info is None:
                continue

            subagent_id = info["subagent_id"]
            if info["provider_thread_id"]:
                self.provider_thread_subagents[info["provider_thread_id"]] = {
                    "agent_id": subagent_id,
                    "agent_type": info["agent_type"],
                    "provider": info["provider"],
                    "model": info["model"],
                    "session_id": info["session_id"],
                    "cwd": info["cwd"],
                    "started_at": info["started_at"],
                }

            status_val = info["status"]
            prev_status = self.subagent_statuses.get(subagent_id)
            if prev_status == status_val:
                continue

            self.subagent_statuses[subagent_id] = status_val
            session_id = info["session_id"]
            started_ts = _clamp_ts(_parse_ts(info["started_at"]), session_latest_ts, session_id)

            if prev_status is None:
                start_payload: dict[str, Any] = {
                    "session_id": session_id,
                    "hook_event_name": "SubagentStart",
                    "agent_id": subagent_id,
                    "agent_type": info["agent_type"],
                    "source": SOURCE_NAME,
                }
                for key in ("cwd", "provider", "model"):
                    if info.get(key):
                        start_payload[key] = info[key]
                out.append(self._build_event(start_payload, started_ts))

            if status_val in _TERMINAL_RUN_STATUSES and (
                prev_status is None or prev_status not in _TERMINAL_RUN_STATUSES
            ):
                raw_stop_ts = _parse_ts(info["completed_at"])
                stop_ts = max(_clamp_ts(raw_stop_ts, session_latest_ts, session_id), started_ts)
                stop_payload: dict[str, Any] = {
                    "session_id": session_id,
                    "hook_event_name": "SubagentStop",
                    "agent_id": subagent_id,
                    "agent_type": info["agent_type"],
                    "source": SOURCE_NAME,
                }
                for key in ("cwd", "provider", "model"):
                    if info.get(key):
                        stop_payload[key] = info[key]
                out.append(self._build_event(stop_payload, stop_ts))

        return out

    def sync_runtime_requests(
        self,
        rows: Sequence[dict[str, Any]] | None,
        *,
        sessions_updated: set[str] | None = None,
        session_latest_ts: dict[str, datetime] | None = None,
    ) -> list[tuple[HookEvent, datetime]]:
        """Map ``orchestration_v2_projection_runtime_requests`` into waiting notifications."""
        if rows is None:
            return []
        out: list[tuple[HookEvent, datetime]] = []
        current_pending: dict[str, tuple[str, dict[str, Any]]] = {}

        for row in rows:
            req_id = _clean_str(row.get("runtime_request_id"))
            thread_id = _clean_str(row.get("thread_id"))
            if not req_id or not thread_id:
                continue
            meta = self.threads.get(thread_id, {})
            session_id = meta.get("root_thread_id") or thread_id
            if _clean_str(row.get("status")) == "pending":
                current_pending[req_id] = (session_id, row)

        for req_id, (session_id, row) in current_pending.items():
            if req_id in self.pending_requests:
                continue
            self.pending_requests[req_id] = session_id
            payload = _load_json(row.get("payload_json"))
            message = (
                _clean_str(payload.get("message"))
                or _clean_str(payload.get("title"))
                or _clean_str(payload.get("prompt"))
                or _clean_str(payload.get("reason"))
                or "T3 Code is waiting for your input"
            )
            ts = _clamp_ts(_parse_ts(row.get("created_at")), session_latest_ts, session_id)
            notif = {
                "session_id": session_id,
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": message,
                "runtime_request_id": req_id,
                "source": SOURCE_NAME,
            }
            cwd = self.threads.get(session_id, {}).get("cwd")
            if cwd:
                notif["cwd"] = cwd
            out.append(self._build_event(notif, ts))

        resolved_ids = [rid for rid in self.pending_requests if rid not in current_pending]
        still_pending_sessions = {sid for sid, _ in current_pending.values()}
        resolved_ts_by_req = {
            _clean_str(r.get("runtime_request_id")): _parse_ts(r.get("resolved_at"))
            for r in rows
            if r.get("resolved_at")
        }

        emitted_resume_for: set[str] = set()
        for req_id in resolved_ids:
            session_id = self.pending_requests.pop(req_id)
            if (
                session_id in still_pending_sessions
                or session_id in emitted_resume_for
                or session_id not in self.active_sessions
                or (sessions_updated is not None and session_id in sessions_updated)
            ):
                continue
            emitted_resume_for.add(session_id)
            ts = _clamp_ts(
                resolved_ts_by_req.get(req_id) or datetime.now(UTC),
                session_latest_ts,
                session_id,
            )
            resume_payload: dict[str, Any] = {
                "session_id": session_id,
                "hook_event_name": "PostToolUse",
                "tool_name": "RuntimeRequest",
                "tool_use_id": f"runtime-request:{req_id}",
                "tool_input": {"description": "Request resolved"},
                "source": SOURCE_NAME,
            }
            cwd = self.threads.get(session_id, {}).get("cwd")
            if cwd:
                resume_payload["cwd"] = cwd
            out.append(self._build_event(resume_payload, ts))

        return out

    def sync_missing_threads(
        self,
        thread_rows: Sequence[dict[str, Any]] | None,
        run_rows: Sequence[dict[str, Any]] | None,
    ) -> list[tuple[HookEvent, datetime]]:
        """Backfill threads present in projections that had no ``orchestration_events``."""
        if thread_rows is None or run_rows is None:
            return []
        latest_run_by_thread: dict[str, dict[str, Any]] = {}
        for run in run_rows:
            tid = _clean_str(run.get("thread_id"))
            if not tid:
                continue
            prev = latest_run_by_thread.get(tid)
            run_ord = _safe_int(run.get("ordinal")) or 0
            prev_ord = (_safe_int(prev.get("ordinal")) or 0) if prev is not None else -1
            if prev is None or run_ord >= prev_ord:
                latest_run_by_thread[tid] = run

        ordered_rows = sorted(
            thread_rows,
            key=lambda r: int(
                bool(self.threads.get(_clean_str(r.get("thread_id")) or "", {}).get("is_subagent"))
            ),
        )
        out: list[tuple[HookEvent, datetime]] = []
        for row in ordered_rows:
            if row.get("deleted_at") is not None:
                continue
            thread_id = _clean_str(row.get("thread_id"))
            if not thread_id or thread_id in self.started_threads:
                continue
            meta = self.threads.get(thread_id)
            if meta is None:
                meta = self._upsert_thread_meta(
                    thread_id, _load_json(row.get("payload_json")), fallback_row=row
                )

            created_ts = _parse_ts(row.get("created_at") or row.get("updated_at"))
            out.extend(self._emit_thread_start(thread_id, meta, created_ts))

            latest_run = latest_run_by_thread.get(thread_id)
            if latest_run and _clean_str(latest_run.get("status")) in _TERMINAL_RUN_STATUSES:
                run_id = _clean_str(latest_run.get("run_id"))
                if run_id and run_id not in self._finished_runs:
                    _bounded_add(self._finished_runs, run_id)
                    stop_ts = _parse_ts(latest_run.get("completed_at") or row.get("updated_at"))
                    is_subagent = bool(meta["is_subagent"] and meta.get("root_thread_id"))
                    stop_payload: dict[str, Any] = {
                        "session_id": meta["root_thread_id"] if is_subagent else thread_id,
                        "hook_event_name": "SubagentStop" if is_subagent else "Stop",
                        "source": SOURCE_NAME,
                        "run_status": latest_run.get("status"),
                    }
                    if is_subagent:
                        stop_payload["agent_id"] = thread_id
                        stop_payload["agent_type"] = _concise_agent_type(
                            meta.get("title"), meta.get("provider")
                        )
                    for key in ("cwd", "provider", "model", "title"):
                        if meta.get(key):
                            stop_payload[key] = meta[key]
                    out.append(self._build_event(stop_payload, stop_ts))

        return out


class T3CodeReader:
    """Polls T3 Code's ``statev2.sqlite`` read-only and stores translated events."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._known_thread_ids: set[str] = set()

    def _connect_readonly(self) -> sqlite3.Connection | None:
        resolved = self._db_path.expanduser().resolve()
        if not resolved.exists():
            return None
        uri = f"{resolved.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _query_optional(conn: sqlite3.Connection, sql: str) -> list[dict[str, Any]] | None:
        try:
            return [dict(row) for row in conn.execute(sql).fetchall()]
        except sqlite3.Error as exc:
            logger.debug("Skipping unavailable T3 Code projection query: %s", exc)
            return None

    def knows_thread(self, thread_id: str) -> bool:
        """Whether ``thread_id`` exists in this reader's T3 Code database."""
        if thread_id in self._known_thread_ids:
            return True
        try:
            conn = self._connect_readonly()
        except sqlite3.Error:
            return False
        if conn is None:
            return False
        try:
            row = conn.execute(
                """
                SELECT 1 FROM orchestration_v2_projection_threads
                WHERE thread_id = ? AND deleted_at IS NULL
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
            if row is not None:
                self._known_thread_ids.add(thread_id)
                return True
            return False
        except sqlite3.Error:
            return False
        finally:
            conn.close()

    def poll_once(self, store: EventStore) -> list[StoredEvent]:
        """Read unseen T3 Code events and projection diffs, persisting atomically."""
        try:
            conn = self._connect_readonly()
        except sqlite3.Error as exc:
            logger.debug("Could not open T3 Code DB %s: %s", self._db_path, exc)
            return []

        if conn is None:
            return []

        all_stored: list[StoredEvent] = []
        try:
            cursor, state = store.get_cursor(SOURCE_NAME)
            try:
                max_row = conn.execute(
                    "SELECT MAX(sequence) AS max_seq FROM orchestration_events"
                ).fetchone()
                first_row = conn.execute(
                    """
                    SELECT sequence, event_id, occurred_at
                    FROM orchestration_events
                    ORDER BY sequence ASC
                    LIMIT 1
                    """
                ).fetchone()
            except sqlite3.Error as exc:
                logger.debug("Could not inspect T3 Code orchestration_events: %s", exc)
                return []

            max_seq = int(max_row["max_seq"]) if max_row and max_row["max_seq"] is not None else 0
            curr_fp = (
                f"{first_row['sequence']}:{first_row['event_id']}:{first_row['occurred_at']}"
                if first_row
                else None
            )
            prev_fp = state.get("db_fingerprint") if isinstance(state, dict) else None
            if max_seq < cursor or (
                prev_fp is not None and curr_fp is not None and curr_fp != prev_fp
            ):
                # T3 Code recreated or replaced its DB; start cleanly from 0.
                cursor = 0
                translator = T3CodeTranslator()
                last_saved_state: dict[str, Any] = {}
            else:
                translator = T3CodeTranslator.from_state(state)
                last_saved_state = state
            translator.db_fingerprint = curr_fp

            project_rows = self._query_optional(
                conn,
                "SELECT project_id, workspace_root, deleted_at FROM projection_projects",
            )
            thread_rows = self._query_optional(
                conn,
                """
                SELECT thread_id, project_id, title, default_provider,
                       provider_instance_id, created_at, updated_at,
                       deleted_at, payload_json
                FROM orchestration_v2_projection_threads
                """,
            )
            subagent_rows = self._query_optional(
                conn,
                """
                SELECT subagent_id, thread_id, run_id, parent_node_id, provider,
                       provider_instance_id, driver, provider_thread_id, child_thread_id,
                       origin, status, started_at, completed_at, updated_at, payload_json
                FROM orchestration_v2_projection_subagents
                """,
            )
            translator.seed_context(project_rows, thread_rows)
            translator.seed_subagents(subagent_rows)

            sessions_updated: set[str] = set()
            session_latest_ts: dict[str, datetime] = {}

            while True:
                event_rows = conn.execute(
                    """
                    SELECT sequence, stream_id, event_type, occurred_at, payload_json
                    FROM orchestration_events
                    WHERE sequence > ?
                    ORDER BY sequence ASC
                    LIMIT ?
                    """,
                    (cursor, BATCH_LIMIT),
                ).fetchall()

                if not event_rows:
                    break

                batch_items: list[tuple[HookEvent, datetime | None]] = []
                batch_cursor = cursor
                for ev_row in event_rows:
                    batch_cursor = int(ev_row["sequence"])
                    payload = _load_json(ev_row["payload_json"])
                    emitted = translator.handle_event(
                        ev_row["event_type"],
                        ev_row["stream_id"],
                        ev_row["occurred_at"],
                        payload,
                    )
                    for ev, ev_ts in emitted:
                        prev_ts = session_latest_ts.get(ev.session_id)
                        if prev_ts is None or ev_ts > prev_ts:
                            session_latest_ts[ev.session_id] = ev_ts
                        if ev.agent_id is None and ev.hook_event_name in _SESSION_PROGRESS_HOOKS:
                            sessions_updated.add(ev.session_id)
                    batch_items.extend(emitted)

                cursor = batch_cursor
                batch_state = translator.to_state()
                all_stored.extend(
                    store.add_batch_with_cursor(SOURCE_NAME, batch_items, cursor, batch_state)
                )
                last_saved_state = batch_state
                if len(event_rows) < BATCH_LIMIT:
                    break

            proj_items: list[tuple[HookEvent, datetime | None]] = []
            run_rows = self._query_optional(
                conn,
                """
                SELECT run_id, thread_id, ordinal, provider, provider_instance_id,
                       status, requested_at, completed_at, payload_json
                FROM orchestration_v2_projection_runs
                """,
            )
            proj_items.extend(translator.sync_missing_threads(thread_rows, run_rows))
            proj_items.extend(
                translator.sync_subagents(subagent_rows, session_latest_ts=session_latest_ts)
            )

            request_rows = self._query_optional(
                conn,
                """
                SELECT runtime_request_id, thread_id, node_id, kind, status,
                       created_at, resolved_at, payload_json
                FROM orchestration_v2_projection_runtime_requests
                """,
            )
            proj_items.extend(
                translator.sync_runtime_requests(
                    request_rows,
                    sessions_updated=sessions_updated,
                    session_latest_ts=session_latest_ts,
                )
            )

            self._known_thread_ids.update(translator.threads.keys())
            new_state = translator.to_state()
            if proj_items or new_state != last_saved_state:
                all_stored.extend(
                    store.add_batch_with_cursor(SOURCE_NAME, proj_items, cursor, new_state)
                )

            return all_stored
        except sqlite3.Error as exc:
            logger.debug("Transient SQLite error reading T3 Code DB: %s", exc)
            return all_stored
        finally:
            conn.close()
