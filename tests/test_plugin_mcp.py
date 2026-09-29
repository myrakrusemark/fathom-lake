"""The plugin's MCP server (plugin/DESIGN.md, MCP section), driven exactly as Claude Code
drives it: spawned as a subprocess over stdio, the initialize handshake, list_tools, tool
calls. A tmp LAKE file per test (a served URL for the remote sediment path); the
per-call self-heal when the lake is unreachable at startup."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from lake import Lake

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "plugin" / "mcp" / "server.py"
EIGHT_TOOLS = frozenset(
    {"remember", "deep_recall", "context", "write", "engage", "lineage", "crystal", "stats"}
)
TEN_STEP_TYPES = (
    "search", "filter", "intersect", "union", "diff",
    "bridge", "chain", "aggregate", "neighbors", "timeline",
)
TIMEOUT = 60.0
NO_ENV_FILE = "/nonexistent/lake-env-file"  # masks the real ~/.lake/env (SPEC §13.8)


# --- driving the server ---------------------------------------------------------------------------


async def _in_session(
    lake_file: Path,
    fn: Callable[[ClientSession], Awaitable[Any]],
    extra_env: dict[str, str] | None = None,
) -> Any:
    """Spawn server.py with the venv python and LAKE, handshake, run fn, tear down. PYTHONPATH: this checkout's
    lake, never whatever an installed one is (get_default_environment passes no PYTHONPATH)."""
    env = {**get_default_environment(), "LAKE": str(lake_file), "LAKE_ENV_FILE": NO_ENV_FILE, "PYTHONPATH": str(REPO)}
    env.update(extra_env or {})
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)


def in_session(
    lake_file: Path,
    fn: Callable[[ClientSession], Awaitable[Any]],
    extra_env: dict[str, str] | None = None,
) -> Any:
    return asyncio.run(asyncio.wait_for(_in_session(lake_file, fn, extra_env), TIMEOUT))


def text_of(result: Any) -> str:
    """The concatenated text content of a successful tool call."""
    assert not result.isError, f"tool call errored: {result.content}"
    return "\n".join(c.text for c in result.content if getattr(c, "text", None) is not None)


async def tool_names(session: ClientSession) -> set[str]:
    listed = await session.list_tools()
    return {t.name for t in listed.tools}


# --- healthy mode ---------------------------------------------------------------------------------


def test_handshake_lists_the_eight_tools(tmp_path: Path) -> None:
    """A writable-but-absent LAKE_FILE is healthy: exactly the eight tools, no lake_status."""
    lake_file = tmp_path / "claude.lake"

    async def fn(session: ClientSession) -> tuple[set[str], str]:
        names = await tool_names(session)
        remembered = text_of(await session.call_tool("remember", {"query": "anything at all"}))
        return names, remembered

    names, remembered = in_session(lake_file, fn)
    assert names == set(EIGHT_TOOLS)
    assert "no lake yet" in remembered  # read tool on a missing file answers, never creates it
    assert not lake_file.exists()


def test_deep_recall_description_teaches_the_plan_dsl(tmp_path: Path) -> None:
    """The handshake lists deep_recall with a description that names all ten §5.5 step
    types with their key params and shows two example plans — the calling model authors
    the plan, so the description is the manual."""

    async def fn(session: ClientSession) -> str:
        listed = await session.list_tools()
        by_name = {t.name: t for t in listed.tools}
        assert "deep_recall" in by_name
        return str(by_name["deep_recall"].description)

    desc = in_session(tmp_path / "claude.lake", fn)
    for step_type in TEN_STEP_TYPES:
        assert f'"{step_type}"' in desc, f"description must name the {step_type} step"
    for param in ("group_by", "radius_minutes", "limit_per_seed", "max_per_side",
                  "tags_include", "source", "since", "limit"):
        assert param in desc, f"description must name the {param} param"
    assert desc.count('[{"id"') >= 2  # two example plans: a filtered search and a bridge
    assert '"bridge": ["a", "b"]' in desc
    assert "sediment [" in desc  # says how the automatic sediment row is rendered


def test_write_then_remember_and_the_row_is_in_the_file(tmp_path: Path) -> None:
    lake_file = tmp_path / "claude.lake"
    content = "the reactor manual lives in cabinet nine"

    async def fn(session: ClientSession) -> tuple[str, str]:
        wrote = text_of(await session.call_tool("write", {"content": content, "tags": ["note", "test"]}))
        remembered = text_of(await session.call_tool("remember", {"query": "reactor manual cabinet"}))
        return wrote.strip(), remembered

    delta_id, remembered = in_session(lake_file, fn)
    assert delta_id, "write must return the new row's id"
    assert delta_id in remembered
    assert "reactor manual" in remembered
    with Lake(lake_file, readonly=True) as lk:
        row = lk.get(delta_id)
        assert row is not None
        assert row.content == content
        assert row.source == "claude-code"
        assert set(row.tags) >= {"note", "test"}


def test_engage_lineage_context_crystal_stats_round_trip(tmp_path: Path) -> None:
    lake_file = tmp_path / "claude.lake"
    content = "the reactor manual lives in cabinet nine"

    async def fn(session: ClientSession) -> dict[str, str]:
        delta_id = text_of(await session.call_tool("write", {"content": content})).strip()
        engaged = text_of(
            await session.call_tool("engage", {"delta_id": delta_id, "kind": "affirm", "note": "still true"})
        ).strip()
        return {
            "delta_id": delta_id,
            "engaged": engaged,
            "lineage": text_of(await session.call_tool("lineage", {"delta_id": engaged})),
            "context": text_of(await session.call_tool("context", {"query": "reactor manual cabinet nine"})),
            "crystal": text_of(await session.call_tool("crystal", {})),
            "stats": text_of(await session.call_tool("stats", {})),
        }

    out = in_session(lake_file, fn)
    assert out["engaged"] and out["engaged"] != out["delta_id"]
    assert out["delta_id"] in out["lineage"]  # the engagement's provenance walks back to the row
    assert out["delta_id"] in out["context"]
    assert "no crystal yet" in out["crystal"]  # fresh lake: consolidation has not run
    stats = json.loads(out["stats"])  # compact JSON, parseable
    assert stats["rows"] >= 2
    assert stats["engagements"]["affirm"] == 1
    with Lake(lake_file, readonly=True) as lk:
        engagement = lk.get(out["engaged"])
        assert engagement is not None
        assert out["delta_id"] in engagement.derived_from


def test_crystal_log(tmp_path: Path) -> None:
    """crystal(log=true) walks the §6.3 growth log; plain crystal() still returns the rendered text."""
    from conftest import FakeThink
    lake_file = tmp_path / "claude.lake"
    with Lake(lake_file) as lk:
        rows = [lk.write(f"release note {i} about shipping small", "chat", timestamp=f"{20 - i} minutes ago") for i in range(3)]
        texts = ["I ship small releases and keep them reversible.", "I answer with the result first, no preamble.",
                 "I want speed and I keep paying for skipped tests."]
        lk.think = FakeThink([{"items": [{"op": "add", "section": s, "text": x, "cite": [r.id], "why": "founding"}
                                         for s, x, r in zip(("core", "core", "tension"), texts, rows)]}])
        crystal = lk.consolidate("crystal", min_chars=100)
        assert crystal is not None

    async def fn(session: ClientSession) -> dict[str, str]:
        return {"text": text_of(await session.call_tool("crystal", {})),
                "log": text_of(await session.call_tool("crystal", {"log": True, "limit": 2}))}

    out = in_session(lake_file, fn)
    assert out["text"] == crystal.content
    lines = out["log"].splitlines()
    assert len(lines) == 2 and lines[0] == f"{crystal.timestamp[:16]} · add · — → {texts[0]} · founding · {rows[0].id}"


def test_remember_and_lineage_show_supersession(tmp_path: Path) -> None:
    """remember appends the §5.6.2 receipt to a superseded hit's line (after the content, so the line still parses);
    lineage of the asserting container lists the pairs it holds."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_remote import seed_link

    lake_file = tmp_path / "claude.lake"
    old, new, by = seed_link(lake_file)

    async def fn(session: ClientSession) -> tuple[str, str]:
        return (text_of(await session.call_tool("remember", {"query": "tidewater Fly.io deploy"})),
                text_of(await session.call_tool("lineage", {"delta_id": by})))

    remembered, lineage = in_session(lake_file, fn)
    lines = remembered.splitlines()
    assert new in lines[0] and old in lines[1] and lines[1].endswith(f" ⟵ superseded by {new}: Hetzner")
    assert f"supersedes: {old} (Fly.io region ord) -> {new} (Hetzner)" in lineage


