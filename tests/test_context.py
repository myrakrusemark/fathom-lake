"""§10 tests owned by _context: the golden render, the budget trims, the empty-query path, filters, labels."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from lake import ContextResult, Lake

CRYSTAL_ID = "d4e5f6a7b8c9"  # the fixture's crystal, an existing id to derive moods from

MakeLake = Callable[..., Lake]
FIXTURE = Path(__file__).parent / "fixtures" / "context.jsonl"
GOLDEN = Path(__file__).parent / "golden" / "context.txt"
QUERY = "lake design brief sqlite"


def fixture_lake(make_lake: MakeLake, **kw: object) -> Lake:
    """The §5.6.4 lake: the fixture imported with import_() under the frozen clock (2026-09-02T18:00:00Z)."""
    lake = make_lake(automation=["source:agent-heartbeat"], **kw)
    counts = lake.import_(FIXTURE)
    assert counts == {"written": 24, "skipped": 0, "errors": 0}
    return lake


def strip_text(rendered: str) -> str:
    """The text from the first strip header on (block 5 and 6)."""
    return rendered[rendered.index("════════") :]


def test_context_golden(make_lake: MakeLake) -> None:
    lake = fixture_lake(make_lake)
    rendered = lake.context(QUERY, budget=2000)
    assert rendered.encode("utf-8") == GOLDEN.read_bytes()
    assert len(rendered) == 1773 and not rendered.endswith("\n")
    r = lake.context_blocks(QUERY, budget=2000)
    assert r.rendered == rendered
    assert len(r.hits) == 9 and len(r.containers) == 2 and len(r.strips) == 3 and r.omitted_strips == 2
    assert r.crystal is not None and r.crystal.id == "d4e5f6a7b8c9"
    assert [c.id for c in r.containers] == ["3f9a1c2b7d4e", "8b1c0d2e3f4a"]
    assert [s.anchor_ids for s in r.strips] == [["c3d4e5f6a7b8"], ["3f9a1c2b7d4e"], ["a1b2c3d4e5f6", "b2c3d4e5f6a7"]]
    assert "── surrounding context ──" not in rendered  # every ambient line went before the fifth strip did
    assert r.warnings == [] and lake.last_warnings == []
    wide = lake.context(QUERY, budget=2900)  # all five strips; ambient lines come back, the astral row among them
    assert len(wide) == 2863 and len(wide.encode("utf-16-le")) // 2 == 2864 and wide.startswith(rendered[:830])
    assert "  17:24:10  claude-code  · user: does 𝄞 count as one character or two in the caps" in wide


def test_context_budget(make_lake: MakeLake) -> None:
    lake = fixture_lake(make_lake)
    full = lake.context_blocks(QUERY, budget=2000)
    top = next(s for s in full.strips if s.anchor_ids == ["a1b2c3d4e5f6", "b2c3d4e5f6a7"])
    ambient = sum(1 for e in top.rows if not getattr(e, "is_anchor", False))
    r = lake.context_blocks(QUERY, budget=600)
    assert len(r.rendered) <= 600 and len(r.strips) == 1 and r.omitted_strips == 4
    assert r.strips[0].anchor_ids == top.anchor_ids  # the highest-scoring strip is the last one standing
    body = strip_text(r.rendered)
    assert "more strips not shown" in body
    assert sum(1 for ln in body.split("\n") if ln.startswith("  17:")) < ambient  # ambient rows trimmed
    r = lake.context_blocks(QUERY, budget=300)
    assert len(r.rendered) <= 300 and len(r.strips) == 1
    body = strip_text(r.rendered)
    anchors = [ln for ln in body.split("\n") if ln.startswith("▸ ")]
    assert len(anchors) < len(top.anchor_ids)  # anchor lines cut from the end
    assert all(ln.startswith("▸ 17:05:44") for ln in anchors)
    assert body.startswith("════════ 2026-09-02 · 17:04–17:31 · 2 anchors ════════")  # the header is never rewritten


def test_context_anchors_before_ambient(make_lake: MakeLake) -> None:
    """§5.6.3 budget: ambient lines go (lowest-scoring strip first) before any strip is dropped, so a strip's
    neighbours never cost another strip its anchors. Six strips, one anchor each, eight neighbours around each."""
    lake = make_lake()
    anchors = []
    for day in range(6):
        base = f"2026-08-{10 + day:02d}T10:"
        for m in range(8):
            lake.write(f"routine note {day}-{m} about the weather and lunch plans", "host",
                       timestamp=f"{base}{m * 2:02d}:00Z")
        anchors.append(lake.write(f"the zephyr kiln firing schedule, batch {day}", "host", tags=["user"],
                                  timestamp=f"{base}09:00Z").id)
    full = lake.context_blocks("zephyr kiln", budget=20000, containers=False)
    assert len(full.strips) == 6 and "── surrounding context ──" in full.rendered
    seen = 0
    for budget in range(400, 3000, 50):
        r = lake.context_blocks("zephyr kiln", budget=budget, containers=False)
        shown = [a for a in anchors if a[:12] in r.rendered]
        assert len(r.rendered) <= budget and len(shown) >= seen, budget  # more room never shows fewer anchors
        seen = len(shown)
        if "── surrounding context ──" in r.rendered:
            assert len(shown) == len(r.strips) == 6, budget  # ambient never shown while a strip is dropped
    r = lake.context_blocks("zephyr kiln", budget=1500, containers=False)
    assert [a for a in anchors if a[:12] in r.rendered] == anchors and r.omitted_strips == 0


def test_context_strip_cap_scales_with_budget(make_lake: MakeLake) -> None:
    """§5.6 step 3: up to 8000 the 12 highest-scoring strips; a larger budget keeps ⌊budget · 12 / 8000⌋."""
    lake = make_lake()
    for day in range(30):
        lake.write(f"the zephyr kiln firing schedule, batch {day}", "host", timestamp=f"2026-07-{day + 1:02d}T10:00:00Z")
    small = lake.context_blocks("zephyr kiln", budget=8000, containers=False)
    assert len(small.strips) == 12 and small.omitted_strips == 18
    big = lake.context_blocks("zephyr kiln", budget=24000, containers=False)
    assert len(big.strips) == 30 and big.omitted_strips == 0
    assert lake.context_blocks("zephyr kiln", budget=16000, containers=False).omitted_strips == 6  # 24 kept
    assert big.rendered.startswith(small.rendered.split("════════", 1)[0])  # blocks before the strips unchanged


def test_context_empty_query(make_lake: MakeLake) -> None:
    lake = fixture_lake(make_lake)
    crystal = lake.crystal()
    assert crystal is not None
    block = f"Identity crystal (crystallized 2026-09-01T03:10):\n\n{crystal.content}\n"
    assert lake.context(None) == block
    assert lake.context("") == block
    cut = lake.context(None, budget=300)
    assert 300 * 0.4 < len(cut) <= 300 and cut.endswith("\n…") and block.startswith(cut[:-2])
    assert lake.context("zzqx") == lake.context(None)
    assert lake.context("zzqx", budget=300) == cut
    assert lake.context(None, crystal=False) == "" and lake.context("zzqx", crystal=False) == ""
    r = lake.context_blocks("zzqx")
    assert r.hits == [] and r.containers == [] and r.strips == [] and r.omitted_strips == 0
    bare = make_lake("bare")
    assert bare.context(None) == "" and bare.context("zzqx") == ""


def test_context_filters(make_lake: MakeLake) -> None:
    lake = make_lake()
    a = lake.write("the dragon story about the mountain dragon", "reader", tags=["user", "session:x"], timestamp="2026-09-02T17:40:00Z")
    lake.write("ada asked about the dragon mountain map", "ada", tags=["user"], timestamp="2026-09-02T17:41:00Z")
    c = lake.write("the dragon flew over the mountain at dusk", "reader", tags=["assistant"], timestamp="2026-09-02T17:42:00Z")
    mood = lake.write(
        '{"state": "curious", "headline": "dragon mountain"}', "host", kind="mood", derived_from=[a.id],
        tags=["feeling:curious"], timestamp="2026-09-02T17:43:00Z",
    )
    box = lake.write(
        "notes on the reader session\n\nA reader and a beast.", "host", kind="container", derived_from=[a.id, c.id],
        meta={"title": "dragon mountain"}, timestamp="2026-09-02T17:44:00Z",
    )

    def ids(r: ContextResult) -> list[str]:
        return [h.delta.id for h in r.hits]

    r = lake.context_blocks("dragon mountain", source=["reader"])
    assert "ada" not in r.rendered and all(h.delta.source == "reader" for h in r.hits)
    r = lake.context_blocks("dragon mountain")
    assert ids(r)[0] == a.id and "ada" in r.rendered
    assert a.id not in ids(lake.context_blocks("dragon mountain", exclude_tags=["session:x"]))
    assert mood.id not in ids(r) and "  17:43:00  mood         · feeling: curious" in r.rendered
    assert [d.id for d in r.containers] == [box.id]
    assert "  ── containers active in this recall ──" in r.rendered
    assert f"▸ 17:44:00  container    · [L1 · 2 deltas · {box.id}] dragon mountain" in r.rendered
    off = lake.context_blocks("dragon mountain", containers=False)
    assert off.containers == [] and "containers active" not in off.rendered
    assert off.rendered.count("\n") == r.rendered.count("\n") - 3  # block 4 (blank, header, one line) is gone


def test_context_refuted_marker(make_lake: MakeLake) -> None:
    lake = make_lake()
    a = lake.write("the dragon story about the mountain dragon", "reader", tags=["assistant"],
                   timestamp="2026-09-02T17:40:00Z")
    b = lake.write("the dragon flew over the mountain at dusk", "reader", tags=["assistant"],
                   timestamp="2026-09-02T17:41:00Z")
    lake.engage(a.id, "refute", by="robin", note="wrong dragon")  # a refuter with a note
    lake.engage(b.id, "refute", by="ada")  # a refuter with no note (the crash path)
    r = lake.context_blocks("dragon mountain")
    assert " ⟵ refuted ×1 (robin · wrong dragon)" in r.rendered  # note rendered
    assert " ⟵ refuted ×1 (ada)" in r.rendered  # no-note refuter falls back to the source, no crash
    over = lake.context_blocks("dragon mountain", labels={"refuted": " [REFUTED {n}x by {detail}]"})
    assert " [REFUTED 1x by robin · wrong dragon]" in over.rendered
    assert " ⟵ refuted" not in over.rendered


def test_context_superseded_marker(make_lake: MakeLake) -> None:
    """§5.6.2: a superseded anchor renders ` ⟵ superseded by {id}: {new value}`; it stays in the render, and the
    superseder ranks above it. The label is overridable like the others."""
    from lake import _db, _store

    lake = make_lake()
    old = lake.write("the dragon lives on the mountain near the lake", "reader", timestamp="2026-08-02T17:40:00Z")
    new = lake.write("the dragon moved to the coast", "reader", timestamp="2026-09-01T17:40:00Z")
    with lake.tx() as conn:
        _store.insert_row(conn, delta_id=_db.new_id(conn), timestamp=lake.now_str(), content="Dragon\n\nIt moved.",
                          source="lake:container", kind="container", level=1, tags=[], derived_from=[old.id, new.id],
                          expires_at=None, media_hash=None, meta={"supersedes": [
                              {"new": new.id, "old": old.id, "old_value": "the mountain", "new_value": "the coast"}]})
    r = lake.context_blocks("dragon mountain lake", containers=False)
    assert f"[{old.id}] the dragon lives on the mountain near the lake ⟵ superseded by {new.id}: the coast" in r.rendered
    order = [h.delta.id for h in r.hits]
    assert order.index(new.id) + 1 == order.index(old.id)
    over = lake.context_blocks("dragon mountain lake", labels={"superseded": " [OLD; now {value}]"})
    assert "near the lake [OLD; now the coast]" in over.rendered


def test_context_labels(make_lake: MakeLake) -> None:
    labels = {"remember": "--- {n} memories ---", "user_role": " child:"}
    lake = fixture_lake(make_lake, labels=labels)
    golden = GOLDEN.read_text(encoding="utf-8")
    expected = golden.replace("--- You remember 9 things ---", "--- 9 memories ---").replace("· user:", "· child:")
    assert expected != golden and lake.context(QUERY, budget=2000) == expected
    per_call = lake.context(QUERY, budget=2000, labels={"user_role": " kid:", "led_to": "  and then"})
    assert per_call == expected.replace("· child:", "· kid:").replace("  …which led to…", "  and then")


# --- system_prompt() (§5.7) ------------------------------------------------------------------------


def make_mood(lake: Lake, headline: str, subtext: str, carrier: str, ts: str, derived: str = CRYSTAL_ID) -> None:
    content = json.dumps({"state": "x", "headline": headline, "subtext": subtext, "carrier_wave": carrier})
    lake.write(content, "host", kind="mood", derived_from=[derived], tags=["feeling:x"], timestamp=ts, dedupe=False)


def test_system_prompt(make_lake: MakeLake) -> None:
    lake = fixture_lake(make_lake)
    baseline = lake.context(None)  # crystal block alone, the reused invariant
    assert lake.system_prompt(moods=0) == baseline  # moods=0 suppresses the section (the fixture ships one)

    crys_only = make_lake("crysonly")  # a crystal with no mood row at all -> exactly context(None)
    seed = crys_only.write("seed", "host")
    crys_only.write("I am a lonely crystal with no moods to my name.", "host", kind="crystal", derived_from=[seed.id])
    assert crys_only.system_prompt() == crys_only.context(None)

    make_mood(lake, "MOODA", "sA", "CARRIER_A", "2026-09-02T17:50:00Z")
    make_mood(lake, "MOODB", "sB", "CARRIER_B", "2026-09-02T17:51:00Z")
    make_mood(lake, "MOODC", "sC", "CARRIER_C", "2026-09-02T17:52:00Z")
    sp = lake.system_prompt()
    assert sp.startswith(baseline) and sp[len(baseline)] == "\n"  # crystal block first, then the section
    assert "Recent moods:" in sp
    assert all(c in sp for c in ("CARRIER_A", "CARRIER_B", "CARRIER_C"))
    assert sp.index("CARRIER_C") < sp.index("CARRIER_B") < sp.index("CARRIER_A")  # newest first
    for stamp in ("(2026-09-02T17:52)", "(2026-09-02T17:51)", "(2026-09-02T17:50)"):
        assert stamp in sp  # the mood_when minute stamp
    assert "MOODC — sC" in sp  # headline — subtext

    make_mood(lake, "MOODD", "sD", "CARRIER_D", "2026-09-02T17:53:00Z")
    sp3 = lake.system_prompt()  # default moods=3: D, C, B; the oldest kept (A) is now off the tail
    assert "CARRIER_D" in sp3 and "CARRIER_A" not in sp3
    assert "CARRIER_A" in lake.system_prompt(moods=4)  # widening brings it back

    lake.write("not json at all, just prose about the lake", "host", kind="mood",
               derived_from=[CRYSTAL_ID], tags=["feeling:x"], timestamp="2026-09-02T17:59:30Z", dedupe=False)
    sp_bad = lake.system_prompt(moods=1)
    assert "not json at all, just prose about the lake" in sp_bad  # non-JSON falls back to oneline(content, 400)

    assert make_lake("bare2").system_prompt() == ""  # no crystal, no mood

    nocrys = make_lake("nocrys")
    seed = nocrys.write("seed", "host")
    make_mood(nocrys, "h", "s", "CARRIER_ONLY", "2026-09-02T17:00:00Z", derived=seed.id)
    sp_only = nocrys.system_prompt()
    assert sp_only.startswith("Recent moods:") and "CARRIER_ONLY" in sp_only and "Identity crystal" not in sp_only


def test_system_prompt_budget(make_lake: MakeLake) -> None:
    lake = fixture_lake(make_lake)
    big = "X" * 500
    make_mood(lake, "MOODA", "sA", big + "A", "2026-09-02T17:50:00Z")
    make_mood(lake, "MOODB", "sB", big + "B", "2026-09-02T17:51:00Z")
    make_mood(lake, "MOODC", "sC", big + "C", "2026-09-02T17:52:00Z")
    for b in (8000, 2000, 1000, 600, 300, 120, 60):
        assert len(lake.system_prompt(budget=b)) <= b

    out = lake.system_prompt(budget=300)  # tiny: crystal survives cut, mood section truncated to the newest
    assert len(out) <= 300 and out.startswith("Identity crystal")
    crystal_part, sep, mood_part = out.partition("Recent moods:")
    assert sep == "Recent moods:"
    assert crystal_part.rstrip("\n").endswith("…")  # the crystal itself was cut
    assert mood_part.endswith("…")  # the single newest mood hard-cut
    assert "MOODC" in out and "MOODA" not in out and "MOODB" not in out  # oldest dropped first

    over = lake.system_prompt(labels={"mood_header": "MOODS>>", "mood_when": "at {ts}"})
    assert "MOODS>>" in over and "Recent moods:" not in over and "at 2026-09-02T17:52" in over

    nocrys = make_lake("nocrysb")  # crystal absent, one huge mood: the mood-only path stays within budget
    seed = nocrys.write("seed", "host")
    make_mood(nocrys, "HH", "SS", "Y" * 2000, "2026-09-02T17:00:00Z", derived=seed.id)
    for b in (5000, 1000, 300, 120, 40):
        assert len(nocrys.system_prompt(budget=b)) <= b
    assert nocrys.system_prompt(budget=200).endswith("…")  # single newest truncated with the ellipsis


def test_anchor_window_centres_on_the_query() -> None:
    from lake._recall import window
    text = "filler " * 300 + "the scale said 10 pounds after the diet " + "more filler " * 300
    out = window(text, ["pounds", "diet"], 120)
    assert len(out) == 120 and out.startswith("…") and out.endswith("…") and "10 pounds" in out
    assert window("short line", ["x"], 120) == "short line"  # under the cap: whole
    assert window(text, [], 120) == text[:119] + "…"  # no tokens: the head, as oneline() cuts it


def test_anchor_cap_scales_with_budget(make_lake: MakeLake) -> None:
    lake = make_lake()
    long = "intro " * 50 + "the fact sits here near the start " + "detail " * 600  # ~4.5k chars
    lake.write(long, "host", tags=["assistant"])
    small = lake.context("fact sits here", budget=8000, crystal=False)
    big = lake.context("fact sits here", budget=48000, crystal=False)
    line = lambda text: next(x for x in text.splitlines() if x.startswith("▸"))
    assert len(line(small)) < len(line(big)) and len(line(big)) - len(line(small)) >= 2500


def test_widening_never_costs_a_strip(make_lake: MakeLake) -> None:
    # past the WIDE_TOP best hits, anchors widen only with budget left over: every strip that fits at the base cap shows
    lake = make_lake()
    for i in range(16):
        lake.write(f"kiln note {i} " + "glaze detail " * 400, "host", tags=["assistant"],
                   timestamp=f"2026-08-{i + 1:02d}T10:00:00Z")
    res = lake.context_blocks("kiln note glaze", budget=16000, crystal=False)
    lines = [x for x in res.rendered.splitlines() if x.startswith("▸")]
    assert len(res.strips) == 16 and len(res.rendered) <= 16000
    assert max(map(len, lines)) > 1000 and min(map(len, lines)) < 700  # some widened; the rest at the base cap
