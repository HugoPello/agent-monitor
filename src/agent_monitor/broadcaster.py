"""In-memory fan-out of messages to every connected SSE client.

Each client gets its own bounded ``asyncio.Queue``. ``publish`` never waits:
if a client stops reading and its queue fills up, that client is dropped
instead of slowing everyone else down. Its browser reconnects and reloads
the current state from ``GET /agents``.

``asyncio.Queue`` is not thread-safe, so every method must be called from
the event loop (i.e. from ``async`` code).
"""

import asyncio

from agent_monitor.sse import ServerSentEvent

# ``None`` is the "you have been dropped, close the stream" signal.
type Message = ServerSentEvent | None


class Broadcaster:
    def __init__(self, max_queue_size: int = 100) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: set[asyncio.Queue[Message]] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue[Message]:
        queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Message]) -> None:
        self._subscribers.discard(queue)

    def publish(self, message: ServerSentEvent) -> None:
        # Iterate over a copy: we may remove subscribers inside the loop.
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self._drop(queue)

    def close(self) -> None:
        """Disconnect every subscriber (used on server shutdown)."""
        for queue in list(self._subscribers):
            self._drop(queue)

    def _drop(self, queue: asyncio.Queue[Message]) -> None:
        self._subscribers.discard(queue)
        # Discard the backlog so there is room for the close signal.
        while not queue.empty():
            queue.get_nowait()
        queue.put_nowait(None)
