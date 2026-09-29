"""§13.10 tests owned by remote.py: RemoteLake round-trips against a real `lake serve` subprocess,
the §13.4 error mapping, §13.6 fail-soft, the §13.7 spool (offline writes, flush, flock, cap), the
CLI's LAKE_URL routing and `spool` command, and the §13.8 env file. The serve-side pins (auth
startup refusals, token file modes) belong to tests/test_serve.py; the serve harness itself is
shared from tests/conftest.py."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, script, serve_env, served

import lake as lake_pkg
from lake import (
    CollapsedRun, ConsolidateError, Delta, Engagement, Lake, LakeError, NotFoundError, PlanError,
    RemoteLake, cli,
)
from lake._io import export_line
from lake.remote import _delta as remote_delta
from lake import setting
from lake.remote import read_env_file  # the §13.8 parser, re-exported from lake._env for older callers

PROPOSE = '{"kind":"propose","title":"A stretch","summary":"Rows about one thing.","rationale":"one thread"}'


# --- fixtures and helpers --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test runs with HOME redirected (spool and env-file defaults land under tmp_path) and
    no LAKE_* in the environment, so a stray flush can never touch the real ~/.lake."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("LAKE", "LAKE_URL", "LAKE_FILE", "LAKE_TOKEN", "LAKE_THINK", "LAKE_EMBED"):
        monkeypatch.delenv(var, raising=False)
    return home


def dead_url() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def fab_line(i: int, ts: str = "2026-09-01T00:00:00.000Z") -> str:
    """One fabricated §4.8 lake-format spool line with a deterministic id."""
    return export_line(Delta(f"{i:012x}", ts, f"prefab row {i}", "prefab", None, 0, [], [], None, None,
                             {"spooled": True}, None))


def hit_ids(hits: list[Any]) -> list[str]:
    return [h.delta.id for h in hits]


# --- factory and refusals (§13.6) ------------------------------------------------------------------


def test_remote_factory(tmp_path: Path) -> None:
    rl = lake_pkg.open("http://127.0.0.1:1/")
    assert isinstance(rl, RemoteLake) and rl.url == "http://127.0.0.1:1", "trailing slash stripped"
    with lake_pkg.open(tmp_path / "t.lake") as lk:
        assert isinstance(lk, Lake)
    with pytest.raises(ValueError, match=r"not an http\(s\) url: ftp://x"):
        RemoteLake("ftp://x")
    with pytest.raises(LakeError, match="think= is not available over HTTP"):
        lake_pkg.open("http://127.0.0.1:1", think=lambda p, **kw: p)
    for kw in ("readonly", "clock", "embed", "model_name", "collapse_sources"):
        with pytest.raises(LakeError, match=f"{kw}= is not available over HTTP"):
            RemoteLake("http://127.0.0.1:1", **{kw: 1})  # type: ignore[arg-type]
    with pytest.raises(LakeError, match="media_path is not available over HTTP"):
        rl.media_path("a" * 16)
    with pytest.raises(LakeError, match="meta_get is not available over HTTP"):
        rl.meta_get("host:x")
    with pytest.raises(LakeError, match="meta_set is not available over HTTP"):
        rl.meta_set("host:x", "1")
    rl.close()  # a no-op; the context manager form works too
    with RemoteLake("http://127.0.0.1:1") as same:
        assert same.last_warnings == [] and same.last_run is None


# --- wire round-trips (§13.1, §13.3) ---------------------------------------------------------------


def test_serve_wire_roundtrip(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        parent = rl.write("the parent thought 🦋\nsecond line", "chat", tags=["x", "session:9c"],
                          meta={"k": [1, "🦋"], "n": None}, media="a" * 16, timestamp="10 minutes ago")
        child = rl.write("a follow-up that cites it", "chat", derived_from=[parent.id])
        with Lake(file, readonly=True) as lk:
            assert lk.get(parent.id) == parent, "every §4.1 field survives the wire"
            assert lk.get(child.id) == child and child.derived_from == [parent.id]
        assert parent.engagement is None and parent.tags == ["x", "session:9c"]
        assert parent.meta == {"k": [1, "🦋"], "n": None} and parent.media_hash == "a" * 16
        assert rl.get(parent.id[:8]) == parent, "prefix get"
        assert rl.get("aaaaaaaaaaaa") is None and rl.get("not-an-id") is None
        assert rl.crystal() is None, "crystal on a crystal-less lake is null -> None"
        stats = rl.stats()
        with Lake(file, readonly=True) as lk:
            local = lk.stats()
            assert rl.due("container") is lk.due("container") and rl.due("mood") is lk.due("mood")
        for key in ("rows", "live_rows", "by_kind", "by_source", "tags", "engagements", "schema_version"):
            assert stats[key] == local[key], key
        assert rl.sweep() == {"deleted": 0, "orphan_media": 0}


def test_serve_recall_plan(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        anchor = rl.write("the lake design decision about drift thresholds", "chat", timestamp="30 minutes ago")
        for i, ago in enumerate(("29 minutes ago", "28 minutes ago", "27 minutes ago")):
            rl.write(f"background chatter number {i}", "noisy", timestamp=ago)
        rl.write("an unrelated note about cooking pasta", "chat", timestamp="2 hours ago")
        steps = [
            {"id": "s", "search": "lake design drift", "limit": 5},
            {"id": "agg", "aggregate": "s", "group_by": "source"},
            {"id": "tl", "timeline": "s", "radius_minutes": 10, "max_per_side": 5, "collapse_sources": ["noisy"]},
        ]
        with Lake(file, readonly=True) as lk:
            local_hits = lk.recall(plan=steps)
            local_res = lk.plan(steps)
        hits = rl.recall(plan=steps)
        assert hit_ids(hits) == hit_ids(local_hits)
        for mine, theirs in zip(hits, local_hits, strict=True):
            assert mine.score == pytest.approx(theirs.score, abs=1e-6), "scores to 6 places (§13.10)"
            assert (mine.delta, mine.matched, mine.step) == (theirs.delta, theirs.matched, theirs.step)
        res = rl.plan(steps)
        assert list(res.steps) == ["s", "agg", "tl"], "steps cross as pairs in plan order"
        assert res.steps["agg"].buckets == local_res.steps["agg"].buckets
        assert res.steps["tl"].timelines == local_res.steps["tl"].timelines
        strips = res.steps["tl"].timelines
        assert strips is not None
        runs = [r for tl in strips for r in tl.rows if isinstance(r, CollapsedRun)]
        assert runs and runs[0].source == "noisy" and runs[0].count == 3, "a CollapsedRun crossed by its keys"
        assert any(r.is_anchor for tl in strips for r in tl.rows
                   if not isinstance(r, CollapsedRun) and r.delta.id == anchor.id)
        assert res.warnings == local_res.warnings and res.timing_ms >= 0.0
        with pytest.raises(PlanError):
            rl.plan([{"id": "bad", "search": 7}])
        with pytest.raises(ValueError, match="limit"):
            rl.recall("x", limit=0)


def test_serve_context_blocks(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        rl.write("we decided the drift threshold is 0.3 for the lake", "chat", timestamp="20 minutes ago")
        rl.write("the sky was heavy with rain all afternoon", "journal", timestamp="3 hours ago")
        query = "what did we decide about drift thresholds?"
        with Lake(file, readonly=True) as lk:
            local = lk.context_blocks(query)
        res = rl.context_blocks(query)
        assert res.rendered == local.rendered
        assert hit_ids(res.hits) == hit_ids(local.hits)
        assert res.containers == local.containers and res.strips == local.strips
        assert res.omitted_strips == local.omitted_strips and res.crystal == local.crystal
        assert rl.last_warnings == res.warnings == local.warnings, "response warnings populate last_warnings"
        assert rl.context(query) == local.rendered


def test_serve_engage_valence(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        target = rl.write("the migration plan for the lake file", "chat", timestamp="5 minutes ago")
        row = rl.engage(target.id, "affirm", by="robin", note="yes, this")
        assert row.engagement == Engagement(target.id, "affirm", "robin", "yes, this")
        assert row.kind == "engagement" and row.derived_from == [target.id]
        assert "> the migration plan for the lake file" in row.content, "the §4.6 snapshot crossed intact"
        with Lake(file, readonly=True) as lk:
            assert lk.get(row.id) == row
        hit = next(h for h in rl.recall("migration plan lake") if h.delta.id == target.id)
        assert hit.valence == pytest.approx(1.05)
        with pytest.raises(NotFoundError):
            rl.engage("aaaaaaaaaaaa", "affirm")


def test_serve_lineage_cited_by(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        a = rl.write("first observation about the reeds", "chat")
        b = rl.write("a sediment layer over it", "chat", kind="sediment", derived_from=[a.id])
        c = rl.write("a container over both", "chat", kind="container", derived_from=[a.id, b.id])
        assert (b.level, c.level) == (0, 1)
        with Lake(file, readonly=True) as lk:
            assert rl.lineage(c.id) == lk.lineage(c.id)
            assert rl.lineage(c.id, depth=1).rows == lk.lineage(c.id, depth=1).rows
            assert rl.cited_by(a.id, depth=2) == lk.cited_by(a.id, depth=2)
        for exc_call in (lambda: rl.lineage("aaaaaaaaaaaa"), lambda: rl.cited_by("aaaaaaaaaaaa")):
            with pytest.raises(NotFoundError, match="no live delta matches"):
                exc_call()


def test_serve_consolidate(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    think = script(tmp_path, "think.sh", f"echo '{PROPOSE}'")
    with Lake(file) as lk:  # rows first, so due('container') is true when the server answers
        for i in range(3):
            lk.write(f"row {i} about the lake migration plan", "chat", timestamp="45 minutes ago")
    with served(home, "--file", str(file), "--think", f"cmd:{think}", "--model-name", "fake-1") as url:
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            assert rl.due("container") is True
            row = rl.consolidate("container")
            assert row is not None and (row.source, row.kind) == ("lake:container", "container")
            run = rl.last_run
            assert run is not None and run.kind == "container" and run.written == [row]
            assert run.skipped == 0 and run.think_calls >= 1
            assert run.window is not None and isinstance(run.window, tuple)
            assert all(re.fullmatch(r"\d{4}-.*Z", w) for w in run.window)
            with Lake(file, readonly=True) as lk:
                stored = lk.get(row.id)
                assert stored is not None and stored.meta == row.meta, "the think ran server-side"


def test_serve_crystal_edits_and_log(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.3 over HTTP: the options travel in opts, the server-side think writes an items crystal, a second
    edits pass keeps its items, and `lake crystal --log` walks the growth log through the remote client."""
    file = tmp_path / "t.lake"
    body = ("import json, re, sys; ids = re.findall(r'^\\[([0-9a-f]{12})\\]', sys.stdin.read(), re.M);"
            " t = ['I ship small releases and keep them reversible.', 'I answer with the result first, no preamble.',"
            " 'I want speed and I keep paying for skipped tests.'];"
            " print(json.dumps({'items': [{'op': 'add', 'section': s, 'text': x, 'cite': ids[-1:], 'why': 'rows'}"
            " for s, x in zip(['core', 'core', 'tension'], t)]}))")
    think = script(tmp_path, "think.sh", f'{sys.executable} -c "{body}"')
    with Lake(file) as lk:
        for i in range(3):
            lk.write(f"row {i} about the release process", "chat", timestamp="45 minutes ago")
    with served(home, "--file", str(file), "--think", f"cmd:{think}") as url:
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            first = rl.consolidate("crystal", min_chars=100)
            assert first is not None and first.meta is not None and len(first.meta["items"]) == 3
            assert first.content.startswith("## What I hold to\n\nI ship small releases")
            rl.write("row 3 about the release process", "chat")
            second = rl.consolidate("crystal", min_chars=100, max_edits=3)
            assert second is not None and second.meta is not None
            assert [i["id"] for i in second.meta["items"]] == ["c1", "c2", "c3", "c4", "c5", "c6"]
            assert second.meta["implicit_keep"] == 3 and rl.system_prompt(moods=0).endswith(second.content + "\n")
        monkeypatch.setenv("LAKE_URL", url)
        code, out, err = run_cli(capsys, "crystal", "--log", "--limit", "4")
        assert code == 0, err
        lines = out.splitlines()
        assert len(lines) == 4 and all(" · add · — → " in ln for ln in lines)
        assert lines[3].startswith(first.timestamp[:16]) and lines[0].startswith(second.timestamp[:16])


