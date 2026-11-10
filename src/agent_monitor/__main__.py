"""Run the server: ``python -m agent_monitor`` (or the ``agent-monitor`` script)."""

import uvicorn

from agent_monitor.api import create_app
from agent_monitor.config import Settings


def main() -> None:
    settings = Settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
