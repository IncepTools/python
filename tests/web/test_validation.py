"""A validation failure names the field the client sent."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from inceptools.core import errs
from inceptools.web import binding


class Rule(BaseModel):
    protocol: str


class Payload(BaseModel):
    name: str = Field(min_length=2)
    price: float = Field(ge=0)
    rules: list[Rule] = Field(default_factory=list)


def test_messages_name_the_json_field(client, tenant):
    reply = tenant.post("/api/v1/catalog/products", {"name": "a", "price": -1})
    reply.expect_status(400).expect_code("VALIDATION_FAILED")
    assert set(reply.fields) == {"name", "price"}


def test_a_nested_path_is_rendered_client_side():
    with pytest.raises(errs.AppError) as caught:
        binding.validate(Payload, {"name": "ab", "price": 1, "rules": [{"wrong": 1}]})
    assert "rules[0].protocol" in [d.field for d in caught.value.details]


@pytest.mark.parametrize(
    ("payload", "field", "rule", "message"),
    [
        ({"price": 1}, "name", "required", "name is required"),
        ({"name": "a", "price": 1}, "name", "min", "name must be at least 2 characters"),
        ({"name": "ab", "price": -1}, "price", "gte", "price must be 0 or more"),
        ({"name": "ab", "price": "x"}, "price", "numeric", "price must be a number"),
    ],
)
def test_the_message_vocabulary(payload, field, rule, message):
    with pytest.raises(errs.AppError) as caught:
        binding.validate(Payload, payload)
    detail = next(d for d in caught.value.details if d.field == field)
    assert detail.rule == rule
    assert detail.message == message


def test_dto_hooks_run_in_order():
    order: list[str] = []

    class Hooked(BaseModel):
        name: str = ""

        def normalize(self) -> None:
            order.append("normalize")
            self.name = self.name.strip()

        def set_defaults(self) -> None:
            order.append("set_defaults")
            self.name = self.name or "default"

        def validate_self(self) -> None:
            order.append("validate_self")

    dto = binding.validate(Hooked, {"name": "  "})
    assert order == ["normalize", "set_defaults", "validate_self"]
    assert dto.name == "default"


def test_a_cross_field_rule_reports_as_a_field_error():
    class Range(BaseModel):
        low: int
        high: int

        def validate_self(self) -> None:
            if self.low > self.high:
                raise ValueError("low must not exceed high")

    with pytest.raises(errs.AppError) as caught:
        binding.validate(Range, {"low": 5, "high": 1})
    assert caught.value.details[0].message == "low must not exceed high"


def test_validation_never_leaks_a_pydantic_error(client, tenant):
    reply = tenant.post("/api/v1/catalog/products", {"name": "a", "price": -1})
    assert "pydantic" not in str(reply.body).lower()
    assert "ValidationError" not in str(reply.body)
