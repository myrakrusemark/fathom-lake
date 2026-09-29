"""§8 CLI tests: every subcommand through `lake.cli.main([...])` against a temporary file, the --json
forms, exit codes 0/1/2, LAKE_FILE, the cmd:/claude think adapters and the cmd:/ollama embed adapters
through tiny scripts and a local http.server stub (no network)."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import lake as lake_pkg
from lake import Lake, LakeError, adapters, cli

HIT_LINE = re.compile(r"^\d+\.\d{3}  [0-9a-f]{12}  \d{4}-\d\d-\d\dT\d\d:\d\d  \S+  .+$")
PROPOSE = '{"kind":"propose","title":"A stretch","summary":"Three rows about lake migrations.","from_ids":%s}'


def run(capsys: pytest.CaptureFixture[str], *argv: str, stdin: str | None = None) -> tuple[int, str, str]:
    """main(argv) -> (exit code, stdout, stderr); `stdin` feeds the process's standard input."""
    if stdin is not None:
        real, cli.sys.stdin = cli.sys.stdin, io.StringIO(stdin)
    try:
        code = cli.main(list(argv))
    finally:
        if stdin is not None:
            cli.sys.stdin = real
    out, err = capsys.readouterr()
    return code, out, err


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@contextmanager
def http_stub(handle: Callable[[str, dict[str, Any]], object]) -> Iterator[tuple[str, list[tuple[str, dict[str, Any]]]]]:
    """A local server: `handle(path, body)` returns the JSON reply (None -> 500); yields (url, calls)."""
    calls: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
            calls.append((self.path, body))
            reply = handle(self.path, body)
            data = json.dumps(reply).encode("utf-8")
            self.send_response(200 if reply is not None else 500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}", calls
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def lake_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A fresh lake path exported as LAKE (§13.8), so commands need no --lake."""
    path = str(tmp_path / "t.lake")
    monkeypatch.setenv("LAKE", path)
    monkeypatch.delenv("LAKE_EXCLUDE_SOURCES", raising=False)
    monkeypatch.delenv("LAKE_COLLAPSE_SOURCES", raising=False)
    return path


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Records the keyword arguments of every Lake the CLI opens through lake.open (readonly, model_name, automation)."""
    seen: list[dict[str, Any]] = []
    real = lake_pkg.Lake

    def recording(path: str, **kw: Any) -> Lake:
        seen.append(dict(kw))
        return real(path, **kw)

    monkeypatch.setattr(lake_pkg, "Lake", recording)
    return seen


def write(capsys: pytest.CaptureFixture[str], content: str, source: str = "host", *extra: str) -> str:
    code, out, err = run(capsys, "write", content, "--source", source, *extra)
    assert code == 0, err
    return out.strip()


# --- write, recall, context, engage ---------------------------------------------------------------


