"""A retried POST must not charge twice."""

from __future__ import annotations

from pydantic import BaseModel

from inceptools import App, Controller, Module, Post, testkit
from inceptools.reliability.idempotency import HEADER, IdempotencyMiddleware, MemoryStore

CHARGES: list[str] = []


class Charge(BaseModel):
    amount: int


@Controller("/charges")
class ChargeController:
    @Post("/", public=True)
    async def create(self, body: Charge) -> dict:
        CHARGES.append(str(body.amount))
        return {"charge_id": len(CHARGES), "amount": body.amount}

    @Post("/boom", public=True)
    async def boom(self) -> dict:
        raise RuntimeError("the gateway exploded")


@Module(controllers=[ChargeController])
class ChargesModule: ...


@App(modules=[ChargesModule], name="billing")
class BillingApp: ...


def build():
    CHARGES.clear()
    app = testkit.build(BillingApp)
    app.app.fastapi.add_middleware(IdempotencyMiddleware, store=MemoryStore(), ttl=60)
    return app


def test_a_repeated_key_replays_the_first_response():
    with build() as client:
        first = client.post("/billing/charges", {"amount": 10}, headers={HEADER: "k1"})
        second = client.post("/billing/charges", {"amount": 10}, headers={HEADER: "k1"})
        first.expect_status(201)
        assert second.status == first.status
        assert second.data == first.data
        assert CHARGES == ["10"]
        second.expect_header("idempotent-replay", "true")


def test_a_different_key_runs_again():
    with build() as client:
        client.post("/billing/charges", {"amount": 10}, headers={HEADER: "k1"})
        client.post("/billing/charges", {"amount": 10}, headers={HEADER: "k2"})
        assert CHARGES == ["10", "10"]


def test_the_same_key_with_a_different_body_is_refused():
    with build() as client:
        client.post("/billing/charges", {"amount": 10}, headers={HEADER: "k1"})
        reply = client.post("/billing/charges", {"amount": 99}, headers={HEADER: "k1"})
        reply.expect_status(422).expect_code("IDEMPOTENCY_KEY_REUSED")
        assert CHARGES == ["10"]


def test_no_key_means_no_protection():
    with build() as client:
        client.post("/billing/charges", {"amount": 10})
        client.post("/billing/charges", {"amount": 10})
        assert CHARGES == ["10", "10"]


def test_a_server_error_is_not_replayed():
    """The retry should actually retry, not replay a 500 for a day."""
    store = MemoryStore()
    app = testkit.build(BillingApp)
    app.app.fastapi.add_middleware(IdempotencyMiddleware, store=store, ttl=60)
    with app as client:
        client.post("/billing/charges/boom", headers={HEADER: "k1"}).expect_status(500)
        client.post("/billing/charges/boom", headers={HEADER: "k1"}).expect_status(500)


def test_a_required_key_is_enforced():
    app = testkit.build(BillingApp)
    app.app.fastapi.add_middleware(IdempotencyMiddleware, store=MemoryStore(), required=True)
    with app as client:
        client.post("/billing/charges", {"amount": 1}).expect_status(400).expect_code(
            "IDEMPOTENCY_KEY_REQUIRED"
        )
