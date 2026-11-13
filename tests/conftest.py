"""Shared fixtures.

Everything here builds the *real* application. There is no mock of the
framework in this suite: the behaviour worth testing lives in the middleware,
the guards and the exception filter, and a mock of those tests nothing.
"""

from __future__ import annotations

import logging

import pytest
from pydantic import BaseModel, Field

from inceptools import App, Controller, Delete, Get, Module, Patch, Post, errs, guard, testkit
from inceptools.core.di import Injectable


@pytest.fixture(autouse=True)
def _quiet_logging():
    """Keep expected failures out of the test output."""
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


class CreateProduct(BaseModel):
    name: str = Field(min_length=2)
    price: float = Field(ge=0)
    tags: list[str] = Field(default_factory=list)


class UpdateProduct(BaseModel):
    # Optional so a PATCH can distinguish "leave it" from "set it to false".
    name: str | None = None
    archived: bool | None = None


class ProductOut(BaseModel):
    id: int
    name: str
    archived: bool = False


@Injectable
class ProductRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict] = {1: {"id": 1, "name": "Desk lamp", "archived": False}}
        self.next_id = 2
        self.initialised = False
        self.destroyed = False

    async def on_module_init(self) -> None:
        self.initialised = True

    async def on_module_destroy(self) -> None:
        self.destroyed = True


@Injectable
class ProductService:
    def __init__(self, repo: ProductRepo) -> None:
        self.repo = repo

    async def get(self, pid: int) -> dict:
        row = self.repo.rows.get(pid)
        if row is None:
            raise errs.not_found("no such product").with_code("PRODUCT_NOT_FOUND")
        if row["archived"]:
            raise errs.conflict("this product is archived").with_code("PRODUCT_ARCHIVED")
        return row

    async def create(self, dto: CreateProduct) -> dict:
        row = {"id": self.repo.next_id, "name": dto.name, "archived": False}
        self.repo.rows[row["id"]] = row
        self.repo.next_id += 1
        return row

    async def update(self, pid: int, dto: UpdateProduct) -> dict:
        row = await self.get(pid)
        for key, value in dto.model_dump(exclude_unset=True).items():
            row[key] = value
        return row

    async def boom(self) -> None:
        raise ValueError("a driver error nobody classified")


@Controller("/products", tags=["products"])
class ProductController:
    def __init__(self, service: ProductService) -> None:
        self.service = service

    @Get("/{pid}", response_model=ProductOut)
    async def retrieve(self, pid: int):
        """Fetch one product."""
        return await self.service.get(pid)

    @Get("/")
    async def index(self, limit: int = 10):
        return list(self.service.repo.rows.values())[:limit]

    @Post("/", guards=[guard.Tenanted], response_model=ProductOut)
    async def create(self, body: CreateProduct):
        return await self.service.create(body)

    @Patch("/{pid}", guards=[guard.Tenanted])
    async def update(self, pid: int, body: UpdateProduct):
        return await self.service.update(pid, body)

    @Delete("/{pid}", guards=[guard.Tenanted, guard.has_role("catalog:write")])
    async def remove(self, pid: int):
        self.service.repo.rows.pop(pid, None)
        return {"deleted": pid}

    @Post("/signup", public=True)
    async def signup(self):
        return {"ok": True}

    @Get("/crash/now")
    async def crash(self):
        await self.service.boom()


@Module(
    controllers=[ProductController],
    providers=[ProductService, ProductRepo],
    exports=[ProductService],
)
class ProductsModule: ...


@App(modules=[ProductsModule])
class CatalogApp: ...


@pytest.fixture
def client():
    with testkit.build(CatalogApp, prefix="/api/v1") as test_app:
        yield test_app


@pytest.fixture
def tenant(client):
    return client.as_actor("user_1", account_id="acc_1", roles="catalog:write")
