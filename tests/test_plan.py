"""§10 tests owned by _plan: every test_plan_* over the §5.5 fixture lake (frozen clock, 3650 d half-life)."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from conftest import FrozenClock

from lake import Bucket, CollapsedRun, Hit, Lake, PlanError, TimelineRow
from lake import _plan, _recall

MakeLake = Callable[..., Lake]
FIXTURE_NOW = "2026-05-06T10:00:00.000Z"
ROWS: tuple[tuple[str, str, str, tuple[str, ...], str], ...] = (  # §5.5: id, timestamp, source, tags, content
    ("r1", "2026-04-29T14:00:00.000Z", "claude-code", ("migration", "team"), "Discussed the postgres migration plan for Thursday with the team"),
    ("r2", "2026-04-29T14:00:20.000Z", "claude-code", (), "ok"),
    ("r3", "2026-04-29T14:00:40.000Z", "claude-code", ("migration",), "We agreed to run the migration at 09:00 Thursday after the backup finishes"),
    ("r4", "2026-04-29T14:01:00.000Z", "agent-heartbeat", (), "heartbeat"),
    ("r5", "2026-04-29T14:01:05.000Z", "agent-heartbeat", (), "heartbeat"),
    ("r6", "2026-04-29T14:01:10.000Z", "agent-heartbeat", (), "heartbeat"),
    ("r7", "2026-04-29T14:02:00.000Z", "claude-code", ("backup",), "Backup finishes around 08:30 so 09:00 gives us margin"),
    ("r8", "2026-04-29T15:30:00.000Z", "fathom-chat", ("nova", "kitchen", "fathom-chat", "chat:sunday"), "Nova stretched mozzarella in the kitchen tonight"),
    ("r9", "2026-04-29T15:30:30.000Z", "fathom-chat", ("fathom-chat", "chat:sunday"), "short remark"),
    ("r10", "2026-05-06T10:00:00.000Z", "claude-code", ("migration", "backup"), "Migration done; the backup took 40 minutes"),
    ("r11", "2026-04-20T09:00:00.000Z", "vault/fathom", ("vault",), "## Migration runbook"),
    ("r12", "2026-04-20T09:00:00.400Z", "vault/fathom", ("vault",), "## Rollback steps"),
)
EXAMPLE_1 = {"r1": 1.0, "r2": 0.9, "r3": 0.8, "r10": 0.7, "r9": 0.6, "r7": 0.5}  # worked example 1's rel_fts
KEYWORDS = ("migration", "backup", "heartbeat", "nova", "remark", "rollback")


def build(make_lake: MakeLake, clock: FrozenClock, **kw: Any) -> tuple[Lake, dict[str, str]]:
    """The §5.5 fixture: twelve rows, now frozen at r10's timestamp, half-life 3650 d; r-name -> real id."""
    clock.set(FIXTURE_NOW)
    kw.setdefault("recency_half_life", "3650d")
    kw.setdefault("automation", ["source:agent-heartbeat"])
    lake = make_lake(**kw)
    m = {name: lake.write(text, src, tags=list(tags), timestamp=ts, dedupe=False).id for name, ts, src, tags, text in ROWS}
    return lake, m


def names(hits: Sequence[Hit] | None, m: Mapping[str, str]) -> list[str]:
    inv = {v: k for k, v in m.items()}
    return [inv.get(h.delta.id, h.delta.id) for h in hits or []]


def stipulate(monkeypatch: pytest.MonkeyPatch, m: Mapping[str, str], rels: Mapping[str, float]) -> None:
    """Replace the §5.2 step-1 SQL so rel_fts is the stipulated value per row (the best row at 1.0)."""

    def fake(lake: Lake, match: str, where: str, params: Sequence[object]) -> list[tuple[Any, float]]:
        sql = f"SELECT {_recall.DCOLS} FROM deltas d WHERE {where} AND d.id = ?"
        rows = [(lake.conn.execute(sql, [*params, m[name]]).fetchone(), rel) for name, rel in rels.items()]
        return [(row, rel) for row, rel in rows if row is not None]

    monkeypatch.setattr(_recall, "fts_pool", fake)