def test_serve_consolidate_no_think(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        with pytest.raises(ConsolidateError) as err:
            rl.consolidate("mood")
        assert "consolidate needs a server-side think" in str(err.value)
        req = urllib.request.Request(url + "/v1/consolidate", data=b'{"kind":"mood"}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as raw:
            urllib.request.urlopen(req, timeout=5)
        assert raw.value.code == 409


def test_serve_export_import(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    emb = script(tmp_path, "emb.sh",
                 "python3 -c 'import json,sys; t=json.load(sys.stdin); "
                 "print(json.dumps([[1,0] if \"lake\" in s else [0,1] for s in t]))'")
    with served(home, "--file", str(file), "--embed", f"cmd:{emb}") as url:
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            rl.write("about the lake", "chat")
            rl.write("about something else", "chat")
            n = rl.export(tmp_path / "remote.jsonl")
            with Lake(file, readonly=True) as lk:
                assert n == lk.export(tmp_path / "local.jsonl") == 2
                rl.export(tmp_path / "remote-v.jsonl", vectors=True)
                lk.export(tmp_path / "local-v.jsonl", vectors=True)
            assert (tmp_path / "remote.jsonl").read_bytes() == (tmp_path / "local.jsonl").read_bytes()
            assert (tmp_path / "remote-v.jsonl").read_bytes() == (tmp_path / "local-v.jsonl").read_bytes()
            assert '"vector"' in (tmp_path / "remote-v.jsonl").read_text(encoding="utf-8")
            # import: two fresh lake-format lines; a second import skips them all by id
            batch = tmp_path / "batch.jsonl"
            batch.write_text(fab_line(1) + fab_line(2), encoding="utf-8")
            assert rl.import_(batch) == {"written": 2, "skipped": 0, "errors": 0}
            assert rl.import_(batch) == {"written": 0, "skipped": 2, "errors": 0}
            assert rl.get("000000000001") is not None
            # an expired line lands only under include_expired, then hides from plain get()
            old = tmp_path / "old.jsonl"
            old.write_text(export_line(Delta("00000000000e", "2026-01-01T00:00:00.000Z", "expired row",
                                             "prefab", None, 0, [], [], "2026-01-02T00:00:00.000Z",
                                             None, None, None)), encoding="utf-8")
            assert rl.import_(old) == {"written": 0, "skipped": 1, "errors": 0}
            assert rl.import_(old, include_expired=True)["written"] == 1
            assert rl.get("00000000000e") is None
            got = rl.get("00000000000e", include_expired=True)
            assert got is not None and got.content == "expired row"


def test_serve_error_mapping(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file), "--embed", "cmd:exit 2") as url:
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            with pytest.raises(ValueError, match="limit"):
                rl.recall("x", limit=0)
            with pytest.raises(PlanError, match="invalid search"):
                rl.recall(plan=[{"id": "s", "search": 3}])
            with pytest.raises(NotFoundError):
                rl.engage("aaaaaaaaaaaa", "affirm")
            with pytest.raises(LakeError, match=r"unknown endpoint: GET /v1/nope"):
                rl._send("GET", "/v1/nope")
            from lake._types import ClosedLoopError, EmbedError
            with pytest.raises(ClosedLoopError):
                rl.write("x" * 20, "lake:container", derived_from=["aaaaaaaaaaaa"])
            with pytest.raises(EmbedError) as err:  # the 500 family: raised as the named class
                rl.write("a row the broken embed rejects", "chat")
            assert err.value.delta is None and err.value.stored is None
            assert rl.stats()["rows"] == 1, "the §4.4 row committed server-side despite the embed failure"


def test_serve_auth(tmp_path: Path, home: Path) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file), env_extra={"LAKE_TOKEN": "k3yb0ard-c4t"}) as url:
        with urllib.request.urlopen(url + "/v1/health", timeout=5) as resp:  # no auth needed
            assert json.load(resp) == {"ok": True, "rows": 0}
        for token in (None, "wrong"):
            with RemoteLake(url, token=token, spool_path=tmp_path / "s.jsonl") as rl:
                with pytest.raises(LakeError, match="unauthorized"):
                    rl.stats()
        with pytest.raises(urllib.error.HTTPError) as raw:
            urllib.request.urlopen(url + "/v1/stats", timeout=5)
        assert raw.value.code == 401 and raw.value.headers.get("WWW-Authenticate") == "Bearer"
        assert json.loads(raw.value.read()) == {"error": "LakeError", "message": "unauthorized"}
        with RemoteLake(url, token="k3yb0ard-c4t", spool_path=tmp_path / "s.jsonl") as rl:
            assert rl.stats()["rows"] == 0


