"""Locks release only their own; schedulers skip rather than queue."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from inceptools.core import errs
from inceptools.platform import lock
from inceptools.platform.scheduler import DEFAULT_TIMEOUT, Job, Scheduler, cron


async def test_a_second_acquire_is_refused():
    locker = lock.Memory()
    await locker.acquire("k")
    with pytest.raises(lock.NotAcquired):
        await locker.acquire("k")


async def test_release_frees_the_key():
    locker = lock.Memory()
    held = await locker.acquire("k")
    await held.release()
    assert (await locker.acquire("k")).key == "k"


async def test_releasing_a_lock_you_no_longer_hold_is_a_no_op():
    """The whole point of the token: never delete the next holder's lock."""
    locker = lock.Memory()
    first = await locker.acquire("k", ttl=0.01)
    await asyncio.sleep(0.02)  # first's TTL lapses
    second = await locker.acquire("k")
    await first.release()
    with pytest.raises(lock.NotAcquired):
        await locker.acquire("k")
    await second.release()


async def test_not_getting_the_lock_is_not_an_error():
    locker = lock.Memory()
    await locker.acquire("job:x")
    ran, result = await lock.run_locked(locker, "job:x", lambda: asyncio.sleep(0, "done"))
    assert (ran, result) == (False, None)


async def test_run_locked_releases_even_on_failure():
    locker = lock.Memory()

    async def failing():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await lock.run_locked(locker, "k", failing)
    await asyncio.sleep(0)
    assert (await locker.acquire("k")).key == "k"


async def test_the_noop_locker_always_succeeds():
    assert (await lock.Noop().acquire("k")).token == "noop"


def test_a_bad_cron_expression_fails_at_registration():
    scheduler = Scheduler()
    with pytest.raises(errs.AppError, match="invalid cron expression"):
        scheduler.add(Job(name="nightly", cron="0 3 * *", run=lambda: None))


def test_a_job_must_set_exactly_one_trigger():
    scheduler = Scheduler()
    with pytest.raises(errs.AppError, match="exactly one of cron or every"):
        scheduler.add(Job(name="j", cron="* * * * *", every=5, run=lambda: None))
    with pytest.raises(errs.AppError, match="exactly one of cron or every"):
        scheduler.add(Job(name="j", run=lambda: None))


def test_a_singleton_job_needs_a_locker():
    with pytest.raises(errs.AppError, match="no locker"):
        Scheduler().add(Job(name="j", every=5, singleton=True, run=lambda: None))


def test_two_jobs_cannot_share_a_name():
    scheduler = Scheduler()
    scheduler.add(Job(name="j", every=5, run=lambda: None))
    with pytest.raises(errs.AppError, match="two jobs named"):
        scheduler.add(Job(name="j", every=5, run=lambda: None))


def test_a_job_without_a_timeout_gets_one():
    """Zero is not unlimited."""
    scheduler = Scheduler()
    job = Job(name="j", every=5, run=lambda: None)
    scheduler.add(job)
    assert job.timeout == DEFAULT_TIMEOUT
    assert job.lock_ttl == DEFAULT_TIMEOUT


async def test_an_interval_job_fires():
    scheduler = Scheduler()
    ticks: list[int] = []
    scheduler.add(Job(name="tick", every=0.02, run=lambda: ticks.append(1)))
    await scheduler.start()
    await asyncio.sleep(0.09)
    await scheduler.stop()
    assert len(ticks) >= 2


async def test_an_overlapping_run_is_skipped_not_queued():
    """Queueing a slow job behind itself turns a slowdown into a backlog."""
    scheduler = Scheduler()
    started: list[int] = []

    async def slow() -> None:
        started.append(1)
        await asyncio.sleep(0.2)

    scheduler.add(Job(name="slow", every=0.02, run=slow))
    await scheduler.start()
    await asyncio.sleep(0.12)
    await scheduler.stop()
    assert len(started) == 1


async def test_run_on_start_fires_immediately():
    scheduler = Scheduler()
    ran: list[int] = []
    scheduler.add(Job(name="boot", every=60, run_on_start=True, run=lambda: ran.append(1)))
    await scheduler.start()
    await asyncio.sleep(0.02)
    await scheduler.stop()
    assert ran == [1]


async def test_a_failing_job_does_not_stop_the_schedule():
    scheduler = Scheduler()
    attempts: list[int] = []

    def failing() -> None:
        attempts.append(1)
        raise RuntimeError("boom")

    scheduler.add(Job(name="bad", every=0.02, run=failing))
    await scheduler.start()
    await asyncio.sleep(0.09)
    await scheduler.stop()
    assert len(attempts) >= 2


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0 3 * * *", dt.datetime(2026, 11, 14, 3, 0)),
        ("*/15 * * * *", dt.datetime(2026, 11, 13, 10, 45)),
        ("@daily", dt.datetime(2026, 11, 14, 0, 0)),
        ("0 9 * * mon-fri", dt.datetime(2026, 11, 16, 9, 0)),
        ("0 0 1 jan *", dt.datetime(2027, 1, 1, 0, 0)),
    ],
)
def test_cron_next_occurrence(expression, expected):
    """2026-11-13 is a Friday, so mon-fri next fires on Monday the 16th."""
    assert cron.parse(expression).next_after(dt.datetime(2026, 11, 13, 10, 30)) == expected


@pytest.mark.parametrize("bad", ["0 3 * *", "99 * * * *", "@nope", "0 3 * * xyz", ""])
def test_cron_rejects_nonsense(bad):
    with pytest.raises(cron.CronError):
        cron.parse(bad)


def test_every_parses_durations():
    assert cron.parse_every("@every 1h30m") == 5400
    assert cron.parse_every("30s") == 30
    with pytest.raises(cron.CronError):
        cron.parse_every("soon")
