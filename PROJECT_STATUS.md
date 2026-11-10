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

- [ ] Pydantic `HookEvent` model (required: `session_id`, `hook_event_name`; extra fields allowed)
- [ ] SQLite schema and repository (stdlib `sqlite3`)
- [ ] Tests for the model and the repository

## Phase 2 — REST API and status (`feat/events-api`)

- [ ] `POST /events`
- [ ] `GET /agents` (current state per session)
- [ ] `GET /agents/{id}/events` (timeline)
- [ ] Pure status computation function
- [ ] API and status tests

## Phase 3 — Real-time stream (`feat/sse-stream`)

- [ ] In-memory broadcaster (one queue per client)
- [ ] `GET /stream` (SSE)
- [ ] Stream tests

## Phase 4 — Hook script (`feat/hook-script`)

- [ ] `hooks/send_event.py` (stdlib only, short timeout, always exit 0)
- [ ] Example `settings.json` snippet (async command hook)
- [ ] Tests, including "server down" behaviour
- [ ] Verify with real events: subagent `session_id` and `agent_id`

## Phase 5 — Dashboard (`feat/dashboard`)

- [ ] Static page served by FastAPI
- [ ] One card per session, subagents nested, status badge
- [ ] Event timeline, live updates via `EventSource`

## Phase 6 — Docs and Docker (`docs/readme-docker`)

- [ ] README: GIF/screenshot, mermaid diagram, install, hook setup
- [ ] `Dockerfile`
