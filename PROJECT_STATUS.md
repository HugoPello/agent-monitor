# Project status

Each phase is one branch and one PR. A phase is done when tests are green,
ruff is clean and the PR is merged.

## Phase 0 — Project setup (`chore/project-setup`)

- [x] `CLAUDE.md` with project rules
- [x] `PROJECT_STATUS.md` (this file)
- [x] `researcher` subagent (`.claude/agents/researcher.md`)
- [x] `pyproject.toml` with ruff and pytest config
- [x] `.gitignore` and `.env.example`
- [x] Package skeleton with a smoke test
- [x] README skeleton (UTF-8)
- [x] GitHub Actions CI: ruff + pytest

## Phase 1 — Event model and storage (`feat/event-model-storage`)

- [x] Pydantic `HookEvent` model (required: `session_id`, `hook_event_name`; extra fields allowed)
- [x] Long strings truncated before storage (10k chars)
- [x] SQLite schema and `EventStore` (stdlib `sqlite3`, WAL, one connection per operation)
- [x] Tests for the model and the store

## Phase 2 — REST API and status (`feat/events-api`)

- [x] Settings from `.env` (pydantic-settings)
- [x] `POST /events`
- [x] `GET /agents` (current state per session, with subagents)
- [x] `GET /agents/{id}/events` (timeline)
- [x] Pure status computation function
- [x] `python -m agent_monitor` entry point
- [x] Config, status, storage and API tests

## Phase 3 — Real-time stream (`feat/sse-stream`)

- [x] In-memory broadcaster (one bounded queue per client, slow clients dropped)
- [x] `GET /stream` (SSE) with retry hint and keep-alive pings
- [x] Each message carries the event and the session's updated summary
- [x] Clean shutdown with open streams (`MonitorServer`)
- [x] Stream tests, including a real-server integration test

## Phase 4 — Hook script (`feat/hook-script`)

- [x] `hooks/send_event.py` (stdlib only, 2 s timeout, always exit 0, ignores proxies)
- [x] `hooks/settings.example.json` (async command hooks, enabled per project)
- [x] Tests: server up/down/unresponsive, invalid input, proxies, logging
- [x] Verify with real events: subagent `session_id` and `agent_id`
  - Subagent events carry the **parent's** `session_id`, plus `agent_id` and `agent_type`.
  - Main-agent events have no `agent_id`.
  - The `Agent` tool can return (`PostToolUse`) before the subagent finishes (background subagents), so subagent state must come from `SubagentStart`/`SubagentStop`, not from the `Agent` tool call.

## Phase 5 — Dashboard (`feat/dashboard`)

- [x] Static page served by FastAPI (`/` and `/static`), shipped as package data
- [x] One card per session, subagents nested, status badge (colour + label)
- [x] Event timeline, live updates via `EventSource`, catch-up on reconnect
- [x] XSS-safe rendering (`textContent` only, guarded by a test)
- [x] CI checks the built wheel contains the dashboard files
- [x] Verified in a browser with real events (dark/light, desktop/mobile)

## Phase 6 — Docs and Docker (`docs/readme-docker`)

- [x] README: demo GIF, mermaid diagram, quick start, hook setup, design decisions
- [x] `scripts/demo.py`: fictional sessions to try the dashboard without Claude Code
- [x] `Dockerfile` (multi-stage, from wheel, non-root, health check) and `.dockerignore`
- [x] CI job that builds the image and smoke-tests the container
- [x] MIT license

**MVP complete.**

## Phase 7 — Activity model (`feat/activity-model`)

- [x] `Activity` (idle, thinking, researching, coding, running, delegating,
      working, waiting for you, stale, ended) derived by a pure function from
      the status and the running tool
- [x] One `TOOL_ACTIVITY` table with tool names from the Claude Code tools
      reference; unknown and MCP tools fall back to "working"
- [x] Per-subagent activity from its own latest event, plus `last_event_at`;
      a running subagent silent past `stale_after` is stale
- [x] `activity` exposed in `/agents` and in every SSE message
      (`current_tool` kept for debugging)
- [x] Tests with exact timestamps for every rule

## Phase 8 — Flow dashboard (`feat/flow-dashboard`)

- [x] One stage per session: main agent on the left, subagents on the right,
      each node showing only its activity (and `agent_type` for subagents)
- [x] Keyed DOM updates instead of rebuilding on every event, stable stage order
- [x] SVG connectors measured from the CSS-grid layout, redrawn on resize
- [x] Colour and soft pulse per activity, in light and dark themes
- [x] Live-only animations: connector draws on start, dashes flow while
      running, a pulse returns to the main agent on stop, then the node fades
- [x] `prefers-reduced-motion`: no movement, colours and labels still change
- [x] Edge cases: "+N done" beyond four finished subagents, stale subagents
      (dashed, no flow), several sessions, reconnect catch-up without replay
- [x] Full timeline behind a per-stage "Details" button
- [x] XSS guard extended (DOMParser, setHTMLUnsafe, inline handlers...)
- [x] Verified in a browser with demo data (desktop/mobile, dark/light)

## Phase 9 — Demo and docs (`docs/flow-demo`)

- [ ] `scripts/demo.py` with parallel subagents
- [ ] README: features, activities, GIF caption
