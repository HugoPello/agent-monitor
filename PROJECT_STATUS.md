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

Design review with the `impeccable` skill (critique: isolated design review
plus detector). First run 23/36; its five priority issues are fixed here:

- [x] "Finished" activity: a turn that ended with `Stop` is no longer "Idle"
- [x] Waiting sessions surface: urgency order, "(1) waiting" tab title,
      tinted stage, polite screen-reader announcements, named Details buttons
- [x] Timeline as a full-screen sheet below 900px; focus moves into it
- [x] Finished nodes keep 4.5:1 text (fade the frame, not the label)
- [x] Plain-language timeline ("You asked", "Tool failed"), raw hook name kept small
- [x] Dashboard files served with `Cache-Control: no-cache` (a cached
      index.html could meet a newer app.js)

Second run (23/36) found three deeper P1s, fixed here too:

- [x] Timeline as a `<dialog>`: modal sheet below 900px (focus contained,
      inert page, 44px close on touch), non-modal side drawer above it that
      pushes the page instead of reflowing the grid to one column
- [x] The stage says why it needs you ("Claude needs your permission to use
      Bash") and when a step failed in this turn (`waiting_reason`,
      `failed_steps`, `last_failure_at` in the summary)
- [x] Ended stages keep 4.5:1 text; new stages paint in their colours on load

Third run: 24/40 (21/36 on the same heuristics). Scores are noisy across
independent reviews; the remaining issues (failure tied to its node,
attention hierarchy, empty cards, palette families) mostly belong to the
shell redesign. Reports live in `.impeccable/critique/` (local, ignored).

## Phase 9 — Shell redesign (`feat/shell-redesign`)

Done with the `impeccable` skill: `PRODUCT.md` from an interview, two
concept-seed hands rejected, then a direction pinned by the owner ("simple,
modern, cybernetic, with marked and moving flows") and approved from one mock.
Contract in `.impeccable/surfaces/`, system documented in `DESIGN.md`.

- [x] A "live circuit": dark reticle ground (light variant too), hairline
      panels, monospace for anything measured, no web fonts
- [x] Colour families with one job each: cyan busy, amber needs you (the only
      pulse), red failures, green delivered, slate at rest
- [x] Live sessions as full stages; quiet ones as compact "At rest" rows
- [x] Flows: streaming dashes and packets of light to running subagents
      (CSS offset-path), a green packet back when one finishes
- [x] Attention summary chips that jump to sessions; keys w, f, j/k, Esc
- [x] Failures on the node that failed ("1 failed"), a note naming agent and
      tool that opens the timeline at that row (`failed_steps`,
      `last_failure_tool`, `last_failure_agent` in the API)
- [x] Project names as headings; detector clean against `DESIGN.md`
- [x] Verified in a browser: dark/light, desktop/mobile, live hand-back,
      shortcuts, failure jump

## Phase 10 — Demo and docs (`docs/flow-demo`)

- [x] `scripts/demo.py` replays three sessions in about 40 s: three
      subagents in parallel, one failing a test run and retrying, a
      permission prompt, a session already ended (for "At rest")
- [x] `tests/test_demo.py` checks the end state: activities, three finished
      subagents, the failure attributed to the right subagent and tool
- [x] README: features of the live circuit, a "Status and activity" section
      with the tool → activity table, GIF caption, design decision on
      animations showing only what hooks prove
- [x] Demo pacing checked live in a browser
- [x] `docs/demo.gif` re-recorded on the new design by the owner (1200 px, 2 MB)
