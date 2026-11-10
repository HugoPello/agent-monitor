"""SQLite persistence for hook events.

Uses the standard library ``sqlite3`` module directly: with a single table an
ORM would add more concepts than it removes, and plain SQL is easy to read.

A new connection is opened for every operation. FastAPI runs sync endpoints
in a thread pool and a ``sqlite3`` connection must not be shared between
threads; opening a SQLite connection is cheap, so this is the simplest safe
option.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from agent_monitor.models import HookEvent, StoredEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    agent_id        TEXT,
    hook_event_name TEXT    NOT NULL,
    tool_name       TEXT,
    payload         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, id);
"""


class EventStore:
    """Stores hook events and reads them back per session."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            # WAL lets readers (the dashboard) work while a hook is writing.
            # The setting is stored in the database file, so once is enough.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # `with conn` commits on success and rolls back on error,
            # but it does NOT close the connection; `finally` does that.
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, event: HookEvent, received_at: datetime | None = None) -> StoredEvent:
        """Persist ``event`` and return it with its id and timestamp."""
        received_at = received_at or datetime.now(UTC)
        payload = event.payload()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO events
                    (received_at, session_id, agent_id, hook_event_name, tool_name, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    received_at.isoformat(),
                    event.session_id,
                    event.agent_id,
                    event.hook_event_name,
                    event.tool_name,
                    json.dumps(payload),
                ),
            )
            event_id = cursor.lastrowid

        assert event_id is not None  # always set after a successful INSERT
        return StoredEvent(
            id=event_id,
            received_at=received_at,
            session_id=event.session_id,
            agent_id=event.agent_id,
            hook_event_name=event.hook_event_name,
            tool_name=event.tool_name,
            payload=payload,
        )

    def events_for_session(self, session_id: str) -> list[StoredEvent]:
        """All events of a session, oldest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def session_ids(self) -> list[str]:
        """Every session id, most recently active first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id FROM events GROUP BY session_id ORDER BY MAX(id) DESC"
            ).fetchall()
        return [row["session_id"] for row in rows]


def _row_to_event(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        id=row["id"],
        received_at=datetime.fromisoformat(row["received_at"]),
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        hook_event_name=row["hook_event_name"],
        tool_name=row["tool_name"],
        payload=json.loads(row["payload"]),
    )
