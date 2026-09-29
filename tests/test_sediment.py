"""§10 tests for the §6.5 sediment pass (test_sediment_*) and its §13.10 wire row
(test_serve_sediment): automatic on the deep path, the switch, the gates, prompt and row shape,
retry and failure warnings, no lease + dedupe, and the server-side think powering remote recalls."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeThink, script, served

from lake import ClosedLoopError, Delta, Hit, Lake, RemoteLake
from lake import _time
from lake._context import render_line
from lake._db import meta_get, meta_set, new_id
from lake._store import insert_row
from lake._types import TimelineRow

MakeLake = Callable[..., Lake]
MakeThink = Callable[[Sequence[str | dict[str, Any]]], FakeThink]

STEP = {"id": "s", "search": "lake migration sqlite"}
PROMPT = (resources.files("lake") / "prompts" / "sediment.txt").read_text(encoding="utf-8")


def seed(lake: Lake) -> list[Delta]:
    """Three rows across two sources that all match STEP's search tokens."""
    return [
        lake.write("the lake migration to sqlite started with one file", "alice", timestamp="30 minutes ago"),
        lake.write("sqlite journals made the lake migration safe", "bob", timestamp="20 minutes ago"),
        lake.write("the lake migration wrapped up in sqlite last night", "alice", timestamp="10 minutes ago"),
    ]


def hit_ids(hits: Sequence[Hit]) -> list[str]:
    return [h.delta.id for h in hits]


def sediments(lake: Lake) -> list[Delta]:
    """Every kind=sediment row, newest first (straight SQL: no recall, no warnings reset)."""
    from lake._db import DELTA_COLS, rows_to_deltas

    sql = f"SELECT {DELTA_COLS} FROM deltas d WHERE d.kind = 'sediment' ORDER BY d.timestamp DESC, d.seq DESC"
    return rows_to_deltas(lake.conn, lake.conn.execute(sql).fetchall())


