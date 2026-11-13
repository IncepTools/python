"""Liveness and readiness are different questions."""

from __future__ import annotations

import asyncio

from inceptools.web import health


def test_livez_checks_nothing(client):
    reply = client.get("/livez")
    reply.expect_status(200)
    assert reply.body == {"status": "UP"}


def test_healthz_is_the_compatibility_alias(client):
    assert client.get("/healthz").body == {"status": "UP"}


def test_readyz_reports_the_detail(client):
    reply = client.get("/readyz")
    reply.expect_status(200)
    assert reply.body["service"] == "test"


async def test_a_failing_critical_check_is_down():
    registry = health.Registry("shop", "1.0")
    registry.register(health.of("database", lambda: (_ for _ in ()).throw(OSError("refused"))))
    report = await registry.run()
    assert report.status is health.Status.DOWN
    assert report.checks["database"].message == "refused"


async def test_a_failing_optional_check_only_degrades():
    registry = health.Registry("shop", "1.0")
    registry.register(health.of("database", lambda: None))
    registry.register(
        health.of(
            "cache",
            lambda: (_ for _ in ()).throw(OSError("down")),
            severity=health.Severity.OPTIONAL,
        )
    )
    report = await registry.run()
    assert report.status is health.Status.DEGRADED
    assert report.checks["cache"].as_dict()["severity"] == "optional"


async def test_degraded_still_answers_200(client):
    """A dead optional cache is not a reason to leave the load balancer."""
    client.app.health.register(
        health.of(
            "cache",
            lambda: (_ for _ in ()).throw(OSError("down")),
            severity=health.Severity.OPTIONAL,
        )
    )
    reply = client.get("/readyz")
    reply.expect_status(200)
    assert reply.body["status"] == "DEGRADED"


async def test_down_answers_503(client):
    client.app.health.register(
        health.of("database", lambda: (_ for _ in ()).throw(OSError("refused")))
    )
    client.get("/readyz").expect_status(503)
    # /health is for humans and always answers.
    client.get("/health").expect_status(200)


async def test_a_hanging_check_cannot_hang_the_probe():
    registry = health.Registry("shop", "1.0", timeout=0.05)

    async def hangs() -> None:
        await asyncio.sleep(10)

    registry.register(health.of("slow", hangs))
    report = await asyncio.wait_for(registry.run(), timeout=2)
    assert report.status is health.Status.DOWN
    assert "timed out" in report.checks["slow"].message


async def test_checks_run_concurrently():
    registry = health.Registry("shop", "1.0")

    async def slow() -> None:
        await asyncio.sleep(0.05)

    for name in ("a", "b", "c", "d"):
        registry.register(health.of(name, slow))

    loop = asyncio.get_running_loop()
    started = loop.time()
    await registry.run()
    assert loop.time() - started < 0.15
