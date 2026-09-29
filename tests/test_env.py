"""SPEC §13.8: one target key (LAKE), one resolver (lake.resolve, lake/_env.py), one environment entry point
(lake.open() with no target). The precedence table is written once and tested here row by row; every host
(CLI, serve, MCP server, hooks, eval isolation) goes through it, and no host keeps its own copy."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import REPO, served

import lake
from lake import Lake, LakeError, RemoteLake, Target, resolve, setting
from lake._env import DEFAULT_THINK, Config, config, read_env_file

NO_FILE = "/nonexistent/lake-env"


def envfile(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "env"
    path.write_text(text, encoding="utf-8")
    return path


# --- the precedence table (SPEC §13.8), one test per row --------------------------------------------


def test_env_file_url_and_token_the_laptop(tmp_path: Path) -> None:
    t = resolve(environ={}, env_file=envfile(tmp_path, "export LAKE_URL=http://server:8377\nexport LAKE_TOKEN=t\n"))
    assert t == Target("http://server:8377", True, "t", "env file", ("LAKE_URL is deprecated; use LAKE=<the same value>",))


def test_env_file_lake_file_bs(tmp_path: Path) -> None:
    t = resolve(environ={}, env_file=envfile(tmp_path, "LAKE_FILE=/home/robin/.lake/claude.lake\nLAKE_TOKEN=t\n"))
    assert t == Target("/home/robin/.lake/claude.lake", False, None, "env file",
                       ("LAKE_FILE is deprecated; use LAKE=<the same value>",))


def test_process_lake_file_beats_env_file_url_eval_isolation(tmp_path: Path) -> None:
    t = resolve(environ={"LAKE_FILE": "/tmp/x.lake"}, env_file=envfile(tmp_path, "LAKE_URL=http://server:8377\nLAKE_TOKEN=t\n"))
    assert (t.target, t.remote, t.token, t.source) == ("/tmp/x.lake", False, None, "environment")


def test_lake_beats_both_within_a_source() -> None:
    t = resolve(environ={"LAKE": "/a.lake", "LAKE_URL": "http://x", "LAKE_FILE": "/y"}, env_file=NO_FILE)
    assert (t.target, t.remote, t.notes) == ("/a.lake", False, ())


def test_url_beats_file_within_a_source() -> None:
    t = resolve(environ={"LAKE_URL": "http://x", "LAKE_FILE": "/y"}, env_file=NO_FILE)
    assert (t.target, t.remote) == ("http://x", True)


def test_token_follows_the_env_files_own_url(tmp_path: Path) -> None:
    file = envfile(tmp_path, "LAKE=http://server:8377/\nLAKE_TOKEN=t\n")
    assert resolve(environ={"LAKE": "http://server:8377"}, env_file=file).token == "t"  # same URL, trailing / ignored
    assert resolve(environ={"LAKE": "http://other"}, env_file=file).token is None  # a bearer never leaves its URL
    assert resolve("http://other", environ={}, env_file=file).token is None
    assert resolve("http://server:8377", environ={}, env_file=file).token == "t"


def test_nothing_named_default_else_error(tmp_path: Path) -> None:
    t = resolve(environ={"HOME": str(tmp_path)}, env_file=NO_FILE, default="~/.lake/claude.lake")
    assert (t.target, t.source, t.remote) == (os.path.expanduser("~/.lake/claude.lake"), "default", False)
    with pytest.raises(ValueError, match="no lake"):
        resolve(environ={}, env_file=NO_FILE)
    with pytest.raises(LakeError, match="no lake"):
        lake.open()  # conftest: no LAKE*, LAKE_ENV_FILE nonexistent


# --- the rest of the rule ----------------------------------------------------------------------------


def test_first_source_wins_whole(tmp_path: Path) -> None:
    """A lower source never adds a key to a higher one: the env file's LAKE never beats a process LAKE_URL."""
    file = envfile(tmp_path, "LAKE=/from-file.lake\n")
    t = resolve(environ={"LAKE_URL": "http://proc"}, env_file=file)
    assert (t.target, t.source) == ("http://proc", "environment")


def test_explicit_argument_wins_and_takes_the_process_token(tmp_path: Path) -> None:
    file = envfile(tmp_path, "LAKE=/from-file.lake\n")
    t = resolve("http://arg/", environ={"LAKE": "/proc.lake", "LAKE_TOKEN": "p"}, env_file=file)
    assert t == Target("http://arg", True, "p", "argument", ())
    assert resolve("http://arg", token="x", environ={"LAKE_TOKEN": "p"}, env_file=file).token == "x"
    assert resolve("~/x.lake", environ={"LAKE_TOKEN": "p"}, env_file=file).token is None  # a local target takes none


