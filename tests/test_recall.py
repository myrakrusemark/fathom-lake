"""§10 tests owned by _recall and _vectors: valence, FTS, noise, no-query order, no embed, embed
failure, vector relevance, vector blob layout, the recall half of test_ttl_visibility, and the §5.1
filter builder and §5.6.2 oneline the other modules share."""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import statistics
import struct
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import FakeEmbed, FrozenClock, hash_embed

from lake import Delta, EmbedError, Hit, Lake, LakeError, NoiseRules, Refutation, Supersession
from lake import _db, _recall, _time, _vectors
from lake._db import new_id
from lake._store import insert_row

MakeLake = Callable[..., Lake]


def ids(hits: Sequence[Hit]) -> list[str]:
    return [h.delta.id for h in hits]


def ago(clock: FrozenClock, **delta: float) -> datetime:
    return clock() - timedelta(**delta)


def same_embed(texts: list[str]) -> list[list[float]]:
    """Every text on one axis: every row is a vector candidate with relevance 1.0 (§5.2 max == min)."""
    return [[1.0] + [0.0] * 15 for _ in texts]


def angle_embed(texts: list[str]) -> list[list[float]]:
    """`candidate i` sits at angle 0.5 + i/600 from the query axis; `candidate 0` and the probe lie on it."""
    out: list[list[float]] = []
    for text in texts:
        m = re.search(r"\d+", text)
        i = int(m.group()) if m else 0
        theta = 0.0 if i == 0 else 0.5 + i / 600
        out.append([math.cos(theta), math.sin(theta)] + [0.0] * 14)
    return out


# --- valence ------------------------------------------------------------------------------------


def test_valence_numbers(make_lake: MakeLake) -> None:
    lake = make_lake()

    def valence(delta_id: str, query: str | None = None) -> float:
        return next(h.valence for h in lake.recall(query, kind="plain", limit=100) if h.delta.id == delta_id)

    def engaged(kind: str, n: int) -> str:
        d = lake.write(f"a {kind} row engaged {n} times", "host")
        for i in range(n):
            lake.engage(d.id, kind, by=f"reader{i}")
        return d.id

    assert valence(lake.write("no engagements yet", "host").id) == 1.0
    assert valence(engaged("affirm", 1)) == pytest.approx(1.05)
    assert valence(engaged("refute", 1)) == pytest.approx(0.50)
    assert valence(engaged("reply", 1)) == pytest.approx(1.0125)
    six = engaged("affirm", 6)
    assert valence(six) == pytest.approx(1.30)
    assert valence(engaged("affirm", 7)) == pytest.approx(1.30)
    assert valence(six, "engaged times") == pytest.approx(1.30)  # the query path reports the same factor
    hit = next(h for h in lake.recall("engaged times", kind="plain") if h.delta.id == six)
    assert hit.score == pytest.approx(hit.relevance * hit.recency * 1.30)


def test_valence_reorders(make_lake: MakeLake) -> None:
    lake = make_lake()
    lake.write("the migration plan for thursday", "s1")
    lake.write("the migration plan for thursday", "s2")
    first, second = lake.recall("migration plan", kind="plain")
    assert first.score == second.score and first.delta.id < second.delta.id  # tie: timestamp DESC, id ASC
    lake.engage(first.delta.id, "refute", by="robin")
    hits = lake.recall("migration plan", kind="plain")
    assert ids(hits) == [second.delta.id, first.delta.id]
    assert hits[0].valence == 1.0 and hits[1].valence == pytest.approx(0.50)
    assert hits[1].score == pytest.approx(hits[0].score * 0.50)


def test_refute_outweighs_boost(make_lake: MakeLake) -> None:
    lake = make_lake()
    ts = "2026-09-02T12:00:00Z"  # equal recency for both rows
    text = "the quarterly migration retrospective notes"
    raw = lake.write(text, "host", timestamp=ts)
    box = lake.write(text, "host", kind="container", derived_from=[raw.id], timestamp=ts, dedupe=False)
    query = "quarterly migration retrospective"
    before = lake.recall(query, kind=["plain", "container"])
    assert ids(before) == [box.id, raw.id]  # the ×1.176 container boost lifts it above the raw row
    assert before[0].valence == 1.0 and before[1].valence == 1.0
    lake.engage(box.id, "refute", by="robin")
    after = lake.recall(query, kind=["plain", "container"])
    assert ids(after) == [raw.id, box.id]  # one refute (×0.50 × 1.176 = 0.588 < 1.0) sinks the container
    box_hit = next(h for h in after if h.delta.id == box.id)
    raw_hit = next(h for h in after if h.delta.id == raw.id)
    assert box_hit.valence == pytest.approx(0.50) and box_hit.score < raw_hit.score
    assert box_hit.relevance == raw_hit.relevance == 1.0 and box_hit.recency == raw_hit.recency
    assert lake.get(box.id) is not None  # never deleted or excluded (C3)


def test_extractive_container_gets_no_boost(make_lake: MakeLake) -> None:
    """§5.3: a container with meta.fallback (the §6.1 extractive session container) scores like a plain row."""
    lake = make_lake()
    ts = "2026-09-02T12:00:00Z"
    text = "the quarterly migration retrospective notes"
    raw = lake.write(text, "host", timestamp=ts)
    named = lake.write(text, "host", kind="container", derived_from=[raw.id], timestamp=ts, dedupe=False)
    quoted = lake.write(text, "host", kind="container", derived_from=[raw.id], timestamp=ts, dedupe=False,
                        meta={"fallback": "extractive"})
    hits = {h.delta.id: h for h in lake.recall("quarterly migration retrospective", kind=["plain", "container"])}
    assert hits[named.id].score > hits[quoted.id].score == pytest.approx(hits[raw.id].score)