def test_non_envelope_error_body(tmp_path: Path) -> None:
    class Broken(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"boom: not a JSON envelope"
            self.send_response(500)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Broken)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rl = RemoteLake(f"http://127.0.0.1:{srv.server_port}", spool_path=tmp_path / "s.jsonl")
        with pytest.raises(LakeError, match="HTTP 500: boom: not a JSON envelope"):
            rl.stats()
    finally:
        srv.shutdown()
        srv.server_close()


# --- fail soft, fail loud (§13.6) ------------------------------------------------------------------


def test_remote_fail_soft(tmp_path: Path) -> None:
    rl = RemoteLake(dead_url(), spool_path=tmp_path / "s.jsonl")
    assert rl.recall("anything at all") == []
    assert len(rl.last_warnings) == 1 and rl.last_warnings[0].startswith("lake unreachable: ")
    warning = rl.last_warnings[0]
    assert rl.context("a long enough query here") == "" and rl.last_warnings == [warning]
    assert rl.system_prompt() == "" and rl.last_warnings == [warning]
    res = rl.context_blocks("a long enough query here")
    assert (res.crystal, res.hits, res.containers, res.strips, res.omitted_strips, res.rendered) == \
        (None, [], [], [], 0, "") and res.warnings == [warning]
    plan_res = rl.plan([{"id": "s", "search": "x"}])
    assert plan_res.steps == {} and plan_res.warnings == [warning] and plan_res.timing_ms == 0.0
    for loud in (lambda: rl.get("aaaaaaaaaaaa"), rl.stats, rl.sweep, rl.crystal,
                 lambda: rl.due("mood"), lambda: rl.engage("aaaaaaaaaaaa", "affirm"),
                 lambda: rl.consolidate("mood"), lambda: rl.lineage("aaaaaaaaaaaa"),
                 lambda: rl.cited_by("aaaaaaaaaaaa"), lambda: rl.export(tmp_path / "e.jsonl"),
                 rl.embed_missing):
        with pytest.raises(LakeError, match="lake unreachable: "):
            loud()