def test_empty_values_count_as_unset(tmp_path: Path) -> None:
    file = envfile(tmp_path, "LAKE=\nLAKE_FILE=/from-file.lake\nLAKE_THINK=\n")
    t = resolve(environ={"LAKE": "", "LAKE_URL": ""}, env_file=file)
    assert (t.target, t.source) == ("/from-file.lake", "env file")
    assert setting("LAKE_THINK", environ={"LAKE_THINK": ""}, env_file=file) is None
    assert resolve("", environ={}, env_file=file).source == "env file"


def test_lake_env_file_names_the_file(tmp_path: Path) -> None:
    file = envfile(tmp_path, "LAKE=/named-by-lake-env-file.lake\n")
    assert resolve(environ={"LAKE_ENV_FILE": str(file)}).target == "/named-by-lake-env-file.lake"


def test_tilde_expands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve(environ={"LAKE": "~/a.lake"}, env_file=NO_FILE).target == str(tmp_path / "a.lake")


def test_setting_per_key(tmp_path: Path) -> None:
    file = envfile(tmp_path, "LAKE_THINK=cmd:file\nLAKE_EMBED=cmd:embed\n")
    assert setting("LAKE_THINK", environ={"LAKE_THINK": "cmd:proc"}, env_file=file) == "cmd:proc"
    assert setting("LAKE_EMBED", environ={"LAKE_THINK": "cmd:proc"}, env_file=file) == "cmd:embed"
    assert setting("LAKE_NOPE", environ={}, env_file=file) is None


def test_config_is_the_one_host_rule(tmp_path: Path) -> None:
    """config(): the given spec, else the key, else DEFAULT_THINK only where a host digests; automation: the given
    rules, else LAKE_AUTOMATION (unset: the default; empty: none), plus the deprecated source keys with notes."""
    file = envfile(tmp_path, "LAKE_THINK=cmd:file\nLAKE_EMBED=cmd:embed\n")
    assert config(environ={}, env_file=NO_FILE) == Config(None, None, ("tag:automation",))
    assert config(environ={}, env_file=NO_FILE, digest=True).think == DEFAULT_THINK
    assert config("cmd:given", environ={}, env_file=file, digest=True).think == "cmd:given"
    assert config(environ={}, env_file=file) == Config("cmd:file", "cmd:embed", ("tag:automation",))
    assert config(environ={"LAKE_AUTOMATION": ""}, env_file=NO_FILE).automation == ()
    assert config(automation=["source:x"], environ={"LAKE_AUTOMATION": ""}, env_file=NO_FILE).automation == ("source:x",)
    file = envfile(tmp_path, "LAKE_AUTOMATION='tag:automation,prefix:# Watcher'\nLAKE_EXCLUDE_SOURCES=hidden\n")
    c = config(environ={"LAKE_COLLAPSE_SOURCES": "heartbeat, ,daemon"}, env_file=file)
    assert c.automation == ("tag:automation", "prefix:# Watcher", "source:heartbeat", "source:daemon", "source:hidden")
    assert [n.split(" ", 1)[0] for n in c.notes] == ["LAKE_COLLAPSE_SOURCES", "LAKE_EXCLUDE_SOURCES"]
    assert "exclude_sources per call" in c.notes[1] and "exclude_sources per call" not in c.notes[0]


def test_the_automation_default_holds_however_the_file_is_opened(tmp_path: Path) -> None:
    """tag:automation is the Lake's own default: Lake(path), lake.open(path) and lake.open() from the environment
    agree; automation=[] turns it off."""
    path = tmp_path / "a.lake"
    with Lake(path) as a, lake.open(path) as b:
        assert a.automation == b.automation == (("tag", "automation"),)
    with Lake(path, automation=[]) as c:
        assert c.automation == ()


def test_env_file_parser(tmp_path: Path) -> None:
    """The §13.8 grammar: comments, export, one quote layer, later-wins, malformed lines ignored (moved here from
    test_serve: the parser lives in lake/_env.py; lake.remote re-exports it)."""
    env = envfile(tmp_path, "# a comment\n  # indented comment\n\nLAKE_URL=http://10.0.0.1:8377\n"
                  "export LAKE_THINK=ollama:qwen3:32b@http://127.0.0.1:11434\n" 'LAKE_TOKEN="k3yb0ard-c4t"\n'
                  "LAKE_FILE='/data/a.lake'\nnot a setting at all\nlower_case=ignored\n"
                  "LAKE_URL=http://10.241.80.73:8377 \n"  # §13.8: a trailing space must not survive
                  'LAKE_EMBED="cmd:echo x" \n'  # trailing space outside the quotes must not defeat the strip
                  "EMPTY=\n")
    assert read_env_file(env) == {
        "LAKE_URL": "http://10.241.80.73:8377", "LAKE_THINK": "ollama:qwen3:32b@http://127.0.0.1:11434",
        "LAKE_TOKEN": "k3yb0ard-c4t", "LAKE_FILE": "/data/a.lake", "LAKE_EMBED": "cmd:echo x", "EMPTY": "",
    }
    assert read_env_file(tmp_path / "missing") == {}
    from lake.remote import read_env_file as old_path
    assert old_path is read_env_file  # `from lake.remote import read_env_file` (older callers)


