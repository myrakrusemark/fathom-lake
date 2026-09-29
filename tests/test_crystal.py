"""§6.3 the crystal as cited edits (AAA phase 4, B2; the one crystal since simplify/core): its prompt pinned byte
for byte, sourced items rendered as prose, the growth log, and the operator guard."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from conftest import FakeEmbed, FakeThink, FrozenClock
from test_consolidate import GOLDEN, MOOD, PROPOSE, crystal_text, golden_ids, stretch

from lake import ConsolidateError, Delta, Lake

MakeLake = Callable[..., Lake]
MakeThink = Callable[[Sequence[str | dict[str, Any]]], FakeThink]


def scene(make_lake: MakeLake, clock: FrozenClock, think: FakeThink) -> tuple[Lake, list[Delta]]:
    """A lake with a container, a mood, a first crystal and newer rows; then a second crystal run whose first
    answer is invalid (so the retry restates the schema)."""
    lake = make_lake(think=think, model_name="fake-1")
    rows = stretch(lake, 3, minutes_ago=90)
    lake.consolidate("container")
    rows += stretch(lake, 3, minutes_ago=20, tags=("user",), step=60)
    lake.consolidate("mood")
    think.answers.append({"items": [{"op": "add", **i} for i in four(rows)]})
    lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=2)
    new = stretch(lake, 2, minutes_ago=30)
    think.answers += [{"items": [op("retire", "c1", why="no reason")]},
                      {"items": [op("add", None, "I want the loop closed and I keep leaving it open.", new[0].id,
                                    section="tension")]}]
    lake.consolidate("crystal", min_chars=100)
    return lake, [*rows, *new]


def test_crystal_prompt_golden(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    """The crystal prompts pinned byte for byte: the first crystal, the second with a prior's items, a container
    and a mood, and its schema-restating retry (golden written at simplify/core, when the prose crystal left)."""
    think = fake_think([PROPOSE, MOOD])
    scene(make_lake, clock, think)
    got = golden_ids("\n\n=== CALL ===\n\n".join(
        f"{p}\n\n=== SYSTEM ===\n\n{s}" for p, s, _ in think.calls[2:]))
    path = GOLDEN / "crystal_edits_prompt.txt"
    if os.environ.get("LAKE_WRITE_GOLDEN"):
        path.write_text(got, encoding="utf-8")
    assert got == path.read_text(encoding="utf-8")


# --- items -----------------------------------------------------------------------------------------------------------

def item(section: str, text: str, *cite: str) -> dict[str, Any]:
    return {"section": section, "text": text, "cite": list(cite)}


def op(kind: str, iid: str | None = None, text: str | None = None, *cite: str, why: str = "", section: str | None = None
       ) -> dict[str, Any]:
    out: dict[str, Any] = {"op": kind, "cite": list(cite), "why": why}
    out.update({k: v for k, v in (("id", iid), ("text", text), ("section", section)) if v is not None})
    return out


def four(rows: Sequence[Delta], tag: str = "") -> list[dict[str, Any]]:
    """Four valid items (core, tension, open, core) citing rows 0-3."""
    return [item("core", f"I ship small releases and keep them reversible{tag}.", rows[0].id),
            item("tension", f"I want speed, and I keep paying for skipped tests{tag}.", rows[1].id),
            item("open", f"I have not decided whether to take on a co-maintainer{tag}.", rows[2].id),
            item("core", f"I answer with the result first and no preamble{tag}.", rows[3].id)]


def test_render_crystal_cap_changes_then_open_then_tension() -> None:
    """The 3000-char render cap drops the oldest change entries first (the growth log keeps them all), then
    trailing open items, then tension items; core stays."""
    from lake._consolidate import RENDER_CAP, render_crystal
    from lake._types import DEFAULT_LABELS
    assert RENDER_CAP == 3000
    items = [{"id": f"c{k}", "section": s, "text": f"{s} {k} " + "x" * 380, "cite": ["a"], "since": "t"}
             for k, s in enumerate(["core", "core", "tension", "tension", "tension", "open", "open", "open", "open"], 1)]
    text, cut = render_crystal(items, [], DEFAULT_LABELS)
    assert len(text) <= RENDER_CAP and cut == 2 and text.count("open ") == 2 and text.count("tension ") == 3
    text, cut = render_crystal(items + [{**items[2], "id": "c10"}] * 3, [], DEFAULT_LABELS)
    assert len(text) <= RENDER_CAP and "open " not in text and text.count("core ") == 2 and cut == 4 + 1
    history = [{"op": "revise", "old": "o" * 300, "new": "n" * 300, "why": "w" * 150}] * 3
    text, cut = render_crystal(items[:7], history, DEFAULT_LABELS)
    assert len(text) <= RENDER_CAP and cut == 0 and text.count("I used to hold") < 3, "changes yield before any item"
    text, cut = render_crystal(items, history, DEFAULT_LABELS)
    assert cut == 2 and "What changed" not in text, "every change entry goes before the first item"
    changed = [{"op": "revise", "old": "I used to X.", "new": "I do Y.", "why": "row r said so"},
               {"op": "retire", "old": "I held Z.", "new": None, "why": ""},
               {"op": "resolve", "old": "Should I do W?", "new": "I do W: the rows settled it", "why": ""}]
    text, _ = render_crystal(items[:2], changed, DEFAULT_LABELS)
    assert text.endswith("## What changed in me lately\n\nI used to hold: I used to X. Now: I do Y. What changed it:"
                         " row r said so\n\nI no longer hold: I held Z.\n\nSettled: Should I do W? Now: I do W: the rows"
                         " settled it.")


def test_items_refute_trigger_narrows_to_cited(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    """§6.4 with an items crystal: refuting a cited row makes due('crystal') true; refuting a row that was only
    shown is no longer a premise."""
    lake = make_lake(think=fake_think([]))
    rows = stretch(lake, 6, minutes_ago=10)
    lake.think = FakeThink([{"items": [{"op": "add", **i} for i in four(rows)]}])
    assert lake.consolidate("crystal", min_chars=100) is not None
    clock.advance(minutes=5)
    lake.engage(rows[5].id, "refute", by="robin")
    assert lake.due("crystal") is False
    lake.engage(rows[0].id, "refute", by="robin")
    assert lake.due("crystal") is True


def test_removed_crystal_options_are_refused(make_lake: MakeLake, fake_think: MakeThink) -> None:
    """The crystal has one form: crystal_mode (prose, structured, edits), require_h2 and the Witness are gone."""
    lake = make_lake(think=fake_think([]))
    stretch(lake, 3, minutes_ago=10)
    for opts in ({"crystal_mode": "prose"}, {"crystal_mode": "edits"}, {"require_h2": False}, {"witness": True}):
        with pytest.raises(ValueError, match="unknown consolidate"):
            lake.consolidate("crystal", **opts)


# --- edits (B2) ----------------------------------------------------------------------------------------------------


def edits_chain(make_lake: MakeLake, clock: FrozenClock, **kw: Any) -> tuple[Lake, list[Delta], list[Delta]]:
    """Three edits-mode crystals: the first all adds; the second revises c1, resolves the open c3 and adds a tension;
    the third returns no ops."""
    lake = make_lake(think=FakeThink([]), model_name="fake-1", **kw)
    rows = stretch(lake, 4, minutes_ago=10)
    first = [{"op": "add", **i, "why": "founding"} for i in four(rows)]
    lake.think = FakeThink([{"items": first}])
    c1 = lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=2)
    new = stretch(lake, 3, minutes_ago=30)
    lake.think = FakeThink([{"items": [
        op("keep", "c2"),
        op("revise", "c1", "I ship small releases, and every hotfix carries its regression test.", new[0].id,
           why="the same bug class came back twice"),
        op("resolve", "c3", "I took on a co-maintainer; the invitation went out.", new[1].id,
           why="the co-maintainer invitation went out"),
        op("add", None, "I want to answer fast, and the docs keep falling behind.", new[2].id, why="confused issues",
           section="tension"),
    ]}])
    c2 = lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=2)
    stretch(lake, 2, minutes_ago=30)
    lake.think = FakeThink([{"items": []}])
    c3 = lake.consolidate("crystal", min_chars=100)
    assert c1 is not None and c2 is not None and c3 is not None
    return lake, [c1, c2, c3], [*rows, *new]


def test_edits_apply_keep_revise_add_resolve(make_lake: MakeLake, clock: FrozenClock) -> None:
    """B2: ops apply to the prior items; unmentioned items are kept; ids are stable across three rewrites; a resolved
    open item becomes core with what settled it; the growth log is in meta.edits and the newest changes render as
    "What changed in me lately"."""
    lake, (c1, c2, c3), rows = edits_chain(make_lake, clock)
    assert c1.meta is not None and [i["id"] for i in c1.meta["items"]] == ["c1", "c2", "c3", "c4"]
    assert [e["op"] for e in c1.meta["edits"]] == ["add"] * 4 and "What changed" not in c1.content
    assert c2.meta is not None and [i["id"] for i in c2.meta["items"]] == ["c1", "c2", "c3", "c4", "c5"]
    items = {i["id"]: i for i in c2.meta["items"]}
    assert items["c1"]["since"] == c2.timestamp and items["c2"]["since"] == c1.timestamp
    assert items["c1"]["cite"] == [rows[4].id] and items["c2"]["cite"] == [rows[1].id]
    assert items["c3"] == {"id": "c3", "section": "core", "text": "I took on a co-maintainer; the invitation went out.",
                           "cite": [rows[5].id], "since": c2.timestamp, "resolved_from": "open"}
    assert c2.meta["implicit_keep"] == 1 and c2.meta["item_seq"] == 5
    assert c2.meta["edits"] == [
        {"op": "revise", "id": "c1", "old": "I ship small releases and keep them reversible.",
         "new": items["c1"]["text"], "cite": [rows[4].id], "why": "the same bug class came back twice"},
        {"op": "resolve", "id": "c3", "old": "I have not decided whether to take on a co-maintainer.",
         "new": items["c3"]["text"], "cite": [rows[5].id], "why": "the co-maintainer invitation went out"},
        {"op": "add", "id": "c5", "old": None, "new": items["c5"]["text"], "cite": [rows[6].id],
         "why": "confused issues"}]
    assert c2.derived_from == [c1.id, rows[4].id, rows[5].id, rows[6].id]
    assert "## What I haven't settled" not in c2.content, "the only open item was resolved"
    assert "reversible.\n\n" not in c2.content and "test.\n\nI took on a co-maintainer; the invitation went out." \
        "\n\nI answer with the result first" in c2.content, "a resolved item renders in core, in item order"
    assert c2.content.endswith(
        "## What changed in me lately\n\nI used to hold: I ship small releases and keep them reversible. Now: I ship"
        " small releases, and every hotfix carries its regression test. What changed it: the same bug class came back"
        " twice\n\nSettled: I have not decided whether to take on a co-maintainer. Now: I took on a co-maintainer; the"
        " invitation went out. What changed it: the co-maintainer invitation went out\n\nNew in me: I want to answer"
        " fast, and the docs keep falling behind. What changed it: confused issues")
    assert c3.meta is not None and c3.meta["items"] == c2.meta["items"] and c3.meta["edits"] == []
    assert c3.meta["implicit_keep"] == 5 and c3.derived_from == [c2.id]
    assert "What changed in me lately" in c3.content, "the prior's changes still show when nothing changed now"


def test_edits_retire_core_only_and_resolve_open_or_tension(make_lake: MakeLake, clock: FrozenClock) -> None:
    """F2: retire is for a core item the rows contradict; a retire naming an open or tension item is refused with a
    note and the item is kept; resolve takes only an open or tension item, needs text and a cite, and counts
    toward max_edits."""
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    lake.think = FakeThink([{"items": [{"op": "add", **i} for i in four(rows)]}])
    lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=1)
    new = stretch(lake, 4, minutes_ago=30)
    lake.think = think = FakeThink([{"items": [
        op("retire", "c2", None, new[0].id, why="the tests were written"),
        op("retire", "c3", None, new[1].id, why="settled"),
        op("resolve", "c1", "I ship small releases, and I have settled it.", new[2].id, why="core"),
        op("resolve", "c2", "I pay for the skipped tests now; the release waits.", new[0].id, why="settled"),
        op("resolve", "c3", None, new[1].id, why="no text"),
        op("retire", "c4", None, new[3].id, why="the person asked for the reasoning first"),
    ]}])
    row = lake.consolidate("crystal", min_chars=100, max_edits=2)
    assert row is not None and row.meta is not None and len(think.prompts) == 1
    assert [(i["id"], i["section"]) for i in row.meta["items"]] == [("c1", "core"), ("c2", "core"), ("c3", "open")]
    assert row.meta["items"][1]["resolved_from"] == "tension" and "resolved_from" not in row.meta["items"][0]
    assert [(e["op"], e["id"]) for e in row.meta["edits"]] == [("resolve", "c2"), ("retire", "c4")]
    assert lake.last_run is not None and lake.last_run.warnings[:3] == [
        "retire c2 refused: c2 is a tension item; resolve it: say what settled it",
        "retire c3 refused: c3 is an open item; resolve it: say what settled it",
        "resolve c1 refused: c1 is a core item; revise or retire it"]
    assert lake.last_run.warnings[3].startswith("resolve c3 (no text) dropped: text is 0 chars")
    assert "Settled: I want speed, and I keep paying for skipped tests." in row.content
    assert "I no longer hold: I answer with the result first and no preamble." in row.content


def test_edits_prompt_states_the_render_room(make_lake: MakeLake, clock: FrozenClock) -> None:
    """F1/F2/F3: the edits prompt states the render cap, the keep default first, resolve, and the coverage
    wording."""
    from lake._consolidate import prompt
    for text in (prompt("crystal_edits_user"),):
        assert "The crystal renders within {render_cap} characters; what does not fit is not shown." in text
        assert "the current value of anything a row corrected (say what it replaced)" in text
        assert "including two rows that disagree with nothing later settling it" in text
    edits = prompt("crystal_edits_user")
    assert edits.index("keep is the default") < edits.index("- revise") and "- resolve:" in edits
    assert '"op": "resolve"' in prompt("crystal_edits_schema")
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    lake.think = think = FakeThink([{"items": [{"op": "add", **i} for i in four(rows)]}])
    lake.consolidate("crystal", min_chars=100)
    assert "The crystal renders within 3000 characters; what does not fit is not shown." in think.prompts[0]


def test_edits_prompt_lists_items(make_lake: MakeLake, clock: FrozenClock) -> None:
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    lake.think = FakeThink([{"items": [{"op": "add", **i} for i in four(rows)]}])
    lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=1)
    stretch(lake, 1, minutes_ago=5)
    lake.think = think = FakeThink([{"items": []}])
    lake.consolidate("crystal", min_chars=100, max_edits=4)
    prompt = think.prompts[0]
    assert ("=== Previous crystal (1.0 hours ago) ===\n[c1] core · I ship small releases and keep them reversible.\n"
            "[c2] tension · I want speed") in prompt
    assert "at most 4 revise, add, retire and resolve operations" in prompt and '"op": "retire"' in (think.systems[0] or "")


def test_edits_rejections_retry(make_lake: MakeLake, clock: FrozenClock) -> None:
    """A retire without a cite is not applied; a pass whose ops are mostly invalid, or with a 7th edit, is retried
    with the reason; a keep of an unknown id is only a warning."""
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    lake.think = FakeThink([{"items": [{"op": "add", **i} for i in four(rows)]}])
    lake.consolidate("crystal", min_chars=100)
    clock.advance(hours=1)
    new = stretch(lake, 8, minutes_ago=30)
    adds = [op("add", None, f"I hold new thing number {k} from the rows.", new[k].id, section="core") for k in range(7)]
    lake.think = think = FakeThink([{"items": [op("retire", "c1", why="no reason")]},
                                    {"items": adds}, {"items": [op("keep", "c99"), {**adds[0], "id": "c1"}]}])
    with pytest.raises(ConsolidateError, match="7 edits, at most 6: keep the rest"):
        lake.consolidate("crystal", min_chars=100)
    assert "rejected: only 0 of 1 edits are valid: retire c1 (no text) dropped: no cited id" in think.prompts[1]
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and row.meta is not None and [i["id"] for i in row.meta["items"]] == ["c1", "c2", "c3", "c4", "c5"]
    assert row.meta["items"][0]["text"] == "I ship small releases and keep them reversible.", "an add never edits an item"
    assert lake.last_run is not None and lake.last_run.warnings == ["keep c99 ignored: no such item"]
    first = make_lake("first", think=FakeThink([]))
    many = stretch(first, 8, minutes_ago=10)
    first.think = FakeThink([{"items": [op("add", None, f"I hold founding thing {k} in the rows.", many[k].id,
                                           section="core") for k in range(8)]}])
    one = first.consolidate("crystal", min_chars=100)
    assert one is not None and one.meta is not None and len(one.meta["items"]) == 8, "the first crystal is unbounded"


def test_edits_prose_prior_becomes_p_items(make_lake: MakeLake, clock: FrozenClock, fake_think: MakeThink) -> None:
    """An old prose crystal is read as h2-facet items p1…pn; that pass is unbounded; an unmentioned p
    item is kept."""
    lake = make_lake(think=fake_think([]))
    rows = stretch(lake, 3, minutes_ago=10)
    prior = lake.write(crystal_text("first"), "host", kind="crystal", derived_from=[rows[0].id])
    clock.advance(hours=1)
    new = stretch(lake, 3, minutes_ago=30)
    lake.think = think = FakeThink([{"items": [
        op("revise", "p1", "I keep building the lake and reading what it holds.", new[0].id, why="still true"),
        op("add", None, "I want the loop closed and I keep leaving it open.", new[1].id, section="tension"),
    ]}])
    row = lake.consolidate("crystal", min_chars=100, max_edits=1)
    assert "[p1] core · Where I am: I keep building the first lake" in think.prompts[0]
    assert "[p2] core · What pulls: The closed loop." in think.prompts[0]
    assert row is not None and row.meta is not None
    assert [(i["id"], i["cite"]) for i in row.meta["items"]] == [("p1", [new[0].id]), ("p2", []), ("c1", [new[1].id])]
    assert row.meta["implicit_keep"] == 1 and row.derived_from == [prior.id, new[0].id, new[1].id]


def test_edits_cap_with_embedder(make_lake: MakeLake, clock: FrozenClock) -> None:
    """With an embedder the A2 cap still gates the rendered text, and drift is the cosine distance."""
    lake, (c1, c2, _), _ = edits_chain(make_lake, clock, embed=FakeEmbed())
    assert c2.meta is not None and c2.meta["drift"]["method"] == "cosine" and c2.meta["drift"]["prior_id"] == c1.id
    assert 0 <= c2.meta["drift"]["value"] <= 0.5


def test_crystal_log_cli(make_lake: MakeLake, clock: FrozenClock, capsys: pytest.CaptureFixture[str]) -> None:
    """`lake crystal --log` walks meta.edits back through the prior crystals, newest first."""
    from lake import cli
    lake, (c1, c2, _), _ = edits_chain(make_lake, clock)
    path = str(lake.path)
    lake.close()
    assert cli.main(["--file", path, "crystal", "--log"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 7 and lines[0].startswith(f"{c2.timestamp[:16]} · revise · I ship small releases and keep")
    assert " · the same bug class came back twice · " in lines[0] and lines[1].split(" · ")[1:3] == [
        "resolve", "I have not decided whether to take on a co-maintainer. → I took on a co-maintainer; the invitation went out."]
    assert lines[3].startswith(f"{c1.timestamp[:16]} · add · — → I ship small releases") and lines[3].endswith("· founding · " + c1.derived_from[0])
    assert cli.main(["--file", path, "--json", "crystal", "--log", "--limit", "2"]) == 0
    log = json.loads(capsys.readouterr().out)
    assert [(e["crystal"], e["op"]) for e in log] == [(c2.id, "revise"), (c2.id, "resolve")]
    from conftest import script
    body = ("import json, re, sys; ids = re.findall(r'^\\[([0-9a-f]{12})\\]', sys.stdin.read(), re.M);"
            " print(json.dumps({'items': [{'op': 'add', 'section': 'tension', 'text': 'I want X and I keep doing Y.',"
            " 'cite': ids[-1:], 'why': 'cli'}]}))")
    think = script(lake.path.parent, "think.sh", f'{sys.executable} -c "{body}"')
    clock.advance(minutes=1)
    with Lake(path, clock=clock) as lk:
        lk.write("one more row about the release process", "chat")
    assert cli.main(["--file", path, "consolidate", "crystal", "--force", "--max-edits", "1",
                     "--min-chars", "100", "--think", f"cmd:{think}"]) == 0
    capsys.readouterr()
    assert cli.main(["--file", path, "crystal", "--log", "--limit", "1"]) == 0
    assert " · add · — → I want X and I keep doing Y. · cli · " in capsys.readouterr().out
    empty = make_lake("empty")
    stretch(empty, 1, minutes_ago=1)
    empty.close()
    assert cli.main(["--file", str(empty.path), "crystal", "--log"]) == 0
    assert "no growth log" in capsys.readouterr().out


def test_edits_long_prose_facet_kept_whole(make_lake: MakeLake, clock: FrozenClock) -> None:
    """A prose facet over TEXT_MAX becomes one uncited p item kept whole: an unmentioned p item is rendered as it
    stands, so a cut there would inject a sentence broken mid-word."""
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 3, minutes_ago=10)
    long = " ".join(f"I work with the user on part {k} of the release process, and I keep notes as I go." for k in range(8))
    assert len(long) > 400
    prior = lake.write(f"## Who I work with\n\n{long}\n\n## Short\n\nI keep it short.", "host", kind="crystal",
                       derived_from=[rows[0].id])
    clock.advance(hours=1)
    new = stretch(lake, 2, minutes_ago=30)
    lake.think = think = FakeThink([{"items": [op("add", None, "I want the loop closed and I keep leaving it open.",
                                                  new[0].id, section="tension")]}])
    row = lake.consolidate("crystal", min_chars=100)
    assert f"[p1] core · Who I work with: {long}\n" in think.prompts[0]
    assert row is not None and row.meta is not None and row.meta["items"][0] == {
        "id": "p1", "section": "core", "cite": [], "since": prior.timestamp, "text": f"Who I work with: {long}"}
    assert f"Who I work with: {long}\n\n" in row.content and "…" not in row.content
    assert rows


def test_edits_host_crystal_with_bad_meta(make_lake: MakeLake, clock: FrozenClock,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    """A host-written crystal row whose item_seq, edits or drift meta is malformed neither breaks the edits pass
    nor `lake crystal --log`: the library never trusts meta."""
    from lake import cli
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    items = [{"id": f"c{k}", "since": "x", **i} for k, i in enumerate(four(rows), 1)]
    lake.write("## What I hold to\n\nI ship small releases.", "host", kind="crystal", meta={
        "item_seq": "c7", "items": items, "edits": ["oops", {"op": "zap"}, {"op": "add", "new": "x", "cite": "ab"}],
        "drift": "none"}, derived_from=[rows[0].id])
    clock.advance(hours=1)
    new = stretch(lake, 2, minutes_ago=30)
    lake.think = FakeThink([{"items": [op("add", None, "I want the loop closed and I keep leaving it open.",
                                          new[0].id, section="tension")]}])
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and row.meta is not None and row.meta["item_seq"] == 5
    assert "New in me: x." in row.content and "zap" not in row.content
    path = str(lake.path)
    lake.close()
    assert cli.main(["--file", path, "crystal", "--log"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [ln.split(" · ")[1] for ln in lines] == ["add", "add"] and lines[1].endswith(" → x · — · ")


# --- the operator guard (§6.3; phase 5's write-time guard) --------------------------------------------------------


def test_operator_guard_refuses_a_name_no_row_names(make_lake: MakeLake, clock: FrozenClock) -> None:
    """The account context is not memory: a new or revised item naming the think's operator is refused and
    retried when nothing the model was shown names them; once a row does, the name is material and is kept."""
    lake = make_lake(think=FakeThink([]))
    rows = stretch(lake, 4, minutes_ago=10)
    leak = [{"op": "add", **i} for i in four(rows)]
    leak[0] = {**leak[0], "text": "I ship small releases for zelda and keep them reversible."}
    lake.think = think = FakeThink([{"items": leak}, {"items": [{"op": "add", **i} for i in four(rows)]}])
    think.operator = ("Zelda", "zh@example.com")  # type: ignore[attr-defined]
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and "zelda" not in row.content.lower()
    assert "it names Zelda, which nothing in the material does" in think.prompts[1]
    clock.advance(hours=1)
    named = lake.write("Zelda asked for smaller releases", "chat", tags=["user"])
    lake.think = think = FakeThink([{"items": [op("add", None, "I ship small releases because Zelda asked for them.",
                                                  named.id, section="core")]}])
    think.operator = ("Zelda",)  # type: ignore[attr-defined]
    row = lake.consolidate("crystal", min_chars=100)
    assert row is not None and "because Zelda asked" in row.content and len(think.calls) == 1


def test_claude_adapter_carries_the_operator_names(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """make_think('claude…') reads what the account context shows claude -p (~/.claude.json oauthAccount); the
    digest's call counter passes the names on; an unreadable file names nobody."""
    from lake import adapters
    from lake._digest import Counter
    monkeypatch.setenv("HOME", str(tmp_path))
    assert adapters.make_think("claude").operator == ()  # type: ignore[attr-defined]
    (tmp_path / ".claude.json").write_text(json.dumps({"oauthAccount": {
        "displayName": "Zelda", "fullName": "Zelda Q Hart", "emailAddress": "zh@example.com"}}), encoding="utf-8")
    think = adapters.make_think("claude:--model opus")
    assert think.operator == ("Hart", "Zelda", "Zelda Q Hart", "zh@example.com")  # type: ignore[attr-defined]
    assert Counter(think).operator == think.operator  # type: ignore[attr-defined]
