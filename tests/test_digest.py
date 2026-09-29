"""digest() (SPEC §6.6): whatever consolidation is due, in order: session containers and backfill, the catch-up
since a date (state in the lake), mood, crystal. Idempotent, resumable, no cap by default, optional caps, a lease,
a dry run on a copy, and the same call over HTTP and from the CLI. Every model call is a deterministic fake; every
lake is a tmp file; every server is a `lake serve` subprocess on 127.0.0.1."""

from __future__ import annotations

import inspect
import io
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO, FrozenClock, script, serve_env, served
from lake import ConsolidateError, DigestRun, DigestStep, Lake, RemoteLake, _db
from test_digestion import ID_RE, Oracle, edits_answer, real, session

BACK = (12, 11, 10)  # history days before the clock's day: older than the default pass's 7d lookback
BEDS = ["plan the raised garden beds for spring", "Two beds, cedar, 4x8.", "order the cedar boards from the mill",
        "Ordered twelve boards."]
SESSION_ANSWER = ('{"kind":"skip","title":"A session","summary":"It happened.","changes":[],"state":"steady",'  # valid for
                  '"headline":"A steady day.","subtext":"Work moved.","carrier_wave":"Still here.","levels":{"focus":0.5},'
                  '"threads":["the garden"]}')  # a session, a cluster (skip) and a mood


def history(path: Path, clock: FrozenClock) -> dict[str, set[str]]:
    """Two real sessions on each of three old days; returns their row ids by day (YYYY-MM-DD)."""
    today = clock().replace(hour=0, minute=0, second=0, microsecond=0)
    out: dict[str, set[str]] = {}
    with Lake(path, clock=clock) as lk:
        for back in BACK:
            day = today - timedelta(days=back)
            out[day.date().isoformat()] = {d.id for d in real(lk, day + timedelta(hours=8), key=f"-{back}")}
    return out


def since(clock: FrozenClock) -> str:
    return (clock() - timedelta(days=BACK[0])).date().isoformat()


def all_days(clock: FrozenClock) -> list[str]:
    first, today = date.fromisoformat(since(clock)), clock().date()
    return [(first + timedelta(days=k)).isoformat() for k in range((today - first).days)]


def containers(lake: Lake) -> list[Any]:
    return [h.delta for h in lake.recall(None, kind="container", limit=200)]


def test_nothing_due_makes_no_calls(make_lake: Any) -> None:
    think = Oracle()
    lake = make_lake("empty", think=think)
    run = lake.digest()
    assert isinstance(run, DigestRun) and run.think_calls == 0 and run.prompt_chars == 0 and not think.calls
    assert [(s.step, s.due) for s in run.steps] == [("container", False), ("mood", False), ("crystal", False)]
    assert run.errors == [] and run.stopped is None and run.days == []
    assert lake.stats()["digest_last"]["calls"] == 0


def test_order_catchup_and_idempotence(tmp_path: Path, clock: FrozenClock) -> None:
    """Steps run container → catch-up days → mood → crystal; every old session is containered once; a second
    digest with nothing due makes no model call and writes no row."""
    path = tmp_path / "hist.lake"
    ids = history(path, clock)
    think = Oracle()
    with Lake(path, think=think, clock=clock) as lake:
        run = lake.digest(since=since(clock), backfill_max=0)
        days = all_days(clock)
        assert [s.step for s in run.steps] == ["container", *(f"catch-up {d}" for d in days), "mood", "crystal"]
        assert run.days == days and run.errors == [] and run.stopped is None
        conts = containers(lake)
        assert len(conts) == 6 and {i for c in conts for i in c.derived_from} == set().union(*ids.values())
        assert [s.due for s in run.steps][-2:] == [True, True] and lake.crystal() is not None, "the new containers are mood material"
        assert run.think_calls == len(think.calls) and run.prompt_chars == sum(len(p) + len(s or "") for p, s, _ in think.calls)
        assert sum(s.prompt_chars for s in run.steps) == run.prompt_chars
        state = lake.stats()["digest"]
        assert state["since"] == since(clock) and state["through"] == days[-1]
        rows, calls = lake.stats()["rows"], len(think.calls)
        again = lake.digest()
        assert again.think_calls == 0 and len(think.calls) == calls and lake.stats()["rows"] == rows + 0
        assert again.days == [] and [s.step for s in again.steps] == ["container", "mood", "crystal"]
        clock.advance(days=1)  # a new UTC day: the catch-up takes it (nothing there), still no call
        third = lake.digest()
        assert third.days == [(date.fromisoformat(days[-1]) + timedelta(days=1)).isoformat()] and third.think_calls == 0


