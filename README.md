# agent-monitor

[![CI](https://github.com/HugoPello/agent-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/HugoPello/agent-monitor/actions/workflows/ci.yml)

Real-time monitor for [Claude Code](https://code.claude.com) agents. Claude Code
hooks send events to a small FastAPI server, which stores them in SQLite and
streams them to a web dashboard: one card per session, with its status and an
event timeline.

> 🚧 Work in progress. See [PROJECT_STATUS.md](PROJECT_STATUS.md) for the roadmap.

## Development

Requires Python 3.12+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows (on macOS/Linux: source .venv/bin/activate)
pip install -e ".[dev]"
cp .env.example .env

ruff check . && ruff format --check .
pytest
```

## Running the server

```bash
python -m agent_monitor        # or: agent-monitor
```

The API listens on `http://127.0.0.1:8765` by default (see `.env.example`).
Interactive API docs are available at `/docs`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/events` | Receive a hook event (body: the hook's JSON input) |
| `GET` | `/agents` | Current state of every session, most recent first |
| `GET` | `/agents/{session_id}/events` | Event timeline of one session |
| `GET` | `/health` | Liveness check |
