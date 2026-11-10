"""Integration test: a real server with a real SSE client.

Slower than the other tests (about a second), but it is the only way to
check that an open stream does not block shutdown.
"""

import json
import threading
import time
import urllib.request

from agent_monitor.server import GRACEFUL_SHUTDOWN_SECONDS
from tests.conftest import RunningServer


def test_stream_receives_events_and_does_not_block_shutdown(
    running_server: RunningServer,
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