def test_deep_recall_runs_a_plan_and_renders_the_last_steps_hits(tmp_path: Path) -> None:
    """A live multi-step plan over a tmp lake: search + filter + union comes back as
    remember-style hit lines; an invalid plan surfaces the §5.5 validator's message
    as the tool result instead of an opaque tool error."""
    lake_file = tmp_path / "claude.lake"
    with Lake(lake_file) as lk:
        planned = lk.write("Discussed the postgres migration plan with the team", "claude-code", tags=["migration"])
        backup = lk.write("Backup finishes around 08:30 so 09:00 gives us margin", "claude-code", tags=["backup"])
        lk.write("heartbeat", "agent-heartbeat")
    plan = [
        {"id": "a", "search": "postgres migration plan", "limit": 10},
        {"id": "b", "filter": {"tags_include": ["backup"]}},
        {"id": "both", "union": ["a", "b"], "limit": 10},
    ]

    async def fn(session: ClientSession) -> tuple[str, str, str]:
        good = text_of(await session.call_tool("deep_recall", {"plan": plan}))
        bad = text_of(await session.call_tool("deep_recall", {"plan": [{"id": "x"}]}))
        agg_last = text_of(await session.call_tool("deep_recall", {"plan": [
            {"id": "a", "search": "postgres"}, {"id": "g", "aggregate": "a"},
        ]}))
        return good, bad, agg_last

    good, bad, agg_last = in_session(lake_file, fn)
    assert planned.id in good and backup.id in good
    assert "postgres migration plan" in good  # rendered like remember: content on the hit line
    assert "heartbeat" not in good  # only the last step's hits come back
    assert bad.startswith("plan error:") and "has no action" in bad
    assert agg_last.startswith("plan error:") and "aggregate" in agg_last  # recall-view semantics kept


