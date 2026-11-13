"""One error type: a kind, a safe message, an internal cause."""

from __future__ import annotations

import pytest

from inceptools.core import errs


@pytest.mark.parametrize(
    ("build", "status"),
    [
        (errs.invalid, 400),
        (errs.unauthorized, 401),
        (errs.payment_required, 402),
        (errs.forbidden, 403),
        (errs.not_found, 404),
        (errs.conflict, 409),
        (errs.precondition_failed, 412),
        (errs.unprocessable, 422),
        (errs.exhausted, 429),
        (errs.internal, 500),
        (errs.not_supported, 501),
        (errs.bad_gateway, 502),
        (errs.unavailable, 503),
        (errs.timeout, 504),
    ],
)
def test_each_kind_maps_to_its_status(build, status):
    assert build("x").status_code == status


def test_builders_return_copies():
    base = errs.conflict("archived")
    tagged = base.with_code("PRODUCT_ARCHIVED").with_meta(product="7")
    assert base.code == "CONFLICT"
    assert base.meta == {}
    assert tagged.code == "PRODUCT_ARCHIVED"


def test_an_internal_message_is_redacted():
    leaky = errs.internal("connection to 10.0.1.7 as root failed")
    assert leaky.client_message == errs.OPAQUE_MESSAGE
    assert "10.0.1.7" not in leaky.as_dict()["message"]


def test_a_deliberate_internal_disclosure_is_allowed():
    assert errs.internal().with_message("the export is still building").client_message == (
        "the export is still building"
    )


def test_the_cause_is_never_serialized():
    error = errs.conflict("taken").with_cause(ValueError("UNIQUE products_sku_key"))
    assert "products_sku_key" not in str(error.as_dict())


def test_an_unclassified_exception_becomes_a_redacted_500():
    converted = errs.from_exception(ValueError("boom"))
    assert converted.status_code == 500
    assert converted.client_message == errs.OPAQUE_MESSAGE
    assert isinstance(converted.cause, ValueError)


def test_an_app_error_passes_through_from_exception():
    original = errs.not_found("gone")
    assert errs.from_exception(original) is original


def test_builtin_exceptions_are_classified():
    assert errs.from_exception(TimeoutError()).status_code == 504
    assert errs.from_exception(PermissionError()).status_code == 403
    assert errs.from_exception(NotImplementedError()).status_code == 501


def test_status_for_error_reports_200_for_success():
    assert errs.status_for_error(None) == 200
    assert errs.status_for_error(errs.conflict("x")) == 409


def test_details_carry_the_client_facing_field_name():
    error = errs.invalid("validation failed").with_details(
        errs.FieldError("price", "price must be 0 or more", "gte")
    )
    assert error.as_dict()["errors"] == [
        {"field": "price", "message": "price must be 0 or more", "rule": "gte"}
    ]


def test_is_kind_and_code_of():
    error = errs.conflict("x").with_code("TAKEN")
    assert errs.is_kind(error, errs.ErrorKind.CONFLICT)
    assert errs.code_of(error) == "TAKEN"
    assert errs.kind_of(ValueError()) is errs.ErrorKind.INTERNAL


def test_aliases_build_the_same_kind():
    assert errs.unauthenticated("x").kind is errs.ErrorKind.UNAUTHORIZED
    assert errs.rate_limited("x").kind is errs.ErrorKind.EXHAUSTED
    assert errs.not_implemented("x").kind is errs.ErrorKind.NOT_SUPPORTED
