"""Typed events: exceptions contained, contexts detached."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from inceptools.core import ctxkit, errs
from inceptools.platform import events


@dataclass
class OrderPlaced:
    id: int


@dataclass
class OrderCancelled:
    id: int


async def test_dispatch_is_by_event_class():
    bus = events.Bus()
    seen: list[str] = []
    bus.on(OrderPlaced, lambda e: seen.append("placed"), mode=events.Mode.SYNC)
    bus.on(OrderCancelled, lambda e: seen.append("cancelled"), mode=events.Mode.SYNC)
    await bus.publish(OrderPlaced(1))
    assert seen == ["placed"]


async def test_publishing_with_no_subscribers_is_fine():
    await events.Bus().publish(OrderPlaced(1))


async def test_a_sync_subscriber_failure_reaches_the_publisher():
    bus = events.Bus()
    bus.on(
        OrderPlaced, lambda e: (_ for _ in ()).throw(RuntimeError("boom")), mode=events.Mode.SYNC
    )
    with pytest.raises(errs.AppError):
        await bus.publish(OrderPlaced(1))


async def test_an_async_subscriber_failure_does_not():
    bus = events.Bus()
    bus.on(OrderPlaced, lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
    await bus.publish(OrderPlaced(1))
    await bus.wait(timeout=1)


async def test_one_bad_subscriber_does_not_stop_the_others():
    bus = events.Bus()
    seen: list[str] = []
    bus.on(
        OrderPlaced,
        lambda e: (_ for _ in ()).throw(RuntimeError("boom")),
        name="bad",
        mode=events.Mode.SYNC,
    )
    bus.on(OrderPlaced, lambda e: seen.append("good"), name="good", mode=events.Mode.SYNC)
    with pytest.raises(errs.AppError):
        await bus.publish(OrderPlaced(1))
    assert seen == ["good"]


async def test_an_async_subscriber_keeps_the_identity():
    bus = events.Bus()
    seen: list[str | None] = []

    async def audit(event: OrderPlaced) -> None:
        await asyncio.sleep(0.01)
        seen.append(ctxkit.account_id())

    bus.subscribe()(audit)
    with ctxkit.use(account_id="acc_1", trace_id="t1"):
        await bus.publish(OrderPlaced(1))
    await bus.wait(timeout=1)
    assert seen == ["acc_1"]


async def test_the_decorator_reads_the_event_type():
    bus = events.Bus()
    seen: list[int] = []

    @bus.subscribe(mode=events.Mode.SYNC)
    async def record(event: OrderPlaced) -> None:
        seen.append(event.id)

    await bus.publish(OrderPlaced(7))
    assert seen == [7]


async def test_a_handler_without_an_event_annotation_is_refused():
    bus = events.Bus()
    with pytest.raises(errs.AppError, match="annotate its event parameter"):
        bus.subscribe()(lambda e: None)


def test_subscribers_are_listable():
    bus = events.Bus()
    bus.on(OrderPlaced, lambda e: None, name="audit")
    assert bus.subscribers() == {"OrderPlaced": ["audit"]}