def keyword_embed(texts: list[str]) -> list[list[float]]:
    """One axis per keyword (unit-normalised); a text with none sits on axis 15, orthogonal to every centroid."""
    out: list[list[float]] = []
    for text in texts:
        vec = [1.0 if k in text.lower() else 0.0 for k in KEYWORDS] + [0.0] * (16 - len(KEYWORDS))
        if not any(vec):
            vec[15] = 1.0
        norm = sum(x * x for x in vec) ** 0.5
        out.append([x / norm for x in vec])
    return out


def test_plan_search_limit(make_lake: MakeLake, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    lake, m = build(make_lake, clock)
    stipulate(monkeypatch, m, EXAMPLE_1)
    step = {"id": "a", "search": "migration backup plan", "limit": 20}
    result = lake.plan([step])
    hits = result.steps["a"].hits or []
    assert names(hits, m) == ["r1", "r3", "r10", "r9", "r7"]  # r2 dropped by N2/N3, r9 takes the soft rule
    for hit, score in zip(hits, (0.999352, 0.799481, 0.700000, 0.499679, 0.499676), strict=True):
        assert hit.score == pytest.approx(score, abs=1e-6) and hit.matched == "fts" and hit.step == "a"
    assert hits[3].relevance == 0.6 and hits[3].recency == pytest.approx(0.999358, abs=1e-6)
    assert hits[0].recency == pytest.approx(0.999352, abs=1e-6) and hits[2].recency == 1.0
    assert result.warnings == [] and lake.last_warnings == [] and result.timing_ms >= 0
    assert list(result.steps) == ["a"] and result.steps["a"].buckets is None and result.steps["a"].timelines is None
    assert names(lake.recall(plan=[{**step, "limit": 2}]), m) == ["r1", "r3"]  # the cut comes after scoring
    assert names(lake.recall(plan=[{**step, "tags_exclude": ["migration"], "limit": 2}]), m) == ["r9", "r7"]


def test_plan_setops_nary(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake, m = build(make_lake, clock)
    result = lake.plan([
        {"id": "a", "filter": {"tags_include": ["migration"]}},
        {"id": "b", "filter": {"tags": ["backup"]}},
        {"id": "c", "filter": {"source": "fathom-chat"}},
        {"id": "u", "union": ["a", "b", "c"]},
        {"id": "d", "diff": ["a", "b", "c"]},
        {"id": "i", "intersect": ["a", "b", "c"]},
        {"id": "i2", "intersect": ["b", "a"]},
        {"id": "u2", "union": ["c", "a"], "limit": 3},
        {"id": "none", "filter": {"tags_include": ["no-such-tag"]}},
        {"id": "e", "union": ["none", "none"]},
    ])
    assert names(result.steps["a"].hits, m) == ["r10", "r3", "r1"] and names(result.steps["c"].hits, m) == ["r9", "r8"]
    assert names(result.steps["u"].hits, m) == ["r10", "r3", "r1", "r7", "r9", "r8"]  # r9, r8 come from the third ref only
    assert names(result.steps["d"].hits, m) == ["r3", "r1"]  # a − (b ∪ c)
    assert result.steps["i"].hits == [] and names(result.steps["i2"].hits, m) == ["r10"]
    assert names(result.steps["u2"].hits, m) == ["r9", "r8", "r10"]
    assert result.steps["e"].hits == [] and result.warnings == []
    with pytest.raises(PlanError, match=re.escape("Step 'one' needs at least two references")):
        lake.plan([{"id": "a", "filter": {}}, {"id": "one", "intersect": ["a"]}])


def test_plan_setops_best_score(make_lake: MakeLake, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    lake, m = build(make_lake, clock)
    stipulate(monkeypatch, m, EXAMPLE_1)
    plan: list[dict[str, Any]] = [
        {"id": "a", "search": "migration backup plan", "limit": 20},
        {"id": "b", "filter": {"tags_include": ["backup"]}},
        {"id": "only_plan", "diff": ["a", "b"]},
        {"id": "top3", "union": ["b", "a"], "limit": 3},
    ]
    result = lake.plan(plan)
    b = result.steps["b"].hits or []
    assert names(b, m) == ["r10", "r7"] and b[0].score == 1.0 and b[1].score == pytest.approx(0.999352, abs=1e-6)
    assert all(h.matched == "filter" and h.relevance == 1.0 and h.step == "b" for h in b)
    assert names(result.steps["only_plan"].hits, m) == ["r1", "r3", "r9"]
    top = result.steps["top3"].hits or []
    assert names(top, m) == ["r10", "r7", "r1"]
    assert top[0].score == 1.0 and top[0].matched == "filter" and top[0].step == "b"  # 1.0 from b beats 0.70 from a
    other = lake.recall(plan=[*plan[:2], {"id": "x", "union": ["a", "b"]}])
    assert names(other, m) == ["r1", "r3", "r10", "r9", "r7"]  # a fixes the positions, b's score still wins
    assert other[2].score == 1.0 and other[2].step == "b"


def test_plan_neighbors(make_lake: MakeLake, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    lake, m = build(make_lake, clock)
    stipulate(monkeypatch, m, {"r7": 1.0})
    a: dict[str, Any] = {"id": "a", "search": "backup margin 09:00", "limit": 1}
    ctx: dict[str, Any] = {"id": "ctx", "neighbors": "a", "radius_minutes": 5, "limit_per_seed": 3}
    result = lake.plan([a, ctx, {"id": "all", "union": ["a", "ctx"]}, {"id": "bytag", "aggregate": "all", "group_by": "tag"}])
    assert names(result.steps["a"].hits, m) == ["r7"]
    hits = result.steps["ctx"].hits or []
    assert names(hits, m) == ["r3", "r2", "r1"]  # r2 is present: neighbors apply no noise rule
    assert [h.score for h in hits] == pytest.approx([0.7333, 0.6667, 0.6000], abs=1e-4)
    assert all(h.matched == "neighbor" and h.step == "ctx" and h.score == h.relevance and h.valence == 1.0 for h in hits)
    assert names(result.steps["all"].hits, m) == ["r7", "r3", "r2", "r1"]
    assert result.steps["bytag"].buckets == [
        Bucket("backup", 1, [m["r7"]]), Bucket("migration", 2, [m["r3"], m["r1"]]), Bucket("team", 1, [m["r1"]]),
    ]
    loose = lake.recall(plan=[a, {**ctx, "source_match": False}])
    assert names(loose, m) == ["r6", "r5", "r4"]  # gaps 50, 55, 60 s beat the conversation rows
    assert loose[0].score == pytest.approx(1 - 50 / 300)
    assert names(lake.recall(plan=[a, {**ctx, "tags_exclude": ["team"]}]), m) == ["r3", "r2"]
    assert names(lake.recall(plan=[a, {**ctx, "source_match": False, "limit": 2}]), m) == ["r6", "r5"]
    wide = lake.recall(plan=[a, {**ctx, "source_match": False, "limit_per_seed": 10, "radius_minutes": 2}])
    assert names(wide, m) == ["r6", "r5", "r4", "r3", "r2", "r1"] and wide[-1].score == pytest.approx(0.0)  # r1 sits on the bound
    assert names(lake.recall(plan=[a, {**ctx, "source_match": False}], filters={"source": ["claude-code"]}), m) == ["r3", "r2", "r1"]


def test_plan_timeline(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake, m = build(make_lake, clock)
    w: dict[str, Any] = {"id": "w", "filter": {"source": "claude-code", "since": "2026-04-29T14:00:30Z", "until": "2026-04-29T14:02:30Z"}}
    result = lake.plan([w, {"id": "tl", "timeline": "w"}])
    assert names(result.steps["w"].hits, m) == ["r7", "r3"]
    step = result.steps["tl"]
    assert step.hits is None and step.buckets is None and step.timelines is not None and len(step.timelines) == 1
    strip = step.timelines[0]
    assert strip.id == "tl_0" and strip.anchor_ids == sorted([m["r3"], m["r7"]])
    assert (strip.t_start, strip.t_end) == ("2026-04-29T14:00:00.000Z", "2026-04-29T14:02:00.000Z")
    rows = strip.rows
    assert [type(r).__name__ for r in rows] == ["TimelineRow", "TimelineRow", "TimelineRow", "CollapsedRun", "TimelineRow"]
    assert [names([Hit(r.delta, 0, 0, 0, 0, "", None)], m)[0] for r in rows if isinstance(r, TimelineRow)] == ["r1", "r2", "r3", "r7"]
    assert [r.is_anchor for r in rows if isinstance(r, TimelineRow)] == [False, False, True, True]
    assert rows[3] == CollapsedRun("agent-heartbeat", 3, "2026-04-29T14:01:00.000Z", "2026-04-29T14:01:10.000Z")
    flat = lake.recall(plan=[w, {"id": "tl", "timeline": "w"}])  # a trailing timeline flattens to its real rows
    assert names(flat, m) == ["r1", "r2", "r3", "r7"]
    assert all(h.matched == "timeline" and h.relevance == 1.0 and h.score == h.recency * h.valence and h.step == "tl" for h in flat)
    raw = lake.plan([w, {"id": "tl", "timeline": "w", "collapse_sources": []}]).steps["tl"].timelines or []
    assert len(raw[0].rows) == 7 and all(isinstance(r, TimelineRow) for r in raw[0].rows)  # the step overrides the Lake's
    seeds: list[dict[str, Any]] = [w, {"id": "late", "filter": {"since": "2026-05-06"}}, {"id": "s", "union": ["w", "late"]}]
    two = lake.plan([*seeds, {"id": "tl", "timeline": "s"}]).steps["tl"].timelines or []
    assert [t.id for t in two] == ["tl_0", "tl_1"] and two[0].rows == rows
    assert two[1].anchor_ids == [m["r10"]] and two[1].t_start == two[1].t_end == FIXTURE_NOW and len(two[1].rows) == 1
    one = lake.plan([*seeds, {"id": "tl", "timeline": "s", "limit": 1}]).steps["tl"].timelines or []
    assert [t.id for t in one] == ["tl_0"]
    assert len(lake.plan([*seeds, {"id": "tl", "timeline": "s", "limit": 0}]).steps["tl"].timelines or []) == 2
    hits = result.steps["w"].hits or []  # the context() call shape: every strip, t_start order, no cut
    direct = _plan.timeline(lake, hits, _plan.TimelineParams(20, 6, 15, 300), _recall.Filters(), now=lake.now())
    assert len(direct) == 1 and direct[0] == strip
    tagged = _plan.timeline(lake, hits, _plan.TimelineParams(), _recall.Filters(exclude_tags=["migration"]), now=lake.now())
    real = [r for r in tagged[0].rows if isinstance(r, TimelineRow)]  # T2: r3 is filtered out, r2 stands in as its anchor
    assert tagged[0].anchor_ids == sorted([m["r3"], m["r7"]]) and [r.is_anchor for r in real] == [True, True]
    assert names([Hit(r.delta, 0, 0, 0, 0, "", None) for r in real], m) == ["r2", "r7"] and len(tagged[0].rows) == 3
    assert _plan.timeline(lake, [], _plan.TimelineParams(), _recall.Filters(), now=lake.now()) == []


def test_plan_refuted_seed(make_lake: MakeLake, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    lake, m = build(make_lake, clock)
    e = lake.engage(m["r3"], "refute", by="robin", note="not thursday")  # r3 is a neighbor and a strip row
    stipulate(monkeypatch, m, {"r7": 1.0})
    a: dict[str, Any] = {"id": "a", "search": "backup margin 09:00", "limit": 1}
    ctx: dict[str, Any] = {"id": "ctx", "neighbors": "a", "radius_minutes": 5, "limit_per_seed": 3}
    hits = lake.plan([a, ctx]).steps["ctx"].hits or []
    r3n = next(h for h in hits if h.delta.id == m["r3"])
    assert r3n.valence == pytest.approx(0.50)  # one refute demotes even a neighbor hit
    assert [x.id for x in r3n.delta.refuted_by] == [e.id] and r3n.delta.refuted_by[0].note == "not thursday"
    others = [h for h in hits if h.delta.id != m["r3"]]
    assert others and all(h.valence == 1.0 and h.delta.refuted_by == () for h in others)  # fixed point at s==0
    w: dict[str, Any] = {"id": "w", "filter": {"source": "claude-code", "since": "2026-04-29T14:00:30Z", "until": "2026-04-29T14:02:30Z"}}
    flat = lake.recall(plan=[w, {"id": "tl", "timeline": "w"}])
    r3f = next(h for h in flat if h.delta.id == m["r3"])
    assert r3f.valence == pytest.approx(0.50) and r3f.delta.refuted_by[0].id == e.id


def test_plan_timeline_collapse_count(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake(automation=["source:daemon"])
    t = "2026-09-01T{}Z"
    lake.write("the anchor of the first strip", "chat", tags=["anchor"], timestamp=t.format("12:00:00.000"))
    for i, ms in enumerate(("10.000", "10.200", "10.400", "11.000", "11.500")):  # bursts of 3 and 2 in one second each
        lake.write(f"daemon tick {i}", "daemon", timestamp=t.format(f"12:00:{ms}"))
    lake.write("a conversation row before the anchor", "chat", timestamp=t.format("14:58:00.000"))
    for i in range(8):
        lake.write(f"daemon before {i}", "daemon", timestamp=t.format(f"14:59:0{i}.000"))
    lake.write("the anchor of the second strip", "chat", tags=["anchor"], timestamp=t.format("15:00:00.000"))
    for i in range(8):
        lake.write(f"daemon after {i}", "daemon", timestamp=t.format(f"15:00:0{i + 1}.000"))
    lake.write("a conversation row after the anchor", "chat", timestamp=t.format("15:02:00.000"))
    plan: list[dict[str, Any]] = [{"id": "s", "filter": {"tags_include": ["anchor"]}}, {"id": "tl", "timeline": "s", "max_per_side": 2}]
    first, second = lake.plan(plan).steps["tl"].timelines or []
    assert [type(r).__name__ for r in first.rows] == ["TimelineRow", "CollapsedRun"]
    assert first.rows[1] == CollapsedRun("daemon", 5, t.format("12:00:10.000"), t.format("12:00:11.500"))
    assert [type(r).__name__ for r in second.rows] == ["TimelineRow", "CollapsedRun", "TimelineRow", "CollapsedRun", "TimelineRow"]
    assert [r.count for r in second.rows if isinstance(r, CollapsedRun)] == [8, 8]
    assert [r.is_anchor for r in second.rows if isinstance(r, TimelineRow)] == [False, True, False]
    assert (second.t_start, second.t_end) == (t.format("14:58:00.000"), t.format("15:02:00.000"))
    plain = lake.plan([plan[0], {"id": "tl", "timeline": "s", "max_per_side": 2, "collapse_sources": []}]).steps["tl"].timelines or []
    assert len(plain[1].rows) == 5 and all(isinstance(r, TimelineRow) for r in plain[1].rows)  # raw rows fill the side cap
    assert [type(r).__name__ for r in plain[0].rows] == ["TimelineRow", "CollapsedRun", "CollapsedRun"]  # T4 alone: 3 and 2


def test_plan_aggregate(make_lake: MakeLake, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    lake, m = build(make_lake, clock)
    stipulate(monkeypatch, m, {"r1": 1.0, "r3": 0.9, "r10": 0.8, "r7": 0.7, "r11": 0.6, "r12": 0.5, "r9": 0.4, "r8": 0.3})
    a: dict[str, Any] = {"id": "a", "search": "migration backup plan", "limit": 20}
    result = lake.plan([
        a, {"id": "byweek", "aggregate": "a", "group_by": "week"},
        {"id": "bytag", "aggregate": "a", "group_by": "tag", "metric": "centroid"},
        {"id": "byhour", "aggregate": "a", "group_by": "hour"}, {"id": "bysrc", "aggregate": "a", "group_by": "source"},
        {"id": "default", "aggregate": "a"}, {"id": "bykind", "aggregate": "a", "group_by": "kind"},
    ])
    assert names(result.steps["a"].hits, m) == ["r1", "r3", "r10", "r7", "r11", "r12", "r9", "r8"]

    def r(*ks: str) -> list[str]:
        return [m[k] for k in ks]

    assert result.steps["byweek"].buckets == [
        Bucket("2026-W17", 2, r("r11", "r12")), Bucket("2026-W18", 5, r("r1", "r3", "r7", "r9", "r8")), Bucket("2026-W19", 1, r("r10")),
    ]
    assert result.steps["bytag"].buckets == [
        Bucket("backup", 2, r("r10", "r7")), Bucket("chat:sunday", 2, r("r9", "r8")), Bucket("fathom-chat", 2, r("r9", "r8")),
        Bucket("kitchen", 1, r("r8")), Bucket("migration", 3, r("r1", "r3", "r10")), Bucket("nova", 1, r("r8")),
        Bucket("team", 1, r("r1")), Bucket("vault", 2, r("r11", "r12")),
    ]
    assert [b.key for b in result.steps["byhour"].buckets or []] == ["2026-04-20 09:00", "2026-04-29 14:00", "2026-04-29 15:00", "2026-05-06 10:00"]
    assert [(b.key, b.count) for b in result.steps["bysrc"].buckets or []] == [("claude-code", 4), ("fathom-chat", 2), ("vault/fathom", 2)]
    assert result.steps["default"].buckets == result.steps["byweek"].buckets
    assert result.steps["bykind"].buckets == [Bucket("plain", 8, r("r1", "r3", "r10", "r7", "r11", "r12", "r9", "r8"))]
    assert result.steps["byweek"].hits is None and result.steps["byweek"].timelines is None
    assert _plan.aggregate([], "day") == []
    with pytest.raises(PlanError, match=re.escape("Step 'x' has unknown group_by 'fortnight'")):
        lake.plan([a, {"id": "x", "aggregate": "a", "group_by": "fortnight"}])
    with pytest.raises(PlanError):
        lake.recall(plan=[a, {"id": "byday", "aggregate": "a", "group_by": "day"}])


def test_plan_bridge_chain_embed(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake, m = build(make_lake, clock, embed=keyword_embed)
    m["r13"] = lake.write("Migration retrospective written afterwards", "notes", timestamp="2026-05-01T10:00:00Z").id
    m["r14"] = lake.write("A row stored without a vector on purpose", "claude-code", tags=["novec"], embed=False).id
    a = {"id": "a", "filter": {"tags_include": ["migration"]}}
    b = {"id": "b", "filter": {"tags_include": ["backup"]}}
    none = {"id": "none", "filter": {"tags_include": ["no-such-tag"]}}
    nv = {"id": "nv", "filter": {"tags_include": ["novec"]}}
    result = lake.plan([
        a, b, none, nv, {"id": "br", "bridge": ["a", "b"]}, {"id": "br_ex", "bridge": ["a", "b"], "exclude_sources": ["vault/fathom"]},
        {"id": "br_none", "bridge": ["a", "none"]}, {"id": "ch", "chain": "a"}, {"id": "ch_none", "chain": "none"},
        {"id": "ch_nv", "chain": "nv"}, {"id": "br_nv", "bridge": ["nv", "a"]}, {"id": "ch_lim", "chain": "a", "limit": 1},
    ])
    inputs = {m[k] for k in ("r1", "r3", "r10", "r7")}
    br = result.steps["br"].hits or []
    assert names(br, m) == ["r13", "r11"] and not inputs & {h.delta.id for h in br}
    assert all(h.matched == "bridge" and h.step == "br" and h.relevance == 1.0 for h in br)
    assert br[1].score == pytest.approx(br[1].recency / 1.2)  # "## Migration runbook" takes the soft rule
    assert names(result.steps["br_ex"].hits, m) == ["r13"]
    ch = result.steps["ch"].hits or []
    assert set(names(ch, m)) == {"r13", "r11", "r7"} and names(ch, m)[:2] == ["r13", "r11"]  # a's own rows are excluded
    assert all(h.matched == "chain" and h.step == "ch" for h in ch) and ch[-1].relevance == 0.0  # min-max: the floor row
    assert names(result.steps["ch_lim"].hits, m) == ["r13"]
    for sid in ("br_none", "ch_none", "ch_nv", "br_nv"):
        assert result.steps[sid].hits == []
    assert result.warnings == [
        "step 'br_none' skipped: input 'none' is empty", "step 'ch_none' skipped: input 'none' is empty",
        "step 'ch_nv' skipped: no embeddings in 'nv'", "step 'br_nv' skipped: no embeddings in 'nv'",
    ] and lake.last_warnings == result.warnings
    scoped = lake.plan([
        {"id": "q", "search": "nova kitchen"}, {"id": "f", "filter": {"any_tags": ["chat:sunday"]}}, a, {"id": "ch", "chain": "a"},
        {"id": "n", "neighbors": "f", "source_match": False}, {"id": "tl", "timeline": "f"},
        {"id": "clash", "filter": {"source": "fathom-chat"}},
    ], filters={"source": ["claude-code"]})
    assert names(lake.plan([{"id": "q", "search": "nova kitchen"}]).steps["q"].hits, m) == ["r8"]
    assert scoped.steps["q"].hits == [] and scoped.steps["f"].hits == [] and scoped.steps["clash"].hits == []
    assert names(scoped.steps["ch"].hits, m) == ["r7"] and scoped.steps["n"].hits == [] and scoped.steps["tl"].timelines == []
    for sid, hits in scoped.steps.items():
        assert all(h.delta.source == "claude-code" for h in hits.hits or []), sid


def test_plan_validation_messages(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake, _ = build(make_lake, clock)
    actions = "search, filter, intersect, union, diff, bridge, aggregate, chain, neighbors, timeline"
    cases: list[tuple[list[dict[str, Any]], str]] = [
        ([{"id": "a", "search": "x"}, {"id": "a", "filter": {}}], "Duplicate step id: 'a'"),
        ([{"id": "1", "search": "x"}, {"search": "y"}], "Duplicate step id: '1'"),  # §12.6: an implied id collides too
        ([{"id": "a"}], f"Step 'a' has no action — must set exactly one of: {actions}"),
        ([{"id": "a", "search": "x", "filter": {}}], "Step 'a' sets more than one action"),
        ([{"id": "a", "chain": "b"}, {"id": "b", "search": "x"}], "Step 'a' references 'b' which is not defined (or comes later in the plan)"),
        ([{"id": "a", "search": "x"}, {"id": "u", "union": ["a", "zz"]}], "Step 'u' references 'zz' which is not defined (or comes later in the plan)"),
        ([{"id": "a", "search": "x"}, {"id": "u", "union": ["a"]}], "Step 'u' needs at least two references"),
        ([{"id": "a", "search": "x"}, {"id": "g", "aggregate": "a"}, {"id": "c", "chain": "g"}],
         "Step 'c' references aggregate step 'g' which produces buckets, not deltas"),
        ([{"id": "a", "search": "x"}, {"id": "g", "aggregate": "a", "group_by": "fortnight"}], "Step 'g' has unknown group_by 'fortnight'"),
    ]
    for steps, message in cases:
        with pytest.raises(PlanError) as err:
            lake.plan(steps)
        assert str(err.value) == message
        with pytest.raises(PlanError):
            lake.recall(plan=steps)
    for bad in ({"id": "a", "search": "x", "since": "last tuesday"}, {"id": "a", "filter": {"until": "yesterday-ish"}}):
        with pytest.raises(PlanError):
            lake.plan([bad])
    with pytest.raises(PlanError):
        lake.plan([{"id": "a", "search": "x"}], filters={"since": "not a time"})
    with pytest.raises(PlanError):
        lake.plan([{"id": "a", "search": "x", "limit": 0}])
    implied = lake.plan([{"search": "migration"}, {"filter": {"tags_include": ["backup"]}}, {"union": ["0", "1"]}])
    assert list(implied.steps) == ["0", "1", "2"] and (implied.steps["2"].hits or [])[0].step in ("0", "1")
    assert lake.plan([]).steps == {} and lake.recall(plan=[]) == []


def test_plan_malformed_param_types_are_plan_errors(make_lake: MakeLake, clock: FrozenClock) -> None:
    """A malformed param type is a PlanError message, never a TypeError traceback (so MCP
    deep_recall can answer `plan error: ...` for every bad plan a model might author)."""
    lake, _ = build(make_lake, clock)
    with pytest.raises(PlanError, match=re.escape("Step 'g' has unknown group_by '['day']'")):
        lake.plan([{"id": "a", "search": "x"}, {"id": "g", "aggregate": "a", "group_by": ["day"]}])
    for steps in (
        [{"id": "a", "filter": {"tags_include": 123}}],
        [{"id": "a", "filter": {"source": {"x": 1}}}],
        [{"id": "a", "search": "x"}, {"id": "t", "timeline": "a", "collapse_sources": 5}],
    ):
        with pytest.raises(PlanError, match="expected a string or a list of strings"):
            lake.plan(steps)
