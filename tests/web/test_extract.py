"""Typed extraction that cannot silently yield a zero value."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from starlette.datastructures import QueryParams

from inceptools.core import errs
from inceptools.web import extract


class FakeRequest:
    def __init__(self, query: str = "", **path: object) -> None:
        self.query_params = QueryParams(query)
        self.path_params = path


# -- required path parameters ---------------------------------------------


def test_string_param_is_trimmed():
    assert extract.string_param(FakeRequest(sku="  abc  "), "sku") == "abc"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_missing_required_param_is_refused(value):
    with pytest.raises(errs.AppError) as caught:
        extract.string_param(FakeRequest(sku=value), "sku")
    assert caught.value.code == "INVALID_PARAMETER"
    assert caught.value.details[0].rule == "required"
    assert caught.value.details[0].field == "sku"


def test_uuid_param():
    identifier = uuid.uuid4()
    assert extract.uuid_param(FakeRequest(pid=str(identifier)), "pid") == identifier


def test_a_bad_uuid_param_names_the_field():
    with pytest.raises(errs.AppError) as caught:
        extract.uuid_param(FakeRequest(pid="not-a-uuid"), "pid")
    assert caught.value.details[0].message == "pid must be a valid UUID"
    assert caught.value.details[0].rule == "uuid"


def test_int_param():
    assert extract.int_param(FakeRequest(page="7"), "page") == 7
    with pytest.raises(errs.AppError, match="whole number"):
        extract.int_param(FakeRequest(page="seven"), "page")


# -- optional query parameters --------------------------------------------


def test_uuid_query_is_optional():
    """Absent means None, not an error — that is the difference from a param."""
    assert extract.uuid_query(FakeRequest(""), "owner") is None
    identifier = uuid.uuid4()
    assert extract.uuid_query(FakeRequest(f"owner={identifier}"), "owner") == identifier


def test_a_bad_uuid_query_is_still_refused():
    with pytest.raises(errs.AppError, match="valid UUID"):
        extract.uuid_query(FakeRequest("owner=nope"), "owner")


def test_int_query_uses_its_default():
    assert extract.int_query(FakeRequest(""), "limit", 20) == 20
    assert extract.int_query(FakeRequest("limit=5"), "limit", 20) == 5
    with pytest.raises(errs.AppError, match="whole number"):
        extract.int_query(FakeRequest("limit=many"), "limit")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
    ],
)
def test_bool_query_accepts_the_usual_spellings(raw, expected):
    assert extract.bool_query(FakeRequest(f"active={raw}"), "active") is expected


def test_bool_query_default_and_refusal():
    assert extract.bool_query(FakeRequest(""), "active", True) is True
    with pytest.raises(errs.AppError, match="true or false"):
        extract.bool_query(FakeRequest("active=maybe"), "active")


def test_enum_query():
    assert extract.enum_query(FakeRequest(""), "status", "draft", "live") == ""
    assert extract.enum_query(FakeRequest("status=live"), "status", "draft", "live") == "live"
    with pytest.raises(errs.AppError, match="must be one of: draft, live"):
        extract.enum_query(FakeRequest("status=deleted"), "status", "draft", "live")


def test_datetime_query_accepts_rfc3339():
    found = extract.datetime_query(FakeRequest("since=2026-11-13T10:30:00Z"), "since")
    assert found == dt.datetime(2026, 11, 13, 10, 30, tzinfo=dt.UTC)
    assert extract.datetime_query(FakeRequest(""), "since") is None
    with pytest.raises(errs.AppError, match="RFC 3339"):
        extract.datetime_query(FakeRequest("since=yesterday"), "since")


def test_csv_query_never_fails():
    assert extract.csv_query(FakeRequest("tags=a, b ,,c"), "tags") == ["a", "b", "c"]
    assert extract.csv_query(FakeRequest(""), "tags") == []