# --- the spool (§13.7) -----------------------------------------------------------------------------


def test_spool_offline_write(tmp_path: Path) -> None:
    spool = tmp_path / "sub" / "spool.jsonl"
    rl = RemoteLake(dead_url(), spool_path=spool)
    delta = rl.write("an offline note about the lake", "chat", tags=["t"], meta={"mine": 1})
    assert re.fullmatch(r"[0-9a-f]{12}", delta.id) and delta.meta == {"mine": 1, "spooled": True}
    assert delta.kind is None and delta.level == 0
    assert len(rl.last_warnings) == 1 and rl.last_warnings[0].startswith("lake unreachable: ")
    assert f"write spooled to {spool}" in rl.last_warnings[0], "a spooled write warns"
    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    lines = spool.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == delta.id
    second = rl.write("a second one, with no caller meta", "chat")
    assert second.meta == {"spooled": True}
    with Lake(tmp_path / "t.lake") as lk:  # the lines are §4.8 lake-format a local import_() accepts
        assert lk.import_(spool) == {"written": 2, "skipped": 0, "errors": 0}
        got = lk.get(delta.id)
        assert got == delta, "the spooled Delta matches what the flush will land"
    # a structural write fails loud and spools nothing; a bad write raises before any spooling
    with pytest.raises(LakeError, match="lake unreachable: "):
        rl.write("a container", "chat", kind="sediment", derived_from=[delta.id])
    with pytest.raises(Exception, match="reserved for consolidate"):
        rl.write("x" * 20, "lake:container", derived_from=[delta.id])
    with pytest.raises(ValueError, match="non-empty"):
        rl.write("   ", "chat")
    with pytest.raises(ValueError, match="media"):
        rl.write("has bad media", "chat", media="nope")
    assert len(spool.read_text(encoding="utf-8").splitlines()) == 2, "nothing extra spooled"


