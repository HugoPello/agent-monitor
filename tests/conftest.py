"""Shared pytest configuration and fixtures."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from agent_monitor.config import Settings
from agent_monitor.server import MonitorServer, build_server


@pytest.fixture
def anyio_backend() -> str:
    # Async tests (marked with @pytest.mark.anyio) run on asyncio only.
    return "asyncio"


class RunningServer(NamedTuple):
    server: MonitorServer
    thread: threading.Thread
    base_url: str


@pytest.fixture
def running_server(tmp_path: Path) -> Iterator[RunningServer]:
    """A real uvicorn server in a background thread, on a free port."""
    settings = Settings(_env_file=None, db_path=tmp_path / "events.db")
    server = build_server(settings, port=0, log_level="warning")  # port 0: OS picks a free one
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield RunningServer(server, thread, f"http://127.0.0.1:{port}")
    server.should_exit = True
    thread.join(timeout=10)
