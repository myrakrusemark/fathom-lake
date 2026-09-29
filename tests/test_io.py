"""§10 tests owned by _io: sweep, the lake-format import, canonical export, scale."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import pytest
from conftest import FakeEmbed, FrozenClock

from lake import Engagement, Lake
from lake._io import export_line

MakeLake = Callable[..., Lake]
SPEC_LINE = (
    '{"id":"3f2a9c1b7d4e","timestamp":"2026-08-30T14:05:12.345Z","content":"what did we decide about drift thresholds?",'
    '"source":"claude-code","kind":null,"level":0,"tags":["user","session:9c4e1f2a"],"derived_from":[],"engagement":null,'
    '"expires_at":null,"media_hash":null,"meta":null}\n'
)


def count(lake: Lake, sql: str, *params: object) -> int:
    return int(lake.conn.execute(sql, params).fetchone()[0])


def meta(lake: Lake, key: str) -> str | None:
    row = lake.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sweep(make_lake: MakeLake, clock: FrozenClock, fake_embed: FakeEmbed) -> None:
    lake = make_lake(embed=fake_embed)
    assert lake.media_dir == lake.path.with_name("t.media")
    h1, h2, h3 = "a" * 16, "b" * 16, "c" * 16
    keep = lake.write("a permanent row with a picture", "host", media=h1)
    gone = lake.write("a state that expires soon", "daemon", tags=["state"], derived_from=[keep.id], expires="1s")
    gen0 = int(meta(lake, "vectors_gen") or 0)
    lake.conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_failed', ?)", (json.dumps([gone.id, keep.id]),))
    media = lake.media_dir
    media.mkdir()
    old, fresh = clock().timestamp() - 11 * 60, clock().timestamp() - 60
    for name, mtime in (("README", old), (f"{h1}.thumb.webp", old), (f"{h2}.webp", old), (f"{h3}.webp", fresh)):
        (media / name).write_bytes(b"x")
        os.utime(media / name, (mtime, mtime))
    assert count(lake, "SELECT count(*) FROM vectors WHERE delta_id = ?", gone.id) == 1
    clock.advance(seconds=2)
    assert lake.sweep() == {"deleted": 1, "orphan_media": 1}
    for table, col in (("deltas", "id"), ("delta_tags", "delta_id"), ("derived_from", "delta_id"), ("vectors", "delta_id")):
        assert count(lake, f"SELECT count(*) FROM {table} WHERE {col} = ?", gone.id) == 0
    assert count(lake, "SELECT count(*) FROM deltas_fts WHERE deltas_fts MATCH '\"expires\"'") == 0
    assert lake.get(keep.id) is not None and count(lake, "SELECT count(*) FROM vectors") == 1
    assert int(meta(lake, "vectors_gen") or 0) == gen0 + 1
    assert json.loads(meta(lake, "embed_failed") or "[]") == [keep.id]
    assert sorted(p.name for p in media.iterdir()) == sorted(["README", f"{h1}.thumb.webp", f"{h3}.webp"])
    assert lake.media_path(h1) == media / f"{h1}.thumb.webp" and lake.media_path(h2) is None
    assert lake.sweep() == {"deleted": 0, "orphan_media": 0}


def test_import_lake_format(make_lake: MakeLake, tmp_path: Path) -> None:
    src = make_lake("src")
    a = src.write("we chose sqlite for the lake file", "notes", tags=["design"])
    b = src.write("the clef 𝄞 marks the astral plane, café included", "notes", tags=["music"], meta={"z": 1, "a": 2.5})
    box = src.write("summary of the design chat", "notes", kind="container", derived_from=[a.id, b.id])
    eng = src.engage(a.id, "affirm", by="robin")
    assert box.level == 1
    first = tmp_path / "first.jsonl"
    assert src.export(first) == 4
    assert "𝄞" in first.read_text(encoding="utf-8") and '"meta":{"z":1,"a":2.5}' in first.read_text(encoding="utf-8")

    dst = make_lake("dst")
    assert dst.import_(first) == {"written": 4, "skipped": 0, "errors": 0}
    assert [dst.get(d.id) is not None for d in (a, b, box, eng)] == [True] * 4
    got_box = dst.get(box.id)
    assert got_box is not None and (got_box.kind, got_box.level, got_box.derived_from) == ("container", 1, [a.id, b.id])
    got_b = dst.get(b.id)
    assert got_b is not None and got_b.content == b.content and got_b.meta == {"z": 1, "a": 2.5}
    got_eng = dst.get(eng.id)
    assert got_eng is not None and got_eng.kind == "engagement" and got_eng.engagement == Engagement(a.id, "affirm", "robin", None)
    row = dst.conn.execute("SELECT target_id, kind, engaged_by, note FROM engagements WHERE delta_id = ?", (eng.id,)).fetchone()
    assert tuple(row) == (a.id, "affirm", "robin", None)
    second = tmp_path / "second.jsonl"
    assert dst.export(second) == 4 and second.read_bytes() == first.read_bytes()
    assert json.loads(meta(dst, "container_watermark") or "{}") == {"seq": 4, "pos": None}

    # error lines: a bad id, a lake: source with no parents, a relative timestamp (§12.8), non-JSON
    lines = first.read_text(encoding="utf-8").splitlines()
    xyz, loop, rel = (json.loads(lines[0]) for _ in range(3))
    xyz["id"] = "XYZ"
    loop.update(id="0123456789ab", source="lake:container", derived_from=[])
    rel.update(id="0123456789ac", timestamp="2 hours ago")
    bad = tmp_path / "bad.jsonl"
    bad.write_text("\n".join([*lines, json.dumps(xyz), "", json.dumps(loop), "not json", json.dumps(rel)]) + "\n", encoding="utf-8")
    assert dst.import_(bad) == {"written": 0, "skipped": 4, "errors": 4}
    assert count(dst, "SELECT count(*) FROM deltas") == 4 and dst.conn.in_transaction is False

    hit = next(h for h in dst.recall("sqlite lake file") if h.delta.id == a.id)
    assert hit.valence == pytest.approx(1.05)


def test_export_canonical(make_lake: MakeLake, tmp_path: Path, fake_embed: FakeEmbed) -> None:
    lake = make_lake(embed=fake_embed)
    d = lake.write(
        "what did we decide about drift thresholds?", "claude-code", tags=["user", "session:9c4e1f2a"],
        timestamp="2026-08-30T14:05:12.345Z",
    )
    out = tmp_path / "out.jsonl"
    assert lake.export(out) == 1
    assert out.read_bytes() == SPEC_LINE.replace("3f2a9c1b7d4e", d.id).encode("utf-8")
    assert export_line(replace(d, id="3f2a9c1b7d4e")) == SPEC_LINE
    e = lake.engage(d.id, "reply", by="robin", note="see the 𝄞 thread")
    assert lake.export(out) == 2
    line = out.read_text(encoding="utf-8").splitlines()[1]
    assert '"engagement":{"target_id":"' + d.id + '","kind":"reply","by":"robin","note":"see the 𝄞 thread"}' in line
    assert json.loads(line)["id"] == e.id and "\\u" not in line

    # vectors=True appends the unit vector as the last key; it imports back into the vectors table
    assert lake.export(out, vectors=True) == 2
    objs = read_jsonl(out)
    assert [list(o)[-1] for o in objs] == ["vector", "vector"] and len(objs[0]["vector"]) == 16
    assert sum(x * x for x in objs[0]["vector"]) == pytest.approx(1.0)
    copy = make_lake("copy")
    assert copy.import_(out) == {"written": 2, "skipped": 0, "errors": 0}
    assert count(copy, "SELECT count(*) FROM vectors") == 2 and meta(copy, "embed_dim") == "16"


@pytest.mark.slow
def test_scale(tmp_path: Path, clock: FrozenClock) -> None:
    n = 50_000
    words = "lake sqlite drift threshold crystal consolidate container mood recall vector migration sweep budget strip anchor timeline".split()
    start = clock() - timedelta(seconds=10 * n)
    bursts = {n // 4, n // 2, 3 * n // 4}
    src = tmp_path / "scale.jsonl"
    with src.open("w", encoding="utf-8") as fh:
        burst_ts: str | None = None
        for i in range(n):
            ts = (start + timedelta(seconds=10 * i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            if i in bursts:
                burst_ts = ts
            if burst_ts is not None and i - max(b for b in bursts if b <= i) < 300:
                ts = burst_ts
            w = [words[(i * k + k) % len(words)] for k in range(1, 6)]
            row: dict[str, Any] = {
                "id": f"{i:012x}", "timestamp": ts, "kind": None, "level": 0, "derived_from": [], "engagement": None,
                "media_hash": None, "meta": None,
            }
            if i % 6 == 5:
                exp = (start + timedelta(seconds=10 * i, hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
                row.update(content=f"office temperature {20 + i % 7}.{i % 10}C humidity {40 + i % 20}%", source="homeassistant",
                           tags=["state", "sensor:office", "kind:reading", f"hour:{i // 360}", "daemon"], expires_at=exp)
            else:
                row.update(content=f"{w[0]} {w[1]} and {w[2]}: note {i} on the {w[3]} while the {w[4]} settles into place",
                           source="claude-code" if i % 2 else "fathom-chat", expires_at=None,
                           tags=["user" if i % 2 else "assistant", f"session:s{i * 30 // n}", f"topic:{w[0]}", f"day:{i // 8640}", "proj:lake"])
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    path = tmp_path / "scale.lake"
    with Lake(path, clock=clock) as lake:
        t0 = perf_counter()
        assert lake.import_(src, include_expired=True) == {"written": n, "skipped": 0, "errors": 0}
        import_s = perf_counter() - t0
    with Lake(path, clock=clock) as fresh:
        t0 = perf_counter()
        rendered = fresh.context("drift threshold sqlite")
        context_s = perf_counter() - t0
        t0 = perf_counter()
        fresh.write("a new note about the lake file", "claude-code", tags=["never", "seen", "tag", "set", "before"])
        write_s = perf_counter() - t0
        t0 = perf_counter()
        fresh.due("container")
        due_s = perf_counter() - t0
    assert isinstance(rendered, str)
    assert import_s < 60, f"import_ took {import_s:.1f}s"
    assert context_s < 1, f"context took {context_s:.3f}s"
    assert write_s < 0.05, f"write took {write_s * 1000:.1f}ms"
    assert due_s < 1, f"due took {due_s:.3f}s"
