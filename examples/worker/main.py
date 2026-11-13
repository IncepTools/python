"""A service with no routes at all.

A module does not have to serve HTTP. This one schedules work and drains a
queue, and still gets the container, the lifecycle, the health endpoints and
graceful shutdown.

    uv run python examples/worker/main.py
    curl localhost:8081/readyz
"""

from __future__ import annotations

import asyncio

import inceptools
from inceptools import App, Module
from inceptools.core.di import Injectable
from inceptools.platform import lock
from inceptools.platform.queue import Queue
from inceptools.platform.scheduler import Job, Scheduler
from inceptools.web import health


@Injectable
class DigestService:
    def __init__(self) -> None:
        self.sent = 0

    async def send_digests(self) -> None:
        await asyncio.sleep(0.1)
        self.sent += 1
        print(f"digest run #{self.sent}")

    async def handle(self, message) -> None:
        print(f"processing {message.body!r} (attempt {message.attempts})")


@Module(providers=[DigestService], exports=[DigestService])
class DigestModule: ...


@App(modules=[DigestModule], name="ops")
class OpsApp: ...


app = inceptools.create_app(service="worker", version="1.0.0")
app.mount(OpsApp)

# The service is resolved at startup, so the handler closes over the container
# rather than over an instance that does not exist yet.
emails = Queue(
    "emails",
    lambda m: app.container.get(DigestService).handle(m),
    workers=4,
    max_attempts=3,
    retry_delay=2.0,
    on_dead_letter=lambda d: print(f"dead letter: {d.message.body!r} ({d.error})"),
)

scheduler = Scheduler(locker=lock.Memory())  # swap for lock.Redis across replicas
scheduler.add(
    Job(
        name="send-digests",
        every="30s",
        singleton=True,  # one replica, via the distributed lock
        timeout=120,
        run=lambda: app.container.get(DigestService).send_digests(),
    )
)

app.supply(emails)
app.supply(scheduler)
app.lifecycle.register_object(emails, name="emails")
app.lifecycle.register_object(scheduler, name="scheduler")
app.health.register(health.of("queue-depth", lambda: None, severity=health.Severity.OPTIONAL))


@app.on_ready
async def seed_some_work() -> None:
    for subject in ("welcome", "weekly-digest"):
        await emails.enqueue(subject)


if __name__ == "__main__":
    app.run(port=8081)
