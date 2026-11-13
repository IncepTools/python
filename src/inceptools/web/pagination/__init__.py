"""Bounded page requests with an allow-listed sort.

Two failures this prevents:

* ``?limit=1000000`` turning a list endpoint into a denial of service. The
  limit is clamped, not rejected — a client asking for too much gets the
  maximum, not an error it has to learn about.
* ``?sort=password`` reaching an ``ORDER BY``. :attr:`Options.sortable` is an
  allow-list, and an empty one refuses sorting entirely rather than passing
  the column through. :meth:`Query.order_by` is interpolated into SQL and is
  safe *only* because the key came from that list.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request

from inceptools.core import errs

__all__ = [
    "DEFAULT_LIMIT",
    "DEFAULT_PAGE",
    "MAX_LIMIT",
    "Direction",
    "Options",
    "Query",
    "from_request",
    "pages_for",
]

DEFAULT_PAGE = 1
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class Direction(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class Query:
    """A validated page request."""

    page: int = DEFAULT_PAGE
    limit: int = DEFAULT_LIMIT
    sort: str = ""
    order: Direction | None = None
    search: str = ""

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit if self.page > 0 else 0

    def order_by(self) -> str:
        """``"created_at DESC"``, or ``""`` when no sort was requested."""
        if not self.sort:
            return ""
        direction = (self.order or Direction.ASC).value.upper()
        return f"{self.sort} {direction}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "limit": self.limit,
            "sort": self.sort,
            "order": self.order.value if self.order else "",
            "search": self.search,
        }


@dataclass(frozen=True, slots=True)
class Options:
    """What this endpoint will accept."""

    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT
    #: Columns a client may sort by. Empty means sorting is refused — fail
    #: closed, because the alternative is an injectable ``ORDER BY``.
    sortable: tuple[str, ...] = ()
    #: Applied when the client sends none. Comes from your code, so it is not
    #: checked against :attr:`sortable`.
    default_sort: str = ""
    default_order: Direction = Direction.ASC


def pages_for(total: int, limit: int) -> int:
    if limit <= 0:
        limit = 1
    return (total + limit - 1) // limit


def _invalid(field_name: str, message: str, rule: str, code: str) -> errs.AppError:
    return (
        errs.invalid(message)
        .with_code(code)
        .with_details(errs.FieldError(field=field_name, message=message, rule=rule))
    )


def _positive_int(raw: str | None, name: str, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise _invalid(
            name, f"{name} must be a positive whole number", "numeric", "INVALID_PAGINATION"
        )
    return value


def from_request(request: Request, opt: Options | None = None) -> Query:
    """Read ``page``, ``limit``, ``sort``, ``order`` and ``search``.

    Raises :class:`~inceptools.core.errs.AppError` (400) naming the offending
    parameter.
    """
    options = opt or Options()
    params = request.query_params

    page = _positive_int(params.get("page"), "page", DEFAULT_PAGE)
    limit = _positive_int(params.get("limit"), "limit", options.default_limit or DEFAULT_LIMIT)
    limit = min(limit, options.max_limit or MAX_LIMIT)

    sort = (params.get("sort") or "").strip()
    if sort and sort not in options.sortable:
        message = (
            f"sort must be one of: {', '.join(options.sortable)}"
            if options.sortable
            else "sorting is not supported on this endpoint"
        )
        raise _invalid("sort", message, "oneof", "INVALID_SORT")
    if not sort:
        sort = options.default_sort

    raw_order = (params.get("order") or "").strip().lower()
    if raw_order and raw_order not in ("asc", "desc"):
        raise _invalid("order", "order must be asc or desc", "oneof", "INVALID_SORT")

    if raw_order:
        order: Direction | None = Direction(raw_order)
    elif sort:
        order = options.default_order
    else:
        order = None

    return Query(
        page=page,
        limit=limit,
        sort=sort,
        order=order,
        search=(params.get("search") or "").strip(),
    )
