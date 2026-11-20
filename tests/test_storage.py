"""Tests for the SQLite EventStore."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_monitor.models import HookEvent
from agent_monitor.storage import EventStore


@pytest.fixture
def store(tmp_path: Path) -> EventStore:
    # A real file, not ":memory:": the store opens a new connection per
    # operation, and an in-memory database would vanish after each one.
    return EventStore(tmp_path / "events.db")


def make_event(session_id: str = "s1", name: str = "PreToolUse", **extra: object) -> HookEvent:
    return HookEvent.model_validate({"session_id": session_id, "hook_event_name": name, **extra})


def test_creates_database_file_and_parent_dirs(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "events.db"

    EventStore(db_path)

    assert db_path.exists()


def test_reopening_existing_database_keeps_events(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    EventStore(db_path).add(make_event())

    assert len(EventStore(db_path).events_for_session("s1")) == 1


def test_add_returns_stored_event(store: EventStore) -> None:
    received_at = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    event = make_event(tool_name="Bash", tool_input={"command": "ls"})

    stored = store.add(event, received_at=received_at)

    assert stored.id > 0
    assert stored.received_at == received_at
    assert stored.hook_event_name == "PreToolUse"
    assert stored.tool_name == "Bash"


def test_round_trip_preserves_payload(store: EventStore) -> None:
    store.add(make_event(agent_id="a1", agent_type="Explore", tool_input={"command": "ls"}))

    [stored] = store.events_for_session("s1")

    assert stored.agent_id == "a1"
    assert stored.payload["agent_type"] == "Explore"
    assert stored.payload["tool_input"] == {"command": "ls"}


def test_received_at_defaults_to_now_in_utc(store: EventStore) -> None:
    before = datetime.now(UTC)
    store.add(make_event())
    after = datetime.now(UTC)

    [stored] = store.events_for_session("s1")

    assert before <= stored.received_at <= after
    assert stored.received_at.tzinfo is not None


def test_events_are_returned_in_insertion_order(store: EventStore) -> None:
    for name in ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]:
        store.add(make_event(name=name))

    names = [e.hook_event_name for e in store.events_for_session("s1")]

    assert names == ["SessionStart", "UserPromptSubmit", "PreToolUse", "Stop"]


def test_sessions_do_not_mix(store: EventStore) -> None:
    store.add(make_event(session_id="s1"))
    store.add(make_event(session_id="s2"))
    store.add(make_event(session_id="s1"))

    assert len(store.events_for_session("s1")) == 2
    assert len(store.events_for_session("s2")) == 1


def test_unknown_session_returns_empty_list(store: EventStore) -> None:
    assert store.events_for_session("nope") == []


def test_session_ids_most_recent_first(store: EventStore) -> None:
    store.add(make_event(session_id="old"))
    store.add(make_event(session_id="new"))
    store.add(make_event(session_id="old"))  # "old" becomes the most recent

    assert store.session_ids() == ["old", "new"]


def test_session_ids_empty_store(store: EventStore) -> None:
    assert store.session_ids() == []


def test_cursor_defaults_to_zero_and_empty_state(store: EventStore) -> None:
    assert store.get_cursor("t3code") == (0, {})


def test_set_and_get_cursor_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    EventStore(db_path).set_cursor("t3code", 42, {"started": ["i1"]})

    assert EventStore(db_path).get_cursor("t3code") == (42, {"started": ["i1"]})


def test_add_batch_with_cursor_persists_events_and_cursor(store: EventStore) -> None:
    t1 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    t2 = datetime(2026, 11, 20, 12, 1, tzinfo=UTC)
    items = [
        (make_event(session_id="s1", name="SessionStart"), t1),
        (make_event(session_id="s1", name="UserPromptSubmit"), t2),
    ]

    stored = store.add_batch_with_cursor("t3code", items, cursor=10, state={"runs": ["r1"]})

    assert [e.hook_event_name for e in stored] == ["SessionStart", "UserPromptSubmit"]
    assert [e.received_at for e in stored] == [t1, t2]
    assert len(store.events_for_session("s1")) == 2
    assert store.get_cursor("t3code") == (10, {"runs": ["r1"]})


def test_add_batch_with_cursor_rolls_back_on_error(store: EventStore) -> None:
    store.set_cursor("t3code", 5, {"k": "v"})

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        store.add_batch_with_cursor(
            "t3code",
            [(make_event(session_id="s1", name="SessionStart"), None)],
            cursor=99,
            state={"bad": Unserializable()},
        )

    assert store.events_for_session("s1") == []
    assert store.get_cursor("t3code") == (5, {"k": "v"})


def test_corrupt_cursor_state_recovers_to_empty(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "events.db"
    store = EventStore(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO source_cursors (source, cursor, state_json) VALUES ('t3code', 12, '{bad')"
        )

    assert store.get_cursor("t3code") == (0, {})
