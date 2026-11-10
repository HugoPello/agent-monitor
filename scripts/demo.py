#!/usr/bin/env python3
"""Replay two fictional Claude Code sessions against a running agent-monitor.

Lets anyone see the dashboard working without setting up Claude Code, and
produces clean data for screenshots. The projects, prompts and paths are
made up.

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


def build_scenario() -> list[tuple[float, dict, dict]]:
    """Steps as (pause before, project, event fields), in order."""
    w, p = WEATHER, PORTFOLIO
    explore = {"agent_id": "agent-" + uuid.uuid4().hex[:12], "agent_type": "Explore"}

    def tool(name: str, **tool_input: str) -> dict:
        return {"tool_name": name, "tool_input": tool_input, "tool_use_id": uuid.uuid4().hex}

    return [
        (0.0, w, {"hook_event_name": "SessionStart", "source": "startup"}),
        (
            1.0,
            w,
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Add a 5-day forecast endpoint with tests",
            },
        ),
        (1.0, w, {"hook_event_name": "PreToolUse", **tool("Read", file_path="src/api/routes.py")}),
        (0.6, w, {"hook_event_name": "PostToolUse", "tool_name": "Read"}),
        (
            0.8,
            w,
            {
                "hook_event_name": "PreToolUse",
                **tool("Agent", description="Find existing forecast helpers"),
            },
        ),
        (0.4, w, {"hook_event_name": "SubagentStart", **explore}),
        (0.8, w, {"hook_event_name": "PreToolUse", **explore, **tool("Grep", pattern="forecast")}),
        (0.5, p, {"hook_event_name": "SessionStart", "source": "startup"}),
        (
            0.8,
            p,
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "Fix the contact form layout on mobile",
            },
        ),
        (0.5, w, {"hook_event_name": "PostToolUse", **explore, "tool_name": "Grep"}),
        (
            0.6,
            p,
            {
                "hook_event_name": "PreToolUse",
                **tool("Read", file_path="src/components/ContactForm.tsx"),
            },
        ),
        (
            0.6,
            w,
            {
                "hook_event_name": "PreToolUse",
                **explore,
                **tool("Read", file_path="src/services/weather.py"),
            },
        ),
        (0.6, w, {"hook_event_name": "PostToolUse", **explore, "tool_name": "Read"}),
        (
            0.8,
            w,
            {
                "hook_event_name": "SubagentStop",
                **explore,
                "last_assistant_message": "get_forecast() already exists in services/weather.py",
            },
        ),
        (0.4, w, {"hook_event_name": "PostToolUse", "tool_name": "Agent"}),
        (0.6, p, {"hook_event_name": "PostToolUse", "tool_name": "Read"}),
        (
            0.8,
            w,
            {"hook_event_name": "PreToolUse", **tool("Edit", file_path="src/api/routes.py")},
        ),
        (
            0.6,
            p,
            {
                "hook_event_name": "PreToolUse",
                **tool("Edit", file_path="src/components/ContactForm.module.css"),
            },
        ),
        (0.6, w, {"hook_event_name": "PostToolUse", "tool_name": "Edit"}),
        (0.5, p, {"hook_event_name": "PostToolUse", "tool_name": "Edit"}),
        (
            0.8,
            w,
            {
                "hook_event_name": "PreToolUse",
                **tool("Bash", command="pytest tests/test_forecast.py -q"),
            },
        ),
        (
            0.5,
            w,
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "Claude needs your permission to use Bash",
            },
        ),
        (
            1.0,
            p,
            {"hook_event_name": "PreToolUse", **tool("Bash", command="npm test -- ContactForm")},
        ),
        (2.0, p, {"hook_event_name": "PostToolUse", "tool_name": "Bash"}),
        (
            0.8,
            p,
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "The form now stacks its fields below 480px. Tests pass.",
            },
        ),
        (1.5, w, {"hook_event_name": "PostToolUse", "tool_name": "Bash"}),
        (
            0.8,
            w,
            {
                "hook_event_name": "Stop",
                "last_assistant_message": "Added GET /forecast?days=5 with 4 tests, all passing.",
            },
        ),
    ]


def post(url: str, event: dict) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def run(url: str, speed: float) -> None:
    session_ids = {WEATHER["name"]: str(uuid.uuid4()), PORTFOLIO["name"]: str(uuid.uuid4())}
    for pause, project, fields in build_scenario():
        if speed > 0:
            time.sleep(pause / speed)
        event = {
            "session_id": session_ids[project["name"]],
            "cwd": project["cwd"],
            "transcript_path": f"~/.claude/projects/demo/{session_ids[project['name']]}.jsonl",
            **fields,
        }
        post(url, event)
        who = f"{project['name']}" + (f" [{fields['agent_type']}]" if "agent_id" in fields else "")
        print(f"{who:<26} {fields['hook_event_name']:<18} {fields.get('tool_name', '')}")


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
