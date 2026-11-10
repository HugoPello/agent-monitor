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


def test_demo_creates_two_finished_sessions_with_a_subagent(
    demo: ModuleType, running_server: RunningServer
) -> None:
    exit_code = demo.main(["--url", f"{running_server.base_url}/events", "--speed", "0"])

    with urllib.request.urlopen(f"{running_server.base_url}/agents") as response:
        agents = json.loads(response.read())

    assert exit_code == 0
    assert sorted(Path(a["cwd"]).name for a in agents) == ["portfolio-site", "weather-app"]
    assert all(a["status"] == "idle" for a in agents)  # both end with Stop
    [weather] = [a for a in agents if a["cwd"].endswith("weather-app")]
    assert [(s["agent_type"], s["status"]) for s in weather["subagents"]] == [("Explore", "done")]


def test_demo_reports_unreachable_server(
    demo: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    exit_code = demo.main(["--url", f"http://127.0.0.1:{port}/events", "--speed", "0"])

    assert exit_code == 1
    assert "Could not reach agent-monitor" in capsys.readouterr().err
