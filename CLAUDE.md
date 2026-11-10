# CLAUDE.md — agent-monitor

Real-time monitor for Claude Code agents. Claude Code hooks POST events to a
FastAPI server, which stores them in SQLite and streams them to a vanilla
HTML/JS dashboard over SSE.

This is a portfolio project: small but complete, clean code, tested, well
documented. The owner is learning from it, so explain important decisions.

## Stack

- Python 3.12+ · FastAPI · Pydantic v2 · SQLite (stdlib `sqlite3`, no ORM)
- SSE via `StreamingResponse` for real-time updates
- Frontend: plain HTML + vanilla JS + CSS. No frameworks, no build step.
- Tooling: pytest, ruff (lint + format), GitHub Actions CI

## Layout

```
src/agent_monitor/   application package
hooks/               hook script installed into Claude Code settings
tests/               pytest suite
.claude/agents/      project subagents (researcher)
```

## Commands

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e ".[dev]"
ruff check . && ruff format --check .
pytest
```

## Working rules

- **Plan first.** Propose a phase and wait for approval before implementing.
- **Research is delegated** to the `researcher` subagent (Haiku, read-only):
  docs lookups, package versions, API checks, listing files. The main agent
  designs and writes code. Verify critical findings against the source
  before relying on them. Never invent hook fields, flags or APIs.
- **Git:**
  - Never commit to `main`. One branch per phase: `feat/...`, `chore/...`,
    `docs/...`, `fix/...`.
  - Atomic commits using Conventional Commits (`feat:`, `fix:`, `chore:`,
    `docs:`, `test:`, `refactor:`, `ci:`).
  - Stage files explicitly by path. Never `git add .` or `git add -A`.
  - Push the branch; the owner merges to `main` through a PR.
- **Definition of done for a phase:** `pytest` green, `ruff check` and
  `ruff format --check` clean, `PROJECT_STATUS.md` updated.
- **Config and secrets** live in `.env` (git-ignored), created from
  `.env.example`. Nothing sensitive in the repo.
- **Three strikes:** if something fails 3 times in a row, stop and explain
  the problem instead of retrying.

## Design principles

- The hook must never slow down or break Claude Code: async command hook,
  short timeout, always exits 0, prints nothing.
- Hook payloads evolve: the event model requires only `session_id` and
  `hook_event_name`, accepts unknown fields, and stores the raw payload.
- Status computation is a pure function of a session's events, so it is
  easy to test.
- The server binds to `127.0.0.1` by default; payloads can contain code and
  secrets from tool inputs.
- Out of scope for the MVP: auth, multi-user, cloud deployment, frontend
  frameworks.
