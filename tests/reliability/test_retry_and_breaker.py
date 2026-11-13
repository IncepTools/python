"""Retry only what is safe to repeat; fail fast on a dependency that is down."""

from __future__ import annotations

import asyncio

import pytest

from inceptools.core import errs
from inceptools.reliability import breaker, retry


async def test_a_transient_failure_is_retried():
    attempts: list[int] = []

    async def flaky() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise errs.unavailable("down")
        return "ok"

    assert await retry.call(flaky, retry.Policy(base_delay=0.001)) == "ok"
    assert len(attempts) == 3


async def test_an_unclassified_error_is_not_retried():
    """Retrying something nobody reasoned about turns one failure into three."""
    attempts: list[int] = []

    async def broken() -> None:
        attempts.append(1)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await retry.call(broken, retry.Policy(base_delay=0.001))
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (errs.unavailable("x"), True),
        (errs.timeout("x"), True),
        (errs.conflict("x").with_code(retry.CONCURRENT_MODIFICATION), True),
        (errs.conflict("x").with_code("SKU_TAKEN"), False),
        (errs.not_found("x"), False),
        (errs.invalid("x"), False),
        (ValueError("x"), False),
    ],
)
def test_classification(error, retried):
    assert retry.is_retryable(error) is retried


async def test_the_last_failure_is_what_is_raised():
    errors = [errs.unavailable("first"), errs.unavailable("last")]

    async def failing() -> None:
        raise errors.pop(0)

    with pytest.raises(errs.AppError, match="last"):
        await retry.call(failing, retry.Policy(max_attempts=2, base_delay=0.001))


async def test_max_attempts_of_one_disables_retrying():
    attempts: list[int] = []

    async def failing() -> None:
        attempts.append(1)
        raise errs.unavailable("down")

    with pytest.raises(errs.AppError):
        await retry.call(failing, retry.Policy(max_attempts=1))
    assert len(attempts) == 1


def test_backoff_doubles_and_is_capped():
    policy = retry.Policy(base_delay=0.1, max_delay=1.0, jitter=False)
    assert [policy.wait_for(n) for n in (1, 2, 3, 10)] == [0.1, 0.2, 0.4, 1.0]


def test_jitter_spreads_the_retries():
    """Without it, everything that failed together retries together."""
    policy = retry.Policy(base_delay=1.0, max_delay=10.0)
    waits = {policy.wait_for(1) for _ in range(50)}
    assert len(waits) > 10
    assert all(0.5 <= w <= 1.5 for w in waits)


async def test_the_breaker_opens_after_the_threshold():
    circuit = breaker.Breaker("payments", threshold=2, cooldown=10)

    async def down() -> None:
        raise errs.unavailable("down")

    for _ in range(2):
        with pytest.raises(errs.AppError):
            await circuit.call(down)
    assert circuit.state is breaker.State.OPEN

    with pytest.raises(errs.AppError) as caught:
        await circuit.call(down)
    assert caught.value.code == "CIRCUIT_OPEN"
    assert caught.value.status_code == 503


async def test_a_404_from_a_dependency_does_not_open_the_breaker():
    """A healthy answer that happens to be negative is not a failure."""
    circuit = breaker.Breaker("search", threshold=2)

    async def missing() -> None:
        raise errs.not_found("no such document")

    for _ in range(5):
        with pytest.raises(errs.AppError):
            await circuit.call(missing)
    assert circuit.state is breaker.State.CLOSED


async def test_one_probe_is_allowed_after_the_cooldown():
    circuit = breaker.Breaker("payments", threshold=1, cooldown=0.01)

    async def down() -> None:
        raise errs.unavailable("down")

    with pytest.raises(errs.AppError):
        await circuit.call(down)
    await asyncio.sleep(0.02)

    async def up() -> str:
        return "ok"

    assert await circuit.call(up) == "ok"
    assert circuit.state is breaker.State.CLOSED


async def test_a_failed_probe_reopens_the_circuit():
    circuit = breaker.Breaker("payments", threshold=1, cooldown=0.01)

    async def down() -> None:
        raise errs.unavailable("down")

    with pytest.raises(errs.AppError):
        await circuit.call(down)
    await asyncio.sleep(0.02)
    with pytest.raises(errs.AppError):
        await circuit.call(down)
    assert circuit.state is breaker.State.OPEN


async def test_state_changes_are_reported():
    seen: list[tuple[str, str]] = []
    circuit = breaker.Breaker(
        "x", threshold=1, on_state_change=lambda a, b: seen.append((a.value, b.value))
    )

    async def down() -> None:
        raise errs.unavailable("down")

    with pytest.raises(errs.AppError):
        await circuit.call(down)
    assert seen == [("closed", "open")]
