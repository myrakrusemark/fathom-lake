"""§10 tests owned by _store: closed loop, derived_from, dedupe, TTL, engage, lineage, host meta, get,
schema/open/WAL, sqlite CLI, and the write() NaN half of test_export_canonical.

Where a test's assertion runs through another module that is still a stub (recall, stats, embed_missing,
store_vectors), `maybe()` returns None and the test asserts the same fact through get() and direct SQL."""

from __future__ import annotations

import multiprocessing
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import DEFAULT_NOW, FakeEmbed, FrozenClock

from lake import ClosedLoopError, Delta, EmbedError, Engagement, Lake, LakeError, Lineage, NotFoundError, Refutation, SchemaError
from lake import _store
from lake._time import dt_to_ts

MakeLake = Callable[..., Lake]


def rows(lake: Lake, table: str = "deltas") -> int:
    return int(lake.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def col(lake: Lake, sql: str, *params: object) -> list[Any]:
    return [r[0] for r in lake.conn.execute(sql, params)]


def maybe(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
    """Call through a sibling module; None when that module is still a stub."""
    try:
        return fn(*args, **kw)
    except NotImplementedError:
        return None


def at(clock: FrozenClock, **delta: float) -> str:
    return dt_to_ts(clock() + timedelta(**delta))


# --- closed loop and provenance -----------------------------------------------------------------


def test_closed_loop_source(make_lake: MakeLake) -> None:
    lake = make_lake()
    base = lake.write("a host row", "host")
    with pytest.raises(ClosedLoopError):
        lake.write("x", "lake:x")
    with pytest.raises(ClosedLoopError):
        lake.write("x", "lake:x", derived_from=[base.id])
    with pytest.raises(ClosedLoopError):
        lake.engage(base.id, "affirm", by="lake:crystal")
    assert rows(lake) == 1  # nothing slipped through; the consolidate half lives in test_consolidate


def test_closed_loop_kind(make_lake: MakeLake) -> None:
    lake = make_lake()
    with pytest.raises(ClosedLoopError):
        lake.write("a take", "host", kind="sediment")
    with pytest.raises(ValueError) as err:  # "engagement" is engage()'s alone, a plain ValueError
        lake.write("x", "host", kind="engagement")
    assert not isinstance(err.value, ClosedLoopError)
    with pytest.raises(ValueError):
        lake.write("x", "host", kind="episode")
    assert rows(lake) == 0


def test_derived_from_must_exist(make_lake: MakeLake) -> None:
    lake = make_lake()
    with pytest.raises(NotFoundError):
        lake.write("x", "host", derived_from=["000000000000"])
    base = lake.write("base", "host")
    with pytest.raises(NotFoundError):  # one missing parent fails the whole write
        lake.write("x", "host", derived_from=[base.id, "000000000000"])
    l1 = lake.write("episode", "host", kind="container", derived_from=[base.id, " ", base.id])
    l2 = lake.write("topic", "host", kind="container", derived_from=[l1.id, base.id])
    l3 = lake.write("era", "host", kind="container", derived_from=[l2.id])
    l4 = lake.write("beyond", "host", kind="container", derived_from=[l3.id])
    assert (l1.level, l2.level, l3.level, l4.level) == (1, 2, 3, 3)
    assert l1.derived_from == [base.id]  # normalised: stripped, empties dropped, deduped
    assert lake.write("take", "host", kind="sediment", derived_from=[l2.id]).level == 0
    assert col(lake, "SELECT parent_id FROM derived_from WHERE delta_id = ? ORDER BY pos", l2.id) == [l1.id, base.id]


# --- dedupe -------------------------------------------------------------------------------------


def test_dedupe_same_tagset(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    first = lake.write("A", "host", tags=["x", "y"])
    clock.advance(minutes=5)
    again = lake.write("A", "host", tags=["y", "x"])
    assert again == first and again.timestamp == first.timestamp == DEFAULT_NOW
    assert rows(lake) == 1


def test_dedupe_tagset_equality(make_lake: MakeLake) -> None:
    lake = make_lake()
    a = lake.write("A", "host", tags=["x"])
    b = lake.write("A", "host", tags=["x", "y"])
    assert a.id != b.id and rows(lake) == 2
    assert col(lake, "SELECT tag_key FROM deltas WHERE id = ?", b.id) == ["x\ny"]
    c = lake.write("A", "host", tags=[" y ", "x", "", "x"])  # normalises to the same set as b
    assert c == b and rows(lake) == 2
    assert lake.write("A", "host").id not in (a.id, b.id)  # empty set matches only an empty set
    assert lake.write("A", "host").id == lake.write("A", "host").id


def test_dedupe_aba(make_lake: MakeLake) -> None:
    lake = make_lake()
    a1 = lake.write("A", "host")
    b = lake.write("B", "host")
    a2 = lake.write("A", "host")
    assert len({a1.id, b.id, a2.id}) == 3 and rows(lake) == 3


def test_dedupe_expired_prior(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    first = lake.write("A", "host", expires="1s")
    clock.advance(seconds=2)
    second = lake.write("A", "host")
    assert second.id != first.id and second.expires_at is None and rows(lake) == 2


def test_dedupe_opt_out_and_window(make_lake: MakeLake) -> None:
    lake = make_lake()
    a = lake.write("A", "host")
    b = lake.write("A", "host", dedupe=False)
    assert a.id != b.id and rows(lake) == 2
    m1 = lake.write("A", "host", media="ab" * 8)  # a media write is always an explicit observation
    m2 = lake.write("A", "host", media="ab" * 8)
    assert m1.id != m2.id and rows(lake) == 4
    windowed = make_lake("w", dedupe_window="1h")
    old = windowed.write("A", "host", timestamp="2 hours ago")
    fresh = windowed.write("A", "host")
    assert fresh.id != old.id and rows(windowed) == 2
    assert windowed.write("A", "host") == fresh  # inside the window it still collapses


def test_dedupe_ttl_refresh(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    a = lake.write("A", "host", expires="1h")
    assert a.expires_at == at(clock, hours=1)
    b = lake.write("A", "host", expires="3h")
    assert b.id == a.id and b.expires_at == at(clock, hours=3) and rows(lake) == 1
    assert lake.get(a.id).expires_at == at(clock, hours=3)  # type: ignore[union-attr]
    c = lake.write("A", "host", expires="30m")
    assert c.expires_at == at(clock, hours=3)
    assert lake.write("A", "host").expires_at == at(clock, hours=3)  # no expires: unchanged too
    p = lake.write("P", "host")  # §12.1: a permanent row never gains a TTL
    q = lake.write("P", "host", expires="1h")
    assert q == p and q.expires_at is None
    assert col(lake, "SELECT expires_at FROM deltas WHERE id = ?", p.id) == [None]


# --- TTL and get --------------------------------------------------------------------------------


def test_ttl_visibility(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    d = lake.write("ephemeral state", "daemon", expires="1s")
    assert lake.get(d.id) == d and d.expires_at == at(clock, seconds=1)
    hits = maybe(lake.recall, source="daemon")
    if hits is not None:
        assert [h.delta.id for h in hits] == [d.id]
    clock.advance(seconds=1)  # expires_at <= now: invisible from that very instant
    assert lake.get(d.id) is None
    assert lake.get(d.id, include_expired=True) == d
    if hits is not None:
        assert maybe(lake.recall, source="daemon") == []
    expired_sql = "SELECT count(*) FROM deltas WHERE expires_at IS NOT NULL AND expires_at <= ?"
    assert col(lake, expired_sql, dt_to_ts(clock())) == [1]  # stats()["expired_unswept"] is _io's half
    stats = maybe(lake.stats)
    if stats is not None:
        assert stats["expired_unswept"] == 1
    with pytest.raises(ValueError):
        lake.write("x", "daemon", expires="0s")
    with pytest.raises(ValueError):
        lake.write("x", "daemon", expires="1 hour ago")


def test_get_prefix(make_lake: MakeLake) -> None:
    lake = make_lake()
    d = lake.write("row", "host")
    assert lake.get(d.id) == d and lake.get(d.id[:8]) == d and lake.get(d.id[:11]) == d
    assert lake.get(d.id[:7]) is None
    assert lake.get(d.id.upper()) is None and lake.get("") is None and lake.get(d.id + "0") is None
    assert lake.get("a%%%%%%%") is None and lake.get("a_______") is None and lake.get(d.id[:8] + "_") is None
    twin = d.id[:8] + ("0000" if d.id[8:] != "0000" else "1111")
    with lake.tx() as conn:
        _store.insert_row(
            conn, delta_id=twin, timestamp=d.timestamp, content="twin", source="host", kind=None, level=0,
            tags=(), derived_from=(), expires_at=None, media_hash=None, meta=None,
        )
    assert lake.get(d.id[:8]) is None  # ambiguous
    assert lake.get(d.id) == d and lake.get(twin).content == "twin"  # type: ignore[union-attr]


def test_write_fields(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    d = lake.write("  kept verbatim \n", "host", tags=[" a ", "b", "a"], timestamp="2026-08-30T14:05:12.345678Z",
                   expires="2026-09-03T00:00:00Z", media="ab" * 8, meta={"z": 1, "a": [1, 2.5, "é"]})
    assert d.content == "  kept verbatim \n" and d.tags == ["a", "b"] and d.level == 0 and d.kind is None
    assert d.timestamp == "2026-08-30T14:05:12.345Z" and d.expires_at == "2026-09-03T00:00:00.000Z"
    assert d.media_hash == "ab" * 8 and d.meta == {"z": 1, "a": [1, 2.5, "é"]} and d.engagement is None
    assert lake.get(d.id) == d
    assert col(lake, "SELECT meta FROM deltas WHERE id = ?", d.id) == ['{"z":1,"a":[1,2.5,"é"]}']
    for bad in ({"content": "   "}, {"content": "a\x00b"}, {"source": ""}, {"source": "a\x00"}, {"media": "XYZ"},
                {"media": "ab" * 7}, {"tags": ["a\x00"]}, {"timestamp": "yesterday"}, {"expires": "soon"}):
        with pytest.raises(ValueError):
            lake.write(**{"content": "ok row", "source": "host", **bad})
    assert rows(lake) == 1
    assert _store.newest(lake, "crystal") is None
    c1 = lake.write("first crystal", "host", kind="crystal", derived_from=[d.id], timestamp="1 hour ago")
    c2 = lake.write("second crystal", "host", kind="crystal", derived_from=[d.id], expires="1h")
    assert _store.newest(lake, "crystal") == c2
    clock.advance(hours=2)
    assert _store.newest(lake, "crystal") == c1
    ro = make_lake(readonly=True)
    with pytest.raises(LakeError):
        ro.write("x", "host")


def test_newest_n(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    base = lake.write("seed", "host")
    lake.write("a crystal is not a mood", "host", kind="crystal", derived_from=[base.id])

    def mood(state: str, ts: str, **kw: Any) -> Delta:
        return lake.write(f'{{"state":"{state}"}}', "host", kind="mood", derived_from=[base.id],
                          tags=["feeling:x"], timestamp=ts, dedupe=False, **kw)

    m1 = mood("a", "2026-09-01T10:00:00Z")
    m2 = mood("b", "2026-09-01T11:00:00Z")
    m3 = mood("c", "2026-09-01T12:00:00Z")  # same second as m4: the later write wins on seq
    m4 = mood("d", "2026-09-01T12:00:00Z")
    gone = mood("gone", "2026-09-01T13:00:00Z", expires="1h")  # expires_at = now + 1h, newest while live

    assert _store.newest_n(lake, "mood", 3) == [gone, m4, m3]  # newest first, seq breaks the 12:00 tie
    clock.advance(hours=2)  # now past gone's expires_at
    assert _store.newest_n(lake, "mood", 3) == [m4, m3, m2]  # expired gone drops out, seq tie holds
    assert _store.newest_n(lake, "mood", 10) == [m4, m3, m2, m1]  # all live, fewer than n
    assert gone not in _store.newest_n(lake, "mood", 10) and base not in _store.newest_n(lake, "mood", 10)
    assert _store.newest_n(lake, "mood", 0) == [] and _store.newest_n(lake, "mood", -1) == []  # n < 1 before SQL


# --- engage -------------------------------------------------------------------------------------


def test_engage_snapshot(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    t = lake.write("  first line\nsecond line  ", "reader")
    e = lake.engage(t.id, "affirm", by="robin", note="useful")
    quoted = f"> first line\n> second line\n> — reader · {t.timestamp[:16]} · {t.id[:8]}"
    assert e.content == f"{quoted}\n\nuseful"
    assert (e.kind, e.source, e.level, e.derived_from, e.expires_at, e.meta) == ("engagement", "robin", 0, [t.id], None, None)
    assert e.engagement == Engagement(t.id, "affirm", "robin", "useful") and lake.get(e.id) == e
    stored = lake.conn.execute("SELECT target_id, kind, engaged_by, note FROM engagements WHERE delta_id = ?", (e.id,))
    assert list(stored.fetchone()) == [t.id, "affirm", "robin", "useful"]
    t2 = lake.write("a\n\nb", "reader")
    e2 = lake.engage(t2.id[:8], "reply-to")  # prefix resolves; the stored target is the full id
    assert e2.content == f"> a\n>\n> b\n> — reader · {t2.timestamp[:16]} · {t2.id[:8]}"
    assert e2.source == "engagement" and e2.engagement == Engagement(t2.id, "reply", None, None)
    t3 = lake.write("x" * 4001, "reader")
    assert lake.engage(t3.id, "refutes").content.split("\n")[0] == "> " + "x" * 4000 + "…"
    t4 = lake.write("pic", "reader", media="c" * 16)
    e4 = lake.engage(t4.id, "affirm", note="  padded  ")
    assert e4.content.endswith(f"· {t4.id[:8]} · [image]\n\n  padded")  # {note} as given, whole string stripped
    assert e4.media_hash == "c" * 16
    ee = lake.engage(e.id, "refute", by="ada", tags=["review"])  # engaging an engagement
    assert ee.content.startswith("> > first line\n") and ee.engagement.target_id == e.id and ee.tags == ["review"]  # type: ignore[union-attr]
    s = lake.engage(t.id, "reply", by="robin", note="a thought", snapshot=False)
    assert s.content == "a thought" and s.meta == {"snapshot": quoted} and lake.get(s.id) == s
    s2 = lake.engage(t.id, "reply", by="robin", snapshot=False)
    assert s2.content == f"> — reader · {t.timestamp[:16]} · {t.id[:8]}" and s2.meta == {"snapshot": quoted}
    with lake.tx() as conn:  # a swept target: the row is gone, sweep() itself belongs to _io
        conn.execute("DELETE FROM deltas WHERE id = ?", (t4.id,))
    with pytest.raises(NotFoundError):
        lake.engage(t4.id, "affirm")
    gone = lake.write("short lived", "reader", expires="1s")
    clock.advance(seconds=1)
    with pytest.raises(NotFoundError):
        lake.engage(gone.id, "affirm")
    with pytest.raises(ValueError):
        lake.engage(t.id, "like")
    assert lake.get(e4.id).media_hash == "c" * 16  # type: ignore[union-attr]


def test_engage_affirm_then_refute(make_lake: MakeLake) -> None:
    lake = make_lake()
    t = lake.write("target", "host")
    a = lake.engage(t.id, "affirm", by="robin")
    r = lake.engage(t.id, "refute", by="robin")
    assert a.id != r.id and a.content == r.content
    got = lake.get(t.id)  # the refutation surfaces as a receipt even though the net valence is 1.0
    assert got is not None and got.refuted_by == (Refutation(r.id, "robin", r.timestamp, None),)
    assert col(lake, "SELECT kind FROM engagements WHERE target_id = ? ORDER BY rowid", t.id) == ["affirm", "refute"]
    hits = maybe(lake.recall, source="host")
    if hits is not None:
        assert [(h.delta.id, h.valence) for h in hits] == [(t.id, 1.0)]
    assert lake.engage(t.id, "affirm", by="robin") == a
    assert lake.engage(t.id, "affirms", by="robin", dedupe=True) == a
    assert rows(lake, "engagements") == 2 and rows(lake) == 3
    assert lake.engage(t.id, "affirm", by="robin", note="now with a note").id not in (a.id, r.id)
    assert lake.engage(t.id, "affirm", by="ada").id != a.id
    assert lake.engage(t.id, "affirm", by="robin", dedupe=False).id != a.id
    assert rows(lake, "engagements") == 5


def test_get_carries_refutations(make_lake: MakeLake) -> None:
    lake = make_lake()
    t = lake.write("the release ships tuesday", "host", tags=["plan"])
    assert lake.get(t.id) == t  # unrefuted: refuted_by == (), equal to the write() return
    e = lake.engage(t.id, "refute", by="robin", note="slipped")
    got = lake.get(t.id)
    assert got is not None and got.refuted_by == (Refutation(e.id, "robin", e.timestamp, "slipped"),)
    assert replace(got, refuted_by=()) == t  # every other field byte-identical to the written row
    assert _store.resolve(lake, t.id).refuted_by == got.refuted_by


def test_engage_embed(make_lake: MakeLake, fake_embed: FakeEmbed, raising_embed: FakeEmbed) -> None:
    lake = make_lake(embed=fake_embed)
    t = lake.write("target", "host")
    gen = int(col(lake, "SELECT value FROM meta WHERE key = 'vectors_gen'")[0])
    e = lake.engage(t.id, "affirm", note="useful")
    assert set(col(lake, "SELECT delta_id FROM vectors")) == {t.id, e.id}
    assert col(lake, "SELECT value FROM meta WHERE key = 'vectors_gen'") == [str(gen + 1)]
    assert fake_embed.calls[-1] == [e.content]
    assert lake.engage(t.id, "affirm", note="useful") == e  # deduped: no vector, no embed call
    assert rows(lake, "vectors") == 2 and len(fake_embed.calls) == 2
    later = make_lake("later", embed=FakeEmbed(), embed_on_write=False)
    t2 = later.write("target", "host")
    e2 = later.engage(t2.id, "affirm", note="useful")
    assert rows(later, "vectors") == 0
    if maybe(later.embed_missing) is not None:
        assert set(col(later, "SELECT delta_id FROM vectors")) == {t2.id, e2.id}
    broken = make_lake("broken", embed=raising_embed)
    t3 = broken.write("target", "host", embed=False)
    with pytest.raises(EmbedError) as err:
        broken.engage(t3.id, "affirm")
    failed = err.value.delta
    assert failed is not None and failed.kind == "engagement" and broken.get(failed.id) == failed
    assert col(broken, "SELECT target_id FROM engagements WHERE delta_id = ?", failed.id) == [t3.id]
    assert rows(broken, "vectors") == 0 and "embed down" in str(err.value)


def test_write_embed(make_lake: MakeLake, fake_embed: FakeEmbed, raising_embed: FakeEmbed) -> None:
    lake = make_lake(embed=fake_embed, embed_on_write=False)
    assert lake.write("no vector", "host").id not in col(lake, "SELECT delta_id FROM vectors")
    d = lake.write("with vector", "host", embed=True)
    assert col(lake, "SELECT delta_id FROM vectors") == [d.id] and fake_embed.calls == [["with vector"]]
    assert col(lake, "SELECT value FROM meta WHERE key IN ('vectors_gen', 'embed_dim') ORDER BY key") == ["16", "1"]
    assert lake.write("with vector", "host", embed=True) == d and len(fake_embed.calls) == 1  # deduped
    on = make_lake("on", embed=fake_embed)
    assert on.write("auto", "host", embed=False).id not in col(on, "SELECT delta_id FROM vectors")
    assert rows(on, "vectors") == 0 and on.write("auto two", "host").id in col(on, "SELECT delta_id FROM vectors")
    broken = make_lake("broken", embed=raising_embed)
    with pytest.raises(EmbedError) as err:
        broken.write("row stays", "host")
    assert err.value.delta is not None and broken.get(err.value.delta.id) == err.value.delta
    assert rows(broken, "vectors") == 0 and broken.conn.in_transaction is False
    short = make_lake("short", embed=FakeEmbed(dim=8))
    first = short.write("sets dim 8", "host")
    assert col(short, "SELECT dim FROM vectors WHERE delta_id = ?", first.id) == [8]
    short.embed = FakeEmbed(dim=16)
    with pytest.raises(EmbedError) as err:  # wrong length: row committed, no vector
        short.write("wrong length", "host")
    assert err.value.delta is not None and rows(short) == 2 and rows(short, "vectors") == 1
    many = make_lake("many", embed=lambda texts: [[1.0] * 16, [1.0] * 16])
    with pytest.raises(EmbedError):
        many.write("two vectors back", "host")


# --- lineage, cited_by, host meta ---------------------------------------------------------------


def test_lineage(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    r1 = lake.write("row one", "host")
    r2 = lake.write("row two", "host")
    c = lake.write("episode", "host", kind="container", derived_from=[r1.id, r2.id])
    x = lake.write("who I am", "host", kind="crystal", derived_from=[c.id, r1.id])
    with lake.tx() as conn:  # r2 swept (sweep() is _io's; the edge from c dangles)
        conn.execute("DELETE FROM deltas WHERE id = ?", (r2.id,))
    lin = lake.lineage(x.id)
    assert [d.id for d in lin.rows] == [c.id, r1.id] and lin.dangling == [r2.id]
    assert lake.lineage(x.id, depth=1) == Lineage([c, r1], [])
    assert lake.lineage(c.id) == Lineage([r1], [r2.id]) and lake.lineage(r1.id) == Lineage([], [])
    assert lake.lineage(x.id[:8], depth=0) == Lineage([], [])
    assert lake.cited_by(r1.id) == [x, c]  # same timestamp under the frozen clock: seq DESC
    assert lake.cited_by(c.id, depth=2) == [x] and lake.cited_by(x.id) == []
    e = lake.engage(r1.id, "affirm", by="robin")
    assert lake.cited_by(r1.id) == [e, x, c] and lake.cited_by(r1.id[:8], depth=2) == [e, x, c]
    ttl = lake.write("fading topic", "host", kind="container", derived_from=[c.id], expires="1h")
    assert lake.cited_by(c.id) == [ttl, x]
    clock.advance(hours=2)
    assert lake.cited_by(c.id) == [x] and [d.id for d in lake.cited_by(c.id, include_expired=True)] == [ttl.id, x.id]
    assert [d.id for d in lake.lineage(ttl.id).rows] == [c.id, r1.id]  # expired root still walks by default
    with pytest.raises(NotFoundError):
        lake.lineage(ttl.id, include_expired=False)
    for bad in ("000000000000", r2.id, "nope"):
        with pytest.raises(NotFoundError):
            lake.lineage(bad)
        with pytest.raises(NotFoundError):
            lake.cited_by(bad)
    with pytest.raises(ValueError):
        lake.cited_by(r1.id, depth=0)


def test_meta_host_keys(make_lake: MakeLake) -> None:
    lake = make_lake()
    assert lake.meta_get("host:watermark") is None
    lake.meta_set("host:watermark", "x")
    assert lake.meta_get("host:watermark") == "x"
    lake.meta_set("host:watermark", "y")
    assert lake.meta_get("host:watermark") == "y" and lake.conn.in_transaction is False
    lake.meta_set("host:watermark", None)
    assert lake.meta_get("host:watermark") is None
    for key in ("vectors_gen", "schema_version", "", "host"):
        with pytest.raises(ValueError):
            lake.meta_set(key, "9")
        with pytest.raises(ValueError):
            lake.meta_get(key)
    assert col(lake, "SELECT value FROM meta WHERE key = 'vectors_gen'") == ["0"]
    ro = make_lake(readonly=True)
    with pytest.raises(LakeError):
        ro.meta_set("host:watermark", "z")


# --- export's write() half ----------------------------------------------------------------------


def test_export_canonical(make_lake: MakeLake) -> None:
    lake = make_lake()
    for bad in ({"v": float("nan")}, {"v": float("inf")}, {"v": -float("inf")}, {"a": {"b": [1, float("nan")]}}):
        with pytest.raises(ValueError):
            lake.write("row", "host", meta=bad)
    with pytest.raises(ValueError):
        lake.write("row", "host", meta={"o": object()})
    assert rows(lake) == 0 and lake.conn.in_transaction is False
    assert lake.write("row", "host", meta={"v": 1.5, "n": None, "t": True}).meta == {"v": 1.5, "n": None, "t": True}


# --- file-level behaviour -----------------------------------------------------------------------


def test_schema_version(tmp_path: Path, clock: FrozenClock) -> None:
    v2 = tmp_path / "v2.lake"
    with sqlite3.connect(v2) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '2')")
    with pytest.raises(SchemaError, match="schema_version 2"):
        Lake(v2, clock=clock)
    empty = tmp_path / "empty.lake"
    empty.touch()
    with Lake(empty, clock=clock) as lake:
        assert lake.get("000000000000") is None
        assert col(lake, "SELECT value FROM meta WHERE key IN ('schema_version', 'created_at', 'vectors_gen') ORDER BY key") == [DEFAULT_NOW, "1", "0"]
        assert col(lake, "PRAGMA journal_mode") == ["wal"]
        assert lake.write("first", "host").id == lake.get(lake.write("first", "host").id).id  # type: ignore[union-attr]
    junk = tmp_path / "junk.lake"
    junk.write_bytes(b"not a database at all, just bytes\n" * 40)
    with pytest.raises(SchemaError, match="not a lake"):
        Lake(junk, clock=clock)


def test_open_existing(tmp_path: Path, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "t.lake"
    with Lake(path, clock=clock) as lake:
        d = lake.write("the migration ran", "host")
    with sqlite3.connect(path) as raw:
        master = raw.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2").fetchall()
    statements: list[str] = []
    real_connect = sqlite3.connect

    def traced(*args: Any, **kw: Any) -> sqlite3.Connection:
        conn = real_connect(*args, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    with Lake(path, clock=clock) as again:
        assert again.get(d.id) == d
    assert statements and not [s for s in statements if s.lstrip().upper().startswith("CREATE")]
    assert not [s for s in statements if "journal_mode" in s and "=" in s]  # read, never re-issued
    with Lake(path, clock=clock, readonly=True) as ro:
        assert ro.get(d.id) == d
        hits = maybe(ro.recall, "migration")
        if hits is not None:
            assert [h.delta.id for h in hits] == [d.id]
        with pytest.raises(LakeError):
            ro.write("x", "host")
        with pytest.raises(LakeError):
            ro.engage(d.id, "affirm")
        assert col(ro, "PRAGMA journal_mode") == ["wal"]
    monkeypatch.undo()
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT type, name, sql FROM sqlite_master ORDER BY 1, 2").fetchall() == master
        raw.execute("DROP TABLE deltas_fts")
    with pytest.raises(SchemaError):
        Lake(path, clock=clock)
    with pytest.raises(LakeError):
        Lake(tmp_path / "absent.lake", clock=clock, readonly=True)


def _write_many(path: str, n: int, count: int) -> None:
    with Lake(path) as lake:
        for i in range(count):
            lake.write(f"row {i} from writer {n}", f"writer-{n}", tags=[f"w{n}"])


def _hold_lock(path: str, held: Any, release: Any) -> None:
    with Lake(path) as lake:
        lake.conn.execute("BEGIN IMMEDIATE")
        held.set()
        release.wait(30)
        lake.conn.execute("ROLLBACK")


def test_wal_two_processes(tmp_path: Path) -> None:
    path = tmp_path / "shared.lake"
    Lake(path).close()
    ctx = multiprocessing.get_context("spawn")
    writers = [ctx.Process(target=_write_many, args=(str(path), n, 200)) for n in range(2)]
    for p in writers:
        p.start()
    for p in writers:
        p.join(120)
    assert [p.exitcode for p in writers] == [0, 0]
    with Lake(path) as lake:
        assert rows(lake) == 400 and col(lake, "PRAGMA journal_mode") == ["wal"]
        assert col(lake, "SELECT count(DISTINCT source) FROM deltas") == [2]
    held, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(path), held, release))
    holder.start()
    try:
        assert held.wait(30)
        started = time.monotonic()
        with Lake(path) as reader:  # a reader never waits on the writer's lock (busy_timeout is 5 s)
            hits = maybe(reader.recall, source="writer-0", limit=5)
            assert (len(hits) if hits is not None else len(col(reader, "SELECT id FROM deltas LIMIT 5"))) == 5
            assert rows(reader) == 400
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        holder.join(30)
    assert holder.exitcode == 0


def test_sqlite_cli_reads_file(make_lake: MakeLake) -> None:
    lake = make_lake()
    for text in ("the migration ran", "migrations are pending", "nothing to see", "one more migration"):
        lake.write(text, "host")
    sql = "SELECT count(*) FROM deltas_fts WHERE deltas_fts MATCH 'migration'"
    cli = shutil.which("sqlite3")
    if cli:
        cmd, want = [cli, str(lake.path), sql], {"3"}
    elif sys.version_info >= (3, 12):  # the stdlib shell: plain SQLite in another process, tuple output
        cmd, want = [sys.executable, "-m", "sqlite3", str(lake.path), sql], {"(3,)"}
    else:
        pytest.skip("no sqlite3 shell available")
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    assert out in want
