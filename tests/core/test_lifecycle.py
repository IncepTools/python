"""Init, ready, reload, shutdown — reverse order, real deadline."""

from __future__ import annotations

import asyncio

import pytest

from inceptools.core.lifecycle import Lifecycle, Phase


async def test_phases_run_in_registration_order():
    order: list[str] = []
    cycle = Lifecycle()
    cycle.on_init(lambda: order.append("init-1"))
    cycle.on_init(lambda: order.append("init-2"))
    cycle.on_ready(lambda: order.append("ready"))
    await cycle.start()
    assert order == ["init-1", "init-2", "ready"]


async def test_shutdown_runs_in_reverse():
    order: list[str] = []
    cycle = Lifecycle()
    cycle.on_shutdown(lambda: order.append("engine"))
    cycle.on_shutdown(lambda: order.append("repo"))
    await cycle.shutdown()
    assert order == ["repo", "engine"]


async def test_init_fails_fast():
    ran: list[str] = []
    cycle = Lifecycle()
    cycle.on_init(lambda: (_ for _ in ()).throw(RuntimeError("no DSN")))
    cycle.on_init(lambda: ran.append("second"))
    with pytest.raises(RuntimeError, match="no DSN"):
        await cycle.start()
    assert ran == []


async def test_shutdown_does_not_stop_at_a_failure():
    ran: list[str] = []
    cycle = Lifecycle()
    cycle.on_shutdown(lambda: ran.append("first"))
    cycle.on_shutdown(lambda: (_ for _ in ()).throw(RuntimeError("stuck")))
    failures = await cycle.shutdown()
    assert ran == ["first"]
    assert len(failures) == 1


async def test_a_hanging_shutdown_hook_cannot_hold_the_process():
    cycle = Lifecycle(drain_timeout=0.05)

    async def hangs() -> None:
        await asyncio.sleep(10)

    cycle.on_shutdown(hangs)
    failures = await asyncio.wait_for(cycle.shutdown(), timeout=2)
    assert failures and isinstance(failures[0].error, TimeoutError)


async def test_reload_never_fails_fast():
    ran: list[str] = []
    cycle = Lifecycle()
    cycle.on_reload(lambda: (_ for _ in ()).throw(RuntimeError("bad config")))
    cycle.on_reload(lambda: ran.append("second"))
    failures = await cycle.reload()
    assert ran == ["second"]
    assert len(failures) == 1


async def test_register_object_wires_the_protocols():
    seen: list[str] = []

    class Component:
        async def on_module_init(self) -> None:
            seen.append("init")

        async def on_application_bootstrap(self) -> None:
            seen.append("bootstrap")

        async def before_application_shutdown(self) -> None:
            seen.append("before_shutdown")

        async def on_application_shutdown(self) -> None:
            seen.append("shutdown")

    cycle = Lifecycle()
    cycle.register_object(Component())
    await cycle.start()
    await cycle.before_shutdown()
    await cycle.shutdown()
    assert seen == ["init", "bootstrap", "before_shutdown", "shutdown"]
    assert cycle.phase is Phase.SHUTDOWN


async def test_on_module_destroy_is_left_to_the_container():
    """Registering it here too would call it twice."""
    seen: list[str] = []

    class Component:
        async def on_module_destroy(self) -> None:
            seen.append("destroy")

    cycle = Lifecycle()
    cycle.register_object(Component())
    await cycle.before_shutdown()
    await cycle.shutdown()
    assert seen == []
