"""`lake` command line (SPEC §8): a table-driven argparse tree, one small function per command."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from . import __version__, _digest, adapters
from . import open as open_target
from ._consolidate import growth_log
from ._env import Config, Target, config, resolve
from ._recall import oneline
from ._types import ConsolidateRun, Delta, Hit, LakeError
from .lake import Lake
from .remote import RemoteLake

READONLY_CMDS: frozenset[str] = frozenset(
    {"recall", "context", "system-prompt", "due", "crystal", "lineage", "stats", "export"}
)
FILTER_KEYS: tuple[str, ...] = ("source", "tags", "any_tags", "exclude_tags")
ANCHOR_KEYS: tuple[str, ...] = FILTER_KEYS + ("kind", "since", "until")
CONSOLIDATE_KEYS: tuple[str, ...] = FILTER_KEYS + (
    "advance_watermark", "inputs", "close_gap", "cluster_gap", "lookback", "max_clusters",
    "min_rows", "max_rows", "budget", "noise", "add_tags", "add_meta", "system", "instructions", "min_chars",
    "lease", "session_prefix", "session_gap", "backfill", "backfill_max", "max_edits",
)


class Out(NamedTuple):
    """What a command hands back: its text, its --json document, and the exit code."""

    text: str
    data: object
    code: int = 0


# --- argument tables ------------------------------------------------------------------------------


def opt(*flags: str, **kw: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    return flags, kw


def json_object(text: str) -> dict[str, Any]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise argparse.ArgumentTypeError("expected a JSON object")
    return obj


def text_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    """The §8 command table. Global options are accepted before and after the command name."""
    off = {"action": "store_const", "const": False, "default": None}  # a --no-X flag that passes False only when given
    on = {"action": "store_const", "const": True, "default": None}
    rep = {"action": "append"}
    filters = (
        opt("--source", **rep), opt("--tag", dest="tags", **rep), opt("--any-tag", dest="any_tags", **rep),
        opt("--exclude-tag", dest="exclude_tags", **rep),
    )
    anchor = filters + (opt("--kind"), opt("--since"), opt("--until"))
    rank = (
        opt("--no-noise", dest="noise", action="store_false"), opt("--no-recency", dest="recency", action="store_false"),
        opt("--min-relevance", type=float, default=0.0),
    )
    globals_ = (
        opt("--lake", metavar="TARGET", help="the lake: a file or an http(s) URL (or LAKE)"),
        opt("--file", help="deprecated alias of --lake"), opt("--url", dest="lake_url", help="deprecated alias of --lake"),
        opt("--default", help=argparse.SUPPRESS),  # a host's fallback file when nothing names a lake (the plugin's)
        opt("--json", action="store_true"),
        opt("--think", metavar="SPEC"), opt("--embed", metavar="SPEC"), opt("--model-name"),
        opt("--num-ctx", type=int), opt("--num-predict", type=int), opt("--think-timeout", type=float, default=1800.0),
        opt("--embed-timeout", type=float, default=10.0), opt("--exclude-source", **rep), opt("--collapse-source", **rep),
        opt("--automation", metavar="RULE", **rep),
    )
    commands: tuple[tuple[str, str, tuple[tuple[tuple[str, ...], dict[str, Any]], ...], Callable[..., Out]], ...] = (
        ("write", "write one row; CONTENT of - reads stdin", (
            opt("content"), opt("--source", required=True), opt("--tag", dest="tags", **rep), opt("--kind"),
            opt("--from", dest="derived_from", **rep), opt("--expires"), opt("--media"), opt("--meta", type=json_object),
            opt("--timestamp"), opt("--no-dedupe", dest="dedupe", action="store_false"),
            opt("--no-embed", dest="embed_row", **off),
        ), cmd_write),
        ("recall", "scored hits, or a plan's hits", anchor + rank + (
            opt("query", nargs="?"), opt("--limit", type=int, default=20), opt("--include-expired", action="store_true"),
            opt("--plan", metavar="FILE|-"),
        ), cmd_recall),
        ("context", "render the prompt block", anchor + rank + (
            opt("query", nargs="?"), opt("--budget", type=int, default=8000), opt("--limit", type=int, default=30),
            opt("--no-crystal", dest="crystal", action="store_false"),
            opt("--no-containers", dest="containers", action="store_false"),
            opt("--min-query-chars", type=int, default=10), opt("--vector", action="store_true"),
            opt("--label", metavar="KEY=VALUE", **rep),
        ), cmd_context),
        ("system-prompt", "render the crystal + recent moods as a system message (§5.7)", (
            opt("--moods", type=int, default=3), opt("--budget", type=int, default=8000),
            opt("--label", metavar="KEY=VALUE", **rep),
        ), cmd_system_prompt),
        ("engage", "affirm, refute, or reply to a row", (
            opt("id"), opt("kind"), opt("--by"), opt("--note"), opt("--tag", dest="tags", **rep),
            opt("--no-snapshot", dest="snapshot", action="store_false"),
            opt("--no-dedupe", dest="dedupe", action="store_false"),
        ), cmd_engage),
        ("consolidate", "run the model over a kind (container, mood, crystal)", filters + (
            opt("kind"), opt("--window"), opt("--since"), opt("--until"), opt("--advance-watermark", **on),
            opt("--inputs", nargs="+"), opt("--close-gap"), opt("--cluster-gap"), opt("--lookback"),
            opt("--max-clusters", type=int), opt("--min-rows", type=int), opt("--max-rows", type=int),
            opt("--budget", type=int), opt("--no-noise", dest="noise", **off), opt("--add-tag", dest="add_tags", **rep),
            opt("--add-meta", type=json_object), opt("--system", type=text_file, metavar="FILE"),
            opt("--instructions", type=text_file, metavar="FILE"), opt("--min-chars", type=int), opt("--lease"),
            opt("--force", action="store_true"), opt("--session-prefix"), opt("--session-gap"),
            opt("--no-backfill", dest="backfill", **off), opt("--backfill-max", type=int), opt("--max-edits", type=int),
        ), cmd_consolidate),
        ("digest", "run whatever consolidation is due: containers, the catch-up, mood, crystal (§6.6)", (
            opt("--since", metavar="YYYY-MM-DD"), opt("--max-units", type=int), opt("--max-tokens", type=int),
            opt("--backfill-max", type=int), opt("--dry-run", action="store_true"),
        ), cmd_digest),
        ("due", "exit 0 when a consolidate kind is due", (opt("kind"),), cmd_due),
        ("crystal", "print the crystal text, or its growth log", (
            opt("--write-file", action="store_true"), opt("--log", action="store_true"), opt("--limit", type=int, default=20),
        ), cmd_crystal),
        ("lineage", "walk a row's derived_from ancestry", (opt("id"), opt("--depth", type=int)), cmd_lineage),
        ("stats", "counts and coverage", (), cmd_stats),
        ("sweep", "delete expired rows and orphan media", (), cmd_sweep),
        ("export", "write JSONL", (
            opt("path"), opt("--include-expired", action="store_true"), opt("--vectors", action="store_true"),
        ), cmd_export),
        ("import", "read lake-format JSONL", (
            opt("path"), opt("--include-expired", action="store_true"),
        ), cmd_import),
        ("embed-missing", "embed live rows that have no vector", (opt("--batch-size", type=int, default=64),),
         cmd_embed_missing),
        ("spool", "count offline spooled writes, or --flush them to the server", (
            opt("--flush", action="store_true"),
        ), cmd_spool),
        # serve (remote host)
        ("serve", "host this lake over HTTP (§13)", (
            opt("--bind", default="127.0.0.1"), opt("--port", type=int, default=8377),
            opt("--digest", default="off", metavar="off|nightly|HH:MM|every:DURATION"), opt("--backfill-max", type=int),
            opt("--max-units", type=int), opt("--max-tokens", type=int),
        ), cmd_serve),
    )
    parser = argparse.ArgumentParser(prog="lake", description="A closed-loop memory in one SQLite file.")
    parser.add_argument("-V", "--version", action="version", version=f"lake {__version__}")
    add_all(parser, globals_)
    subs = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    for name, help_, args, func in commands:
        sub = subs.add_parser(name, help=help_, description=help_)
        add_all(sub, args)
        add_all(sub, globals_, suppress=True)
        sub.set_defaults(func=func)
    return parser


def add_all(
    parser: argparse.ArgumentParser, args: Sequence[tuple[tuple[str, ...], dict[str, Any]]], *, suppress: bool = False
) -> None:
    """Add a table of options; `suppress` makes them optional overrides (a subparser's copy of the globals)."""
    for flags, kw in args:
        parser.add_argument(*flags, **({**kw, "default": argparse.SUPPRESS} if suppress else kw))


# --- helpers ---------------------------------------------------------------------------------------


def given(a: argparse.Namespace, keys: Sequence[str]) -> dict[str, Any]:
    """The keyword arguments among `keys` that the command line actually set."""
    return {k: getattr(a, k) for k in keys if getattr(a, k, None) is not None}


def to_obj(x: object) -> object:
    """A dataclass (nested) or a list of them as plain JSON-able data."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return dataclasses.asdict(x)
    return [to_obj(v) for v in x] if isinstance(x, list) else x


def kv_lines(d: dict[str, Any]) -> str:
    return "\n".join(f"{k}: {v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in d.items())


def warn(messages: Sequence[str]) -> None:
    for m in messages:
        print(f"warning: {m}", file=sys.stderr)


def hops_of(root: Delta | None, rows: Sequence[Delta]) -> dict[str, int]:
    """Hop count per ancestor id, walking derived_from breadth first from the root."""
    by_id = {d.id: d for d in rows}
    hops: dict[str, int] = {}
    frontier, n = ([root] if root else []), 0
    while frontier:
        n += 1
        new = list(dict.fromkeys(p for d in frontier for p in d.derived_from if p in by_id and p not in hops))
        hops.update((p, n) for p in new)
        frontier = [by_id[p] for p in new]
    return hops


def open_lake(a: argparse.Namespace, t: Target, readonly: bool) -> Lake | RemoteLake:
    """The lake a command works on (§8), configured by the one host rule (config(), §13.8). Only a local
    `consolidate` or `digest` runs a model, so only they take LAKE_THINK / LAKE_EMBED (and `digest` the default
    model); other commands take only the flags, and `context` embeds only with --vector. A dry run needs no model."""
    if t.remote:
        return RemoteLake(t.target, token=t.token)
    c = host_config(a, digest=a.cmd == "digest")
    models = a.cmd == "consolidate" or a.cmd == "digest" and not a.dry_run  # the rest take only the flags
    spec, espec = (c.think, c.embed) if models else (a.think, a.embed)
    think = adapters.make_think(spec, timeout=a.think_timeout, num_ctx=a.num_ctx, num_predict=a.num_predict) if spec else None
    embed = adapters.make_embed(espec, timeout=a.embed_timeout) if espec and (a.cmd != "context" or a.vector) else None
    model = a.model_name or (adapters.model_name(spec) if spec else None)
    return open_target(t.target, think=think, embed=embed, model_name=model, automation=c.automation, readonly=readonly)


def old_flags(a: argparse.Namespace) -> list[str]:
    """§13.8 deprecation notes for --file / --url (removed in 0.2.0, with the other aliases)."""
    return [f"{f} is deprecated; use --lake (removed in 0.2.0)" for f, v in (("--file", a.file), ("--url", a.lake_url)) if v]


def host_config(a: argparse.Namespace, *, digest: bool = False) -> Config:
    """config() with the flags as overrides: --think, --embed, --automation; the deprecated --collapse-source and
    --exclude-source add source: rules. Deprecation notes go to stderr."""
    c = config(a.think, a.embed, a.automation, digest=digest)
    if old := [*(a.collapse_source or ()), *(a.exclude_source or ())]:
        c = dataclasses.replace(c, automation=(*c.automation, *(f"source:{s}" for s in old)), notes=(*c.notes, (
            "--collapse-source/--exclude-source are deprecated: these sources are now automation (searchable, never"
            " consolidated); use --automation source:<name>")))
    warn(c.notes)
    return c


# --- commands --------------------------------------------------------------------------------------


def hit_line(h: Hit) -> str:
    d = h.delta
    return f"{h.score:.3f}  {d.id}  {d.timestamp[:16]}  {d.source}  {oneline(d.content, 100)}"


def cmd_write(lake: Lake, a: argparse.Namespace) -> Out:
    content = sys.stdin.read() if a.content == "-" else a.content
    d = lake.write(
        content, a.source, tags=a.tags, kind=a.kind, derived_from=a.derived_from, expires=a.expires, media=a.media,
        meta=a.meta, timestamp=a.timestamp, dedupe=a.dedupe, embed=a.embed_row,
    )
    return Out(d.id, d)


def cmd_recall(lake: Lake, a: argparse.Namespace) -> Out:
    keys = given(a, ANCHOR_KEYS)
    if a.plan is not None:
        steps = json.loads(sys.stdin.read() if a.plan == "-" else Path(a.plan).read_text(encoding="utf-8"))
        if a.include_expired:
            keys["include_expired"] = True
        if a.json:
            return Out("", lake.plan(steps, filters=keys or None))
        hits = lake.recall(plan=steps, filters=keys or None)
    else:
        hits = lake.recall(
            a.query, limit=a.limit, include_expired=a.include_expired, noise=a.noise, recency=a.recency,
            min_relevance=a.min_relevance, **keys,
        )
    return Out("\n".join(hit_line(h) for h in hits), hits)


def cmd_context(lake: Lake, a: argparse.Namespace) -> Out:
    query = a.query if a.query and len(a.query) >= a.min_query_chars else None
    labels = parse_labels(a.label)
    res = lake.context_blocks(
        query, budget=a.budget, limit=a.limit, crystal=a.crystal, containers=a.containers, noise=a.noise,
        recency=a.recency, min_relevance=a.min_relevance, labels=labels or None, **given(a, ANCHOR_KEYS),
    )
    return Out(res.rendered, res)


def parse_labels(pairs: Sequence[str] | None) -> dict[str, str]:
    """--label KEY=VALUE flags into a dict (a missing '=' is a ValueError)."""
    labels: dict[str, str] = {}
    for kv in pairs or ():
        if "=" not in kv:
            raise ValueError(f"--label expects KEY=VALUE, got {kv!r}")
        key, value = kv.split("=", 1)
        labels[key] = value
    return labels


def cmd_system_prompt(lake: Lake, a: argparse.Namespace) -> Out:
    text = lake.system_prompt(moods=a.moods, budget=a.budget, labels=parse_labels(a.label) or None)
    return Out(text, text)


def cmd_engage(lake: Lake, a: argparse.Namespace) -> Out:
    d = lake.engage(a.id, a.kind, by=a.by, note=a.note, tags=a.tags, snapshot=a.snapshot, dedupe=a.dedupe)
    return Out(d.id, d)


def cmd_consolidate(lake: Lake, a: argparse.Namespace) -> Out:
    empty = ConsolidateRun(a.kind, [], 0, [], 0, None)
    if (a.since is None) != (a.until is None):
        raise ValueError("--since and --until go together")
    if not a.force and not lake.due(a.kind):
        return Out("nothing to do", empty)
    window = a.window if a.window else ((a.since, a.until) if a.since else None)
    lake.consolidate(a.kind, window, **given(a, CONSOLIDATE_KEYS))
    run = lake.last_run or empty
    if run.written or run.skipped:
        print(f"skipped: {run.skipped}", file=sys.stderr)
    warn(run.warnings)
    return Out("\n".join(d.id for d in run.written) or "nothing to do", run)


def cmd_digest(lake: Lake, a: argparse.Namespace) -> Out:
    run = lake.digest(since=a.since, max_units=a.max_units, max_tokens=a.max_tokens, backfill_max=a.backfill_max,
                      dry_run=a.dry_run)
    lines = [f"{s.step}: {'due' if s.due else 'nothing due'}" if s.run is None else f"{s.step}: {len(s.run.written)}"
             f" written, {s.run.skipped} skipped, ~{s.prompt_chars / _digest.CHARS_PER_TOKEN:.0f} input tokens"
             for s in run.steps]
    warn([*run.warnings, *(w for s in run.steps if s.run for w in s.run.warnings)])
    lines += [f"error: {e}" for e in run.errors] + [("dry run: " if a.dry_run else "") + _digest.summary(run)]
    return Out("\n".join(lines), run, 1 if run.errors else 0)


def cmd_due(lake: Lake, a: argparse.Namespace) -> Out:
    due = lake.due(a.kind)
    return Out("yes" if due else "no", {"due": due}, 0 if due else 1)


def cmd_crystal(lake: Lake, a: argparse.Namespace) -> Out:
    if a.log:  # §6.3 growth log, newest first
        log = growth_log(lake, a.limit)
        lines = [f"{e['at'][:16]} · {e['op']} · {oneline(str(e.get('old') or '—'), 80)} → {oneline(str(e.get('new') or '—'), 80)}"
                 f" · {e.get('why') or '—'} · {', '.join(e['cite'])}" for e in log]
        return Out("\n".join(lines) or "(no growth log yet)", log)
    d = lake.crystal()
    if d is not None and a.write_file:
        tmp = lake.crystal_path.with_suffix(".md.tmp")
        tmp.write_text(d.content + "\n", encoding="utf-8")
        os.replace(tmp, lake.crystal_path)
    return Out(d.content if d else "", d)


def cmd_lineage(lake: Lake, a: argparse.Namespace) -> Out:
    lin = lake.lineage(a.id, depth=a.depth)
    hops = hops_of(lake.get(a.id, include_expired=True), lin.rows)
    lines = [
        f"{hops.get(d.id, 0)}  {d.id}  {d.timestamp[:16]}  {d.source}  {d.kind or 'plain'}  {oneline(d.content, 80)}"
        for d in lin.rows
    ]
    if lin.dangling:
        lines.append("dangling: " + ", ".join(lin.dangling))
    return Out("\n".join(lines), {"rows": to_obj(lin.rows), "hops": hops, "dangling": lin.dangling})


def cmd_stats(lake: Lake, a: argparse.Namespace) -> Out:
    s = lake.stats()
    return Out(kv_lines(s), s)


def cmd_sweep(lake: Lake, a: argparse.Namespace) -> Out:
    counts = lake.sweep()
    return Out(kv_lines(counts), counts)


def cmd_export(lake: Lake, a: argparse.Namespace) -> Out:
    n = lake.export(a.path, include_expired=a.include_expired, vectors=a.vectors)
    return Out(str(n), {"rows": n})


def cmd_import(lake: Lake, a: argparse.Namespace) -> Out:
    counts = lake.import_(a.path, include_expired=a.include_expired)
    return Out(kv_lines(counts), counts)


def cmd_embed_missing(lake: Lake, a: argparse.Namespace) -> Out:
    n = lake.embed_missing(batch_size=a.batch_size)
    return Out(str(n), {"embedded": n})


def cmd_spool(lake: RemoteLake, a: argparse.Namespace) -> Out:
    """§13.9 spool: the line count under LOCK_SH, or --flush now (a dead server is exit 1 via LakeError)."""
    if a.flush:
        counts = lake.flush_spool()
        if counts is None:
            return Out("spool empty", {"written": 0, "skipped": 0, "errors": 0})
        return Out("flushed: written {written}, skipped {skipped}, errors {errors}".format(**counts), counts)
    count, oldest = lake.spool_status()
    if count == 0 or oldest is None:
        return Out("spool empty", {"count": 0, "oldest": None})
    return Out(f"{count} spooled write{'s' if count != 1 else ''}, oldest {oldest[:16]}",
               {"count": count, "oldest": oldest})


def cmd_serve(lake: Lake, a: argparse.Namespace) -> Out:
    # serve (remote host): never reached — main() hands `serve` to lake.serve.run() before opening a Lake
    raise AssertionError("unreachable: serve is dispatched in main()")


# --- entry point -----------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Exit 0 on success (1 for `due` when not due), 1 on a library error, 2 on a usage error."""
    try:
        a = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:  # argparse's --help/--version (0) and usage errors (2)
        return exc.code if isinstance(exc.code, int) else 2
    if a.cmd == "serve":  # serve (remote host): §13.2 owns its startup order
        from . import serve
        return serve.run(a)
    try:  # §13.8: --lake/--url/--file, else the process environment, else the env file, else --default
        t = resolve(a.lake or a.lake_url or a.file, default=a.default)
    except ValueError as exc:
        print(f"lake: {exc} (--lake PATH|URL)", file=sys.stderr)
        return 2
    warn([*old_flags(a), *t.notes])
    if a.cmd == "spool" and not t.remote:
        print("lake spool needs a remote lake (set LAKE to its URL, or --lake URL)", file=sys.stderr)
        return 2
    if t.remote and a.cmd in ("consolidate", "digest") and (a.think or a.embed):
        print("lake: --think and --embed are server-side on a remote lake (lake serve --think ...);"
              " drop the flag or point LAKE at the file", file=sys.stderr)
        return 2
    if t.remote and a.cmd == "crystal" and a.write_file:
        print("lake: --write-file needs the file; not available over HTTP", file=sys.stderr)
        return 2
    readonly = (a.cmd in READONLY_CMDS and not (a.cmd == "crystal" and a.write_file) or a.cmd == "digest" and a.dry_run) \
        and Path(t.target).exists()
    try:
        with open_lake(a, t, readonly) as target:
            out: Out = a.func(target, a)
            warn(target.last_warnings)
    except LakeError as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"lake: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(to_obj(out.data), ensure_ascii=False))
    elif out.text:
        print(out.text.rstrip("\n"))
    return out.code


if __name__ == "__main__":
    raise SystemExit(main())