def test_spool_flush(tmp_path: Path, home: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    offline = RemoteLake(dead_url(), spool_path=spool)
    ids = [offline.write(f"offline note {i} about the lake", "chat").id for i in range(3)]
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=spool) as rl:
        hits = rl.recall("offline note lake", limit=10)
        assert set(ids) <= set(hit_ids(hits)), "the flush precedes the recall's own request"
        assert spool.stat().st_size == 0, "truncated under the lock"
        got = rl.get(ids[0])
        assert got is not None and got.meta == {"spooled": True}, "offline provenance stays visible"
        assert rl.flush_spool() is None, "an empty spool flushes to None"


def test_spool_double_flush(tmp_path: Path, home: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    offline = RemoteLake(dead_url(), spool_path=spool)
    for i in range(3):
        offline.write(f"double flush row {i}", "chat")
    saved = spool.read_bytes()
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=spool) as rl:
        assert rl.flush_spool() == {"written": 3, "skipped": 0, "errors": 0}
        spool.write_bytes(saved)  # the §13.7 crash window: imported but not truncated
        assert rl.flush_spool() == {"written": 0, "skipped": 3, "errors": 0}
        assert rl.stats()["rows"] == 3, "every line skipped by id; each row exists once"
        assert spool.stat().st_size == 0


