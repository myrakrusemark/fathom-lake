#!/usr/bin/env python3
"""lake-memory — stdio MCP server over one lake (plugin/DESIGN.md, MCP section).

Run by Claude Code as `python3 ${CLAUDE_PLUGIN_ROOT}/mcp/server.py`; needs
`fathom-lake[mcp]` importable by that python3.  Eight thin tools over the
`lake.open()` factory: remember, deep_recall, context, write, engage,
lineage, crystal, stats.  The lake is `lake.resolve(default="~/.lake/claude.lake")`,
once at startup (SPEC §13.8: `LAKE`, a path or a `lake serve` URL, from the
process environment, else the env file; the token comes with its URL).  Read
tools open a file lake read-only where it exists; write and engage open it
writable (lake.open creates `~/.lake` on first write), and so does deep_recall.
A local file gets no think, so deep_recall stays model-free there (as before
simplify/core; SPEC §6.5 sediment runs on a `lake serve` that has a think); a
remote lake is opened the one way it has.

Startup never crashes the session, and a lake that is down at startup is not
sticky: the tools are always served, each re-opens the lake per call (like the
hooks), and a call that can't reach it returns a retry-safe message and recovers
on its own once the lake is back. A startup probe only logs a one-line stderr
diagnostic. The one hard stop is the `mcp` package being unimportable, which
exits non-zero with an install hint.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_LAKE_FILE = "~/.lake/claude.lake"
HEALTH_TIMEOUT = 5.0
TRUNCATE_AT = 100
INSTRUCTIONS = (
    "Deliberate access to the lake, this user's persistent memory across Claude Code sessions. "
    "remember/context/lineage/crystal/stats read it; deep_recall runs a compositional recall "
    "plan (and the deep path may leave a sediment row); write/engage add to it."
)
WRITE_KINDS = ("container", "crystal", "mood", "sediment")  # SPEC §4.4; "engagement" only via engage()
DEEP_RECALL_DESCRIPTION = """\
Compositional recall: run a plan over the lake and get the last step's hits, rendered one
per line like remember (score, id, timestamp, source, content). When the deep recall's
automatic sediment pass writes its first-person take (SPEC §6.5, §12.11), that row is
printed first as `sediment [<id>]: <text>` — it is already in the lake, citing what it read.

You author the plan: a JSON array of steps, run in order. Each step is an object with a
unique string "id", exactly ONE action key, and optional parameters; a step may reference
earlier step ids only. The ten actions:

- "search": <text> — scored full-text/semantic search.
- "filter": {<filter params>} — no query; matching rows newest first.
- "intersect": [<step ids>] — rows present in every referenced step (2+ refs).
- "union": [<step ids>] — rows in any referenced step, best score kept (2+ refs).
- "diff": [<step ids>] — rows in the first step and none of the rest (2+ refs).
- "bridge": [<step ids>] — rows related to ALL inputs at once, the connective tissue
  between threads (2+ refs).
- "chain": <step id> — hop outward from that step's results to associatively related rows.
- "aggregate": <step id> — bucket counts by "group_by" (hour, day, week, month, tag,
  source, kind); must not be the last step.
- "neighbors": <step id> — rows within "radius_minutes" (default 30) of each hit, same
  source unless "source_match": false, "limit_per_seed" (6) per hit.
- "timeline": <step id> — chronological strips around each hit; "radius_minutes" (30),
  "max_per_side" (15), "gap_minutes" (30).

Filter params, valid on search/filter/chain/bridge (the tag and source ones also on
neighbors/timeline): "tags_include", "tags_exclude", "any_tags", "source",
"exclude_sources", "kind", "has_media", "since"/"until" (ISO or "2 weeks ago"),
"limit" (default 100).

Example — a filtered search:
[{"id": "a", "search": "postgres migration decision", "tags_include": ["migration"],
  "since": "30 days ago", "limit": 10}]

Example — a bridge between two threads:
[{"id": "a", "search": "kitchen cooking"},
 {"id": "b", "search": "greenhouse build"},
 {"id": "c", "bridge": ["a", "b"], "limit": 10}]

