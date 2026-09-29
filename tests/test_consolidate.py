"""§10 tests owned by _consolidate: every test_consolidate_*, test_due, and the consolidate half of
test_closed_loop_source (SPEC §6, §6.1–§6.4, §12.2–§12.4, §12.7, §12.10)."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeEmbed, FakeThink, FrozenClock

from lake import ConsolidateError, Delta, Lake, LakeError, NotFoundError
from lake._db import meta_get

MakeLake = Callable[..., Lake]
MakeThink = Callable[[Sequence[str | dict[str, Any]]], FakeThink]

PROPOSE = {"kind": "propose", "title": "Lake design stretch", "summary": "Rows about one thing.", "rationale": "one thread"}
MOOD = {
    "state": "Self-Doubt", "headline": "The lake is *quieter* today.", "subtext": "Not much moving.",
    "carrier_wave": "I am sitting with it.", "levels": {"focus": 0.4, "Doubt": 0.7}, "threads": ["lake — building"],
}
CRYSTAL_KEYS = ["state", "headline", "subtext", "carrier_wave", "threads", "levels"]


def crystal_text(seed: str, chars: int = 900) -> str:
    """A crystal body with two h2 facets padded to exactly `chars` characters."""
    body = f"## Where I am\n\nI keep building the {seed} lake and reading what it holds.\n\n## What pulls\n\nThe closed loop. "
    return (body + f"{seed} sediment settles. " * 60)[:chars]


def stretch(
    lake: Lake, n: int, *, minutes_ago: float, source: str = "chat", tags: Sequence[str] = ("topic:lake",), step: int = 10
) -> list[Delta]:
    """n related rows starting minutes_ago before the clock, `step` seconds apart, distinct content."""
    start = lake.now() - timedelta(minutes=minutes_ago)
    return [
        lake.write(f"{source} row at {start + timedelta(seconds=i * step)} about the lake design", source,
                   tags=list(tags), timestamp=start + timedelta(seconds=i * step))
        for i in range(n)
    ]


def seq_of(lake: Lake, delta: Delta) -> int:
    return int(lake.conn.execute("SELECT seq FROM deltas WHERE id = ?", (delta.id,)).fetchone()[0])


def watermark(lake: Lake) -> dict[str, Any]:
    raw = meta_get(lake.conn, "container_watermark")
    assert raw is not None
    return dict(json.loads(raw))


def count(lake: Lake, kind: str) -> int:
    return int(lake.conn.execute("SELECT count(*) FROM deltas WHERE kind = ?", (kind,)).fetchone()[0])


def test_closed_loop_source(make_lake: MakeLake, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([PROPOSE]))
    rows = stretch(lake, 3, minutes_ago=35)
    row = lake.consolidate("container")
    assert row is not None and row.source == "lake:container"
    hits = lake.recall(source="lake:container")
    assert [h.delta.id for h in hits] == [row.id]
    assert {r.id for r in rows}.isdisjoint({h.delta.id for h in hits})


def test_consolidate_container(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE])
    lake = make_lake(think=think, model_name="fake-1")
    rows = stretch(lake, 3, minutes_ago=35)
    lake.write("ok", "chat", timestamp=clock() - timedelta(minutes=35, seconds=-25))
    rows += stretch(lake, 3, minutes_ago=34.5)
    row = lake.consolidate("container")
    assert row is not None
    assert (row.source, row.kind, row.level) == ("lake:container", "container", 1)
    assert row.derived_from == [r.id for r in rows]
    assert row.meta is not None and row.meta["model"] == "fake-1" and row.meta["filters"] is None
    assert row.meta["title"] == PROPOSE["title"] and row.content == "Lake design stretch\n\nRows about one thing."
    assert row.meta["span"] == [rows[0].timestamp, rows[-1].timestamp] and isinstance(row.meta["window"], list)
    assert row.tags == ["topic:lake"]
    assert watermark(lake)["pos"] == [rows[5].timestamp, seq_of(lake, rows[5])]
    assert lake.last_run is not None and lake.last_run.written == [row] and lake.last_run.think_calls == 1
    assert think.calls[0][2] is True and "Respond with ONLY a JSON object" in (think.systems[0] or "")
    assert "══ THE STRETCH ══\n6 rows from" in think.prompts[0] and "at least 2 of these rows" in think.prompts[0]
    assert lake.get(row.id) == row
    assert lake.consolidate("container") is None
    assert lake.last_run.written == [] and lake.last_run.think_calls == 0


def test_consolidate_container_fenced(make_lake: MakeLake, fake_think: MakeThink) -> None:
    fenced = "```json\n" + json.dumps(PROPOSE) + "\n```"
    thought = "<think>\nlet me weigh this\n</think>\nHere is my answer: " + json.dumps(PROPOSE) + " — done."
    think = fake_think([fenced, thought])
    lake = make_lake(think=think)
    stretch(lake, 3, minutes_ago=180)
    stretch(lake, 3, minutes_ago=120)
    lake.consolidate("container")
    assert lake.last_run is not None
    assert len(lake.last_run.written) == 2 and lake.last_run.think_calls == 2 and len(think.calls) == 2
    assert count(lake, "container") == 2


def test_consolidate_container_retry(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([{"kind": "propose"}, PROPOSE])
    lake = make_lake(think=think)
    rows = stretch(lake, 3, minutes_ago=35)
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in rows]
    assert lake.last_run is not None and lake.last_run.think_calls == 2 and lake.last_run.written == [row]
    shape = (resources.files("lake") / "prompts" / "container_schema.txt").read_text(encoding="utf-8").rstrip("\n")
    assert think.prompts[1] == think.prompts[0] + (
        "\n\nYour previous answer was rejected: title is empty. Reply with only one JSON object in exactly this shape:\n"
        + shape)
    assert think.prompts[1].endswith('{"kind": "skip", "reason": "<one sentence on why no container>"}')
    assert think.systems[0] == think.systems[1]


def test_consolidate_container_skip_after_two(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([{"kind": "nope"}, "no object here", PROPOSE])
    lake = make_lake(think=think)
    first = stretch(lake, 3, minutes_ago=180)
    second = stretch(lake, 3, minutes_ago=120)
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in second]
    assert lake.last_run is not None and lake.last_run.written == [row] and lake.last_run.think_calls == 3
    assert len(lake.last_run.warnings) == 1
    warning = lake.last_run.warnings[0]
    assert warning.startswith(f"cluster {first[0].timestamp[:16]}–{first[-1].timestamp[:16]} (3 rows: ")
    assert warning.endswith("rejected twice: no JSON object in the answer") and first[0].id in warning
    assert 'kind must be "propose" or "skip"' in think.prompts[1]
    assert watermark(lake)["pos"] == [second[-1].timestamp, seq_of(lake, second[-1])]
    assert lake.consolidate("container") is None


def test_consolidate_mood_raises_after_two(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([{"state": "calm"}, {"state": "calm", "headline": "   "}])
    lake = make_lake(think=think)
    stretch(lake, 3, minutes_ago=10)
    with pytest.raises(ConsolidateError, match="headline is missing or empty"):
        lake.consolidate("mood")
    assert count(lake, "mood") == 0 and len(think.calls) == 2
    assert meta_get(lake.conn, "consolidate_lease:mood") is None
    assert lake.last_run is not None and lake.last_run.written == [] and lake.last_run.think_calls == 2


def test_consolidate_max_clusters(make_lake: MakeLake, tmp_path: Any, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE] * 5)
    lake = make_lake(think=think)
    groups = [stretch(lake, 8, minutes_ago=60 * hours) for hours in (6, 5, 4, 3, 2)]
    lake.consolidate("container", max_clusters=2)
    assert lake.last_run is not None and len(lake.last_run.written) == 2
    assert [r.derived_from for r in lake.last_run.written] == [[d.id for d in g] for g in groups[:2]]
    assert watermark(lake)["pos"] == [groups[1][-1].timestamp, seq_of(lake, groups[1][-1])]
    lake.consolidate("container")
    assert len(lake.last_run.written) == 3
    assert [r.derived_from for r in lake.last_run.written] == [[d.id for d in g] for g in groups[2:]]
    before = watermark(lake)
    assert before["pos"] == [groups[4][-1].timestamp, seq_of(lake, groups[4][-1])]
    line = {
        "id": "aaaaaaaaaaaa", "timestamp": "2026-09-02T17:00:00.000Z", "content": "an imported row", "source": "host",
        "kind": None, "level": 0, "tags": [], "derived_from": [], "engagement": None, "expires_at": None,
        "media_hash": None, "meta": None,
    }
    path = tmp_path / "in.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert lake.import_(path)["written"] == 1
    after = watermark(lake)
    assert after["pos"] == before["pos"] and after["seq"] > before["seq"]
    assert after["seq"] == int(lake.conn.execute("SELECT max(seq) FROM deltas").fetchone()[0])


def test_consolidate_watermark_backdated(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE, PROPOSE])
    lake = make_lake(think=think)
    first = stretch(lake, 3, minutes_ago=120)
    assert lake.consolidate("container") is not None
    wm = watermark(lake)
    start = lake.now() - timedelta(minutes=120)
    backdated = [
        lake.write(f"late arrival {i} about the lake design", "chat", tags=["topic:lake"], timestamp=start + timedelta(seconds=3 * i))
        for i in range(1, 4)
    ]
    assert all(seq_of(lake, b) > wm["seq"] and [b.timestamp, seq_of(lake, b)] < wm["pos"] for b in backdated)
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [b.id for b in backdated]
    assert watermark(lake)["pos"] == [first[-1].timestamp, seq_of(lake, first[-1])]
    single = lake.write("one more late row about the lake design", "chat", tags=["topic:lake"], timestamp=start + timedelta(seconds=12))
    wm = watermark(lake)
    assert lake.consolidate("container") is None
    assert lake.last_run is not None and lake.last_run.think_calls == 0
    assert watermark(lake)["pos"] == wm["pos"] and watermark(lake)["seq"] == seq_of(lake, single) > wm["seq"]
    for i in (13, 14):
        lake.write(f"late pair {i} about the lake design", "chat", tags=["topic:lake"], timestamp=start + timedelta(seconds=i))
    assert lake.consolidate("container") is None
    assert lake.last_run.think_calls == 0 and lake.last_run.written == []


def test_consolidate_interleaved(make_lake: MakeLake, fake_think: MakeThink) -> None:
    def fill(lake: Lake, *, expires: str | None, source: str = "homeassistant") -> list[Delta]:
        start = lake.now() - timedelta(minutes=40)
        conv: list[Delta] = []
        for i in range(6):
            at = start + timedelta(seconds=20 * i)
            conv.append(lake.write(f"turn {i} of the conversation about the lake", "chat", tags=["user"], timestamp=at))
            if i < 5:
                lake.write(f"kitchen light brightness {40 + i} percent", source, timestamp=at + timedelta(seconds=10), expires=expires)
        return conv

    lake = make_lake(think=fake_think([PROPOSE]))
    conv = fill(lake, expires="1h")
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [c.id for c in conv]
    assert lake.last_run is not None and len(lake.last_run.written) == 1
    lake2 = make_lake("t2", think=fake_think([PROPOSE]), automation=["source:homeassistant"])
    conv2 = fill(lake2, expires=None)
    row2 = lake2.consolidate("container")
    assert row2 is not None and row2.derived_from == [c.id for c in conv2]
    assert lake2.last_run is not None and len(lake2.last_run.written) == 1


def test_consolidate_filters_and_add_tags(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE, MOOD, MOOD, MOOD])
    lake = make_lake(think=think)
    start = lake.now() - timedelta(minutes=40)
    reader, ada = [], []
    for i in range(3):
        reader.append(lake.write(f"reader line {i} about the story", "reader", tags=["story"], timestamp=start + timedelta(seconds=20 * i)))
        ada.append(lake.write(f"ada answer {i} about the story", "ada", tags=["story"], timestamp=start + timedelta(seconds=20 * i + 10)))
    row = lake.consolidate("container", source=["reader"], add_tags=["session:s1"])
    assert row is not None and row.derived_from == [r.id for r in reader]
    assert "session:s1" in row.tags and row.tags[0] == "story"
    assert row.meta is not None and row.meta["filters"] == {"source": ["reader"]}
    assert lake.consolidate("mood") is not None
    scoped = lake.consolidate("mood", source=["reader"])
    assert scoped is not None and scoped.meta is not None and scoped.meta["filters"] == {"source": ["reader"]}
    assert "(no prior mood — this is your first carrier wave)" in think.prompts[2]
    assert "ada answer" not in think.prompts[2] and "reader line" in think.prompts[2]
    again = lake.consolidate("mood", source="reader")
    assert again is not None and again.meta is not None and again.meta["filters"] == {"source": ["reader"]}
    assert f"[previous state: self-doubt]:\n{scoped.content}" in think.prompts[3]


def test_consolidate_system_override(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([{"state": "calm"}, MOOD])
    lake = make_lake(think=think)
    stretch(lake, 3, minutes_ago=10)
    row = lake.consolidate("mood", system="You are Ada.")
    assert row is not None and lake.last_run is not None and lake.last_run.think_calls == 2
    system = think.systems[0] or ""
    assert system.startswith("You are Ada.\n\nEverything you know about this person and this time is in the rows below.")
    assert "\n\nOUTPUT\nRespond with ONLY a JSON object" in system and "carrier_wave" in system
    assert "quiet moment between activities" not in system
    assert "\n\nYour previous answer was rejected: headline is missing or empty. Reply with only one JSON object" in think.prompts[1]
    assert think.prompts[1].endswith('"threads": ["thread name — one phrase about its current state", ...]\n}')


def test_consolidate_lease(make_lake: MakeLake) -> None:
    entered, release = threading.Event(), threading.Event()

    def blocking_think(prompt: str, *, system: str | None = None, json: bool = False) -> dict[str, Any]:
        entered.set()
        release.wait(10)
        return MOOD

    lake = make_lake(think=blocking_think)
    stretch(lake, 3, minutes_ago=10)
    worker = threading.Thread(target=lake.consolidate, args=("mood",))
    worker.start()
    assert entered.wait(10)
    other = make_lake(think=blocking_think)
    with pytest.raises(ConsolidateError, match="already running \\(lease until "):
        other.consolidate("mood")
    assert other.due("mood") is False
    release.set()
    worker.join(10)
    assert meta_get(other.conn, "consolidate_lease:mood") is None
    assert lake.last_run is not None and len(lake.last_run.written) == 1
    assert other.due("mood") is False


def test_consolidate_inputs_level(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE] * 5)
    lake = make_lake(think=think)
    rows = stretch(lake, 6, minutes_ago=5)
    l1 = [lake.consolidate("container", inputs=[rows[i].id, rows[i + 1].id]) for i in (0, 2, 4)]
    assert all(c is not None and c.level == 1 for c in l1)
    assert l1[0] is not None and l1[0].meta is not None and l1[0].meta["window"] is None and l1[0].meta["filters"] is None
    assert meta_get(lake.conn, "container_watermark") is None
    ids = [c.id for c in l1 if c is not None]
    l2 = lake.consolidate("container", inputs=ids)
    assert l2 is not None and l2.level == 2 and l2.derived_from == ids
    assert "level 2 container" in think.prompts[3] and f"[{ids[0]}]" in think.prompts[3] and " L1 · " in think.prompts[3]
    with pytest.raises(ValueError):
        lake.consolidate("container", inputs=ids[:2])
    with pytest.raises(ValueError):
        lake.consolidate("container", inputs=[rows[0].id])
    with pytest.raises(ValueError):
        lake.consolidate("container", "3h", inputs=[rows[0].id, rows[1].id])
    with pytest.raises(ValueError):
        lake.consolidate("container", inputs=[rows[0].id, rows[1].id], source="chat")
    with pytest.raises(NotFoundError):
        lake.consolidate("container", inputs=[rows[0].id, "000000000000"])
    q = lake.write("Q: what is the lake?", "reader", tags=["qa:q"])
    a = lake.write("A: one SQLite file per someone", "reader", tags=["qa:a"])
    marker = lake.consolidate("container", inputs=[q.id, a.id], noise=False)
    assert marker is not None and marker.level == 1 and marker.derived_from == [q.id, a.id]


def test_consolidate_mood(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    think = fake_think([MOOD, MOOD])
    lake = make_lake(think=think, model_name="fake-1")
    old = lake.write("an old row outside the window", "chat", timestamp=clock() - timedelta(hours=4))
    rows = stretch(lake, 3, minutes_ago=20, tags=("user",), step=60)
    lake.write("sensor temperature 21.5 degrees", "homeassistant", expires="1h", timestamp=clock() - timedelta(minutes=15))
    lake.write("ok", "chat", timestamp=clock() - timedelta(minutes=14))
    row = lake.consolidate("mood")
    assert row is not None and (row.kind, row.source, row.level) == ("mood", "lake:mood", 0)
    assert row.tags == ["feeling:self-doubt"]
    assert row.derived_from == [r.id for r in rows] and old.id not in row.derived_from
    obj = json.loads(row.content)
    assert list(obj) == CRYSTAL_KEYS and obj["state"] == "self-doubt" and obj["levels"] == {"focus": 0.4, "doubt": 0.7}
    assert row.content == json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    assert row.meta == {"model": "fake-1", "window": "3h", "filters": None}
    assert "=== Recent rows (last 3h) ===" in think.prompts[0] and "(no prior mood" in think.prompts[0]
    assert meta_get(lake.conn, "last_consolidate_id:mood") == row.id
    a, b = rows[0].timestamp, rows[1].timestamp
    scoped = lake.consolidate("mood", (a, b))
    assert scoped is not None and scoped.derived_from == [rows[0].id, rows[1].id]
    assert scoped.meta is not None and scoped.meta["window"] == [a, b]
    assert f"=== Recent rows ({a[:16]} to {b[:16]}) ===" in think.prompts[1]
    assert "Prior mood (0 minutes ago — anchor weight: heavy) [previous state: self-doubt]:" in think.prompts[1]


def adds(rows: Sequence[Delta], tag: str) -> dict[str, Any]:
    """A crystal answer (§6.3 cited edits): four add ops, three core and one open, citing rows 0.. in turn."""
    return {"items": [{"op": "add", "section": s, "cite": [rows[k % len(rows)].id],
                       "text": f"I keep the {tag} lake close and read what it holds, part {k}."}
                      for k, s in enumerate(("core", "core", "open", "core"))]}


def test_consolidate_crystal(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([]), model_name="fake-1")
    rows = stretch(lake, 3, minutes_ago=10)
    lake.think = think = FakeThink([adds(rows, "first")])
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and (row.kind, row.source) == ("crystal", "lake:crystal") and "part 0" in row.content
    crystal = lake.crystal()
    assert crystal is not None and crystal.id == row.id
    assert lake.crystal_path.read_text(encoding="utf-8") == row.content + "\n"
    assert row.meta is not None and row.meta["drift"] == {"value": None, "method": "token", "prior_id": None}
    assert row.meta["window"] is None and row.meta["filters"] is None and row.meta["model"] == "fake-1"
    assert row.derived_from == [r.id for r in rows] and row.tags == []
    assert think.calls[0][2] is True and '"items"' in (think.systems[0] or "")
    prompt = think.prompts[0]
    assert "=== Previous crystal (none) ===\n(none — this is the first crystal)" in prompt
    assert "(0 of 0, highest level first) ===\n(none)" in prompt and "=== Current mood (none) ===\n(none)" in prompt
    assert "=== Since the previous crystal (3 rows) ===" in prompt
    clock.advance(hours=1)
    new = stretch(lake, 1, minutes_ago=5)
    lake.think = think = FakeThink([{"items": [{"op": "add", "section": "tension", "cite": [new[0].id],
                                                "text": "I want a second lake and I keep tending the first one."}]}])
    row2 = lake.consolidate("crystal", min_chars=100)
    assert row2 is not None and row2.meta is not None
    value = row2.meta["drift"]["value"]
    assert 0 < value <= 1 and row2.meta["drift"]["prior_id"] == row.id and row2.derived_from[0] == row.id
    drift = json.loads(meta_get(lake.conn, "crystal_drift") or "null")
    assert drift == {"value": value, "method": "token", "crystal_id": row2.id, "prior_id": row.id, "at": row2.timestamp}
    assert lake.crystal() == row2 and "=== Previous crystal (1.0 hours ago) ===\n[c1] core · I keep the first" in think.prompts[0]
    with pytest.raises(ValueError):
        lake.consolidate("crystal", "3h")
    quiet = make_lake("quiet", think=fake_think([]), write_crystal_file=False)
    quiet.think = FakeThink([adds(stretch(quiet, 3, minutes_ago=10), "quiet")])
    assert quiet.consolidate("crystal", min_chars=100) is not None and not quiet.crystal_path.exists()
    empty = make_lake("empty", think=fake_think([]))
    assert empty.consolidate("crystal") is None
    bad = make_lake("bad", think=fake_think([]))
    bad.think = FakeThink([adds(stretch(bad, 3, minutes_ago=10), "short")] * 2)
    with pytest.raises(ConsolidateError, match="too short"):
        bad.consolidate("crystal")  # the default min_chars, 800 rendered characters


def test_crystal_ingests_sediment(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6.3/§12.3: a permanent kind='sediment' row is not in NOT_CLUSTERED and carries no expires_at,
    so it stays in the crystal's recent-rows section and its content feeds the next crystal."""
    lake = make_lake(think=fake_think([]), model_name="fake-1")
    rows = stretch(lake, 3, minutes_ago=10)
    sed = lake.write("a settled take drawn from an earlier recall of the lake", "claude-code",
                     kind="sediment", derived_from=[rows[0].id], timestamp=lake.now() - timedelta(minutes=5))
    assert sed.kind == "sediment" and sed.expires_at is None
    lake.think = think = FakeThink([adds([*rows, sed], "sediment")])
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and row.kind == "crystal" and sed.id in think.prompts[0]
    assert sed.id in row.derived_from, "the permanent sediment row feeds the crystal's recent-rows section"


