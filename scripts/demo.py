#!/usr/bin/env python3
"""Replay three fictional Claude Code sessions against a running agent-monitor.

Lets anyone see the dashboard working without setting up Claude Code, and
produces clean data for screenshots and the README's GIF. The projects,
prompts and paths are made up.

What it shows, in about 40 seconds at normal speed:

- weather-app delegates to three subagents that work in parallel. One of
  them fails a test run, fixes the test and retries. Then the main agent
  needs a permission before it can finish.
- portfolio-site works through a small fix on its own and finishes first.
- notes-cli has already ended, so the "At rest" list is not empty.

    python scripts/demo.py                 # real-time-ish pacing
    python scripts/demo.py --speed 3       # three times faster
    python scripts/demo.py --speed 0       # no pauses (used by the tests)

Standard library only.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://127.0.0.1:8765/events"

WEATHER = {"name": "weather-app", "cwd": "/home/dev/projects/weather-app"}
PORTFOLIO = {"name": "portfolio-site", "cwd": "/home/dev/projects/portfolio-site"}
NOTES = {"name": "notes-cli", "cwd": "/home/dev/projects/notes-cli"}
PROJECTS = (WEATHER, PORTFOLIO, NOTES)

Step = tuple[float, dict, dict]  # (pause before, project, event fields)


# ---- Event builders. ``agent`` is a subagent; without it, the main agent. ----


def subagent(agent_type: str) -> dict:
    return {"agent_id": "agent-" + uuid.uuid4().hex[:12], "agent_type": agent_type}


def pre(tool: str, agent: dict | None = None, **tool_input: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        **(agent or {}),
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": uuid.uuid4().hex,
    }


def post(tool: str, agent: dict | None = None) -> dict:
    return {"hook_event_name": "PostToolUse", **(agent or {}), "tool_name": tool}


def failure(tool: str, agent: dict | None = None, *, error: str, **tool_input: str) -> dict:
    return {
        "hook_event_name": "PostToolUseFailure",
        **(agent or {}),
        "tool_name": tool,
        "tool_input": tool_input,
        "error": error,
    }


def prompt(text: str) -> dict:
    return {"hook_event_name": "UserPromptSubmit", "prompt": text}


def stop(message: str, agent: dict | None = None) -> dict:
    name = "SubagentStop" if agent else "Stop"
    return {"hook_event_name": name, **(agent or {}), "last_assistant_message": message}


def build_scenario() -> list[Step]:
    """Steps in the order they are sent."""
    w, p, n = WEATHER, PORTFOLIO, NOTES
    helpers = subagent("Explore")
    docs = subagent("Explore")
    tests = subagent("general-purpose")
    start = {"hook_event_name": "SessionStart", "source": "startup"}
    pytest_forecast = "pytest tests/test_forecast.py -q"

    return [
        # notes-cli: an earlier session, already over.
        (0.0, n, start),
        (0.0, n, prompt("Add a --json flag to the list command")),
        (0.0, n, stop("Added --json to `notes list`, with a test.")),
        (0.0, n, {"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}),
        # Two sessions start working.
        (0.5, w, start),
        (0.8, w, prompt("Add a 5-day forecast endpoint with tests")),
        (0.6, p, start),
        (0.6, p, prompt("Fix the contact form layout on mobile")),
        (0.6, w, pre("Read", file_path="src/api/routes.py")),
        (0.5, p, pre("Read", file_path="src/components/ContactForm.tsx")),
        (1.0, w, post("Read")),
        # weather-app delegates three jobs that run in parallel.
        (0.6, w, pre("Agent", description="Find existing forecast helpers")),
        (0.3, w, {"hook_event_name": "SubagentStart", **helpers}),
        (0.6, w, pre("Agent", description="Check the provider's daily forecast API")),
        (0.3, w, {"hook_event_name": "SubagentStart", **docs}),
        (0.6, w, pre("Agent", description="Write tests for GET /forecast")),
        (0.3, w, {"hook_event_name": "SubagentStart", **tests}),
        (0.5, p, post("Read")),
        (0.4, w, pre("Grep", helpers, pattern="forecast")),
        (0.4, w, pre("WebFetch", docs, url="https://api.example.com/docs/forecast")),
        (0.4, w, pre("Read", tests, file_path="tests/conftest.py")),
        (0.6, p, pre("Edit", file_path="src/components/ContactForm.module.css")),
        (1.0, w, post("Grep", helpers)),
        (0.4, w, pre("Read", helpers, file_path="src/services/weather.py")),
        (0.6, w, post("Read", tests)),
        (0.4, w, pre("Write", tests, file_path="tests/test_forecast.py")),
        (0.8, w, post("WebFetch", docs)),
        (1.0, w, post("Read", helpers)),
        (0.6, w, stop("get_forecast() already exists in services/weather.py", helpers)),
        (0.6, p, post("Edit")),
        (0.5, p, pre("Bash", command="npm test -- ContactForm")),
        (1.0, w, post("Write", tests)),
        (0.4, w, pre("Bash", tests, command=pytest_forecast)),
        # The test writer's first run fails; it fixes the test and retries.
        (
            1.2,
            w,
            failure(
                "Bash",
                tests,
                command=pytest_forecast,
                error="1 failed: KeyError 'daily' in test_five_days",
            ),
        ),
        (0.8, w, stop("The API returns `daily`, capped at 7 days.", docs)),
        (0.6, w, pre("Edit", tests, file_path="tests/test_forecast.py")),
        (0.8, w, post("Edit", tests)),
        (0.4, w, pre("Bash", tests, command=pytest_forecast)),
        (1.2, p, post("Bash")),
        (0.6, p, stop("The form now stacks its fields below 480px. Tests pass.")),
        (0.8, w, post("Bash", tests)),
        (0.6, w, stop("4 tests for GET /forecast, all passing.", tests)),
        # The three parallel Agent calls return together.
        (0.4, w, post("Agent")),
        (0.1, w, post("Agent")),
        (0.1, w, post("Agent")),
        # weather-app wires it up and needs a permission to run the suite.
        (0.8, w, pre("Edit", file_path="src/api/routes.py")),
        (1.0, w, post("Edit")),
        (0.6, w, pre("Bash", command="pytest -q")),
        (
            0.5,
            w,
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission to use Bash",
            },
        ),
        (3.5, w, post("Bash")),
        (0.8, w, stop("Added GET /forecast?days=5 with 4 tests, all passing.")),
    ]


def send(url: str, event: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def run(url: str, speed: float) -> None:
    session_ids = {project["name"]: str(uuid.uuid4()) for project in PROJECTS}
    for pause, project, fields in build_scenario():
        if speed > 0:
            time.sleep(pause / speed)
        event = {
            "session_id": session_ids[project["name"]],
            "cwd": project["cwd"],
            "transcript_path": f"~/.claude/projects/demo/{session_ids[project['name']]}.jsonl",
            **fields,
        }
        send(url, event)
        who = f"{project['name']}" + (f" [{fields['agent_type']}]" if "agent_id" in fields else "")
        print(f"{who:<32} {fields['hook_event_name']:<18} {fields.get('tool_name', '')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"events endpoint (default {DEFAULT_URL})"
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="pacing multiplier; 0 disables pauses"
    )
    args = parser.parse_args(argv)
    try:
        run(args.url, args.speed)
    except urllib.error.URLError as exc:
        print(f"Could not reach agent-monitor at {args.url}: {exc}", file=sys.stderr)
        print("Start it first with: python -m agent_monitor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