`limit` here caps how many hit lines are rendered; step limits decide what is recalled.
"""


def resolve_target() -> Any:
    """The lake this server serves: lake.resolve() with the plugin's default file (SPEC §13.8). An importable lake
    older than lake.resolve (a split install) gets the resolver from the checkout this plugin lives in, by path
    (lake/_env.py is a leaf, safe to load alone), as the hooks do."""
    import lake as lake_pkg

    resolver = getattr(lake_pkg, "resolve", None)
    if resolver is None:
        import importlib.util

        path = Path(__file__).resolve().parents[2] / "lake" / "_env.py"
        spec = importlib.util.spec_from_file_location("lake_env", path)
        if spec is None or spec.loader is None or not path.is_file():
            raise ImportError(f"lake has no resolve and {path} is missing")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["lake_env"] = mod  # dataclasses resolves the module by name
        spec.loader.exec_module(mod)
        resolver = mod.resolve
    return resolver(default=DEFAULT_LAKE_FILE)


def label(target: Any) -> str:
    return f"LAKE={target.target}"


def oneline(text: str, limit: int = TRUNCATE_AT) -> str:
    """Whitespace collapsed to single spaces, truncated with an ellipsis."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def superseded(delta: Any) -> str:
    """The §5.6.2 supersession receipt: ` ⟵ superseded by {id}: {new value}` for the newest superseder, else ''.
    getattr: a Delta from an older library has no superseded_by."""
    links = getattr(delta, "superseded_by", ())
    return f" ⟵ superseded by {links[0].id}: {oneline(links[0].new_value, 60)}" if links else ""


def hit_lines(hits: list[Any]) -> list[str]:
    """Recall hits rendered one per line: score, id, timestamp, source, content, and the supersession receipt."""
    return [
        f"{h.score:.3f}  {h.delta.id}  {h.delta.timestamp[:16]}  {h.delta.source}  {oneline(h.delta.content)}"
        f"{superseded(h.delta)}"
        for h in hits
    ]


def server_class() -> Any:
    """The FastMCP-style server class: MCPServer on mcp 2.x, FastMCP on 1.x."""
    try:
        from mcp.server.mcpserver import MCPServer

        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP

        return FastMCP


