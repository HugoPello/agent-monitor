"""Run the server: ``python -m agent_monitor`` (or the ``agent-monitor`` script)."""

from agent_monitor.config import Settings
from agent_monitor.server import build_server


def main() -> None:
    build_server(Settings()).run()


if __name__ == "__main__":
    main()
