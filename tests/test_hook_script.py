"""Tests for hooks/send_event.py.

The script is run as a real subprocess, exactly as Claude Code runs it: event
JSON on stdin, configuration through environment variables. What matters
most is what it must never do: exit non-zero, print anything, or hang.
"""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import RunningServer

SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "send_event.py"
EVENT = {"session_id": "hook-test", "hook_event_name": "Stop", "cwd": "/project"}


def run_hook(stdin: bytes, **env: str) -> tuple[subprocess.CompletedProcess[bytes], float]:
    full_env = {k: v for k, v in os.environ.items() if not k.startswith("AGENT_MONITOR_HOOK_")}
    full_env.update(env)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        env=full_env,
        capture_output=True,
        timeout=30,
    )
    return result, time.monotonic() - started


def assert_silent_success(result: subprocess.CompletedProcess[bytes]) -> None:
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""


def session_events(base_url: str, session_id: str) -> list[dict]:
    with urllib.request.urlopen(f"{base_url}/agents/{session_id}/events") as response:
        return json.loads(response.read())


@pytest.fixture
def closed_port_url() -> str:
    """URL of a port nobody listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/events"


@pytest.fixture
def silent_server_url() -> Iterator[str]:
    """A server that accepts connections but never answers."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()  # the OS completes the handshake; we never read or reply
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/events"


def test_forwards_event_to_server(running_server: RunningServer) -> None:
    url = f"{running_server.base_url}/events"

    result, _ = run_hook(json.dumps(EVENT).encode(), AGENT_MONITOR_URL=url)

    assert_silent_success(result)
    [stored] = session_events(running_server.base_url, "hook-test")
    assert stored["hook_event_name"] == "Stop"
    assert stored["payload"]["cwd"] == "/project"


def test_ignores_proxy_environment_variables(running_server: RunningServer) -> None:
    # A proxy on a closed port: if the script used it, the event would be lost.
    dead_proxy = "http://127.0.0.1:9"
    url = f"{running_server.base_url}/events"

    result, _ = run_hook(
        json.dumps(EVENT).encode(),
        AGENT_MONITOR_URL=url,
        HTTP_PROXY=dead_proxy,
        http_proxy=dead_proxy,
        NO_PROXY="",
        no_proxy="",
    )

    assert_silent_success(result)
    assert len(session_events(running_server.base_url, "hook-test")) == 1


def test_server_down_exits_quickly_and_silently(closed_port_url: str) -> None:
    result, elapsed = run_hook(json.dumps(EVENT).encode(), AGENT_MONITOR_URL=closed_port_url)

    assert_silent_success(result)
    assert elapsed < 5


def test_unresponsive_server_is_cut_off_by_timeout(silent_server_url: str) -> None:
    result, elapsed = run_hook(
        json.dumps(EVENT).encode(),
        AGENT_MONITOR_URL=silent_server_url,
        AGENT_MONITOR_HOOK_TIMEOUT="0.5",
    )

    assert_silent_success(result)
    assert elapsed < 5  # without a timeout this would hang forever


def test_invalid_json_is_rejected_by_server_but_hook_stays_silent(
    running_server: RunningServer, tmp_path: Path
) -> None:
    log = tmp_path / "hook.log"

    result, _ = run_hook(
        b"not json",
        AGENT_MONITOR_URL=f"{running_server.base_url}/events",
        AGENT_MONITOR_HOOK_LOG=str(log),
    )

    assert_silent_success(result)
    assert "HTTPError" in log.read_text(encoding="utf-8")
    assert "422" in log.read_text(encoding="utf-8")


def test_empty_stdin_sends_nothing(closed_port_url: str, tmp_path: Path) -> None:
    log = tmp_path / "hook.log"

    result, _ = run_hook(b"", AGENT_MONITOR_URL=closed_port_url, AGENT_MONITOR_HOOK_LOG=str(log))

    assert_silent_success(result)
    assert not log.exists()  # no request was attempted, so nothing failed


def test_errors_are_logged_when_log_is_configured(closed_port_url: str, tmp_path: Path) -> None:
    log = tmp_path / "hook.log"

    run_hook(
        json.dumps(EVENT).encode(),
        AGENT_MONITOR_URL=closed_port_url,
        AGENT_MONITOR_HOOK_LOG=str(log),
    )

    assert "URLError" in log.read_text(encoding="utf-8")


def test_unwritable_log_path_does_not_break_the_hook(closed_port_url: str, tmp_path: Path) -> None:
    result, _ = run_hook(
        json.dumps(EVENT).encode(),
        AGENT_MONITOR_URL=closed_port_url,
        AGENT_MONITOR_HOOK_LOG=str(tmp_path / "missing-dir" / "hook.log"),
    )

    assert_silent_success(result)


@pytest.fixture
def hook_module() -> ModuleType:
    """Import the script as a module to unit-test its helpers."""
    spec = importlib.util.spec_from_file_location("send_event", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.5", 0.5), ("abc", 2.0), ("0", 2.0), ("-1", 2.0)],
)
def test_timeout_from_environment(
    hook_module: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str, expected: float
) -> None:
    monkeypatch.setenv("AGENT_MONITOR_HOOK_TIMEOUT", value)

    assert hook_module.get_timeout() == expected