def test_consolidate_without_think(make_lake: MakeLake) -> None:
    lake = make_lake()
    stretch(lake, 3, minutes_ago=10)
    with pytest.raises(ConsolidateError):
        lake.consolidate("mood")
    assert lake.due("mood") is True and lake.due("container") is False
    with pytest.raises(ValueError):
        lake.consolidate("mood", bogus=1)
    with pytest.raises(ValueError):
        lake.due("episode")


def test_due(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([MOOD]))
    assert not any(lake.due(k) for k in ("container", "mood", "crystal"))
    for i in range(10):
        lake.write(f"the user said something number {i}", "chat", tags=["user"])
    assert lake.due("mood") is True and lake.due("container") is False
    stretch(lake, 3, minutes_ago=40)
    assert lake.due("container") is True and lake.due("crystal") is False
    for i in range(37):
        lake.write(f"filler row number {i} for the crystal count", "chat")
    assert lake.due("crystal") is True
    assert lake.consolidate("mood") is not None
    assert lake.due("mood") is False
    for i in range(30):
        clock.advance(minutes=1)
        lake.write(f"sensor reading {i} temperature 21 degrees", "homeassistant", expires="1h")
    assert lake.due("mood") is False
    clock.advance(hours=6)
    assert lake.due("mood") is False
    lake.write("a fresh user row after the mood aged out", "chat", tags=["user"])
    assert lake.due("mood") is True
    small = make_lake("small", due_thresholds={"crystal_rows": 5})
    for i in range(4):
        small.write(f"row number {i} in the small lake", "chat")
    assert small.due("crystal") is False
    small.write("row number 4 in the small lake", "chat")
    assert small.due("crystal") is True


