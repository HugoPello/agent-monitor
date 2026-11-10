"""Tests for SSE formatting and the per-client stream generator."""

import pytest

from agent_monitor.broadcaster import Broadcaster
from agent_monitor.sse import PING, ServerSentEvent, event_stream


class TestEncode:
    def test_full_message(self) -> None:
        message = ServerSentEvent(data='{"a": 1}', event="agent_event", id="42")

        assert message.encode() == 'id: 42\nevent: agent_event\ndata: {"a": 1}\n\n'

    def test_data_only(self) -> None:
        assert ServerSentEvent(data="hi").encode() == "data: hi\n\n"

    def test_multiline_data_gets_one_prefix_per_line(self) -> None:
        assert ServerSentEvent(data="a\nb").encode() == "data: a\ndata: b\n\n"

    def test_empty_data(self) -> None:
        assert ServerSentEvent(data="").encode() == "data: \n\n"


class TestEventStream:
    @pytest.mark.anyio
    async def test_starts_with_retry_and_subscribes(self) -> None:
        broadcaster = Broadcaster()
        stream = event_stream(broadcaster, retry_ms=1234)

        assert await anext(stream) == "retry: 1234\n\n"
        assert broadcaster.subscriber_count == 1

        await stream.aclose()

    @pytest.mark.anyio
    async def test_yields_published_messages(self) -> None:
        broadcaster = Broadcaster()
        stream = event_stream(broadcaster)
        await anext(stream)  # retry line

        broadcaster.publish(ServerSentEvent(data="x", event="e", id="1"))

        assert await anext(stream) == "id: 1\nevent: e\ndata: x\n\n"
        await stream.aclose()

    @pytest.mark.anyio
    async def test_pings_when_idle(self) -> None:
        stream = event_stream(Broadcaster(), ping_interval=0.01)
        await anext(stream)

        assert await anext(stream) == PING
        await stream.aclose()

    @pytest.mark.anyio
    async def test_closing_the_stream_unsubscribes(self) -> None:
        # This is what happens when the browser tab is closed.
        broadcaster = Broadcaster()
        stream = event_stream(broadcaster)
        await anext(stream)

        await stream.aclose()

        assert broadcaster.subscriber_count == 0

    @pytest.mark.anyio
    async def test_ends_when_dropped_by_broadcaster(self) -> None:
        broadcaster = Broadcaster(max_queue_size=1)
        stream = event_stream(broadcaster)
        await anext(stream)

        broadcaster.publish(ServerSentEvent(data="1"))
        broadcaster.publish(ServerSentEvent(data="2"))  # overflow -> dropped

        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert broadcaster.subscriber_count == 0
