"""Callback adapters for --think and --embed specs (SPEC §8). Imported by cli.py, serve.py and lake.open()."""

from __future__ import annotations

import json as _json
import math
import os
import re
import shlex
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ._types import LakeError

# §8: `--setting-sources ""`, `--strict-mcp-config` (no --mcp-config), `--tools ""`, and a fresh temp cwd per call keep
# the host's CLAUDE.md, auto-memory, MCP server instructions, skills and tools out of a think call; the account email
# and the date/environment block still reach the model under OAuth (no flag removes them; SPEC §8).
CLAUDE_ARGS: tuple[str, ...] = (
    "claude", "-p", "--output-format", "text", "--settings", '{"hooks": {}}', "--setting-sources", "",
    "--strict-mcp-config", "--tools", "", "--no-session-persistence", "--disable-slash-commands",
)
JSON_LINE = "Respond with only a JSON object."
CMD_CLAUDE = (
    "cmd:claude … never receives the lake's system prompt and gets none of the claude adapter's isolation from the"
    " host's context (CLAUDE.md, memory, MCP servers, tools); use claude:<args> (e.g. claude:--model sonnet)"
)
WRAPPERS = ("env", "exec", "command", "nice", "nohup", "timeout", "stdbuf", "ionice", "setsid", "time")
SHELLS = ("sh", "bash", "dash", "zsh")
ARG_FLAGS = ("-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-n", "-s", "--signal", "-k", "--kill-after",
             "-c", "--class", "-i", "-e", "-o")  # wrapper flags (env, nice, timeout, ionice, stdbuf) taking a word
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


class CmdClaudeError(ValueError):
    """§8: the refused `cmd:claude …` spec; `lake serve` logs it and serves with no think (§13.2)."""


def _parse_ollama(spec: str) -> tuple[str, str]:
    """`ollama:<model>@<url>` -> (model, url without a trailing slash)."""
    model, sep, url = spec[len("ollama:"):].rpartition("@")
    if not sep or not model or not url:
        raise ValueError(f"expected ollama:<model>@<url>, got {spec!r}")
    return model, url.rstrip("/")