def test_due_crystal_on_refuted_premise(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.4: a live refute of a memory the crystal derives from, written after the crystal, makes
    due('crystal') true even inside crystal_min_age — the injected crystal must drop the premise."""
    lake = make_lake()
    premise = lake.write("the crystal premise probe row", "host")
    lake.write("I am a probe crystal resting on that premise.", "host",
               kind="crystal", derived_from=[premise.id], dedupe=False)
    assert lake.due("crystal") is False   # freshly written, within crystal_min_age, nothing refuted yet
    clock.advance(minutes=5)              # the refute lands after the crystal was written
    lake.engage(premise.id, "refute", by="robin")
    assert lake.due("crystal") is True
    # a refute that predates the crystal was already in view when it was built, so it does not re-trigger
    other = make_lake("other")
    old = other.write("premise the crystal already knew was refuted", "host", timestamp="2 hours ago")
    other.engage(old.id, "refute", by="robin")
    other.write("I am a crystal built after that refute.", "host", kind="crystal", derived_from=[old.id], dedupe=False)
    assert other.due("crystal") is False


def test_consolidate_backfill_advance_no_regression(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6.1: advance_watermark on an older backfill window moves the watermark pos forward only, never backward."""
    think = fake_think([PROPOSE] * 4)
    lake = make_lake(think=think)
    stretch(lake, 6, minutes_ago=35)
    assert lake.consolidate("container") is not None
    pos = watermark(lake)["pos"]
    start = lake.now() - timedelta(days=30)
    for i in range(6):
        lake.write(f"old row {i} about the lake design", "chat", tags=["topic:lake"], timestamp=start + timedelta(seconds=10 * i))
    window = (start - timedelta(minutes=1), start + timedelta(minutes=10))
    assert lake.consolidate("container", window=window, advance_watermark=True) is not None
    assert watermark(lake)["pos"] == pos  # the older backfill leaves pos where the default run put it
    assert lake.consolidate("container") is None  # already-processed recent rows are not re-offered
    assert lake.last_run is not None and lake.last_run.think_calls == 0


def test_consolidate_crystal_drift_cap(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.3 A2: a rewrite that lurches past drift_cap is rejected and retried with the reason; the accepted
    candidate's cap distance is reused as the recorded drift (no double embedding)."""
    far = "rocket engine orbit fuel launch satellite thrust payload booster nozzle"
    embed = FakeEmbed()
    lake = make_lake(think=FakeThink([]), embed=embed)
    rows = [lake.write(f"otter river lake water stream marsh entry {i}", "chat") for i in range(4)]
    lake.think = FakeThink([adds(rows, "otter")])
    assert lake.consolidate("crystal", min_chars=100) is not None  # the first crystal: no prior, so no cap
    clock.advance(minutes=5)
    new = lake.write("otter river lake water stream marsh entry 5", "chat")
    embed.calls.clear()
    lake.think = think = FakeThink([{"items": [{"op": "revise", "id": f"c{k}", "text": f"{far} {k}", "cite": [new.id]}
                                               for k in range(1, 5)]}, {"items": []}])
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and row.meta is not None and "part 0" in row.content  # the far candidate was rejected
    assert row.meta["drift"]["method"] == "cosine" and 0 <= row.meta["drift"]["value"] <= 0.5
    assert "per-rewrite cap: keep more items" in think.prompts[1]
    assert [len(c) for c in embed.calls] == [2, 2, 1]  # one cap check per candidate, then the row's own embed
    clock.advance(minutes=5)
    new = lake.write("otter river lake water stream marsh entry 6", "chat")
    lake.think = FakeThink([{"items": [{"op": "revise", "id": f"c{k}", "text": f"{far} {k}", "cite": [new.id]}
                                       for k in range(1, 5)]}] * 2)
    with pytest.raises(ConsolidateError, match="per-rewrite cap"):
        lake.consolidate("crystal", min_chars=100)


def test_consolidate_crystal_tiny_budget_provenance(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§1: a crystal whose budget is too small for any section line still cites at least one row."""
    lake = make_lake(think=fake_think([]), write_crystal_file=False)
    rows = stretch(lake, 5, minutes_ago=5)
    lake.think = FakeThink([adds(rows[-1:], "tiny")])
    row = lake.consolidate("crystal", budget=300, min_chars=100)
    assert row is not None and row.source == "lake:crystal"
    assert row.derived_from == [rows[-1].id]
    edges = lake.conn.execute("SELECT count(*) FROM derived_from WHERE delta_id = ?", (row.id,)).fetchone()[0]
    assert edges == 1


# --- AAA phase 3: answer parsing, citations, the input-set invariant (§4.9, §6, §6.1) ----------------------------

GOLDEN = Path(__file__).parent / "golden"
ID_RE = re.compile(r"\b[0-9a-f]{12}\b")
SESSION = {"title": "Tidewater deploy session", "summary": "Moved the deploy and fixed the port."}


def golden_ids(text: str) -> str:
    """Replace every 12-hex id with <id1>, <id2>, … in order of first appearance (ids are random per run)."""
    seen: dict[str, str] = {}
    return ID_RE.sub(lambda m: seen.setdefault(m.group(0), f"<id{len(seen) + 1}>"), text)


@pytest.mark.parametrize(("text", "expected"), [
    ('Sure.\n```json\n{"a": 1}\n```\nHope that helps.', {"a": 1}),
    ('```json\n{"a": 1,}\n```\nor\n```\n{"b": 2}\n```', {"b": 2}),
    ('Rows like {id} and {x} are listed; answer: {"a": {"b": 1}} ok', {"a": {"b": 1}}),
    ('<think>\nweigh {it}\n</think>\n```json\n{"a": 2}\n```', {"a": 2}),
    ('{"propose": {"title": "t", "summary": "s"}}', {"propose": {"title": "t", "summary": "s"}}),
    ('{"propose": {"title": "t", "summary": "s"},}', None),  # a malformed wrapper is skipped whole, never unwrapped
    ('{"kind": "propose", "meta": {"x": 1}, "title": "t" "summary": "s"}', None),
    ('{"bad": 1,} then {"good": 2}', {"good": 2}),
    ('{"a": "a } in a string", "b": 1}', {"a": "a } in a string", "b": 1}),
    ("no object at all", None),
    ("[1, 2, 3]", None),
    ("{x}" * 40 + '{"late": true}', None),
])
def test_parse_answer_table(text: str, expected: dict[str, Any] | None) -> None:
    from lake import parse_answer
    assert parse_answer(text, json=True) == expected
    assert parse_answer({"k": 1}, json=True) == {"k": 1} and parse_answer({"k": 1}, json=False) is None
    assert parse_answer("  <think>x</think> prose  ", json=False) == "prose"


def test_nested_propose_is_rejected_not_unwrapped(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """(f) no key aliasing: a nested {"propose": {...}} parses to the outer object, which validation rejects."""
    think = fake_think([{"propose": PROPOSE}, PROPOSE])
    lake = make_lake(think=think)
    stretch(lake, 3, minutes_ago=35)
    assert lake.consolidate("container") is not None
    assert 'rejected: kind must be "propose" or "skip". Reply with only one JSON object' in think.prompts[1]


def test_container_citations_drop_minority(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6.1 rule 4: 1 of 13 foreign ids is dropped, recorded in meta.dropped_ids, and warned about."""
    lake = make_lake(think=fake_think([]))
    rows = stretch(lake, 13, minutes_ago=40)
    ids = [r.id for r in rows]
    lake.think = FakeThink([{**PROPOSE, "from_ids": [*ids[:12], "29e28ffc7e0"]}])
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == ids[:12]
    assert row.meta is not None and row.meta["dropped_ids"] == ["29e28ffc7e0"]
    assert lake.last_run is not None and lake.last_run.think_calls == 1
    assert lake.last_run.warnings == [
        f"cluster {rows[0].timestamp[:16]}–{rows[-1].timestamp[:16]} (13 rows: {', '.join(ids[:5])}) dropped 1 of 13"
        " ids not in the stretch: 29e28ffc7e0"]


def test_container_citations_reject_majority(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """More than half foreign is a retry with the §12.7 reason; an answer whose kept ids miss the floor too."""
    lake = make_lake(think=fake_think([]))
    rows = stretch(lake, 13, minutes_ago=40)
    ids = [r.id for r in rows]
    fake = [f"{i:012x}" for i in range(7)]
    think = FakeThink([{**PROPOSE, "from_ids": [*ids[:6], *fake]}, PROPOSE])
    lake.think = think
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == ids and "dropped_ids" not in (row.meta or {})
    assert f"rejected: 7 of 13 ids are not in the stretch: {', '.join(fake[:5])}. Reply with only" in think.prompts[1]
    lake2 = make_lake("floor", think=fake_think([]))
    rows2 = stretch(lake2, 4, minutes_ago=40)
    think2 = FakeThink([{**PROPOSE, "from_ids": [rows2[0].id, "000000000000"]}, PROPOSE])
    lake2.think = think2
    assert lake2.consolidate("container") is not None
    assert "rejected: only 1 ids; at least 2 must belong together." in think2.prompts[1]


def test_commit_input_set_invariant(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6 Row written: every derived_from id is in the run's input set and resolves in the file."""
    from lake._consolidate import Run
    lake = make_lake(think=fake_think([]))
    rows = stretch(lake, 3, minutes_ago=40)
    run = Run(lake, "container", {})
    run.inputs.update(r.id for r in rows[:2])
    with pytest.raises(ConsolidateError, match="outside the run's input set: " + rows[2].id):
        run.commit(content="t\n\ns", level=1, tags=[], derived_from=[r.id for r in rows], meta={})
    run.inputs.add("abcdefabcdef")
    with pytest.raises(ConsolidateError, match="missing from the file: abcdefabcdef"):
        run.commit(content="t\n\ns", level=1, tags=[], derived_from=[rows[0].id, "abcdefabcdef"], meta={})
    assert count(lake, "container") == 0
    ok = run.commit(content="t\n\ns", level=1, tags=[], derived_from=[rows[0].id, rows[1].id], meta={})
    assert ok.derived_from == [rows[0].id, rows[1].id]


def test_cluster_prompt_golden(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """Rows without a session tag still cluster and may still skip; the prompt, system and retry are pinned."""
    think = fake_think([{"kind": "nope"}, {"kind": "skip", "reason": "thin"}])
    lake = make_lake(think=think)
    stretch(lake, 3, minutes_ago=40)
    assert lake.consolidate("container") is None and lake.last_run is not None and lake.last_run.skipped == 1
    got = golden_ids("\n\n=== SYSTEM ===\n\n".join([think.prompts[1], think.systems[1] or ""]))
    assert got == (GOLDEN / "container_cluster_prompt.txt").read_text(encoding="utf-8")


# --- AAA phase 3: session groups (§6.1) ----------------------------------------------------------------------------


def session(
    lake: Lake, tag: str, n: int, *, minutes_ago: float, step: int = 60, source: str = "claude-code",
    extra_tags: Sequence[str] = (),
) -> list[Delta]:
    """n rows of one session `session:<tag>`, `step` seconds apart; even rows are user prompts."""
    start = lake.now() - timedelta(minutes=minutes_ago)
    return [
        lake.write(f"{tag} turn {i}: working on part {i} of the task", source,
                   tags=[*(["user"] if i % 2 == 0 else ["assistant"]), f"session:{tag}", *extra_tags],
                   timestamp=start + timedelta(seconds=i * step))
        for i in range(n)
    ]


def containers(lake: Lake) -> list[Delta]:
    rows = lake.conn.execute("SELECT id FROM deltas WHERE kind = 'container' ORDER BY timestamp, seq").fetchall()
    return [d for d in (lake.get(r[0]) for r in rows) if d is not None]


def test_session_groups_are_pure_and_whole(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """Two interleaved sessions give two containers with no cross-session rows; the naming prompt is pinned."""
    think = fake_think([SESSION, SESSION])
    lake = make_lake(think=think, model_name="fake-1")
    a = session(lake, "a", 38, minutes_ago=120, step=60)
    b = session(lake, "b", 5, minutes_ago=110, step=300)
    loose = stretch(lake, 3, minutes_ago=200)  # no session tag: clusters as before
    lake.think = FakeThink([PROPOSE, SESSION, SESSION])
    think = lake.think
    lake.consolidate("container")
    assert lake.last_run is not None and len(lake.last_run.written) == 3 and lake.last_run.think_calls == 3
    by_session = {(r.meta or {}).get("session"): r for r in lake.last_run.written}
    assert by_session["session:a"].derived_from == [r.id for r in a]
    assert by_session["session:b"].derived_from == [r.id for r in b]
    assert by_session[None].derived_from == [r.id for r in loose]
    row = by_session["session:a"]
    assert row.meta is not None and row.meta["part"] == [1, 1] and "fallback" not in row.meta
    assert row.meta["title"] == SESSION["title"] and row.level == 1 and "session:a" in row.tags
    assert row.meta["model"] == "fake-1" and row.meta["span"] == [a[0].timestamp, a[-1].timestamp]
    assert "· user: b turn 0:" in think.prompts[2] and "· assistant: b turn 1:" in think.prompts[2]  # §6 row roles
    got = golden_ids("\n\n=== SYSTEM ===\n\n".join([think.prompts[2], think.systems[2] or ""]))
    assert got == (GOLDEN / "container_session_prompt.txt").read_text(encoding="utf-8")
    assert lake.consolidate("container") is None and lake.last_run.think_calls == 0


def test_session_long_splits_into_parts(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """A 130-row session gives 3 parts of at most 60 rows, each carrying meta.part; later parts name the previous."""
    think = fake_think([SESSION, {**SESSION, "title": "Second"}, {**SESSION, "title": "Third"}])
    lake = make_lake(think=think)
    rows = session(lake, "long", 130, minutes_ago=300, step=60)
    lake.consolidate("container")
    assert lake.last_run is not None
    written = lake.last_run.written
    assert [len(r.derived_from) for r in written] == [43, 43, 44]
    assert [i for r in written for i in r.derived_from] == [r.id for r in rows]
    assert [(r.meta or {})["part"] for r in written] == [[1, 3], [2, 3], [3, 3]]
    assert "Part 1 of 3 of this session.\n" in think.prompts[0]
    assert 'Part 2 of 3 of this session; the previous part is titled "Tidewater deploy session".' in think.prompts[1]
    assert 'Part 3 of 3 of this session; the previous part is titled "Second".' in think.prompts[2]


def test_session_gap_splits_episodes(make_lake: MakeLake, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([SESSION, SESSION]))
    first = session(lake, "day", 4, minutes_ago=600)
    start = lake.now() - timedelta(minutes=300)
    second = [lake.write(f"day later turn {i}", "claude-code", tags=["session:day"], timestamp=start + timedelta(minutes=i))
              for i in range(3)]
    lake.consolidate("container")
    assert lake.last_run is not None
    assert [r.derived_from for r in lake.last_run.written] == [[r.id for r in first], [r.id for r in second]]


def test_session_still_open_is_deferred_then_whole(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([SESSION]))
    rows = session(lake, "live", 6, minutes_ago=50, step=300)  # last row 25 minutes ago: inside close_gap
    assert lake.due("container") is False
    assert lake.consolidate("container") is None
    assert lake.last_run is not None and lake.last_run.think_calls == 0
    clock.advance(minutes=10)
    assert lake.due("container") is True
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in rows]


def test_session_straddling_lookback_and_continuation(
    make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink
) -> None:
    """A session that straddles the lookback edge is containered whole; one resumed later gets a continuation."""
    lake = make_lake(think=fake_think([SESSION, SESSION]))
    rows = session(lake, "edge", 6, minutes_ago=7 * 24 * 60 + 60, step=1200)  # starts 1h before lookback
    assert rows[0].timestamp < lake.now().isoformat() and rows[-1].timestamp > (lake.now() - timedelta(days=7)).isoformat()
    row = lake.consolidate("container", backfill=False)
    assert row is not None and row.derived_from == [r.id for r in rows]
    clock.advance(days=1)
    more = session(lake, "edge", 4, minutes_ago=120)
    row2 = lake.consolidate("container")
    assert row2 is not None and row2.derived_from == [r.id for r in more]
    assert lake.last_run is not None and len(lake.last_run.written) == 1


def test_session_resumed_ending_in_noise_is_containered_later(
    make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink
) -> None:
    """A covered session resumes and ends in a noise row inside close_gap: the noise row never holds the session
    open, so its continuation is containered (review regression: the watermark passed the deferred rows for good)."""
    lake = make_lake(think=fake_think([SESSION, SESSION]))
    first = session(lake, "a", 5, minutes_ago=300)
    assert lake.consolidate("container") is not None
    clock.advance(hours=2)
    more = session(lake, "a", 5, minutes_ago=50)
    lake.write("ok", "claude-code", tags=["user", "session:a"], timestamp=lake.now() - timedelta(minutes=5))
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in more] and first[0].id not in row.derived_from
    clock.advance(hours=1)
    assert lake.consolidate("container") is None and lake.due("container") is False


def test_session_resumed_real_row_deferral_is_rediscovered(
    make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink
) -> None:
    """A resumed session whose last candidate row is inside close_gap waits, although the watermark passes its older
    rows; the next run sees that row, rediscovers the session, and containers every uncovered row."""
    lake = make_lake(think=fake_think([SESSION, SESSION]))
    session(lake, "a", 5, minutes_ago=300)
    assert lake.consolidate("container") is not None
    clock.advance(hours=2)
    more = [*session(lake, "a", 5, minutes_ago=50), *session(lake, "a", 1, minutes_ago=5)]
    assert lake.consolidate("container") is None and lake.last_run is not None and lake.last_run.think_calls == 0
    clock.advance(hours=1)
    assert lake.due("container") is True
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in more]


def test_session_future_row_does_not_block(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """A session row dated in the future (host clock skew, an import) never holds the session open."""
    lake = make_lake(think=fake_think([SESSION]))
    rows = session(lake, "a", 10, minutes_ago=300)
    lake.write("stray future row", "claude-code", tags=["session:a"], timestamp=lake.now() + timedelta(days=2))
    assert lake.due("container") is True
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in rows]


def test_session_small_gap_pieces_join_a_neighbour(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """Pieces under min_rows after a session_gap split join the neighbour across the smaller gap: rows at +0h, +0.1h,
    +4h, +4.1h give one 4-row part; a 2-row tail 5h after a 5-row episode joins it."""
    lake = make_lake(think=fake_think([SESSION, SESSION]))
    start = lake.now() - timedelta(hours=10)
    g = [lake.write(f"turn {i} on the thing", "claude-code", tags=["user", "session:g"],
                    timestamp=start + timedelta(hours=h)) for i, h in enumerate([0, 0.1, 4, 4.1])]
    t = [*session(lake, "t", 5, minutes_ago=900), *session(lake, "t", 2, minutes_ago=600)]
    lake.consolidate("container")
    assert lake.last_run is not None
    assert sorted(r.derived_from for r in lake.last_run.written) == sorted([[r.id for r in g], [r.id for r in t]])


def test_session_container_keeps_user_tag(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """Session container tags: the part's shared tags, then those its user rows share, so `user` survives a session
    that is mostly assistant rows and tags=["user"] recall still finds the session's name."""
    lake = make_lake(think=fake_think([SESSION]))
    start = lake.now() - timedelta(minutes=120)
    for i in range(7):
        tags = ["user", "proj"] if i in (0, 4) else ["assistant"]
        lake.write(f"turn {i} on tidewater", "claude-code", tags=[*tags, "session:m"], timestamp=start + timedelta(minutes=i))
    row = lake.consolidate("container")
    assert row is not None and row.tags == ["session:m", "assistant", "user", "proj"]
    assert row.id in [h.delta.id for h in lake.recall("tidewater", tags=["user"])]


@pytest.mark.parametrize("answer", [
    {"kind": "skip", "reason": "a grab-bag of small tasks"},
    "This session is a grab-bag; I would not name it.",
    {"name": "A session", "member_ids": []},
    {"propose": SESSION},
], ids=["skip", "prose", "wrongkeys", "nested"])
def test_session_fallback_is_extractive(make_lake: MakeLake, fake_think: MakeThink, answer: object) -> None:
    think = fake_think([answer, answer])
    lake = make_lake(think=think)
    rows = session(lake, "s1", 5, minutes_ago=90)
    row = lake.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in rows] and len(think.calls) == 2
    assert row.meta is not None and row.meta["fallback"] == "extractive" and row.meta["session"] == "session:s1"
    assert row.content == (
        f"claude-code session {rows[0].timestamp[:10]}: s1 turn 0: working on part 0 of the task\n\n"
        f"5 rows, {rows[0].timestamp[:16]} to {rows[-1].timestamp[:16]}. Last row: s1 turn 4: working on part 4 of the task")
    assert lake.last_run is not None and len(lake.last_run.warnings) == 1
    warning = lake.last_run.warnings[0]
    assert warning.startswith(f"session session:s1 {rows[0].timestamp[:16]}–{rows[-1].timestamp[:16]} (5 rows: {rows[0].id}")
    assert " rejected twice: " in warning and warning.endswith("; wrote an extractive container")
    reason = "no JSON object in the answer" if isinstance(answer, str) else "title is empty"
    assert f"rejected: {reason}. Reply with only one JSON object in exactly this shape:\n" in think.prompts[1]
    assert think.prompts[1].endswith('"changes": ["<row-id>", ...]}')


def test_session_fallback_on_think_error(make_lake: MakeLake) -> None:
    from lake import LakeError

    def down(prompt: str, *, system: str | None = None, json: bool = False) -> dict[str, Any]:
        raise LakeError("claude exited 1: Not logged in")

    lake = make_lake(think=down)
    rows = session(lake, "s1", 4, minutes_ago=90)
    loose = stretch(lake, 3, minutes_ago=300)
    with pytest.raises(LakeError):  # a cluster unit still propagates a think exception (§6)
        lake.consolidate("container")
    assert count(lake, "container") == 0 and loose
    lake2 = make_lake("only-session", think=down)
    rows = session(lake2, "s1", 4, minutes_ago=90)
    row = lake2.consolidate("container")
    assert row is not None and row.derived_from == [r.id for r in rows] and (row.meta or {})["fallback"] == "extractive"
    assert lake2.last_run is not None and "think failed: claude exited 1: Not logged in" in lake2.last_run.warnings[0]


def test_session_backfill_bounded_newest_first(make_lake: MakeLake, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([]))
    old = [session(lake, f"old{i}", 3, minutes_ago=(30 - i) * 24 * 60) for i in range(6)]  # 30..25 days ago
    think = FakeThink([SESSION] * 6)
    lake.think = think
    assert lake.due("container") is True
    lake.consolidate("container")
    assert lake.last_run is not None and lake.last_run.think_calls == 4
    got = [r.derived_from for r in lake.last_run.written]
    assert got == [[r.id for r in old[i]] for i in (5, 4, 3, 2)], "newest first, backfill_max 4"
    assert all((r.meta or {})["backfill"] is True and (r.meta or {})["window"] == (r.meta or {})["span"]
               for r in lake.last_run.written)
    assert lake.due("container") is True
    lake.consolidate("container")
    assert [r.derived_from for r in lake.last_run.written] == [[r.id for r in old[i]] for i in (1, 0)]
    assert lake.due("container") is False
    lake.consolidate("container")
    assert lake.last_run.written == [] and lake.last_run.think_calls == 0


def test_session_backfill_skips_partly_covered_and_honours_scope(make_lake: MakeLake, fake_think: MakeThink) -> None:
    lake = make_lake(think=fake_think([PROPOSE]))
    part = session(lake, "part", 6, minutes_ago=20 * 24 * 60)
    lake.consolidate("container", inputs=[part[0].id, part[1].id])  # an older container covers two rows
    other = session(lake, "codex", 3, minutes_ago=21 * 24 * 60, source="codex")
    think = FakeThink([SESSION])
    lake.think = think
    lake.consolidate("container", source=["claude-code"])
    assert lake.last_run is not None and lake.last_run.written == [] and lake.last_run.think_calls == 0
    lake.consolidate("container", source=["codex"])
    assert [r.derived_from for r in lake.last_run.written] == [[r.id for r in other]]
    assert (lake.last_run.written[0].meta or {})["filters"] == {"source": ["codex"]}
    assert lake.due("container") is False
    assert lake.consolidate("container", backfill=False) is None


def test_session_host_container_is_not_coverage(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """A host-written container (not source lake:container) citing one row of a session is a lineage pointer: the
    session is still backfilled, whole."""
    lake = make_lake(think=fake_think([SESSION]))
    rows = session(lake, "old", 4, minutes_ago=20 * 24 * 60)
    lake.write("a host summary of one decision", "claude-code", kind="container", derived_from=[rows[1].id])
    lake.consolidate("container")
    assert lake.last_run is not None and [r.derived_from for r in lake.last_run.written] == [[r.id for r in rows]]


def test_session_prefix_off_restores_clustering(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think([PROPOSE])
    lake = make_lake(think=think)
    rows = session(lake, "s1", 4, minutes_ago=90)
    row = lake.consolidate("container", session_prefix="")
    assert row is not None and row.derived_from == [r.id for r in rows] and "session" not in (row.meta or {})
    assert "══ THE STRETCH ══" in think.prompts[0]


# --- AAA phase 3: supersession detection (§6.1) --------------------------------------------------------------------

STALE = "tidewater deploys to Fly.io region ord for now"
CHANGE = "Change of plan: tidewater moves off Fly.io to Hetzner in Falkenstein"


def change_session(lake: Lake) -> tuple[Delta, list[Delta]]:
    """An older host row stating a value, and a closed 4-row session whose second row replaces it."""
    old = lake.write(STALE, "notes-app", timestamp=lake.now() - timedelta(days=3))
    start = lake.now() - timedelta(minutes=90)
    texts = ["tidewater: reading the deploy config again", CHANGE, "updated the runbook for the new host",
             "ran the smoke tests, all green"]
    rows = [lake.write(t, "claude-code", tags=["user" if i % 2 == 0 else "assistant", "session:s1"],
                       timestamp=start + timedelta(minutes=i)) for i, t in enumerate(texts)]
    return old, rows


def test_supersede_pairs_validated_and_written(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """changes flag the correcting row; code retrieves the stale row; the supersede call's pairs are checked in code
    (a value not quoted from its row, an old not shown, an old newer than new are dropped with a warning) and the
    valid pair lands in the session container's meta.supersedes, which then drives ranking."""
    lake = make_lake()
    old, rows = change_session(lake)
    new = rows[1]
    good = {"new": new.id, "old": old.id, "old_value": "Fly.io  REGION ord", "new_value": "Hetzner in Falkenstein"}
    think = fake_think([
        {**SESSION, "changes": [new.id, "zzzzzzzzzzzz"]},
        {"pairs": [good, {**good, "old_value": "Render"}, {**good, "old": rows[3].id},
                   {"new": rows[0].id, "old": old.id, "old_value": "tidewater", "new_value": "tidewater"}]},
    ])
    lake.think = think
    row = lake.consolidate("container")
    assert row is not None and lake.last_run is not None and lake.last_run.think_calls == 2
    assert row.meta is not None and row.meta["supersedes"] == [{**good, "old_value": "Fly.io  REGION ord"}]
    assert row.derived_from == [r.id for r in rows]  # the supersede call's rows never enter derived_from
    prompt, system = think.prompts[1], think.systems[1] or ""
    assert prompt.startswith("Now (the lake's clock): ") and f"FLAGGED [{new.id}]" in prompt
    assert f"  earlier: [{old.id}]" in prompt and rows[3].id not in prompt
    assert system.startswith("You are checking corrections in a memory.") and system.rstrip().endswith('{"pairs": []}')
    warnings = lake.last_run.warnings
    assert any("changes kept the rest: 1 of 2 ids are not in the stretch: zzzzzzzzzzzz" in w for w in warnings)
    dropped = next(w for w in warnings if "supersede pairs" in w)
    assert "dropped 3 of 4 supersede pairs" in dropped and "not quoted from its row" in dropped
    assert "is not a flagged row and a candidate shown under it" in dropped
    got = lake.get(old.id)
    assert got is not None and [x.id for x in got.superseded_by] == [new.id]
    order = [h.delta.id for h in lake.recall("tidewater Fly.io region")]
    assert order.index(new.id) < order.index(old.id)


def test_supersede_candidates_honour_run_scope(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """The supersede prompt and its links stay inside the run's scope: rows of an excluded source or carrying an
    excluded tag never reach it, however well they match the flagged row."""
    lake = make_lake()
    _, rows = change_session(lake)
    secret = lake.write(f"SECRETJOURNAL {STALE}", "private-journal", timestamp=lake.now() - timedelta(days=2))
    private = lake.write(f"PRIVATETAG {STALE}", "notes-app", tags=["private"], timestamp=lake.now() - timedelta(days=2))
    think = fake_think([{**SESSION, "changes": [rows[1].id]}, {"pairs": []}])
    lake.think = think
    assert lake.consolidate("container", exclude_sources=["private-journal"], exclude_tags=["private"]) is not None
    assert len(think.prompts) == 2 and "earlier: [" in think.prompts[1]
    for prompt in think.prompts:
        assert "SECRETJOURNAL" not in prompt and secret.id not in prompt
        assert "PRIVATETAG" not in prompt and private.id not in prompt


def test_supersede_failure_keeps_container(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """A supersede call answered in prose twice, or raising LakeError, writes no links and a warning; the session
    container is written as named."""
    lake = make_lake()
    old, rows = change_session(lake)
    lake.think = fake_think([{**SESSION, "changes": [rows[1].id]}, "no JSON here", "still none"])
    row = lake.consolidate("container")
    assert row is not None and row.meta is not None and "supersedes" not in row.meta and "fallback" not in row.meta
    assert lake.last_run is not None and lake.last_run.think_calls == 3
    assert any("supersede call: no JSON object in the answer; no links written" in w for w in lake.last_run.warnings)
    assert "Your previous answer was rejected: no JSON object in the answer." in lake.think.prompts[2]  # type: ignore[union-attr]

    calls: list[str] = []

    def flaky(prompt: str, *, system: str | None = None, json: bool = False) -> dict[str, Any]:
        calls.append(prompt)
        if len(calls) == 2:
            raise LakeError("model down")
        return {**SESSION, "changes": [c.id for c in change_rows]}

    lake2 = make_lake("second", think=flaky)
    _, change_rows = change_session(lake2)
    row = lake2.consolidate("container")
    assert row is not None and row.meta is not None and "supersedes" not in row.meta and len(calls) == 2
    assert any("supersede call: think failed: model down" in w for w in lake2.last_run.warnings)  # type: ignore[union-attr]


@pytest.mark.parametrize("changes", [None, [], ["aaaaaaaaaaaa", "bbbbbbbbbbbb", "x"], "not a list"])
def test_supersede_no_flags_no_call(make_lake: MakeLake, fake_think: MakeThink, changes: object) -> None:
    """No `changes` (absent, empty, mostly foreign, malformed) means no supersede call and no links; a bad list is
    a warning, never a rejection of the container."""
    lake = make_lake()
    change_session(lake)
    answer = dict(SESSION) if changes is None else {**SESSION, "changes": changes}
    lake.think = fake_think([answer])
    row = lake.consolidate("container")
    assert row is not None and row.meta is not None and "supersedes" not in row.meta and "fallback" not in row.meta
    assert lake.last_run is not None and lake.last_run.think_calls == 1
    assert (len(lake.last_run.warnings) == 1) == (changes not in (None, []))


def test_supersede_without_candidates_no_call(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """A flagged row with no earlier host row sharing a token: nothing to show, so no second call."""
    lake = make_lake()
    rows = session(lake, "only", 4, minutes_ago=90)
    lake.think = fake_think([{**SESSION, "changes": [rows[0].id]}])
    row = lake.consolidate("container")
    assert row is not None and lake.last_run is not None and lake.last_run.think_calls == 1
