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

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response
from starlette.types import Scope

from agent_monitor import __version__
from agent_monitor.broadcaster import Broadcaster
from agent_monitor.config import Settings
from agent_monitor.models import HookEvent, StoredEvent
from agent_monitor.sse import ServerSentEvent, event_stream
from agent_monitor.status import AgentSummary, build_summary
from agent_monitor.storage import EventStore
from agent_monitor.t3code import T3CodeReader

STREAM_EVENT_NAME = "agent_event"

# The dashboard: plain HTML/CSS/JS shipped inside the package
# (see [tool.setuptools.package-data] in pyproject.toml).
STATIC_DIR = Path(__file__).parent / "static"

# Browsers may cache index.html and app.js separately on their own
# heuristics; after an upgrade they could run new JS against an old page.
# "no-cache" makes them revalidate on every load, which is still cheap: an
# unchanged file comes back as a 304 thanks to its ETag.
DASHBOARD_CACHE_HEADERS = {"Cache-Control": "no-cache"}


class DashboardFiles(StaticFiles):
    """StaticFiles whose responses are always revalidated."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(DASHBOARD_CACHE_HEADERS)
        return response


class StreamMessage(BaseModel):
    """Payload of every SSE message: the new event and its session's new state."""

    event: StoredEvent
    agent: AgentSummary


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = EventStore(settings.db_path)
    broadcaster = Broadcaster()
    stale_after = timedelta(seconds=settings.stale_after_seconds)

    def summarize(session_id: str) -> AgentSummary:
        events = store.events_for_session(session_id)
        return build_summary(session_id, events, datetime.now(UTC), stale_after)

    def publish_stored(stored: StoredEvent, agent: AgentSummary) -> None:
        message = StreamMessage(event=stored, agent=agent)
        broadcaster.publish(
            ServerSentEvent(
                data=message.model_dump_json(),
                event=STREAM_EVENT_NAME,
                id=str(stored.id),
            )
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not settings.t3_enabled:
            yield
            return

        reader = T3CodeReader(settings.t3_db_path)
        await run_in_threadpool(reader.poll_once, store)

        async def poll_t3code() -> None:
            while True:
                await asyncio.sleep(settings.t3_poll_interval_seconds)
                stored_events = await run_in_threadpool(reader.poll_once, store)
                if not stored_events:
                    continue
                summaries: dict[str, AgentSummary] = {}
                for stored in stored_events:
                    agent = summaries.get(stored.session_id)
                    if agent is None:
                        agent = await run_in_threadpool(summarize, stored.session_id)
                        summaries[stored.session_id] = agent
                    publish_stored(stored, agent)

        task = asyncio.create_task(poll_t3code())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="agent-monitor", version=__version__, lifespan=lifespan)
    app.state.broadcaster = broadcaster  # exposed for tests
    app.mount("/static", DashboardFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=DASHBOARD_CACHE_HEADERS)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", status_code=status.HTTP_201_CREATED, response_model=StoredEvent)
    async def post_event(
        event: HookEvent,
        x_t3code_thread_id: str | None = Header(default=None),
        x_agent_monitor_source: str | None = Header(default=None),
    ) -> StoredEvent | Response:
        """Store one hook event and push it to every stream client.

        Invalid bodies get FastAPI's automatic 422.
        """
        if settings.t3_enabled and x_t3code_thread_id and settings.t3_db_path.expanduser().exists():
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        if x_agent_monitor_source == "claude-code" and not event.payload().get("provider"):
            event = HookEvent.model_validate({**event.payload(), "provider": "claude"})
        stored = await run_in_threadpool(store.add, event)
        agent = await run_in_threadpool(summarize, stored.session_id)
        publish_stored(stored, agent)
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