def test_catchup_error_resumes_and_gives_up(tmp_path: Path, clock: FrozenClock) -> None:
    """A day whose run raises counts one attempt and ends the catch-up for this digest; the next digest retries it;
    after three attempts it is given up and recorded, and later digests go on from the next day."""
    path = tmp_path / "flaky.lake"
    ids = history(path, clock)
    bad = min(ids)
    oracle = Oracle()

    def flaky(prompt: str, *, system: str | None = None, json: bool = False) -> Any:
        if bad in prompt and "══ THE SESSION ══" in prompt:  # naming that day's sessions fails; nothing else
            raise RuntimeError("model down")
        return oracle(prompt, system=system, json=json)

    with Lake(path, think=flaky, clock=clock) as lake:
        for attempt in (1, 2):
            run = lake.digest(since=since(clock), backfill_max=0)
            assert run.days == [] and run.given_up == [] and f"catch-up {bad}: RuntimeError: model down" in run.errors
            assert lake.stats()["digest"]["attempts"] == {bad: attempt}
            assert [s.step for s in run.steps][1:] == [f"catch-up {d}" for d in all_days(clock) if d <= bad] + ["mood", "crystal"]
        run = lake.digest(backfill_max=0)
        assert run.given_up == [bad] and run.days == [] and lake.stats()["digest"]["given_up"] == [bad]
        run = lake.digest(backfill_max=0)
        assert run.errors == [] and run.days == [d for d in all_days(clock) if d > bad]
        cited = {i for c in containers(lake) for i in c.derived_from}
        assert cited == set().union(*(v for k, v in ids.items() if k != bad)) and not cited & ids[bad]


def test_skipped_cluster_day_is_done_after_one_pass(tmp_path: Path, clock: FrozenClock) -> None:
    """A cluster the model answers skip for is final for its day: the day is done and nothing is re-asked."""
    path = tmp_path / "skip.lake"
    day = clock().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=10)
    with Lake(path, clock=clock) as lk:
        for k in range(4):
            lk.write(f"orchard note {k} about the apple trees", "notes", tags=["topic:orchard"], timestamp=day + timedelta(minutes=k))
    calls: list[str] = []

    def skip(prompt: str, *, system: str | None = None, json: bool = False) -> Any:
        calls.append(prompt)
        return {"kind": "skip"}

    with Lake(path, think=skip, clock=clock) as lake:
        run = lake.digest(since=day.date().isoformat())
        assert len(calls) == 1 and day.date().isoformat() in run.days
        step = next(s for s in run.steps if s.step == f"catch-up {day.date().isoformat()}")
        assert step.run is not None and step.run.skipped == 1 and not step.run.written
        assert lake.digest().think_calls == 0 and len(calls) == 1


def test_caps_stop_and_the_next_digest_resumes(tmp_path: Path, clock: FrozenClock) -> None:
    """No cap by default; max_units bounds container units (a day cut by it is resumed), max_tokens bounds the
    estimated input tokens (a started step always finishes). Each session is containered exactly once."""
    path = tmp_path / "caps.lake"
    ids = history(path, clock)
    days = sorted(ids)
    think = Oracle()
    with Lake(path, think=think, clock=clock) as lake:
        first = lake.digest(since=since(clock), max_units=3, backfill_max=0)
        assert first.stopped == "max_units" and first.days == [d for d in all_days(clock) if d <= days[0]]
        assert len(containers(lake)) == 3, "day one's two sessions, then one of day two's"
        second = lake.digest(backfill_max=0)
        assert second.stopped is None and second.days[0] == days[1] and second.days[-1] == all_days(clock)[-1]
        sessions = [(c.meta or {}).get("session") for c in containers(lake)]
        assert len(sessions) == 6 == len(set(sessions)), "none twice"
    path2 = tmp_path / "tokens.lake"
    history(path2, clock)
    with Lake(path2, think=Oracle(), clock=clock) as lake:
        run = lake.digest(since=since(clock), max_tokens=1, backfill_max=0)
        assert run.stopped == "max_tokens" and run.days == [d for d in all_days(clock) if d <= days[0]]
        assert run.steps[-1].step == f"catch-up {days[0]}" and len(containers(lake)) == 2
        assert lake.digest(backfill_max=0).days[0] == (date.fromisoformat(days[0]) + timedelta(days=1)).isoformat()
        assert len(containers(lake)) == 6


