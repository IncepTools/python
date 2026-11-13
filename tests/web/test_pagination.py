"""Bounded pages with an allow-listed sort."""

from __future__ import annotations

import pytest
from starlette.datastructures import QueryParams

from inceptools.core import errs
from inceptools.web import pagination


class FakeRequest:
    def __init__(self, query: str) -> None:
        self.query_params = QueryParams(query)


def read(query: str, **options) -> pagination.Query:
    return pagination.from_request(FakeRequest(query), pagination.Options(**options))


def test_defaults():
    page = read("")
    assert (page.page, page.limit, page.sort) == (1, 20, "")


def test_offset_and_pages():
    assert read("page=3&limit=20").offset == 40
    assert pagination.pages_for(134, 50) == 3
    assert pagination.pages_for(0, 50) == 0


def test_an_oversized_limit_is_clamped_not_rejected():
    assert read("limit=1000000", max_limit=100).limit == 100


@pytest.mark.parametrize("query", ["page=0", "page=-1", "page=abc", "limit=0"])
def test_a_nonsense_page_is_refused(query):
    with pytest.raises(errs.AppError) as caught:
        read(query)
    assert caught.value.code == "INVALID_PAGINATION"


def test_sorting_is_refused_when_nothing_is_sortable():
    with pytest.raises(errs.AppError) as caught:
        read("sort=password")
    assert caught.value.code == "INVALID_SORT"
    assert "not supported" in caught.value.details[0].message


def test_only_an_allow_listed_column_reaches_order_by():
    with pytest.raises(errs.AppError, match="sort must be one of"):
        read("sort=password", sortable=("created_at", "price"))
    assert read("sort=price&order=desc", sortable=("price",)).order_by() == "price DESC"


def test_a_sort_without_an_order_defaults_to_ascending():
    assert read("sort=price", sortable=("price",)).order_by() == "price ASC"


def test_a_bad_order_is_refused():
    with pytest.raises(errs.AppError, match="order must be asc or desc"):
        read("sort=price&order=sideways", sortable=("price",))


def test_search_is_trimmed():
    assert read("search=  lamp  ").search == "lamp"