def probe(target: Any) -> str | None:
    """What is wrong with serving this lake — None when it is servable."""
    if target.remote:
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(
                target.target + "/v1/health", timeout=HEALTH_TIMEOUT
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return f"cannot reach lake server {target.target}: {exc}"
        if body.get("ok") is not True:
            return f"lake server {target.target} is unhealthy: {body!r}"
        return None
    path = Path(target.target)
    if path.exists():
        try:
            open_lake(target, readonly=True).close()
        except Exception as exc:
            return f"cannot open lake file {path}: {exc}"
        return None
    nearest = path.parent
    while not nearest.exists() and nearest != nearest.parent:
        nearest = nearest.parent
    if not nearest.is_dir():
        return f"cannot create lake file {path}: {nearest} is not a directory"
    if not os.access(nearest, os.W_OK | os.X_OK):
        return f"cannot create lake file {path}: {nearest} is not writable"
    return None


def open_lake(target: Any, *, readonly: bool = False) -> Any:
    """A Lake (or RemoteLake) over the resolved target, via lake.open. `readonly` shapes only a file open (the
    server applies it per endpoint remotely); a writable file open creates the parent directory. A local file gets
    no think, so deep_recall stays model-free on it (the §6.5 sediment pass runs only on a server with a think)."""
    import lake as lake_pkg

    if target.remote:
        return lake_pkg.open(target.target, token=target.token)
    if not readonly:  # lake.open does this too; an older lake.open did not
        Path(target.target).parent.mkdir(parents=True, exist_ok=True)
    return lake_pkg.open(target.target, readonly=readonly)


def no_lake_yet(path: Path) -> str:
    return f"(no lake yet at {path} — nothing recorded)"


def build_server(target: Any) -> Any:
    """The healthy server: DESIGN.md's seven tools plus deep_recall, each opening the lake per call."""
    server = server_class()(name="lake-memory", instructions=INSTRUCTIONS)

    def not_yet() -> str | None:
        """The no-lake-yet answer for a file target that does not exist; a
        read tool on a missing file answers, never creates it. A remote
        target exists by definition — the probe reached it."""
        if not target.remote and not Path(target.target).exists():
            return no_lake_yet(Path(target.target))
        return None

    def unreachable(exc: object) -> str:
        return (
            f"lake-memory can't reach the lake right now ({label(target)}): {exc}. "
            "This clears on its own once the lake is back — just call again."
        )

    def answer(readonly: bool, work: Callable[[Any], str], *, allow_create: bool = False) -> str:
        """Open the lake per call, run work(lk), always close. A read tool on a not-yet-created
        file answers via not_yet; any failure to reach or open the lake returns a retry-safe
        message instead of raising, so the tools recover on their own once the lake is back — the
        hooks already work this way, and only the MCP server used to freeze its state at startup."""
        if not allow_create:
            miss = not_yet()
            if miss is not None:
                return miss
        try:
            lk = open_lake(target, readonly=readonly)
        except Exception as exc:  # unreachable server, uncreatable file: report, do not crash the tool
            return unreachable(exc)
        try:
            return work(lk)
        except Exception as exc:  # domain errors are handled inside work; this is I/O / connectivity
            return unreachable(exc)
        finally:
            lk.close()

    @server.tool()
    def remember(query: str, source: str | None = None, tags: list[str] | None = None, limit: int = 20) -> str:
        """Search the lake. Scored hits, one line each: score, id, timestamp, source, content."""
        def work(lk: Any) -> str:
            hits = lk.recall(query, source=source, tags=tags, limit=limit)
            lines = hit_lines(hits)
            lines.extend(f"warning: {w}" for w in lk.last_warnings)
            return "\n".join(lines) or "(no matches)"
        return answer(True, work)

    @server.tool(description=DEEP_RECALL_DESCRIPTION)
    def deep_recall(plan: list[dict[str, Any]], limit: int | None = None) -> str:
        """The §5.5 plan runner; DEEP_RECALL_DESCRIPTION teaches the caller the DSL."""
        def work(lk: Any) -> str:  # writable: a readonly file open would gate off the §6.5 sediment pass
            try:
                if plan and "aggregate" in plan[-1]:
                    # recall(plan=...) refuses an aggregate last step in validation, before any step runs
                    lk.recall(plan=plan)
                result = lk.plan(plan)  # PlanResult.sediment is the §12.11 surface for the row (§4.5)
                warnings = list(lk.last_warnings)
                last = list(result.steps.values())[-1] if result.steps else None
                if last is None:
                    hits: list[Any] = []
                elif last.hits is not None:
                    hits = last.hits
                else:  # timeline last step: the recall view flattens the strips; the pass already ran
                    hits = lk.recall(plan=plan, sediment=False)
                    warnings.extend(w for w in lk.last_warnings if w not in warnings)
            except ValueError as exc:  # PlanError included — the validator's message names the fix
                return f"plan error: {exc}"
            row = result.sediment
            lines = [] if row is None else [f"sediment [{row.id}]: {row.content}"]
            shown = hits if limit is None else hits[: max(limit, 0)]
            lines.extend(hit_lines(shown))
            if len(shown) < len(hits):
                lines.append(f"({len(hits) - len(shown)} more hits not rendered)")
            lines.extend(f"warning: {w}" for w in warnings)
            return "\n".join(lines) or "(no matches)"
        return answer(False, work)  # not_yet guards a missing file; writable open enables the sediment write

    @server.tool()
    def context(query: str, budget: int = 24000) -> str:
        """Render the full memory context block for a query — a deliberate deep pull."""
        def work(lk: Any) -> str:
            block = lk.context(query, budget=budget)
            warnings = list(lk.last_warnings)
            if not block and warnings:
                return "\n".join(f"warning: {w}" for w in warnings)
            return block or "(nothing relevant in the lake)"
        return answer(True, work)

    @server.tool()
    def write(
        content: str,
        tags: list[str] | None = None,
        source: str | None = None,
        kind: str | None = None,
        derived_from: list[str] | None = None,
    ) -> str:
        """Write one deliberate note into the lake. Returns the new row's id. Optional kind
        (container, crystal, mood, or sediment) marks a distillation over other rows and
        requires derived_from: the ids of the rows it was made from, in the order you read
        them (keep it to the ~20 that carried the conclusion). A deliberate take over what a
        recall surfaced is kind="sediment" plus derived_from=[the ids you read]. source
        defaults to this host's name (LAKE_SOURCE, else claude-code)."""
        source = source or os.environ.get("LAKE_SOURCE") or "claude-code"
        if kind is not None and kind not in WRITE_KINDS:
            return (
                f"kind must be omitted or one of {', '.join(WRITE_KINDS)}, not {kind!r}"
                " (engagement rows are written by the engage tool)"
            )
        from lake import ClosedLoopError, NotFoundError

        def work(lk: Any) -> str:
            try:
                return lk.write(content, source, tags=tags, kind=kind, derived_from=derived_from).id
            except (ClosedLoopError, NotFoundError) as exc:  # the library's closed loop, surfaced as the answer
                return str(exc)
        return answer(False, work, allow_create=True)

    @server.tool()
    def engage(delta_id: str, kind: str, note: str | None = None) -> str:
        """Correct or endorse a remembered row. Use this whenever the user says a recalled
        memory is wrong, outdated, or misattributed (kind="refute", with the correction in
        note) or confirms one as important or exactly right (kind="affirm"). kind="reply"
        attaches a follow-up thought. A refuted row sinks in every later search and the next
        consolidation stops repeating it; an affirmed row rises. Returns the engagement id."""
        from lake import NotFoundError

        def work(lk: Any) -> str:
            try:
                return lk.engage(delta_id, kind, note=note).id
            except (NotFoundError, ValueError) as exc:  # bad id / bad kind: the message, not a crash
                return str(exc)
        return answer(False, work, allow_create=True)

    @server.tool()
    def lineage(delta_id: str) -> str:
        """Walk a row's provenance, one line per ancestor: id, timestamp, source, kind, content."""
        from lake import NotFoundError

        def work(lk: Any) -> str:
            try:
                lin = lk.lineage(delta_id)
            except NotFoundError as exc:  # unknown id: the message, not an unreachable-lake report
                return str(exc)
            lines = [
                f"{d.id}  {d.timestamp[:16]}  {d.source}  {d.kind or 'plain'}  {oneline(d.content, 80)}"
                for d in lin.rows
            ]
            if lin.dangling:
                lines.append("dangling: " + ", ".join(lin.dangling))
            root = lk.get(delta_id, include_expired=True)
            pairs = (root.meta or {}).get("supersedes") if root is not None and root.kind == "container" else None
            for e in pairs if isinstance(pairs, list) else ():  # §5.6.2: the links this container asserts
                if isinstance(e, dict):
                    lines.append(f"supersedes: {e.get('old')} ({oneline(str(e.get('old_value')), 60)})"
                                 f" -> {e.get('new')} ({oneline(str(e.get('new_value')), 60)})")
            return "\n".join(lines) or "(no ancestry — the row cites nothing)"
        return answer(True, work)

    @server.tool()
    def crystal(log: bool = False, limit: int = 20) -> str:
        """The current identity crystal text. With log=true, its growth log instead (SPEC §6.3): what changed in
        the crystal, newest first, as `time · op · old → new · why · cited ids`."""
        def work(lk: Any) -> str:
            d = lk.crystal()
            if not log:
                return d.content if d is not None else "(no crystal yet — consolidation has not run)"
            from lake._consolidate import growth_log  # well-formed meta.edits only, walked back via prior_id

            lines = [f"{e['at'][:16]} · {e['op']} · {oneline(str(e.get('old') or '—'), 80)} → "
                     f"{oneline(str(e.get('new') or '—'), 80)} · {e.get('why') or '—'} · {', '.join(e['cite'])}"
                     for e in growth_log(lk, limit)]
            return "\n".join(lines) or "(no growth log yet)"
        return answer(True, work)

    @server.tool()
    def stats() -> str:
        """Lake counts and coverage (SPEC §4.8), as compact JSON."""
        if not target.remote and not Path(target.target).exists():
            return json.dumps({"path": target.target, "exists": False}, separators=(",", ":"))
        def work(lk: Any) -> str:
            return json.dumps(lk.stats(), ensure_ascii=False, separators=(",", ":"))
        return answer(True, work, allow_create=True)

    return server


def main() -> int:
    try:
        target = resolve_target()
    except Exception as exc:
        print(f"lake-memory MCP server cannot start: the lake package is not importable by {sys.executable}"
              f" (or has no lake.resolve): {exc}; pip install 'fathom-lake[mcp]'", file=sys.stderr)
        return 1
    for note in target.notes:  # e.g. "LAKE_URL is deprecated; use LAKE=<the same value>"
        print(f"lake-memory: {note}", file=sys.stderr)
    try:
        server_class()
    except ImportError as exc:
        print(
            f"lake-memory MCP server cannot start: the mcp package is not importable by "
            f"{sys.executable}: {exc}; pip install 'fathom-lake[mcp]'",
            file=sys.stderr,
        )
        return 1
    # The lake being down at startup is not fatal and not sticky: always serve the tools, and let
    # each one re-open per call (like the hooks) so they recover on their own. The startup probe is
    # kept only as a one-line diagnostic — it no longer freezes the server into a degraded state.
    problem = probe(target)
    if problem is not None:
        print(f"lake-memory: lake unreachable at startup ({problem}); tools will retry per call", file=sys.stderr)
    server = build_server(target)
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
