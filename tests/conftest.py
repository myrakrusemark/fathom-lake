"""Shared fixtures: temp-file lakes, a frozen clock, fake think/embed callbacks, row helpers,
and the §13 serve harness (a real `lake serve` subprocess) shared by test_serve and test_remote."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lake import Delta, Lake
from lake._time import parse_timespec

DEFAULT_NOW = "2026-09-02T18:00:00.000Z"
EMBED_DIM = 16
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_lake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Safety net: a test must never reach a real server or a real model. Mask LAKE and every LAKE_* key the
    developer's shell exports (the target, the token, LAKE_THINK / LAKE_EMBED, the automation and plugin keys) and
    point LAKE_ENV_FILE at a nonexistent path so neither the CLI nor the hooks read a real ~/.lake/env
    (SPEC §13.8). Tests that need a key set it themselves, after this runs; LAKE_WRITE_GOLDEN (a test switch) stays."""
    for key in [k for k in os.environ if (k == "LAKE" or k.startswith("LAKE_")) and k != "LAKE_WRITE_GOLDEN"]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("LAKE_ENV_FILE", "/nonexistent/lake-test-env")


class FrozenClock:
    """A settable clock; returns aware UTC datetimes. Pass as Lake(clock=clock)."""

    def __init__(self, at: datetime | str = DEFAULT_NOW) -> None:
        self.at = self._parse(at)

    def _parse(self, at: datetime | str) -> datetime:
        return parse_timespec(at, datetime.now(UTC)) if isinstance(at, str) else at.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.at

    def set(self, at: datetime | str) -> None:
        self.at = self._parse(at)

    def advance(self, seconds: float = 0.0, **kw: float) -> None:
        """Move forward by seconds and/or timedelta keywords (minutes=, hours=, days=)."""
        self.at += timedelta(seconds=seconds, **kw)


class FakeThink:
    """Returns canned answers in order and records every call as (prompt, system, json)."""

    def __init__(self, answers: Sequence[str | dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[str, str | None, bool]] = []

    def __call__(self, prompt: str, *, system: str | None = None, json: bool = False) -> str | dict[str, Any]:
        self.calls.append((prompt, system, json))
        if not self.answers:
            raise AssertionError(f"fake_think: no canned answer left for call {len(self.calls)}")
        return self.answers.pop(0)

    @property
    def prompts(self) -> list[str]:
        return [c[0] for c in self.calls]

    @property
    def systems(self) -> list[str | None]:
        return [c[1] for c in self.calls]


def hash_embed(texts: Sequence[str], dim: int = EMBED_DIM) -> list[list[float]]:
    """Deterministic word-hash embedding: each token adds ±1 to one of `dim` slots; L2-normalised."""
    out: list[list[float]] = []
    for text in texts:
        vec = [0.0] * dim
        for tok in re.findall(r"[a-z0-9']+", text.lower()):
            h = hashlib.sha256(tok.encode("utf-8")).digest()
            vec[h[0] % dim] += 1.0 if h[1] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            vec[0], norm = 1.0, 1.0
        out.append([x / norm for x in vec])
    return out


class FakeEmbed:
    """hash_embed as a callable that records its calls; `fail` makes every call raise it;
    `dim` other than 16 produces a wrong-length vector for the mismatch tests."""

    def __init__(self, dim: int = EMBED_DIM, fail: BaseException | None = None) -> None:
        self.dim = dim
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        return hash_embed(texts, self.dim)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def fake_embed() -> FakeEmbed:
    return FakeEmbed()


@pytest.fixture
def raising_embed() -> FakeEmbed:
    return FakeEmbed(fail=RuntimeError("embed down"))


@pytest.fixture
def fake_think() -> Callable[[Sequence[str | dict[str, Any]]], FakeThink]:
    """Factory: fake_think([answer, answer, ...]) -> FakeThink."""
    return FakeThink


@pytest.fixture
def make_lake(tmp_path: Path, clock: FrozenClock) -> Iterator[Callable[..., Lake]]:
    """Factory: make_lake(name="t", **Lake kwargs) -> Lake under tmp_path, frozen clock unless given."""
    opened: list[Lake] = []

    def factory(name: str = "t", **kw: Any) -> Lake:
        kw.setdefault("clock", clock)
        lk = Lake(tmp_path / f"{name}.lake", **kw)
        opened.append(lk)
        return lk

    yield factory
    for lk in opened:
        lk.close()


# --- the §13 serve harness -------------------------------------------------------------------------


def serve_env(home: Path, extra: Mapping[str, str] | None = None, **kw: str) -> dict[str, str]:
    """A clean subprocess environment: no inherited LAKE or LAKE_*, HOME redirected under tmp_path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LAKE_") and k != "LAKE"}
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO)
    env.update(extra or {})
    env.update(kw)
    return env


@contextmanager
def served(home: Path, *args: str, env_extra: Mapping[str, str] | None = None) -> Iterator[str]:
    """`lake serve --port 0 <args>` as a subprocess; yields the announced base url; SIGTERM must exit 0."""
    home.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "lake.cli", "serve", "--port", "0", *args]
    proc = subprocess.Popen(cmd, cwd=str(REPO), env=serve_env(home, env_extra),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    assert proc.stderr is not None
    lines: list[str] = []
    url = ""
    for line in proc.stderr:
        lines.append(line)
        if " on http" in line:
            url = line.strip().rsplit(" on ", 1)[1]
            break
    if not url:
        raise AssertionError(f"serve did not start (exit {proc.wait()}): {''.join(lines)}")
    threading.Thread(target=proc.stderr.read, daemon=True).start()  # drain the request log
    try:
        yield url
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0, "SIGTERM must exit 0 (§13.2)"


def script(tmp_path: Path, name: str, body: str) -> str:
    """An executable /bin/sh script under tmp_path (for cmd: think/embed specs); returns its path."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def write_rows(lake: Lake, specs: Sequence[Mapping[str, Any] | tuple[Any, ...]]) -> list[Delta]:
    """Write many rows. Each spec is write() kwargs as a dict, or a tuple (content, source[, tags[, timestamp]])."""
    out: list[Delta] = []
    for spec in specs:
        if isinstance(spec, tuple):
            keys = ("content", "source", "tags", "timestamp")
            spec = dict(zip(keys, spec, strict=False))
        out.append(lake.write(**spec))
    return out