def test_deep_recall_prints_the_sediment_row_first_over_a_served_lake(tmp_path: Path) -> None:
    """§12.11 end to end: against a `lake serve` whose think is a canned script, the deep
    recall writes the lake:sediment row server-side and the tool prints it before the hits."""
    from conftest import script, served

    file = tmp_path / "t.lake"
    with Lake(file) as lk:
        seeded = [  # two sources, so the §6.5 gate opens
            lk.write("the lake migration to sqlite started with one file", "alice", timestamp="30 minutes ago"),
            lk.write("sqlite journals made the lake migration safe", "bob", timestamp="20 minutes ago"),
        ]
    think = script(tmp_path, "think.sh", "echo 'I recall the served take.'")

    async def fn(session: ClientSession) -> str:
        return text_of(await session.call_tool(
            "deep_recall", {"plan": [{"id": "s", "search": "lake migration sqlite"}]}
        ))

    with served(tmp_path / "home", "--file", str(file), "--think", f"cmd:{think}") as url:
        out = in_session(tmp_path / "unused.lake", fn, extra_env={"LAKE": url})
    first, rest = out.splitlines()[0], out.splitlines()[1:]
    assert first.startswith("sediment [") and first.endswith("]: I recall the served take.")
    assert all(d.id in "\n".join(rest) for d in seeded)  # the hits still follow, rendered as usual
    with Lake(file, readonly=True) as lk:
        rows = lk.recall(kind="sediment", limit=5)
        assert len(rows) == 1 and rows[0].delta.source == "lake:sediment"
        assert rows[0].delta.id in first  # the printed row is the row in the lake
        assert set(rows[0].delta.derived_from) == {d.id for d in seeded}


def test_write_with_kind_sediment_and_derived_from_lands_in_the_file(tmp_path: Path) -> None:
    """A deliberate sediment written through the tool: kind and derived_from land on the row."""
    lake_file = tmp_path / "claude.lake"

    async def fn(session: ClientSession) -> tuple[str, str, str]:
        first = text_of(await session.call_tool("write", {"content": "the reactor manual lives in cabinet nine"})).strip()
        second = text_of(await session.call_tool("write", {"content": "cabinet nine moved to the annex last week"})).strip()
        sediment = text_of(await session.call_tool("write", {
            "content": "I recall the reactor manual living in cabinet nine, which moved to the annex.",
            "kind": "sediment",
            "derived_from": [first, second],
        })).strip()
        return first, second, sediment

    first, second, sediment = in_session(lake_file, fn)
    assert sediment and sediment not in (first, second)
    with Lake(lake_file, readonly=True) as lk:
        row = lk.get(sediment)
        assert row is not None
        assert row.kind == "sediment"
        assert row.derived_from == [first, second]
        assert row.source == "claude-code"  # a deliberate take stays under the caller's source
        assert row.level == 0