def test_sediment_automatic(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think(["I recall it settling once.", "I recall it settling again, differently."])
    lake = make_lake(think=think, model_name="fake-1")
    seed(lake)
    base = lake.recall(plan=[STEP], sediment=False)  # baseline first, so no row can contaminate it
    assert base and sediments(lake) == []
    hits = lake.recall(plan=[STEP])
    assert [(h.delta.id, h.score) for h in hits] == [(h.delta.id, h.score) for h in base], "nothing appended"
    row = sediments(lake)[0]
    assert (row.source, row.kind, row.level, row.tags) == ("lake:sediment", "sediment", 0, [])
    assert row.content == "I recall it settling once." and row.expires_at is None
    assert row.derived_from == hit_ids(hits), "the final hits' ids in hit order"
    assert row.meta == {"model": "fake-1", "query": "lake migration sqlite", "grounding": "external"}
    res = lake.plan([STEP])
    assert res.sediment is not None and res.sediment.content == "I recall it settling again, differently."
    assert res.sediment.derived_from == hit_ids(hits) and lake.get(res.sediment.id) == res.sediment
    assert lake.last_run is None, "sediment is a read-path write, not a consolidate run"
    n = lake.conn.execute("SELECT count(*) FROM meta WHERE key LIKE 'last_consolidate%'").fetchone()[0]
    assert int(n) == 0


def test_sediment_switch(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think(["I recall the one forced take."])
    lake = make_lake(think=think, model_name="fake-1")
    seed(lake)
    assert lake.recall(plan=[STEP], sediment=False) and sediments(lake) == []
    assert lake.recall(plan=[STEP], sediment=True) and len(sediments(lake)) == 1
    with pytest.raises(ValueError, match="needs a plan"):
        lake.recall("lake migration", sediment=True)
    assert lake.recall("lake migration") and lake.context("lake migration")
    assert len(sediments(lake)) == 1 and len(think.calls) == 1, "non-plan recall and context never sediment"


def test_sediment_gates(make_lake: MakeLake, fake_think: MakeThink, tmp_path: Path) -> None:
    one = make_lake("one", think=fake_think([]))  # any think call would leave a 'sediment failed' warning
    one.write("the lake migration to sqlite started well", "alice", timestamp="20 minutes ago")
    one.write("the lake migration to sqlite kept going", "alice", timestamp="10 minutes ago")
    assert one.recall(plan=[STEP], sediment=True), "hits still returned"
    assert sediments(one) == [] and one.last_warnings == [], "one source: shut gate, no warning"
    two = make_lake("two")  # no think
    seed(two)
    assert two.recall(plan=[STEP], sediment=True) and sediments(two) == [] and two.last_warnings == []
    three = make_lake("three")
    seed(three)
    ro = make_lake("three", think=fake_think([]), readonly=True)
    assert ro.recall(plan=[STEP], sediment=True) and sediments(ro) == [] and ro.last_warnings == []
    four = make_lake("four", think=fake_think(["I recall the flattened moment holding together."]))
    seed(four)
    res = four.plan([STEP, {"id": "g", "aggregate": "s", "group_by": "source"}])
    assert res.sediment is None and res.steps["g"].buckets and sediments(four) == []
    res = four.plan([STEP, {"id": "tl", "timeline": "s"}])
    assert res.sediment is not None and res.sediment.kind == "sediment"
    parents = [four.get(i) for i in res.sediment.derived_from]
    assert len({d.source for d in parents if d is not None}) >= 2, "flattened timeline rows span two sources"


def test_sediment_prompt_and_row(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think(["I recall the whole sweep converging.", "I recall the filtered pair too."])
    lake = make_lake(think=think, model_name="fake-1")
    for i in range(25):
        lake.write(f"lake migration sqlite file format note {i:02d}", "alice" if i % 2 else "bob",
                   timestamp=f"{60 - i} minutes ago")
    plan = [{"id": "a", "search": "lake migration sqlite", "limit": 25},
            {"id": "b", "search": "sqlite   file\nformat"}, {"id": "u", "union": ["a", "b"]}]
    hits = lake.recall(plan=plan)
    assert len(hits) == 25
    joined = "lake migration sqlite · sqlite file format"
    prompt, system, as_json = think.calls[0]
    assert system == PROMPT and as_json is False, "the shipped system prompt, called with json=False"
    head, _, body = prompt.partition("\n\nMemories that surfaced:\n")
    assert head == f"Now (the lake's clock): 2026-09-02T18:00Z\n\nQuery: \"{joined}\"", "the §6 clock line, then the query"
    assert "is not memory; never state it." in PROMPT, "the §6 sources sentence closes the system prompt"
    lines = body.split("\n")
    assert len(lines) == 20, "the first 20 final hits, and only those"
    want = [f"[{h.delta.id}] {h.delta.timestamp[:16]} {h.delta.source} · {h.delta.content}" for h in hits[:20]]
    assert lines == want, "§6 row lines in hit order"
    row = sediments(lake)[0]
    assert row.derived_from == hit_ids(hits)[:20]
    assert row.meta == {"model": "fake-1", "query": joined, "grounding": "external"}
    res = lake.plan([{"id": "f", "filter": {"source": ["alice", "bob"]}}])  # no search step anywhere
    assert think.calls[1][0].startswith("Now (the lake's clock): 2026-09-02T18:00Z\n\nMemories that surfaced:\n"), \
        "Query line and blank line omitted"
    assert res.sediment is not None and res.sediment.meta == {"model": "fake-1", "grounding": "external"}


def test_sediment_retry_and_failure(make_lake: MakeLake, fake_think: MakeThink) -> None:
    think = fake_think(["", "I recall the retry landing."])
    lake = make_lake(think=think, model_name="fake-1")
    seed(lake)
    assert lake.recall(plan=[STEP])
    assert [d.content for d in sediments(lake)] == ["I recall the retry landing."]
    assert len(think.calls) == 2
    assert think.prompts[1] == think.prompts[0] + "\n\nYour previous answer was rejected: empty answer."
    twice = make_lake("twice", think=fake_think(["", "  "]))
    seed(twice)
    res = twice.plan([STEP])
    assert res.sediment is None and sediments(twice) == []
    step_hits = res.steps["s"].hits
    assert step_hits, "hits still returned"
    assert "sediment rejected twice: empty answer" in res.warnings
    assert "sediment rejected twice: empty answer" in twice.last_warnings

    def boom(prompt: str, *, system: str | None = None, json: bool = False) -> str:
        raise RuntimeError("think down")

    broken = make_lake("broken", think=boom)
    seed(broken)
    assert broken.recall(plan=[STEP]), "hits still returned when think raises (§4.2 does not apply here)"
    assert sediments(broken) == [] and broken.last_warnings == ["sediment failed: think down"]


def test_sediment_no_lease_dedupe(make_lake: MakeLake, fake_think: MakeThink) -> None:
    prose = "I recall one settled take."
    lake = make_lake(think=fake_think([prose, prose]), model_name="fake-1")
    rows = seed(lake)
    with lake.tx() as conn:
        meta_set(conn, "consolidate_lease:mood", "9999-01-01T00:00:00.000Z")
    first = lake.plan([STEP])
    assert first.sediment is not None, "a live consolidate lease never blocks the pass"
    assert meta_get(lake.conn, "consolidate_lease:sediment") is None
    second = lake.plan([STEP])
    assert second.sediment == first.sediment, "byte-identical prose dedupes to the first row (§4.4)"
    assert len(sediments(lake)) == 1
    assert hit_ids(lake.recall(kind="sediment")) == [first.sediment.id], "findable by the next recall"
    with pytest.raises(ClosedLoopError):
        lake.write("an impostor", "lake:sediment")
    own = lake.write("my own deliberate take on it", "claude-code", kind="sediment", derived_from=[rows[0].id])
    assert (own.kind, own.source) == ("sediment", "claude-code"), "the host's own sediment coexists"


def test_sediment_cites_live_expiring_row(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6.5/§12.3: sediment reads the deep recall's live hits with no TTL guard, so a still-live
    expiring row can be cited, and the sediment row it lays down is itself permanent."""
    think = fake_think(["I recall the migration settling, the ephemeral note folded in."])
    lake = make_lake(think=think, model_name="fake-1")
    lake.write("the lake migration to sqlite started with one file", "alice", timestamp="30 minutes ago")
    ephemeral = lake.write(
        "the lake migration sqlite note that will expire", "bob", timestamp="10 minutes ago", expires="1h"
    )
    assert ephemeral.expires_at is not None, "the input row carries a still-future TTL"
    hits = lake.recall(plan=[STEP])
    assert ephemeral.id in hit_ids(hits), "the live expiring row is a final hit"
    row = sediments(lake)[0]
    assert ephemeral.id in row.derived_from, "sediment cites the still-live expiring row"
    assert row.expires_at is None, "the sediment row is permanent — the TTL does not propagate"


def test_sediment_self_referential(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """Two distinct lake:* sources pass the 2-source gate yet label self-referential (§6.5)."""
    think = fake_think(["I recall my own prior takes echoing back."])
    lake = make_lake(think=think, model_name="fake-1")
    anchor = lake.write("plain host memory about the lake", "alice", timestamp="30 minutes ago")
    ts = _time.dt_to_ts(lake.now())
    with lake.tx() as conn:
        for src in ("lake:container", "lake:crystal"):
            insert_row(
                conn, delta_id=new_id(conn), timestamp=ts, content=f"zephyrium synthesis from {src}",
                source=src, kind=src.split(":", 1)[1], level=1 if src.endswith("container") else 0,
                tags=[], derived_from=[anchor.id], expires_at=None, media_hash=None, meta={"model": "fake-1"},
            )
    hits = lake.recall(plan=[{"id": "z", "search": "zephyrium"}])
    assert {h.delta.source for h in hits} == {"lake:container", "lake:crystal"}, "only the two lake:* rows"
    row = sediments(lake)[0]
    assert row.meta is not None and row.meta["grounding"] == "self-referential"
    parents = [lake.get(i) for i in row.derived_from]
    assert parents and all(p is not None and p.source.startswith("lake:") for p in parents)


def test_sediment_boost_by_grounding(make_lake: MakeLake) -> None:
    """§5.3: external sediment out-scores a self-referential, unlabelled, or plain row of equal relevance."""
    lake = make_lake()
    ts = _time.dt_to_ts(lake.now())
    anchor = lake.write("plain probe anchor row", "alice", timestamp=ts)  # same instant: equal evidence-time recency
    content = "sediment grounding boost probe row"
    metas: dict[str, dict[str, Any] | None] = {
        "external": {"grounding": "external"}, "self": {"grounding": "self-referential"},
        "unlabelled": {"model": "x"}, "plain": None,
    }
    ids: dict[str, str] = {}
    with lake.tx() as conn:
        for key, meta in metas.items():
            did = new_id(conn)
            ids[key] = did
            insert_row(
                conn, delta_id=did, timestamp=ts, content=content,
                source="alice" if key == "plain" else "claude-code",
                kind=None if key == "plain" else "sediment", level=0, tags=[],
                derived_from=[] if key == "plain" else [anchor.id], expires_at=None, media_hash=None, meta=meta,
            )
    score = {h.delta.id: h.score for h in lake.recall(content)}
    assert score[ids["external"]] > score[ids["self"]], "external sediment earns the 1/0.92 boost"
    assert score[ids["self"]] == score[ids["unlabelled"]] == score[ids["plain"]], "no boost otherwise"


def test_sediment_render_marker(make_lake: MakeLake) -> None:
    """§5.6.2: a self-referential sediment renders the visible marker; external and unlabelled do not."""
    def sed(meta: dict[str, Any] | None) -> Delta:
        return Delta(
            id="a" * 12, timestamp="2026-01-01T00:00:00.000Z", content="I recall my own echo.",
            source="lake:sediment", kind="sediment", level=0, tags=[], derived_from=["b" * 12],
            expires_at=None, media_hash=None, meta=meta, engagement=None,
        )
    lab = {"user_role": "user", "assistant_role": "assistant"}
    assert "(self-referential)" in render_line(TimelineRow(sed({"grounding": "self-referential"}), True), lab)
    assert "(self-referential)" not in render_line(TimelineRow(sed({"grounding": "external"}), True), lab)
    assert "(self-referential)" not in render_line(TimelineRow(sed(None), True), lab)


def test_sediment_grounding_by_role(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """§6.5 grounding follows whose statement a source was. The assistant's own reply (tag=assistant) is
    self-report even under a host source, so a sediment over it and a lake row is self-referential — a
    conclusion echoed back through a captured reply cannot launder itself into external standing. A user
    statement (tag=user, the same host source) is genuine evidence and grounds the sediment."""
    def ground(tag: str) -> str:
        lake = make_lake(tag, think=fake_think([f"I recall zephyrium from the {tag} line."]), model_name="fake-1")
        anchor = lake.write("plain host memory about the deep water", "alice", timestamp="40 minutes ago")
        lake.write("zephyrium synthesis is basically settled", "claude-code", tags=[tag], timestamp="30 minutes ago")
        ts = _time.dt_to_ts(lake.now())
        with lake.tx() as conn:
            insert_row(
                conn, delta_id=new_id(conn), timestamp=ts, content="zephyrium synthesis from the container",
                source="lake:container", kind="container", level=1, tags=[], derived_from=[anchor.id],
                expires_at=None, media_hash=None, meta={"model": "fake-1"},
            )
        hits = lake.recall(plan=[{"id": "z", "search": "zephyrium"}])
        assert {h.delta.source for h in hits} == {"claude-code", "lake:container"}, "one host reply, one lake row"
        row = sediments(lake)[0]
        assert row.meta is not None
        return str(row.meta["grounding"])

    assert ground("assistant") == "self-referential"  # the echoed reply grants nothing
    assert ground("user") == "external"                # the user statement grounds it


def test_rests_on_refuted_render_marker() -> None:
    """§5.6.2: a row resting on a corrected memory renders the dependency marker; one that rests on
    nothing does not."""
    from lake._types import DEFAULT_LABELS

    def box(rests: tuple[str, ...]) -> Delta:
        return Delta(
            id="a" * 12, timestamp="2026-01-01T00:00:00.000Z", content="a conclusion built on a premise",
            source="lake:container", kind="container", level=1, tags=[], derived_from=["b" * 12],
            expires_at=None, media_hash=None, meta={"model": "x"}, engagement=None, rests_on_refuted=rests,
        )
    assert "rests on ×1 corrected" in render_line(TimelineRow(box(("b" * 12,)), True), DEFAULT_LABELS)
    assert "rests on" not in render_line(TimelineRow(box(()), True), DEFAULT_LABELS)


def test_serve_sediment(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        seeded = [d.id for d in seed(lk)]
    think = script(tmp_path, "think.sh", "echo 'I recall the served take.'")
    home = tmp_path / "home"
    with served(home, "--file", str(file), "--think", f"cmd:{think}", "--model-name", "fake-1") as url:
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            hits = rl.recall(plan=[STEP])
            assert sorted(hit_ids(hits)) == sorted(seeded), "the hits cross unchanged"
            with Lake(file, readonly=True) as lk:
                rows = sediments(lk)
            assert len(rows) == 1 and rows[0].source == "lake:sediment", "the fake think ran server-side"
            assert rows[0].content == "I recall the served take." and rows[0].derived_from == hit_ids(hits)
            res = rl.plan([STEP])
            assert res.sediment is not None and res.sediment.content == "I recall the served take."
            assert rl.recall(plan=[STEP], sediment=False)
            with Lake(file, readonly=True) as lk:
                assert len(sediments(lk)) == 1, "sediment: false writes none; identical prose deduped"
            with pytest.raises(ValueError):
                rl.recall("lake migration", sediment=True)
    with served(home, "--file", str(file)) as url:  # the same server without a think
        with RemoteLake(url, spool_path=tmp_path / "s2.jsonl") as rl:
            assert rl.recall(plan=[STEP]), "still answers 200 with hits"
    with Lake(file, readonly=True) as lk:
        assert len(sediments(lk)) == 1, "no think: no new row"
