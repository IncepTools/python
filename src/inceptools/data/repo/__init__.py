"""The scoped generic repository.

The ownership filter is not yours to remember::

    repo = Repository(Product, entity_name="product",
                      tenanted=True, sortable=("created_at", "price"))

Every read is filtered by the account on the context. Every create takes the
account *from the context*, not from the payload. A request with no account is
an error, never "all accounts" — which is what forgetting
``.where(Product.account_id == ...)`` silently means, and it fails by returning
another customer's rows rather than by raising.

Reaching wider is possible and has to be spelled out::

    async with repo.unscoped():      # a nightly report across every account
        ...                          # deliberate, visible, greppable

:meth:`Repository.update_fields` refuses an empty filter, because an update
with no ``WHERE`` rewrites every row the account owns. It takes a mapping
rather than a model instance, because a model carries defaults for the fields
you did not set — so a model-based update cannot distinguish "leave it alone"
from "set it to false", and the PATCH that disables something appears to
succeed while changing nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from contextvars import ContextVar
from typing import Any, Generic, TypeVar

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from inceptools.core import ctxkit, errs
from inceptools.data import sqlx
from inceptools.web import pagination

__all__ = ["Page", "Repository"]

T = TypeVar("T")

_unscoped: ContextVar[bool] = ContextVar("inceptools_repo_unscoped", default=False)


class Page(Generic[T]):
    """One page of results, and what it took to get it."""

    __slots__ = ("items", "page", "size", "total")

    def __init__(self, items: list[T], *, page: int, size: int, total: int) -> None:
        self.items = items
        self.page = page
        self.size = size
        self.total = total

    @property
    def pages(self) -> int:
        return pagination.pages_for(self.total, self.size)

    def __iter__(self) -> Any:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"<Page {self.page}/{self.pages} items={len(self.items)} total={self.total}>"


class Repository(Generic[T]):
    """Scoped data access for one entity."""

    def __init__(
        self,
        model: type[T],
        *,
        entity_name: str = "",
        tenanted: bool = False,
        tenant_column: str = "account_id",
        soft_delete_column: str = "",
        id_column: str = "id",
        sortable: Sequence[str] = (),
        default_sort: str = "",
        session_factory: Any = None,
    ) -> None:
        self.model = model
        self.entity_name = entity_name or getattr(model, "__name__", "record").lower()
        self.tenanted = tenanted
        self.tenant_column = tenant_column
        self.soft_delete_column = soft_delete_column
        #: The primary key attribute. Named because not every table calls it ``id``.
        self.id_column = id_column
        self.sortable = tuple(sortable)
        self.default_sort = default_sort
        self._session_factory = session_factory

        if tenanted and not hasattr(model, tenant_column):
            raise errs.internal(
                f"{self.entity_name}: tenanted=True but {model.__name__} has no "
                f"{tenant_column!r} column"
            )

    # -- session ---------------------------------------------------------

    def _session(self) -> Any:
        session = sqlx.current_session()
        if session is not None:
            return session
        if self._session_factory is not None:
            return self._session_factory()
        raise errs.internal(
            f"{self.entity_name}: no database session on this context; "
            "open one with sqlx.session_scope()"
        )

    @contextlib.asynccontextmanager
    async def unscoped(self) -> AsyncIterator[None]:
        """Drop the ownership filter for this block.

        Deliberate and greppable. Use it for a cross-account report, never to
        make a test pass.
        """
        token = _unscoped.set(True)
        try:
            yield
        finally:
            _unscoped.reset(token)

    # -- scoping ---------------------------------------------------------

    def _scope(self, statement: Any) -> Any:
        if self.tenanted and not _unscoped.get():
            account = ctxkit.require_account()
            statement = statement.where(getattr(self.model, self.tenant_column) == account)
        if self.soft_delete_column:
            statement = statement.where(getattr(self.model, self.soft_delete_column).is_(None))
        return statement

    def _order(self, statement: Any, sort: str, descending: bool) -> Any:
        column = sort or self.default_sort
        if not column:
            return statement
        if column not in self.sortable and column != self.default_sort:
            # Only an allow-listed key ever reaches ORDER BY.
            raise (
                errs.invalid(
                    f"sort must be one of: {', '.join(self.sortable)}"
                    if self.sortable
                    else "sorting is not supported here"
                )
                .with_code("INVALID_SORT")
                .with_details(
                    errs.FieldError(field="sort", message=f"{column} is not sortable", rule="oneof")
                )
            )
        attribute = getattr(self.model, column)
        return statement.order_by(attribute.desc() if descending else attribute.asc())

    # -- reads -----------------------------------------------------------

    async def find_by_id(self, identifier: Any) -> T | None:
        """The row, or ``None`` — scoped, so another account's row is ``None``."""
        column = getattr(self.model, self.id_column)
        statement = self._scope(select(self.model).where(column == identifier))
        return await self._scalar(statement)

    async def get_by_id(self, identifier: Any) -> T:
        """The row, or a 404.

        A row that exists but belongs to someone else is **not found**, never
        forbidden: a 403 confirms the id exists, which is itself a leak.
        """
        found = await self.find_by_id(identifier)
        if found is None:
            raise errs.not_found(f"this {self.entity_name} does not exist").with_code(
                f"{self.entity_name.upper()}_NOT_FOUND"
            )
        return found

    async def find_one(self, **filters: Any) -> T | None:
        statement = self._scope(select(self.model).filter_by(**filters))
        return await self._scalar(statement)

    async def find_all(self, *, limit: int = 0, **filters: Any) -> list[T]:
        statement = self._scope(select(self.model).filter_by(**filters))
        if limit > 0:
            statement = statement.limit(limit)
        return await self._scalars(statement)

    async def exists(self, **filters: Any) -> bool:
        statement = self._scope(select(func.count()).select_from(self.model).filter_by(**filters))
        return bool(await self._scalar(statement))

    async def count(self, **filters: Any) -> int:
        statement = self._scope(select(func.count()).select_from(self.model).filter_by(**filters))
        return int(await self._scalar(statement) or 0)

    async def paginate(self, query: pagination.Query, **filters: Any) -> Page[T]:
        """A page, with its total, under the same scope."""
        base = self._scope(select(self.model).filter_by(**filters))
        total_stmt = self._scope(select(func.count()).select_from(self.model).filter_by(**filters))
        total = int(await self._scalar(total_stmt) or 0)

        descending = query.order is pagination.Direction.DESC
        statement = self._order(base, query.sort, descending)
        statement = statement.offset(query.offset).limit(query.limit)
        items = await self._scalars(statement)
        return Page(items, page=query.page, size=query.limit, total=total)

    # -- writes ----------------------------------------------------------

    async def create(self, entity: T) -> T:
        """Insert, stamping the account from the **context**.

        Taking it from the payload would let a caller write into another
        account by sending its id.
        """
        if self.tenanted and not _unscoped.get():
            setattr(entity, self.tenant_column, ctxkit.require_account())
        session = self._session()
        session.add(entity)
        try:
            await session.flush()
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return entity

    async def create_all(self, entities: Sequence[T]) -> list[T]:
        account = ctxkit.require_account() if self.tenanted and not _unscoped.get() else None
        session = self._session()
        for entity in entities:
            if account is not None:
                setattr(entity, self.tenant_column, account)
            session.add(entity)
        try:
            await session.flush()
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return list(entities)

    async def update_fields(self, values: Mapping[str, Any], /, **filters: Any) -> int:
        """Set *values* on the rows matching *filters*. Returns rows affected.

        Both arguments are checked: an empty ``values`` is a no-op worth
        catching, and an empty ``filters`` would rewrite every row this account
        owns.
        """
        if not values:
            raise errs.internal(f"{self.entity_name}: update with no values")
        if not filters:
            raise errs.internal(
                f"{self.entity_name}: update with no condition would rewrite every row"
            )

        statement = self._scope(update(self.model).filter_by(**filters)).values(**values)
        session = self._session()
        try:
            result = await session.execute(statement)
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return int(result.rowcount or 0)

    async def update_by_id(self, identifier: Any, values: Mapping[str, Any]) -> int:
        return await self.update_fields(values, **{self.id_column: identifier})

    async def delete(self, **filters: Any) -> int:
        """Delete the matching rows. Refuses an empty condition."""
        if not filters:
            raise errs.internal(
                f"{self.entity_name}: delete with no condition would remove every row"
            )
        statement = self._scope(delete(self.model).filter_by(**filters))
        session = self._session()
        try:
            result = await session.execute(statement)
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return int(result.rowcount or 0)

    async def delete_by_id(self, identifier: Any) -> int:
        return await self.delete(**{self.id_column: identifier})

    # -- plumbing --------------------------------------------------------

    async def _scalar(self, statement: Any) -> Any:
        session = self._session()
        try:
            result = await session.execute(statement)
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return result.scalar_one_or_none() if hasattr(result, "scalar_one_or_none") else None

    async def _scalars(self, statement: Any) -> list[Any]:
        session = self._session()
        try:
            result = await session.execute(statement)
        except SQLAlchemyError as exc:
            raise sqlx.translate(exc, entity=self.entity_name) from exc
        return list(result.scalars().all())

    def __repr__(self) -> str:
        scope = "tenanted" if self.tenanted else "global"
        return f"<Repository {self.entity_name} {scope}>"
