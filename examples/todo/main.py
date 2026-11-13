"""In-memory CRUD, tenant scoping, events, and a scheduled purge.

    uv run python examples/todo/main.py
    curl -H 'x-account: acc_1' localhost:8080/api/v1/todo/tasks
    curl -H 'x-account: acc_1' -X POST localhost:8080/api/v1/todo/tasks \
         -d '{"title":"Write the docs"}' -H 'content-type: application/json'

Note what the account header does: without it, every write is a 401 and every
read is a 401 — not an empty list. That is the point of the scoped repository.
"""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from starlette.requests import Request

import inceptools
from inceptools import App, Controller, Delete, Get, Module, Patch, Post, ctxkit, errs, guard
from inceptools.core.di import Injectable
from inceptools.platform import events
from inceptools.platform.scheduler import Job, Scheduler
from inceptools.web import pagination, response

# -- dto -----------------------------------------------------------------


class CreateTask(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    due: dt.date | None = None


class UpdateTask(BaseModel):
    # Optional so a PATCH can tell "leave it alone" from "set it to false".
    title: str | None = None
    done: bool | None = None


class TaskOut(BaseModel):
    id: int
    title: str
    done: bool
    due: dt.date | None = None


# -- entity and events ---------------------------------------------------


@dataclass
class Task:
    id: int
    account_id: str
    title: str
    done: bool = False
    due: dt.date | None = None
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(tz=dt.UTC))


@dataclass
class TaskCompleted:
    task_id: int
    account_id: str


# -- repository ----------------------------------------------------------


@Injectable
class TaskRepo:
    """An in-memory stand-in that still applies the ownership filter.

    Even here the filter is applied in one place rather than at each call
    site: the habit is the point, not the storage.
    """

    def __init__(self) -> None:
        self._rows: dict[int, Task] = {}
        self._ids = itertools.count(1)

    def _mine(self) -> list[Task]:
        account = ctxkit.require_account()
        return [t for t in self._rows.values() if t.account_id == account]

    def add(self, title: str, due: dt.date | None) -> Task:
        task = Task(
            id=next(self._ids),
            account_id=ctxkit.require_account(),  # from the context, not the payload
            title=title,
            due=due,
        )
        self._rows[task.id] = task
        return task

    def get(self, task_id: int) -> Task:
        task = self._rows.get(task_id)
        if task is None or task.account_id != ctxkit.require_account():
            # Not "forbidden": a 403 would confirm the id exists.
            raise errs.not_found("no such task").with_code("TASK_NOT_FOUND")
        return task

    def page(self, query: pagination.Query) -> tuple[list[Task], int]:
        rows = sorted(self._mine(), key=lambda t: t.id)
        return rows[query.offset : query.offset + query.limit], len(rows)

    def remove(self, task_id: int) -> None:
        self._rows.pop(self.get(task_id).id, None)

    def purge_completed(self, older_than: dt.timedelta) -> int:
        cutoff = dt.datetime.now(tz=dt.UTC) - older_than
        stale = [t.id for t in self._rows.values() if t.done and t.created_at < cutoff]
        for task_id in stale:
            self._rows.pop(task_id, None)
        return len(stale)


# -- service -------------------------------------------------------------


@Injectable
class TaskService:
    """No HTTP here. The same methods serve the scheduled purge."""

    def __init__(self, repo: TaskRepo, bus: events.Bus) -> None:
        self.repo = repo
        self.bus = bus

    async def create(self, dto: CreateTask) -> Task:
        return self.repo.add(dto.title, dto.due)

    async def update(self, task_id: int, dto: UpdateTask) -> Task:
        task = self.repo.get(task_id)
        changes = dto.model_dump(exclude_unset=True)
        newly_done = changes.get("done") and not task.done
        for key, value in changes.items():
            setattr(task, key, value)
        if newly_done:
            await self.bus.publish(TaskCompleted(task_id=task.id, account_id=task.account_id))
        return task

    async def purge(self) -> None:
        removed = self.repo.purge_completed(dt.timedelta(days=30))
        if removed:
            print(f"purged {removed} completed task(s)")


# -- controller ----------------------------------------------------------


@Controller("/tasks", tags=["tasks"], guards=[guard.Tenanted])
class TaskController:
    def __init__(self, service: TaskService) -> None:
        self.service = service

    @Get("/")
    async def index(self, request: Request):
        query = pagination.from_request(
            request, pagination.Options(sortable=("id", "title"), default_sort="id")
        )
        rows, total = self.service.repo.page(query)
        return response.listing(
            [TaskOut(**vars(t)).model_dump(mode="json") for t in rows],
            page=query.page,
            page_size=query.limit,
            total=total,
        )

    @Get("/{task_id}", response_model=TaskOut)
    async def show(self, task_id: int) -> Task:
        """Fetch one task."""
        return self.service.repo.get(task_id)

    @Post("/", response_model=TaskOut)
    async def create(self, body: CreateTask) -> Task:
        """Create a task."""
        return await self.service.create(body)

    @Patch("/{task_id}", response_model=TaskOut)
    async def update(self, task_id: int, body: UpdateTask) -> Task:
        return await self.service.update(task_id, body)

    @Delete("/{task_id}")
    async def remove(self, task_id: int) -> dict:
        self.service.repo.remove(task_id)
        return {"deleted": task_id}


@Module(controllers=[TaskController], providers=[TaskService, TaskRepo], exports=[TaskService])
class TasksModule: ...


@App(modules=[TasksModule], name="todo")
class TodoApp: ...


# -- wiring --------------------------------------------------------------

bus = events.Bus()


@bus.subscribe(mode=events.Mode.ASYNC, name="audit")
async def record_completion(event: TaskCompleted) -> None:
    """Runs on a detached context: it keeps the trace, drops the cancellation."""
    print(f"[audit trace={ctxkit.trace_id()}] task {event.task_id} completed")


def identity(request: Request) -> dict:
    """Stand-in for real authentication. A guard still decides access."""
    return {"account_id": request.headers.get("x-account") or None}


app = inceptools.create_app(
    service="todo",
    version="1.0.0",
    prefix="/api/v1",
    middleware=inceptools.ChainOptions(timeout=15.0, identity=identity),
)
app.supply(bus)
app.mount(TodoApp)

scheduler = Scheduler()
scheduler.add(
    Job(
        name="purge-completed-tasks",
        cron="0 3 * * *",  # a typo here fails the boot, not the night it runs
        timeout=600,
        jitter=30,
        run=lambda: app.container.get(TaskService).purge(),
    )
)
app.supply(scheduler)
app.lifecycle.register_object(scheduler, name="scheduler")


if __name__ == "__main__":
    app.run(port=8080)
