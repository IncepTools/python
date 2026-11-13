"""``Idempotency-Key``, so a retried POST does not charge twice.

A client that times out does not know whether the charge happened. It retries.
Without this, it is charged twice, and the second charge is indistinguishable
from a legitimate second purchase.

With a key, the first request runs and its **response** is stored; a repeat of
the same key replays that stored response rather than running the handler
again.

Three cases are handled separately, and the third is the one people miss:

* **Replay** — the first request finished. Return what it returned.
* **In progress** — the first request is still running. Answer 409 rather than
  running a second copy concurrently; the client should retry shortly.
* **Different body, same key** — a client bug or an attack. Answer 422 rather
  than returning a response that describes a different request.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from inceptools.core import errs
from inceptools.web import response as response_mod

__all__ = [
    "HEADER",
    "IdempotencyMiddleware",
    "MemoryStore",
    "Record",
    "RedisStore",
    "Store",
    "fingerprint",
]

_log = logging.getLogger("inceptools.idempotency")

HEADER = "idempotency-key"
#: Only methods that create or charge something need this.
DEFAULT_METHODS = frozenset({"POST", "PATCH", "PUT"})


def fingerprint(method: str, path: str, body: bytes) -> str:
    """A hash of what was asked, so the same key with a different body is caught."""
    digest = hashlib.sha256()
    digest.update(method.encode())
    digest.update(b"\0")
    digest.update(path.encode())
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


@dataclass(slots=True)
class Record:
    """What the first request did."""

    fingerprint: str
    #: None while the first request is still running.
    status_code: int | None = None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def completed(self) -> bool:
        return self.status_code is not None

    def to_json(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "status_code": self.status_code,
                "body": self.body.decode("utf-8", "replace"),
                "headers": self.headers,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> Record:
        data = json.loads(raw)
        return cls(
            fingerprint=data["fingerprint"],
            status_code=data.get("status_code"),
            body=(data.get("body") or "").encode("utf-8"),
            headers=data.get("headers") or {},
            created_at=data.get("created_at", time.time()),
        )


@runtime_checkable
class Store(Protocol):
    async def claim(self, key: str, record: Record, ttl: float) -> Record | None:
        """Store *record* if the key is free. Returns the existing one otherwise."""

    async def complete(self, key: str, record: Record, ttl: float) -> None: ...

    async def release(self, key: str) -> None:
        """Drop a claim whose request failed, so a retry is not stuck at 409."""


class MemoryStore:
    """Per process. Correct only for a single replica."""

    def __init__(self) -> None:
        self._records: dict[str, tuple[Record, float]] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str, record: Record, ttl: float) -> Record | None:
        async with self._lock:
            found = self._records.get(key)
            if found is not None and found[1] > time.monotonic():
                return found[0]
            self._records[key] = (record, time.monotonic() + ttl)
            return None

    async def complete(self, key: str, record: Record, ttl: float) -> None:
        async with self._lock:
            self._records[key] = (record, time.monotonic() + ttl)

    async def release(self, key: str) -> None:
        async with self._lock:
            self._records.pop(key, None)


class RedisStore:
    """Shared across replicas, which is where this actually matters."""

    def __init__(self, client: Any, prefix: str = "idem") -> None:
        self.client = client
        self.prefix = prefix.rstrip(":")

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def claim(self, key: str, record: Record, ttl: float) -> Record | None:
        full = self._key(key)
        # SET NX is the claim: whoever wins it runs the handler.
        won = await self.client.set(full, record.to_json(), nx=True, ex=int(ttl) or 1)
        if won:
            return None
        existing = await self.client.get(full)
        return Record.from_json(existing) if existing else None

    async def complete(self, key: str, record: Record, ttl: float) -> None:
        await self.client.set(self._key(key), record.to_json(), ex=int(ttl) or 1)

    async def release(self, key: str) -> None:
        await self.client.delete(self._key(key))


class IdempotencyMiddleware:
    """Replay the stored response for a repeated ``Idempotency-Key``."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: Store | None = None,
        ttl: float = 86400.0,
        methods: frozenset[str] = DEFAULT_METHODS,
        required: bool = False,
        response_options: Any = None,
    ) -> None:
        self.app = app
        self.store = store or MemoryStore()
        self.ttl = ttl
        self.methods = methods
        #: Refuse a mutating request that carries no key at all.
        self.required = required
        self.response_options = response_options

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "") not in self.methods:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        key = request.headers.get(HEADER, "").strip()

        if not key:
            if self.required:
                await self._refuse(
                    send,
                    scope,
                    errs.invalid(f"this endpoint requires an {HEADER} header").with_code(
                        "IDEMPOTENCY_KEY_REQUIRED"
                    ),
                )
                return
            await self.app(scope, receive, send)
            return

        body = await request.body()
        print_fp = fingerprint(scope["method"], scope.get("path", ""), body)

        existing = await self.store.claim(key, Record(fingerprint=print_fp), self.ttl)

        if existing is not None:
            if existing.fingerprint != print_fp:
                await self._refuse(
                    send,
                    scope,
                    errs.unprocessable(
                        f"this {HEADER} was already used for a different request"
                    ).with_code("IDEMPOTENCY_KEY_REUSED"),
                )
                return
            if not existing.completed:
                await self._refuse(
                    send,
                    scope,
                    errs.conflict("a request with this key is still in progress").with_code(
                        "IDEMPOTENCY_IN_PROGRESS"
                    ),
                )
                return
            await self._replay(send, existing)
            return

        await self._run_and_store(scope, receive, send, key, print_fp, body)

    async def _run_and_store(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        key: str,
        print_fp: str,
        body: bytes,
    ) -> None:
        captured = Record(fingerprint=print_fp)
        chunks: list[bytes] = []

        async def replay_body() -> Message:
            return {"type": "http.request", "body": body, "more_body": False}

        async def capturing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status_code = message["status"]
                captured.headers = {
                    k.decode("latin-1"): v.decode("latin-1")
                    for k, v in message.get("headers", [])
                    if k.decode("latin-1").lower() not in ("content-length", "date", "server")
                }
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_body, capturing_send)
        except BaseException:
            # The handler blew up. Drop the claim so a retry is not met with a
            # 409 for the next 24 hours.
            with contextlib.suppress(Exception):
                await self.store.release(key)
            raise

        captured.body = b"".join(chunks)

        if captured.status_code is not None and captured.status_code >= 500:
            # A server error is not an outcome worth replaying; the retry
            # should actually retry.
            with contextlib.suppress(Exception):
                await self.store.release(key)
            return

        with contextlib.suppress(Exception):
            await self.store.complete(key, captured, self.ttl)

    async def _replay(self, send: Send, record: Record) -> None:
        headers = dict(record.headers)
        headers["Idempotent-Replay"] = "true"
        response = Response(
            content=record.body,
            status_code=record.status_code or 200,
            headers=headers,
        )
        await response({"type": "http"}, _no_receive, send)

    async def _refuse(self, send: Send, scope: Scope, error: errs.AppError) -> None:
        await response_mod.fail(error, options=self.response_options)(scope, _no_receive, send)


async def _no_receive() -> Message:  # pragma: no cover
    return {"type": "http.disconnect"}
