"""Heartbeats, framing and disconnect awareness."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from inceptools import App, Controller, Get, Module, Request, testkit
from inceptools.web import sse


def test_event_framing():
    assert sse.Event(data="hello").encode() == b"data: hello\n\n"
    assert sse.Event(data={"id": 1}, event="created", id="7").encode() == (
        b'event: created\nid: 7\ndata: {"id": 1}\n\n'
    )


def test_a_newline_in_data_becomes_two_data_lines():
    """One newline would terminate the event early and truncate the payload."""
    assert sse.Event(data="a\nb").encode() == b"data: a\ndata: b\n\n"


def test_a_comment_only_event_is_valid():
    """How the heartbeat works: traffic that every client ignores."""
    assert sse.HEARTBEAT.encode() == b": heartbeat\n\n"


def test_retry_tells_the_browser_when_to_reconnect():
    assert b"retry: 3000" in sse.Event(data="x", retry=3000).encode()


async def test_the_stream_emits_a_heartbeat_while_idle():
    async def slow() -> AsyncIterator[str]:
        await asyncio.sleep(0.05)
        yield "late"

    chunks: list[bytes] = []
    async for chunk in sse.EventSourceResponse._wrap(slow(), None, 0.01, None):
        chunks.append(chunk)
        if len(chunks) > 6:
            break

    assert b": heartbeat\n\n" in chunks
    assert b"data: late\n\n" in chunks


async def test_on_disconnect_runs_when_the_stream_ends():
    called: list[bool] = []

    async def source() -> AsyncIterator[str]:
        yield "one"

    async for _ in sse.EventSourceResponse._wrap(source(), None, 10.0, lambda: called.append(True)):
        pass
    assert called == [True]


# -- through a real route -------------------------------------------------


@Controller("/stream")
class StreamController:
    @Get("/", raw=True)
    async def events(self, request: Request):
        """A raw route: the envelope must not try to wrap a stream."""
        return sse.stream(self._ticks(), request, heartbeat=5.0)

    async def _ticks(self):
        for index in range(3):
            yield sse.Event(data={"tick": index}, event="tick", id=str(index))


@Module(controllers=[StreamController])
class StreamModule: ...


@App(modules=[StreamModule], name="live")
class StreamApp: ...


def test_a_raw_route_streams_without_the_envelope():
    with testkit.build(StreamApp) as client:
        response = client.client.get("/live/stream")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Without this an intermediary batches the stream into lumps.
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["cache-control"] == "no-cache, no-transform"
        body = response.text
        assert body.count("event: tick") == 3
        assert '"tick": 0' in body
        assert "meta" not in body


@pytest.mark.parametrize("value", ["", None, 0])
def test_a_falsey_payload_still_produces_a_data_line(value):
    assert sse.Event(data=value).encode().startswith(b"data:")
