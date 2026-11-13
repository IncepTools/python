"""A five-field cron parser.

Standard ``minute hour day-of-month month day-of-week`` — no seconds field,
because a cron line that means something different here than it does in
``crontab`` is a trap. Descriptors (``@daily``) and ``@every 30s`` are
supported.

Written out rather than taken from a dependency: a framework's dependencies
become every importer's, and this is a hundred lines of well-understood
arithmetic.

An expression is parsed at registration, so a typo fails the boot rather than
the night the job was meant to run.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

__all__ = ["CronError", "Schedule", "parse", "parse_every"]

_MONTHS = {
    m: i
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(),
        start=1,
    )
}
_DOWS = {d: i for i, d in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}

_DESCRIPTORS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_DURATION = re.compile(r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|[smhd])")


class CronError(ValueError):
    """A cron expression that cannot mean anything."""


@dataclass(frozen=True, slots=True)
class Schedule:
    """The matching sets for each field."""

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: True when both day-of-month and day-of-week were restricted. Cron
    #: ORs them in that case, which is odd but is what every crontab does.
    day_union: bool
    expression: str = ""

    def matches(self, moment: dt.datetime) -> bool:
        if moment.minute not in self.minutes or moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        dom = moment.day in self.days
        dow = (moment.weekday() + 1) % 7 in self.weekdays
        return (dom or dow) if self.day_union else (dom and dow)

    def next_after(self, moment: dt.datetime) -> dt.datetime:
        """The first matching minute strictly after *moment*."""
        candidate = (moment + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
        # Four years covers every February 29th placement.
        limit = candidate + dt.timedelta(days=366 * 4)
        while candidate <= limit:
            if candidate.month not in self.months:
                candidate = _start_of_next_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + dt.timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = _next_hour(candidate)
                continue
            if candidate.minute not in self.minutes:
                candidate += dt.timedelta(minutes=1)
                continue
            return candidate
        raise CronError(f"{self.expression!r} never matches")

    def _day_matches(self, moment: dt.datetime) -> bool:
        dom = moment.day in self.days
        dow = (moment.weekday() + 1) % 7 in self.weekdays
        return (dom or dow) if self.day_union else (dom and dow)


def _start_of_next_month(moment: dt.datetime) -> dt.datetime:
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    return moment.replace(year=year, month=month, day=1, hour=0, minute=0)


def _next_hour(moment: dt.datetime) -> dt.datetime:
    nxt = moment + dt.timedelta(hours=1)
    return nxt.replace(minute=0)


def _field(spec: str, low: int, high: int, names: dict[str, int], label: str) -> frozenset[int]:
    values: set[int] = set()

    for part in spec.split(","):
        part = part.strip().lower()
        if not part:
            raise CronError(f"empty {label} field")

        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) == 0:
                raise CronError(f"{label}: {raw_step!r} is not a step")
            step = int(raw_step)
            part = part or "*"

        if part == "*":
            start, end = low, high
        elif "-" in part[1:] or (part.startswith("-") is False and "-" in part):
            left, _, right = part.partition("-")
            start, end = (
                _value(left, names, low, high, label),
                _value(right, names, low, high, label),
            )
            if start > end:
                raise CronError(f"{label}: range {part!r} runs backwards")
        else:
            start = end = _value(part, names, low, high, label)

        values.update(range(start, end + 1, step))

    if not values:
        raise CronError(f"{label}: {spec!r} matches nothing")
    return frozenset(values)


def _value(raw: str, names: dict[str, int], low: int, high: int, label: str) -> int:
    raw = raw.strip()
    if raw in names:
        return names[raw]
    if not raw.lstrip("-").isdigit():
        raise CronError(f"{label}: {raw!r} is not a number")
    value = int(raw)
    if label == "day-of-week" and value == 7:
        value = 0  # both 0 and 7 are Sunday, as in crontab
    if not low <= value <= high:
        raise CronError(f"{label}: {value} is outside {low}-{high}")
    return value


def parse(expression: str) -> Schedule:
    """Parse a five-field expression or a descriptor."""
    text = expression.strip()
    if not text:
        raise CronError("empty cron expression")

    lowered = text.lower()
    if lowered in _DESCRIPTORS:
        text = _DESCRIPTORS[lowered]
    elif lowered.startswith("@every"):
        raise CronError("'@every' is an interval, not a cron expression; use Job(every=...)")
    elif lowered.startswith("@"):
        raise CronError(f"unknown descriptor {text!r}")

    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"{expression!r} has {len(fields)} fields; a cron expression has 5 "
            "(minute hour day-of-month month day-of-week)"
        )

    minute, hour, dom, month, dow = fields
    return Schedule(
        minutes=_field(minute, 0, 59, {}, "minute"),
        hours=_field(hour, 0, 23, {}, "hour"),
        days=_field(dom, 1, 31, {}, "day-of-month"),
        months=_field(month, 1, 12, _MONTHS, "month"),
        weekdays=_field(dow, 0, 6, _DOWS, "day-of-week"),
        day_union=dom.strip() != "*" and dow.strip() != "*",
        expression=expression,
    )


def parse_every(text: str) -> float:
    """``"@every 1h30m"`` or ``"30s"`` -> seconds."""
    raw = text.strip().lower().removeprefix("@every").strip()
    if not raw:
        raise CronError("no interval given")
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
    total = 0.0
    matched = 0
    for match in _DURATION.finditer(raw):
        total += float(match.group("value")) * units[match.group("unit")]
        matched += len(match.group(0))
    if matched != len(raw.replace(" ", "")) or total <= 0:
        raise CronError(f"{text!r} is not a duration (try 30s, 5m, 1h30m)")
    return total
