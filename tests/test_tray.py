"""Tests for the tray launcher's logic. The tray icon itself is not tested."""

import socket
import urllib.request
from pathlib import Path

import pytest

from agent_monitor.config import Settings
from agent_monitor.tray import (
    Debounce,
    ServerThread,
    already_running,
    dashboard_url,
    draw_icon,
    main,
    redirect_output,
)
from tests.conftest import RunningServer


def make_settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "events.db")


def test_server_thread_starts_serves_and_stops(tmp_path: Path) -> None:
    server = ServerThread(make_settings(tmp_path), port=0, log_level="warning")

    server.start()
    url = dashboard_url("127.0.0.1", server.port)
    with urllib.request.urlopen(f"{url}health", timeout=5) as response:
        assert response.status == 200
    server.stop()

    assert not server.running


def test_server_thread_reports_a_port_in_use(tmp_path: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        server = ServerThread(make_settings(tmp_path), port=port, log_level="critical")

        with pytest.raises(RuntimeError, match="port in use"):
            server.start()


def test_already_running_finds_a_monitor(running_server: RunningServer) -> None:
    assert already_running(f"{running_server.base_url}/")


def test_already_running_is_false_when_nothing_listens() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # free once the socket closes

    assert not already_running(dashboard_url("127.0.0.1", port), timeout=0.5)


def test_redirect_output_keeps_a_real_console(tmp_path: Path) -> None:
    assert redirect_output(tmp_path) is None  # pytest's stderr is not None
    assert not (tmp_path / "agent-monitor-tray.log").exists()


def test_redirect_output_writes_to_a_log_without_a_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)

    log = redirect_output(tmp_path / "data")
    assert log is not None
    try:
        print("hello")
    finally:
        log.close()

    assert (tmp_path / "data" / "agent-monitor-tray.log").read_text(encoding="utf-8") == "hello\n"


def test_draw_icon_is_a_transparent_square() -> None:
    pytest.importorskip("PIL")

    icon = draw_icon(64)

    assert icon.size == (64, 64)
    assert icon.mode == "RGBA"
    assert icon.getpixel((0, 0))[3] == 0  # transparent corner
    assert icon.getpixel((48, 13))[3] == 255  # a subagent dot


def test_write_icon_saves_an_ico_with_bmp_entries(tmp_path: Path) -> None:
    PIL = pytest.importorskip("PIL.Image")
    path = tmp_path / "icons" / "agent-monitor.ico"

    assert main(["--write-icon", str(path)]) == 0

    data = path.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"  # ICO header
    assert b"\x89PNG" not in data  # every size stored as a bitmap
    with PIL.open(path) as ico:
        assert (32, 32) in ico.info["sizes"]


def test_debounce_lets_one_call_through_per_window() -> None:
    now = [100.0]
    debounce = Debounce(1.0, clock=lambda: now[0])

    first = debounce()
    now[0] += 0.3  # second click of a double-click
    second = debounce()
    now[0] += 1.0
    later = debounce()

    assert (first, second, later) == (True, False, True)
