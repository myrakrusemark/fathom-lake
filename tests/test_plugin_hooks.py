"""Pipe-tests for the plugin's three hook scripts, run exactly as Claude Code runs them.

Each script is a subprocess fed synthetic hook stdin JSON, with LAKE_FILE in
a tmp dir and LAKE_BIN set to `<venv python> -m lake.cli` (a multi-word
command, which also exercises the scripts' shlex splitting). Assertions
against the lake go through the CLI, never the library import.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.plugin

HOOKS_DIR = Path(__file__).resolve().parent.parent / "plugin" / "hooks"
SESSION_START = HOOKS_DIR / "session-start.sh"
PROMPT = HOOKS_DIR / "prompt.sh"
STOP = HOOKS_DIR / "stop.sh"
ALL_SCRIPTS = [SESSION_START, PROMPT, STOP]
LAKE_BIN = f"{sys.executable} -m lake.cli"
NO_ENV_FILE = "/nonexistent/lake-env-file"  # hooks read LAKE_ENV_FILE, default ~/.lake/env


def base_env() -> dict[str, str]:
    """The caller's env minus every LAKE_* variable, with the real ~/.lake/env
    masked (SPEC §13.8: hooks read LAKE_ENV_FILE, default ~/.lake/env — tests
    must never touch the real home)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAKE_") and k != "LAKE"}
    env["LAKE_ENV_FILE"] = NO_ENV_FILE
    return env


def hook_env(lake_file: str, **extra: str) -> dict[str, str]:
    """A hermetic hook environment: base_env plus LAKE_FILE and LAKE_BIN."""
    env = base_env()
    env["LAKE_FILE"] = lake_file
    env["LAKE_BIN"] = LAKE_BIN
    env.update(extra)
    return env


def write_env_file(tmp_path: Path, text: str) -> str:
    """A §13.8 env file in the tmp dir, path returned for LAKE_ENV_FILE."""
    path = tmp_path / "lake-env"
    path.write_text(text, encoding="utf-8")
    return str(path)


def dead_port() -> int:
    """A loopback port with nothing listening on it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def run_hook(
    script: Path, stdin_obj: dict[str, Any], env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script)],
        input=json.dumps(stdin_obj),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def cli(lake_file: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the lake CLI against `lake_file`, hermetically."""
    return subprocess.run(
        [sys.executable, "-m", "lake.cli", *args],
        capture_output=True,
        text=True,
        env=hook_env(lake_file),
        timeout=30,
        check=False,
    )


def lake_rows(lake_file: str) -> list[dict[str, Any]]:
    """Every row in the lake, as recall --json delta dicts."""
    done = cli(lake_file, "recall", "--json")
    assert done.returncode == 0, done.stderr
    return [hit["delta"] for hit in json.loads(done.stdout)]


def prompt_stdin(prompt: str, session_id: str = "s1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "prompt": prompt,
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": "/tmp/x.jsonl",
        "cwd": "/tmp",
    }


def stop_stdin(message: str | None, session_id: str = "s1") -> dict[str, Any]:
    data: dict[str, Any] = {
        "session_id": session_id,
        "hook_event_name": "Stop",
        "transcript_path": "/tmp/x.jsonl",
        "cwd": "/tmp",
    }
    if message is not None:
        data["last_assistant_message"] = message
    return data


def session_start_stdin() -> dict[str, Any]:
    return {
        "session_id": "s1",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "transcript_path": "/tmp/x.jsonl",
        "cwd": "/tmp",
    }


@pytest.fixture
def lake_file(tmp_path: Path) -> str:
    return str(tmp_path / "hooks.lake")


def test_scripts_are_executable() -> None:
    for script in ALL_SCRIPTS:
        assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_hooks_json_wires_the_three_events() -> None:
    config = json.loads((HOOKS_DIR / "hooks.json").read_text())
    hooks = config["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop"}
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear|compact"
    expected = {
        "SessionStart": "session-start.sh",
        "UserPromptSubmit": "prompt.sh",
        "Stop": "stop.sh",
    }
    for event, script in expected.items():
        (entry,) = hooks[event][0]["hooks"]
        assert entry["type"] == "command"
        assert entry["command"] == "${CLAUDE_PLUGIN_ROOT}/hooks/" + script
        assert entry["timeout"] == 8