def test_refute_of_premise_suspends_dependent_boost(make_lake: MakeLake) -> None:
    """§5.3 correction propagation: refuting a memory a summary derives from suspends that summary's
    boost, so it no longer outranks the rows beneath it, and the summary carries a rests_on_refuted
    receipt. The summary is never deleted and its own valence is untouched."""
    lake = make_lake()
    ts = "2026-09-02T12:00:00Z"  # equal recency for both payload rows
    premise = lake.write("the harness bench baseline jotting", "host", timestamp=ts)
    text = "the quarterly migration retrospective notes"
    raw = lake.write(text, "host", timestamp=ts)
    box = lake.write(text, "host", kind="container", derived_from=[premise.id], timestamp=ts, dedupe=False)
    query = "quarterly migration retrospective"
    before = lake.recall(query, kind=["plain", "container"])
    assert ids(before) == [box.id, raw.id]                       # the ×1.176 boost lifts the container
    assert all(h.delta.rests_on_refuted == () for h in before)   # nothing refuted yet
    lake.engage(premise.id, "refute", by="robin")
    after = lake.recall(query, kind=["plain", "container"])
    box_hit = next(h for h in after if h.delta.id == box.id)
    raw_hit = next(h for h in after if h.delta.id == raw.id)
    assert box_hit.score == pytest.approx(raw_hit.score)         # boost suspended: no longer above the raw row
    assert box_hit.valence == 1.0                                # the container itself was not refuted
    assert box_hit.delta.rests_on_refuted == (premise.id,)       # the receipt names the corrected premise
    assert raw_hit.delta.rests_on_refuted == ()                  # a row deriving from nothing is unaffected
    assert lake.get(box.id) is not None                          # never deleted


def test_valence_floor(make_lake: MakeLake) -> None:
    lake = make_lake()

    def valence(delta_id: str) -> float:
        return next(h.valence for h in lake.recall(kind="plain", limit=100) if h.delta.id == delta_id)

    two = lake.write("a row refuted twice", "host")
    lake.engage(two.id, "refute", by="a")
    lake.engage(two.id, "refute", by="b")
    assert valence(two.id) == pytest.approx(0.30)  # s = -2 -> 1 - min(0.70, 1.0)
    three = lake.write("a row refuted three times", "host")
    for by in ("a", "b", "c"):
        lake.engage(three.id, "refute", by=by)
    assert valence(three.id) == pytest.approx(0.30)  # s = -3 still floors at 0.30
    mixed = lake.write("a row affirmed once and refuted twice", "host")
    lake.engage(mixed.id, "affirm", by="a")
    lake.engage(mixed.id, "refute", by="b")
    lake.engage(mixed.id, "refute", by="c")
    assert valence(mixed.id) == pytest.approx(0.50)  # s = -1 -> the first-refute drop


def test_refuted_by_receipts(make_lake: MakeLake) -> None:
    lake = make_lake()
    t = lake.write("the deploy window is friday", "host", tags=["plan"])
    e = lake.engage(t.id, "refute", by="robin", note="actually monday")
    hit = next(h for h in lake.recall("deploy window", kind="plain") if h.delta.id == t.id)
    assert hit.delta.refuted_by == (Refutation(e.id, "robin", e.timestamp, "actually monday"),)
    assert hit.delta.content == t.content and hit.delta.tags == t.tags  # the target row is intact
    assert hit.delta.derived_from == t.derived_from and hit.delta.meta == t.meta
    e2 = lake.engage(t.id, "refute", by="ada")  # a second, note-less, still-live refuter shows too
    hit = next(h for h in lake.recall("deploy window", kind="plain") if h.delta.id == t.id)
    assert [r.id for r in hit.delta.refuted_by] == [e2.id, e.id]  # newest first
    assert hit.delta.refuted_by[0] == Refutation(e2.id, "ada", e2.timestamp, None)
    other = lake.write("the backup runs nightly and finishes early", "host")
    o = next(h for h in lake.recall("backup nightly", kind="plain") if h.delta.id == other.id)
    assert o.delta.refuted_by == ()  # an unrefuted row carries no receipts


# --- FTS ----------------------------------------------------------------------------------------