def test_spool_flock(tmp_path: Path, home: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    url = dead_url()
    writer = ("import sys\nfrom lake.remote import RemoteLake\n"
              "rl = RemoteLake(sys.argv[1], spool_path=sys.argv[2])\n"
              "for i in range(100):\n"
              "    rl.write(f'concurrent row {sys.argv[3]} {i}', 'proc' + sys.argv[3])\n")
    procs = [subprocess.Popen([sys.executable, "-c", writer, url, str(spool), tag],
                              cwd=str(REPO), env=serve_env(home)) for tag in ("a", "b")]
    for proc in procs:
        assert proc.wait(timeout=120) == 0
    lines = spool.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 200, "two concurrent writers lose nothing"
    ids = {json.loads(line)["id"] for line in lines}  # every line parses, every id distinct
    assert len(ids) == 200
    # an append issued while a flush holds LOCK_EX completes afterwards and survives the truncation
    fd = os.open(spool, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    rl = RemoteLake(url, spool_path=spool)
    thread = threading.Thread(target=lambda: rl.write("the late append", "chat"))
    thread.start()
    time.sleep(0.3)  # the writer is now blocked on the lock
    os.ftruncate(fd, 0)  # what a real flush does in step 4
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    thread.join(timeout=30)
    assert not thread.is_alive()
    lines = spool.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["content"] == "the late append"


def test_spool_cap(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.parent.mkdir(exist_ok=True)
    spool.write_text("".join(fab_line(i) for i in range(10_000)), encoding="utf-8")
    os.chmod(spool, 0o600)
    rl = RemoteLake(dead_url(), spool_path=spool)
    new_ids = [rl.write(f"overflow row {i} beyond the cap", "chat").id for i in range(3)]
    lines = spool.read_text(encoding="utf-8").splitlines()
    dropped = spool.with_suffix(".dropped")
    assert len(lines) == 10_000 and dropped.read_text(encoding="utf-8") == "3\n"
    assert json.loads(lines[0])["id"] == f"{3:012x}", "the oldest lines dropped"
    assert [json.loads(line)["id"] for line in lines[-3:]] == new_ids, "the newest kept"
    assert stat.S_IMODE(dropped.stat().st_mode) == 0o600
    rl.write("one more past the cap", "chat")
    rl.write("and another", "chat")
    assert dropped.read_text(encoding="utf-8") == "5\n", "the dropped count is cumulative"
    assert len(spool.read_text(encoding="utf-8").splitlines()) == 10_000


def test_spool_refuses_symlink(tmp_path: Path) -> None:
    """§13.7 hardening: a symlink planted at the spool path (or its .dropped side file) is refused
    via O_NOFOLLOW, never followed — no arbitrary-file append and no truncate-to-zero of the target."""
    victim = tmp_path / "victim.conf"
    victim.write_text("precious config\n", encoding="utf-8")
    spool = tmp_path / "spool.jsonl"
    os.symlink(victim, spool)
    rl = RemoteLake(dead_url(), spool_path=spool)
    with pytest.raises(OSError):
        rl.write("an offline note that must not follow the symlink", "chat")
    assert victim.read_text(encoding="utf-8") == "precious config\n", "the symlinked target is untouched"
    assert spool.is_symlink(), "the planted symlink was not replaced by a regular file"
    # a fresh (non-symlinked) spool parent is created 0700
    nested = tmp_path / "fresh" / "spool.jsonl"
    RemoteLake(dead_url(), spool_path=nested).write("a real offline note", "chat")
    assert stat.S_IMODE(nested.parent.stat().st_mode) == 0o700


# --- the CLI over LAKE_URL and `lake spool` (§13.9) ------------------------------------------------


def test_cli_remote(capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path,
                    monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file), env_extra={"LAKE_TOKEN": "tok"}) as url:
        monkeypatch.setenv("LAKE_URL", url)
        monkeypatch.setenv("LAKE_TOKEN", "tok")
        code, out, err = run_cli(capsys, "write", "a row through the remote CLI", "--source", "host", "--tag", "x")
        assert code == 0, err
        delta_id = out.strip()
        assert re.fullmatch(r"[0-9a-f]{12}", delta_id)
        code, out, _ = run_cli(capsys, "recall", "remote CLI row")
        assert code == 0 and delta_id in out
        code, out, _ = run_cli(capsys, "--json", "stats")
        assert code == 0 and json.loads(out)["rows"] == 1
        code, out, _ = run_cli(capsys, "context", "a row through the remote CLI")
        assert code == 0 and "a row through the remote CLI" in out
        code, out, _ = run_cli(capsys, "write", "a child row citing it", "--source", "host", "--from", delta_id)
        assert code == 0
        code, out, _ = run_cli(capsys, "lineage", out.strip())
        assert code == 0 and delta_id in out, "lineage walks the remote ancestry"
        code, _, err = run_cli(capsys, "consolidate", "container", "--think", "ollama:x@http://y")
        assert code == 2 and "--think and --embed are server-side on a remote lake" in err
        code, _, err = run_cli(capsys, "crystal", "--write-file")
        assert code == 2 and "--write-file needs the file; not available over HTTP" in err
        code, _, _ = run_cli(capsys, "crystal")
        assert code == 0, "plain crystal still works remotely"
        # --file beats LAKE_URL (§13.9 target order): a fresh local lake, not the served one
        code, out, _ = run_cli(capsys, "--json", "--file", str(tmp_path / "other.lake"), "stats")
        assert code == 0 and json.loads(out)["rows"] == 0


def test_cli_spool(capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path,
                   monkeypatch: pytest.MonkeyPatch) -> None:
    gone = dead_url()
    monkeypatch.setenv("LAKE_URL", gone)
    code, out, _ = run_cli(capsys, "spool")
    assert (code, out) == (0, "spool empty\n")
    for i in range(2):
        code, out, err = run_cli(capsys, "write", f"an offline CLI row {i}", "--source", "host")
        assert code == 0 and re.fullmatch(r"[0-9a-f]{12}\n", out), err
    code, out, _ = run_cli(capsys, "spool")
    assert code == 0 and re.fullmatch(r"2 spooled writes, oldest \d{4}-\d\d-\d\dT\d\d:\d\d\n", out)
    code, _, err = run_cli(capsys, "spool", "--flush")
    assert code == 1 and "lake unreachable" in err, "a failed flush prints the error and exits 1"
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file)) as url:
        monkeypatch.setenv("LAKE_URL", url)
        code, out, err = run_cli(capsys, "spool", "--flush")
        assert (code, out) == (0, "flushed: written 2, skipped 0, errors 0\n"), err
        code, out, _ = run_cli(capsys, "spool", "--flush")
        assert (code, out) == (0, "spool empty\n")
        with Lake(file, readonly=True) as lk:
            assert lk.stats()["rows"] == 2
    monkeypatch.delenv("LAKE_URL")
    monkeypatch.setenv("LAKE_FILE", str(file))
    code, _, err = run_cli(capsys, "spool")
    assert code == 2 and "lake spool needs a remote lake (set LAKE to its URL, or --lake URL)" in err


