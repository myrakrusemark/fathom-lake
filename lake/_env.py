"""Where the lake is (SPEC §13.8): the env-file grammar, the one target resolver, per-key settings.

A leaf: stdlib only and no import from the package, so a host that cannot import `lake` (a plugin hook)
can load this file by path. `Lake`, `RemoteLake` and `lake.open(target)` never call it; `lake.open()` with
no target and the hosts (CLI, serve, MCP server, hooks) do.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
TARGET_KEYS = ("LAKE", "LAKE_URL", "LAKE_FILE")  # within one source, the first one set wins
SOURCE_KEYS = ("LAKE_COLLAPSE_SOURCES", "LAKE_EXCLUDE_SOURCES")  # deprecated: now source: automation rules
DEFAULT_AUTOMATION = ("tag:automation",)  # §4.3: Lake(automation=None), and every host's default
DEFAULT_THINK = "claude:--model claude-opus-5-5[1m]"  # §13.8: used only where a host runs digestion
StrPath = str | os.PathLike[str]


@dataclass(frozen=True)
class Target:
    """A resolved lake: an expanded path, or an http(s) URL without a trailing '/'."""

    target: str
    remote: bool
    token: str | None
    source: str  # "argument" | "environment" | "env file" | "default"
    notes: tuple[str, ...] = ()  # deprecation lines for the host to print; the library prints nothing


def env_file_path(environ: Mapping[str, str] | None = None) -> Path:
    """`LAKE_ENV_FILE`, else ~/.lake/env."""
    named = (os.environ if environ is None else environ).get("LAKE_ENV_FILE")
    return Path(named) if named else Path.home() / ".lake" / "env"


def read_env_file(path: StrPath | None = None) -> dict[str, str]:
    """KEY=VALUE lines from `path` (default env_file_path(): LAKE_ENV_FILE, else ~/.lake/env) under the §13.8
    grammar; fail open: a missing file is {}, a line that fits no rule is ignored."""
    try:
        text = Path(env_file_path() if path is None else path).read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()  # §13.8: strip both ends so shell `source` and every reader agree (a
        if not line or line.startswith("#"):  # trailing space must not survive to defeat the quote-strip)
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        if not sep or not ENV_KEY_RE.match(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value  # a later line for the same key wins
    return out


def is_url(target: object) -> bool:
    """§13.6 factory rule: the exact lower-case prefix http:// or https://."""
    return isinstance(target, str) and target.startswith(("http://", "https://"))


def named(entries: Mapping[str, str]) -> tuple[str, str] | None:
    """The (key, value) that names the lake in one source: LAKE, else LAKE_URL, else LAKE_FILE; "" is unset."""
    return next(((k, entries[k]) for k in TARGET_KEYS if entries.get(k)), None)


def sources(environ: Mapping[str, str] | None, env_file: StrPath | None) -> tuple[Mapping[str, str], dict[str, str]]:
    """(the process environment, the env file's entries); `environ` and `env_file` are injectable."""
    env = os.environ if environ is None else environ
    return env, read_env_file(env_file_path(env) if env_file is None else env_file)


def resolve(target: StrPath | None = None, *, token: str | None = None, default: StrPath | None = None,
            environ: Mapping[str, str] | None = None, env_file: StrPath | None = None) -> Target:
    """§13.8 precedence: the explicit `target` > the process environment > the env file > `default`; the
    first source that names a lake wins whole. The token: `token`, else the process LAKE_TOKEN, else the env
    file's, the last only for the env file's own URL. ValueError when nothing names a lake."""
    env, stored = sources(environ, env_file)
    notes: tuple[str, ...] = ()
    if target is not None and str(target):
        value, source = str(target), "argument"
    elif hit := named(env) or named(stored):
        (key, value), source = hit, "environment" if named(env) else "env file"
        notes = () if key == "LAKE" else (f"{key} is deprecated; use LAKE=<the same value>",)
    elif default is not None and str(default):
        value, source = str(default), "default"
    else:
        raise ValueError("no lake: pass a target or set LAKE")
    if not is_url(value):
        return Target(os.path.expanduser(value), False, None, source, notes)
    url, own = value.rstrip("/"), named(stored)
    mine = own is not None and own[1].rstrip("/") == url  # a bearer from the file goes only to the file's own URL
    tok = token or env.get("LAKE_TOKEN") or (stored.get("LAKE_TOKEN") if mine else None) or None
    return Target(url, True, tok, source, notes)


def setting(name: str, *, environ: Mapping[str, str] | None = None, env_file: StrPath | None = None) -> str | None:
    """One key, per key: the process environment, else the env file; "" counts as unset."""
    env, stored = sources(environ, env_file)
    return env.get(name) or stored.get(name) or None


@dataclass(frozen=True)
class Config:
    """What a host opens a local lake with (§13.8): the think and embed specs, the automation rules, notes."""

    think: str | None
    embed: str | None
    automation: tuple[str, ...]
    notes: tuple[str, ...] = ()


def config(think: str | None = None, embed: str | None = None, automation: Sequence[str] | None = None, *,
           digest: bool = False, environ: Mapping[str, str] | None = None, env_file: StrPath | None = None) -> Config:
    """§13.8, the one rule every host (lake.open(), the CLI, lake serve) configures a local lake by. think: the
    given spec, else LAKE_THINK, else DEFAULT_THINK when the host runs digestion (`digest`); embed: the given
    spec, else LAKE_EMBED. automation: the given rules, else LAKE_AUTOMATION (unset: DEFAULT_AUTOMATION; set but
    empty: none) plus LAKE_COLLAPSE_SOURCES / LAKE_EXCLUDE_SOURCES as source: rules (deprecated, with a note)."""
    env, stored = sources(environ, env_file)
    raw = env["LAKE_AUTOMATION"] if "LAKE_AUTOMATION" in env else stored.get("LAKE_AUTOMATION")
    rules = list(automation) if automation is not None else list(DEFAULT_AUTOMATION) if raw is None else [
        r.strip() for r in raw.split(",") if r.strip()]
    notes = []
    for key in SOURCE_KEYS if automation is None else ():  # given rules replace the environment's wholly
        if names := [s.strip() for s in (env.get(key) or stored.get(key) or "").split(",") if s.strip()]:
            rules += [f"source:{s}" for s in names]
            hide = "; pass exclude_sources per call to hide them" if key == "LAKE_EXCLUDE_SOURCES" else ""
            notes.append(f"{key} is deprecated: these sources are now automation (searchable, never consolidated);"
                         f" use LAKE_AUTOMATION=source:<name>{hide}")
    think = think or env.get("LAKE_THINK") or stored.get("LAKE_THINK") or (DEFAULT_THINK if digest else None)
    return Config(think, embed or env.get("LAKE_EMBED") or stored.get("LAKE_EMBED") or None, tuple(rules), tuple(notes))
