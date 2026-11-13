"""Liveness and readiness as the different questions they are.

``/livez``
    *Is this process alive?* Checks nothing, always 200. A liveness probe that
    checks the database restarts every replica when the database blips — which
    is the one moment you need them all up.
``/readyz``
    *Should this replica receive traffic?* Runs every check. ``DOWN`` answers
    503 so the load balancer removes it; ``DEGRADED`` still answers 200,
    because a dead optional cache is not a reason to stop serving.
``/health``
    The detail, for a human or a dashboard. Always 200. Put it behind auth —
    it names your dependencies.

These bodies are deliberately **not** enveloped: a probe is not an API client.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = [
    "Check",
    "Checker",
    "CheckerFunc",
    "Registry",
    "Report",
    "Severity",
    "Status",
    "detail_handler",
    "live_handler",
    "of",
    "ready_handler",
]


class Status(str, enum.Enum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class Severity(enum.IntEnum):
    #: Its failure means this replica cannot serve. ``/readyz`` answers 503.
    CRITICAL = 0
    #: Its failure degrades the service but traffic should keep arriving.
    OPTIONAL = 1


Probe = Callable[[], Awaitable[None] | None]


@runtime_checkable
class Checker(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def severity(self) -> Severity: ...

    async def check(self) -> None: ...


class CheckerFunc:
    __slots__ = ("_name", "_probe", "_severity")

    def __init__(self, name: str, severity: Severity, probe: Probe) -> None:
        self._name = name
        self._severity = severity
        self._probe = probe

    @property
    def name(self) -> str:
        return self._name

    @property
    def severity(self) -> Severity:
        return self._severity

    async def check(self) -> None:
        result = self._probe()
        if inspect.isawaitable(result):
            await result


def of(name: str, probe: Probe, *, severity: Severity = Severity.CRITICAL) -> Checker:
    """Build a check from a function. Raise to report it unhealthy."""
    return CheckerFunc(name, severity, probe)


@dataclass(slots=True)
class Check:
    """One dependency's answer."""

    name: str
    status: Status
    latency_ms: int = 0
    message: str = ""
    severity: Severity = Severity.CRITICAL
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status.value,
            "latencyMs": self.latency_ms,
        }
        if self.message:
            out["message"] = self.message
        if self.details:
            out["details"] = self.details
        if self.severity is Severity.OPTIONAL:
            out["severity"] = "optional"
        return out


@dataclass(slots=True)
class Report:
    status: Status
    service: str = ""
    version: str = ""
    uptime: str = ""
    checks: dict[str, Check] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status.value}
        for key, value in (
            ("service", self.service),
            ("version", self.version),
            ("uptime", self.uptime),
        ):
            if value:
                out[key] = value
        if self.checks:
            out["checks"] = {k: v.as_dict() for k, v in self.checks.items()}
        return out


def _uptime(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


class Registry:
    """Every registered dependency check."""

    def __init__(self, service: str = "", version: str = "", *, timeout: float = 2.0) -> None:
        self.service = service
        self.version = version
        #: Per-check ceiling. A probe that hangs must not hang the probe.
        self.timeout = timeout
        self._checkers: list[Checker] = []
        self._started = time.monotonic()

    def register(self, *checkers: Checker) -> Registry:
        self._checkers.extend(c for c in checkers if c is not None)
        return self

    @property
    def names(self) -> list[str]:
        return [c.name for c in self._checkers]

    async def run(self) -> Report:
        """Run every check concurrently and aggregate."""
        if not self._checkers:
            return Report(
                status=Status.UP,
                service=self.service,
                version=self.version,
                uptime=_uptime(time.monotonic() - self._started),
            )

        results = await asyncio.gather(*(self._run_one(c) for c in self._checkers))
        checks = {check.name: check for check in results}

        if any(c.status is Status.DOWN for c in results):
            overall = Status.DOWN
        elif any(c.status is Status.DEGRADED for c in results):
            overall = Status.DEGRADED
        else:
            overall = Status.UP

        return Report(
            status=overall,
            service=self.service,
            version=self.version,
            uptime=_uptime(time.monotonic() - self._started),
            checks=checks,
        )

    async def _run_one(self, checker: Checker) -> Check:
        started = time.monotonic()
        timeout = self.timeout if self.timeout and self.timeout > 0 else 2.0
        try:
            await asyncio.wait_for(checker.check(), timeout)
        except TimeoutError:
            return self._failed(checker, started, f"check timed out after {timeout:g}s")
        except Exception as exc:
            return self._failed(checker, started, str(exc) or type(exc).__name__)
        return Check(
            name=checker.name,
            status=Status.UP,
            latency_ms=int((time.monotonic() - started) * 1000),
            severity=checker.severity,
        )

    @staticmethod
    def _failed(checker: Checker, started: float, message: str) -> Check:
        return Check(
            name=checker.name,
            status=Status.DEGRADED if checker.severity is Severity.OPTIONAL else Status.DOWN,
            latency_ms=int((time.monotonic() - started) * 1000),
            message=message,
            severity=checker.severity,
        )


# -- handlers ------------------------------------------------------------


def live_handler() -> Callable[[Request], Awaitable[JSONResponse]]:
    """Always 200. Checks nothing, on purpose."""

    async def handler(request: Request) -> JSONResponse:
        return JSONResponse({"status": Status.UP.value})

    return handler


def ready_handler(registry: Registry) -> Callable[[Request], Awaitable[JSONResponse]]:
    """200 for ``UP`` and ``DEGRADED``, 503 for ``DOWN``."""

    async def handler(request: Request) -> JSONResponse:
        report = await registry.run()
        status = 503 if report.status is Status.DOWN else 200
        return JSONResponse(report.as_dict(), status_code=status)

    return handler


def detail_handler(registry: Registry) -> Callable[[Request], Awaitable[JSONResponse]]:
    """The full report, always 200."""

    async def handler(request: Request) -> JSONResponse:
        report = await registry.run()
        return JSONResponse(report.as_dict())

    return handler
