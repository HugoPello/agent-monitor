"""Tests for scripts/demo.py: the scenario must drive the monitor to a sensible end state."""

import importlib.util
import json
import socket
import urllib.request
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import RunningServer

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo.py"


@pytest.fixture
def demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_ends_with_every_session_at_rest(
    demo: ModuleType, running_server: RunningServer
) -> None:
    exit_code = demo.main(["--url", f"{running_server.base_url}/events", "--speed", "0"])

    with urllib.request.urlopen(f"{running_server.base_url}/agents") as response:
        agents = {Path(a["cwd"]).name: a for a in json.loads(response.read())}

    assert exit_code == 0
    assert {name: a["activity"] for name, a in agents.items()} == {
        "weather-app": "finished",
        "portfolio-site": "finished",
        "notes-cli": "ended",
    }
    weather = agents["weather-app"]
    assert sorted((s["agent_type"], s["status"]) for s in weather["subagents"]) == [
        ("Explore", "done"),
        ("Explore", "done"),
        ("general-purpose", "done"),
    ]
    # The test writer's failed run stays visible for the rest of the turn.
    assert weather["failed_steps"] == 1
    assert weather["last_failure_agent"] == "general-purpose"
    assert weather["last_failure_tool"] == "Bash"
    assert agents["portfolio-site"]["failed_steps"] == 0


def test_demo_reports_unreachable_server(
    demo: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    exit_code = demo.main(["--url", f"http://127.0.0.1:{port}/events", "--speed", "0"])

    assert exit_code == 1
    assert "Could not reach agent-monitor" in capsys.readouterr().err
