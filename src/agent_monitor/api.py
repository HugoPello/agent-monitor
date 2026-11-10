"""HTTP API.

``create_app`` builds a new FastAPI application around its own
``EventStore`` and ``Broadcaster``. Using a factory instead of a
module-level ``app`` lets every test create an isolated app with a
temporary database.

Sync vs async endpoints: ``sqlite3`` is blocking, and blocking calls inside
``async def`` would freeze the event loop and every other request.

- Read endpoints are plain ``def``: FastAPI runs them in a thread pool.
- ``POST /events`` is ``async def`` because it publishes to the
  broadcaster, whose ``asyncio.Queue``s are not thread-safe and must be used
  from the event loop. Its SQLite work is sent to the thread pool explicitly
  with ``run_in_threadpool``.
"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent_monitor import __version__
from agent_monitor.broadcaster import Broadcaster
from agent_monitor.config import Settings
from agent_monitor.models import HookEvent, StoredEvent
from agent_monitor.sse import ServerSentEvent, event_stream
from agent_monitor.status import AgentSummary, build_summary
from agent_monitor.storage import EventStore

STREAM_EVENT_NAME = "agent_event"


class StreamMessage(BaseModel):
    """Payload of every SSE message: the new event and its session's new state."""

    event: StoredEvent
    agent: AgentSummary


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = EventStore(settings.db_path)
    broadcaster = Broadcaster()
    stale_after = timedelta(seconds=settings.stale_after_seconds)

    app = FastAPI(title="agent-monitor", version=__version__)
    app.state.broadcaster = broadcaster  # exposed for tests

    def summarize(session_id: str) -> AgentSummary:
        events = store.events_for_session(session_id)
        return build_summary(session_id, events, datetime.now(UTC), stale_after)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", status_code=status.HTTP_201_CREATED)
    async def post_event(event: HookEvent) -> StoredEvent:
        """Store one hook event and push it to every stream client.

        Invalid bodies get FastAPI's automatic 422.
        """
        stored = await run_in_threadpool(store.add, event)
        agent = await run_in_threadpool(summarize, stored.session_id)
        message = StreamMessage(event=stored, agent=agent)
        broadcaster.publish(
            ServerSentEvent(
                data=message.model_dump_json(),
                event=STREAM_EVENT_NAME,
                id=str(stored.id),
            )
        )
        return stored

    @app.get("/agents")
    def list_agents() -> list[AgentSummary]:
        """Current state of every session, most recently active first."""
        return [summarize(session_id) for session_id in store.session_ids()]

    @app.get("/agents/{session_id}/events")
    def agent_events(session_id: str) -> list[StoredEvent]:
        """Timeline of one session, oldest first."""
        events = store.events_for_session(session_id)
        if not events:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown session: {session_id}")
        return events

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        """Live feed of new events as Server-Sent Events."""
        return StreamingResponse(
            event_stream(broadcaster),
            media_type="text/event-stream",
            # Don't let browsers or proxies cache or buffer the stream.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
