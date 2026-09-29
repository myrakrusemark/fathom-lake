"""§13.10 server tests: a real `lake serve` subprocess on an ephemeral loopback port, every §13.3
endpoint exercised with urllib, auth, startup refusals, §13.4 error mapping, §13.5 streams, the
§13.8 env file, and concurrent writes. Client-side (RemoteLake, spool) tests live with the client."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, FrozenClock, hash_embed, script, serve_env, served

from lake import Lake, RemoteLake, _db, _time
from lake._io import export_line

EXPORT_KEY_ORDER = ["id", "timestamp", "content", "source", "kind", "level", "tags", "derived_from",
                    "engagement", "expires_at", "media_hash", "meta"]
PROPOSE = '{"kind":"propose","title":"A stretch","summary":"Three rows about lake migrations.","from_ids":%s}'
NO_THINK_MSG = (
    "consolidate needs a server-side think: start lake serve with --think or set LAKE_THINK in ~/.lake/env on the server"
)


# --- harness (the serve fixture itself lives in conftest.py) ---------------------------------------


def run_serve(home: Path, *args: str, env_extra: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """A serve invocation expected to refuse: returns the completed process."""
    home.mkdir(exist_ok=True)
    cmd = [sys.executable, "-m", "lake.cli", "serve", "--port", "0", *args]
    return subprocess.run(cmd, cwd=str(REPO), env=serve_env(home, env_extra),
                          capture_output=True, text=True, timeout=30)


def call(url: str, method: str, path: str, body: object = None, *, token: str | None = None,
         raw: bytes | None = None) -> tuple[int, Any, dict[str, str]]:
    """One request; returns (status, parsed JSON body, headers). HTTP errors return, never raise."""
    data = raw if raw is not None else (json.dumps(body).encode("utf-8") if body is not None else None)
    req = urllib.request.Request(url + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"null"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null"), dict(exc.headers)


def get_bytes(url: str, path: str, *, token: str | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url + path)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read(), dict(resp.headers)


# --- the env file (§13.8) --------------------------------------------------------------------------


def test_serve_env_file(tmp_path: Path) -> None:
    """LAKE_FILE and LAKE_TOKEN come from ~/.lake/env; the process environment wins over the file."""
    home = tmp_path / "home"
    (home / ".lake").mkdir(parents=True)
    env_lake, other = tmp_path / "env.lake", tmp_path / "other.lake"
    (home / ".lake" / "env").write_text(f'LAKE_FILE="{env_lake}"\nexport LAKE_TOKEN=tok-from-file\n', encoding="utf-8")
    with served(home) as url:  # no --file: the env file supplies it, and its token gates requests
        assert call(url, "GET", "/v1/health")[0] == 200
        assert call(url, "GET", "/v1/stats")[0] == 401
        status, body, _ = call(url, "GET", "/v1/stats", token="tok-from-file")
        assert status == 200 and body["rows"] == 0
    assert env_lake.exists()
    with served(home, env_extra={"LAKE_FILE": str(other)}) as url:  # process env beats the env file
        status, body, _ = call(url, "POST", "/v1/write", {"content": "into other", "source": "host"},
                               token="tok-from-file")
        assert status == 200
    with Lake(other, readonly=True) as lk:
        assert lk.get(body["id"]) is not None
    with Lake(env_lake, readonly=True) as lk:
        assert lk.stats()["rows"] == 0


def test_serve_honours_lake_env_file(tmp_path: Path) -> None:
    """§13.8: serve reads the env file LAKE_ENV_FILE names, as the CLI does, never ~/.lake/env beside it."""
    home = tmp_path / "home"
    (home / ".lake").mkdir(parents=True)
    default_lake, scratch_lake, scratch_env = tmp_path / "default.lake", tmp_path / "scratch.lake", tmp_path / "s.env"
    (home / ".lake" / "env").write_text(f'LAKE_FILE="{default_lake}"\nLAKE_TOKEN=default-tok\n', encoding="utf-8")
    scratch_env.write_text(f'LAKE_FILE="{scratch_lake}"\n', encoding="utf-8")
    with served(home, env_extra={"LAKE_ENV_FILE": str(scratch_env)}) as url:
        status, body, _ = call(url, "POST", "/v1/write", {"content": "into scratch", "source": "host"})
        assert status == 200  # no token: the default env file's LAKE_TOKEN was never read
    assert not default_lake.exists()
    with Lake(scratch_lake, readonly=True) as lk:
        assert lk.get(body["id"]) is not None


# --- wire round trip (§13.1, §13.3) ----------------------------------------------------------------


def test_serve_health_write_get_wire_roundtrip(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        assert call(url, "GET", "/v1/health")[1] == {"ok": True, "rows": 0}
        _, parent, _ = call(url, "POST", "/v1/write", {"content": "parent row", "source": "host"})
        status, d, headers = call(url, "POST", "/v1/write", {
            "content": "child row with an astral char \U0001f30a", "source": "host",
            "tags": ["beta", "alpha"], "derived_from": [parent["id"]], "media": "0123456789abcdef",
            "meta": {"note": "wire", "n": 2.5}, "timestamp": "2026-09-03T12:00:00Z", "dedupe": False,
        })
        assert status == 200 and headers["Content-Type"] == "application/json"
        assert list(d) == EXPORT_KEY_ORDER, "a Delta crosses in the §4.8 export key order"
        assert d["content"].endswith("\U0001f30a") and d["timestamp"] == "2026-09-03T12:00:00.000Z"
        assert d["tags"] == ["beta", "alpha"] and d["derived_from"] == [parent["id"]]
        assert d["meta"] == {"note": "wire", "n": 2.5} and d["media_hash"] == "0123456789abcdef"
        assert d["kind"] is None and d["level"] == 0 and d["engagement"] is None and d["expires_at"] is None
        status, got, _ = call(url, "GET", "/v1/get/" + d["id"])
        assert status == 200 and got == d
        with Lake(file, readonly=True) as lk:  # field for field what a direct local get() holds
            local = lk.get(d["id"])
            assert local is not None and json.loads(export_line(local)) == got
        status, stats, _ = call(url, "GET", "/v1/stats")
        assert status == 200 and stats["rows"] == 2 and stats["schema_version"] == "1"
        assert call(url, "POST", "/v1/sweep", {})[1] == {"deleted": 0, "orphan_media": 0}


def test_serve_get_null_and_expired(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with Lake(file, clock=FrozenClock("2026-09-02T18:00:00.000Z")) as lk:  # expired for the real clock
        expired_id = lk.write("already gone", "host", expires="1h").id
    with served(tmp_path / "home", "--lake", str(file)) as url:
        assert call(url, "GET", "/v1/get/aaaaaaaaaaaa") == (200, None, {})[:2] + (call(url, "GET", "/v1/get/aaaaaaaaaaaa")[2],)
        assert call(url, "GET", "/v1/get/" + expired_id)[1] is None
        status, row, _ = call(url, "GET", f"/v1/get/{expired_id}?include_expired=1")
        assert status == 200 and row["id"] == expired_id and row["expires_at"] == "2026-09-02T19:00:00.000Z"
        assert call(url, "GET", f"/v1/get/{expired_id}?include_expired=0")[1] is None
        assert call(url, "GET", "/v1/crystal")[1] is None, "a crystal-less lake answers null"
        assert call(url, "POST", "/v1/sweep", {})[1]["deleted"] == 1


def test_serve_engage_valence(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        _, target, _ = call(url, "POST", "/v1/write", {"content": "the drift threshold decision", "source": "host"})
        status, e, _ = call(url, "POST", "/v1/engage",
                            {"delta_id": target["id"], "kind": "affirm", "by": "robin"})
        assert status == 200 and e["kind"] == "engagement"
        assert e["engagement"] == {"target_id": target["id"], "kind": "affirm", "by": "robin", "note": None}
        assert target["content"] in e["content"], "the §4.6 snapshot content crossed intact"
        status, res, _ = call(url, "POST", "/v1/recall", {"query": None, "limit": 20})
        assert status == 200 and res["warnings"] == []
        valences = {h["delta"]["id"]: h["valence"] for h in res["hits"]}
        assert valences[target["id"]] == pytest.approx(1.05)


def test_serve_refuted_by_wire(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        _, target, _ = call(url, "POST", "/v1/write", {"content": "the drift threshold decision", "source": "host"})
        _, e, _ = call(url, "POST", "/v1/engage",
                       {"delta_id": target["id"], "kind": "refute", "by": "robin", "note": "no"})
        status, got, _ = call(url, "GET", "/v1/get/" + target["id"])
        assert status == 200 and list(got)[-1] == "refuted_by", "receipts ride after the export key order"
        assert list(got)[:-1] == EXPORT_KEY_ORDER
        assert got["refuted_by"] == [{"id": e["id"], "source": "robin", "timestamp": e["timestamp"], "note": "no"}]
        status, res, _ = call(url, "POST", "/v1/recall", {"query": None, "limit": 20})
        hit = next(h for h in res["hits"] if h["delta"]["id"] == target["id"])
        assert hit["valence"] == pytest.approx(0.50) and hit["delta"]["refuted_by"][0]["id"] == e["id"]
        assert list(hit["delta"])[-1] == "refuted_by"


def test_serve_superseded_by_wire(tmp_path: Path) -> None:
    """§13.1: `superseded_by` rides after `meta` (and after `refuted_by` / `rests_on_refuted` when present) only on a
    superseded row; the export stream never carries it."""
    from test_remote import seed_link

    file = tmp_path / "t.lake"
    old, new, by = seed_link(file)
    with served(tmp_path / "home", "--lake", str(file)) as url:
        status, got, _ = call(url, "GET", "/v1/get/" + old)
        assert status == 200 and list(got)[:-1] == EXPORT_KEY_ORDER and list(got)[-1] == "superseded_by"
        assert got["superseded_by"] == [{"id": new, "by": by, "old_value": "Fly.io region ord", "new_value": "Hetzner"}]
        status, fresh, _ = call(url, "GET", "/v1/get/" + new)
        assert status == 200 and "superseded_by" not in fresh


# --- recall, plan, context (§13.3) -----------------------------------------------------------------


def seed_plan_rows(file: Path) -> list[str]:
    rows = [
        ("Discussed the postgres migration plan for Thursday", "claude-code", ["migration"], "2026-09-01T14:00:00Z"),
        ("We agreed to run the migration at 09:00 Thursday", "claude-code", ["migration"], "2026-09-01T14:00:40Z"),
        ("heartbeat", "agent-heartbeat", [], "2026-09-01T14:01:00Z"),
        ("heartbeat", "agent-heartbeat", [], "2026-09-01T14:01:05Z"),
        ("heartbeat", "agent-heartbeat", [], "2026-09-01T14:01:10Z"),
        ("Backup finishes around 08:30 so 09:00 gives margin", "claude-code", ["backup"], "2026-09-01T14:02:00Z"),
    ]
    with Lake(file, automation=["source:agent-heartbeat"]) as lk:
        return [lk.write(c, s, tags=t, timestamp=ts, dedupe=False).id for c, s, t, ts in rows]


def test_serve_recall_plan_timeline(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    seed_plan_rows(file)
    steps = [{"id": "w", "filter": {"tags": ["migration"]}}, {"id": "tl", "timeline": "w"},
             {"id": "agg", "aggregate": "w", "group_by": "tag"}]
    with served(tmp_path / "home", "--lake", str(file), "--automation", "source:agent-heartbeat") as url:
        status, res, _ = call(url, "POST", "/v1/plan", {"steps": steps})
        assert status == 200
        with Lake(file, readonly=True, automation=["source:agent-heartbeat"]) as lk:
            local = lk.plan(steps)
        assert [sid for sid, _ in res["steps"]] == ["w", "tl", "agg"], "steps cross as pairs in plan order"
        by_id = dict(res["steps"])
        w_hits = by_id["w"]["hits"]
        assert [h["delta"]["id"] for h in w_hits] == [h.delta.id for h in local.steps["w"].hits or []]
        for got, want in zip(w_hits, local.steps["w"].hits or [], strict=True):
            assert got["score"] == pytest.approx(want.score, abs=1e-6)
        assert by_id["w"]["buckets"] is None and by_id["w"]["timelines"] is None
        strips = by_id["tl"]["timelines"]
        local_strip = (local.steps["tl"].timelines or [])[0]
        assert strips[0]["anchor_ids"] == local_strip.anchor_ids
        collapsed = [r for r in strips[0]["rows"] if "count" in r]
        assert collapsed and all("delta" not in r for r in collapsed), "a CollapsedRun carries count, never delta"
        assert collapsed[0] == {"source": "agent-heartbeat", "count": 3,
                                "t_start": "2026-09-01T14:01:00.000Z", "t_end": "2026-09-01T14:01:10.000Z"}
        assert all("delta" in r and "is_anchor" in r for r in strips[0]["rows"] if "count" not in r)
        buckets = by_id["agg"]["buckets"]
        assert {b["key"]: b["count"] for b in buckets} == {"migration": 2}
        # recall(plan=...) over the wire: the last step's hits
        status, res, _ = call(url, "POST", "/v1/recall", {"plan": steps[:1]})
        assert status == 200 and [h["delta"]["id"] for h in res["hits"]] == [h["delta"]["id"] for h in w_hits]


def test_serve_context_blocks(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    seed_plan_rows(file)
    body = {"query": "migration plan for thursday", "budget": 4000, "limit": 10}
    with served(tmp_path / "home", "--lake", str(file), "--automation", "source:agent-heartbeat") as url:
        status, res, _ = call(url, "POST", "/v1/context", body)
        assert status == 200
        with Lake(file, readonly=True, automation=["source:agent-heartbeat"]) as lk:
            local = lk.context_blocks("migration plan for thursday", budget=4000, limit=10)
        assert res["rendered"] == local.rendered
        assert [h["delta"]["id"] for h in res["hits"]] == [h.delta.id for h in local.hits]
        assert [c["id"] for c in res["containers"]] == [c.id for c in local.containers]
        assert len(res["strips"]) == len(local.strips) and res["omitted_strips"] == local.omitted_strips
        assert res["crystal"] is None and res["warnings"] == local.warnings


def test_serve_system_prompt(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        seed = lk.write("seed row for the crystal and moods", "host")
        lk.write("I am the identity crystal.", "host", kind="crystal", derived_from=[seed.id])
        for i, cw in enumerate(("CW_ONE", "CW_TWO", "CW_THREE")):
            lk.write(json.dumps({"state": "x", "headline": f"h{i}", "subtext": f"s{i}", "carrier_wave": cw}),
                     "host", kind="mood", derived_from=[seed.id], tags=["feeling:x"],
                     timestamp=f"2026-09-02T17:5{i}:00Z", dedupe=False)
    with served(tmp_path / "home", "--lake", str(file)) as url:
        status, res, _ = call(url, "POST", "/v1/system-prompt", {"moods": 3, "budget": 4000})
        assert status == 200
        with Lake(file, readonly=True) as lk:
            local = lk.system_prompt(moods=3, budget=4000)
        assert res["prompt"] == local
        assert "Recent moods:" in local and "CW_THREE" in local and local.startswith("Identity crystal")
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            assert rl.system_prompt(moods=3, budget=4000) == local


def test_serve_exclude_sources_become_automation(tmp_path: Path) -> None:
    """§4.3, §13.2: the deprecated --exclude-source (and LAKE_EXCLUDE_SOURCES) is a source: automation rule, read once
    at startup: the rows stay searchable over every endpoint and are left out of consolidation (due is false)."""
    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        start = datetime.now(UTC) - timedelta(minutes=40)
        rows = [lk.write(f"noisy-bot migration row {i} long enough to escape the noise rules", "noisy-bot",
                         timestamp=start + timedelta(seconds=10 * i)).id for i in range(4)]
    with served(tmp_path / "home", "--lake", str(file)) as url:
        assert call(url, "GET", "/v1/due/container")[1] == {"due": True}
    for args, env in ((("--exclude-source", "noisy-bot"), {}), ((), {"LAKE_EXCLUDE_SOURCES": "noisy-bot"})):
        with served(tmp_path / "home", "--lake", str(file), *args, env_extra=env) as url:
            _, res, _ = call(url, "POST", "/v1/recall", {"query": None})
            assert sorted(h["delta"]["id"] for h in res["hits"]) == sorted(rows), "searchable, not hidden"
            _, res, _ = call(url, "POST", "/v1/context", {"query": "noisy-bot migration row noise rules"})
            assert res["hits"], "context still finds them"
            assert call(url, "GET", "/v1/due/container")[1] == {"due": False}, "never consolidated"


# --- lineage and cited-by (§13.3) ------------------------------------------------------------------


def test_serve_lineage_cited_by(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        _, a, _ = call(url, "POST", "/v1/write", {"content": "grandparent", "source": "host"})
        _, b, _ = call(url, "POST", "/v1/write", {"content": "parent", "source": "host",
                                                  "derived_from": [a["id"]]})
        _, c, _ = call(url, "POST", "/v1/write", {"content": "child", "source": "host",
                                                  "derived_from": [b["id"]]})
        status, lin, _ = call(url, "GET", "/v1/lineage/" + c["id"])
        assert status == 200 and [r["id"] for r in lin["rows"]] == [b["id"], a["id"]] and lin["dangling"] == []
        status, lin, _ = call(url, "GET", f"/v1/lineage/{c['id']}?depth=1")
        assert status == 200 and [r["id"] for r in lin["rows"]] == [b["id"]]
        status, cited, _ = call(url, "GET", "/v1/cited-by/" + a["id"])
        assert status == 200 and [r["id"] for r in cited["rows"]] == [b["id"]]
        status, cited, _ = call(url, "GET", f"/v1/cited-by/{a['id']}?depth=2")
        assert status == 200 and {r["id"] for r in cited["rows"]} == {b["id"], c["id"]}
        status, err, _ = call(url, "GET", "/v1/lineage/aaaaaaaaaaaa")
        assert status == 404 and err["error"] == "NotFoundError"
        status, err, _ = call(url, "GET", "/v1/cited-by/aaaaaaaaaaaa")
        assert status == 404 and err["error"] == "NotFoundError"


# --- consolidate (§13.2, §13.3) --------------------------------------------------------------------


def test_serve_consolidate(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        ids = [lk.write(f"row {i} about the lake migration", "host", timestamp="45 minutes ago").id for i in range(3)]
    log = tmp_path / "think.log"
    think = script(tmp_path, "think.sh",
                   f'echo "json=$LAKE_JSON" >> "{log}"; cat >> "{log}"; echo \'{PROPOSE % json.dumps(ids)}\'')
    with served(tmp_path / "home", "--lake", str(file), "--think", f"cmd:{think}", "--model-name", "fake-1") as url:
        assert call(url, "GET", "/v1/due/container")[1] == {"due": True}
        status, res, _ = call(url, "POST", "/v1/consolidate", {"kind": "container"})
        assert status == 200
        delta, run = res["delta"], res["run"]
        assert delta is not None and delta["kind"] == "container" and delta["derived_from"] == ids
        assert delta["meta"]["model"] == "fake-1"
        assert run["kind"] == "container" and run["think_calls"] == 1 and run["skipped"] == 0
        assert [w["id"] for w in run["written"]] == [delta["id"]]
        assert run["window"] is None or (isinstance(run["window"], list) and len(run["window"]) == 2)
        assert log.exists() and all(i in log.read_text() for i in ids), "the fake think ran on the server"
        assert call(url, "GET", "/v1/due/container")[1] == {"due": False}
        status, err, _ = call(url, "POST", "/v1/consolidate", {"kind": "container", "opts": {"bogus": 1}})
        assert status == 400 and err["error"] == "ValueError"
        with Lake(file) as lk:  # a held lease answers 409 (§13.4)
            with lk.tx() as conn:
                _db.meta_set(conn, "consolidate_lease:mood", "9999-01-01T00:00:00.000Z")
        status, err, _ = call(url, "POST", "/v1/consolidate", {"kind": "mood"})
        assert status == 409 and err["error"] == "ConsolidateError" and "already running" in err["message"]


def test_serve_consolidate_no_think(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        status, err, _ = call(url, "POST", "/v1/consolidate", {"kind": "mood"})
        assert status == 409
        assert err == {"error": "ConsolidateError", "message": NO_THINK_MSG}


def test_serve_refused_cmd_claude_think_still_serves(tmp_path: Path) -> None:
    """§13.2 step 4: the refused `cmd:claude …` think spec is logged and the server starts with no think, so reads
    and writes keep working and only /v1/consolidate fails; any other bad spec is still a usage error (exit 2)."""
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file), "--think", "cmd:timeout 60 claude -p") as url:
        assert call(url, "POST", "/v1/write", {"content": "still served", "source": "host"})[0] == 200
        status, err, _ = call(url, "POST", "/v1/consolidate", {"kind": "mood"})
        assert status == 409 and err == {"error": "ConsolidateError", "message": NO_THINK_MSG}
    done = run_serve(tmp_path / "home", "--lake", str(file), "--think", "nonsense:x")
    assert done.returncode == 2 and "unknown --think spec" in done.stderr


# --- export and import (§13.5) ---------------------------------------------------------------------


def test_serve_export_import(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with Lake(file, embed=lambda texts: hash_embed(texts)) as lk:
        a = lk.write("the first exported row", "host", tags=["x"], dedupe=False)
        lk.write("the second exported row", "host", meta={"n": 1}, dedupe=False)
        lk.engage(a.id, "affirm", by="robin")
    with served(tmp_path / "home", "--lake", str(file)) as url:
        for qs, vectors in (("", False), ("?vectors=1", True)):
            status, body, headers = get_bytes(url, "/v1/export" + qs)
            assert status == 200 and headers["Content-Type"] == "application/x-ndjson"
            want = tmp_path / "local.jsonl"
            with Lake(file, readonly=True) as lk:
                n = lk.export(want, vectors=vectors)
            assert body == want.read_bytes() and body.count(b"\n") == n, "byte for byte what export() writes"
        lines = (
            '{"id":"aaaaaaaaaaa1","timestamp":"2026-09-01T10:00:00.000Z","content":"imported one","source":"importer"}\n'
            '{"id":"aaaaaaaaaaa2","timestamp":"2026-09-01T10:00:01.000Z","content":"imported two","source":"importer"}\n'
            "not json at all\n"
        ).encode("utf-8")
        status, counts, _ = call(url, "POST", "/v1/import", raw=lines)
        assert status == 200 and counts == {"written": 2, "skipped": 0, "errors": 1}
        status, counts, _ = call(url, "POST", "/v1/import", raw=lines)
        assert status == 200 and counts == {"written": 0, "skipped": 2, "errors": 1}, "a double import skips by id"
        assert call(url, "GET", "/v1/get/aaaaaaaaaaa1")[1]["content"] == "imported one"
        assert call(url, "GET", "/v1/health")[1]["rows"] == 5


# --- auth and startup refusals (§13.2) -------------------------------------------------------------


def test_serve_health_and_auth(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file), env_extra={"LAKE_TOKEN": "s3cret"}) as url:
        assert call(url, "GET", "/v1/health")[1] == {"ok": True, "rows": 0}, "health needs no auth"
        for method, path, body in (("GET", "/v1/stats", None), ("POST", "/v1/write",
                                                               {"content": "x", "source": "host"})):
            status, err, headers = call(url, method, path, body)
            assert status == 401 and err == {"error": "LakeError", "message": "unauthorized"}
            assert headers["WWW-Authenticate"] == "Bearer"
        assert call(url, "GET", "/v1/stats", token="wrong")[0] == 401
        status, d, _ = call(url, "POST", "/v1/write", {"content": "x", "source": "host"}, token="s3cret")
        assert status == 200 and call(url, "GET", "/v1/get/" + d["id"], token="s3cret")[1] == d


def test_serve_refuses_public_bind(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    proc = run_serve(tmp_path / "home", "--lake", str(file), "--bind", "127.0.0.2")
    assert proc.returncode == 1 and not file.exists()
    assert proc.stderr.strip() == ("lake serve: refusing to bind 127.0.0.2 without a token — "
                                   "set LAKE_TOKEN or write ~/.lake/token (0600)")
    with served(tmp_path / "home", "--lake", str(file), "--bind", "127.0.0.2",
                env_extra={"LAKE_TOKEN": "tok"}) as url:
        assert url.startswith("http://127.0.0.2:") and call(url, "GET", "/v1/health")[0] == 200
    with served(tmp_path / "home", "--lake", str(file)) as url:  # loopback without a token is fine
        assert call(url, "GET", "/v1/health")[0] == 200


def test_serve_token_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".lake").mkdir(parents=True)
    token_path = home / ".lake" / "token"
    token_path.write_text("tok-from-file\n", encoding="utf-8")
    token_path.chmod(0o644)
    file = tmp_path / "t.lake"
    proc = run_serve(home, "--lake", str(file))
    assert proc.returncode == 1
    assert proc.stderr.strip() == "lake serve: ~/.lake/token is readable by others — chmod 600 ~/.lake/token"
    token_path.chmod(0o600)
    with served(home, "--lake", str(file), "--bind", "127.0.0.2") as url:  # the file's token allows the bind
        assert call(url, "GET", "/v1/stats")[0] == 401
        assert call(url, "GET", "/v1/stats", token="tok-from-file")[0] == 200, "the token is stripped"


def test_serve_missing_file_usage_error(tmp_path: Path) -> None:
    proc = run_serve(tmp_path / "home")
    assert proc.returncode == 2 and "no lake" in proc.stderr
    proc = run_serve(tmp_path / "home", "--lake", "http://127.0.0.1:9")  # §13.8: serve hosts a file, never a URL
    assert proc.returncode == 2 and "serve hosts a file" in proc.stderr


# --- error mapping (§13.4) -------------------------------------------------------------------------


def test_serve_error_mapping(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        cases: list[tuple[int, str, str, object]] = [
            (400, "ValueError", "/v1/recall", {"limit": 0}),
            (400, "ValueError", "/v1/recall", {"bogus": 1}),
            (400, "ValueError", "/v1/write", {"content": "x"}),
            (400, "PlanError", "/v1/plan", {"steps": [{"id": "a", "nonsense": 1}]}),
            (400, "ClosedLoopError", "/v1/write", {"content": "x", "source": "lake:x"}),
            (404, "NotFoundError", "/v1/engage", {"delta_id": "aaaaaaaaaaaa", "kind": "affirm"}),
            (400, "ValueError", "/v1/sweep", {"extra": 1}),
            (500, "LakeError", "/v1/embed-missing", {}),
        ]
        for want_status, want_error, path, body in cases:
            status, err, _ = call(url, "POST", path, body)
            assert (status, err["error"]) == (want_status, want_error), (path, body, err)
        status, err, _ = call(url, "POST", "/v1/recall", raw=b"[1, 2]")
        assert (status, err["error"]) == (400, "ValueError") and "invalid JSON body" in err["message"]
        status, err, _ = call(url, "POST", "/v1/recall", raw=b"{nope")
        assert (status, err["error"]) == (400, "ValueError") and "invalid JSON body" in err["message"]
        status, err, _ = call(url, "GET", "/v1/get/aaaaaaaaaaaa?include_expired=maybe")
        assert (status, err["message"]) == (400, "bad query parameter include_expired=maybe")
        status, err, _ = call(url, "GET", "/v1/stats?x=1")
        assert (status, err["message"]) == (400, "bad query parameter x=1")
        status, err, _ = call(url, "GET", "/v1/nope")
        assert (status, err) == (404, {"error": "LakeError", "message": "unknown endpoint: GET /v1/nope"})
        status, err, _ = call(url, "POST", "/v1/stats", {})
        assert (status, err["message"]) == (404, "unknown endpoint: POST /v1/stats")
        host, _, port = url.removeprefix("http://").partition(":")
        with socket.create_connection((host, int(port)), timeout=10) as sock:  # no Content-Length
            sock.sendall(b"POST /v1/write HTTP/1.0\r\nHost: t\r\n\r\n")
            resp = b""
            while chunk := sock.recv(4096):
                resp += chunk
        assert b" 400 " in resp.split(b"\r\n", 1)[0] and b"Content-Length required" in resp
        with socket.create_connection((host, int(port)), timeout=10) as sock:  # a lying negative length
            sock.sendall(b"POST /v1/write HTTP/1.0\r\nHost: t\r\nContent-Length: -1\r\n\r\n{}")
            resp = b""  # without the guard, read(-1) blocks to EOF and this recv times out
            while chunk := sock.recv(4096):
                resp += chunk
        assert b" 400 " in resp.split(b"\r\n", 1)[0] and b"invalid Content-Length" in resp


def test_serve_embed_error_and_embed_missing(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file), "--embed", "cmd:false") as url:
        status, err, _ = call(url, "POST", "/v1/write", {"content": "vector fails", "source": "host"})
        assert status == 500 and err["error"] == "EmbedError"
        assert call(url, "GET", "/v1/health")[1]["rows"] == 1, "the §4.4 row commits without its vector"
        status, d, _ = call(url, "POST", "/v1/write", {"content": "skip the vector", "source": "host",
                                                       "embed": False})
        assert status == 200 and d["content"] == "skip the vector"
    emb = script(tmp_path, "emb.sh",
                 f'exec "{sys.executable}" -c \'import json,sys; t=json.load(sys.stdin); '
                 "print(json.dumps([[float(len(x)), 1.0] for x in t]))'")
    with served(tmp_path / "home", "--lake", str(file), "--embed", f"cmd:{emb}") as url:
        status, res, _ = call(url, "POST", "/v1/embed-missing", {"batch_size": 8})
        assert status == 200 and res == {"stored": 2}
        with Lake(file, readonly=True) as lk:
            assert lk.stats()["vectors"] == 2


# --- concurrency (§9, §13.2) -----------------------------------------------------------------------


def test_serve_concurrent_writes(tmp_path: Path) -> None:
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        results: list[tuple[int, Any]] = []
        lock = threading.Lock()

        def writer(n: int) -> None:
            for i in range(8):
                status, body, _ = call(url, "POST", "/v1/write",
                                       {"content": f"concurrent row {n}-{i}", "source": f"w{n}"})
                with lock:
                    results.append((status, body))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert [s for s, _ in results] == [200] * 48, [b for s, b in results if s != 200][:3]
        assert len({b["id"] for _, b in results}) == 48
        assert call(url, "GET", "/v1/health")[1]["rows"] == 48


# --- digest (§13.2 --digest, §13.3 /v1/digest) ----------------------------------------------------------------


def test_serve_digest_endpoint(tmp_path: Path) -> None:
    """POST /v1/digest: 409 with the consolidate message without a server think (unless dry_run), unknown fields
    refuse, and with a think a DigestRun comes back."""
    file = tmp_path / "t.lake"
    with served(tmp_path / "home", "--lake", str(file)) as url:
        status, err, _ = call(url, "POST", "/v1/digest", {})
        assert status == 409 and err == {"error": "ConsolidateError", "message": NO_THINK_MSG}
        status, res, _ = call(url, "POST", "/v1/digest", {"dry_run": True})
        assert status == 200 and res["run"]["think_calls"] == 0
        assert call(url, "POST", "/v1/digest", {"bogus": 1})[0] == 400
    think = script(tmp_path, "think.sh", "echo '{\"kind\":\"skip\"}'")
    with served(tmp_path / "home", "--lake", str(file), "--think", f"cmd:{think}") as url:
        status, res, _ = call(url, "POST", "/v1/digest", {"max_units": 2, "backfill_max": 0})
        assert status == 200 and [s["step"] for s in res["run"]["steps"]] == ["container", "mood", "crystal"]
        with Lake(file) as lk:
            assert lk.stats()["digest_last"]["calls"] == 0
            with lk.tx() as conn:
                _db.meta_set(conn, "consolidate_lease:digest", "9999-01-01T00:00:00.000Z")
        status, err, _ = call(url, "POST", "/v1/digest", {})
        assert status == 409 and "already running" in err["message"]


def test_serve_digest_schedule_parse_and_wait() -> None:
    """--digest off|nightly|HH:MM|every:<duration>; the wait until the next slot, or 0 when a slot was missed
    (the old timer's Persistent=true), daily slots in the server's local time."""
    from lake import serve

    assert serve.schedule("off") is None and serve.schedule("nightly") == (1, 0) and serve.schedule("03:15") == (3, 15)
    assert serve.schedule("every:6h") == 21600.0
    for bad in ("sometimes", "25:00", "every:0s", "every:soon"):
        with pytest.raises(ValueError):
            serve.schedule(bad)
    now = datetime(2026, 9, 27, 0, 30).astimezone()
    assert serve.wait_seconds((1, 0), now, None) == 0, "never digested: run at once"
    assert serve.wait_seconds((1, 0), now, now - timedelta(hours=1)) == 1800, "ran after the last slot: wait for 01:00"
    assert serve.wait_seconds((1, 0), now, now - timedelta(days=2)) == 0, "missed a slot: run at once"
    later = datetime(2026, 9, 27, 2, 0).astimezone()
    assert serve.wait_seconds((1, 0), later, later - timedelta(hours=1, minutes=30)) == 0, "the 01:00 slot was missed"
    assert serve.wait_seconds((1, 0), later, later - timedelta(minutes=30)) == 23 * 3600
    assert serve.wait_seconds(21600.0, now, now - timedelta(hours=2)) == 4 * 3600
    assert serve.wait_seconds(21600.0, now, now - timedelta(hours=7)) == 0


def test_serve_digest_loop_shares_the_server_think(tmp_path: Path) -> None:
    """The schedule thread's loop, driven by a fake clock and a fake wait (no sleeping): it digests at once when a
    slot is owed, with the server's own think, logs one line per run, and waits for the next slot; a failing
    digest is logged and never retried in a tight loop."""
    import io

    from lake import serve
    from test_digestion import Oracle, session

    clock = FrozenClock()
    file = tmp_path / "t.lake"
    with Lake(file, clock=clock) as lk:
        session(lk, "a", ["plan the raised garden beds for spring", "Two beds, cedar, 4x8.",
                         "order the cedar boards from the mill", "Ordered twelve boards."], clock() - timedelta(hours=5))
    think = Oracle()
    srv = serve.LakeServer(("127.0.0.1", 0), file=str(file), token=None, think=think, embed=None, model_name="m",
                           automation=(), clock=clock)
    waits: list[float] = []

    def wait(seconds: float) -> bool:  # stands in for threading.Event.wait: moves the clock, never sleeps
        waits.append(seconds)
        clock.advance(seconds)
        return len(waits) >= 3

    log = io.StringIO()
    try:
        serve.digest_loop(srv, 21600.0, {"backfill_max": 0}, wait=wait, log=log)
    finally:
        srv.server_close()
    lines = log.getvalue().splitlines()
    assert waits == [21600.0] * 3 and len(lines) == 3 and all("digest done" in x for x in lines)
    assert "1 container" in lines[0] and "0 calls" in lines[1]
    assert think.calls and all(c[0] for c in think.calls), "the server's think ran the scheduled digest"
    with Lake(file) as lk:
        assert lk.stats()["digest_last"] is not None and lk.crystal() is not None
    srv = serve.LakeServer(("127.0.0.1", 0), file=str(file), token=None, think=None, embed=None, model_name=None,
                           automation=(), clock=clock)
    waits.clear()
    log = io.StringIO()
    try:
        serve.digest_loop(srv, 21600.0, {}, wait=wait, log=log)
    finally:
        srv.server_close()
    assert waits == [21600.0] * 3 and all("digest failed" in x for x in log.getvalue().splitlines())


def test_serve_digest_loop_survives_a_failed_check_and_a_held_lease(tmp_path: Path) -> None:
    """The thread never dies: an exception in the pre-check (the file locked) is logged and retried after RETRY;
    a digest lease left by a killed run is retried until it expires instead of skipping the slot to tomorrow."""
    import io
    import sqlite3

    from lake import serve
    from test_digestion import Oracle, session

    clock = FrozenClock()
    file = tmp_path / "t.lake"
    with Lake(file, clock=clock) as lk:
        session(lk, "a", ["plan the raised garden beds for spring", "Two beds, cedar, 4x8.",
                         "order the cedar boards from the mill", "Ordered twelve boards."], clock() - timedelta(hours=5))
        with lk.tx() as conn:  # a killed digest's lease: one more hour
            _db.meta_set(conn, "consolidate_lease:digest", _time.dt_to_ts(clock() + timedelta(minutes=50)))
    srv = serve.LakeServer(("127.0.0.1", 0), file=str(file), token=None, think=Oracle(), embed=None, model_name="m",
                           automation=(), clock=clock)
    real_open, fails = srv.open, [sqlite3.OperationalError("database is locked")]

    def flaky_open(*, readonly: bool) -> Lake:
        if fails:
            raise fails.pop()
        return real_open(readonly=readonly)

    srv.open = flaky_open  # type: ignore[method-assign]
    waits: list[float] = []

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        clock.advance(seconds)
        return "digest done" in log.getvalue()

    log = io.StringIO()
    try:
        serve.digest_loop(srv, (1, 0), {"backfill_max": 0}, wait=wait, log=log)
    finally:
        srv.server_close()
    lines = log.getvalue().splitlines()
    assert "OperationalError: database is locked" in lines[0] and waits[0] == serve.RETRY
    assert all("LeaseHeld" in x for x in lines[1:-1]) and len(lines) - 2 == 9 and "digest done" in lines[-1]
    assert waits[1:-1] == [serve.RETRY] * 9 and waits[-1] > serve.RETRY, "the lease ran out at +50 min; then the next slot"


def test_serve_digest_flag_starts_the_thread(tmp_path: Path) -> None:
    """`lake serve --digest every:1h` announces the schedule and digests at once (no digest yet); --digest without
    a think falls back to the default model spec, and a bad schedule is a usage error."""
    file = tmp_path / "t.lake"
    think = script(tmp_path, "think.sh", "echo '{\"kind\":\"skip\"}'")
    home = tmp_path / "home"
    home.mkdir()
    cmd = [sys.executable, "-m", "lake.cli", "serve", "--port", "0", "--lake", str(file), "--think", f"cmd:{think}",
           "--digest", "every:1h"]
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=serve_env(home), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    assert proc.stderr is not None
    seen = []
    for line in proc.stderr:
        seen.append(line)
        if "digest done" in line or "digest failed" in line:
            break
    proc.terminate()
    assert proc.wait(timeout=10) == 0
    text = "".join(seen)
    assert "digest every 1h" in text and "digest done" in text, text
    done = run_serve(home, "--lake", str(file), "--digest", "whenever")
    assert done.returncode == 2 and "--digest" in done.stderr
