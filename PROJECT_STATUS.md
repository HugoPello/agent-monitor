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

- [ ] Static page served by FastAPI
- [ ] One card per session, subagents nested, status badge
- [ ] Event timeline, live updates via `EventSource`

## Phase 6 — Docs and Docker (`docs/readme-docker`)

- [ ] README: GIF/screenshot, mermaid diagram, install, hook setup
- [ ] `Dockerfile`
