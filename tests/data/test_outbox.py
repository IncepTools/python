"""A side effect as durable as the write that caused it."""

from __future__ import annotations

import pytest
from sqlalchemy import MetaData

from inceptools.core import errs
from inceptools.data import sqlx
from inceptools.reliability import outbox

METADATA = MetaData()
TABLE = outbox.outbox_table(METADATA)


@pytest.fixture
async def outbox_engine():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(METADATA.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_enqueue_outside_a_transaction_is_refused(outbox_engine):
    """Outside one it is just a slower publish with the same hole in it."""
    async with sqlx.session_scope(outbox_engine):
        with pytest.raises(errs.AppError, match="outside a transaction"):
            await outbox.enqueue(TABLE, "order.placed", {"id": 1})


async def test_a_message_survives_to_be_relayed(outbox_engine):
    async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
        await outbox.enqueue(TABLE, "order.placed", {"id": 1})

    published: list[tuple[str, dict]] = []
    relay = outbox.Relay(
        TABLE, lambda m: published.append((m.topic, m.body)), session_factory=outbox_engine
    )
    assert await relay.pending() == 1
    assert await relay.drain_once() == 1
    assert published == [("order.placed", {"id": 1})]
    assert await relay.pending() == 0


async def test_a_rolled_back_transaction_enqueues_nothing(outbox_engine):
    """The message and the state change commit together or not at all."""
    with pytest.raises(RuntimeError):
        async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
            await outbox.enqueue(TABLE, "order.placed", {"id": 1})
            raise RuntimeError("the write failed")

    relay = outbox.Relay(TABLE, lambda m: None, session_factory=outbox_engine)
    assert await relay.pending() == 0


async def test_a_failed_publish_is_retried(outbox_engine):
    async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
        await outbox.enqueue(TABLE, "t", {"id": 1})

    attempts: list[int] = []

    async def flaky(message: outbox.Message) -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("broker down")

    relay = outbox.Relay(TABLE, flaky, session_factory=outbox_engine, backoff=0.001)
    await relay.drain_once()
    assert await relay.pending() == 1

    import asyncio

    await asyncio.sleep(0.01)
    await relay.drain_once()
    assert await relay.pending() == 0
    assert len(attempts) == 2


async def test_a_poison_message_is_dropped_after_its_attempts(outbox_engine):
    async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
        await outbox.enqueue(TABLE, "t", {"id": 1})

    async def always_fails(message: outbox.Message) -> None:
        raise RuntimeError("never works")

    relay = outbox.Relay(
        TABLE, always_fails, session_factory=outbox_engine, max_attempts=2, backoff=0.001
    )
    import asyncio

    for _ in range(3):
        await relay.drain_once()
        await asyncio.sleep(0.01)
    assert await relay.pending() == 0


async def test_a_publisher_may_drop_a_message(outbox_engine):
    async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
        await outbox.enqueue(TABLE, "retired.topic", {"id": 1})

    relay = outbox.Relay(TABLE, lambda m: outbox.DROP, session_factory=outbox_engine)
    await relay.drain_once()
    assert await relay.pending() == 0


async def test_the_trace_id_travels_with_the_message(outbox_engine):
    from inceptools.core import ctxkit

    seen: list[str] = []
    with ctxkit.use(trace_id="t-123"):
        async with sqlx.session_scope(outbox_engine) as session, sqlx.transaction(session):
            await outbox.enqueue(TABLE, "t", {"id": 1})

    relay = outbox.Relay(
        TABLE, lambda m: seen.append(ctxkit.trace_id()), session_factory=outbox_engine
    )
    await relay.drain_once()
    assert seen == ["t-123"]
