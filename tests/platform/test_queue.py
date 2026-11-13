"""Bounded workers, retries, and a dead-letter callback."""

from __future__ import annotations

import asyncio

import pytest

from inceptools.core import ctxkit, errs
from inceptools.platform.queue import Queue


async def test_messages_are_processed():
    done: list[str] = []
    queue = Queue("emails", lambda m: done.append(m.body), workers=2)
    await queue.enqueue("a")
    await queue.enqueue("b")
    await queue.start()
    await asyncio.sleep(0.05)
    await queue.stop()
    assert sorted(done) == ["a", "b"]


async def test_enqueue_before_start_is_valid():
    done: list[str] = []
    queue = Queue("q", lambda m: done.append(m.body))
    await queue.enqueue("early")
    await queue.start()
    await asyncio.sleep(0.03)
    await queue.stop()
    assert done == ["early"]


async def test_a_failing_message_is_retried_then_dead_lettered():
    attempts: list[int] = []
    dead: list[str] = []

    def handler(message) -> None:
        attempts.append(message.attempts)
        raise RuntimeError("nope")

    queue = Queue(
        "q",
        handler,
        max_attempts=3,
        retry_delay=0.01,
        on_dead_letter=lambda d: dead.append(d.message.body),
    )
    await queue.enqueue("bad")
    await queue.start()
    await asyncio.sleep(0.15)
    await queue.stop()
    assert attempts == [1, 2, 3]
    assert dead == ["bad"]


async def test_one_bad_message_does_not_block_the_others():
    done: list[str] = []

    def handler(message) -> None:
        if message.body == "bad":
            raise RuntimeError("nope")
        done.append(message.body)

    queue = Queue("q", handler, workers=2, max_attempts=1)
    for body in ("a", "bad", "b"):
        await queue.enqueue(body)
    await queue.start()
    await asyncio.sleep(0.08)
    await queue.stop()
    assert sorted(done) == ["a", "b"]


async def test_stop_drains_what_was_accepted():
    """A normal deploy never abandons accepted work."""
    done: list[str] = []

    async def handler(message) -> None:
        await asyncio.sleep(0.01)
        done.append(message.body)

    queue = Queue("q", handler, workers=1)
    for i in range(5):
        await queue.enqueue(str(i))
    await queue.start()
    await queue.stop(timeout=5)
    assert len(done) == 5


async def test_identity_travels_with_the_message():
    seen: list[str | None] = []
    queue = Queue("q", lambda m: seen.append(ctxkit.account_id()))
    with ctxkit.use(account_id="acc_1"):
        await queue.enqueue("x")
    await queue.start()
    await asyncio.sleep(0.03)
    await queue.stop()
    assert seen == ["acc_1"]


async def test_a_closed_queue_refuses_work():
    queue = Queue("q", lambda m: None)
    await queue.start()
    await queue.stop()
    with pytest.raises(errs.AppError, match="closed"):
        await queue.enqueue("x")


async def test_a_full_buffer_is_backpressure_not_an_oom():
    queue = Queue("q", lambda m: None, buffer=1)
    queue.enqueue_nowait("a")
    with pytest.raises(errs.AppError, match="full"):
        queue.enqueue_nowait("b")


def test_a_queue_needs_a_name_and_a_handler():
    with pytest.raises(errs.AppError, match="name is required"):
        Queue("", lambda m: None)
    with pytest.raises(errs.AppError, match="handler is required"):
        Queue("q", None)
