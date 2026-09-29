"""digest(): whatever consolidation is due, in order, idempotent; the catch-up state lives in the lake (SPEC §6.6)."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _db, _time
from ._consolidate import CUT
from ._types import ConsolidateError, DigestRun, DigestStep, LakeError, LeaseHeld

if TYPE_CHECKING:
    from .lake import Lake

CHARS_PER_TOKEN = 2.2  # measured 2.14–2.33 on Opus 5.5 consolidation prompts (opus-consolidation.md §3)
ATTEMPTS = 3  # digests in which one catch-up day raised before it is given up
LEASE = "consolidate_lease:digest"


class Counter:
    """Wraps a think callback: counts the calls and the prompt characters sent (system + user)."""

    def __init__(self, think: Callable[..., Any]) -> None:
        self.think, self.calls, self.chars, self.operator = think, 0, 0, getattr(think, "operator", ())

    def __call__(self, prompt: str, *, system: str | None = None, json: bool = False) -> Any:
        self.calls, self.chars = self.calls + 1, self.chars + len(prompt) + len(system or "")
        return self.think(prompt, system=system, json=json)


def state(lake: Lake, key: str) -> dict[str, Any] | None:
    """The `digest` (catch-up) or `digest_last` meta value (§3.6)."""
    raw = _db.meta_get(lake.conn, key)
    return None if raw is None else dict(json.loads(raw))


def put(lake: Lake, key: str, value: object) -> None:
    with lake.tx() as conn:
        _db.meta_set(conn, key, None if value is None else json.dumps(value, sort_keys=True))


def digest(lake: Lake, *, since: str | None = None, max_units: int | None = None, max_tokens: int | None = None,
           backfill_max: int | None = None, dry_run: bool = False) -> DigestRun:
    """§6.6: session containers and backfill, the catch-up since `since` (stored; one UTC day per step), mood,
    crystal; each step due-gated under its own lease, all of it under the digest lease. A failed step is recorded
    and the next one runs; a failed catch-up day ends the catch-up for this digest. max_units ends the container
    steps, max_tokens every step; a started step always finishes."""
    if since is not None:
        date.fromisoformat(since)  # ValueError on anything but YYYY-MM-DD
    if dry_run:
        return dry(lake, since, max_units, max_tokens, backfill_max)
    lake.require_writable()
    return run(lake, since, max_units, max_tokens, backfill_max, late=True)


def run(lake: Lake, since: str | None, max_units: int | None, max_tokens: int | None, backfill_max: int | None,
        *, late: bool) -> DigestRun:
    """The digest proper; `late=False` (a dry run) reports whether mood and crystal are due and runs neither."""
    if (think := lake.think) is None:
        raise ConsolidateError("digest() needs a think callback")
    with lake.tx() as conn:
        if (held := _db.meta_get(conn, LEASE)) is not None and held > lake.now_str():
            raise LeaseHeld(f"digest already running (lease until {held})")
        _db.meta_set(conn, LEASE, _time.dt_to_ts(lake.now() + timedelta(hours=1)))
    out, cu = DigestRun([], [], [], [], [], None, 0, 0), state(lake, "digest") or {}
    if since is not None and since != cu.get("since"):
        if cu:
            out.warnings.append(f"catch-up restarts from {since} (was {cu['since']}, through {cu['through']})")
        cu = {"since": since, "through": None, "attempts": {}, "given_up": []}
        put(lake, "digest", cu)
    first = date.fromisoformat(cu["through"]) + timedelta(days=1) if cu.get("through") else (
        date.fromisoformat(cu["since"]) if cu.get("since") else lake.now().date())
    days = [first + timedelta(days=k) for k in range((lake.now().date() - first).days)]
    lake.think = counter = Counter(think)
    left, stopped, halt = max_units, None, False
    try:
        for kind, day in [("container", None), *(("container", d) for d in days), ("mood", None), ("crystal", None)]:
            name = kind if day is None else f"catch-up {day.isoformat()}"
            if max_tokens is not None and counter.chars / CHARS_PER_TOKEN >= max_tokens:
                stopped = "max_tokens"
                break
            if kind == "container" and (halt or left == 0):
                stopped = stopped or ("max_units" if left == 0 else None)
                continue
            if day is None and (not (due := lake.due(kind)) or kind != "container" and not late):
                out.steps.append(DigestStep(name, due, None, 0))  # a dry run reports mood and crystal, never runs them
                continue
            with lake.tx() as conn:  # the lease refresh
                _db.meta_set(conn, LEASE, _time.dt_to_ts(lake.now() + timedelta(hours=1)))
            opts = {k: v for k, v in (("max_clusters", left), ("backfill_max", backfill_max)) if v is not None and
                    kind == "container" and (day is None or k == "max_clusters")}
            start = None if day is None else datetime.combine(day, time(0), tzinfo=UTC)
            chars, before, ok, busy = counter.chars, lake.last_run, True, False
            try:
                lake.consolidate(kind, None if start is None else (
                    _time.dt_to_ts(start), _time.dt_to_ts(start + timedelta(days=1, milliseconds=-1))), **opts)
            except Exception as exc:  # the old timer's status=1: recorded, and the next step still runs
                out.errors.append(f"{name}: {type(exc).__name__}: {exc}")
                ok, busy = False, isinstance(exc, LeaseHeld)
            got = lake.last_run if lake.last_run is not before else None  # None: the call failed before running
            out.steps.append(DigestStep(name, True, got, counter.chars - chars))
            if ok and got is not None and left is not None:  # units used: sessions (their parts) and clusters
                left = max(0, left - len({(d.meta or {}).get("session") or d.id for d in got.written}) - got.skipped)
            if ok and got is not None and any(w.startswith(CUT) for w in got.warnings):
                stopped, halt = "max_units", True  # the next digest resumes the day it cut
            if day is None or ok and halt:
                continue
            label, halt = day.isoformat(), not ok
            if ok:
                cu["through"], _ = label, cu["attempts"].pop(label, None)
                out.days.append(label)
            elif busy:  # another run holds the lease: not an attempt at the day
                continue
            elif (n := int(cu["attempts"].get(label, 0)) + 1) < ATTEMPTS:
                cu["attempts"][label] = n
            else:  # given up: recorded, and the catch-up moves past it
                cu["through"], cu["given_up"], _ = label, [*cu["given_up"], label], cu["attempts"].pop(label, None)
                out.given_up.append(label)
            put(lake, "digest", cu)
    finally:
        lake.think = think
        put(lake, "digest_last", {"at": lake.now_str(), "calls": counter.calls, "prompt_chars": counter.chars,
                                  "stopped": stopped, "errors": len(out.errors)})
        put(lake, LEASE, None)
    return dataclasses.replace(out, stopped=stopped, think_calls=counter.calls, prompt_chars=counter.chars)


def dry(lake: Lake, since: str | None, max_units: int | None, max_tokens: int | None,
        backfill_max: int | None) -> DigestRun:
    """§6.6 dry run: the real digest on a copy beside the lake (not /tmp: a big file) with a think that answers
    skip (a session part takes it as its name) and counts; the real file is only read, and the copy is deleted."""
    copy, size = lake.path.with_name(f"{lake.stem}.digest-dry.lake"), lake.path.stat().st_size
    if (free := shutil.disk_usage(lake.path.parent).free) < 2 * size:
        raise LakeError(f"digest --dry-run copies the lake beside it: it needs {2 * size} bytes free, {free} are")

    def skip(prompt: str, *, system: str | None = None, json: bool = False) -> dict[str, Any]:
        return {"kind": "skip", "title": "(dry run)", "summary": "(dry run)", "changes": []}

    try:
        with sqlite3.connect(copy) as dst:
            lake.conn.backup(dst)
        dst.close()
        with type(lake)(copy, **{**lake.init, "think": skip, "embed": None, "write_crystal_file": False,
                                 "readonly": False}) as twin:  # this lake's options; no model or embed call
            return run(twin, since, max_units, max_tokens, backfill_max, late=False)
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{copy}{suffix}").unlink(missing_ok=True)


def summary(r: DigestRun) -> str:
    """One line for a log or the CLI: containers, skips, mood, crystal, calls, estimated input tokens, errors."""
    def n(kind: str) -> int:
        return sum(len(s.run.written) for s in r.steps if s.run is not None and s.run.kind == kind)
    tokens, skipped = r.prompt_chars / CHARS_PER_TOKEN, sum(s.run.skipped for s in r.steps if s.run is not None)
    est = f"~{tokens / 1000:.0f}k" if tokens >= 1000 else f"~{tokens:.0f}"
    return (f"{n('container')} containers, {skipped} skipped, mood {n('mood')}, crystal {n('crystal')}, {r.think_calls}"
            f" calls, {est} input tokens, {len(r.errors)} errors" + (f", stopped at {r.stopped}" if r.stopped else ""))
