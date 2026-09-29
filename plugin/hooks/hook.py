"""The plugin's three hooks in one file: `hook.py session-start|prompt|stop`, run by the .sh files of those names
(and `hook.py lake-bin`, the resolved CLI for scripts/lake-consolidate.sh).

session-start  inject the crystal and recent moods (`lake system-prompt --budget 4000`, SPEC §5.7).
prompt         write the prompt (user + session:<id>, plus LAKE_TAGS), then inject recall for it, without this
               session's own rows and without the crystal. A "/..." prompt is a command, not speech: skipped.
stop           write the finished turn (last_assistant_message; assistant + session:<id>).

The hook never resolves the lake: it runs the CLI with `--default ~/.lake/claude.lake` and an unmodified
environment, and the CLI resolves target and token with lake.resolve() like every other host (SPEC §13.8).
The one thing it needs first is the CLI itself, LAKE_BIN (a multi-word command is split with shlex).

Fail-open: any error exits 0 with no stdout. A broken memory must never block a session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType

DEFAULT_LAKE = "~/.lake/claude.lake"


def env_module() -> ModuleType | None:
    """lake/_env.py: from an importable lake, else by path from the checkout this plugin lives in (a leaf,
    safe to load alone), else None (a copied plugin with a venv-only lake)."""
    try:
        import lake._env

        return lake._env
    except Exception:
        pass
    path = Path(__file__).resolve().parents[2] / "lake" / "_env.py"
    spec = importlib.util.spec_from_file_location("lake_env", path)
    if spec is None or spec.loader is None or not path.is_file():
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lake_env"] = mod  # dataclasses resolves the module by name
    spec.loader.exec_module(mod)
    return mod


def lake_bin() -> list[str]:
    """SPEC §13.8: the process LAKE_BIN; else the env file's, only when the process environment names no lake
    (a wrapper there may pin the env file's own lake); else `lake`."""
    if os.environ.get("LAKE_BIN"):
        return shlex.split(os.environ["LAKE_BIN"])
    env = env_module()
    if env is not None and env.named(os.environ) is None:
        return shlex.split(env.read_env_file(env.env_file_path()).get("LAKE_BIN") or "lake")
    return ["lake"]


def cli(*args: str, stdin: str | None = None, timeout: float) -> subprocess.CompletedProcess[str]:
    """The CLI with the plugin's default file. A CLI older than --default (a split install: LAKE_BIN names an older
    tree) rejects it with exit 2; it resolved the lake itself, so the call is repeated without it."""
    def run(extra: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run([*lake_bin(), *extra, *args], input=stdin, capture_output=True, text=True,
                              timeout=timeout, check=False)
    done = run(["--default", DEFAULT_LAKE])
    return run([]) if done.returncode == 2 and "--default" in done.stderr else done


def extra_tags() -> list[str]:
    """LAKE_TAGS (comma-separated, process environment only): extra tags on every row a hook writes, so an automated
    caller (a headless `claude -p` job) can label its rows, e.g. LAKE_TAGS=automation; the host's automation rules
    then keep them out of consolidation while recall still finds them."""
    tags = [t.strip() for t in (os.environ.get("LAKE_TAGS") or "").split(",")]
    return [x for t in tags if t for x in ("--tag", t)]


def source() -> str:
    return os.environ.get("LAKE_SOURCE") or "claude-code"


def emit(event: str, block: str) -> None:
    if block.strip():
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": block.strip()}}))


def session_start(data: dict[str, object]) -> None:
    done = cli("system-prompt", "--budget", "4000", timeout=6)
    if done.returncode == 0:
        emit("SessionStart", done.stdout)  # a fresh lake has no crystal: say nothing


def prompt(data: dict[str, object]) -> None:
    text, session = data.get("prompt"), data.get("session_id")
    if not isinstance(text, str) or not isinstance(session, str) or not text or not session or text.startswith("/"):
        return
    tag = "session:" + session
    cli("write", "-", "--source", source(), "--tag", "user", "--tag", tag, *extra_tags(), stdin=text, timeout=3)
    # "--" so a dash-prefixed prompt ("--help", "-v") is the query, never parsed as flags
    done = cli("context", "--no-crystal", "--exclude-tag", tag, "--budget", "24000", "--", text, timeout=4)
    if done.returncode == 0:
        emit("UserPromptSubmit", done.stdout)


def stop(data: dict[str, object]) -> None:
    text, session = data.get("last_assistant_message"), data.get("session_id")
    if isinstance(text, str) and isinstance(session, str) and text and session:  # nothing said, nothing written
        cli("write", "-", "--source", source(), "--tag", "assistant", "--tag", "session:" + session, *extra_tags(),
            stdin=text, timeout=6)


def main(event: str) -> None:
    if event == "lake-bin":  # for lake-consolidate.sh: the CLI by the same rule, one word per line
        print("\n".join(lake_bin()))
        return
    raw = sys.stdin.read()
    data = json.loads(raw) if event != "session-start" and raw.strip() else {}
    {"session-start": session_start, "prompt": prompt, "stop": stop}[event](data if isinstance(data, dict) else {})


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except Exception:
        pass  # fail open
