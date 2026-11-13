"""Heartbeats, flushing and disconnect-aware server-sent events.

Three things an SSE endpoint gets wrong if it is hand-rolled:

* **No heartbeat.** An idle stream is indistinguishable from a dead one, and
  proxies close it after their own idle timeout — typically 60 seconds.
* **No disconnect check.** The generator keeps producing, and keeps holding
  whatever it holds, long after the client has gone.
* **Buffering.** Without the right headers an intermediary batches events and
  the stream stops being a stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import StreamingResponse

from inceptools.core import ctxkit

__all__ = ["Event", "EventSourceResponse", "stream"]

_log = logging.getLogger("inceptools.sse")


@dataclass(slots=True)
class Event:
    """One server-sent event."""

    data: Any = ""
    event: str = ""
    id: str = ""
    #: Tells the browser how long to wait before reconnecting, in milliseconds.
    retry: int | None = None
    comment: str = ""

    def encode(self) -> bytes:
        lines: list[str] = []
        if self.comment:
            lines.extend(f": {part}" for part in self.comment.splitlines())
        if self.event:
            lines.append(f"event: {self.event}")
        if self.id:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")

        if self.data != "" or not lines:
            payload = (
                self.data if isinstance(self.data, str) else json.dumps(self.data, default=str)
            )
            # A newline inside data would terminate the event early.
            lines.extend(f"data: {part}" for part in payload.split("\n"))

        return ("\n".join(lines) + "\n\n").encode("utf-8")


#: Sent periodically so the connection is not idle. A comment is ignored by
#: every client but still counts as traffic.
HEARTBEAT = Event(comment="heartbeat")

_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # nginx honours this; without it the stream is buffered and arrives in
    # lumps or not at all.
    "X-Accel-Buffering": "no",
}


class EventSourceResponse(StreamingResponse):
    """A streaming response that keeps itself alive and notices a disconnect."""

    media_type = "text/event-stream"

    def __init__(
        self,
        source: AsyncIterator[Event | Any],
        *,
        request: Request | None = None,
        heartbeat: float = 15.0,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        on_disconnect: Callable[[], Any] | None = None,
    ) -> None:
        merged = dict(_HEADERS)
        merged.update(headers or {})
        super().__init__(
            content=self._wrap(source, request, heartbeat, on_disconnect),
            status_code=status_code,
            headers=merged,
            media_type=self.media_type,
        )

    @staticmethod
    async def _wrap(
        source: AsyncIterator[Event | Any],
        request: Request | None,
        heartbeat: float,
        on_disconnect: Callable[[], Any] | None,
    ) -> AsyncIterator[bytes]:
        iterator = source.__aiter__()
        pending: asyncio.Task[Any] | None = None
        try:
            while True:
                if request is not None and await request.is_disconnected():
                    return

                # One pending pull at a time. Starting a second ``__anext__``
                # while the first is still running raises "asynchronous
                # generator is already running" — so a heartbeat keeps waiting
                # on the *same* task rather than beginning another.
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())

                try:
                    item = await asyncio.wait_for(asyncio.shield(pending), heartbeat)
                except TimeoutError:
                    # Nothing to send yet; prove the connection is alive.
                    yield HEARTBEAT.encode()
                    continue
                except StopAsyncIteration:
                    pending = None
                    return

                pending = None
                yield (item if isinstance(item, Event) else Event(data=item)).encode()
        except asyncio.CancelledError:
            raise
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await pending
            if on_disconnect is not None:
                with contextlib.suppress(Exception):
                    on_disconnect()
            with contextlib.suppress(Exception):
                aclose = getattr(iterator, "aclose", None)
                if aclose is not None:
                    await aclose()


def stream(
    source: AsyncIterator[Event | Any],
    request: Request | None = None,
    *,
    heartbeat: float = 15.0,
    **kwargs: Any,
) -> EventSourceResponse:
    """Return *source* as an event stream.

    Mark the route ``raw`` so the envelope does not try to wrap the stream::

        @Get("/events", raw=True)
        async def events(self, request: Request):
            return sse.stream(self.service.subscribe(), request)
    """
    _ = ctxkit.current()  # keeps the identity attached for logs inside the stream
    return EventSourceResponse(source, request=request, heartbeat=heartbeat, **kwargs)
