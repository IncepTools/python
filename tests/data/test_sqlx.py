"""Driver errors become the application's vocabulary."""

from __future__ import annotations

import pytest

from inceptools.data import sqlx


@pytest.mark.parametrize(
    ("message", "status", "code"),
    [
        ('duplicate key value violates unique constraint "products_sku_key"', 409, "DUPLICATE"),
        ("UNIQUE constraint failed: products.sku", 409, "DUPLICATE"),
        ("violates foreign key constraint", 409, "FOREIGN_KEY"),
        ("null value violates not-null constraint", 400, "NOT_NULL"),
        ("new row violates check constraint", 400, "CHECK_FAILED"),
        ("deadlock detected", 409, sqlx.CONCURRENT_MODIFICATION),
        ("connection refused", 503, "DB_UNAVAILABLE"),
        ("canceling statement due to statement timeout", 504, "DB_TIMEOUT"),
        ("something nobody has mapped", 500, "INTERNAL_ERROR"),
    ],
)
def test_translation(message, status, code):
    error = sqlx.translate(Exception(message))
    assert (error.status_code, error.code) == (status, code)


@pytest.mark.parametrize(
    ("message", "constraint"),
    [
        ('duplicate key value violates unique constraint "products_sku_key"', "products_sku_key"),
        ("Duplicate entry 'abc' for key 'products.sku_unique'", "products.sku_unique"),
        ("UNIQUE constraint failed: products.sku", "products.sku"),
    ],
)
def test_the_constraint_name_is_extracted(message, constraint):
    assert sqlx.translate(Exception(message)).meta["constraint"] == constraint


def test_an_unmapped_error_is_redacted():
    error = sqlx.translate(Exception("password authentication failed for user 'root'"))
    assert "root" not in error.client_message
    assert error.cause is not None


def test_an_app_error_passes_through():
    from inceptools.core import errs

    original = errs.conflict("already archived")
    assert sqlx.translate(original) is original


async def test_a_transaction_does_not_relabel_an_application_exception(sessions):
    """`except PaymentDeclined` upstream must keep matching."""

    class PaymentDeclined(Exception): ...

    with pytest.raises(PaymentDeclined):
        async with sqlx.session_scope(sessions) as session, sqlx.transaction(session):
            raise PaymentDeclined("card refused")


async def test_in_transaction_reports_the_truth(sessions):
    async with sqlx.session_scope(sessions) as session:
        assert not sqlx.in_transaction()
        async with sqlx.transaction(session):
            assert sqlx.in_transaction()
        assert not sqlx.in_transaction()


async def test_nesting_reuses_the_outer_transaction(sessions):
    """A service calling another must not commit half of it."""
    # Deliberately nested: the nesting is what is under test.
    async with sqlx.session_scope(sessions) as session, sqlx.transaction(session) as outer:  # noqa: SIM117
        async with sqlx.transaction(session) as inner:
            assert inner is outer


async def test_a_transaction_needs_a_session():
    from inceptools.core import errs

    with pytest.raises(errs.AppError, match="no database session"):
        async with sqlx.transaction():
            pass
