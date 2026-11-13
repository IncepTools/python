"""Pipes transform and validate before the handler sees the value."""

from __future__ import annotations

import enum
import uuid

import pytest
from pydantic import BaseModel

from inceptools.core import errs
from inceptools.web.pipes import (
    DefaultValue,
    ParseBool,
    ParseEnum,
    ParseFloat,
    ParseInt,
    ParseList,
    ParseUUID,
    Piped,
    Trim,
    ValueMeta,
    apply_pipes,
    pipe,
)

META = ValueMeta(name="limit", source="query")


def test_parse_int_within_bounds():
    assert ParseInt().transform("42", META) == 42
    assert ParseInt(minimum=1, maximum=100).transform(50, META) == 50
    assert ParseInt().transform(None, META) is None


@pytest.mark.parametrize(
    ("kwargs", "value", "rule"),
    [({}, "x", "numeric"), ({"minimum": 10}, "1", "gte"), ({"maximum": 10}, "99", "lte")],
)
def test_parse_int_refusals_name_the_rule(kwargs, value, rule):
    with pytest.raises(errs.AppError) as caught:
        ParseInt(**kwargs).transform(value, META)
    assert caught.value.details[0].rule == rule
    assert caught.value.details[0].field == "limit"


def test_parse_float():
    assert ParseFloat().transform("1.5", META) == 1.5
    with pytest.raises(errs.AppError, match="must be a number"):
        ParseFloat().transform("x", META)


def test_parse_bool_passes_through_a_real_bool():
    assert ParseBool().transform(True, META) is True
    assert ParseBool().transform(" YES ", META) is True
    with pytest.raises(errs.AppError, match="true or false"):
        ParseBool().transform("perhaps", META)


def test_parse_uuid():
    identifier = uuid.uuid4()
    assert ParseUUID().transform(str(identifier), META) == identifier
    assert ParseUUID().transform(identifier, META) is identifier
    with pytest.raises(errs.AppError, match="valid UUID"):
        ParseUUID().transform("nope", META)


def test_parse_enum_allows_a_real_enum_through():
    class Status(enum.Enum):
        LIVE = "live"

    assert ParseEnum("live").transform(Status.LIVE, META) is Status.LIVE
    assert ParseEnum("live", "draft").transform("draft", META) == "draft"
    with pytest.raises(errs.AppError, match="must be one of: live, draft"):
        ParseEnum("live", "draft").transform("deleted", META)


def test_parse_list_splits_and_pipes_each_item():
    assert ParseList().transform("a, b ,c", META) == ["a", "b", "c"]
    assert ParseList(ParseInt()).transform("1,2,3", META) == [1, 2, 3]
    assert ParseList().transform(["a"], META) == ["a"]
    assert ParseList().transform(None, META) is None


def test_default_value_only_fills_an_absence():
    assert DefaultValue(20).transform(None, META) == 20
    assert DefaultValue(20).transform(0, META) == 0


class Nested(BaseModel):
    label: str


class Payload(BaseModel):
    name: str
    tags: list[str]
    nested: Nested


def test_trim_walks_models_lists_and_dicts():
    """' ada@example.com ' passes an email rule and then matches no row."""
    payload = Payload(name="  Ada  ", tags=["  x  "], nested=Nested(label="  y  "))
    trimmed = Trim().transform(payload, META)
    assert trimmed.name == "Ada"
    assert trimmed.tags == ["x"]
    assert trimmed.nested.label == "y"
    assert Trim().transform({"a": "  b  "}, META) == {"a": "b"}


def test_trim_can_turn_an_empty_string_into_none():
    assert Trim(empty_to_none=True).transform("   ", META) is None
    assert Trim().transform("   ", META) == ""


def test_trim_leaves_non_text_alone():
    assert Trim().transform(7, META) == 7


def test_a_custom_pipe():
    clamp = pipe("clamp", lambda value, meta: max(1, min(100, value)))
    assert clamp.transform(500, META) == 100
    assert "clamp" in repr(clamp)


def test_pipes_apply_in_order():
    assert apply_pipes(" 7 ", [Trim(), ParseInt()], META) == 7


def test_piped_collects_its_pipes():
    marker = Piped(Trim(), ParseInt())
    assert len(marker.pipes) == 2
