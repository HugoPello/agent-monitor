"""HTTP API.

``create_app`` builds a new FastAPI application around its own
``EventStore``. Using a factory instead of a module-level ``app`` lets every
test create an isolated app with a temporary database.

Endpoints are plain ``def`` functions, not ``async def``: ``sqlite3`` is
blocking, and FastAPI runs ``def`` endpoints in a thread pool. Blocking calls
inside ``async def`` would freeze the event loop and every other request.
"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, status

from agent_monitor import __version__
from agent_monitor.config import Settings
from agent_monitor.models import HookEvent, StoredEvent
from agent_monitor.status import AgentSummary, build_summary
from agent_monitor.storage import EventStore


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    store = EventStore(settings.db_path)
    stale_after = timedelta(seconds=settings.stale_after_seconds)

    app = FastAPI(title="agent-monitor", version=__version__)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/events", status_code=status.HTTP_201_CREATED)
    def post_event(event: HookEvent) -> StoredEvent:
        """Receive one hook event. Invalid bodies get FastAPI's automatic 422."""
        return store.add(event)

    @app.get("/agents")
    def list_agents() -> list[AgentSummary]:
        """Current state of every session, most recently active first."""
        now = datetime.now(UTC)
        return [
            build_summary(session_id, store.events_for_session(session_id), now, stale_after)
            for session_id in store.session_ids()
        ]

    @app.get("/agents/{session_id}/events")
    def agent_events(session_id: str) -> list[StoredEvent]:
        """Timeline of one session, oldest first."""
        events = store.events_for_session(session_id)
        if not events:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown session: {session_id}")
        return events

    return app