def _post(url: str, body: dict[str, Any], timeout: float, err: type[Exception]) -> dict[str, Any]:
    """POST a JSON body and return the parsed JSON object; any failure raises `err`."""
    data = _json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise err(f"{url}: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
    except (OSError, ValueError) as exc:  # URLError, a timeout, bad JSON
        raise err(f"{url}: {exc}") from exc
    if not isinstance(out, dict):
        raise err(f"{url}: expected a JSON object in the response")
    return out


def _run(
    cmd: str | list[str], stdin: str, env: dict[str, str], timeout: float, err: type[Exception], *, cwd: str | None = None
) -> str:
    """Run a shell command (str) or argv (list) with `stdin` on its input; stdout is the answer."""
    name = cmd if isinstance(cmd, str) else cmd[0]
    try:
        proc = subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str),
            env={**os.environ, **env}, cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise err(f"{name}: {exc}") from exc
    if proc.returncode != 0:
        raise err(f"{name} exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def _maybe_json(text: str) -> str | dict[str, Any]:
    """The parsed object when `text` is a JSON object, else the text (the library's §4.9 cleaning retries)."""
    try:
        obj = _json.loads(text)
    except ValueError:
        return text
    return obj if isinstance(obj, dict) else text


def _vectors(obj: object) -> list[list[float]]:
    if not isinstance(obj, list) or not all(isinstance(v, list) for v in obj):
        raise RuntimeError("embed output is not a JSON list of float lists")
    return [[float(x) for x in v] for v in obj]


def _first_word(command: str) -> str:
    """The basename of a shell command's first command word, skipping `VAR=value` assignments, common wrappers
    (WRAPPERS, with their flags, a flag's argument (ARG_FLAGS) and timeout's duration) and looking inside
    `sh -c '…'`; comments are dropped; "" when the text does not split. Best-effort, not a parser."""
    try:
        words = shlex.split(command, comments=True)
    except ValueError:
        return ""
    i = 0
    while i < len(words):
        name = os.path.basename(words[i])
        if ASSIGN_RE.match(words[i]):
            i += 1
        elif name in SHELLS:
            j = next((j for j in range(i + 1, len(words)) if not words[j].startswith("-") or "c" in words[j][1:]), len(words))
            return _first_word(words[j + 1]) if j + 1 < len(words) and words[j].startswith("-") else name
        elif name in WRAPPERS:
            i += 1
            while i < len(words) and (ASSIGN_RE.match(words[i]) or words[i].startswith("-")):
                i += 2 if words[i] in ARG_FLAGS else 1
            i += 1 if name == "timeout" else 0
        else:
            return name
    return ""


def _names_system(command: str) -> bool:
    """Whether the command text, comments dropped, mentions LAKE_SYSTEM."""
    try:
        return "LAKE_SYSTEM" in " ".join(shlex.split(command, comments=True))
    except ValueError:
        return "LAKE_SYSTEM" in command


def _claude(extra: list[str], timeout: float) -> Callable[..., str | dict[str, Any]]:
    """The built-in claude adapter: CLAUDE_ARGS, the extra CLI args, then `--system-prompt`; each call runs in a
    fresh empty temp directory (no project CLAUDE.md, no auto-memory, no git status)."""

    def claude(prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
        argv = [*CLAUDE_ARGS, *extra] + ([] if system is None else ["--system-prompt", system])
        text = prompt.rstrip("\n") + "\n" + JSON_LINE if json else prompt
        with tempfile.TemporaryDirectory(prefix="lake-think-") as cwd:
            out = _run(argv, text, {"LAKE_HOOKS_OFF": "1"}, timeout, LakeError, cwd=cwd)
        return _maybe_json(out) if json else out

    claude.operator = operator_names()  # type: ignore[attr-defined]  # §6.3 operator guard
    return claude


def operator_names(path: str | os.PathLike[str] | None = None) -> tuple[str, ...]:
    """What the account context shows `claude -p` about its operator (§8): ~/.claude.json's oauthAccount
    displayName, fullName and emailAddress (and its local part), words of 3+ characters; () when unreadable."""
    try:
        with open(path or os.path.expanduser("~/.claude.json"), encoding="utf-8") as f:
            acct = _json.load(f)["oauthAccount"]
        values = [str(acct.get(k) or "") for k in ("displayName", "fullName", "emailAddress")]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ()
    words = {w for v in values for w in [v, v.split("@")[0], *re.split(r"[\s,]+", v)] if len(w) >= 3}
    return tuple(sorted(words))


def make_think(
    spec: str, *, timeout: float = 1800.0, num_ctx: int | None = None, num_predict: int | None = None
) -> Callable[..., str | dict[str, Any]]:
    """§8 --think: `ollama:<model>@<url>`, `claude`, `claude:<args>`, or `cmd:<shell command>` as a think callback."""
    if spec.startswith("ollama:"):
        model, url = _parse_ollama(spec)

        def ollama(prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
            chars = len((system or "") + prompt)
            options = {
                "num_ctx": num_ctx or max(8192, math.ceil(chars / 3) + 2048),
                "num_predict": num_predict or (1024 if json else 4096),
            }
            body: dict[str, Any] = {
                "model": model, "prompt": prompt, "system": system or "", "stream": False, "think": False,
                "options": options,
            }
            if json:
                body["format"] = "json"
            out = _post(f"{url}/api/generate", body, timeout, LakeError)
            count = out.get("prompt_eval_count")
            if isinstance(count, int) and count < chars / 8:
                raise LakeError(f"ollama truncated the prompt: {count} tokens evaluated for {chars} characters")
            return str(out.get("response", ""))

        return ollama
    if spec == "claude":
        return _claude([], timeout)
    if spec.startswith("claude:"):
        return _claude(shlex.split(spec[len("claude:"):]), timeout)
    if spec.startswith("cmd:"):
        command = spec[len("cmd:"):]
        if _first_word(command) == "claude" and not _names_system(command):
            raise CmdClaudeError(CMD_CLAUDE)

        def shell(prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
            env = {"LAKE_SYSTEM": system or "", "LAKE_HOOKS_OFF": "1", **({"LAKE_JSON": "1"} if json else {})}
            return _run(command, prompt, env, timeout, LakeError)

        return shell
    raise ValueError(
        f"unknown --think spec {spec!r}: expected ollama:<model>@<url>, claude, claude:<args>, or cmd:<command>"
    )


def make_embed(spec: str, *, timeout: float = 10.0) -> Callable[[list[str]], list[list[float]]]:
    """§8 --embed: `ollama:<model>@<url>` or `cmd:<shell command>` as an embed callback. Failures raise
    RuntimeError, never LakeError, so recall()/context() turn them into the FTS-only warning (§5.2)."""
    if spec.startswith("ollama:"):
        model, url = _parse_ollama(spec)

        def ollama(texts: list[str]) -> list[list[float]]:
            out = _post(f"{url}/api/embed", {"model": model, "input": list(texts)}, timeout, RuntimeError)
            return _vectors(out.get("embeddings"))

        return ollama
    if spec.startswith("cmd:"):
        command = spec[len("cmd:"):]

        def shell(texts: list[str]) -> list[list[float]]:
            out = _run(command, _json.dumps(list(texts)), {}, timeout, RuntimeError)
            try:
                return _vectors(_json.loads(out))
            except ValueError as exc:
                raise RuntimeError(f"{command}: embed output is not JSON: {exc}") from exc

        return shell
    raise ValueError(f"unknown --embed spec {spec!r}: expected ollama:<model>@<url> or cmd:<command>")


def model_name(spec: str) -> str | None:
    """The model a --think spec names (the default for --model-name), or None for `cmd:`."""
    if spec.startswith("ollama:"):
        return _parse_ollama(spec)[0]
    if spec.startswith("claude:"):
        return " ".join(["claude", *shlex.split(spec[len("claude:"):])])
    return "claude" if spec == "claude" else None