def test_a_held_lease_is_not_an_attempt_at_a_day(tmp_path: Path, clock: FrozenClock) -> None:
    """A catch-up day that meets another run's container lease (a concurrent run, or one a killed process left) is
    an error for this digest but not an attempt: it is never given up for it, and runs once the lease is gone."""
    path = tmp_path / "busy.lake"
    history(path, clock)
    with Lake(path, think=Oracle(), clock=clock) as lake:
        with lake.tx() as conn:
            _db.meta_set(conn, "consolidate_lease:container", "9999-01-01T00:00:00.000Z")
        for _ in range(4):
            run = lake.digest(since=since(clock), backfill_max=0)
            assert run.days == [] and run.given_up == [] and any("LeaseHeld" in e for e in run.errors)
            assert lake.stats()["digest"]["attempts"] == {}
        with lake.tx() as conn:
            _db.meta_set(conn, "consolidate_lease:container", None)
        assert lake.digest(backfill_max=0).days == all_days(clock)


def test_lease_refuses_an_overlapping_digest(make_lake: Any, clock: FrozenClock) -> None:
    lake = make_lake("lease", think=Oracle())
    with lake.tx() as conn:
        _db.meta_set(conn, "consolidate_lease:digest", "9999-01-01T00:00:00.000Z")
    with pytest.raises(ConsolidateError, match="already running"):
        lake.digest()
    with lake.tx() as conn:
        _db.meta_set(conn, "consolidate_lease:digest", None)
    lake.digest()
    assert _db.meta_get(lake.conn, "consolidate_lease:digest") is None, "released"
    with pytest.raises(ConsolidateError, match="think"):
        make_lake("nothink").digest()


def test_since_change_restarts_the_catchup(tmp_path: Path, clock: FrozenClock) -> None:
    path = tmp_path / "since.lake"
    history(path, clock)
    with Lake(path, think=Oracle(), clock=clock) as lake:
        lake.digest(since=since(clock), backfill_max=0)
        later = (clock() - timedelta(days=3)).date().isoformat()
        run = lake.digest(since=later)
        assert any("restarts" in w for w in run.warnings) and run.days[0] == later
        with pytest.raises(ValueError):
            lake.digest(since="last tuesday")


def test_dry_run_leaves_the_file_byte_identical(tmp_path: Path, clock: FrozenClock) -> None:
    """dry_run digests a copy beside the lake with a think that records and answers skip; the real file is opened
    read-only, is unchanged byte for byte, and the copy is gone afterwards."""
    path = tmp_path / "dry.lake"
    history(path, clock)
    before = path.read_bytes()
    with Lake(path, clock=clock, readonly=True) as lake:
        run = lake.digest(since=since(clock), dry_run=True)
    assert path.read_bytes() == before and not list(tmp_path.glob("*digest-dry*"))
    assert run.think_calls == 6 and run.prompt_chars > 0 and run.days == all_days(clock)
    assert sum(s.prompt_chars for s in run.steps) == run.prompt_chars
    assert run.steps[-1].step == "crystal" and run.steps[-1].due is True and run.steps[-1].run is None
    with Lake(path, clock=clock, readonly=True) as lake:
        assert lake.stats()["digest"] is None and lake.stats()["digest_last"] is None


# --- over HTTP and from the CLI ------------------------------------------------------------------------------


def test_digest_over_the_wire(tmp_path: Path) -> None:
    """POST /v1/digest: 409 without a server think (a dry run needs none); with one, a DigestRun back that
    RemoteLake.digest rebuilds; the server's think is the one that ran."""
    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        session(lk, "a", BEDS, datetime.now().astimezone() - timedelta(hours=3))
    with served(tmp_path / "home", "--lake", str(file)) as url:
        with pytest.raises(ConsolidateError, match="server-side think"):
            RemoteLake(url).digest()
        dry = RemoteLake(url).digest(dry_run=True)
        assert isinstance(dry, DigestRun) and dry.think_calls == 1 and isinstance(dry.steps[0], DigestStep)
    log = tmp_path / "think.log"
    think = script(tmp_path, "think.sh", f'cat >> "{log}"; echo \'{SESSION_ANSWER}\'')
    with served(tmp_path / "home", "--lake", str(file), "--think", f"cmd:{think}") as url:
        run = RemoteLake(url).digest(max_units=5)
        assert run.steps[0].step == "container" and run.steps[0].run is not None
        assert len(run.steps[0].run.written) == 1 and run.think_calls >= 1 and log.exists()
        assert run.steps[0].run.written[0].kind == "container"
        assert RemoteLake(url).digest().steps[0].due is False


