"""Digestion phase 1: automation rows stay out of consolidation but stay
searchable (SPEC §4.3 `automation`, §6), no unit cap by default (§6.1 `max_clusters`), idempotent explicit windows
(§6.1) and the material crystal gate (§6.4). The catch-up these made possible is digest(since=) now, tested in
test_digest.py. Every model call is a deterministic fake; every lake is a tmp file."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import FrozenClock
from lake import Delta, Lake

MakeLake = Callable[..., Lake]
REPO = Path(__file__).resolve().parent.parent
ID_RE = re.compile(r"\[([0-9a-f]{12})\]")
AUTO = ("tag:automation", "prefix:# Batch screen", "source:screen-bot")
MOOD = {"state": "steady", "headline": "A steady day.", "subtext": "Work moved.", "carrier_wave": "Still here.",
        "levels": {"focus": 0.5}, "threads": ["the garden"]}


def edits_answer(prompt: str) -> dict[str, Any]:
    """An edits-crystal answer: three core items and an open one, each citing the newest row id in the prompt."""
    cite = ID_RE.findall(prompt)[-1:]
    return {"items": [{"op": "add", "section": s, "cite": cite, "text": f"I keep building the garden ({s} {k}); "
                       + "the closed loop, the next bed to dig. " * 6} for k, s in enumerate(("core",) * 3 + ("open",))]}


class Oracle:
    """A deterministic fake think that answers by prompt shape: session, cluster, mood, supersede, prose or edits crystal."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []

    def __call__(self, prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
        self.calls.append((prompt, system, json))
        if "══ THE SESSION ══" in prompt:
            return {"title": f"Session {len(self.calls)}", "summary": "Worked through one thing.", "changes": []}
        if "══ THE STRETCH ══" in prompt:
            return {"kind": "propose", "title": "A stretch", "summary": "Rows about one thing.", "rationale": "one"}
        if not json:
            return ("## Where I am\n\nI keep building the garden and reading what it holds.\n\n## What pulls\n\n"
                    + "The closed loop, the next bed to dig. " * 30)
        if "pairs" in (system or ""):
            return {"pairs": []}
        if '"items"' in (system or ""):  # the edits crystal (the default): four cited items, over min_chars rendered
            return edits_answer(prompt)
        return dict(MOOD)

    def shown(self) -> set[str]:
        return {i for p, _, _ in self.calls for i in ID_RE.findall(p)}


def session(lake: Lake, tag: str, texts: Sequence[str], start: datetime, *, source: str = "claude-code",
            extra: Sequence[str] = ()) -> list[Delta]:
    """One hook-shaped session: alternating user / assistant rows one minute apart."""
    return [lake.write(t, source, tags=["user" if i % 2 == 0 else "assistant", f"session:{tag}", *extra],
                       timestamp=start + timedelta(minutes=i)) for i, t in enumerate(texts)]


def flood(lake: Lake, start: datetime, n: int = 30, key: str = "") -> list[Delta]:
    """Automation dominating a day: labelled sessions (tag), unlabelled historical ones (content prefix) with
    their replies, and a source-labelled bot writing untagged rows that would cluster."""
    rows: list[Delta] = []
    for k in range(n):
        at = start + timedelta(minutes=3 * k)
        prompt = f"# Batch screen — stage B\n\nRank these {k} postings against the profile. " + "posting text " * 200
        reply = f'[{{"id": {k}, "verdict": "pass", "reason": "the posting number {k} does not match the profile"}}]'
        if k % 2:
            rows += session(lake, f"auto{k}{key}", [prompt, reply, prompt + " again"], at, extra=["automation"])
        else:
            rows += session(lake, f"hist{k}{key}", [prompt, reply, "follow the screen with the triage pass " + str(k)], at)
    rows += [lake.write(f"screen bot heartbeat {k} checked the boards", "screen-bot", timestamp=start + timedelta(seconds=20 * k))
             for k in range(8)]
    return rows


def real(lake: Lake, start: datetime, key: str = "") -> list[Delta]:
    return (session(lake, f"garden{key}", ["plan the raised garden beds for spring", "Two beds, cedar, 4x8.",
                                     "order the cedar boards from the mill", "Ordered twelve boards."], start)
            + session(lake, f"tax{key}", ["file the quarterly estimated tax", "Filed; confirmation saved.",
                                    "set a reminder for the next quarter", "Reminder set for January."],
                      start + timedelta(hours=4)))


def test_automation_rules_validate(make_lake: MakeLake) -> None:
    for bad in (["automation"], ["tag:"], ["kind:x"]):
        with pytest.raises(ValueError, match="automation rule"):
            make_lake("bad", automation=bad)
    assert make_lake("ok", automation=AUTO).automation == (("tag", "automation"), ("prefix", "# Batch screen"),
                                                           ("source", "screen-bot"))


def test_flood_automation_never_becomes_containers_or_crystal_material(
    make_lake: MakeLake, clock: FrozenClock, tmp_path: Path
) -> None:
    """A day where automation is most of the rows and characters: none of it is containered, read by the mood or
    offered to the crystal (nor are containers written over it before the host named it), and all of it stays
    searchable."""
    think = Oracle()
    lake = make_lake("flood", think=think, automation=AUTO)
    day = clock() - timedelta(hours=20)
    auto = flood(lake, day)
    mine = real(lake, day + timedelta(hours=2))
    auto_ids, mine_ids = {d.id for d in auto}, {d.id for d in mine}
    assert sum(len(d.content) for d in auto) > 20 * sum(len(d.content) for d in mine)
    clock.advance(hours=4)
    lake.write("a fresh line to keep the mood window warm", "claude-code", tags=["user", "session:now"])
    clock.advance(hours=1)
    lake.consolidate("container")
    conts = lake.last_run.written if lake.last_run else []
    assert len(conts) == 2 and all(set(c.derived_from) <= mine_ids for c in conts)
    assert not think.shown() & auto_ids
    assert lake.due("container") is False, "automation units are never due"
    lake.write("one more line in the mood window", "claude-code", tags=["user", "session:now"])
    mood = lake.consolidate("mood")
    assert mood is not None and not set(mood.derived_from) & auto_ids
    crystal = lake.consolidate("crystal")
    assert crystal is not None and not set(crystal.derived_from) & auto_ids
    assert not think.shown() & auto_ids and not any("Batch screen" in p for p, _, _ in think.calls)
    hits = lake.recall("Batch screen stage B postings profile", limit=50)
    assert {h.delta.id for h in hits} & auto_ids, "automation rows stay searchable"


def test_old_automation_containers_are_not_crystal_material(make_lake: MakeLake, clock: FrozenClock) -> None:
    """Containers written over automation before the host named it (the pre-rule clusterer's) are left out of the
    crystal's container list once the rules exist; the host's own containers stay in."""
    before = make_lake("pre", think=Oracle())
    day = clock() - timedelta(hours=20)
    auto = flood(before, day, n=6)
    real(before, day + timedelta(hours=2))
    before.consolidate("container")
    written = before.last_run.written if before.last_run else []
    auto_ids = {d.id for d in auto}
    bot = [c for c in written if set(c.derived_from) & auto_ids]
    assert len(bot) >= 3 and len(written) > len(bot)
    path = before.path
    before.close()
    think = Oracle()
    after = Lake(path, think=think, clock=clock, automation=AUTO)
    try:
        after.consolidate("crystal")
        (prompt,) = [p for p, s, _ in think.calls if "Previous crystal" in p]
        shown = set(ID_RE.findall(prompt))
        assert not shown & {c.id for c in bot} and not shown & auto_ids
        assert shown & {c.id for c in written if c not in bot}
    finally:
        after.close()


def test_no_unit_cap_by_default_and_the_optional_cap(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.1: max_clusters is unset by default, so one default run takes every due unit; a host may still cap it."""
    day = clock() - timedelta(hours=30)
    lake = make_lake("all", think=Oracle())
    for k in range(12):
        session(lake, f"s{k}", [f"do thing {k}", f"Did thing {k}.", "and the next one", "Done with both."],
                day + timedelta(hours=2 * k))
    clock.advance(hours=1)
    lake.consolidate("container")
    assert lake.last_run is not None and len(lake.last_run.written) == 12
    capped = make_lake("capped", think=Oracle())
    for k in range(12):
        session(capped, f"s{k}", [f"do thing {k}", f"Did thing {k}.", "and the next one", "Done with both."],
                day + timedelta(hours=2 * k))
    capped.consolidate("container", max_clusters=5, backfill=False)
    assert capped.last_run is not None and len(capped.last_run.written) == 5


def test_explicit_window_is_idempotent(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.1: re-running an explicit window re-offers no row a live library container covers (sessions or clusters)."""
    think = Oracle()
    lake = make_lake("win", think=think)
    day = clock() - timedelta(days=12)
    session(lake, "old", ["an old plan", "An old answer.", "an old follow-up"], day)
    for k in range(4):
        lake.write(f"field note {k} about the orchard", "notes", tags=["topic:orchard"], timestamp=day + timedelta(minutes=5 + k))
    window = tuple((day + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ") for h in (-1, 1))
    lake.consolidate("container", window)
    assert lake.last_run is not None and len(lake.last_run.written) == 2
    calls = len(think.calls)
    assert lake.consolidate("container", window) is None
    assert lake.last_run is not None and not lake.last_run.written and len(think.calls) == calls


def test_crystal_material_gate(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.4 default gate: a crystal is due once it is crystal_min_age old and crystal_containers containers were
    written since it (or the age+rows clause holds); centroid drift is not consulted."""
    think = Oracle()
    lake = make_lake("gate", think=think, due_thresholds={"crystal_containers": 2})
    day = clock() - timedelta(hours=10)
    session(lake, "a", ["first thing", "First answer.", "first follow-up"], day)
    clock.advance(hours=1)
    lake.consolidate("container")
    assert lake.due("crystal") is True  # bootstrap: a container exists
    assert lake.consolidate("crystal") is not None
    assert lake.due("crystal") is False
    for k in range(2):
        session(lake, f"b{k}", ["next thing", "Next answer.", "next follow-up"], lake.now() + timedelta(minutes=k * 10))
    clock.advance(hours=2)
    lake.consolidate("container")
    assert lake.last_run is not None and len(lake.last_run.written) == 2
    assert lake.due("crystal") is False, "younger than crystal_min_age (20h)"
    clock.advance(hours=18)
    assert lake.due("crystal") is True
    quiet = make_lake("quiet", think=Oracle(), due_thresholds={"crystal_rows": 3})
    session(quiet, "a", ["first thing", "First answer.", "first follow-up"], clock() - timedelta(hours=3))
    assert quiet.consolidate("crystal") is not None
    clock.advance(days=4)
    assert quiet.due("crystal") is False, "old, but nothing new"
    session(quiet, "c", ["a later row", "A later answer.", "a later follow-up"], clock() - timedelta(hours=1))
    assert quiet.due("crystal") is True, "the age + rows clause"


def test_cli_reads_lake_automation(tmp_path: Path) -> None:
    """SPEC §8, §13.8: the CLI takes LAKE_AUTOMATION (or --automation) for every command that consolidates or gates;
    unset, the host default tag:automation applies (the label LAKE_TAGS=automation gives a job's rows); set but
    empty, no rule."""
    path = tmp_path / "cli.lake"
    lk = Lake(path)
    start = datetime.now(UTC) - timedelta(hours=3)
    session(lk, "bot", ["# Batch screen — rank these", "Ranked the batch of postings.", "# Batch screen — again"], start)
    lk.close()
    with Lake(tmp_path / "tagged.lake") as tagged:
        session(tagged, "job", ["run the nightly job", "The nightly job ran.", "run it again"], start, extra=["automation"])

    def due(file: Path = path, **env: str) -> int:
        base = {k: v for k, v in __import__("os").environ.items() if not k.startswith("LAKE_") and k != "LAKE"}
        cmd = [sys.executable, "-m", "lake.cli", "--lake", str(file), "due", "container"]
        return subprocess.run(cmd, env={**base, "PYTHONPATH": str(REPO), "LAKE_ENV_FILE": "/nonexistent/x", **env},
                              capture_output=True, text=True, timeout=60, cwd=str(REPO)).returncode

    assert due() == 0
    assert due(LAKE_AUTOMATION="tag:other,prefix:# Batch screen") == 1
    assert due(tmp_path / "tagged.lake") == 1, "the default rule tag:automation"
    assert due(tmp_path / "tagged.lake", LAKE_AUTOMATION="") == 0, "set but empty: no rule"
