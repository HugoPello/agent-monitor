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

Open **http://127.0.0.1:8765** for the dashboard: one card per session, with
its status, current tool and subagents. Click a card to see its event timeline.
Both update live.

The API is on the same port (see `.env.example` to change it). Interactive API
docs are available at `/docs`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/events` | Receive a hook event (body: the hook's JSON input) |
| `GET` | `/agents` | Current state of every session, most recent first |
| `GET` | `/agents/{session_id}/events` | Event timeline of one session |
| `GET` | `/stream` | Live feed of new events ([Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events)) |
| `GET` | `/health` | Liveness check |

Each `/stream` message is an `agent_event` whose `data` is
`{"event": <stored event>, "agent": <updated session summary>}`. Try it with:

```bash
curl -N http://127.0.0.1:8765/stream
```

## Hook setup

Claude Code sends events to the monitor through
[hooks](https://code.claude.com/docs/en/hooks).
[`hooks/send_event.py`](hooks/send_event.py) forwards each event to the server.
It uses only the Python standard library and runs with your system Python,
so it needs no virtualenv.

The hook is designed to never get in your way:

- It runs as an `async` hook, so Claude does not wait for it.
- It gives up after 2 seconds.
- It always exits 0 and prints nothing, even when the monitor is not running.

Enable it **per project**, only where you want it. Your global Claude Code
configuration is not touched.

1. In the project you want to monitor, create `.claude/settings.local.json`
   with the content of [`hooks/settings.example.json`](hooks/settings.example.json).
2. Replace `/ABSOLUTE/PATH/TO/agent-monitor` with the path to this repository.
   Use forward slashes, also on Windows: on Windows Claude Code runs hook
   commands in Git Bash, for example `C:/Users/you/agent-monitor`.
3. Make sure `.claude/settings.local.json` is git-ignored in that project.
   Claude Code only does this automatically for files it creates itself.

Claude Code only runs hooks in folders you have accepted in the workspace
trust dialog.

To monitor a **single session** instead, pass the file when you start it:

```bash
claude --settings /path/to/your-hooks.json
```

Optional environment variables for the hook:

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MONITOR_URL` | `http://127.0.0.1:8765/events` | Where to send events |
| `AGENT_MONITOR_HOOK_TIMEOUT` | `2` | Seconds before giving up |
| `AGENT_MONITOR_HOOK_LOG` | unset | File to append errors to, for troubleshooting |