def test_write_prints_id_and_reads_stdin(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    a = write(capsys, "the migration plan for the lake file", "host", "--tag", "x", "--tag", "y")
    assert re.fullmatch(r"[0-9a-f]{12}", a)
    code, out, _ = run(capsys, "write", "-", "--source", "host", stdin="a row read from stdin")
    assert code == 0 and re.fullmatch(r"[0-9a-f]{12}\n", out)
    with Lake(lake_file, readonly=True) as lk:
        d = lk.get(a)
        assert d is not None and d.tags == ["x", "y"]
        assert lk.get(out.strip()).content == "a row read from stdin"  # type: ignore[union-attr]
    again = write(capsys, "the migration plan for the lake file", "host", "--tag", "x", "--tag", "y")
    assert again == a, "dedupe returns the first row's id"


def test_write_json_and_meta(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    code, out, _ = run(
        capsys, "--json", "write", "x", "--source", "host", "--meta", '{"k": 1}', "--kind", "sediment",
        "--from", write(capsys, "parent row"), "--timestamp", "2026-01-02T03:04:05Z", "--no-dedupe",
    )
    assert code == 0
    obj = json.loads(out)
    assert obj["meta"] == {"k": 1} and obj["kind"] == "sediment" and obj["timestamp"] == "2026-01-02T03:04:05.000Z"
    assert len(obj["derived_from"]) == 1 and obj["engagement"] is None


def test_recall_lines_and_json(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    a = write(capsys, "we chose sqlite for the lake file format", "reader", "--tag", "design")
    b = write(capsys, "the migration to the new lake schema is done", "ada", "--tag", "ops")
    code, out, err = run(capsys, "recall", "lake migration schema")
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines and all(HIT_LINE.match(ln) for ln in lines)
    assert lines[0].split("  ")[1] == b
    code, out, _ = run(capsys, "--json", "recall", "lake file format")
    hits = json.loads(out)
    assert isinstance(hits, list) and hits[0]["delta"]["id"] == a
    assert {"score", "relevance", "recency", "valence", "matched", "step"} <= hits[0].keys()
    code, out, _ = run(capsys, "recall", "--source", "ada", "--limit", "1")
    assert code == 0 and out.splitlines()[0].split("  ")[1] == b
    code, out, _ = run(capsys, "recall", "--tag", "design", "--json")
    assert [h["delta"]["id"] for h in json.loads(out)] == [a]
    code, out, _ = run(capsys, "recall", "--exclude-tag", "design", "--exclude-tag", "ops", "--json")
    assert json.loads(out) == []


def test_recall_plan_file_and_stdin(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path) -> None:
    a = write(capsys, "the lake migration is finished", "reader")
    write(capsys, "unrelated lunch note", "reader")
    plan = [{"id": "a", "search": "lake migration"}, {"id": "b", "filter": {"source": "reader"}}]
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    code, out, _ = run(capsys, "recall", "--plan", str(tmp_path / "plan.json"))
    assert code == 0 and len(out.splitlines()) == 2, "hits of the last step, one line each"
    code, out, _ = run(capsys, "recall", "--plan", "-", stdin=json.dumps(plan[:1]))
    assert code == 0 and out.splitlines()[0].split("  ")[1] == a
    code, out, _ = run(capsys, "--json", "recall", "--plan", "-", stdin=json.dumps(plan))
    doc = json.loads(out)
    assert set(doc["steps"]) == {"a", "b"} and doc["steps"]["a"]["hits"][0]["delta"]["id"] == a
    assert "warnings" in doc and "timing_ms" in doc
    code, _, err = run(capsys, "recall", "--plan", "-", stdin=json.dumps([{"id": "a"}]))
    assert code == 1 and "no action" in err


def test_context_block_and_min_query(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    write(capsys, "we chose sqlite for the lake file format", "reader")
    write(capsys, "the migration to the new lake schema is done", "reader")
    code, out, err = run(capsys, "context", "lake sqlite migration")
    assert code == 0 and err == "" and "You remember" in out and "sqlite" in out
    code, short, _ = run(capsys, "context", "lake")
    code2, none, _ = run(capsys, "context")
    assert code == code2 == 0 and short == none == "", "a query under --min-query-chars renders like no query"
    code, out, _ = run(capsys, "context", "lake", "--min-query-chars", "3")
    assert code == 0 and "You remember" in out
    code, out, _ = run(capsys, "--json", "context", "lake sqlite migration", "--label", "remember=<< {n} >>")
    doc = json.loads(out)
    assert doc["rendered"].startswith("<< 2 >>") and len(doc["hits"]) == 2 and doc["omitted_strips"] == 0
    assert {"crystal", "containers", "strips", "warnings"} <= doc.keys()
    code, _, err = run(capsys, "context", "lake sqlite migration", "--label", "nokey")
    assert code == 2 and "KEY=VALUE" in err


def test_cmd_system_prompt(
    capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path, opened: list[dict[str, Any]]
) -> None:
    from lake.cli import READONLY_CMDS
    assert "system-prompt" in READONLY_CMDS
    seed = write(capsys, "a seed row for the crystal and moods", "host")
    write(capsys, "I am the identity crystal.", "lake", "--kind", "crystal", "--from", seed)
    for i, cw in enumerate(("CW_ONE", "CW_TWO", "CW_THREE")):
        write(capsys, json.dumps({"state": "x", "headline": f"h{i}", "subtext": f"s{i}", "carrier_wave": cw}),
              "host", "--kind", "mood", "--from", seed, "--tag", "feeling:x",
              "--timestamp", f"2026-09-02T17:5{i}:00Z", "--no-dedupe")

    code, out, err = run(capsys, "system-prompt")
    assert code == 0 and err == "" and out.startswith("Identity crystal")
    assert "Recent moods:" in out and all(cw in out for cw in ("CW_ONE", "CW_TWO", "CW_THREE"))
    assert opened[-1]["readonly"] is True  # system-prompt opens readonly

    code, out, _ = run(capsys, "system-prompt", "--moods", "1")
    assert code == 0 and "CW_THREE" in out and "CW_ONE" not in out  # --moods limits the section
    code, out, _ = run(capsys, "system-prompt", "--budget", "300")
    assert code == 0 and len(out.rstrip("\n")) <= 300  # --budget bounds the output

    code, out, _ = run(capsys, "system-prompt", "--label", "mood_header=MOODS>>")
    assert code == 0 and "MOODS>>" in out and "Recent moods:" not in out  # --label KEY=VALUE
    code, _, err = run(capsys, "system-prompt", "--label", "nokey")
    assert code == 2 and "KEY=VALUE" in err

    code, out, _ = run(capsys, "--json", "system-prompt", "--moods", "1")
    assert code == 0 and isinstance(json.loads(out), str) and "CW_THREE" in json.loads(out)  # --json emits the string

    code, out, err = run(capsys, "system-prompt", "--file", str(tmp_path / "fresh.lake"))
    assert code == 0 and out == ""  # a fresh lake prints nothing


def test_engage_prints_id(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    a = write(capsys, "the lake migration is finished", "reader")
    code, out, _ = run(capsys, "engage", a, "affirm", "--by", "robin", "--note", "useful")
    assert code == 0 and re.fullmatch(r"[0-9a-f]{12}\n", out)
    code, out, _ = run(capsys, "--json", "engage", a, "reply", "--note", "more", "--no-snapshot", "--tag", "t")
    obj = json.loads(out)
    assert obj["engagement"] == {"target_id": a, "kind": "reply", "by": None, "note": "more"}
    assert obj["content"] == "more" and obj["tags"] == ["t"] and obj["meta"]["snapshot"]
    code, out, _ = run(capsys, "--json", "recall", "lake migration")
    hit = next(h for h in json.loads(out) if h["delta"]["id"] == a)
    assert abs(hit["valence"] - 1.0625) < 1e-9, "one affirm and one reply: 1 + 0.05 × 1.25"
    code, _, err = run(capsys, "engage", "000000000000", "affirm")
    assert code == 1 and "000000000000" in err


# --- stats, export, import, sweep, due, crystal, lineage -----------------------------------------


def test_stats_export_import(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path) -> None:
    write(capsys, "one", "reader", "--tag", "a")
    write(capsys, "two 𝄞 astral", "ada", "--tag", "b")
    code, out, _ = run(capsys, "stats")
    assert code == 0
    kv = dict(ln.split(": ", 1) for ln in out.splitlines())
    assert kv["rows"] == "2" and kv["tags"] == "2" and json.loads(kv["by_source"]) == {"reader": 1, "ada": 1}
    code, out, _ = run(capsys, "--json", "stats")
    assert json.loads(out)["live_rows"] == 2 and json.loads(out)["schema_version"] == "1"
    dump = tmp_path / "dump.jsonl"
    code, out, _ = run(capsys, "export", str(dump))
    assert code == 0 and out == "2\n" and len(dump.read_text(encoding="utf-8").splitlines()) == 2
    other = str(tmp_path / "other.lake")
    code, out, _ = run(capsys, "--file", other, "import", str(dump))
    assert code == 0 and out == "written: 2\nskipped: 0\nerrors: 0\n"
    code, out, _ = run(capsys, "--file", other, "--json", "import", str(dump))
    assert json.loads(out) == {"written": 0, "skipped": 2, "errors": 0}
    with Lake(other, readonly=True) as lk:
        assert {d.content for d in [lk.get(json.loads(ln)["id"]) for ln in dump.read_text().splitlines()] if d} == {
            "one", "two 𝄞 astral"}


def test_sweep_and_lineage_dangling(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    p = write(capsys, "a parent that expires", "host", "--expires", "1s")
    q = write(capsys, "a parent that stays", "host")
    c = write(capsys, "made from both", "host", "--kind", "sediment", "--from", p, "--from", q)
    g = write(capsys, "a grandchild", "host", "--kind", "sediment", "--from", c)
    code, out, _ = run(capsys, "lineage", g)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split("  ") == ["1", c, lines[0].split("  ")[2], "host", "sediment", "made from both"]
    assert sorted(ln.split("  ")[0] for ln in lines) == ["1", "2", "2"]
    code, out, _ = run(capsys, "lineage", g, "--depth", "1")
    assert out.splitlines() == lines[:1]
    code, out, _ = run(capsys, "sweep")
    assert code == 0 and out == "deleted: 0\norphan_media: 0\n"
    time.sleep(1.1)
    code, out, _ = run(capsys, "--json", "sweep")
    assert code == 0 and json.loads(out) == {"deleted": 1, "orphan_media": 0}
    code, out, _ = run(capsys, "lineage", c)
    assert code == 0 and out.splitlines()[-1] == f"dangling: {p}"
    assert out.splitlines()[0].split("  ")[:2] == ["1", q] and out.splitlines()[0].endswith("plain  a parent that stays")
    code, out, _ = run(capsys, "--json", "lineage", c)
    assert json.loads(out)["dangling"] == [p] and json.loads(out)["hops"] == {q: 1}
    code, _, err = run(capsys, "lineage", "000000000000")
    assert code == 1 and err.startswith("lake: ")


def test_due_exit_code(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    code, out, _ = run(capsys, "due", "mood")
    assert (code, out) == (1, "no\n")
    code, out, _ = run(capsys, "--json", "due", "crystal")
    assert (code, json.loads(out)) == (1, {"due": False})
    for i in range(3):
        write(capsys, f"row {i} of a cluster about the lake", "host", "--timestamp", "45 minutes ago")
    code, out, _ = run(capsys, "due", "container")
    assert (code, out) == (0, "yes\n")
    code, _, err = run(capsys, "due", "weather")
    assert code == 2 and "weather" in err


def test_crystal_and_write_file(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    code, out, _ = run(capsys, "crystal")
    assert (code, out) == (0, "")
    code, out, _ = run(capsys, "--json", "crystal")
    assert json.loads(out) is None
    base = write(capsys, "a base row")
    text = "# Me\n\n## A facet\n\nwhat I keep pulling toward"
    cid = write(capsys, text, "host", "--kind", "crystal", "--from", base)
    code, out, _ = run(capsys, "crystal")
    assert (code, out) == (0, text + "\n")
    md = Path(lake_file).with_name("t.crystal.md")
    assert not md.exists()
    code, out, _ = run(capsys, "crystal", "--write-file")
    assert code == 0 and md.read_text(encoding="utf-8") == text + "\n"
    code, out, _ = run(capsys, "--json", "crystal")
    assert json.loads(out)["id"] == cid and json.loads(out)["kind"] == "crystal"


# --- exit codes, LAKE_FILE, readonly, sources, version -------------------------------------------


def test_exit_codes(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    assert run(capsys, "write", "x")[0] == 2, "usage: --source is required"
    assert run(capsys, "nonsense")[0] == 2
    assert run(capsys)[0] == 2
    assert run(capsys, "write", "x", "--source", "s", "--meta", "[1]")[0] == 2, "--meta must be an object"
    code, _, err = run(capsys, "write", "x", "--source", "lake:x")
    assert code == 1 and err.startswith("lake: ")
    code, _, err = run(capsys, "write", "x", "--source", "s", "--expires", "soon")
    assert code == 2 and err.startswith("lake: ")
    code, _, err = run(capsys, "write", "x", "--source", "s", "--from", "000000000000")
    assert code == 1
    assert run(capsys, "--think", "bogus", "stats")[0] == 2
    assert run(capsys, "recall", "--limit", "0")[0] == 2
    assert run(capsys, "--help")[0] == 0


def test_lake_file_env_and_missing(capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§13.8: LAKE names the lake; LAKE_FILE still does, with a deprecation line; the flags win; nothing is exit 2."""
    monkeypatch.delenv("LAKE", raising=False)
    monkeypatch.delenv("LAKE_FILE", raising=False)
    code, out, err = run(capsys, "write", "x", "--source", "s")
    assert code == 2 and "no lake" in err and "--lake" in err and out == ""
    env_path = tmp_path / "env.lake"
    monkeypatch.setenv("LAKE", str(env_path))
    code, _, err = run(capsys, "write", "x", "--source", "s")
    assert code == 0 and env_path.exists() and err == ""
    old_path = tmp_path / "old.lake"
    monkeypatch.setenv("LAKE_FILE", str(old_path))
    assert run(capsys, "write", "x", "--source", "s")[0] == 0 and not old_path.exists(), "LAKE beats LAKE_FILE"
    monkeypatch.delenv("LAKE")
    code, _, err = run(capsys, "write", "x", "--source", "s")
    assert code == 0 and old_path.exists() and "LAKE_FILE is deprecated; use LAKE=" in err
    for flag in ("--lake", "--file"):
        flag_path = tmp_path / f"flag{flag}.lake"
        assert run(capsys, "write", "x", "--source", "s", flag, str(flag_path))[0] == 0
        assert flag_path.exists(), f"{flag} wins over the environment"
    with Lake(env_path, readonly=True) as lk:
        assert lk.stats()["rows"] == 1
    monkeypatch.delenv("LAKE_FILE")
    nested = tmp_path / "a" / "b" / "default.lake"
    assert run(capsys, "--default", str(nested), "write", "x", "--source", "s")[0] == 0 and nested.exists()


def test_readonly_open(capsys: pytest.CaptureFixture[str], lake_file: str, opened: list[dict[str, Any]]) -> None:
    assert run(capsys, "stats")[0] == 0 and opened[-1]["readonly"] is False, "a missing file is created"
    write(capsys, "x")
    assert opened[-1]["readonly"] is False
    for argv in (["recall", "x"], ["context", "x"], ["due", "mood"], ["crystal"], ["stats"], ["lineage", "x"]):
        run(capsys, *argv)
        assert opened[-1]["readonly"] is True, argv
    run(capsys, "export", str(Path(lake_file).with_name("d.jsonl")))
    assert opened[-1]["readonly"] is True
    for argv in (["crystal", "--write-file"], ["sweep"], ["engage", "x", "affirm"], ["embed-missing"]):
        run(capsys, *argv)
        assert opened[-1]["readonly"] is False, argv


def test_exclude_and_collapse_sources(
    capsys: pytest.CaptureFixture[str], lake_file: str, opened: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.3: --exclude-source/--collapse-source and LAKE_EXCLUDE_SOURCES/LAKE_COLLAPSE_SOURCES are deprecated names
    for source: automation rules (searchable, never consolidated, collapsed in timelines); each prints a note."""
    a = write(capsys, "lake migration notes", "reader")
    b = write(capsys, "lake migration notes", "daemon")
    ids = lambda out: sorted(h["delta"]["id"] for h in json.loads(out))  # noqa: E731
    assert ids(run(capsys, "--json", "recall", "lake migration")[1]) == sorted([a, b])
    code, out, err = run(capsys, "--json", "--exclude-source", "daemon", "recall", "lake migration")
    assert ids(out) == sorted([a, b]) and "deprecated" in err, "no longer hidden: searchable"
    assert list(opened[-1]["automation"]) == ["tag:automation", "source:daemon"]
    monkeypatch.setenv("LAKE_EXCLUDE_SOURCES", "daemon, other")
    code, out, err = run(capsys, "--json", "recall", "lake migration")
    assert ids(out) == sorted([a, b]) and "LAKE_EXCLUDE_SOURCES is deprecated" in err
    assert list(opened[-1]["automation"]) == ["tag:automation", "source:daemon", "source:other"]
    monkeypatch.delenv("LAKE_EXCLUDE_SOURCES")
    monkeypatch.setenv("LAKE_COLLAPSE_SOURCES", "daemon,sensor")
    run(capsys, "context", "lake migration")
    assert list(opened[-1]["automation"]) == ["tag:automation", "source:daemon", "source:sensor"]
    run(capsys, "--automation", "tag:bot", "--collapse-source", "x", "context", "lake migration")
    assert list(opened[-1]["automation"]) == ["tag:bot", "source:x"], "--automation replaces the environment's rules"
    monkeypatch.delenv("LAKE_COLLAPSE_SOURCES")
    code, _, err = run(capsys, "stats")
    assert code == 0 and err == "" and list(opened[-1]["automation"]) == ["tag:automation"], "the host default"


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "--version")
    assert code == 0 and re.fullmatch(r"lake \d+\.\d+\.\d+\n", out)


# --- think adapters --------------------------------------------------------------------------------


def test_think_cmd_adapter(tmp_path: Path) -> None:
    log = tmp_path / "log.json"
    think = adapters.make_think(
        f"cmd:python3 -c 'import json,os,sys; json.dump({{\"stdin\": sys.stdin.read(), \"env\": dict(os.environ)}}, "
        f"open(\"{log}\", \"w\")); print(\"the answer\")'"
    )
    assert think("the prompt", system="be brief") == "the answer\n"
    rec = json.loads(log.read_text())
    assert rec["stdin"] == "the prompt" and rec["env"]["LAKE_SYSTEM"] == "be brief"
    assert rec["env"]["LAKE_HOOKS_OFF"] == "1" and "LAKE_JSON" not in rec["env"]
    think("p", json=True)
    rec = json.loads(log.read_text())
    assert rec["env"]["LAKE_JSON"] == "1" and rec["env"]["LAKE_SYSTEM"] == ""
    with pytest.raises(LakeError, match="exited 3"):
        adapters.make_think("cmd:echo boom >&2; exit 3")("p")
    with pytest.raises(ValueError):
        adapters.make_think("magic")


def test_think_claude_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "claude.log"
    script(tmp_path, "claude", f'printf "%s\\n" "$@" > "{log}"; echo "hooks=$LAKE_HOOKS_OFF" >> "{log}"; '
                               f'pwd > "{log}.cwd"; ls -A >> "{log}.cwd"; '
                               f'cat >> "{log}"; echo \'{{"kind": "skip", "reason": "thin"}}\'')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    think = adapters.make_think("claude")
    assert think("look at this", system="You are Ada.", json=True) == {"kind": "skip", "reason": "thin"}
    argv, rest = log.read_text().split("hooks=", 1)
    assert argv.split("\n")[:-1] == [*adapters.CLAUDE_ARGS[1:], "--system-prompt", "You are Ada."]
    assert argv.split("\n")[:-1] == [
        "-p", "--output-format", "text", "--settings", '{"hooks": {}}', "--setting-sources", "", "--strict-mcp-config",
        "--tools", "", "--no-session-persistence", "--disable-slash-commands", "--system-prompt", "You are Ada.",
    ]
    assert rest == "1\nlook at this\nRespond with only a JSON object."
    cwd_lines = (tmp_path / "claude.log.cwd").read_text().splitlines()
    assert cwd_lines[0] != os.getcwd() and Path(cwd_lines[0]).name.startswith("lake-think-")
    assert cwd_lines[1:] == [], "the think cwd is a fresh empty directory"
    assert not Path(cwd_lines[0]).exists(), "the temp cwd is removed after the call"
    assert think("plain text", json=False) == '{"kind": "skip", "reason": "thin"}\n'
    argv, rest = log.read_text().split("hooks=", 1)
    assert "--system-prompt" not in argv and rest == "1\nplain text"
    first = (tmp_path / "claude.log.cwd").read_text().splitlines()[0]
    assert first != cwd_lines[0], "every call gets its own temp cwd"
    assert adapters.model_name("claude") == "claude"


def test_think_claude_args_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§8 `claude:<args>`: the built-in adapter plus extra CLI args, placed before --system-prompt."""
    log = tmp_path / "claude.log"
    script(tmp_path, "claude", f'printf "%s\\n" "$@" > "{log}"; cat > /dev/null; echo ok')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    think = adapters.make_think("claude:--model sonnet")
    assert think("p", system="S") == "ok\n"
    assert log.read_text().split("\n")[:-1] == [*adapters.CLAUDE_ARGS[1:], "--model", "sonnet", "--system-prompt", "S"]
    adapters.make_think("claude:--model 'my model'")("p", system="S")
    assert log.read_text().split("\n")[:-1][-4:] == ["--model", "my model", "--system-prompt", "S"]
    assert adapters.model_name("claude:--model sonnet") == "claude --model sonnet"
    assert adapters.model_name("claude:") == "claude"


@pytest.mark.parametrize("spec", [
    "cmd:claude -p", "cmd:claude -p --model sonnet", "cmd:env FOO=1 claude -p", "cmd:FOO=1 BAR=2 claude -p",
    "cmd:/usr/local/bin/claude -p", "cmd:env -u LAKE_URL claude -p", "cmd:timeout 60 claude -p", "cmd:nice claude -p",
    "cmd:command claude -p", "cmd:sh -c 'claude -p'", "cmd:claude -p # LAKE_SYSTEM", "cmd:timeout -s KILL 60 claude -p",
    "cmd:nice -n 5 claude -p", "cmd:exec claude -p", "cmd:bash -ec 'exec claude -p'",
])
def test_think_cmd_claude_refused(spec: str) -> None:
    """§8: a `cmd:` whose first command word is claude (past assignments, common wrappers and `sh -c`) and whose text,
    comments dropped, never names LAKE_SYSTEM is refused; the message recommends only the isolated claude:<args>."""
    with pytest.raises(adapters.CmdClaudeError, match="never receives the lake's system prompt and gets none of the"
                       r" claude adapter's isolation .*; use claude:<args> \(e\.g\. claude:--model sonnet\)$"):
        adapters.make_think(spec)
    assert issubclass(adapters.CmdClaudeError, ValueError) and "$LAKE_SYSTEM" not in adapters.CMD_CLAUDE


def test_think_cmd_claude_with_system_prompt_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = tmp_path / "claude.log"
    script(tmp_path, "claude", f'printf "%s\\n" "$@" > "{log}"; cat > /dev/null; echo ok')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    think = adapters.make_think('cmd:claude -p --system-prompt "$LAKE_SYSTEM"')
    assert think("p", system="the lake system text") == "ok\n"
    assert log.read_text().split("\n")[:-1] == ["-p", "--system-prompt", "the lake system text"]
    shim = script(tmp_path, "claude-shim", 'cat > /dev/null; echo "$LAKE_SYSTEM"')
    assert adapters.make_think(f"cmd:{shim}")("p", system="kept") == "kept\n"  # a wrapper script is never refused


def test_cli_cmd_claude_exit_2(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    code, _, err = run(capsys, "--think", "cmd:claude -p", "consolidate", "mood", "--force")
    assert code == 2 and "claude:<args>" in err


def test_think_ollama_adapter() -> None:
    reply: dict[str, Any] = {"response": "the answer", "prompt_eval_count": 5000}
    with http_stub(lambda path, body: reply) as (url, calls):
        think = adapters.make_think(f"ollama:qwen3:8b@{url}/")
        assert adapters.model_name(f"ollama:qwen3:8b@{url}") == "qwen3:8b"
        assert think("short prompt", system="sys", json=True) == "the answer"
        path, body = calls[-1]
        assert path == "/api/generate"
        assert body == {
            "model": "qwen3:8b", "prompt": "short prompt", "system": "sys", "stream": False, "think": False,
            "options": {"num_ctx": 8192, "num_predict": 1024}, "format": "json",
        }
        long = "x" * 30000
        think(long, json=False)
        body = calls[-1][1]
        assert "format" not in body and body["options"] == {"num_ctx": 12048, "num_predict": 4096}
        adapters.make_think(f"ollama:m@{url}", num_ctx=4096, num_predict=77)(long, system="s" * 300, json=True)
        assert calls[-1][1]["options"] == {"num_ctx": 4096, "num_predict": 77}
        reply["prompt_eval_count"] = 10
        with pytest.raises(LakeError, match=r"^ollama truncated the prompt: 10 tokens evaluated for 30000 characters$"):
            think(long)
        reply["prompt_eval_count"] = 10
        assert think("tiny") == "the answer", "under the ratio only when count < chars / 8"
    with http_stub(lambda path, body: time.sleep(1.0)) as (url, calls):
        with pytest.raises(LakeError):
            adapters.make_think(f"ollama:m@{url}", timeout=0.2)("p")
    with pytest.raises(ValueError):
        adapters.make_think("ollama:no-url")


# --- embed adapters --------------------------------------------------------------------------------


def test_embed_ollama_adapter(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    def handle(path: str, body: dict[str, Any]) -> object:
        return {"embeddings": [[1.0, 0.0, 0.0] if "lake" in t else [0.0, 1.0, 0.0] for t in body["input"]]}

    with http_stub(handle) as (url, calls):
        embed = adapters.make_embed(f"ollama:nomic@{url}")
        assert embed(["about the lake", "other"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        assert calls[-1] == ("/api/embed", {"model": "nomic", "input": ["about the lake", "other"]})
        a = write(capsys, "about the lake", "host", "--embed", f"ollama:nomic@{url}")
        assert calls[-1][1]["input"] == ["about the lake"]
        with Lake(lake_file, readonly=True) as lk:
            assert lk.conn.execute("SELECT count(*) FROM vectors WHERE delta_id = ?", (a,)).fetchone()[0] == 1
        code, out, err = run(capsys, "--embed", f"ollama:nomic@{url}", "--json", "recall", "lake")
        assert code == 0 and json.loads(out)[0]["matched"] in ("both", "vector") and err == ""
    with http_stub(lambda path, body: time.sleep(1.0)) as (url, calls):
        with pytest.raises(RuntimeError):
            adapters.make_embed(f"ollama:m@{url}", timeout=0.2)(["x"])
        code, out, err = run(capsys, "--embed", f"ollama:m@{url}", "--embed-timeout", "0.2", "recall", "lake")
        assert code == 0 and "embed failed:" in err and a in out, "a timed-out embed is an FTS-only result"


def test_context_vector_flag(capsys: pytest.CaptureFixture[str], lake_file: str) -> None:
    def handle(path: str, body: dict[str, Any]) -> object:
        return {"embeddings": [[1.0, 0.0, 0.0] for _ in body["input"]]}

    with http_stub(handle) as (url, calls):
        write(capsys, "about the lake", "host", "--embed", f"ollama:nomic@{url}")
        code, out, _ = run(capsys, "--embed", f"ollama:nomic@{url}", "context", "about the lake")
        assert code == 0 and "about the lake" in out
        assert calls[-1][1]["input"] == ["about the lake"] and len(calls) == 1, "no embed call without --vector"
        code, out, _ = run(capsys, "--embed", f"ollama:nomic@{url}", "context", "about the lake", "--vector")
        assert code == 0 and "about the lake" in out
        assert len(calls) == 2 and calls[-1][1]["input"] == ["about the lake"]


def test_embed_cmd_adapter(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path) -> None:
    emb = script(tmp_path, "emb.sh", "python3 -c 'import json,sys; t=json.load(sys.stdin); "
                                     "print(json.dumps([[1,0] if \"lake\" in s else [0,1] for s in t]))'")
    embed = adapters.make_embed(f"cmd:{emb}")
    assert embed(["a lake", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    with pytest.raises(RuntimeError):
        adapters.make_embed("cmd:echo notjson")(["x"])
    with pytest.raises(RuntimeError, match="exited 2"):
        adapters.make_embed("cmd:exit 2")(["x"])
    a = write(capsys, "the lake row", "host", "--embed", f"cmd:{emb}")
    b = write(capsys, "no vector row", "host", "--embed", f"cmd:{emb}", "--no-embed")
    with Lake(lake_file, readonly=True) as lk:
        assert [r[0] for r in lk.conn.execute("SELECT delta_id FROM vectors")] == [a]
        assert lk.conn.execute("SELECT value FROM meta WHERE key = 'embed_dim'").fetchone()[0] == "2"
    code, out, _ = run(capsys, "--embed", f"cmd:{emb}", "embed-missing", "--batch-size", "8")
    assert (code, out) == (0, "1\n")
    with Lake(lake_file, readonly=True) as lk:
        assert lk.conn.execute("SELECT count(*) FROM vectors WHERE delta_id = ?", (b,)).fetchone()[0] == 1
    assert adapters.model_name(f"cmd:{emb}") is None


# --- consolidate through the CLI -------------------------------------------------------------------


def test_consolidate_cmd(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path, opened: list[dict[str, Any]]) -> None:
    ids = [write(capsys, f"row {i} about the lake migration", "host", "--timestamp", "45 minutes ago") for i in range(3)]
    log = tmp_path / "think.log"
    think = script(tmp_path, "think.sh", f'echo "json=$LAKE_JSON hooks=$LAKE_HOOKS_OFF" > "{log}"; cat >> "{log}"; '
                                         f"echo '{PROPOSE % json.dumps(ids)}'")
    code, out, err = run(capsys, "--think", f"cmd:{think}", "--model-name", "fake-1", "consolidate", "container")
    assert code == 0, err
    cid = out.strip()
    assert re.fullmatch(r"[0-9a-f]{12}", cid) and "skipped: 0" in err
    assert log.read_text().startswith("json=1 hooks=1\n") and all(i in log.read_text() for i in ids)
    assert opened[-1]["model_name"] == "fake-1"
    with Lake(lake_file, readonly=True) as lk:
        d = lk.get(cid)
        assert d is not None and d.kind == "container" and d.derived_from == ids and d.meta["model"] == "fake-1"  # type: ignore[index]
    code, out, _ = run(capsys, "--think", f"cmd:{think}", "consolidate", "container")
    assert (code, out) == (0, "nothing to do\n"), "not due after the run"
    code, out, _ = run(capsys, "--think", f"cmd:{think}", "--json", "consolidate", "container", "--force")
    doc = json.loads(out)
    assert doc["written"] == [] and doc["kind"] == "container" and {"skipped", "warnings", "think_calls"} <= doc.keys()
    code, out, _ = run(capsys, "--think", f"cmd:{think}", "--json", "consolidate", "container", "--force",
                       "--inputs", *ids, "--add-tag", "s:1")
    assert code == 0 and json.loads(out)["written"][0]["tags"] == ["s:1"]
    code, _, err = run(capsys, "consolidate", "mood", "--force")
    assert code == 1 and "think" in err
    code, _, err = run(capsys, "--think", f"cmd:{think}", "consolidate", "mood", "--force", "--since", "1h ago")
    assert code == 2 and "--until" in err
    code, _, err = run(capsys, "--think", f"cmd:{think}", "consolidate", "crystal", "--force", "--close-gap", "1h")
    assert code == 2, "a container-only opt on the crystal is a ValueError"


def test_consolidate_session_flags(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path) -> None:
    """§8: --no-backfill, --backfill-max, --session-gap and --session-prefix reach the §6.1 session opts."""
    old = [write(capsys, f"old row {i} of one session", "host", "--tag", "session:old", "--timestamp", f"2026-08-01T10:0{i}:00Z")
           for i in range(3)]
    think = script(tmp_path, "think.sh", 'cat > /dev/null; echo \'{"title": "Old session", "summary": "Three rows."}\'')
    code, out, _ = run(capsys, "--think", f"cmd:{think}", "consolidate", "container", "--force", "--no-backfill")
    assert (code, out) == (0, "nothing to do\n")
    code, out, _ = run(capsys, "--think", f"cmd:{think}", "--json", "consolidate", "container", "--force",
                       "--backfill-max", "1", "--session-gap", "1h", "--session-prefix", "session:")
    doc = json.loads(out)
    assert code == 0 and [w["derived_from"] for w in doc["written"]] == [old]
    assert doc["written"][0]["meta"]["session"] == "session:old" and doc["written"][0]["meta"]["backfill"] is True


def test_consolidate_claude_through_cli(capsys: pytest.CaptureFixture[str], lake_file: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = [write(capsys, f"row {i} about the lake migration", "host", "--timestamp", "45 minutes ago") for i in range(3)]
    log = tmp_path / "claude.log"
    script(tmp_path, "claude", f'echo "$LAKE_HOOKS_OFF $1 $2 $3" > "{log}"; cat >> "{log}"; echo \'{PROPOSE % json.dumps(ids)}\'')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    code, out, err = run(capsys, "--think", "claude", "consolidate", "container", "--force")
    assert code == 0, err
    assert log.read_text().startswith("1 -p --output-format text\n")
    assert log.read_text().rstrip().endswith("Respond with only a JSON object.")
    with Lake(lake_file, readonly=True) as lk:
        d = lk.get(out.strip())
        assert d is not None and d.derived_from == ids and d.meta["model"] == "claude"  # type: ignore[index]
