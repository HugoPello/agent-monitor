"""Server-Sent Events: message format and the per-client stream.

An SSE response is a long-lived HTTP response with
``Content-Type: text/event-stream``. The server keeps writing text blocks
separated by a blank line::

    id: 42
    event: agent_event
    data: {"some": "json"}

The browser's ``EventSource`` turns each block into a JS event and
reconnects automatically if the connection drops.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_monitor.broadcaster import Broadcaster

PING_INTERVAL_SECONDS = 15.0
RETRY_MILLISECONDS = 3000

# Lines starting with ":" are comments: EventSource ignores them, but they
# keep proxies from closing an idle connection and reveal dead clients.
PING = ": ping\n\n"


@dataclass(frozen=True)
class ServerSentEvent:
    data: str
    event: str | None = None
    id: str | None = None

    def encode(self) -> str:
        lines = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        if self.event is not None:
            lines.append(f"event: {self.event}")
        # Each line of data needs its own "data:" prefix; the browser joins
        # them back with "\n".
        lines.extend(f"data: {line}" for line in self.data.splitlines() or [""])
        return "\n".join(lines) + "\n\n"


async def event_stream(
    broadcaster: "Broadcaster",
    ping_interval: float = PING_INTERVAL_SECONDS,
    retry_ms: int = RETRY_MILLISECONDS,
) -> AsyncIterator[str]:
    """Yield SSE text for one client until it disconnects or is dropped."""
    queue = broadcaster.subscribe()
    try:
        # Tell the browser how long to wait before reconnecting.
        yield f"retry: {retry_ms}\n\n"
        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=ping_interval)
            except TimeoutError:
                yield PING
                continue
            if message is None:  # dropped by the broadcaster: end the response
                return
            yield message.encode()
    finally:
        # Runs when the client disconnects (Starlette cancels the generator)
        # or the server shuts down, so closed tabs never leak queues.
        broadcaster.unsubscribe(queue)
