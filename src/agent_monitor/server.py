"""uvicorn server that shuts down cleanly with SSE clients connected.

On Ctrl+C, uvicorn first waits for every open response to finish and only
then runs the application's shutdown (lifespan) hooks. SSE responses never
finish on their own, so without help the server would wait forever (or,
with ``timeout_graceful_shutdown``, cancel them and log a traceback).

``MonitorServer`` closes all streams at the very start of ``shutdown``, so
the responses end normally and the server stops right away.
"""

import socket

import uvicorn
from fastapi import FastAPI

from agent_monitor.api import create_app
from agent_monitor.broadcaster import Broadcaster
from agent_monitor.config import Settings

# Safety net in case some other long request is still open.
GRACEFUL_SHUTDOWN_SECONDS = 5


class MonitorServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, broadcaster: Broadcaster) -> None:
        super().__init__(config)
        self._broadcaster = broadcaster

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        self._broadcaster.close()
        await super().shutdown(sockets)


def build_server(settings: Settings, **config_overrides: object) -> MonitorServer:
    """Build the server; ``config_overrides`` are extra ``uvicorn.Config`` options."""
    app: FastAPI = create_app(settings)
    options: dict[str, object] = {
        "host": settings.host,
        "port": settings.port,
        "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
        **config_overrides,  # later keys win
    }
    return MonitorServer(uvicorn.Config(app, **options), app.state.broadcaster)
