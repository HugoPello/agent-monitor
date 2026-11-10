"""Application settings, read from environment variables and ``.env``.

Every variable uses the ``AGENT_MONITOR_`` prefix, e.g. ``AGENT_MONITOR_PORT``.
pydantic-settings validates and converts the values, so a bad setting fails
at startup with a clear message instead of somewhere later.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_MONITOR_",
        env_file=".env",
        extra="ignore",
    )

    # 127.0.0.1 by default: payloads can contain code and secrets.
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    db_path: Path = Path("data/agent_monitor.db")

    # An agent that is "working" but silent for this long is shown as stale.
    stale_after_seconds: int = Field(default=600, gt=0)
