"""Tests for Settings."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_monitor.config import Settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Make sure variables from the developer's shell don't leak into tests.
    for name in ["HOST", "PORT", "DB_PATH", "STALE_AFTER_SECONDS"]:
        monkeypatch.delenv(f"AGENT_MONITOR_{name}", raising=False)


def test_defaults() -> None:
    # _env_file=None ignores any local .env file.
    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.db_path == Path("data/agent_monitor.db")
    assert settings.stale_after_seconds == 600


def test_reads_prefixed_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_MONITOR_PORT", "9000")
    monkeypatch.setenv("AGENT_MONITOR_DB_PATH", "/tmp/x.db")

    settings = Settings(_env_file=None)

    assert settings.port == 9000
    assert settings.db_path == Path("/tmp/x.db")


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_MONITOR_STALE_AFTER_SECONDS=30\n", encoding="utf-8")

    assert Settings(_env_file=env_file).stale_after_seconds == 30


@pytest.mark.parametrize("port", ["abc", "0", "70000"])
def test_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("AGENT_MONITOR_PORT", port)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
