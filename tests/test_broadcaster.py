"""Tests for the in-memory Broadcaster."""

from agent_monitor.broadcaster import Broadcaster
from agent_monitor.sse import ServerSentEvent


def msg(data: str) -> ServerSentEvent:
    return ServerSentEvent(data=data)


def test_publish_reaches_every_subscriber() -> None:
    broadcaster = Broadcaster()
    a, b = broadcaster.subscribe(), broadcaster.subscribe()

    broadcaster.publish(msg("hello"))

    assert a.get_nowait() == msg("hello")
    assert b.get_nowait() == msg("hello")


def test_publish_without_subscribers_is_a_no_op() -> None:
    Broadcaster().publish(msg("nobody listening"))


def test_unsubscribed_queue_gets_nothing() -> None:
    broadcaster = Broadcaster()
    queue = broadcaster.subscribe()

    broadcaster.unsubscribe(queue)
    broadcaster.publish(msg("hello"))

    assert queue.empty()
    assert broadcaster.subscriber_count == 0


def test_unsubscribe_twice_is_harmless() -> None:
    broadcaster = Broadcaster()
    queue = broadcaster.subscribe()

    broadcaster.unsubscribe(queue)
    broadcaster.unsubscribe(queue)


def test_slow_subscriber_is_dropped_with_close_signal() -> None:
    broadcaster = Broadcaster(max_queue_size=2)
    slow = broadcaster.subscribe()

    for i in range(3):  # the third message overflows the queue
        broadcaster.publish(msg(str(i)))

    assert broadcaster.subscriber_count == 0
    # Backlog discarded; only the close signal is left.
    assert slow.get_nowait() is None
    assert slow.empty()


def test_close_disconnects_everyone() -> None:
    broadcaster = Broadcaster()
    a, b = broadcaster.subscribe(), broadcaster.subscribe()
    broadcaster.publish(msg("pending"))

    broadcaster.close()

    assert broadcaster.subscriber_count == 0
    assert a.get_nowait() is None
    assert b.get_nowait() is None


def test_dropping_one_subscriber_does_not_affect_others() -> None:
    broadcaster = Broadcaster(max_queue_size=2)
    slow = broadcaster.subscribe()
    broadcaster.publish(msg("0"))
    broadcaster.publish(msg("1"))
    fast = broadcaster.subscribe()  # joins with an empty queue

    broadcaster.publish(msg("2"))  # overflows `slow` only

    assert broadcaster.subscriber_count == 1
    assert slow.get_nowait() is None
    assert fast.get_nowait() == msg("2")