def test_fts_recency_order(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    content = "the postgres migration plan for thursday"
    rows = [lake.write(content, "host", tags=[t], timestamp=ago(clock, days=d)) for t, d in (("a", 0), ("b", 40), ("c", 80))]
    hits = lake.recall("migration plan")
    assert ids(hits) == [r.id for r in rows]
    expected = [1.0, 0.5 + 0.5 * 0.5 ** (40 / 30), 0.5 + 0.5 * 0.5 ** (80 / 30)]
    for hit, rec in zip(hits, expected, strict=True):
        assert hit.recency == pytest.approx(rec, abs=1e-6) and hit.relevance == 1.0 and hit.matched == "fts"
        assert hit.score == pytest.approx(rec, abs=1e-6)
    flat = lake.recall("migration plan", recency=False)
    assert ids(flat) == [r.id for r in rows] and len({h.score for h in flat}) == 1
    assert [h.recency for h in flat] == [h.recency for h in hits]  # still reported with recency=False


def test_consolidated_recency_is_evidence_time(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3: a consolidated row (container, mood, crystal, sediment) is as old as the newest row it rests on,
    through consolidated parents; a row with no resolvable ancestor, and an engagement row, keep their own."""
    lake = make_lake()
    old = lake.write("the harbour survey raw notes", "host", timestamp=ago(clock, days=60))
    mid = lake.write("the harbour survey follow-up", "host", timestamp=ago(clock, days=30))
    box = lake.write("harbour survey session summary", "host", kind="container", derived_from=[old.id, mid.id])
    top = lake.write("harbour survey month summary", "host", kind="container", derived_from=[box.id])
    with lake.tx() as conn:  # a parent swept or never imported: derived_from may dangle (§3.2)
        lone_id = new_id(conn)
        insert_row(conn, delta_id=lone_id, timestamp=_time.dt_to_ts(lake.now()), content="harbour survey orphan summary",
                   source="host", kind="container", level=1, tags=[], derived_from=["0" * 12], expires_at=None,
                   media_hash=None, meta=None)
    note = lake.engage(old.id, "affirm", by="robin", note="harbour survey still right")
    hits = {h.delta.id: h for h in lake.recall("harbour survey", limit=20)}
    month = 0.5 + 0.5 * 0.5 ** (30 / 30)
    assert hits[box.id].recency == pytest.approx(month) and hits[top.id].recency == pytest.approx(month)
    assert hits[lone_id].recency == 1.0 and hits[note.id].recency == 1.0
    assert hits[old.id].recency == pytest.approx(0.5 + 0.5 * 0.5 ** 2)


def test_question_rows_rank_below_statements(make_lake: MakeLake) -> None:
    """§5.3: a plain row that only asks scores 1 / QUESTION_DROP, so the statement that answers ranks first;
    a question is never excluded, a row with a statement before its question is not demoted, and no-query
    listings ignore the rule."""
    lake = make_lake()
    ask = lake.write("what port is the harbour database on?", "host")
    say = lake.write("the harbour database listens on port 6432 behind the pooler", "host", timestamp="1 day ago")
    mixed = lake.write("The harbour database moved. Which port is it on now?", "host", timestamp="2 days ago")
    hits = {h.delta.id: h for h in lake.recall("harbour database port")}
    assert ids(lake.recall("harbour database port"))[0] == say.id and ask.id in hits
    assert hits[ask.id].score == pytest.approx(hits[ask.id].relevance * hits[ask.id].recency / _recall.QUESTION_DROP)
    assert hits[mixed.id].score == pytest.approx(hits[mixed.id].relevance * hits[mixed.id].recency)
    assert _recall.is_question("  ok?  ") and not _recall.is_question("Done. Anything else?")
    assert ids(lake.recall(limit=3)) == [ask.id, say.id, mixed.id]


def test_engagement_row_never_outranks_its_target(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3: a fresh affirm quoting an old target scores at most the target's score and sorts after it; it stays
    in the hits, and without its target among the candidates it keeps its own score. A refute is not capped."""
    lake = make_lake()
    old = lake.write("the harbour database listens on port 6432", "host", timestamp=ago(clock, days=90))
    note = lake.engage(old.id, "affirm", by="robin", note="still true: harbour database port 6432")
    hits = lake.recall("harbour database port")
    assert ids(hits)[:2] == [old.id, note.id] and hits[1].score == pytest.approx(hits[0].score)
    alone = lake.recall("harbour database port", kind="engagement")
    assert ids(alone) == [note.id] and alone[0].score > hits[1].score
    stale = lake.write("the harbour pooler listens on port 5433", "host", timestamp=ago(clock, days=90))
    fix = lake.engage(stale.id, "refute", by="robin", note="moved to 6432")
    ranked = ids(lake.recall("harbour pooler 5433"))
    assert ranked.index(fix.id) < ranked.index(stale.id)  # a refute carries the correction: never capped


def test_engagement_chain_capped_root_first(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3: an affirm of a reply is capped against the reply's capped score, so neither outranks the root row."""
    lake = make_lake()
    old = lake.write("the harbour database listens on port 6432", "host", timestamp=ago(clock, days=90))
    reply = lake.engage(old.id, "reply", by="robin", note="harbour database port 6432 confirmed")
    affirm = lake.engage(reply.id, "affirm", by="robin", note="yes harbour database port 6432")
    hits = lake.recall("harbour database port 6432")
    assert ids(hits)[:3] == [old.id, reply.id, affirm.id]
    assert hits[1].score == pytest.approx(hits[0].score) and hits[2].score == pytest.approx(hits[0].score)


def test_fts_relevance_beats_age(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    old = lake.write("Discussed the postgres migration plan for Thursday", "host", timestamp=ago(clock, days=60))
    fresh = lake.write("The migration is scheduled", "host")
    for i in range(6):  # `migration` is in every row, so only `postgres` and `thursday` carry weight
        lake.write(f"migration note number {i}", "host")
    hits = lake.recall("postgres migration thursday", limit=50)
    assert ids(hits)[0] == old.id and fresh.id in ids(hits)
    assert hits[0].relevance == 1.0 and hits[0].recency == pytest.approx(0.625, abs=1e-6)


def test_fts_tokens(make_lake: MakeLake, monkeypatch: pytest.MonkeyPatch) -> None:
    lake = make_lake()
    row = lake.write("We ran the migration at 09:00 this morning", "host")
    lake.write("Nova stretched mozzarella in the kitchen tonight", "host")
    seen: list[str] = []
    real = _recall.fts_pool

    def spy(lake_: Lake, match: str, where: str, params: Sequence[object]) -> list[tuple[object, float]]:
        seen.append(match)
        return real(lake_, match, where, params)

    monkeypatch.setattr(_recall, "fts_pool", spy)
    assert ids(lake.recall("the migrations to run")) == [row.id]  # porter: migrations ~ migration
    assert seen == ['"migrations" OR "run"']
    assert lake.recall("is it a") == [] and len(seen) == 1  # no token survives: no MATCH is issued
    assert lake.recall("ok db") == [] and seen[-1] == '"ok" OR "db"'  # 2-character tokens are kept
    assert _recall.fts_tokens("Is IT ok? Run, run, RUN the 09:00 migrations") == ["ok", "run", "09", "00", "migrations"]
    assert len(_recall.fts_tokens(" ".join(f"tok{i}" for i in range(100)))) == 64


# --- noise --------------------------------------------------------------------------------------


def test_noise_rules(make_lake: MakeLake) -> None:
    lake = make_lake(embed=same_embed)
    texts = ["ok", "looks good", "toolu_01AbCdEfGhIj toolu_01XyZwVuTsRq", '{"hook_event_name": "Stop", "session_id": "x"}']
    noisy = {lake.write(t, "echo").id for t in texts}
    kept = lake.write("a sentence long enough to be kept", "host")
    query = "anything at all"  # no FTS match; the constant embed makes every row a vector candidate
    assert set(ids(lake.recall(query))) == {kept.id}
    assert noisy <= set(ids(lake.recall()))
    assert noisy <= set(ids(lake.recall(query, noise=False)))
    exempt = make_lake(embed=same_embed, noise=NoiseRules(exempt_sources=["echo"]))  # the same file
    assert noisy <= set(ids(exempt.recall(query)))
    assert set(ids(lake.recall(query, noise=False))) == set(ids(exempt.recall(query)))
    short = lake.write("twelve chars", "host")
    long_ = lake.write("thirty characters of text here", "host")
    scores = {h.delta.id: h.score for h in lake.recall(query)}
    assert scores[short.id] == pytest.approx(scores[long_.id] / 1.2)
    scores = {h.delta.id: h.score for h in lake.recall(query, noise=False)}
    assert scores[short.id] == scores[long_.id]
    rules = NoiseRules()
    assert _recall.is_noise(rules, "  ", "h", None, None) and _recall.is_noise(rules, "Sure, Go", "h", None, None)
    assert not _recall.is_noise(rules, "0123456789ab 0123456789cd x", "h", None, None)  # 2 of 3 pieces: 66 % < 80 %
    assert _recall.is_noise(rules, "0123456789ab, 0123456789cd;0123456789ef 0123456789aa x", "h", None, None)  # 4 of 5
    assert not _recall.is_noise(rules, "ok", "h", "mood", None) and not _recall.is_noise(rules, "ok", "h", None, "a" * 16)
    assert _recall.is_noise(rules, '{"entity": "light.x", "state": "on"}', "h", None, None)
    assert not _recall.is_noise(rules, json.dumps({"note": "a" * 40}), "h", None, None)


# --- ordering, embed absent or failing ----------------------------------------------------------


def test_recall_no_query_order(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    oldest = lake.write("the first thing that happened", "host", timestamp=ago(clock, hours=2))
    box = lake.write("an episode summary", "host", kind="container", derived_from=[oldest.id], timestamp=ago(clock, hours=1))
    newest = lake.write("the newest thing that happened", "host")
    lake.engage(newest.id, "refute", by="robin")
    hits = lake.recall(kind=["plain", "container"])
    assert ids(hits) == [newest.id, box.id, oldest.id]
    assert [h.matched for h in hits] == ["filter"] * 3 and all(h.relevance == 1.0 for h in hits)
    assert hits[0].valence == pytest.approx(0.50) and hits[0].score == pytest.approx(hits[0].recency * 0.50)
    assert hits[1].score == pytest.approx(hits[1].recency)  # no boost without a query (§12.5)
    assert ids(lake.recall(kind="container", limit=1)) == [box.id]
    with pytest.raises(ValueError):
        lake.recall(limit=0)


def test_recall_no_embed(make_lake: MakeLake) -> None:
    lake = make_lake()
    a = lake.write("Discussed the postgres migration plan for Thursday", "host")
    b = lake.write("Backup finishes around 08:30 so 09:00 gives us margin", "host")
    c = lake.write("We agreed to run the migration after the backup finishes", "host")
    hits = lake.recall("migration")
    assert set(ids(hits)) == {a.id, c.id} and all(h.matched == "fts" for h in hits)
    assert lake.vectors is None and lake.last_warnings == []
    result = lake.plan([
        {"id": "a", "search": "postgres", "limit": 1},
        {"id": "b", "search": "margin", "limit": 1},
        {"id": "bridge", "bridge": ["a", "b"]},
        {"id": "chain", "chain": "a"},
    ])
    assert ids(result.steps["a"].hits or []) == [a.id] and ids(result.steps["b"].hits or []) == [b.id]
    assert ids(result.steps["bridge"].hits or []) == [c.id] and (result.steps["bridge"].hits or [])[0].matched == "bridge"
    assert ids(result.steps["chain"].hits or []) == [c.id] and (result.steps["chain"].hits or [])[0].matched == "chain"


def test_recall_slow_query_embed_falls_back_to_fts(make_lake: MakeLake, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    release = threading.Event()
    slow = threading.Event()

    def embed(texts: list[str]) -> list[list[float]]:
        if slow.is_set():
            release.wait(5)
        return hash_embed(texts)
    lake = make_lake(embed=embed)
    row = lake.write("the migration ran overnight", "host")
    monkeypatch.setattr(_recall, "QUERY_EMBED_WAIT", 0.2)
    slow.set()
    hits = lake.recall("migration")
    text = lake.context("migration", crystal=False)
    release.set()
    assert ids(hits) == [row.id] and hits[0].matched == "fts"
    assert lake.last_warnings == ["embed failed: query embed took over 0.2s; FTS only"]
    assert "keyword-only recall" in text and "a bit less accurate" in text  # the reader is told
    slow.clear()
    assert lake.recall("migration") and lake.last_warnings == []  # a prompt embedder is used again
    assert "keyword-only recall" not in lake.context("migration", crystal=False)


def test_recall_embed_failure(make_lake: MakeLake, raising_embed: FakeEmbed) -> None:
    lake = make_lake(embed=raising_embed)
    with pytest.raises(EmbedError) as err:
        lake.write("the migration ran overnight", "host")
    row = err.value.delta
    assert row is not None
    hits = lake.recall("migration")
    assert ids(hits) == [row.id] and hits[0].matched == "fts"
    assert lake.last_warnings == ["embed failed: embed down; FTS only"]
    assert lake.recall("migration") and len(lake.last_warnings) == 1  # a fresh list per call
    assert lake.recall() and lake.last_warnings == []  # no query: embed is never called
    assert len(raising_embed.calls) == 3
    with pytest.raises(LakeError), lake.tx():  # the transaction guard is re-raised, not swallowed
        lake.recall("migration")


# --- vectors ------------------------------------------------------------------------------------


def test_vector_relevance(make_lake: MakeLake, monkeypatch: pytest.MonkeyPatch) -> None:
    lake = make_lake(embed=angle_embed)
    rows = [lake.write(f"candidate {i}", "host") for i in range(600)]
    where, params = _recall.filter_sql(lake, _recall.Filters(), lake.now())
    pool, rel_all = _recall.vector_pool(lake, "vector probe", where, params)
    assert len(pool) == 500 and pool[0] == (rows[0].id, 1.0) and pool[-1] == (rows[499].id, 0.0) and len(rel_all) == 600
    base = statistics.median([1.0] + [math.cos(0.5 + i / 600) for i in range(1, 600)])  # the median cosine: rel_vec 0
    assert dict(pool)[rows[100].id] == pytest.approx((math.cos(0.5 + 100 / 600) - base) / (1.0 - base), abs=1e-5)
    hits = lake.recall("vector probe", limit=1000)
    assert hits[0].delta.id == rows[0].id and hits[0].relevance == 1.0 and hits[0].matched == "vector"
    expected = sum(1 for i in range(600) if (1.0 if i == 0 else (math.cos(0.5 + i / 600) - base) / (1.0 - base)) >= 0.3)
    assert rows[499].id not in ids(hits) and all(h.relevance >= 0.3 for h in hits) and len(hits) == expected == 195
    assert ids(lake.recall("vector probe", min_relevance=0.9)) == [rows[0].id]
    assert ids(lake.recall("vector probe", tags=["none"])) == []  # filters bound the vector pass too
    monkeypatch.setattr(_vectors, "_np", lambda: None)  # the pure-Python path agrees
    cache = lake.vectors
    assert cache is not None
    cache.gen = -1
    assert ids(lake.recall("vector probe", min_relevance=0.9)) == [rows[0].id]
    assert cache.cosine_top(lake, [1.0] + [0.0] * 15, [rows[1].id, "nope"], 5) == [(rows[1].id, pytest.approx(math.cos(0.5 + 1 / 600)))]
    assert cache.centroid(lake, ["nope"]) is None and cache.centroid(lake, [rows[0].id]) == pytest.approx([1.0] + [0.0] * 15)


def test_hybrid_fusion(make_lake: MakeLake) -> None:
    lake = make_lake(embed=angle_embed)
    filler = [lake.write(f"candidate {i} filler", "host") for i in range(1, 400, 40)]
    a = lake.write("candidate 0 migration", "host")
    b = lake.write("candidate 200 migration migration", "host")
    lake.embed = None
    c = lake.write("candidate 100 migration", "host")  # no vector: keeps rel_fts alone
    lake.embed = angle_embed
    where, params = _recall.filter_sql(lake, _recall.Filters(), lake.now())
    found = _recall.fts_pool(lake, _recall.fts_match(["migration"]), where, params)
    top = max(bm for _, bm in found)
    rel_fts = {row["id"]: bm / top for row, bm in found}
    rel_vec = dict(_recall.vector_pool(lake, "migration probe", where, params)[0])
    hits = {h.delta.id: h for h in lake.recall("migration probe", limit=50)}
    for d in (a, b):
        want = _recall.FUSE_FTS * rel_fts[d.id] + (1 - _recall.FUSE_FTS) * rel_vec.get(d.id, 0.0)
        assert hits[d.id].relevance == pytest.approx(want)
    assert hits[a.id].matched == "both" and hits[c.id].relevance == pytest.approx(rel_fts[c.id]) and hits[c.id].matched == "fts"
    vec_only = [h for h in hits.values() if h.matched == "vector"]
    assert vec_only and all(h.delta.id in {f.id for f in filler} for h in vec_only)
    assert all(h.relevance == pytest.approx((1 - _recall.FUSE_FTS) * rel_vec[h.delta.id]) for h in vec_only)
    assert ids(lake.recall("the of and", limit=50))[0] == a.id  # no FTS token: rel_vec alone
    assert lake.recall("the of and", limit=1)[0].relevance == 1.0


def test_hybrid_fusion_outside_vector_pool(make_lake: MakeLake, monkeypatch: pytest.MonkeyPatch) -> None:
    """§5.2 step 3: an FTS hit whose cosine ranks outside the vector pool still fuses with its real rel_vec."""
    monkeypatch.setattr(_recall, "FTS_POOL", 5)
    lake = make_lake(embed=angle_embed)
    for i in range(1, 30):
        lake.write(f"candidate {i} filler", "host")
    far = lake.write("candidate 12 zebra", "host")  # 11 filler rows lie closer to the query axis: outside the pool
    where, params = _recall.filter_sql(lake, _recall.Filters(), lake.now())
    ranked, rel_vec = _recall.vector_pool(lake, "zebra", where, params)
    assert len(ranked) == 5 and far.id not in dict(ranked) and len(rel_vec) == 30 and rel_vec[far.id] > 0.1
    hit = next(h for h in lake.recall("zebra", limit=50) if h.delta.id == far.id)
    assert hit.relevance == pytest.approx(_recall.FUSE_FTS + (1 - _recall.FUSE_FTS) * rel_vec[far.id]) and hit.matched == "fts"


def test_vector_blob_layout(make_lake: MakeLake, fake_embed: FakeEmbed) -> None:
    lake = make_lake(embed=fake_embed)
    d = lake.write("a row with a vector", "host")
    blob, dim = lake.conn.execute("SELECT vec, dim FROM vectors WHERE delta_id = ?", (d.id,)).fetchone()
    n = len(blob) // 4
    vec = struct.unpack(f"<{n}f", blob)
    assert n == dim == 16 and math.sqrt(sum(x * x for x in vec)) == pytest.approx(1.0, abs=1e-6)
    assert lake.conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone()[0] == "16"
    assert list(vec) == pytest.approx(hash_embed([d.content])[0], abs=1e-6)
    assert blob == _vectors.pack(_vectors.unpack(blob)) and _vectors.normalise([0.0, 0.0]) is None
    lake.embed = FakeEmbed(dim=8)
    with pytest.raises(EmbedError) as err:
        lake.write("a row whose vector has the wrong length", "host")
    bad = err.value.delta
    assert bad is not None and lake.conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
    assert lake.embed_missing() == 0
    failed = lake.conn.execute("SELECT value FROM meta WHERE key = 'embed_failed'").fetchone()[0]
    assert json.loads(failed) == [bad.id]
    calls = len(lake.embed.calls)
    assert lake.embed_missing() == 0 and len(lake.embed.calls) == calls  # listed: skipped next call


# --- TTL, filters, oneline ----------------------------------------------------------------------


def test_ttl_visibility(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    d = lake.write("ephemeral daemon state", "daemon", expires="1s")
    assert ids(lake.recall(source="daemon")) == [d.id] and ids(lake.recall("daemon state")) == [d.id]
    clock.advance(seconds=1)  # expires_at <= now: invisible from that instant
    assert lake.recall(source="daemon") == [] and lake.recall("daemon state") == []
    assert ids(lake.recall(source="daemon", include_expired=True)) == [d.id]


def test_filter_sql(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    a = lake.write("reader row one", "reader", tags=["x", "y"], timestamp=ago(clock, hours=3))
    b = lake.write("reader row two", "reader", tags=["x"], timestamp=ago(clock, hours=2))
    c = lake.write("ada row", "ada", tags=["z"], media="ab" * 8, timestamp=ago(clock, hours=1))
    m = lake.write("mood row", "host", kind="mood", derived_from=[a.id], expires="1h")

    def only(**kw: object) -> list[str]:
        where, params = _recall.filter_sql(lake, _recall.Filters(**kw), lake.now())  # type: ignore[arg-type]
        sql = f"SELECT d.id FROM deltas d WHERE {where} ORDER BY d.seq"
        return [r[0] for r in lake.conn.execute(sql, params)]

    assert only() == [a.id, b.id, c.id, m.id] and _recall.filter_sql(lake, _recall.Filters(include_expired=True), lake.now())[0] == "1"
    assert only(source="reader") == [a.id, b.id] and only(source=["ada", "host"]) == [c.id, m.id]
    assert only(exclude_sources=["reader"]) == [c.id, m.id]
    assert only(tags=["x", "y", "y"]) == [a.id] and only(any_tags=["y", "z"]) == [a.id, c.id]
    assert only(exclude_tags=["x"]) == [c.id, m.id] and only(tags=[], any_tags=(), exclude_tags=None) == only()
    assert only(kind="plain") == [a.id, b.id, c.id] and only(kind=["mood", "plain"]) == only()
    assert only(kind="mood") == [m.id] and only(exclude_kinds=["mood"]) == [a.id, b.id, c.id]
    assert only(since="2 hours ago") == [b.id, c.id, m.id] and only(until=ago(clock, hours=2)) == [a.id, b.id]
    assert only(has_media=True) == [c.id] and only(has_media=False) == [a.id, b.id, m.id]
    assert only(no_ttl=True) == [a.id, b.id, c.id]
    clock.advance(hours=1)
    assert only() == [a.id, b.id, c.id] and only(include_expired=True) == [a.id, b.id, c.id, m.id]
    with pytest.raises(ValueError):
        lake.recall(kind="episode")
    with pytest.raises(ValueError):
        lake.recall(since="last tuesday")


def test_oneline() -> None:
    assert _recall.oneline("  a  b\n\tc  ") == "a b c"
    assert _recall.oneline("<p>a &amp; b</p><br/>c &#39;d") == "a bc d"  # tags removed, entities become spaces
    assert _recall.oneline("x < y & z") == "x < y & z"  # no tag: entities untouched
    assert _recall.oneline("abcdef", 4) == "abc…" and _recall.oneline("abcd", 4) == "abcd"
    assert _recall.oneline("𝄞" * 10, 5) == "𝄞" * 4 + "…"  # code points, not UTF-16 units


# --- AAA phase 3: supersession links (§3.2 meta.supersedes, §5.3) ----------------------------------------------------


def link(
    lake: Lake, pairs: Sequence[tuple[Delta, Delta, str, str]], *, source: str = "lake:container",
    derived: Sequence[Delta] | None = None, expires_at: str | None = None,
) -> str:
    """A container row carrying meta.supersedes for (old, new, old_value, new_value) pairs, as consolidate writes it
    (source lake:container bypasses the host closed loop, so it is inserted directly)."""
    with lake.tx() as conn:
        did = new_id(conn)
        insert_row(conn, delta_id=did, timestamp=_time.dt_to_ts(lake.now()), content="Stack change\n\nMoved hosting.",
                   source=source, kind="container", level=1, tags=[], expires_at=expires_at, media_hash=None,
                   derived_from=[d.id for d in (derived if derived is not None else [p[1] for p in pairs])],
                   meta={"title": "Stack change", "supersedes": [
                       {"new": n.id, "old": o.id, "old_value": ov, "new_value": nv} for o, n, ov, nv in pairs]})
    return did


def stale_and_fix(lake: Lake, clock: FrozenClock) -> tuple[Delta, Delta]:
    """An older row that matches the query better than the newer row correcting it."""
    old = lake.write("tidewater deploys to Fly.io region ord; tidewater Fly.io deploy works", "host",
                     timestamp=ago(clock, days=40))
    new = lake.write("change of plan: tidewater moves off Fly.io to Hetzner", "host", timestamp=ago(clock, days=10))
    return old, new


def test_supersession_demotes_and_caps(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3: the superseded row scores ×0.50 and sorts right after its superseder; it is still returned and carries
    the receipt; without the superseder among the candidates it keeps the ×0.50 alone."""
    lake = make_lake()
    old, new = stale_and_fix(lake, clock)
    before = lake.recall("tidewater Fly.io deploy")
    assert ids(before)[:2] == [old.id, new.id]
    alone_before = lake.recall("region ord")
    by = link(lake, [(old, new, "Fly.io region ord", "Hetzner")])
    hits = lake.recall("tidewater Fly.io deploy")
    assert ids(hits)[:2] == [new.id, old.id] and hits[1].score <= hits[0].score
    assert hits[1].delta.superseded_by == (Supersession(new.id, by, "Fly.io region ord", "Hetzner"),)
    assert hits[0].delta.superseded_by == () and hits[1].valence == 1.0  # valence stays engagement-only
    assert hits[1].delta.content == old.content  # never hidden, never rewritten
    ord_hits = lake.recall("region ord")  # the superseder does not match: no cap, the ×0.50 alone
    assert ids(ord_hits) == [old.id] and ids(alone_before) == [old.id]
    assert ord_hits[0].score == pytest.approx(alone_before[0].score * _recall.SUPERSEDE_DROP)
    assert lake.get(old.id).superseded_by[0].id == new.id  # type: ignore[union-attr]
    assert ids(lake.recall(None, limit=5))[:3] == [by, new.id, old.id]  # the no-query order ignores links


def test_supersession_chain_newest_first(make_lake: MakeLake, clock: FrozenClock) -> None:
    """Two links A -> B -> C: C, B, A in that order, each capped at the one above it."""
    lake = make_lake()
    a = lake.write("harbour pooler port 5433 harbour pooler port", "host", timestamp=ago(clock, days=60))
    b = lake.write("harbour pooler port is now 6432", "host", timestamp=ago(clock, days=30))
    c = lake.write("harbour pooler port 7000 from today", "host", timestamp=ago(clock, days=5))
    link(lake, [(a, b, "5433", "6432"), (b, c, "6432", "7000")])
    assert ids(lake.recall("harbour pooler port"))[:3] == [c.id, b.id, a.id]


@pytest.mark.parametrize("case", ["refuted_container", "expired_new", "not_older", "host_container", "refuted_new",
                                  "expired_container", "malformed", "new_not_cited"])
def test_supersession_link_disabled(make_lake: MakeLake, clock: FrozenClock, case: str) -> None:
    """A link is live only while its library container is live and not refuted, both rows are live host rows,
    `new` is not refuted, and old.timestamp < new.timestamp; otherwise nothing moves and no receipt appears."""
    lake = make_lake()
    old, new = stale_and_fix(lake, clock)
    if case == "expired_new":
        new = lake.write("change of plan: tidewater moves off Fly.io to Hetzner", "host2", expires="1h",
                         timestamp=ago(clock, days=10))
    if case == "not_older":
        old, new = new, old
    by = link(lake, [(old, new, "Fly.io", "Fly.io")], source="host" if case == "host_container" else "lake:container",
              expires_at=_time.dt_to_ts(ago(clock, hours=1)) if case == "expired_container" else None,
              derived=[old] if case == "new_not_cited" else None)  # `new` must be in the container's derived_from
    if case == "refuted_container":
        lake.engage(by, "refute", by="robin", note="not a change")
    if case == "refuted_new":
        lake.engage(new.id, "refute", by="robin")
    if case == "expired_new":
        clock.advance(hours=2)
    if case == "malformed":
        with lake.tx() as conn:
            conn.execute("UPDATE deltas SET meta = ? WHERE id = ?",
                         (json.dumps({"supersedes": [{"new": new.id, "old": old.id}, "x", 7]}), by))
    assert _db.supersessions(lake.conn, lake.now_str()) == {}
    got = lake.get(old.id)
    assert got is not None and got.superseded_by == ()


def test_supersession_propagates_to_summaries(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3 propagation: a container built on the superseded row loses its summary boost; the container that asserts
    the link does not, although it may derive from the superseded row too."""
    lake = make_lake()
    old, new = stale_and_fix(lake, clock)
    summary = lake.write("tidewater deploy summary on Fly.io", "host", kind="container", derived_from=[old.id],
                         timestamp=ago(clock, days=40))
    before = {h.delta.id: h.score for h in lake.recall("tidewater deploy summary")}
    by = link(lake, [(old, new, "Fly.io region ord", "Hetzner")], derived=[old, new])
    after = {h.delta.id: h.score for h in lake.recall("tidewater deploy summary")}
    assert after[summary.id] == pytest.approx(before[summary.id] * 0.85)  # the 1/0.85 boost is suspended
    assert _db.dep_corrected_set(lake.conn, lake.now_str(), _db.supersessions(lake.conn, lake.now_str())) == {summary.id}
    assert by not in _db.dep_corrected_set(lake.conn, lake.now_str(), _db.supersessions(lake.conn, lake.now_str()))


def test_supersession_query_cost_without_links(make_lake: MakeLake, clock: FrozenClock) -> None:
    """A lake that never held a link reads the has_supersedes meta key once per scoring call and runs no supersession
    query over deltas, so the cost does not grow with the container count; the first link row sets the key."""
    lake = make_lake()
    old, new = stale_and_fix(lake, clock)
    lake.write("a host container citing the row", "host", kind="container", derived_from=[old.id],
               meta={"supersedes": [{"new": new.id, "old": old.id, "old_value": "x", "new_value": "y"}]})
    seen: list[str] = []
    lake.conn.set_trace_callback(seen.append)
    try:
        lake.recall("tidewater Fly.io deploy")
    finally:
        lake.conn.set_trace_callback(None)
    assert not [q for q in seen if "supersedes" in q and "FROM deltas" in q]
    assert len([q for q in seen if "FROM meta" in q]) == 1  # one gate lookup (has_supersedes, has_stances)
    assert _db.meta_get(lake.conn, _db.HAS_LINKS) is None  # a host-written container never sets the gate
    link(lake, [(old, new, "Fly.io region ord", "Hetzner")])
    assert _db.meta_get(lake.conn, _db.HAS_LINKS) == "1"


def test_supersession_old_lakes_open_unchanged(tmp_path: Path, clock: FrozenClock) -> None:
    """No DDL: a v1 file written by an older library (raw SQL, no meta.supersedes anywhere) and a copy of the eval's
    base.lake open, recall, and gain a link without any schema change."""
    old_file = tmp_path / "old.lake"
    conn = sqlite3.connect(old_file)
    conn.executescript(";\n".join(_db.DDL) + ";")
    conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)",
                     [("schema_version", "1"), ("created_at", "2026-01-01T00:00:00.000Z"), ("vectors_gen", "0")])
    for i, (text, ts) in enumerate([("the harbour pooler port is 5433", "2026-08-01T10:00:00.000Z"),
                                    ("the harbour pooler port is 6432 now", "2026-08-20T10:00:00.000Z")]):
        conn.execute("INSERT INTO deltas(id, timestamp, content, source, tag_key) VALUES (?, ?, ?, 'host', '')",
                     (f"{i:012x}", ts, text))
    conn.commit()
    schema = conn.execute("SELECT group_concat(sql, ';') FROM sqlite_master").fetchone()[0]
    conn.close()
    base = Path(__file__).resolve().parent.parent / "evals" / "base.lake"
    targets = [old_file]
    if base.exists():
        shutil.copyfile(base, tmp_path / "base.lake")
        targets.append(tmp_path / "base.lake")
    for path in targets:
        with Lake(path, clock=clock) as lake:
            assert lake.recall("harbour pooler port") or path != old_file
            assert _db.supersessions(lake.conn, lake.now_str()) == {}
            rows = [r[0] for r in lake.conn.execute("SELECT id FROM deltas WHERE kind IS NULL ORDER BY timestamp LIMIT 2")]
            o, n = (lake.get(i) for i in rows)
            assert o is not None and n is not None
            link(lake, [(o, n, o.content[:5], n.content[:5] + "x" if o.content[:5] == n.content[:5] else n.content[:5])])
            assert lake.get(o.id).superseded_by  # type: ignore[union-attr]
    conn = sqlite3.connect(old_file)
    assert conn.execute("SELECT group_concat(sql, ';') FROM sqlite_master").fetchone()[0] == schema
    conn.close()


def test_supersession_export_import(make_lake: MakeLake, clock: FrozenClock, tmp_path: Path) -> None:
    """meta.supersedes crosses export/import verbatim, so the link is live in the new file; the derived receipt is
    never exported."""
    lake = make_lake()
    old, new = stale_and_fix(lake, clock)
    link(lake, [(old, new, "Fly.io region ord", "Hetzner")])
    out = tmp_path / "x.jsonl"
    lake.export(out)
    assert "superseded_by" not in out.read_text(encoding="utf-8")
    fresh = make_lake("fresh")
    fresh.import_(out)
    got = fresh.get(old.id)
    assert got is not None and [x.id for x in got.superseded_by] == [new.id]
    assert ids(fresh.recall("tidewater Fly.io deploy"))[:2] == [new.id, old.id]
