"""Timestamp and duration grammar (SPEC §3.7). Pure functions; every result is UTC."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ._types import Duration, TimeSpec

STORED_FMT = "%Y-%m-%dT%H:%M:%S.%f"

TS_RE = re.compile(
    r"^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?"
    r"\s*(Z|z|[+-]\d{2}:?\d{2})?\s*$"
)
REL_RE = re.compile(
    r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours"
    r"|d|day|days|w|wk|wks|week|weeks)\s*ago\s*$",
    re.IGNORECASE,
)
DUR_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d|w)\s*$")
UNIT_S = (("w", 604800.0), ("d", 86400.0), ("h", 3600.0), ("m", 60.0), ("s", 1.0))


def to_utc(dt: datetime) -> datetime:
    """Aware UTC datetime; a naive input is taken as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def dt_to_ts(dt: datetime) -> str:
    """Render to the stored format `YYYY-MM-DDTHH:MM:SS.mmmZ` (sub-millisecond digits dropped)."""
    return to_utc(dt).strftime(STORED_FMT)[:-3] + "Z"


def ts_to_dt(ts: str) -> datetime:
    """Parse a stored-format timestamp to an aware UTC datetime."""
    return datetime.strptime(ts, STORED_FMT + "Z").replace(tzinfo=UTC)


def now_str(clock: Callable[[], datetime]) -> str:
    return dt_to_ts(clock())


def parse_timespec(spec: TimeSpec, now: datetime) -> datetime:
    """A TimeSpec (datetime, absolute string, or `N units ago`) as an aware UTC datetime."""
    if isinstance(spec, datetime):
        return to_utc(spec)
    if not isinstance(spec, str):
        raise ValueError(f"not a TimeSpec: {spec!r}")
    m = TS_RE.match(spec)
    if m:
        y, mo, d, hh, mm, ss, frac, off = m.groups()
        micro = int((frac or "0").ljust(6, "0"))
        dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0), micro, UTC)
        if off and off not in ("Z", "z"):
            sign = 1 if off[0] == "+" else -1
            digits = off[1:].replace(":", "")
            dt -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        return dt
    r = REL_RE.match(spec)
    if r:
        return to_utc(now) - timedelta(seconds=int(r.group(1)) * dict(UNIT_S)[r.group(2)[0].lower()])
    raise ValueError(f"unrecognised timestamp: {spec!r}")


def is_relative(spec: object) -> bool:
    """True for the `N units ago` string form (which import_() rejects, §12.8)."""
    return isinstance(spec, str) and REL_RE.match(spec) is not None


def render_timespec(spec: TimeSpec, now: datetime) -> str:
    """Parse and render to the stored format, ready to bind against a stored timestamp."""
    return dt_to_ts(parse_timespec(spec, now))


def parse_duration(d: Duration) -> float:
    """A Duration (timedelta or `<number><s|m|h|d|w>`) in seconds; negative is a ValueError."""
    if isinstance(d, timedelta):
        secs = d.total_seconds()
    elif isinstance(d, str) and (m := DUR_RE.match(d)):
        secs = float(m.group(1)) * dict(UNIT_S)[m.group(2)]
    else:
        raise ValueError(f"not a duration: {d!r}")
    if secs < 0:
        raise ValueError(f"negative duration: {d!r}")
    return secs


def render_duration(seconds: float) -> str:
    """Seconds as the string with the largest unit that divides it exactly (`"90m"`, `"60h"`)."""
    for unit, size in UNIT_S:
        n = seconds / size
        if n == int(n):
            return f"{int(n)}{unit}"
    return f"{seconds:g}s"


def parse_expires(value: Duration | TimeSpec, now: datetime) -> datetime:
    """`expires` argument of write(): a Duration is added to now, a TimeSpec is used as is."""
    if isinstance(value, timedelta) or (isinstance(value, str) and DUR_RE.match(value)):
        return to_utc(now) + timedelta(seconds=parse_duration(value))
    return parse_timespec(value, now)


def age_seconds(now: datetime, ts: str) -> float:
    """`max(0, now - ts)` in seconds (§5.3)."""
    return max(0.0, (to_utc(now) - ts_to_dt(ts)).total_seconds())