def test_env_file(capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path,
                  monkeypatch: pytest.MonkeyPatch) -> None:
    lake_dir = home / ".lake"
    lake_dir.mkdir()
    monkeypatch.delenv("LAKE_ENV_FILE", raising=False)  # this test drives the default ~/.lake/env path (HOME is isolated)
    (lake_dir / "env").write_text(
        "# a comment line\n"
        "\n"
        "export LAKE_TOKEN='first'\n"
        'LAKE_TOKEN="tok"\n'
        "BAD LINE without an equals\n"
        "lower_case=ignored\n"
        "1BAD=ignored too\n"
        "LAKE_EMBED=cmd:echo x\n",
        encoding="utf-8",
    )
    assert read_env_file() == {"LAKE_TOKEN": "tok", "LAKE_EMBED": "cmd:echo x"}, \
        "quotes stripped, export stripped, later duplicate wins, malformed lines ignored"
    assert setting("LAKE_TOKEN") == "tok"
    monkeypatch.setenv("LAKE_TOKEN", "from-process")
    assert setting("LAKE_TOKEN") == "from-process", "the process environment wins over the file"
    monkeypatch.delenv("LAKE_TOKEN")
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file), env_extra={"LAKE_TOKEN": "tok"}) as url:
        with (lake_dir / "env").open("a", encoding="utf-8") as fh:
            fh.write(f"LAKE_URL={url}\n")
        code, out, _ = run_cli(capsys, "--json", "stats")  # LAKE_URL and LAKE_TOKEN from ~/.lake/env
        assert code == 0 and json.loads(out)["rows"] == 0
        monkeypatch.setenv("LAKE_URL", dead_url())
        code, _, err = run_cli(capsys, "stats")
        assert code == 1 and "lake unreachable" in err, "process env wins over the env file"
        code, out, _ = run_cli(capsys, "--url", url, "--json", "stats")
        assert code == 0 and json.loads(out)["rows"] == 0, "--url wins over both"