def test_write_with_kind_but_no_derived_from_returns_the_closed_loop_message(tmp_path: Path) -> None:
    """The library enforces the closed loop; the tool surfaces its messages as the result."""
    lake_file = tmp_path / "claude.lake"

    async def fn(session: ClientSession) -> tuple[str, str, str]:
        loop = text_of(await session.call_tool("write", {"content": "an ungrounded take", "kind": "sediment"}))
        bad_kind = text_of(await session.call_tool(
            "write", {"content": "x", "kind": "engagement", "derived_from": ["feedbeefdead"]}
        ))
        missing = text_of(await session.call_tool(
            "write", {"content": "y", "kind": "sediment", "derived_from": ["feedbeefdead"]}
        ))
        return loop, bad_kind, missing

    loop, bad_kind, missing = in_session(lake_file, fn)
    assert "needs a non-empty derived_from" in loop  # ClosedLoopError, SPEC §4.4 step 3
    assert "kind must be omitted or one of" in bad_kind and "engage" in bad_kind  # §4.4 set, tool-side
    assert "derived_from ids not in this lake" in missing  # NotFoundError
    with Lake(lake_file, readonly=True) as lk:
        assert lk.stats()["rows"] == 0  # none of the three wrote anything


# --- lake unavailable at startup: the tools still serve and recover per call ----------------------


def test_unwritable_lake_file_still_serves_the_tools(tmp_path: Path) -> None:
    """A LAKE_FILE that can never be created no longer degrades the whole server to lake_status:
    the eight tools are served, and a write reports the problem per call (and would recover if the
    path became usable) rather than freezing the session."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    lake_file = blocker / "claude.lake"

    async def fn(session: ClientSession) -> tuple[set[str], str]:
        names = await tool_names(session)
        wrote = text_of(await session.call_tool("write", {"content": "a note that cannot land"}))
        return names, wrote

    names, wrote = in_session(lake_file, fn)
    assert names == set(EIGHT_TOOLS)  # served, not collapsed to a lone lake_status
    assert "can't reach the lake" in wrote and str(lake_file) in wrote


def test_unreachable_lake_url_still_serves_the_tools(tmp_path: Path) -> None:
    """SPEC §13.8: a LAKE naming a dead server does not degrade the server. The eight
    tools are served; a call reports the unreachable URL and is retry-safe, so the tools recover on
    their own once the server is up (the old startup-only probe froze them for the whole session).
    The URL is the process LAKE here; the deprecated LAKE_FILE beside it loses (LAKE beats it)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    async def fn(session: ClientSession) -> tuple[set[str], str]:
        names = await tool_names(session)
        remembered = text_of(await session.call_tool("remember", {"query": "anything"}))
        return names, remembered

    names, remembered = in_session(
        tmp_path / "unused.lake", fn, extra_env={"LAKE": url, "LAKE_FILE": str(tmp_path / "unused.lake")}
    )
    assert names == set(EIGHT_TOOLS)  # served, not collapsed to a lone lake_status
    assert "unreachable" in remembered  # a read degrades to a warning and retries per call, no frozen session
    assert not (tmp_path / "unused.lake").exists()  # within the process env, LAKE outranks LAKE_FILE


def test_process_env_lake_file_outranks_env_file_url(tmp_path: Path) -> None:
    """SPEC §13.8/§13.9 source-major target: an explicit LAKE_FILE in the process environment is
    NOT overridden by a LAKE_URL that appears only in the env file. This is the isolation a second
    lake consumer needs on a machine whose ~/.lake/env points at someone else's remote lake — the
    consumer names its file and gets its file. The dead URL below is never contacted."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env_file = tmp_path / "lake-env"
    env_file.write_text(f"LAKE_URL=http://127.0.0.1:{port}\n", encoding="utf-8")
    lake_file = tmp_path / "unused.lake"

    async def fn(session: ClientSession) -> tuple[set[str], str, str]:
        names = await tool_names(session)
        written = text_of(await session.call_tool("write", {"content": "a private line", "source": "reader"}))
        remembered = text_of(await session.call_tool("remember", {"query": "private"}))
        return names, written, remembered

    for key in ("LAKE_FILE", "LAKE"):  # the deprecated name keeps working, and so does the new one
        mine = tmp_path / f"{key}.lake"
        names, written, remembered = in_session(
            lake_file, fn, extra_env={"LAKE": "", key: str(mine), "LAKE_ENV_FILE": str(env_file)}
        )
        assert names == set(EIGHT_TOOLS)
        assert "unreachable" not in written and "unreachable" not in remembered  # the file was used, not the URL
        assert mine.exists()  # the process-env target won; the write landed in it
        assert "private" in remembered