def lake_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "lake.cli", *args], cwd=str(REPO), capture_output=True, text=True,
                          timeout=60, env=serve_env(home, {}, LAKE_ENV_FILE="/nonexistent/x"))


def test_cli_digest(tmp_path: Path) -> None:
    """`lake digest`: one line per step and a total; --json prints the DigestRun; exit 1 when a step failed; on a
    remote lake --think is a usage error (the model is the server's)."""
    file = tmp_path / "cli.lake"
    with Lake(file) as lk:
        session(lk, "a", BEDS, datetime.now().astimezone() - timedelta(hours=5))
    before = file.read_bytes()
    dry = lake_cli(tmp_path, "--lake", str(file), "digest", "--dry-run")
    assert dry.returncode == 0 and "container: " in dry.stdout and "~" in dry.stdout and file.read_bytes() == before
    oracle = tmp_path / "think.py"  # the session/cluster/mood answer, or an edits crystal citing the prompt's newest id
    oracle.write_text(f"import json, re, sys\nfrom typing import Any\nID_RE = re.compile({ID_RE.pattern!r})\n"
                      f"{inspect.getsource(edits_answer)}\ntext = sys.stdin.read()\n"
                      f"print(json.dumps(edits_answer(text)) if '\"items\"' in text else {SESSION_ANSWER!r})\n", encoding="utf-8")
    think = script(tmp_path, "think.sh", f"exec {sys.executable} {oracle}")
    done = lake_cli(tmp_path, "--lake", str(file), "--think", f"cmd:{think}", "digest", "--json")
    assert done.returncode == 0, done.stderr
    run = json.loads(done.stdout)
    assert [s["step"] for s in run["steps"]] == ["container", "mood", "crystal"] and run["errors"] == []
    assert run["steps"][0]["run"]["written"] and run["steps"][2]["run"]["written"]
    again = lake_cli(tmp_path, "--lake", str(file), "--think", f"cmd:{think}", "digest")
    assert again.returncode == 0 and "container: nothing due" in again.stdout and "0 calls" in again.stdout
    failing = script(tmp_path, "fail.sh", "exit 3")
    with Lake(tmp_path / "fresh.lake") as lk:  # a first mood is due there, and it cannot be written
        session(lk, "b", BEDS, datetime.now().astimezone() - timedelta(hours=2))
    bad = lake_cli(tmp_path, "--lake", str(tmp_path / "fresh.lake"), "--think", f"cmd:{failing}", "digest")
    assert bad.returncode == 1 and "error" in bad.stdout + bad.stderr
    remote = lake_cli(tmp_path, "--lake", "http://127.0.0.1:9", "--think", "claude", "digest")
    assert remote.returncode == 2 and "server-side" in remote.stderr


def test_catchup_wrapper_runs_lake_digest(tmp_path: Path, clock: FrozenClock) -> None:
    """plugin/scripts/lake-catchup.py is a thin wrapper: its flags map onto `lake digest`; the dropped ones warn."""
    path = tmp_path / "wrap.lake"
    history(path, clock)
    before = path.read_bytes()
    cmd = [sys.executable, str(REPO / "plugin" / "scripts" / "lake-catchup.py"), "--file", str(path),
           "--since", "2026-08-01", "--dry-run", "--json", "--state", str(tmp_path / "s.json")]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=serve_env(tmp_path, {}, LAKE_ENV_FILE="/nonexistent/x"))
    assert done.returncode == 0, done.stderr
    run = json.loads(done.stdout)
    assert run["think_calls"] == 6 and "2026-08-01" in run["days"] and "--state is ignored" in done.stderr
    assert path.read_bytes() == before and not (tmp_path / "s.json").exists()


def test_digest_run_is_exported() -> None:
    import lake

    assert {"DigestRun", "DigestStep"} <= set(lake.__all__)
    out = io.StringIO()
    print(DigestRun([], [], [], [], [], None, 0, 0), file=out)
    assert "DigestRun" in out.getvalue()
