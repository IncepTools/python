"""A side effect as durable as the write that caused it.

The problem: you write the order, then publish ``order.placed``. If the
process dies between the two, the order exists and nothing downstream knows.
If you publish first and the write fails, the reverse. There is no ordering of
two systems that fixes this.

The outbox writes the message **in the same transaction as the state change**,
so they commit or roll back together. A relay then moves rows to the real
broker.

:func:`enqueue` *refuses* to run outside a transaction. Outside one it is just
a slower publish with the same hole in the middle, and the hole is invisible.

Delivery is **at-least-once**. Consumers must be idempotent; exactly-once is
not on offer here or anywhere else.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    func,
    select,
    update,
)

from inceptools.core import ctxkit, errs
from inceptools.data import sqlx

__all__ = ["DROP", "Message", "Relay", "enqueue", "outbox_table"]

_log = logging.getLogger("inceptools.outbox")

#: Return this from a publisher to discard a message permanently — a topic
#: that no longer exists, a payload that will never be accepted.
DROP = object()

TABLE_NAME = "outbox_messages"


def outbox_table(metadata: MetaData, name: str = TABLE_NAME) -> Table:
    """Define the outbox table on *metadata*.

    The partial index is what keeps the relay's poll cheap once the table has
    millions of published rows in it.
    """
    table = Table(
        name,
        metadata,
        Column("id", String(36), primary_key=True),
        Column("topic", String(255), nullable=False),
        Column("payload", Text, nullable=False),
        Column("trace_id", String(64)),
        Column("attempts", Integer, nullable=False, default=0),
        Column("last_error", Text),
        Column("available_at", DateTime(timezone=True), nullable=False),
        Column("published_at", DateTime(timezone=True), nullable=True),
        Column("dropped", Boolean, nullable=False, default=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        extend_existing=True,
    )
    Index(
        f"{name}_pending_idx",
        table.c.available_at,
        sqlite_where=table.c.published_at.is_(None),
        postgresql_where=table.c.published_at.is_(None),
    )
    return table


@dataclass(slots=True)
class Message:
    """One pending side effect."""

    id: str
    topic: str
    payload: Any
    trace_id: str = ""
    attempts: int = 0
    last_error: str = ""

    @property
    def body(self) -> Any:
        """The decoded payload."""
        if isinstance(self.payload, str | bytes):
            with contextlib.suppress(json.JSONDecodeError, UnicodeDecodeError):
                return json.loads(self.payload)
        return self.payload


def _now() -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC)


async def enqueue(
    table: Table,
    topic: str,
    payload: Any,
    *,
    session: Any = None,
    available_at: dt.datetime | None = None,
) -> str:
    """Write a message in the current transaction. Returns its id.

    Raises when called outside one — see the module docstring.
    """
    if not sqlx.in_transaction():
        raise errs.internal(
            f"outbox.enqueue({topic!r}) was called outside a transaction; "
            "wrap it and the state change it describes in sqlx.transaction()"
        )

    target = session or sqlx.current_session()
    if target is None:
        raise errs.internal("outbox: no database session on this context")

    message_id = str(uuid.uuid4())
    await target.execute(
        table.insert().values(
            id=message_id,
            topic=topic,
            payload=json.dumps(payload, default=str),
            trace_id=ctxkit.trace_id(),
            attempts=0,
            last_error="",
            available_at=available_at or _now(),
            published_at=None,
            dropped=False,
            created_at=_now(),
        )
    )
    return message_id


Publisher = Callable[[Message], Awaitable[Any]]


class Relay:
    """Moves outbox rows to the real broker.

    Runs on every replica. Rows are claimed with ``SKIP LOCKED`` where the
    database supports it, so N relays share the work instead of fighting over
    the same batch.
    """

    def __init__(
        self,
        table: Table,
        publisher: Publisher,
        *,
        session_factory: Any,
        batch: int = 50,
        interval: float = 5.0,
        max_attempts: int = 10,
        backoff: float = 10.0,
        skip_locked: bool = True,
    ) -> None:
        self.table = table
        self.publisher = publisher
        self.session_factory = session_factory
        self.batch = max(batch, 1)
        self.interval = interval if interval > 0 else 5.0
        self.max_attempts = max(max_attempts, 1)
        self.backoff = backoff if backoff > 0 else 10.0
        self.skip_locked = skip_locked

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def on_application_ready(self) -> None:
        await self.start()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.get_running_loop().create_task(self._loop(), name="outbox-relay")
        _log.info("outbox relay started", extra={"interval_s": self.interval})

    async def on_module_destroy(self) -> None:
        await self.stop()

    async def stop(self, timeout: float = 10.0) -> None:
        if self._task is None:
            return
        self._stopping.set()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._task, timeout)
        if not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                moved = await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.exception("outbox relay cycle failed", exc_info=exc)
                moved = 0
            if moved < self.batch:
                # Nothing left; wait. A full batch means there is more to do.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), self.interval)

    async def drain_once(self) -> int:
        """Publish one batch. Returns how many rows were handled."""
        async with sqlx.session_scope(self.session_factory) as session:
            messages = await self._claim(session)
            if not messages:
                return 0
            for message in messages:
                await self._publish_one(session, message)
            await session.commit()
            return len(messages)

    async def _claim(self, session: Any) -> list[Message]:
        table = self.table
        statement = (
            select(table)
            .where(
                and_(
                    table.c.published_at.is_(None),
                    table.c.dropped.is_(False),
                    table.c.available_at <= _now(),
                )
            )
            .order_by(table.c.available_at)
            .limit(self.batch)
        )
        if self.skip_locked:
            with contextlib.suppress(Exception):  # SQLite has no SKIP LOCKED
                statement = statement.with_for_update(skip_locked=True)

        try:
            result = await session.execute(statement)
        except Exception as exc:
            _log.debug("outbox: claiming without SKIP LOCKED", exc_info=exc)
            await session.rollback()
            result = await session.execute(
                select(table)
                .where(
                    and_(
                        table.c.published_at.is_(None),
                        table.c.dropped.is_(False),
                        table.c.available_at <= _now(),
                    )
                )
                .order_by(table.c.available_at)
                .limit(self.batch)
            )

        return [
            Message(
                id=row.id,
                topic=row.topic,
                payload=row.payload,
                trace_id=row.trace_id or "",
                attempts=row.attempts,
                last_error=row.last_error or "",
            )
            for row in result.mappings().all()
        ]

    async def _publish_one(self, session: Any, message: Message) -> None:
        table = self.table
        token = ctxkit.set_context(ctxkit.RequestContext(trace_id=message.trace_id))
        try:
            outcome = self.publisher(message)
            if inspect.isawaitable(outcome):
                outcome = await outcome
        except Exception as exc:
            await self._record_failure(session, message, exc)
            return
        finally:
            ctxkit.clear(token)

        if outcome is DROP:
            _log.warning(
                "outbox message dropped by the publisher",
                extra={"message_id": message.id, "topic": message.topic},
            )
            await session.execute(
                update(table).where(table.c.id == message.id).values(dropped=True)
            )
            return

        await session.execute(
            update(table)
            .where(table.c.id == message.id)
            .values(published_at=_now(), attempts=message.attempts + 1, last_error="")
        )

    async def _record_failure(self, session: Any, message: Message, exc: BaseException) -> None:
        table = self.table
        attempts = message.attempts + 1
        exhausted = attempts >= self.max_attempts
        # Exponential, capped so a poison message does not schedule itself
        # years into the future and disappear from view.
        delay = min(self.backoff * (2 ** (attempts - 1)), 3600.0)

        if exhausted:
            _log.error(
                "outbox message exhausted its attempts and was dropped",
                exc_info=exc,
                extra={"message_id": message.id, "topic": message.topic, "attempts": attempts},
            )
        else:
            _log.warning(
                "outbox publish failed; will retry",
                exc_info=exc,
                extra={"message_id": message.id, "topic": message.topic, "attempts": attempts},
            )

        await session.execute(
            update(table)
            .where(table.c.id == message.id)
            .values(
                attempts=attempts,
                last_error=str(exc)[:2000],
                dropped=exhausted,
                available_at=_now() + dt.timedelta(seconds=delay),
            )
        )

    async def pending(self) -> int:
        """How many messages are still waiting. Worth a health check."""
        async with sqlx.session_scope(self.session_factory) as session:
            result = await session.execute(
                select(func.count())
                .select_from(self.table)
                .where(and_(self.table.c.published_at.is_(None), self.table.c.dropped.is_(False)))
            )
            return int(result.scalar_one() or 0)