def test_env_file_token_only_goes_to_its_own_url(capsys: pytest.CaptureFixture[str], tmp_path: Path, home: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """§13.9: the env file's LAKE_TOKEN is sent only to the env file's own LAKE_URL. A process LAKE_URL (or
    --url) naming another server gets no bearer; the same URL named again still gets it."""
    lake_dir = home / ".lake"
    lake_dir.mkdir()
    monkeypatch.delenv("LAKE_ENV_FILE", raising=False)
    file = tmp_path / "t.lake"
    with served(home, "--file", str(file), env_extra={"LAKE_TOKEN": "tok"}) as url:
        (lake_dir / "env").write_text(f"LAKE_URL={dead_url()}\nLAKE_TOKEN=tok\n", encoding="utf-8")
        monkeypatch.setenv("LAKE_URL", url)
        code, _, err = run_cli(capsys, "stats")
        assert code == 1 and "unauthorized" in err, err
        (lake_dir / "env").write_text(f"LAKE_URL={url}/\nLAKE_TOKEN=tok\n", encoding="utf-8")
        code, out, _ = run_cli(capsys, "--json", "stats")
        assert code == 0 and json.loads(out)["rows"] == 0


def test_mcp_server_env_file_token_only_for_its_own_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin MCP server resolves LAKE_TOKEN the way the CLI does (§13.9)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", Path(__file__).resolve().parent.parent / "plugin" / "mcp" / "server.py")
    assert spec and spec.loader
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    env_file = tmp_path / "env"
    env_file.write_text("LAKE_URL=http://127.0.0.1:9/\nLAKE_TOKEN=live\n", encoding="utf-8")
    monkeypatch.setenv("LAKE_ENV_FILE", str(env_file))
    assert server.resolve_target().token == "live"  # the env file's own target
    monkeypatch.setenv("LAKE_URL", "http://127.0.0.1:10")
    assert server.resolve_target().token is None  # another URL: no bearer
    monkeypatch.setenv("LAKE_URL", "http://127.0.0.1:9")
    assert server.resolve_target().token == "live"
    monkeypatch.setenv("LAKE_TOKEN", "mine")
    assert server.resolve_target().token == "mine"


def seed_link(file: Path) -> tuple[str, str, str]:
    """(old, new, container) ids: two host rows days old and a library container whose meta.supersedes links them."""
    from datetime import UTC, datetime, timedelta

    from lake import _db, _store

    now = datetime.now(UTC)
    with lake_pkg.Lake(file) as lk:
        old = lk.write("tidewater deploys to Fly.io region ord; tidewater Fly.io deploy", "host", timestamp=now - timedelta(days=40))
        new = lk.write("change of plan: tidewater moves off Fly.io to Hetzner", "host", timestamp=now - timedelta(days=10))
        with lk.tx() as conn:
            by = _db.new_id(conn)
            _store.insert_row(conn, delta_id=by, timestamp=lk.now_str(), content="Stack change\n\nMoved hosting.",
                              source="lake:container", kind="container", level=1, tags=[], derived_from=[new.id],
                              expires_at=None, media_hash=None, meta={"supersedes": [
                                  {"new": new.id, "old": old.id, "old_value": "Fly.io region ord", "new_value": "Hetzner"}]})
    return old.id, new.id, by


def test_remote_superseded_by(tmp_path: Path, home: Path) -> None:
    """§13.1: supersession receipts and the ranking they drive cross the wire; the spool is never involved; a row
    without the key (an older server, or an unlinked row) decodes to ()."""
    file = tmp_path / "r.lake"
    old, new, by = seed_link(file)
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        got = rl.get(old)
        assert got is not None and [(x.id, x.by, x.old_value, x.new_value) for x in got.superseded_by] == [
            (new, by, "Fly.io region ord", "Hetzner")]
        hits = rl.recall("tidewater Fly.io deploy")
        assert [h.delta.id for h in hits][:2] == [new, old] and hits[1].delta.superseded_by[0].id == new
        assert rl.get(new).superseded_by == ()  # type: ignore[union-attr]
        assert "superseded by " + new in rl.context("tidewater Fly.io deploy")
    assert not (tmp_path / "s.jsonl").exists() or (tmp_path / "s.jsonl").read_text(encoding="utf-8") == ""
    wire = json.loads(export_line(got))  # an older server's shape: no superseded_by key
    assert remote_delta(wire).superseded_by == ()
    wire["superseded_by"] = [{"id": new, "by": by, "old_value": "a", "new_value": "b"}]
    assert remote_delta(wire).superseded_by[0].old_value == "a"


def test_remote_refuted_by(tmp_path: Path, home: Path) -> None:
    """§13.1/§13.5: a refuted row's refuted_by receipts survive get()/recall() over the wire."""
    file = tmp_path / "r.lake"
    with served(home, "--file", str(file)) as url, RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
        d = rl.write("a contested claim", "robin")
        rl.engage(d.id, "refute", by="critic", note="that is wrong")
        got = rl.get(d.id)
        assert got is not None and len(got.refuted_by) == 1
        assert got.refuted_by[0].source == "critic" and got.refuted_by[0].note == "that is wrong"
        assert got.content == "a contested claim" and got.source == "robin"  # the row itself is intact
        hits = rl.recall("contested claim")
        assert hits and any(h.delta.refuted_by for h in hits)
        plain = rl.write("an unrefuted note", "robin")
        assert rl.get(plain.id).refuted_by == ()