def test_prompt_write_lands_with_tags(lake_file: str) -> None:
    text = "please remember the moss-covered widget bench"
    done = run_hook(PROMPT, prompt_stdin(text), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    (row,) = lake_rows(lake_file)
    assert row["content"] == text
    assert row["source"] == "claude-code"
    assert set(row["tags"]) == {"user", "session:s1"}


def test_automation_label_env_tags_both_write_hooks(lake_file: str) -> None:
    """LAKE_TAGS / LAKE_SOURCE (process env): a headless automated caller labels the rows the prompt and stop hooks
    write, so the host's LAKE_AUTOMATION rules keep them out of consolidation (SPEC §8, digestion phase 1)."""
    env = hook_env(lake_file, LAKE_TAGS="automation, job-watcher,", LAKE_SOURCE="job-watcher")
    assert run_hook(PROMPT, prompt_stdin("# Automated screen: rank these postings"), env).returncode == 0
    assert run_hook(STOP, stop_stdin("[{\"verdict\": \"pass\"}]"), env).returncode == 0
    rows = lake_rows(lake_file)
    assert [r["source"] for r in rows] == ["job-watcher", "job-watcher"]
    assert sorted(sorted(r["tags"]) for r in rows) == [["assistant", "automation", "job-watcher", "session:s1"],
                                                       ["automation", "job-watcher", "session:s1", "user"]]


def test_prompt_slash_command_is_skipped_entirely(lake_file: str) -> None:
    """A "/..." slash command is a CLI command, not speech: the hook neither writes it nor runs a
    recall on its text — even when the lake holds a row whose words the command would match."""
    seeded = "the compact ritual happens at the north garden bench"
    cli(lake_file, "write", seeded, "--source", "claude-code", "--tag", "user", "--tag", "session:other")
    done = run_hook(PROMPT, prompt_stdin("/compact the north garden bench notes"), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""  # no additionalContext: the recall never ran on the command text
    assert [r["content"] for r in lake_rows(lake_file)] == [seeded]  # the command itself was not written


def test_prompt_recall_is_valid_hookspecificoutput(lake_file: str) -> None:
    seeded = "the moss-covered widget bench is in the north garden"
    done = cli(
        lake_file,
        "write", seeded,
        "--source", "claude-code",
        "--tag", "user", "--tag", "session:other",
    )
    assert done.returncode == 0, done.stderr

    done = run_hook(
        PROMPT, prompt_stdin("where is the moss-covered widget bench?"), hook_env(lake_file)
    )
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)  # the whole stdout is one JSON document
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert seeded in hso["additionalContext"]


def test_prompt_dash_prefixed_prompt_is_query_not_flags(lake_file: str) -> None:
    """A prompt starting with "-" must be the recall query, never CLI flags."""
    seeded = "the moss-covered widget bench is in the north garden"
    done = cli(
        lake_file,
        "write", seeded,
        "--source", "claude-code",
        "--tag", "user", "--tag", "session:other",
    )
    assert done.returncode == 0, done.stderr

    text = "--where is the moss-covered widget bench?"
    done = run_hook(PROMPT, prompt_stdin(text), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert seeded in context  # recall worked despite the leading dash
    rows = lake_rows(lake_file)
    assert text in [row["content"] for row in rows]  # the write landed verbatim


def test_prompt_help_flag_prompt_injects_no_usage_text(lake_file: str) -> None:
    """A prompt of exactly "--help" must not inject argparse usage as memory."""
    done = run_hook(PROMPT, prompt_stdin("--help"), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    if done.stdout:
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "usage:" not in context
    (row,) = lake_rows(lake_file)
    assert row["content"] == "--help"


def test_prompt_excludes_own_session_rows(lake_file: str) -> None:
    env = hook_env(lake_file)
    first = run_hook(PROMPT, prompt_stdin("the amethyst lighthouse keeper waved"), env)
    assert first.returncode == 0, first.stderr
    assert len(lake_rows(lake_file)) == 1  # the write landed, so recall had material

    second = run_hook(PROMPT, prompt_stdin("who was the amethyst lighthouse keeper?"), env)
    assert second.returncode == 0, second.stderr
    assert second.stdout == ""  # the only match is this session's own row: excluded


def test_session_start_injects_crystal(lake_file: str) -> None:
    crystal = "I am the identity crystal. I remember the widget work."
    done = cli(lake_file, "write", "a plain seed row about widgets", "--source", "claude-code")
    assert done.returncode == 0, done.stderr
    seed_id = done.stdout.strip()
    done = cli(
        lake_file,
        "write", crystal,
        "--source", "lake",
        "--kind", "crystal",
        "--from", seed_id,
    )
    assert done.returncode == 0, done.stderr

    done = run_hook(SESSION_START, session_start_stdin(), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert crystal in hso["additionalContext"]


def test_session_start_injects_mood_carrier_wave(lake_file: str) -> None:
    """§5.7/§6.2: the SessionStart hook runs `lake system-prompt`, so the newest mood's carrier_wave
    reaches additionalContext — the promise the mood prompt makes to future-you, now kept."""
    carrier = "CARRIER_WAVE_DISTINCT_MARKER"
    done = cli(lake_file, "write", "a plain seed row about widgets", "--source", "claude-code")
    assert done.returncode == 0, done.stderr
    seed_id = done.stdout.strip()
    done = cli(lake_file, "write", "I am the identity crystal.", "--source", "lake",
               "--kind", "crystal", "--from", seed_id)
    assert done.returncode == 0, done.stderr
    mood = json.dumps({"state": "focused", "headline": "h", "subtext": "s", "carrier_wave": carrier})
    done = cli(lake_file, "write", mood, "--source", "lake", "--kind", "mood",
               "--from", seed_id, "--tag", "feeling:focused")
    assert done.returncode == 0, done.stderr

    done = run_hook(SESSION_START, session_start_stdin(), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert carrier in output["hookSpecificOutput"]["additionalContext"]


def test_session_start_fresh_lake_prints_nothing(lake_file: str) -> None:
    done = run_hook(SESSION_START, session_start_stdin(), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_stop_writes_last_assistant_message(lake_file: str) -> None:
    text = "here is what I concluded about the widget bench"
    done = run_hook(STOP, stop_stdin(text), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    (row,) = lake_rows(lake_file)
    assert row["content"] == text
    assert row["source"] == "claude-code"
    assert set(row["tags"]) == {"assistant", "session:s1"}


@pytest.mark.parametrize("message", ["", None], ids=["empty", "absent"])
def test_stop_skips_empty_message(lake_file: str, message: str | None) -> None:
    done = run_hook(STOP, stop_stdin(message), hook_env(lake_file))
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert lake_rows(lake_file) == []


def stdin_for(script: Path) -> dict[str, Any]:
    if script is PROMPT:
        return prompt_stdin("a perfectly ordinary question")
    if script is STOP:
        return stop_stdin("a perfectly ordinary answer")
    return session_start_stdin()


def test_tilde_lake_file_expands_to_home_for_writes(tmp_path: Path) -> None:
    """A literal ~ in LAKE_FILE (settings env does no shell expansion) must
    resolve to $HOME for both writer hooks — never create a "./~" directory
    in the session cwd — matching mcp/server.py's expanduser semantics."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # PYTHONPATH: with HOME moved, a user-site (pip --user -e) install of lake is no longer importable by LAKE_BIN
    env = hook_env("~/lakedir/claude.lake", HOME=str(home), PYTHONPATH=str(HOOKS_DIR.parent.parent))

    done = subprocess.run(
        [str(PROMPT)],
        input=json.dumps(prompt_stdin("the amethyst lighthouse keeper waved")),
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=30,
    )
    assert done.returncode == 0, done.stderr
    done = subprocess.run(
        [str(STOP)],
        input=json.dumps(stop_stdin("noted: the keeper waved back")),
        capture_output=True, text=True, env=env, cwd=str(cwd), timeout=30,
    )
    assert done.returncode == 0, done.stderr

    assert list(cwd.iterdir()) == []  # no literal "~" directory in the cwd
    expanded = home / "lakedir" / "claude.lake"
    assert expanded.exists()
    rows = lake_rows(str(expanded))
    assert {row["content"] for row in rows} == {
        "the amethyst lighthouse keeper waved",
        "noted: the keeper waved back",
    }


def test_tilde_lake_file_expands_to_home_for_session_start(tmp_path: Path) -> None:
    """SessionStart must read the same expanded lake the writers use."""
    home = tmp_path / "home"
    (home / "lakedir").mkdir(parents=True)
    expanded = home / "lakedir" / "claude.lake"
    crystal = "I am the identity crystal. I remember the widget work."
    done = cli(str(expanded), "write", "a plain seed row about widgets", "--source", "claude-code")
    assert done.returncode == 0, done.stderr
    seed_id = done.stdout.strip()
    done = cli(
        str(expanded), "write", crystal, "--source", "lake", "--kind", "crystal", "--from", seed_id
    )
    assert done.returncode == 0, done.stderr

    env = hook_env("~/lakedir/claude.lake", HOME=str(home))
    done = run_hook(SESSION_START, session_start_stdin(), env)
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    assert crystal in output["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_fail_open_hooks_off(lake_file: str, script: Path) -> None:
    env = hook_env(lake_file, LAKE_HOOKS_OFF="1")
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert not Path(lake_file).exists()  # nothing touched the lake


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_fail_open_missing_lake_bin(lake_file: str, script: Path) -> None:
    env = hook_env(lake_file, LAKE_BIN="/nonexistent/lake-bin-49204")
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def no_lake_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An environment where the hook's python3 cannot import lake: no PYTHONPATH, no user site, and a PATH without
    ~/.local/bin, so no installed `lake` (least of all a live one) can run."""
    env = {k: v for k, v in base_env().items() if k not in ("PYTHONPATH", "LAKE_BIN")}
    env.update(PYTHONNOUSERSITE="1", PATH="/usr/bin:/bin", HOME=str(tmp_path / "home"), **extra)
    return env


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_lake_not_importable_loads_the_checkouts_resolver(tmp_path: Path, script: Path) -> None:
    """§13.8 LAKE_BIN, step 2: with lake not importable, hook.py loads <plugin>/../lake/_env.py by path and still
    honours the env file's LAKE_BIN (the process names no lake)."""
    probe = tmp_path / "probe.py"
    probe.write_text(f"import sys; sys.path.insert(0, {str(HOOKS_DIR.parent.parent)!r})\n" + ENV_PROBE, encoding="utf-8")
    out = tmp_path / "probe.jsonl"
    env = no_lake_env(tmp_path, PROBE_OUT=str(out),
                      LAKE_ENV_FILE=write_env_file(tmp_path, f"LAKE_BIN={sys.executable} {probe}\nLAKE=/x.lake\n"))
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0 and done.stdout == "", done.stderr
    calls = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert calls and all(c["target"] == "/x.lake" for c in calls)


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_copied_plugin_without_lake_fails_open(tmp_path: Path, script: Path) -> None:
    """Step 3: a copied plugin (no ../lake beside it) with no importable lake runs `lake` from PATH and never the env
    file's LAKE_BIN; with no `lake` on PATH it stays silent (exit 0, no stdout)."""
    copy = tmp_path / "plugin" / "hooks"
    copy.mkdir(parents=True)
    for f in HOOKS_DIR.iterdir():
        if f.is_file():
            (copy / f.name).write_bytes(f.read_bytes())
            (copy / f.name).chmod(f.stat().st_mode)
    marker = tmp_path / "hostile-bin-ran"
    hostile = tmp_path / "hostile_bin.py"
    hostile.write_text(f"open({str(marker)!r}, 'a').write('ran\\n')\n", encoding="utf-8")
    env = no_lake_env(tmp_path, LAKE_ENV_FILE=write_env_file(tmp_path, f"LAKE_BIN={sys.executable} {hostile}\n"))
    done = run_hook(copy / script.name, stdin_for(script), env)
    assert done.returncode == 0 and done.stdout == "", done.stderr
    assert not marker.exists()


def test_older_cli_without_default_still_gets_the_write(tmp_path: Path) -> None:
    """A split install: LAKE_BIN names a CLI older than --default (it exits 2 on the flag, as argparse does). The hook
    repeats the call without it, so the write still lands instead of failing open silently."""
    lake_file = str(tmp_path / "old.lake")
    old = tmp_path / "old_cli.py"
    old.write_text("import sys\nif '--default' in sys.argv:\n    sys.exit(print('lake: error: unrecognized arguments:"
                   " --default ~/.lake/claude.lake', file=sys.stderr) or 2)\nfrom lake.cli import main\n"
                   "sys.exit(main())\n", encoding="utf-8")
    done = run_hook(PROMPT, stdin_for(PROMPT), hook_env(lake_file, LAKE_BIN=f"{sys.executable} {old}"))
    assert done.returncode == 0, done.stderr
    assert [r["content"] for r in lake_rows(lake_file)] == ["a perfectly ordinary question"]


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_fail_open_unwritable_lake_file(tmp_path: Path, script: Path) -> None:
    env = hook_env(str(tmp_path))  # LAKE_FILE is a directory: every open fails
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


# --- the §13.8 env file ---------------------------------------------------------------------------


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_fail_open_dead_lake_url_from_env_file(tmp_path: Path, script: Path) -> None:
    """SPEC §13.9: an env file naming an unreachable LAKE_URL must leave every
    hook silent — exit 0, no stdout — and touch nothing outside HOME."""
    home = tmp_path / "home"
    home.mkdir()
    env = base_env()
    env["HOME"] = str(home)
    env["LAKE_BIN"] = LAKE_BIN
    env["LAKE_ENV_FILE"] = write_env_file(
        tmp_path, f"LAKE_URL=http://127.0.0.1:{dead_port()}\n"
    )
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""


def test_env_file_names_lake_file_hooks_write_there(tmp_path: Path) -> None:
    """SPEC §13.8: LAKE_FILE and LAKE_BIN resolve through the env file when the
    process environment does not name them; comments, an `export ` prefix,
    quoted values, and a malformed line all parse per the grammar."""
    target = tmp_path / "from-env-file.lake"
    env = base_env()
    env["LAKE_ENV_FILE"] = write_env_file(
        tmp_path,
        "# lake env for the hook tests\n"
        f'export LAKE_FILE="{target}"\n'
        f"LAKE_BIN='{LAKE_BIN}'\n"
        "this line fits no rule and is ignored\n",
    )
    done = run_hook(PROMPT, prompt_stdin("the copper kettle sings at dawn"), env)
    assert done.returncode == 0, done.stderr
    done = run_hook(STOP, stop_stdin("noted: the kettle sings"), env)
    assert done.returncode == 0, done.stderr
    rows = lake_rows(str(target))
    assert {row["content"] for row in rows} == {
        "the copper kettle sings at dawn",
        "noted: the kettle sings",
    }


def test_process_env_wins_over_env_file(tmp_path: Path) -> None:
    """SPEC §13.8 precedence: LAKE_FILE in the process environment beats the
    env file's; the env file's lake is never created."""
    file_lake = tmp_path / "named-by-env-file.lake"
    process_lake = tmp_path / "named-by-process-env.lake"
    env = hook_env(
        str(process_lake),
        LAKE_ENV_FILE=write_env_file(tmp_path, f"LAKE_FILE={file_lake}\n"),
    )
    done = run_hook(PROMPT, prompt_stdin("the copper kettle sings at dawn"), env)
    assert done.returncode == 0, done.stderr
    assert not file_lake.exists()
    (row,) = lake_rows(str(process_lake))
    assert row["content"] == "the copper kettle sings at dawn"


# --- §13.8 target: the hooks hand the CLI an unmodified environment ------------------------------

# A stand-in LAKE_BIN that records what the real CLI would resolve from what the hook hands it (the
# inherited environment plus --default), through the same lake.resolve(), and opens no lake at all.
ENV_PROBE = """\
import json, os, sys
from lake._env import resolve
args = sys.argv[1:]
t = resolve(default=args[args.index("--default") + 1])
with open(os.environ["PROBE_OUT"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"target": t.target, "remote": t.remote, "token": t.token, "argv": args}) + "\\n")
"""


def probe_targets(tmp_path: Path, script: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    """Run `script` with LAKE_BIN pointed at ENV_PROBE; return one dict per CLI
    call it made, each the target that call would have routed to."""
    probe = tmp_path / "env_probe.py"
    probe.write_text(ENV_PROBE, encoding="utf-8")
    out = tmp_path / "probe.jsonl"
    out.unlink(missing_ok=True)
    env = {**env, "LAKE_BIN": f"{sys.executable} {probe}", "PROBE_OUT": str(out),
           "PYTHONPATH": str(HOOKS_DIR.parent.parent)}
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    calls = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert calls  # every hook calls the CLI at least once
    return calls


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_process_lake_file_outranks_env_file_url(tmp_path: Path, script: Path) -> None:
    """SPEC §13.8: an explicit LAKE_FILE (or LAKE) in the process environment is not overridden by a
    URL that only the env file names; the env file's token does not travel with it."""
    home = tmp_path / "home"
    home.mkdir()
    mine = str(tmp_path / "mine.lake")
    env_file = write_env_file(tmp_path, f"LAKE_URL=http://127.0.0.1:{dead_port()}\nLAKE_TOKEN=operator-token\n")
    for key in ("LAKE_FILE", "LAKE"):
        env = {**base_env(), key: mine, "HOME": str(home), "LAKE_ENV_FILE": env_file}
        for call in probe_targets(tmp_path, script, env):
            assert (call["target"], call["remote"], call["token"]) == (mine, False, None)
            assert call["argv"][:2] == ["--default", "~/.lake/claude.lake"]


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_process_lake_url_still_wins(tmp_path: Path, script: Path) -> None:
    """A URL in the process environment (LAKE_URL or LAKE) wins over the env file's target (URL or
    file), and the env file's LAKE_TOKEN is not handed to it."""
    home = tmp_path / "home"
    home.mkdir()
    url = f"http://127.0.0.1:{dead_port()}"
    env_file = write_env_file(tmp_path, f"LAKE_URL=http://127.0.0.1:{dead_port()}\n"
                              f"LAKE_FILE={tmp_path / 'env-file.lake'}\nLAKE_TOKEN=from-env-file\n")
    for key in ("LAKE_URL", "LAKE"):
        env = {**base_env(), key: url, "HOME": str(home), "LAKE_ENV_FILE": env_file}
        for call in probe_targets(tmp_path, script, env):
            assert (call["target"], call["remote"], call["token"]) == (url, True, None)


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_env_file_only_target_still_resolves(tmp_path: Path, script: Path) -> None:
    """With no target in the process environment, the env file's lake (LAKE_URL today, LAKE after the
    rename) and its LAKE_TOKEN reach the CLI, as on an operator machine; with none at all, the default."""
    home = tmp_path / "home"
    home.mkdir()
    url = f"http://127.0.0.1:{dead_port()}"
    for key in ("LAKE_URL", "LAKE"):
        env = {**base_env(), "HOME": str(home), "LAKE_ENV_FILE": write_env_file(tmp_path, f"{key}={url}/\nLAKE_TOKEN=tok\n")}
        for call in probe_targets(tmp_path, script, env):
            assert (call["target"], call["remote"], call["token"]) == (url, True, "tok")
    env = {**base_env(), "HOME": str(home)}
    for call in probe_targets(tmp_path, script, env):
        assert (call["target"], call["remote"]) == (str(home / ".lake" / "claude.lake"), False)  # the plugin default


@pytest.mark.parametrize("script", ALL_SCRIPTS, ids=lambda s: s.name)
def test_env_file_lake_bin_ignored_when_process_names_target(tmp_path: Path, script: Path) -> None:
    """With LAKE_FILE in the process environment the env file's LAKE_BIN is not
    run: it may be a wrapper that pins the env file's own lake."""
    marker = tmp_path / "hostile-bin-ran"
    hostile = tmp_path / "hostile_bin.py"
    hostile.write_text(f"open({str(marker)!r}, 'a').write('ran\\n')\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    mine = tmp_path / "mine.lake"
    env = hook_env(
        str(mine),
        HOME=str(home),
        LAKE_ENV_FILE=write_env_file(tmp_path, f"LAKE_BIN={sys.executable} {hostile}\n"),
    )
    del env["LAKE_BIN"]
    done = run_hook(script, stdin_for(script), env)
    assert done.returncode == 0, done.stderr
    assert not marker.exists()


def test_process_lake_file_write_lands_despite_env_file_url(tmp_path: Path) -> None:
    """End to end through the real CLI: with a dead LAKE_URL in the env file and
    LAKE_FILE in the process environment, the prompt lands in the file and
    nothing is spooled for the URL."""
    home = tmp_path / "home"
    home.mkdir()
    mine = tmp_path / "mine.lake"
    env = hook_env(
        str(mine),
        HOME=str(home),
        LAKE_ENV_FILE=write_env_file(tmp_path, f"LAKE_URL=http://127.0.0.1:{dead_port()}\n"),
    )
    done = run_hook(PROMPT, prompt_stdin("the copper kettle sings at dawn"), env)
    assert done.returncode == 0, done.stderr
    (row,) = lake_rows(str(mine))
    assert row["content"] == "the copper kettle sings at dawn"
    assert not (home / ".lake").exists()
    assert not (tmp_path / "spool.jsonl").exists()