# --- lake.open(): the one environment entry point ---------------------------------------------------


def test_open_with_a_target_reads_no_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE", str(tmp_path / "from-env.lake"))
    monkeypatch.setenv("LAKE_THINK", "cmd:echo never")
    with lake.open(tmp_path / "sub" / "mine.lake") as lk:  # a writable open creates the parent directory
        assert isinstance(lk, Lake) and lk.think is None and lk.automation == (("tag", "automation"),)  # Lake's default
    assert not (tmp_path / "from-env.lake").exists()
    with lake.open(tmp_path / "sub" / "mine.lake", readonly=True) as lk:
        assert lk.readonly


def test_open_with_no_target_uses_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE_ENV_FILE", str(envfile(tmp_path, f"LAKE_FILE={tmp_path / 'e.lake'}\nLAKE_THINK=cmd:cat\n")))
    with lake.open() as lk:
        assert isinstance(lk, Lake) and lk.path == tmp_path / "e.lake"
        assert lk.think is not None and lk.model_name is None  # cmd: names no model
        assert lk.automation == (("tag", "automation"),)  # the host default
    monkeypatch.setenv("LAKE", str(tmp_path / "p.lake"))
    with lake.open(think=False, automation=["source:x"]) as lk:  # what is passed beats the environment
        assert lk.path == tmp_path / "p.lake" and lk.think is None and lk.automation == (("source", "x"),)
    monkeypatch.delenv("LAKE")
    monkeypatch.setenv("LAKE_ENV_FILE", NO_FILE)
    with lake.open(default=tmp_path / "d" / "default.lake") as lk:
        assert lk.path == tmp_path / "d" / "default.lake"


def test_open_remote_ignores_env_think_and_readonly_refuses_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAKE", "http://127.0.0.1:9/")
    monkeypatch.setenv("LAKE_THINK", "cmd:cat")
    monkeypatch.setenv("LAKE_TOKEN", "tok")
    lk = lake.open(readonly=True)
    assert isinstance(lk, RemoteLake) and lk.url == "http://127.0.0.1:9" and lk.token == "tok"
    with pytest.raises(LakeError, match="think= is not available over HTTP"):
        lake.open(think="cmd:cat")
    with pytest.raises(LakeError, match="embed= is not available over HTTP"):
        lake.open("http://127.0.0.1:9", embed=lambda texts: [])
    with pytest.raises(LakeError, match="clock= is not available over HTTP"):
        lake.open("http://127.0.0.1:9", clock=lambda: None)


