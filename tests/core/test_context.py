"""Request identity travels; cancellation does not."""

from __future__ import annotations

import asyncio

import pytest

from inceptools.core import ctxkit, errs


def test_an_empty_context_is_safe_to_read():
    assert ctxkit.current().account_id is None
    assert ctxkit.request_id() == ""


def test_use_scopes_the_context():
    with ctxkit.use(account_id="acc_1"):
        assert ctxkit.account_id() == "acc_1"
    assert ctxkit.account_id() is None


def test_bind_merges_without_scoping():
    with ctxkit.use(trace_id="t1"):
        ctxkit.bind(account_id="acc_1")
        assert ctxkit.trace_id() == "t1"
        assert ctxkit.account_id() == "acc_1"


def test_require_account_refuses_an_anonymous_context():
    with pytest.raises(errs.AppError) as caught:
        ctxkit.require_account()
    assert caught.value.code == ctxkit.NO_TENANT
    assert caught.value.status_code == 401


def test_roles_are_checked_by_membership():
    with ctxkit.use(roles={"admin", "support"}):
        assert ctxkit.has_role("admin")
        assert not ctxkit.has_role("billing")


async def test_identity_survives_a_detached_task():
    """The point of detach: the audit write outlives the request."""
    seen: list[str | None] = []

    async def background() -> None:
        await asyncio.sleep(0.01)
        seen.append(ctxkit.account_id())

    with ctxkit.use(account_id="acc_1", trace_id="t1"):
        task = ctxkit.spawn(background)
    await task
    assert seen == ["acc_1"]


async def test_a_detached_task_is_not_cancelled_with_its_caller():
    finished: list[bool] = []

    async def background() -> None:
        await asyncio.sleep(0.05)
        finished.append(True)

    async def request() -> asyncio.Task:
        return ctxkit.spawn(background)

    task = await request()
    await asyncio.sleep(0.1)
    assert finished == [True]
    assert not task.cancelled()


def test_detach_drops_the_route_but_keeps_the_trace():
    with ctxkit.use(trace_id="t1", route="/api/v1/orders", account_id="acc_1"):
        detached = ctxkit.detach()
    assert detached.trace_id == "t1"
    assert detached.account_id == "acc_1"
    assert detached.route == ""


def test_log_fields_omit_empties():
    with ctxkit.use(trace_id="t1", account_id="acc_1"):
        assert ctxkit.current().log_fields() == {"trace_id": "t1", "account_id": "acc_1"}
