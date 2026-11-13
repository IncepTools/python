"""Structured logging with the request's trace, account and actor attached.

Correlation travels on a :mod:`contextvars` variable and is injected by a
logging *filter*, so it is correct inside a thread pool, an
``asyncio.to_thread`` call, and a background task.

The failure this prevents: correlating by renaming the OS thread
(``threading.current_thread().name = trace_id``) works right up until anything
runs off the event loop thread, and then it silently labels one request's log
lines with another request's id.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path
from typing import Any

from inceptools.core import ctxkit

__all__ = [
    "RESERVED_EXTRA",
    "ContextFilter",
    "ElkJsonFormatter",
    "HumanFormatter",
    "LoggingOptions",
    "configure",
    "get_logger",
    "safe_extra",
    "with_fields",
]

#: Attributes already on every LogRecord; anything else is treated as a field.
_RESERVED = frozenset(
    """
    args asctime created exc_info exc_text filename funcName levelname levelno
    lineno module msecs message msg name pathname process processName
    relativeCreated stack_info thread threadName taskName
    """.split()
)

#: Context attributes :class:`ContextFilter` adds; emitted first, skipped later.
_CONTEXT_KEYS = ("trace_id", "request_id", "account_id", "actor_id", "route", "env", "service")

_HUMAN_FORMAT = (
    "%(asctime)s | [%(name)s:%(levelname)s] | [%(trace_id)s] | "
    "%(filename)s:%(lineno)d %(funcName)s() | MSG=%(message)s"
)


class ContextFilter(logging.Filter):
    """Attach the current :class:`~inceptools.core.ctxkit.RequestContext` to a record.

    Every record gains ``trace_id``, ``request_id``, ``account_id``,
    ``actor_id`` and ``route`` — empty strings when there is no request, so a
    format string referencing them never raises.
    """

    def __init__(self, service: str = "", env: str = "") -> None:
        super().__init__()
        self.service = service
        self.env = env

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = ctxkit.current()
        record.trace_id = ctx.trace_id or ctx.request_id or "-"
        record.request_id = ctx.request_id or "-"
        record.account_id = ctx.account_id or ""
        record.actor_id = ctx.actor_id or ""
        record.route = ctx.route or ""
        record.service = self.service
        record.env = self.env
        return True


class HumanFormatter(logging.Formatter):
    """Readable lines for a terminal, tolerant of a missing field."""

    default_msec_format = "%s.%03d"

    def format(self, record: logging.LogRecord) -> str:
        for attr in ("trace_id", "request_id", "account_id", "actor_id", "route"):
            if not hasattr(record, attr):
                setattr(record, attr, "-")
        try:
            return super().format(record)
        except (TypeError, ValueError):  # a %-style message with mismatched args
            record.msg = f"{record.msg!r} args={record.args!r}"
            record.args = ()
            return super().format(record)


class ElkJsonFormatter(logging.Formatter):
    """One JSON object per line, in the shape the ELK pipeline already expects."""

    def __init__(self, application_name: str = "", *, version: str = "1") -> None:
        super().__init__()
        self.application_name = application_name
        self.version = version

    def format(self, record: logging.LogRecord) -> str:
        created = dt.datetime.fromtimestamp(record.created, tz=dt.UTC)
        payload: dict[str, Any] = {
            "@timestamp": created.isoformat().replace("+00:00", "Z"),
            "@version": self.version,
            "application_name": self.application_name or getattr(record, "service", ""),
            "logger_name": record.name,
            "thread_name": record.threadName,
            "level": record.levelname,
            "level_value": record.levelno,
            "message": record.getMessage(),
            "file": f"{record.filename}:{record.lineno}",
            "function": record.funcName,
        }
        for key in _CONTEXT_KEYS:
            val = getattr(record, key, "")
            if val and val != "-":
                payload[key] = val
        # Keep the legacy nested key the existing dashboards filter on.
        if "trace_id" in payload:
            payload["request.id"] = payload["trace_id"]

        for key, val in record.__dict__.items():
            if key in _RESERVED or key in payload or key in _CONTEXT_KEYS:
                continue
            if key.startswith("_") or val == "" or val is None:
                continue
            payload[key] = _safe(val)

        if record.exc_info:
            payload["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
        if record.stack_info:
            payload["stack"] = record.stack_info

        return json.dumps(payload, default=_safe, ensure_ascii=False)


def _safe(value: Any) -> Any:
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    return repr(value)


class LoggingOptions:
    """How this service logs.

    ``log_dir`` enables the rotating human and ELK files the existing services
    ship; left unset, logging goes to stderr only, which is what a container
    wants.
    """

    __slots__ = (
        "backup_count",
        "capture_warnings",
        "elk_file",
        "env",
        "human_file",
        "json_to_stream",
        "level",
        "log_dir",
        "max_bytes",
        "quiet_loggers",
        "service",
        "stream",
    )

    def __init__(
        self,
        *,
        service: str = "",
        env: str = "",
        level: int | str = logging.INFO,
        log_dir: str | os.PathLike[str] | None = None,
        human_file: str = "",
        elk_file: str = "",
        max_bytes: int = 100 * 1024 * 1024,
        backup_count: int = 5,
        json_to_stream: bool = False,
        stream: Any = None,
        capture_warnings: bool = True,
        quiet_loggers: tuple[str, ...] = (
            "uvicorn",
            "uvicorn.error",
            "uvicorn.access",
            "uvicorn.asgi",
        ),
    ) -> None:
        self.service = service
        self.env = env
        self.level = level
        self.log_dir = Path(log_dir) if log_dir else None
        self.human_file = human_file or (f"{service}.log" if service else "app.log")
        self.elk_file = elk_file or (f"elk-{service}.log" if service else "elk-app.log")
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.json_to_stream = json_to_stream
        self.stream = stream or sys.stderr
        self.capture_warnings = capture_warnings
        self.quiet_loggers = quiet_loggers


def configure(options: LoggingOptions | None = None) -> logging.Logger:
    """Install the root handlers. Idempotent — safe to call again on reload."""
    opts = options or LoggingOptions()
    ctx_filter = ContextFilter(service=opts.service, env=opts.env)

    handlers: list[logging.Handler] = []

    stream_handler = logging.StreamHandler(opts.stream)
    stream_handler.setFormatter(
        ElkJsonFormatter(opts.service) if opts.json_to_stream else HumanFormatter(_HUMAN_FORMAT)
    )
    handlers.append(stream_handler)

    if opts.log_dir is not None:
        opts.log_dir.mkdir(parents=True, exist_ok=True)
        human = logging.handlers.RotatingFileHandler(
            opts.log_dir / opts.human_file,
            maxBytes=opts.max_bytes,
            backupCount=opts.backup_count,
            encoding="utf-8",
        )
        human.setFormatter(HumanFormatter(_HUMAN_FORMAT))
        handlers.append(human)

        elk = logging.handlers.RotatingFileHandler(
            opts.log_dir / opts.elk_file,
            maxBytes=opts.max_bytes,
            backupCount=opts.backup_count,
            encoding="utf-8",
        )
        elk.setFormatter(ElkJsonFormatter(opts.service))
        handlers.append(elk)

    for handler in handlers:
        handler.addFilter(ctx_filter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        with_suppress_close(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(opts.level)

    # uvicorn installs its own handlers; leaving them attached double-logs
    # every line in a different format.
    for name in opts.quiet_loggers:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    if opts.capture_warnings:
        logging.captureWarnings(True)

    return logging.getLogger(opts.service or "inceptools")


def with_suppress_close(handler: logging.Handler) -> None:
    # Closing a handler must never break boot.
    with contextlib.suppress(Exception):
        handler.close()


#: Attribute names :class:`logging.LogRecord` already uses. Passing one in
#: ``extra`` raises ``KeyError`` at call time, which is a crash in your logging
#: line rather than in the thing you were trying to log.
RESERVED_EXTRA = frozenset(_RESERVED) | {"service", "env", "trace_id", "request_id"}


def safe_extra(fields: dict[str, Any], *, suffix: str = "_") -> dict[str, Any]:
    """Rename any field that would collide with a ``LogRecord`` attribute.

        log.info("mounted", extra=logx.safe_extra({"module": name}))

    ``module`` becomes ``module_``, and the call succeeds instead of raising.
    """
    return {(f"{k}{suffix}" if k in RESERVED_EXTRA else k): v for k, v in fields.items()}


def get_logger(name: str = "inceptools") -> logging.Logger:
    """A logger that carries request context through :class:`ContextFilter`."""
    return logging.getLogger(name)


def with_fields(logger: logging.Logger, **fields: Any) -> logging.LoggerAdapter[Any]:
    """A logger whose every line carries *fields* as structured extras.

    log = logx.with_fields(logx.get_logger(__name__), module="products")
    log.info("created", extra={"product_id": pid})
    """
    return logging.LoggerAdapter(logger, {"_bound": fields, **fields})
