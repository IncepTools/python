"""Bounded in-process workers with retries and dead letters.

**Explicitly not durable.** Messages live in memory and are lost when the
process exits. That is a deliberate scope: it covers "do this just after the
response, with a couple of retries" without pulling in a broker. When the
effect must survive a crash, use :mod:`inceptools.reliability.outbox`.

Shutdown drains: the input is closed and the workers finish what is already
queued. Cancellation is reserved for the deadline path, so a normal deploy
never abandons accepted work.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from inceptools.core import ctxkit, errs

__all__ = ["DeadLetter", "Message", "Queue"]

_log = logging.getLogger("inceptools.queue")


@dataclass(slots=True)
class Message:
    """One unit of work."""

    body: Any
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    #: 0 until a worker picks it up; 1 on the first attempt.
    attempts: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)
    #: The identity of whoever enqueued it, carried into the worker.
    context: ctxkit.RequestContext = field(default_factory=ctxkit.detach)


@dataclass(slots=True)
class DeadLetter:
    """A message that exhausted its attempts."""

    message: Message
    error: BaseException


Handler = Callable[[Message], Awaitable[None] | None]
DeadLetterHandler = Callable[[DeadLetter], Awaitable[None] | None]


class Queue:
    """A bounded work queue with a fixed number of workers."""

    def __init__(
        self,
        name: str,
        handler: Handler,
        *,
        workers: int = 1,
        buffer: int = 128,
        max_attempts: int = 3,
        retry_delay: float = 1.0,
        on_dead_letter: DeadLetterHandler | None = None,
    ) -> None:
        if not name:
            raise errs.internal("queue: name is required")
        if handler is None:
            raise errs.internal("queue: handler is required")

        self.name = name
        self.handler = handler
        self.workers = max(workers, 1)
        self.max_attempts = max(max_attempts, 1)
        self.retry_delay = retry_delay if retry_delay > 0 else 1.0
        self.on_dead_letter = on_dead_letter

        self._queue: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=max(buffer, 1))
        self._tasks: list[asyncio.Task[None]] = []
        self._started = False
        self._closed = False

    # -- producing -------------------------------------------------------

    async def enqueue(self, body: Any) -> str:
        """Add work. Valid **before** start, so boot-time producers work.

        Blocks when the buffer is full — backpressure, rather than an
        unbounded queue that turns into an out-of-memory kill.
        """
        if self._closed:
            raise errs.unavailable("queue is closed").with_code("QUEUE_CLOSED")
        message = Message(body=body)
        await self._queue.put(message)
        return message.id

    def enqueue_nowait(self, body: Any) -> str:
        """Add work without waiting. Raises when the buffer is full."""
        if self._closed:
            raise errs.unavailable("queue is closed").with_code("QUEUE_CLOSED")
        message = Message(body=body)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull as exc:
            raise (
                errs.exhausted(f"queue {self.name!r} is full")
                .with_code("QUEUE_FULL")
                .with_cause(exc)
            ) from exc
        return message.id

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    # -- lifecycle -------------------------------------------------------

    async def on_application_ready(self) -> None:
        await self.start()

    async def start(self) -> None:
        if self._started:
            raise errs.internal(f"queue {self.name!r} is already started")
        self._started = True
        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._worker(i), name=f"queue:{self.name}:{i}")
            for i in range(self.workers)
        ]
        _log.info("queue started", extra={"queue": self.name, "workers": self.workers})

    async def on_module_destroy(self) -> None:
        await self.stop()

    async def stop(self, timeout: float = 20.0) -> None:
        """Drain, then stop. Only the deadline cancels in-flight work."""
        if not self._started or self._closed:
            self._closed = True
            return
        self._closed = True

        for _ in self._tasks:
            await self._queue.put(None)

        _done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        if pending:
            _log.warning(
                "queue: workers did not stop within the drain deadline",
                extra={"queue": self.name, "pending": len(pending)},
            )
            for task in pending:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait(pending, timeout=2.0)
        self._tasks.clear()
        self._started = False

    # -- consuming -------------------------------------------------------

    async def _worker(self, index: int) -> None:
        while True:
            message = await self._queue.get()
            try:
                if message is None:
                    return
                await self._process(message)
            finally:
                self._queue.task_done()

    async def _process(self, message: Message) -> None:
        token = ctxkit.set_context(message.context)
        try:
            while message.attempts < self.max_attempts:
                message.attempts += 1
                try:
                    result = self.handler(message)
                    if inspect.isawaitable(result):
                        await result
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if message.attempts >= self.max_attempts:
                        await self._dead_letter(message, exc)
                        return
                    _log.warning(
                        "queue message failed; retrying",
                        exc_info=exc,
                        extra={
                            "queue": self.name,
                            "message_id": message.id,
                            "attempt": message.attempts,
                        },
                    )
                    await asyncio.sleep(self.retry_delay)
        finally:
            ctxkit.clear(token)

    async def _dead_letter(self, message: Message, error: BaseException) -> None:
        _log.error(
            "queue message exhausted its attempts",
            exc_info=error,
            extra={
                "queue": self.name,
                "message_id": message.id,
                "attempts": message.attempts,
            },
        )
        if self.on_dead_letter is None:
            # Nothing persists it. The log line above is the only record.
            return
        try:
            result = self.on_dead_letter(DeadLetter(message=message, error=error))
            if inspect.isawaitable(result):
                await result
        except Exception:
            _log.exception("queue: dead-letter handler failed", extra={"queue": self.name})