def test_open_round_trip_local_and_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same program against a file and against `lake serve` on loopback with a scratch token: same API."""
    file = tmp_path / "shared.lake"
    monkeypatch.setenv("LAKE", str(file))
    with lake.open() as mem:
        first = mem.write("the heron stands in the shallows", "my-agent", tags=["session:s1", "user"]).id
    with served(tmp_path / "home", "--lake", str(file), env_extra={"LAKE_TOKEN": "scratch-token"}) as url:
        monkeypatch.setenv("LAKE", url)
        monkeypatch.setenv("LAKE_TOKEN", "scratch-token")
        with lake.open() as mem:
            assert isinstance(mem, RemoteLake)
            second = mem.write("the heron caught a fish", "my-agent", tags=["session:s1", "assistant"]).id
            assert {h.delta.id for h in mem.recall("heron")} == {first, second}
            assert "heron" in mem.context("what did the heron do")
    monkeypatch.setenv("LAKE", str(file))
    with lake.open(readonly=True) as mem:
        assert mem.get(second) is not None


def test_collapse_sources_is_an_alias_of_source_automation(tmp_path: Path) -> None:
    with pytest.warns(DeprecationWarning, match="collapse_sources"):
        lk = Lake(tmp_path / "a.lake", collapse_sources=["heartbeat"], automation=["tag:automation"])
    with lk:
        assert lk.automation == (("tag", "automation"), ("source", "heartbeat"))
        assert lk.automation_sources == ("heartbeat",)


# --- the hosts --------------------------------------------------------------------------------------


def test_cli_old_names_still_resolve_with_a_note(tmp_path: Path) -> None:
    """Compatibility: LAKE_FILE / LAKE_URL keep working and print one deprecation line; LAKE prints none."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAKE")}
    env.update(LAKE_ENV_FILE=NO_FILE, PYTHONPATH=str(REPO), HOME=str(tmp_path))
    cmd = [sys.executable, "-m", "lake.cli", "write", "a row", "--source", "t"]
    old = subprocess.run(cmd, env={**env, "LAKE_FILE": str(tmp_path / "old.lake")}, capture_output=True, text=True)
    assert old.returncode == 0 and "LAKE_FILE is deprecated; use LAKE=" in old.stderr
    new = subprocess.run(cmd, env={**env, "LAKE": str(tmp_path / "new.lake")}, capture_output=True, text=True)
    assert new.returncode == 0 and new.stderr == ""
    assert (tmp_path / "old.lake").exists() and (tmp_path / "new.lake").exists()
    none = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert none.returncode == 2 and "no lake" in none.stderr
    dflt = subprocess.run([*cmd[:3], "--default", str(tmp_path / "d.lake"), *cmd[3:]], env=env, capture_output=True,
                          text=True)
    assert dflt.returncode == 0 and (tmp_path / "d.lake").exists()
    for flag in ("--file", "--url"):  # the flag aliases print their note too (removed in 0.2.0)
        done = subprocess.run([*cmd[:3], flag, str(tmp_path / "f.lake"), *cmd[3:]], env=env, capture_output=True, text=True)
        assert done.returncode == 0 and f"{flag} is deprecated; use --lake (removed in 0.2.0)" in done.stderr


RESOLVER_SIGNS = (
    re.compile(r"""environ(?:\.get)?\(?\[?["']LAKE_(?:URL|FILE)["']"""),  # reading a target key by hand
    re.compile(r"""\.get\(["']LAKE_(?:URL|FILE)["']\)"""),  # ... from a parsed env file
    re.compile(r"""line\.partition\(["']=["']\)"""),  # a private copy of the env-file parser
)


def test_no_host_keeps_its_own_resolver() -> None:
    """lake/_env.py is the only resolver: the CLI, serve, the MCP server, the hooks and any host beside them call it."""
    hosts = [*REPO.glob("lake/*.py"), *REPO.glob("plugin/**/*.py"), *REPO.glob("plugin/**/*.sh"), *REPO.glob("hosts/**/*.py"),
             *(p for p in (REPO / "evals/common.py", REPO / "evals/env_probe.py") if p.exists())]
    offenders = []
    for path in hosts:
        rel = path.relative_to(REPO).as_posix()
        if rel == "lake/_env.py":
            continue
        text = path.read_text(encoding="utf-8")
        offenders += [f"{rel}: {sign.pattern}" for sign in RESOLVER_SIGNS if sign.search(text)]
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        if path.suffix == ".py" and not rel.startswith("evals/") and re.search(r"""["']LAKE_(?:URL|FILE)["']""", code):
            offenders.append(f"{rel}: names a target key")  # evals may: they scrub and set keys, never resolve
        if path.suffix == ".sh" and re.search(r"LAKE_(?:URL|FILE)", code):
            offenders.append(f"{rel}: a shell host reads a target key")
    assert not offenders, "a host resolves the lake itself: " + "; ".join(offenders)


def test_suite_ignores_a_hostile_shell_think_and_embed(tmp_path: Path) -> None:
    """The autouse fixture masks every LAKE_* key the developer's shell exports: with LAKE_THINK and LAKE_EMBED
    naming a canary, the tests that run `lake consolidate` without --think (and open a lake from the environment)
    still pass and the canary is never called (a shell with LAKE_THINK=claude:… must not spend model calls)."""
    log = tmp_path / "canary.log"
    canary = tmp_path / "canary.sh"
    canary.write_text(f'#!/bin/sh\necho called >> "{log}"\necho "{{}}"\n', encoding="utf-8")
    canary.chmod(0o755)
    env = {**{k: v for k, v in os.environ.items() if k != "LAKE_ENV_FILE"}, "LAKE_THINK": f"cmd:{canary}",
           "LAKE_EMBED": f"cmd:{canary}", "LAKE_AUTOMATION": "", "PYTHONPATH": str(REPO)}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_cli.py::test_consolidate_cmd",
         "tests/test_env.py::test_open_round_trip_local_and_remote"],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout[-2000:]
    assert not log.exists(), "a test reached the shell's LAKE_THINK / LAKE_EMBED"
