"""Driver errors into typed errors, and the session on a context.

A service should never import a database driver to find out whether a write
failed because of a duplicate key. It raises and catches
:class:`~inceptools.core.errs.AppError`; translation happens once, here.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any, TypeVar

from sqlalchemy.exc import SQLAlchemyError

from inceptools.core import errs

__all__ = [
    "CONCURRENT_MODIFICATION",
    "current_session",
    "in_transaction",
    "session_scope",
    "set_session",
    "transaction",
    "translate",
]

T = TypeVar("T")

#: The code a retryable optimistic-concurrency clash carries.
CONCURRENT_MODIFICATION = "CONCURRENT_MODIFICATION"

_session: ContextVar[Any | None] = ContextVar("inceptools_db_session", default=None)
_in_tx: ContextVar[bool] = ContextVar("inceptools_in_transaction", default=False)

#: Prefer an explicitly quoted constraint name. Without the quotes the
#: unquoted form happily matches the word after "duplicate key", which is
#: "value", not a constraint.
_QUOTED_CONSTRAINT = re.compile(r"(?:constraint|key|index)\s+[\"'`]([\w.]+)[\"'`]", re.IGNORECASE)
#: SQLite: "UNIQUE constraint failed: products.sku".
_SQLITE_CONSTRAINT = re.compile(r"constraint\s+failed:\s*([\w.]+)", re.IGNORECASE)
_BARE_CONSTRAINT = re.compile(r"constraint\s+(?!failed\b)([\w.]+)", re.IGNORECASE)


def current_session() -> Any | None:
    """The session for this request or job, if one is open."""
    return _session.get()


def set_session(session: Any) -> Any:
    """Install a session on the context. Returns a token to reset with."""
    return _session.set(session)


def in_transaction() -> bool:
    """True inside :func:`transaction`.

    The outbox checks this: enqueueing outside a transaction is just a slower
    publish with the same hole in the middle.
    """
    return _in_tx.get()


@contextlib.asynccontextmanager
async def session_scope(session_factory: Callable[[], Any]) -> AsyncIterator[Any]:
    """Open a session for the duration of the block and put it on the context."""
    session = session_factory()
    token = _session.set(session)
    try:
        async with session:
            yield session
    finally:
        _session.reset(token)


@contextlib.asynccontextmanager
async def transaction(session: Any = None) -> AsyncIterator[Any]:
    """Run a block in one transaction.

        async with sqlx.transaction() as session:
            await repo.create(order)
            await outbox.enqueue("order.placed", order)

    Nesting reuses the outer transaction, so a service calling another
    service's method does not commit half of it.
    """
    target = session or current_session()
    if target is None:
        raise errs.internal(
            "no database session on this context; open one with sqlx.session_scope()"
        )

    if _in_tx.get():
        yield target  # already inside one; a savepoint is the caller's choice
        return

    token = _in_tx.set(True)
    try:
        async with target.begin():
            yield target
    except SQLAlchemyError as exc:
        # Only a *driver* failure is translated. Re-labelling the caller's own
        # exception would lose its type, so `except PaymentDeclined` upstream
        # would stop matching and the rollback would look like a database bug.
        raise translate(exc) from exc
    finally:
        _in_tx.reset(token)


#: Driver-independent substrings, checked in order. Matching on text is
#: unlovely, but every driver spells these differently and the alternative is
#: a hard dependency on each one.
_PATTERNS: tuple[tuple[tuple[str, ...], str, str, str], ...] = (
    (
        ("unique constraint", "duplicate key", "unique violation", "duplicate entry"),
        "conflict",
        "that already exists",
        "DUPLICATE",
    ),
    (
        ("foreign key constraint", "foreign key violation", "violates foreign key"),
        "conflict",
        "a related record is missing or still in use",
        "FOREIGN_KEY",
    ),
    (
        ("not-null constraint", "not null violation", "cannot be null"),
        "invalid",
        "a required value was missing",
        "NOT_NULL",
    ),
    (
        ("check constraint", "check violation"),
        "invalid",
        "a value is outside the allowed range",
        "CHECK_FAILED",
    ),
    (
        ("deadlock", "serialization failure", "could not serialize", "lock wait timeout"),
        "concurrent",
        "the record changed while you were editing it; try again",
        CONCURRENT_MODIFICATION,
    ),
    (
        (
            "connection refused",
            "server closed",
            "connection reset",
            "could not connect",
            "connection already closed",
            "terminating connection",
        ),
        "unavailable",
        "the database is unavailable",
        "DB_UNAVAILABLE",
    ),
    (
        ("statement timeout", "query timeout", "canceling statement"),
        "timeout",
        "the query took too long",
        "DB_TIMEOUT",
    ),
)


def translate(exc: BaseException, *, entity: str = "record") -> errs.AppError:
    """Turn a driver error into the application's vocabulary.

    Anything unrecognised becomes a redacted 500 carrying the original as the
    cause — a driver message can name a column, a constraint or a hostname, and
    none of those belong in a response.
    """
    if isinstance(exc, errs.AppError):
        return exc

    text = str(exc).lower()
    for needles, kind, message, code in _PATTERNS:
        if not any(needle in text for needle in needles):
            continue

        if code == "DUPLICATE":
            constraint = _constraint_name(str(exc))
            detail = f"this {entity} already exists"
            if constraint:
                return (
                    errs.conflict(detail)
                    .with_code(code)
                    .with_cause(exc)
                    .with_meta(constraint=constraint)
                )
            return errs.conflict(detail).with_code(code).with_cause(exc)

        builder = {
            "conflict": errs.conflict,
            "invalid": errs.invalid,
            "concurrent": errs.conflict,
            "unavailable": errs.unavailable,
            "timeout": errs.timeout,
        }[kind]
        return builder(message).with_code(code).with_cause(exc)

    return errs.internal().with_cause(exc)


def _constraint_name(text: str) -> str:
    for pattern in (_QUOTED_CONSTRAINT, _SQLITE_CONSTRAINT, _BARE_CONSTRAINT):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


async def guard(awaitable: Awaitable[T], *, entity: str = "record") -> T:
    """Await *awaitable*, translating any driver error it raises."""
    try:
        return await awaitable
    except Exception as exc:
        raise translate(exc, entity=entity) from exc
