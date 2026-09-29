"""§6.3.1 stances (AAA phase 4, B3): positions written by the edits pass as `lake:stance` rows, their read-time
confidence, the same-slug supersession, the positions block of system_prompt(), the §5.6.2 suffix, the §6.4
triggers, export/import, the remote path and an older library reading a stance lake."""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, FakeThink, FrozenClock, served
from test_crystal import four, op
from test_recall import link

from lake import Delta, Lake, RemoteLake
from lake import _consolidate, _db, _recall

MakeLake = Callable[..., Lake]
HOTFIX = "a hotfix carries its regression test in the same PR"


def stance(kind: str, slug: str, position: str | None = None, *cite: str, against: Sequence[str] = (),
           because: str = "", why: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {"op": kind, "slug": slug, "cite": list(cite), "against": list(against), "why": why}
    return out | ({"position": position, "because": because, "topic": slug.replace("-", " ")} if position else {})


def world(lake: Lake, clock: FrozenClock) -> dict[str, Delta]:
    """Plain rows (items cite these), user rows in sessions s1-s6, an assistant reply and a host container over s7."""
    rows = {f"r{k}": lake.write(f"plain row {k} about how the release process runs", "chat", timestamp=clock() - timedelta(hours=30 - k))
            for k in range(4)}
    said = {"u1": "hotfix broke again, same bug class, add the regression test in the PR", "u2": "second time the "
            "hotfix bug class came back; test in the same PR from now on", "u3": "hotfix rule holds: the regression "
            "test ships with the fix", "u4": "take the crate if it saves a day", "u5": "sensor firmware goes out by "
            "OTA, automatic", "u6": "OTA bricked a board last night, the power dipped mid-flash"}
    for k, (key, text) in enumerate(said.items(), 1):
        rows[key] = lake.write(text, "claude-code", tags=["user", f"session:s{k}"], timestamp=clock() - timedelta(hours=20 - k))
    rows["a1"] = lake.write("I always put the regression test in the hotfix PR", "claude-code",
                            tags=["assistant", "session:s1"], timestamp=clock() - timedelta(hours=19))
    rows["u7"] = lake.write("docs before features for two releases, the issues are all confusion", "claude-code",
                            tags=["user", "session:s7"], timestamp=clock() - timedelta(hours=10))
    rows["k7"] = lake.write("Docs first\n\nThe user put docs ahead of features.", "host", kind="container",
                            derived_from=[rows["u7"].id], timestamp=clock() - timedelta(hours=9))
    return rows


def first_pass(lake: Lake, clock: FrozenClock) -> tuple[Delta, dict[str, Delta]]:
    rows = world(lake, clock)
    plain = [rows[f"r{k}"] for k in range(4)]
    lake.think = FakeThink([{"items": [{"op": "add", **i} for i in four(plain)], "stances": [
        stance("new", "Hotfix Tests", HOTFIX, rows["u1"].id, rows["u2"].id, rows["u3"].id,
               because="the same bug class came back twice", why="decided after s2"),
        stance("new", "deps", "take a crate when it saves a day", rows["u4"].id),
        stance("new", "self-script", "I always answer with the test first", rows["a1"].id),
        stance("new", "ota", "sensor firmware updates go out by automatic OTA", rows["u5"].id, against=[rows["u6"].id]),
        stance("hold", "nothing-here"),
        stance("new", "docs-first", "docs come before new features", rows["k7"].id),
    ]}])
    crystal = lake.consolidate("crystal", min_chars=100)
    assert crystal is not None
    return crystal, rows


def test_stances_written_by_the_edits_pass(make_lake: MakeLake, clock: FrozenClock) -> None:
    """new ops write lake:stance sediment rows (content, tags, derived_from, meta.stance, grounding); the crystal rests
    on them and logs them; assistant-only support, an unknown hold and a 4th new stance are warnings, not retries."""
    lake = make_lake(model_name="fake-1")
    crystal, rows = first_pass(lake, clock)
    assert lake.last_run is not None and lake.last_run.think_calls == 1
    written = [d for d in lake.last_run.written if d.source == "lake:stance"]
    assert [d.tags for d in written] == [["stance:hotfix-tests"], ["stance:deps"], ["stance:ota"]]
    hot = written[0]
    assert hot.kind == "sediment" and hot.derived_from == [rows[k].id for k in ("u1", "u2", "u3")]
    assert hot.content == f"I hold that {HOTFIX}, because the same bug class came back twice."
    assert hot.meta == {"model": "fake-1", "stance": {
        "slug": "hotfix-tests", "topic": "Hotfix Tests", "position": HOTFIX, "because": "the same bug class came back"
        " twice", "against": [], "revises": None, "retired": False}, "grounding": "external"}
    assert written[2].meta is not None and written[2].meta["stance"]["against"] == [rows["u6"].id]
    assert crystal.derived_from[-3:] == [d.id for d in written] and lake.last_run.written[-1].id == crystal.id
    assert crystal.meta is not None and [e["id"] for e in crystal.meta["edits"][4:]] == [
        "stance:hotfix-tests", "stance:deps", "stance:ota"]
    assert crystal.meta["edits"][4] == {"op": "add", "id": "stance:hotfix-tests", "old": None, "new": HOTFIX,
                                        "cite": hot.derived_from, "why": "decided after s2"}
    assert _db.meta_get(lake.conn, "last_consolidate_id:crystal") == crystal.id
    assert _db.meta_get(lake.conn, _db.HAS_STANCES) == "1"
    warnings = lake.last_run.warnings
    assert any("stance new self-script dropped: no outside evidence" in w for w in warnings)
    assert any("stance hold nothing-here dropped: no such live stance" in w for w in warnings)
    assert any("stance new docs-first dropped: more than 3 new or revised stances" in w for w in warnings)


def test_stance_confidence(make_lake: MakeLake, clock: FrozenClock) -> None:
    """support / (support + against + 1) × min(valence, 1) over distinct sessions of outside evidence: 3 sessions
    firm 0.75, 1 held 0.5, 1 for and 1 against contested; an affirm adds support; a refute of the stance makes it
    contested; a superseded or refuted evidence row stops counting."""
    lake = make_lake()
    _, rows = first_pass(lake, clock)
    got = {x.slug: (x.value, x.label, x.support, x.against) for x in _recall.stances(lake, lake.now())}
    assert got == {"hotfix-tests": (0.75, "firm", 3, 0), "deps": (0.5, "held", 1, 0), "ota": (0.3333, "contested", 1, 1)}
    assert [x.slug for x in _recall.stances(lake, lake.now())] == ["hotfix-tests", "deps", "ota"]
    by = {x.slug: x.id for x in _recall.stances(lake, lake.now())}
    clock.advance(minutes=1)
    lake.engage(by["deps"], "affirm", by="robin")
    lake.engage(by["hotfix-tests"], "refute", by="robin", note="not always")
    newer = lake.write("the hotfix rule changed: a test in the same PR only for the same bug class", "claude-code",
                       tags=["user", "session:s9"])
    link(lake, [(rows["u3"], newer, "hotfix rule holds", "test in the same PR only for the same bug class")])
    lake.engage(rows["u2"].id, "refute", by="robin")
    got = {x.slug: (x.value, x.label, x.support) for x in _recall.stances(lake, lake.now())}
    assert got["deps"] == (0.6667, "firm", 2), "the affirm is one more session of support"
    assert got["hotfix-tests"] == (0.25, "contested", 1), "refuted: valence 0.5, and u2, u3 no longer count"


def test_only_a_users_affirm_is_support(make_lake: MakeLake, clock: FrozenClock) -> None:
    """An affirm with no engager and no `user` tag (what the MCP engage tool, driven by the assistant, writes) adds
    no support: the assistant cannot certify its own stance. One with `by` or a `user` tag does."""
    lake = make_lake()
    first_pass(lake, clock)
    deps = next(x for x in _recall.stances(lake, lake.now()) if x.slug == "deps")
    clock.advance(minutes=1)
    lake.engage(deps.id, "affirm")
    now = next(x for x in _recall.stances(lake, lake.now()) if x.slug == "deps")
    assert (now.value, now.label, now.support) == (0.5, "held", 1)
    clock.advance(days=1)
    lake.engage(deps.id, "affirm", tags=["user"])
    now = next(x for x in _recall.stances(lake, lake.now()) if x.slug == "deps")
    assert (now.value, now.label, now.support) == (0.6667, "firm", 2)


def test_same_slug_revision_supersedes(make_lake: MakeLake, clock: FrozenClock) -> None:
    """A revise writes a new row naming the old in meta.stance.revises; the old row scores ×0.50, sorts after the
    new one and carries the receipt; a drop retires the slug; context lines carry the §5.6.2 stance suffix; the
    retired and superseded rows fall out of the positions block."""
    lake = make_lake()
    c1, rows = first_pass(lake, clock)
    old = {x.slug: x for x in _recall.stances(lake, lake.now())}
    clock.advance(hours=2)
    fix = lake.write("hotfix regression test in the same PR, plus a note in the changelog", "claude-code",
                     tags=["user", "session:s8"])
    ota = lake.write("OTA went out to both sensors overnight, no trouble", "claude-code", tags=["user", "session:s9"])
    lake.think = FakeThink([{"items": [], "stances": [
        stance("revise", "hotfix-tests", f"{HOTFIX}, with a changelog note", fix.id, rows["u1"].id, why="s8"),
        stance("drop", "deps", None, fix.id, why="vendoring now"),
        stance("new", "docs-first", "docs come before new features", rows["k7"].id),
        stance("revise", "ota", "sensor firmware updates go out by automatic OTA", ota.id, why="it worked again")]}])
    c2 = lake.consolidate("crystal", min_chars=100)
    assert c2 is not None and c2.meta is not None
    assert [(e["op"], e["id"]) for e in c2.meta["edits"]] == [
        ("revise", "stance:hotfix-tests"), ("retire", "stance:deps"), ("add", "stance:docs-first"), ("revise", "stance:ota")]
    assert lake.last_run is not None and lake.last_run.warnings == [], "u1 is not shown this run: dropped, no warning"
    live = {x.slug: x for x in _recall.stances(lake, lake.now())}
    assert sorted(live) == ["docs-first", "hotfix-tests", "ota"], "the dropped slug is gone"
    assert live["docs-first"].support == 1, "a cited container counts through the rows it rests on"
    assert (live["ota"].support, live["ota"].against, live["ota"].since) == (2, 1, old["ota"].since), "same position"
    assert (live["hotfix-tests"].support, live["hotfix-tests"].label) == (1, "held"), "a new position starts over"
    new = lake.get(live["hotfix-tests"].id)
    assert new is not None and new.meta is not None and new.meta["stance"]["revises"] == old["hotfix-tests"].id
    assert live["hotfix-tests"].since == new.timestamp, "a new position starts its own since"
    dropped = lake.get(lake.last_run.written[1].id) if lake.last_run else None
    assert dropped is not None and dropped.content == "I no longer hold that take a crate when it saves a day: vendoring now."
    hits = lake.recall("hotfix regression test same PR", kind="sediment")
    ids = [h.delta.id for h in hits]
    assert ids.index(new.id) + 1 == ids.index(old["hotfix-tests"].id)
    stale = hits[ids.index(old["hotfix-tests"].id)]
    assert stale.score <= hits[ids.index(new.id)].score
    assert stale.delta.superseded_by[0].id == new.id and stale.delta.superseded_by[0].new_value == new.meta["stance"]["position"]
    crate = lake.recall("take a crate when it saves a day", kind="sediment")
    held = next(h.delta for h in crate if h.delta.id == old["deps"].id)
    assert [x.new_value for x in held.superseded_by] == ["no longer held"], "a drop's receipt is not the position"
    assert "I no longer hold: take a crate when it saves a day. What changed it: vendoring now" in c2.content
    text = lake.context("hotfix regression test same PR changelog")
    assert f"[{new.id}] I hold that {HOTFIX}, with a changelog note, because" not in text  # no because given
    assert f"[{new.id}] I hold that {HOTFIX}, with a changelog note… (from 1 sources) · stance, held" in text
    assert f"· stance, superseded ⟵ superseded by {new.id}: " in text
    block = lake.system_prompt(moods=0).split("\n\n## Positions I hold")[1]
    assert block.startswith(" (how sure I am)\n- ") and "take a crate" not in block
    assert f"- {HOTFIX}, with a changelog note (held; since {new.timestamp[:10]})" in block


def test_stance_never_outranks_its_evidence(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.3: a stance row (sediment, externally grounded, so boosted) is capped at the score of the best-scoring row
    it rests on when both are candidates, and sorts directly after it; alone it keeps its own score."""
    lake = make_lake()
    _, rows = first_pass(lake, clock)
    hot = next(x.id for x in _recall.stances(lake, lake.now()) if x.slug == "hotfix-tests")
    hits = lake.recall("hotfix regression test same PR bug class")
    ids = [h.delta.id for h in hits]
    evidence = [i for i in ids if i in {rows[k].id for k in ("u1", "u2", "u3")}]
    assert ids.index(hot) == ids.index(evidence[0]) + 1 and hits[ids.index(hot)].score <= hits[ids.index(evidence[0])].score
    alone = lake.recall("hotfix regression test same PR bug class", kind="sediment")
    assert alone[0].delta.id == hot and alone[0].score > hits[ids.index(hot)].score


def test_positions_block_and_byte_identity(make_lake: MakeLake, clock: FrozenClock) -> None:
    """system_prompt() puts the positions block between the crystal and the moods, most confident first, within
    budget // 5; without a stance the output is the pre-stance one (crystal, then moods)."""
    lake = make_lake()
    seed = lake.write("seed row for a crystal and a mood", "host", timestamp=clock() - timedelta(hours=40))
    lake.write("## Me\n\nI am the identity crystal.", "host", kind="crystal", derived_from=[seed.id],
               timestamp=clock() - timedelta(hours=40))
    lake.write(json.dumps({"state": "x", "headline": "h", "subtext": "s", "carrier_wave": "CW"}), "host", kind="mood",
               derived_from=[seed.id])
    plain = lake.system_prompt(budget=4000)
    assert "Positions" not in plain and plain.endswith("Recent moods:\n(2026-09-02T18:00)\nh — s\nCW")
    first_pass(lake, clock)
    full = lake.system_prompt(budget=4000)
    crystal_end = full.index("## Positions I hold")
    assert full[crystal_end:].startswith("## Positions I hold (how sure I am)\n- a hotfix carries its regression test"
                                         " in the same PR (firm; since ")
    lines = full[crystal_end:].split("\n\nRecent moods:")[0].splitlines()
    assert [ln.rsplit(" (", 1)[1].split(";")[0] for ln in lines[1:]] == ["firm", "held", "contested"]
    assert len(full) <= 4000 and "\n\nRecent moods:\n" in full
    small = lake.system_prompt(budget=600)  # the edits crystal (584 chars) fits whole: it comes first (§5.7)
    assert len(small) <= 600 and small.endswith("co-maintainer.\n") and "Positions" not in small
    small = lake.system_prompt(budget=584)  # it does not fit: the fixed shares, budget // 5 = 116 keeps one line
    assert len(small) <= 584 and small.count("\n- ") == 1


def test_items_crystal_is_shown_whole_at_the_hook_budget(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§5.7: an items crystal (rendered within RENDER_CAP) that fits the budget is never cut by the positions and mood
    sections: they take what it leaves. A prose crystal keeps the fixed shares (moods 2/5, positions 1/5) and is cut
    as before."""
    lake = make_lake()
    first_pass(lake, clock)
    seed = lake.write("seed row for a long crystal", "host")
    body = "## What I hold\n\n" + "\n\n".join(f"I hold point {k}: " + "x" * 180 for k in range(14))
    assert 2700 < len(body) <= _consolidate.RENDER_CAP
    for k in range(3):
        lake.write(json.dumps({"state": "x", "headline": f"h{k}", "subtext": "s" * 300, "carrier_wave": "w" * 1200}),
                   "host", kind="mood", derived_from=[seed.id])
    lake.write(body, "host", kind="crystal", derived_from=[seed.id], meta={"items": [{"id": "c1"}]})
    got = lake.system_prompt(budget=4000)
    assert len(got) <= 4000 and body in got and "## Positions I hold" in got
    lake.write(body + "\n\nI hold one more point.", "host", kind="crystal", derived_from=[seed.id])  # prose: no items
    prose = lake.system_prompt(budget=4000)
    assert len(prose) <= 4000 and "one more point" not in prose and "Recent moods:" in prose


def test_due_crystal_on_stance_evidence(make_lake: MakeLake, clock: FrozenClock) -> None:
    """§6.4: refuting a stance's evidence, or superseding it after the crystal was written, makes due('crystal')
    true through the crystal -> stance -> evidence ancestry; a link older than the crystal does not."""
    for how in ("refute", "supersede"):
        lake = make_lake(how)
        crystal, rows = first_pass(lake, clock)
        assert rows["u1"].id not in crystal.derived_from and lake.due("crystal") is False
        clock.advance(minutes=5)
        if how == "refute":
            lake.engage(rows["u1"].id, "refute", by="robin")
        else:
            newer = lake.write("hotfix: the test may follow in a day", "claude-code", tags=["user", "session:s9"])
            link(lake, [(rows["u1"], newer, "add the regression test in the PR", "the test may follow in a day")])
        assert lake.due("crystal") is True, how
    early = make_lake("early")
    rows = world(early, clock)
    newer = early.write("ota is off for the hive scale", "claude-code", tags=["user", "session:s9"])
    link(early, [(rows["u5"], newer, "OTA, automatic", "ota is off")])
    clock.advance(minutes=1)
    early.think = FakeThink([{"items": [{"op": "add", **i} for i in four([rows[f"r{k}"] for k in range(4)])],
                              "stances": [stance("new", "ota", "firmware goes out by automatic OTA", rows["u5"].id,
                                                 rows["u6"].id)]}])
    assert early.consolidate("crystal", min_chars=100) is not None
    assert early.due("crystal") is False, "a link written before the crystal was already in view"


def test_scoped_runs_keep_no_stances(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    rows = world(lake, clock)
    plain = [rows[f"r{k}"] for k in range(4)]
    ops = [stance("new", "deps", "take a crate when it saves a day", rows["u4"].id)]
    lake.think = think = FakeThink([{"items": [{"op": "add", **i} for i in four(plain)], "stances": ops}])
    assert lake.consolidate("crystal", min_chars=100, source="chat") is not None
    assert lake.last_run is not None and "stances ignored: a scoped crystal keeps none" in lake.last_run.warnings
    assert "(kept only by the unscoped crystal)" in think.prompts[0] and _recall.stances(lake, lake.now()) == []
    assert _db.meta_get(lake.conn, _db.HAS_STANCES) is None


def test_edits_prompt_lists_live_stances(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake()
    first_pass(lake, clock)
    clock.advance(hours=1)
    lake.write("one more row about the release", "chat")
    lake.think = think = FakeThink([{"items": []}])
    assert lake.consolidate("crystal", min_chars=100) is not None
    assert (f"=== Positions I hold (3 live stances) ===\n[hotfix-tests] {HOTFIX} — firm; evidence 3; against 0\n"
            "[deps] take a crate when it saves a day — held; evidence 1; against 0\n") in think.prompts[0]
    assert '"stances": [' in (think.systems[0] or "") and "hold: the position stands" in think.prompts[0]


def test_stances_export_import_roundtrip(make_lake: MakeLake, clock: FrozenClock, tmp_path: Path) -> None:
    lake = make_lake()
    first_pass(lake, clock)
    clock.advance(hours=1)
    lake.think = FakeThink([{"items": [], "stances": [stance("drop", "deps", None, lake.recall("crate")[0].delta.id,
                                                             why="settled")]}])
    lake.write("one more row about the crate", "chat")
    lake.consolidate("crystal", min_chars=100)
    dump = tmp_path / "x.jsonl"
    lake.export(dump)
    copy = make_lake("copy")
    copy.import_(str(dump))
    assert _db.meta_get(copy.conn, _db.HAS_STANCES) == "1"
    assert _recall.stances(copy, copy.now()) == _recall.stances(lake, lake.now())
    assert copy.system_prompt() == lake.system_prompt()
    assert _db.supersessions(copy.conn, copy.now_str()).keys() == _db.supersessions(lake.conn, lake.now_str()).keys()


def test_remote_stances_match_local(tmp_path: Path) -> None:
    """/v1/system-prompt and /v1/context compute the positions block and the stance suffix server-side."""
    file = tmp_path / "t.lake"
    clock = FrozenClock(datetime.now(UTC))
    with Lake(file, clock=clock) as lk:
        first_pass(lk, clock)
    with served(tmp_path / "home", "--file", str(file)) as url:
        with Lake(file, readonly=True) as lk:
            local, ctx = lk.system_prompt(budget=4000), lk.context("hotfix regression test same PR")
        with RemoteLake(url, spool_path=tmp_path / "s.jsonl") as rl:
            assert rl.system_prompt(budget=4000) == local and "## Positions I hold" in local
            assert rl.context("hotfix regression test same PR") == ctx and "· stance, firm" in ctx


def test_old_library_reads_a_stance_lake(make_lake: MakeLake, clock: FrozenClock, tmp_path: Path) -> None:
    """Additive storage: the library at 0e2a4e8 opens a stance lake, recalls a stance row and writes; on a lake with
    no stance its system_prompt() is byte-identical to this library's."""
    blob = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "0e2a4e8", "lake"], capture_output=True)
    if blob.returncode != 0:
        pytest.skip("git archive of 0e2a4e8 unavailable")
    lib = tmp_path / "oldlib"
    with tarfile.open(fileobj=BytesIO(blob.stdout)) as tar:
        tar.extractall(lib, filter="data")
    lake = make_lake("stances")
    first_pass(lake, clock)
    plain = make_lake("plain")
    seed = plain.write("seed row for a crystal and a mood", "host")
    plain.write("## Me\n\nI am the identity crystal.", "host", kind="crystal", derived_from=[seed.id])
    plain.write(json.dumps({"state": "x", "headline": "h", "subtext": "s", "carrier_wave": "CW"}), "host",
                kind="mood", derived_from=[seed.id])
    code = ("import json, sys, lake\nfrom datetime import datetime\nclock = lambda: datetime.fromisoformat(sys.argv[3])\n"
            "with lake.Lake(sys.argv[1], clock=clock) as lk:\n"
            "    hits = lk.recall('hotfix regression test same PR', kind='sediment')\n"
            "    w = lk.write('an old library write', 'oldlib')\n"
            "with lake.Lake(sys.argv[2], clock=clock) as lk:\n"
            "    sp = lk.system_prompt(budget=4000)\n"
            "print(json.dumps({'module': lake.__file__, 'hits': [h.delta.source for h in hits], 'sp': sp}))\n")
    expected_sp = plain.system_prompt(budget=4000)
    for lk in (lake, plain):
        lk.close()
    out = subprocess.run([sys.executable, "-c", code, str(lake.path), str(plain.path), clock().isoformat()],
                         env={"PYTHONPATH": str(lib), "PATH": "/usr/bin:/bin", "LAKE_ENV_FILE": "/nonexistent/x"},
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["module"].startswith(str(lib)) and res["hits"][0] == "lake:stance"
    assert res["sp"] == expected_sp
