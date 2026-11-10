"""Integration test: a real server with a real SSE client.

Slower than the other tests (about a second), but it is the only way to
check that an open stream does not block shutdown.
"""

import json
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_monitor.config import Settings
from agent_monitor.server import GRACEFUL_SHUTDOWN_SECONDS, MonitorServer, build_server


@pytest.fixture
def running_server(tmp_path: Path) -> Iterator[tuple[MonitorServer, threading.Thread, str]]:
    settings = Settings(_env_file=None, db_path=tmp_path / "events.db")
    server = build_server(settings, port=0, log_level="warning")  # port 0: OS picks a free one
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield server, thread, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_stream_receives_events_and_does_not_block_shutdown(
    running_server: tuple[MonitorServer, threading.Thread, str],
) -> None:
    server, thread, base_url = running_server
    lines: list[str] = []
    client_done = threading.Event()

    def read_stream() -> None:
        with urllib.request.urlopen(f"{base_url}/stream") as response:
            for raw in response:
                lines.append(raw.decode())
        client_done.set()  # the server ended the response cleanly

    threading.Thread(target=read_stream, daemon=True).start()
    while not lines:  # wait for the "retry:" line
        time.sleep(0.02)

    body = json.dumps({"session_id": "s1", "hook_event_name": "Stop"}).encode()
    request = urllib.request.Request(
        f"{base_url}/events", body, {"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request).read()
    while not any(line.startswith("data:") for line in lines):
        time.sleep(0.02)

    # What uvicorn's Ctrl+C handler does:
    started = time.monotonic()
    server.should_exit = True
    thread.join(timeout=GRACEFUL_SHUTDOWN_SECONDS + 5)

    assert not thread.is_alive()
    assert time.monotonic() - started < GRACEFUL_SHUTDOWN_SECONDS  # no forced cancel
    assert client_done.wait(timeout=1)
    assert "event: agent_event\n" in lines
