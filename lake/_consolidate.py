"""consolidate(), crystal(), due(): clustering, prompts, validation, lease, watermark (SPEC §6)."""

from __future__ import annotations

import itertools
import json
import math
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from importlib import resources
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import _db, _recall, _store, _time, _vectors
from ._answer import check_citations, mostly_foreign, parse_answer
from ._types import ConsolidateError, ConsolidateRun, Delta, Duration, EmbedError, Hit, LakeError, LeaseHeld, TimeSpec

if TYPE_CHECKING:
    import sqlite3

    from .lake import Lake

COMMON = (
    "source", "exclude_sources", "tags", "any_tags", "exclude_tags", "noise", "add_tags", "add_meta", "system",
    "instructions", "budget", "lease",
)
OPTS: MappingProxyType[str, tuple[str, ...]] = MappingProxyType({
    "container": COMMON + ("inputs", "close_gap", "cluster_gap", "lookback", "max_clusters", "min_rows", "max_rows",
                           "advance_watermark", "session_prefix", "session_gap", "backfill", "backfill_max"),
    "mood": COMMON + ("max_rows",),
    "crystal": COMMON + ("min_chars", "drift_cap", "max_edits"),
})
DEFAULTS: MappingProxyType[str, object] = MappingProxyType({
    "noise": True, "close_gap": "30m", "cluster_gap": "30m", "lookback": "7d", "max_clusters": None, "min_rows": 3,
    "advance_watermark": False, "min_chars": 800, "drift_cap": 0.5, "lease": "1h",
    "session_prefix": "session:", "session_gap": "3h", "backfill": True, "backfill_max": 4, "max_edits": 6,
})
BUDGET: MappingProxyType[str, int] = MappingProxyType({"container": 12000, "mood": 12000, "crystal": 24000})
MAX_ROWS: MappingProxyType[str, int] = MappingProxyType({"container": 30, "mood": 60})
FILTER_KEYS = ("source", "exclude_sources", "tags", "any_tags", "exclude_tags")
NOT_CLUSTERED = ("container", "mood", "crystal")
SEDIMENT_MAX_SOURCES = 20  # §6.5: cap on the rows the sediment pass reads
EPISODE_MAX = 60  # §6.1: a session part never exceeds the derived_from cap, so derived_from covers every row shown
# §6.1 supersession: flagged rows per part; per flagged row, earlier host rows retrieved (M3: top 2 finds 7/7) and
# earlier rows of the part sharing FTS tokens; candidate lines and characters per call, and each line's cap; the
# old_value / new_value length bound; pairs kept per flagged row.
CHANGES_MAX, CAND_RETRIEVED, CAND_EPISODE, CAND_LINES, CAND_CHARS, CAND_CAP, QUOTE_MAX, OLDS_PER_NEW = 5, 2, 2, 10, 3000, 240, 120, 2
NO_JSON = "no JSON object in the answer"
CUT = "max_clusters reached"  # §6.1: the warning a capped container run adds
REJECTED = "Your previous answer was rejected: "
SHAPE = " Reply with only one JSON object in exactly this shape:\n"
CLOCK = "Now (the lake's clock): {}Z"
USER_TAG = "user"  # §6.1 extractive fallback: the first row carrying it names the session
TRUNC = "  … (truncated)"
TOKEN_RE = re.compile(r"[a-z0-9']+")
STATE_RE = re.compile(r"[^a-z-]")
# §6.3 the crystal (cited edits to items): sections in render order, growth-log ops, items per crystal, item text bounds,
# `why` cap, edits kept in meta.edits, entries in the "What changed" block, the rendered cap (so the crystal fits the
# plugin's 4000-char SessionStart whole: system_prompt() gives an items crystal its room first, §5.7).
SECTIONS, EDIT_OPS = ("core", "tension", "open"), ("revise", "add", "retire", "resolve")
ITEMS_MIN, ITEMS_MAX, TEXT_MIN, TEXT_MAX, WHY_MAX, EDITS_KEPT, CHANGED_SHOWN, RENDER_CAP = 3, 24, 20, 400, 160, 12, 3, 3000
# §6.3.1 stances: new or revised per pass, the position's length bounds, stances listed in the edits prompt.
STANCE_NEW, POSITION_MIN, POSITION_MAX, STANCES_LISTED = 3, 10, 300, 12
SLUG_RE = re.compile(r"[^a-z0-9]+")


class Run:
    """State of one consolidate() or due() call: validated opts, one clock reading, scope, counters (§6)."""

    def __init__(self, lake: Lake, kind: str, opts: Mapping[str, object]) -> None:
        allowed = OPTS.get(kind)
        if allowed is None:
            raise ValueError(f"kind must be container, mood, or crystal, not {kind!r}")
        if unknown := sorted(set(opts) - set(allowed)):
            raise ValueError(f"unknown consolidate({kind!r}) option(s): {', '.join(unknown)}")
        self.lake, self.kind, self.opts = lake, kind, opts
        self.now = lake.now()
        self.now_s = _time.dt_to_ts(self.now)
        self.lease_until = _time.dt_to_ts(self.now + timedelta(seconds=_time.parse_duration(self.opt("lease"))))
        self.budget = int(self.opt("budget", BUDGET[kind]))
        self.add_tags = _store.normalise_tags(as_list(self.opt("add_tags")))
        add_meta = self.opt("add_meta") or {}
        if not isinstance(add_meta, Mapping):
            raise ValueError("add_meta must be a mapping")
        self.add_meta: dict[str, Any] = dict(add_meta)
        self.scope: dict[str, list[str]] = {k: sorted(set(as_list(opts[k]))) for k in FILTER_KEYS if opts.get(k)}
        self.filters_json = canon(self.scope or None)
        s, self.auto = self.scope, automation(lake, str(self.opt("session_prefix") or ""))
        self.starts = tuple(v for k, v in lake.automation if k == "prefix")
        self.filters = _recall.Filters(  # automation rows are left out, never a scope (§6)
            source=s.get("source"), exclude_sources=[*s.get("exclude_sources", ()), *self.auto[0]],
            tags=s.get("tags"), any_tags=s.get("any_tags"), exclude_tags=[*s.get("exclude_tags", ()), *self.auto[1]], no_ttl=True,
        )
        self.written: list[Delta] = []
        self.warnings: list[str] = []
        self.inputs: set[str] = set()  # every id a prompt of this run rendered (§6 "Row written": the input set)
        self.skipped = self.think_calls = 0
        self.window: tuple[str, str] | None = None

    def opt(self, key: str, default: object = None) -> Any:
        return self.opts.get(key, DEFAULTS.get(key, default))

    def rows(
        self, filters: _recall.Filters, extra: str, params: Sequence[object], order: str, *, noise: bool = True
    ) -> list[sqlite3.Row]:
        """DELTA_COLS rows passing `filters` plus this run's `extra` fragment, minus the §5.4 hard drops."""
        where, bound = _recall.filter_sql(self.lake, filters, self.now)
        sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE {where}{extra} ORDER BY {order}"
        return [r for r in self.lake.conn.execute(sql, [*bound, *params])
                if not (noise and self.noisy(r)) and not str(r["content"]).startswith(self.starts)]

    def noisy(self, row: sqlite3.Row) -> bool:
        """§5.4 hard rules on one candidate row, unless the run has noise=False."""
        return bool(self.opt("noise")) and _recall.is_noise(
            self.lake.noise, row["content"], row["source"], row["kind"], row["media_hash"]
        )

    def ask(self, user: str, system: str, *, json: bool) -> str | dict[str, Any] | None:
        self.think_calls += 1
        return parse_answer(self.lake.call_think(user, system=system, json=json), json=json)

    def prefix(self) -> str:
        """The top of every user message: the lake's clock line (§6), then the `instructions` opt."""
        ins = self.opt("instructions")
        return f"{CLOCK.format(self.now_s[:16])}\n\n" + (f"{ins}\n\n" if ins else "")

    def system(self, name: str, tail: str) -> str:
        """The kind's system prompt: the persona (or the `system` opt) plus the output-schema tail (§6)."""
        return f"{str(self.opt('system') or prompt(name)).rstrip()}\n\n{tail.rstrip()}"

    def refresh(self, conn: sqlite3.Connection, keys: Mapping[str, str] | None) -> None:
        """The lease refresh plus the given meta keys, inside the caller's transaction."""
        for key, value in {**(keys or {}), f"consolidate_lease:{self.kind}": self.lease_until}.items():
            _db.meta_set(conn, key, value)

    def commit(
        self, *, content: str, level: int, tags: Sequence[str], derived_from: Sequence[str], meta: dict[str, Any],
        keys: Mapping[str, str] | None = None, delta_id: str | None = None, source: str | None = None,
    ) -> Delta:
        """Write one consolidated row with its meta keys and the lease refresh in one transaction, then its vector.
        `source` other than `lake:{kind}` (a `lake:stance` row, §6.3.1) writes kind sediment and leaves last_consolidate."""
        lake, own = self.lake, source is None
        source, kind = source or f"lake:{self.kind}", self.kind if own else "sediment"
        if foreign := [i for i in derived_from if i not in self.inputs]:  # §6: closed-loop invariant, a library bug
            raise ConsolidateError(f"derived_from names ids outside the run's input set: {', '.join(foreign[:5])}")
        if missing := _db.missing_ids(lake.conn, list(derived_from)):
            raise ConsolidateError(f"derived_from names ids missing from the file: {', '.join(missing[:5])}")
        tag_list = _store.normalise_tags([*tags, *self.add_tags])
        meta = {**meta, **{k: v for k, v in self.add_meta.items() if k not in meta}}
        with lake.tx() as conn:
            did = delta_id or _db.new_id(conn)
            _store.insert_row(conn, delta_id=did, timestamp=self.now_s, content=content, source=source, kind=kind, level=level,
                              tags=tag_list, derived_from=list(derived_from), expires_at=None, media_hash=None, meta=meta)
            last = {f"last_consolidate:{self.kind}": self.now_s, f"last_consolidate_id:{self.kind}": did} if own else {}
            self.refresh(conn, {**(keys or {}), **last})
        delta = Delta(
            id=did, timestamp=self.now_s, content=content, source=source, kind=kind, level=level, tags=tag_list,
            derived_from=list(derived_from), expires_at=None, media_hash=None,
            meta=json.loads(_store.dump_meta(meta) or "null"), engagement=None,
        )
        self.written.append(delta)
        if lake.embed is not None and lake.embed_on_write:
            _store.embed_and_store(lake, delta)
        return delta

    def consume(self, keys: Mapping[str, str] | None) -> None:
        """A cluster processed without a row: the watermark move and the lease refresh in one transaction."""
        if keys is not None:
            with self.lake.tx() as conn:
                self.refresh(conn, keys)


def consolidate(
    lake: Lake, kind: str, window: Duration | tuple[TimeSpec, TimeSpec] | None = None, **opts: object
) -> Delta | None:
    """§4.7/§6: validate, take the lease, dispatch on kind, release the lease, set lake.last_run."""
    run = Run(lake, kind, opts)
    lake.require_writable()
    if lake.think is None:
        raise ConsolidateError("consolidate() needs a think callback")
    key = f"consolidate_lease:{kind}"
    with lake.tx() as conn:
        held = _db.meta_get(conn, key)
        if held is not None and held > run.now_s:
            raise LeaseHeld(f"consolidate({kind!r}) already running (lease until {held})")
        _db.meta_set(conn, key, run.lease_until)
    try:
        if kind == "container":
            return container(run, window)
        return mood(run, window) if kind == "mood" else crystal_run(run, window)
    finally:
        with lake.tx() as conn:
            _db.meta_set(conn, key, None)
        lake.last_run = ConsolidateRun(kind, run.written, run.skipped, run.warnings, run.think_calls, run.window)


def crystal(lake: Lake) -> Delta | None:
    """§4.7: the newest live crystal (shared with context() block 1 and stats())."""
    return _store.newest(lake, "crystal")


def due(lake: Lake, kind: str) -> bool:
    """§6.4: reads the file and the clock only; False while the kind's lease is live."""
    run = Run(lake, kind, {})
    held = _db.meta_get(lake.conn, f"consolidate_lease:{kind}")
    if held is not None and held > run.now_s:
        return False
    th: Mapping[str, Any] = lake.due_thresholds
    conn = lake.conn
    if kind == "container":
        min_rows = int(th["container_min_rows"])
        groups, other = segments(run, container_rows(run, None)[0], min_rows)
        if groups or clusters(other, _time.parse_duration("30m"), 30, min_rows):
            return True
        return bool(run.opt("backfill")) and next(backfill_groups(run, set(), min_rows), None) is not None
    if kind == "mood":
        if not mood_rows(run, "3h")[0]:
            return False
        prior = newest_scoped(run, "mood")
        if prior is None or _time.age_seconds(run.now, prior.timestamp) > _time.parse_duration(th["mood_max_age"]):
            return True
        weights: Mapping[str, float] = th["mood_source_weights"]
        where, bound = _recall.filter_sql(lake, replace(run.filters, exclude_kinds=("mood", "crystal")), run.now)
        sql = ("SELECT d.source, d.timestamp, EXISTS (SELECT 1 FROM delta_tags t WHERE t.delta_id = d.id AND t.tag = ?)"
               f" FROM deltas d WHERE {where} AND d.timestamp > ?")
        pressure = sum(
            (float(weights.get(src, 1.0)) + (0.5 if user else 0.0)) * 0.5 ** (_time.age_seconds(run.now, ts) / 14400)
            for src, ts, user in conn.execute(sql, [th["mood_user_tag"], *bound, prior.timestamp])
        )
        return bool(pressure >= float(th["mood_pressure"]))
    newest = _store.newest(lake, "crystal")
    if newest is not None and _db.crystal_rests_on_newer_refute(conn, newest.id, newest.timestamp, run.now_s):
        return True  # §6.4: a memory the crystal rests on was refuted after it was written — regenerate now
    if newest is not None and _db.rests_on_newer_link(conn, newest.id, newest.timestamp, _db.supersessions(conn, run.now_s)):
        return True  # §6.4: ... or superseded after it was written (a stance revision, a corrected fact)
    if newest is not None:  # §6.4: new consolidated material, or age + rows
        if _time.age_seconds(run.now, newest.timestamp) < _time.parse_duration(str(th["crystal_min_age"])):
            return False
        sql = f"SELECT count(*) FROM deltas d WHERE d.source = 'lake:container' AND {_store.LIVE} AND d.timestamp > ?"
        new = int(conn.execute(sql, (run.now_s, newest.timestamp)).fetchone()[0])
        if new >= int(th["crystal_containers"]):
            return True
    return due_crystal_age_count(run, newest, th)


def due_crystal_age_count(run: Run, newest: Delta | None, th: Mapping[str, Any]) -> bool:
    """§6.4 crystal age+count trigger: the bootstrap when no crystal exists yet, and the material gate's age clause.
    Rows count when a consolidation could read them (host rows, no TTL, no automation)."""
    conn, need = run.lake.conn, int(th["crystal_rows"])
    where, bound = _recall.filter_sql(run.lake, replace(run.filters, exclude_kinds=NOT_CLUSTERED), run.now)
    rows = int(conn.execute(f"SELECT count(*) FROM deltas d WHERE {where} AND d.timestamp > ?",
                            [*bound, "" if newest is None else newest.timestamp]).fetchone()[0])
    if newest is None:
        live = f"SELECT count(*) FROM deltas d WHERE {_store.LIVE} AND d.kind = 'container'"
        return rows >= need or int(conn.execute(live, (run.now_s,)).fetchone()[0]) > 0
    return _time.age_seconds(run.now, newest.timestamp) > _time.parse_duration(th["crystal_max_age"]) and rows >= need


def prompt(name: str) -> str:
    """Text of lake/prompts/<name>.txt via importlib.resources (system prompts and directives)."""
    return (resources.files("lake") / "prompts" / f"{name}.txt").read_text(encoding="utf-8")


def schema(name: str) -> str:
    """The kind's output-schema block, lake/prompts/<name>_schema.txt: the JSON shape its tail shows and its
    corrective retry restates (§6)."""
    return prompt(f"{name}_schema").rstrip("\n")


def tail(name: str) -> str:
    """The kind's output-schema tail, lake/prompts/<name>_tail.txt, with its `{schema}` line filled in."""
    text = prompt(f"{name}_tail")
    return text.replace("{schema}", schema(name)) if "{schema}" in text else text


def as_list(value: object) -> list[str]:
    """An opt given as one string or a sequence of strings, as a list (filters, add_tags, inputs)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        raise ValueError(f"expected a string or a sequence of strings, not {value!r}")
    return [str(v) for v in value]


def row_line(lake: Lake, delta: Delta, cap: int) -> str:
    """§6 prompt row line `[{id}] {ts[:16]} {source} ·{role} {oneline(content, cap)}` (cap 160 for source: automation;
    role ` user:`/` assistant:` from the row's tag, as §5.6.2 renders it)."""
    if delta.source in lake.automation_sources:
        cap = min(cap, 160)
    role = next((lake.labels[f"{t}_role"] for t in ("user", "assistant") if t in delta.tags), "")
    return f"[{delta.id}] {delta.timestamp[:16]} {delta.source} ·{role} {_recall.oneline(delta.content, cap)}"


def container_line(delta: Delta) -> str:
    """§6 container line `[{id}] {ts[:16]} L{level} · {title} — {oneline(summary, 300)}`."""
    title = (delta.meta or {}).get("title") or delta.content.split("\n", 1)[0]
    summary = delta.content.split("\n\n", 1)[1] if "\n\n" in delta.content else delta.content
    return f"[{delta.id}] {delta.timestamp[:16]} L{delta.level} · {title} — {_recall.oneline(summary, 300)}"


def canon(value: object) -> str:
    """Canonical JSON for meta.filters equality (INTERNALS §4): sorted keys, no spaces, `null` for none."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def in_scope(run: Run, meta_text: str | None) -> bool:
    """Whether a consolidated row's meta.filters equals this run's (a missing key counts as null)."""
    obj = json.loads(meta_text) if meta_text else None
    return canon(obj.get("filters") if isinstance(obj, dict) else None) == run.filters_json


def newest_scoped(run: Run, kind: str) -> Delta | None:
    """The newest live row of `kind` in this run's scope, by (timestamp DESC, seq DESC); SQL, not a recall."""
    sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE d.kind = ? AND {_store.LIVE} ORDER BY d.timestamp DESC, d.seq DESC"
    for row in run.lake.conn.execute(sql, (kind, run.now_s)):
        if in_scope(run, row["meta"]):
            return _db.rows_to_deltas(run.lake.conn, [row])[0]
    return None


def attempt(
    run: Run, user: str, system: str, check: Callable[[Any], str | None], shape: str
) -> tuple[Any, str | None]:
    """§6 units of work: one think call and, when `check` names a reason, one retry with the reason and the
    kind's schema block appended."""
    reason = None
    for _ in range(2):
        message = user if reason is None else f"{user}\n\n{REJECTED}{reason}.{SHAPE}{shape}"
        answer = run.ask(message, system, json=True)
        reason = check(answer)
        if reason is None:
            return answer, None
    return answer, reason


def age_text(now: datetime, ts: str, *, anchor: bool) -> str:
    """§6.2 `age` with the anchor-weight suffix (mood prompt) or the §6.3 plain form (crystal prompt)."""
    s = _time.age_seconds(now, ts)
    if s < 3600:
        text, weight = f"{int(s // 60)} minutes ago", "heavy"
    elif anchor or s < 172800:
        text, weight = f"{s / 3600:.1f} hours ago", "moderate" if s < 14400 else "light, mostly faded"
    else:
        return f"{int(s // 86400)} days ago"
    return f"{text} — anchor weight: {weight}" if anchor else text


def container(run: Run, window: Duration | tuple[TimeSpec, TimeSpec] | None) -> Delta | None:
    """§6.1: `inputs=` mode, or the clustering run over the default or explicit window with the watermark."""
    lake, inputs = run.lake, run.opt("inputs")
    if inputs is not None:
        if window is not None or run.scope:
            raise ValueError("inputs= takes no window and no filters")
        return propose(run, load_inputs(run, inputs), None, None)
    advance = window is None or bool(run.opt("advance_watermark"))
    s_run = _db.max_seq(lake.conn)
    cands, pos, last_pos = container_rows(run, window)
    if advance and window is not None:  # §6.1: a backfill window advances the watermark forward, never backward.
        raw = _db.meta_get(lake.conn, "container_watermark")
        if raw is not None and (wm_pos := json.loads(raw).get("pos")) is not None:
            pos = later(pos, (str(wm_pos[0]), int(wm_pos[1])))
    win_meta: object = list(run.window or ()) if window is None or isinstance(window, tuple) else _time.render_duration(_time.parse_duration(window))
    gap, max_rows = _time.parse_duration(run.opt("cluster_gap")), max(1, int(run.opt("max_rows", MAX_ROWS["container"])))
    min_rows = int(run.opt("min_rows"))
    sessions, other = segments(run, cands, min_rows)
    units: list[tuple[str, SessionGroup | None, list[Delta]]] = [(g.parts[0][0].timestamp, g, []) for g in sessions]
    units += [(c[0].timestamp, None, c) for c in clusters(other, gap, max_rows, min_rows)]
    units.sort(key=lambda u: u[0])  # stable: with no session groups this is the clusters' own t_start order
    max_clusters, last = int(run.opt("max_clusters") or 0) or len(units), None  # None or 0: no cap (§6.1)
    for _, group, rows in units[:max_clusters]:
        if group is not None:  # session units never move the watermark; coverage makes them idempotent
            last = name_session(run, group, win_meta) or last
            continue
        pos = later(pos, position(lake, rows[-1]))
        last = propose(run, rows, win_meta, watermark(s_run, pos) if advance else None) or last
    if len(units) > max_clusters:  # digest() reads this line: the capped window is not done
        run.warnings.append(f"{CUT}: {len(units) - max_clusters} of {len(units)} units wait for the next run")
    elif advance:
        run.consume(watermark(s_run, later(pos, last_pos)))
    if window is None and run.opt("backfill"):
        done = {g.tag for g in sessions}
        for group in itertools.islice(backfill_groups(run, done, min_rows), max(0, int(run.opt("backfill_max")))):
            last = name_session(run, group, None) or last
    return last


def container_rows(
    run: Run, window: Duration | tuple[TimeSpec, TimeSpec] | None
) -> tuple[list[Delta], tuple[str, int] | None, tuple[str, int] | None]:
    """§6.1 candidates in (timestamp, seq) order after the hard noise rules, the watermark position read
    (default runs only), and the position of the window's last row (noise included); sets run.window (§12.4)."""
    lake, now = run.lake, run.now
    b = now - timedelta(seconds=_time.parse_duration(run.opt("close_gap")))
    filters = replace(run.filters, exclude_kinds=NOT_CLUSTERED, until=b)
    extra, pos = "", None
    params: list[object] = []
    if window is None:
        a = now - timedelta(seconds=_time.parse_duration(run.opt("lookback")))
        filters = replace(filters, since=a)
        raw = _db.meta_get(lake.conn, "container_watermark")
        if raw is not None:
            wm = json.loads(raw)
            if wm.get("pos") is None:
                extra, params = " AND d.seq > ?", [int(wm["seq"])]
            else:
                pos = (str(wm["pos"][0]), int(wm["pos"][1]))
                extra = " AND (d.seq > ? OR d.timestamp > ? OR (d.timestamp = ? AND d.seq > ?))"
                params = [int(wm["seq"]), pos[0], pos[0], pos[1]]
                a = max(a, _time.ts_to_dt(pos[0]))
    elif isinstance(window, tuple):
        a, b = (_time.parse_timespec(t, now) for t in window)
        filters = replace(filters, since=a, until=b)
    else:
        a = now - timedelta(seconds=_time.parse_duration(window))
        extra, params = " AND d.timestamp > ?", [_time.dt_to_ts(a)]
    if window is not None:  # §6.1: an explicit window never re-offers a row a live library container covers
        extra += f" AND NOT EXISTS (SELECT 1 FROM derived_from e JOIN deltas c ON c.id = e.delta_id WHERE e.parent_id = d.id AND {_db.LIVE_CONTAINER})"
        params = [*params, run.now_s]
    run.window = (_time.dt_to_ts(a), _time.dt_to_ts(b))
    rows = run.rows(filters, extra, params, "d.timestamp, d.seq", noise=False)
    last = (str(rows[-1]["timestamp"]), int(rows[-1]["seq"])) if rows else None
    return _db.rows_to_deltas(lake.conn, [r for r in rows if not run.noisy(r)]), pos, last


def position(lake: Lake, delta: Delta) -> tuple[str, int]:
    """A row's (timestamp, seq) watermark position (§6.1); `seq` is not on Delta."""
    return delta.timestamp, int(lake.conn.execute("SELECT seq FROM deltas WHERE id = ?", (delta.id,)).fetchone()[0])


def later(a: tuple[str, int] | None, b: tuple[str, int] | None) -> tuple[str, int] | None:
    """The watermark position only moves forward (§6.1)."""
    return max((p for p in (a, b) if p is not None), default=None)


def watermark(seq: int, pos: tuple[str, int] | None) -> dict[str, str]:
    """The §3.6 container_watermark meta value for the per-cluster transaction."""
    return {"container_watermark": json.dumps({"seq": seq, "pos": None if pos is None else list(pos)})}


def load_inputs(run: Run, inputs: object) -> list[Delta]:
    """§6.1 `inputs=`: every id resolves, at most 60, at least `floor`, in (timestamp, seq) order."""
    ids = _store.normalise_tags(as_list(inputs))
    if len(ids) > 60:
        raise ValueError(f"inputs= takes at most 60 ids, got {len(ids)}")
    rows = [_store.resolve(run.lake, i) for i in ids]
    seqs = dict(run.lake.conn.execute(f"SELECT id, seq FROM deltas WHERE id IN ({_db.qmarks(len(rows))})", [d.id for d in rows]))
    rows.sort(key=lambda d: (d.timestamp, seqs[d.id]))
    level = min(3, 1 + max((d.level for d in rows), default=0))
    if len(rows) < (floor := 2 if level == 1 else 3):
        raise ValueError(f"inputs= needs at least {floor} ids for a level {level} container, got {len(rows)}")
    return rows


def secs(delta: Delta) -> float:
    return _time.ts_to_dt(delta.timestamp).timestamp()


def related(a: Delta, b: Delta) -> bool:
    return a.source == b.source or not set(a.tags).isdisjoint(b.tags)


def clusters(rows: Sequence[Delta], gap_s: float, max_rows: int, min_rows: int) -> list[list[Delta]]:
    """§6.1 clustering: open clusters by adjacency and relatedness, split long ones at the largest gap,
    drop those under min_rows, order by t_start."""
    open_: list[list[Delta]] = []
    done: list[list[Delta]] = []
    for d in rows:
        t = secs(d)
        done.extend(c for c in open_ if t - secs(c[-1]) > gap_s)
        open_ = [c for c in open_ if t - secs(c[-1]) <= gap_s]
        for c in reversed(open_):
            if any(related(d, x) for x in c[-5:]):
                c.append(d)
                break
        else:
            open_.append([d])
    out = [p for c in done + open_ for p in split(c, max_rows) if len(p) >= min_rows]
    out.sort(key=lambda c: c[0].timestamp)
    return out


def split(cluster: list[Delta], max_rows: int) -> list[list[Delta]]:
    """Split at the largest internal time gap, recursively, until every piece has at most max_rows rows."""
    if len(cluster) <= max_rows:
        return [cluster]
    i = max(range(1, len(cluster)), key=lambda i: secs(cluster[i]) - secs(cluster[i - 1]))
    return split(cluster[:i], max_rows) + split(cluster[i:], max_rows)


@dataclass
class SessionGroup:
    """§6.1 one session's uncovered, closed rows cut into episodes (parts), each of min_rows to EPISODE_MAX rows."""

    tag: str
    parts: list[list[Delta]]
    backfill: bool = False


def session_tag(run: Run, delta: Delta) -> str | None:
    """The row's first tag (by pos) starting with the `session_prefix` opt, or None (and None when it is empty)."""
    prefix = str(run.opt("session_prefix") or "")
    return next((t for t in delta.tags if t.startswith(prefix)), None) if prefix else None


def segments(run: Run, cands: Sequence[Delta], min_rows: int) -> tuple[list[SessionGroup], list[Delta]]:
    """§6.1 session groups: every session a candidate names becomes one group of the session's uncovered rows
    (not bounded by the window), unless the session is still being written; rows without a session tag are
    returned for clustering. A session with no episode of min_rows rows yields no group."""
    groups: list[SessionGroup] = []
    other: list[Delta] = []
    seen: set[str] = set()
    for d in cands:
        tag = session_tag(run, d)
        if tag is None:
            other.append(d)
        elif tag not in seen:
            seen.add(tag)
            if (group := session_group(run, tag, min_rows)) is not None:
                groups.append(group)
    return groups, other


def close_ts(run: Run) -> str:
    return _time.dt_to_ts(run.now - timedelta(seconds=_time.parse_duration(run.opt("close_gap"))))


def session_group(run: Run, tag: str, min_rows: int, *, backfill: bool = False) -> SessionGroup | None:
    """One session's group: None while it is open (deferred whole) or when no episode reaches min_rows. Rows pass the
    candidate rules (kind, TTL, filters, automation, noise, close). The session is open while a row that passes
    the same rules and names it as its session lies in (now − close_gap, now]: a noisy, out-of-scope or future-dated
    row never holds it, so the row that deferred it is a candidate the next run's window sees again."""
    lake, b = run.lake, close_ts(run)
    tagged = " AND d.timestamp > ? AND EXISTS (SELECT 1 FROM delta_tags s WHERE s.delta_id = d.id AND s.tag = ?)"
    fresh = run.rows(replace(run.filters, exclude_kinds=NOT_CLUSTERED, until=run.now), tagged, [b, tag], "d.seq")
    if any(session_tag(run, d) == tag for d in _db.rows_to_deltas(lake.conn, fresh)):
        return None
    where, bound = _recall.filter_sql(lake, replace(run.filters, exclude_kinds=NOT_CLUSTERED, until=b), run.now)
    rows = [r for r in _db.uncovered_rows(lake.conn, tag, where, bound, run.now_s) if not run.noisy(r)]
    deltas = [d for d in _db.rows_to_deltas(lake.conn, rows) if session_tag(run, d) == tag]
    parts = episodes(deltas, _time.parse_duration(run.opt("session_gap")), min_rows)
    return SessionGroup(tag, parts, backfill) if parts else None


def episodes(rows: Sequence[Delta], gap_s: float, min_rows: int) -> list[list[Delta]]:
    """§6.1: split a session at internal gaps longer than session_gap; a piece under min_rows joins the neighbour
    across the smaller gap (so a group of at least min_rows rows is never dropped), then pieces over EPISODE_MAX rows
    are cut (split_episode). A group under min_rows rows yields nothing."""
    pieces: list[list[Delta]] = []
    for d in rows:
        if pieces and secs(d) - secs(pieces[-1][-1]) <= gap_s:
            pieces[-1].append(d)
        else:
            pieces.append([d])
    while len(pieces) > 1 and (k := next((i for i, p in enumerate(pieces) if len(p) < min_rows), -1)) >= 0:
        gaps = [secs(pieces[j + 1][0]) - secs(pieces[j][-1]) if 0 <= j < len(pieces) - 1 else math.inf for j in (k - 1, k)]
        j = k - 1 if gaps[0] <= gaps[1] else k
        pieces[j : j + 2] = [pieces[j] + pieces[j + 1]]
    return [p for piece in pieces for p in split_episode(piece, min_rows) if len(p) >= min_rows]


def split_episode(rows: list[Delta], min_rows: int) -> list[list[Delta]]:
    """Cut a piece over EPISODE_MAX rows into the fewest parts, each at most EPISODE_MAX rows: the first cut is the
    largest time gap among the cuts that keep that bound (and min_rows on both sides where possible), ties
    nearest an even share; recursively. Unlike split(), evenly spaced rows never leave a one-row part."""
    n = len(rows)
    if n <= EPISODE_MAX:
        return [rows]
    m = math.ceil(n / EPISODE_MAX)
    lo, hi = max(1, n - EPISODE_MAX * (m - 1)), min(EPISODE_MAX, n - 1)
    if max(lo, min_rows) <= min(hi, n - min_rows):
        lo, hi = max(lo, min_rows), min(hi, n - min_rows)
    i = max(range(lo, hi + 1), key=lambda i: (secs(rows[i]) - secs(rows[i - 1]), -abs(i - n / m)))
    return [rows[:i], *split_episode(rows[i:], min_rows)]


def backfill_groups(run: Run, done: set[str], min_rows: int) -> Iterator[SessionGroup]:
    """§6.1 backfill: closed sessions with zero coverage, in the run's scope, newest first, skipping `done`."""
    prefix = str(run.opt("session_prefix") or "")
    if not prefix:
        return
    b = close_ts(run)
    where, bound = _recall.filter_sql(run.lake, replace(run.filters, exclude_kinds=NOT_CLUSTERED, until=b), run.now)
    for tag in _db.uncovered_sessions(run.lake.conn, prefix, where, bound, run.now_s):
        if tag not in done and (group := session_group(run, tag, min_rows, backfill=True)) is not None:
            yield group


def name_session(run: Run, group: SessionGroup, win_meta: object) -> Delta | None:
    """§6.1 one session: a container per part, named by the model or, when both attempts fail or think raises a
    LakeError, written extractively. Every part gets a row, so the session is always covered."""
    last, prev = None, None
    for k, rows in enumerate(group.parts, 1):
        last = session_part(run, group, rows, (k, len(group.parts)), prev, win_meta)
        prev = str((last.meta or {}).get("title") or "")
    return last


def session_part(
    run: Run, group: SessionGroup, rows: list[Delta], part: tuple[int, int], prev: str | None, win_meta: object
) -> Delta:
    """One session part: the naming prompt, up to two think calls, validation, the row (model or extractive)."""
    lake, n = run.lake, len(rows)
    t_start, t_end = rows[0].timestamp[:16], rows[-1].timestamp[:16]
    cap = max(80, min(400, run.budget // n))
    k, of = part
    line = "" if of == 1 else f"Part {k} of {of} of this session" + (
        f'; the previous part is titled "{prev}".' if prev else ".") + "\n"
    template = prompt("container_session_user").rstrip()
    user = run.prefix() + template.format(
        n=n, t_start=t_start, t_end=t_end, tag=group.tag, part=line, rows="\n".join(row_line(lake, d, cap) for d in rows))
    ids = [d.id for d in rows]
    run.inputs.update(ids)
    system = run.system("container_session", tail("container_session"))
    head = f"session {group.tag} {t_start}–{t_end} ({n} rows: {', '.join(ids[:5])})"
    note: str | None = None
    try:
        answer, reason = attempt(run, user, system, session_reason, schema("container_session"))
        if reason is not None:
            note = f"{head} rejected twice: {reason}"
    except LakeError as exc:  # §6.1: a per-unit think failure falls back too; a missing think still raises
        note = f"{head} think failed: {exc}"
    pairs: list[dict[str, str]] = []
    if note is None:
        title, summary = str(answer["title"]).strip(), str(answer["summary"]).strip()
        pairs = supersede(run, rows, flagged(run, answer, rows, head), head)
    else:
        title, summary = extractive(rows)
        run.warnings.append(f"{note}; wrote an extractive container")
    content = f"{title}\n\n{summary}"
    content = content if len(content) <= 4000 else content[:4000] + "…"
    meta: dict[str, Any] = {
        "model": lake.model_name, "title": title, "span": [rows[0].timestamp, rows[-1].timestamp],
        "window": [rows[0].timestamp, rows[-1].timestamp] if group.backfill else win_meta,
        "filters": run.scope or None, "session": group.tag, "part": [k, of],
    }
    if note is not None:
        meta["fallback"] = "extractive"
    if group.backfill:
        meta["backfill"] = True
    if pairs:
        meta["supersedes"] = pairs
    return run.commit(content=content, level=1, tags=session_tags(rows), derived_from=ids, meta=meta)


def flagged(run: Run, answer: dict[str, Any], rows: Sequence[Delta], head: str) -> list[Delta]:
    """§6.1 supersession step 1: `changes` under the citation policy against the part's ids, host rows only, at most
    CHANGES_MAX in row order. A malformed or mostly foreign list is ignored with a warning, never costing the container."""
    raw = answer.get("changes") or []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        run.warnings.append(f"{head} changes ignored: not a list of strings")
        return []
    kept, dropped = check_citations(raw, {d.id for d in rows})
    if dropped:
        verb = "ignored" if mostly_foreign(kept, dropped) else "kept the rest"
        run.warnings.append(f"{head} changes {verb}: {len(dropped)} of {len(kept) + len(dropped)} ids are not in the"
                            f" stretch: {', '.join(dropped[:5])}")
        kept = [] if mostly_foreign(kept, dropped) else kept
    return [d for d in rows if d.id in set(kept) and d.kind is None][:CHANGES_MAX]


def change_candidates(run: Run, rows: Sequence[Delta], flag: Delta) -> list[Delta]:
    """§6.1 supersession step 2 (code, no model): the CAND_RETRIEVED best earlier host rows by search() on the flagged
    row's content (hybrid when the lake has embed) within the run's scope (source, tags and exclusions, as the
    candidates; TTL rows allowed), then up to CAND_EPISODE earlier host rows of the same part (already in scope)
    sharing the most FTS tokens with it (ties: row order), deduped."""
    until = _time.ts_to_dt(flag.timestamp) - timedelta(milliseconds=1)
    filters = replace(run.filters, kind="plain", until=until, no_ttl=False)
    hits = _recall.search(run.lake, flag.content, filters, limit=CAND_RETRIEVED, now=run.now, warnings=run.warnings)
    out = {h.delta.id: h.delta for h in hits}
    toks = set(_recall.fts_tokens(flag.content))
    overlap = {d.id: len(toks & set(_recall.fts_tokens(d.content))) for d in rows
               if d.kind is None and d.timestamp < flag.timestamp and d.id not in out}
    shared = sorted((d for d in rows if overlap.get(d.id)), key=lambda d: overlap[d.id], reverse=True)
    return [*out.values(), *shared[:CAND_EPISODE]]


def supersede(run: Run, rows: Sequence[Delta], flags: Sequence[Delta], head: str) -> list[dict[str, str]]:
    """§6.1 supersession steps 2–4: candidates in code (at most CAND_LINES lines and CAND_CHARS characters), one
    supersede call with one retry restating the schema when any flagged row has candidates, then per-pair validation
    in code (invalid pairs are dropped, not retried). A failed call writes no pairs and a warning; the container stays."""
    blocks: list[str] = []
    shown: dict[str, tuple[Delta, dict[str, Delta]]] = {}
    n_lines = n_chars = 0
    for flag in flags:
        cands: dict[str, Delta] = {}
        lines = [f"FLAGGED {row_line(run.lake, flag, 400)}"]
        for d in change_candidates(run, rows, flag):
            line = f"  earlier: {row_line(run.lake, d, CAND_CAP)}"
            if n_lines >= CAND_LINES or n_chars + len(line) > CAND_CHARS:
                break
            cands[d.id], n_lines, n_chars = d, n_lines + 1, n_chars + len(line)
            lines.append(line)
        if cands:
            shown[flag.id] = (flag, cands)
            blocks.append("\n".join(lines))
    if not shown:
        return []
    user = run.prefix() + prompt("supersede_user").rstrip().replace("{blocks}", "\n\n".join(blocks))
    system = f"{prompt('supersede').rstrip()}\n\n{tail('supersede').rstrip()}"  # the `system` opt names containers
    try:
        answer, reason = attempt(run, user, system, supersede_reason, schema("supersede"))
    except LakeError as exc:
        reason = f"think failed: {exc}"
    if reason is not None:
        run.warnings.append(f"{head} supersede call: {reason}; no links written")
        return []
    pairs: list[dict[str, str]] = []
    drops = [why for p in answer["pairs"] if (why := pair_reason(p, shown, pairs)) is not None]
    if drops:
        run.warnings.append(f"{head} dropped {len(drops)} of {len(answer['pairs'])} supersede pairs: {'; '.join(drops[:5])}")
    return pairs[:_db.SUPERSEDES_MAX]


def supersede_reason(answer: Any) -> str | None:
    """§6.1 supersede validation (the retry trigger): an object whose `pairs` is a list of objects."""
    if not isinstance(answer, dict):
        return NO_JSON
    ok = isinstance(answer.get("pairs"), list) and all(isinstance(p, dict) for p in answer["pairs"])
    return None if ok else "pairs must be a list of objects"


def squash(text: str) -> str:
    """Casefolded, whitespace-collapsed text for the quote check."""
    return " ".join(text.split()).casefold()


def pair_reason(p: dict[str, Any], shown: Mapping[str, tuple[Delta, Mapping[str, Delta]]], kept: list[dict[str, str]]) -> str | None:
    """§6.1 supersession step 4 for one pair; None keeps it (appended to `kept`). `new` is a flagged row, `old` one
    of the candidates shown under it, both host rows, old older than new; each value 1–QUOTE_MAX characters, found
    verbatim (casefolded, whitespace collapsed) in its own row, and the two differ; at most OLDS_PER_NEW per new."""
    fields = [p.get(k) for k in ("new", "old", "old_value", "new_value")]
    if not all(isinstance(x, str) for x in fields):
        return "a field is not a string"
    new, old, ov, nv = (str(x).strip() for x in fields)
    flag, cands = shown.get(new, (None, {}))
    prior = cands.get(old)
    if flag is None or prior is None:
        return f"{old} -> {new} is not a flagged row and a candidate shown under it"
    if flag.kind is not None or prior.kind is not None or not prior.timestamp < flag.timestamp:
        return f"{old} is not a host row older than {new}"
    if not (0 < len(ov) <= QUOTE_MAX and 0 < len(nv) <= QUOTE_MAX) or squash(ov) == squash(nv):
        return f"the values for {old} -> {new} are empty, over {QUOTE_MAX} characters, or the same"
    if squash(ov) not in squash(prior.content) or squash(nv) not in squash(flag.content):
        return f"a value for {old} -> {new} is not quoted from its row"
    same = [k for k in kept if k["new"] == new]
    if len(same) >= OLDS_PER_NEW or any(k["old"] == old for k in same):
        return f"{old} -> {new} is a duplicate or over {OLDS_PER_NEW} per row"
    kept.append({"new": new, "old": old, "old_value": ov, "new_value": nv})
    return None


def session_reason(answer: Any) -> str | None:
    """§6.1 session validation with the §12.7 reason strings; there is no skip."""
    if not isinstance(answer, dict):
        return NO_JSON
    title, summary = str(answer.get("title") or "").strip(), str(answer.get("summary") or "").strip()
    if not title or not summary:
        return "title is empty" if not title else "summary is empty"
    return "title exceeds 200 characters" if len(title) > 200 else None


def extractive(rows: Sequence[Delta]) -> tuple[str, str]:
    """§6.1 extractive fallback: a title and summary quoted from the rows, with no invented text."""
    counts: dict[str, int] = {}
    for d in rows:
        counts[d.source] = counts.get(d.source, 0) + 1
    source = max(counts, key=lambda s: counts[s])  # most rows; ties by first appearance
    first = next((d for d in rows if USER_TAG in d.tags), rows[0])
    title = f"{source} session {rows[0].timestamp[:10]}: {_recall.oneline(first.content, 80)}"
    summary = (f"{len(rows)} rows, {rows[0].timestamp[:16]} to {rows[-1].timestamp[:16]}."
               f" Last row: {_recall.oneline(rows[-1].content, 200)}")
    return title, summary


def propose(run: Run, rows: list[Delta], win_meta: object, keys: Mapping[str, str] | None) -> Delta | None:
    """One container unit of work: prompt, up to two think calls, validation, the row or a skip/warning."""
    lake, n = run.lake, len(rows)
    level = min(3, 1 + max(d.level for d in rows))
    floor, cap = (2 if level == 1 else 3), max(80, min(400, run.budget // n))
    t_start, t_end = rows[0].timestamp[:16], rows[-1].timestamp[:16]
    lines = "\n".join(container_line(d) if d.level >= 1 else row_line(lake, d, cap) for d in rows)
    template = prompt("container_user").rstrip()
    user = run.prefix() + template.format(n=n, t_start=t_start, t_end=t_end, level=level, rows=lines, floor=floor)
    ids = [d.id for d in rows]
    run.inputs.update(ids)
    system = run.system("container", tail("container"))
    answer, reason = attempt(run, user, system, lambda a: container_reason(a, ids, floor), schema("container"))
    if reason is not None:
        run.warnings.append(f"cluster {t_start}–{t_end} ({n} rows: {', '.join(ids[:5])}) rejected twice: {reason}")
    elif answer["kind"] == "skip":
        run.skipped += 1
    else:
        given = from_ids(answer)
        kept, dropped = (ids, []) if given is None else check_citations(given, ids)
        chosen = [d for d in rows if d.id in set(kept)]
        title, summary = str(answer["title"]).strip(), str(answer["summary"]).strip()
        content = f"{title}\n\n{summary}"
        meta: dict[str, Any] = {
            "model": lake.model_name, "title": title, "rationale": answer.get("rationale"),
            "span": [chosen[0].timestamp, chosen[-1].timestamp], "window": win_meta, "filters": run.scope or None,
        }
        if dropped:  # §6.1 rule 4: at most half foreign, the floor held on the rest; the drop is recorded
            meta["dropped_ids"] = dropped[:20]
            run.warnings.append(f"cluster {t_start}–{t_end} ({n} rows: {', '.join(ids[:5])}) dropped {len(dropped)}"
                                f" of {len(dropped) + len(kept)} ids not in the stretch: {', '.join(dropped[:5])}")
        if len(chosen) > 60:
            meta["input_count"] = len(chosen)
        content = content if len(content) <= 4000 else content[:4000] + "…"
        derived = [d.id for d in chosen[:60]]
        return run.commit(content=content, level=level, tags=shared_tags(chosen), derived_from=derived, meta=meta, keys=keys)
    run.consume(keys)
    return None


def from_ids(answer: dict[str, Any]) -> list[str] | None:
    """`from_ids` stripped, empties and duplicates dropped; None when absent; ValueError when not a list of strings."""
    raw = answer.get("from_ids")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValueError("from_ids must be a list of strings")
    return _store.normalise_tags(raw)


def container_reason(answer: Any, ids: Sequence[str], floor: int) -> str | None:
    """§6.1 validation with the §12.7 reason strings; None when the answer is acceptable."""
    if not isinstance(answer, dict):
        return NO_JSON
    if answer.get("kind") not in ("propose", "skip"):
        return 'kind must be "propose" or "skip"'
    if answer["kind"] == "skip":
        return None
    title, summary = str(answer.get("title") or "").strip(), str(answer.get("summary") or "").strip()
    if not title or not summary:
        return "title is empty" if not title else "summary is empty"
    if len(title) > 200:
        return "title exceeds 200 characters"
    try:
        chosen = from_ids(answer)
    except ValueError as exc:
        return str(exc)
    if chosen is None:
        return None
    kept, dropped = check_citations(chosen, ids)
    if mostly_foreign(kept, dropped):
        return f"{len(dropped)} of {len(chosen)} ids are not in the stretch: {', '.join(dropped[:5])}"
    return None if len(kept) >= floor else f"only {len(kept)} ids; at least {floor} must belong together"


def shared_tags(rows: Sequence[Delta]) -> list[str]:
    """Tags carried by at least half of the rows (rounded up), most frequent first, ties by first appearance, ≤ 16."""
    counts: dict[str, int] = {}
    for d in rows:
        for t in d.tags:
            counts[t] = counts.get(t, 0) + 1
    need = (len(rows) + 1) // 2
    return sorted((t for t, c in counts.items() if c >= need), key=lambda t: -counts[t])[:16]


def session_tags(rows: Sequence[Delta]) -> list[str]:
    """§6.1 session container tags: shared_tags() of the part, then shared_tags() of its user rows (USER_TAG included
    when it has any), ≤ 16. A session is mostly assistant rows, so the majority rule alone drops the user's role tag."""
    return list(dict.fromkeys([*shared_tags(rows), *shared_tags([d for d in rows if USER_TAG in d.tags])]))[:16]


def mood_rows(run: Run, window: Duration | tuple[TimeSpec, TimeSpec]) -> tuple[list[Delta], str, object]:
    """§6.2 candidates in chronological order (newest max_rows after the noise rules), the span text, meta.window."""
    now = run.now
    filters = replace(run.filters, exclude_kinds=("mood", "crystal"))
    extra, params = "", []
    if isinstance(window, tuple):
        a, b = (_time.parse_timespec(t, now) for t in window)
        filters = replace(filters, since=a, until=b)
        win_meta: object = [_time.dt_to_ts(a), _time.dt_to_ts(b)]
        span = f"{_time.dt_to_ts(a)[:16]} to {_time.dt_to_ts(b)[:16]}"
    else:
        seconds = _time.parse_duration(window)
        a, b = now - timedelta(seconds=seconds), now
        filters, extra, params = replace(filters, until=b), " AND d.timestamp > ?", [_time.dt_to_ts(a)]
        win_meta = _time.render_duration(seconds)
        span = f"last {win_meta}"
    run.window = (_time.dt_to_ts(a), _time.dt_to_ts(b))
    rows = run.rows(filters, extra, params, "d.timestamp DESC, d.seq DESC")
    return _db.rows_to_deltas(run.lake.conn, rows[: max(1, int(run.opt("max_rows", MAX_ROWS["mood"])))])[::-1], span, win_meta


def mood(run: Run, window: Duration | tuple[TimeSpec, TimeSpec] | None) -> Delta | None:
    """§6.2: candidates, the prior mood of this scope, the prompt under budget, validation, the JSON row."""
    lake = run.lake
    cands, span, win_meta = mood_rows(run, window if window is not None else "3h")
    if not cands:
        return None
    prior = newest_scoped(run, "mood")
    if prior is None:
        block = "(no prior mood — this is your first carrier wave)"
    else:
        state = mood_json(prior).get("state") or "unset"
        block = f"Prior mood ({age_text(run.now, prior.timestamp, anchor=True)}) [previous state: {state}]:\n{prior.content}"
    head, tail_ = f"{run.prefix()}=== Recent rows ({span}) ===\n", f"\n\n=== Prior mood ===\n{block}"
    used = len(head) + len(tail_)
    kept: list[tuple[Delta, str]] = []
    for d in reversed(cands):
        line = row_line(lake, d, 400)
        if kept and used + len(line) + 1 > run.budget:
            break
        used += len(line) + 1
        kept.append((d, line))
    kept.reverse()
    user = head + "\n".join(line for _, line in kept) + tail_
    run.inputs.update(d.id for d, _ in kept)
    answer, reason = attempt(run, user, run.system("mood", tail("mood")), mood_reason, schema("mood"))
    if reason is not None:
        raise ConsolidateError(reason)
    obj = sanitise_mood(answer)
    content = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    meta = {"model": lake.model_name, "window": win_meta, "filters": run.scope or None}
    return run.commit(content=content, level=0, tags=[f"feeling:{obj['state']}"], derived_from=[d.id for d, _ in kept], meta=meta)


def mood_reason(answer: Any) -> str | None:
    if not isinstance(answer, dict):
        return NO_JSON
    return None if str(answer.get("headline") or "").strip() else "headline is missing or empty"


def sanitise_mood(answer: dict[str, Any]) -> dict[str, Any]:
    """§6.2 sanitising (never rejecting) into the six content keys, in order; `-` survives in state (§12.10)."""
    state = STATE_RE.sub("", str(answer.get("state") or "").lower())[:24] or "unset"
    raw_threads, raw_levels = answer.get("threads"), answer.get("levels")
    threads = [t for t in (str(x).strip() for x in raw_threads) if t][:4] if isinstance(raw_threads, list) else []
    levels: dict[str, float] = {}
    if isinstance(raw_levels, dict):
        for key, value in raw_levels.items():
            try:
                x = float(value)
            except (TypeError, ValueError):
                continue
            if isinstance(key, str) and key.strip() and not math.isnan(x):
                levels[key.strip().lower()] = min(1.0, max(0.0, x))
        top = set(sorted(levels, key=lambda k: -levels[k])[:12])
        levels = {k: v for k, v in levels.items() if k in top}
    text = {k: str(answer.get(k) or "").strip() for k in ("headline", "subtext", "carrier_wave")}
    return {"state": state, **text, "threads": threads, "levels": levels}


def mood_json(row: Delta) -> dict[str, Any]:
    """A mood row's JSON content as a dict ({} when it is not an object)."""
    try:
        obj = json.loads(row.content)
    except ValueError:
        return {}
    return obj if isinstance(obj, dict) else {}


def crystal_run(run: Run, window: object) -> Delta | None:
    """§6.3: the four sections under budget, up to two think calls, validation, drift, the row and the file."""
    lake = run.lake
    if window is not None:
        raise ValueError("consolidate('crystal') takes no window")
    prior, mood_row = newest_scoped(run, "crystal"), newest_scoped(run, "mood")
    conts, total = scoped_containers(run)
    extra, params = ("", []) if prior is None else (" AND d.timestamp > ?", [prior.timestamp])
    rows = run.rows(replace(run.filters, exclude_kinds=NOT_CLUSTERED), extra, params, "d.timestamp DESC, d.seq DESC")
    recent = _db.rows_to_deltas(lake.conn, rows[:120])[::-1]
    if prior is None and mood_row is None and not conts and not recent:
        return None
    p_items = prior_items(prior) if prior is not None else None
    p_age = "none" if prior is None else age_text(run.now, prior.timestamp, anchor=False)
    m_age = "none" if mood_row is None else age_text(run.now, mood_row.timestamp, anchor=False)
    p_body = "(none — this is the first crystal)" if prior is None else prior.content
    if p_items is not None:  # §6.3: the prior as its items, one line each, never as prose
        p_body = "\n".join(f"[{i['id']}] {i['section']} · {i['text']}" for i in p_items)
    live = _recall.stances(lake, run.now) if not run.scope else []  # §6.3.1: unscoped only
    p_body += f"\n\n=== Positions I hold ({len(live)} live stances) ===\n" + ("\n".join(
        f"[{x.slug}] {x.position} — {x.label}; evidence {x.support}; against {x.against}"
        for x in live[:STANCES_LISTED]) or ("(none)" if not run.scope else "(kept only by the unscoped crystal)"))
    user_text = prompt("crystal_edits_user").rstrip()
    user_text = user_text.replace("{max_edits}", str(int(run.opt("max_edits")))).replace("{render_cap}", str(RENDER_CAP))
    head = f"{run.prefix()}{user_text}\n\n=== Previous crystal ({p_age}) ===\n{p_body}\n\n"
    mid = f"\n\n=== Current mood ({m_age}) ===\n{'(none)' if mood_row is None else mood_block(mood_row)}\n\n"
    c_hdr = f"=== Containers — named stretches of memory ({{}} of {total}, highest level first) ===\n"
    r_hdr = "=== Since the previous crystal ({} rows) ===\n"
    used = len(head) + len(mid) + len(c_hdr) + len(r_hdr) + 2 * len(TRUNC) + 8
    c_kept, c_lines, used = take(run.budget, used, conts, container_line)
    r_kept, r_lines, used = take(run.budget, used, recent, lambda d: row_line(lake, d, 400))
    if prior is None and mood_row is None and not c_kept and not r_kept:  # §1: a lake: row needs a derived_from edge;
        if recent:  # when the budget is too small for any section line, ground on the newest available row.
            r_kept, r_lines = [recent[-1]], [row_line(lake, recent[-1], 400)]
        elif conts:
            c_kept, c_lines = [conts[0]], [container_line(conts[0])]
    user = head + c_hdr.format(len(c_kept)) + ("\n".join(c_lines) or "(none)")
    user += mid + r_hdr.format(len(r_kept)) + ("\n".join(r_lines) or "(none)")
    parts: list[Delta] = [*([prior] if prior else []), *c_kept, *([mood_row] if mood_row else []), *r_kept]
    run.inputs.update(d.id for d in parts)
    text, dist, item_meta, cited, writes = items_answer(  # the operator guard's evidence: the prompt, the prior aside
        run, user, prior, p_items, [d.id for d in (*c_kept, *r_kept)], {x.slug: x for x in live}, user.replace(p_body, "", 1))
    s_ids = [run.commit(content=c, level=0, tags=[f"stance:{m['stance']['slug']}"], derived_from=cite,
                        meta={"model": lake.model_name, **m}, source="lake:stance").id for c, cite, m in writes]
    run.inputs.update(s_ids)
    derived = [*([prior.id] if prior else []), *cited, *s_ids]
    value, method = crystal_drift_value(lake, text, prior, dist)
    did, prior_id = _db.new_id(lake.conn), None if prior is None else prior.id
    record = {"value": value, "method": method, "crystal_id": did, "prior_id": prior_id, "at": run.now_s}
    meta = {"model": lake.model_name, "drift": {"value": value, "method": method, "prior_id": prior_id}, "window": None,
            "filters": run.scope or None, **item_meta}
    keys = {"crystal_drift": json.dumps(record)}
    row = run.commit(content=text, level=0, tags=[], derived_from=derived, meta=meta, keys=keys, delta_id=did)
    if lake.write_crystal_file and not run.scope:
        tmp = lake.crystal_path.with_name(lake.crystal_path.name + ".tmp")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, lake.crystal_path)
    return row


def items_answer(
    run: Run, user: str, prior: Delta | None, p_items: list[dict[str, Any]] | None, shown: Sequence[str],
    live: Mapping[str, _recall.Stance], material: str,
) -> tuple[str, float | None, dict[str, Any], list[str], list[tuple[str, list[str], dict[str, Any]]]]:
    """§6.3: one JSON think call and one schema-restating retry. Returns the rendered text, the A2 distance, the
    item meta keys, the cited ids (in `shown`, item order, then retire cites) and the §6.3.1 stance rows to write
    (content, derived_from, meta). A new or revised text naming the operator (the think's `operator` names) when
    nothing in `material` does is refused like any invalid answer: the account context is not memory."""
    lake, allowed = run.lake, set(shown)
    pm = (prior.meta if prior is not None else None) or {}
    seq0 = max([int(x) if (x := str(pm.get("item_seq"))).isdigit() else 0,  # meta is never trusted (§3.2)
                *(int(i["id"][1:]) for i in p_items or () if re.fullmatch(r"c\d+", i["id"]))])
    bounded = bool(pm.get("items"))  # first crystal and a prose prior's p items: unbounded
    cap, gated, min_chars = float(run.opt("drift_cap")), lake.embed is not None and prior is not None, int(run.opt("min_chars"))
    out: dict[str, Any] = {}

    def check(answer: Any) -> str | None:
        if not isinstance(answer, dict):
            return NO_JSON
        notes: list[str] = []
        try:
            items, edits, keeps, seq = apply_edits(p_items or [], answer.get("items"), allowed, seq0, run.now_s, notes,
                                                   int(run.opt("max_edits")) if bounded else None)
        except ValueError as exc:
            return str(exc)
        writes, s_edits = stance_ops(run, answer.get("stances"), live, allowed, notes)
        edits = [*edits, *s_edits]
        if names := leaks(run.lake, " ".join([*(str(e.get("new") or "") for e in edits), *(c for c, _, _ in writes)]),
                          material):
            return f"it names {', '.join(names)}, which nothing in the material does: write only what the rows hold"
        changed = [*edits, *growth_edits(pm)][:CHANGED_SHOWN] if prior is not None else []
        text, cut = render_crystal(items, changed, lake.labels)
        if len(text) < min_chars:
            return f"too short ({len(text)} rendered chars, need {min_chars}): write more items, or fuller ones"
        dist = crystal_distance(lake, text, prior.content) if gated else None  # type: ignore[union-attr]
        if dist is not None and dist > cap:
            return f"moved {dist} from the prior crystal, over the {cap} per-rewrite cap: keep more items"
        meta: dict[str, Any] = {"items": items, "items_cut": cut, "item_seq": seq, "edits": edits[:EDITS_KEPT], "implicit_keep": keeps}
        cited = [c for i in items for c in i["cite"] if c in allowed] + [c for e in edits[:len(edits) - len(s_edits)]
                                                                        for c in e["cite"]]
        out.update(text=text, dist=dist, meta=meta, cited=list(dict.fromkeys(cited)), notes=notes, writes=writes)
        return None

    _, reason = attempt(run, user, run.system("crystal", tail("crystal_edits")), check, schema("crystal_edits"))
    if reason is not None:
        raise ConsolidateError(reason)
    run.warnings.extend(out["notes"])
    return out["text"], out["dist"], out["meta"], out["cited"], out["writes"]


def stance_ops(
    run: Run, raw: object, live: Mapping[str, _recall.Stance], allowed: set[str], notes: list[str]
) -> tuple[list[tuple[str, list[str], dict[str, Any]]], list[dict[str, Any]]]:
    """§6.3.1: the answer's `stances` ops, validated (a bad op is a warning, never a retry): the rows to write as
    (content, cites, meta) and their growth-log entries. hold writes nothing; new and revise need a position and a
    cite with outside evidence (§6.5), at most STANCE_NEW per pass; drop needs a live slug and a cite."""
    if not isinstance(raw, list) or not raw or run.scope:
        notes.extend(["stances ignored: a scoped crystal keeps none"] if raw and run.scope else [])
        return [], []
    chains, links = _db.stance_chains(run.lake.conn, run.now_s), _db.supersessions(run.lake.conn, run.now_s)
    writes: list[tuple[str, list[str], dict[str, Any]]] = []
    edits: list[dict[str, Any]] = []
    for op in raw:
        op = op if isinstance(op, dict) else {}
        kind, slug = str(op.get("op") or "").strip().lower(), SLUG_RE.sub("-", str(op.get("slug") or "").lower()).strip("-")[:40]
        cite, against = ([] if not isinstance(v := op.get(k), list) else check_citations([str(c) for c in v], allowed)[0]
                         for k in ("cite", "against"))
        pos = live[slug].position if kind == "drop" and slug in live else re.sub(
            r"^i hold that\s+", "", _recall.oneline(str(op.get("position") or ""), 400), flags=re.I).rstrip(".")
        fresh, grounded = kind in ("new", "revise"), bool(cite and _recall.evidence_sessions(run.lake, cite, run.now_s, links))
        bad = next((why for hit, why in (
            (kind not in ("hold", "new", "revise", "drop"), f"unknown op {kind!r}"), (not slug, "no slug"),
            (kind in ("hold", "drop") and slug not in live, "no such live stance"),
            (any(e["id"] == f"stance:{slug}" for e in edits), "a second op on one slug"),
            (fresh and not POSITION_MIN <= len(pos) <= POSITION_MAX, f"position is {len(pos)} chars, not {POSITION_MIN}-{POSITION_MAX}"),
            (fresh and sum(e["op"] != "retire" for e in edits) >= STANCE_NEW, f"more than {STANCE_NEW} new or revised stances"),
            (kind != "hold" and not cite, "no cited id is in the material"),
            (fresh and not grounded, "no outside evidence among its cites (lake rows and assistant replies do not count)"),
        ) if hit), None)
        if bad is not None or kind == "hold":
            notes.extend([f"stance {kind or 'op'} {slug or '?'} dropped: {bad}"] if bad else [])
            continue
        why, because = _recall.oneline(str(op.get("why") or ""), WHY_MAX), _recall.oneline(str(op.get("because") or ""), WHY_MAX)
        content = (f"I hold that {pos}" + (f", because {because.rstrip('.')}" if because else "") if fresh
                   else f"I no longer hold that {pos}" + (f": {why.rstrip('.')}" if why else "")) + "."
        stance = {"slug": slug, "topic": _recall.oneline(str(op.get("topic") or slug), 80), "position": pos,
                  "because": because if fresh and because else None, "against": against,
                  "revises": chains[slug][0][0] if slug in chains else None, "retired": not fresh}
        writes.append((content, cite, {"stance": stance, "grounding": "external" if grounded else "self-referential"}))
        edits.append({"op": "add" if fresh and slug not in live else "revise" if fresh else "retire", "id": f"stance:{slug}",
                      "old": live[slug].position if slug in live else None, "new": pos if fresh else None, "cite": cite,
                      "why": why})
    return writes, edits


def checked_item(
    raw: object, allowed: set[str], notes: list[str], section: str | None, *, text: bool = True
) -> tuple[str, str, list[str]] | None:
    """One item or edit op after §6.3 validation: (section, text, kept cites), or None with a note. `section`
    None reads it from `raw`; `text=False` (a retire) needs no text. Cites follow the §6.1 policy per item."""
    if not isinstance(raw, dict):
        notes.append("item dropped: not an object")
        return None
    sec = section or str(raw.get("section") or "").strip().lower()
    body = _recall.WS_RE.sub(" ", str(raw.get("text") or "")).strip()
    cites = raw.get("cite")
    kept, dropped = check_citations([str(c) for c in cites] if isinstance(cites, list) else [], allowed)
    label = f"{raw.get('op') or 'item'} {raw.get('id') or ''}".rstrip() + f" ({_recall.oneline(body, 40) or 'no text'})"
    if sec not in SECTIONS:
        why = f"section {sec!r} is not core, tension, or open"
    elif text and not TEXT_MIN <= len(body) <= TEXT_MAX:
        why = f"text is {len(body)} chars, not {TEXT_MIN}-{TEXT_MAX}"
    elif not kept:
        why = "no cited id is in the material" + (f" ({', '.join(dropped[:3])})" if dropped else "")
    else:
        return sec, body, kept
    notes.append(f"{label} dropped: {why}")
    return None


def apply_edits(
    prior: list[dict[str, Any]], raw: object, allowed: set[str], seq: int, now: str, notes: list[str],
    max_edits: int | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """§6.3 edits mode: keep / revise / add / retire / resolve on the prior items. An unmentioned item is kept
    (continuity by construction); only a cited retire of a core item drops one; a cited resolve turns an open or
    tension item into core. A retire or resolve naming the wrong section is refused and keeps the item. Returns
    (items, edits, implicit keeps, last seq)."""
    if not isinstance(raw, list) or (not raw and not prior):
        raise ValueError("items is missing or empty")
    by_id, order = {i["id"]: i for i in prior}, [i["id"] for i in prior]
    touched: set[str] = set()
    edits: list[dict[str, Any]] = []
    tried = 0
    for op in raw:
        kind = str(op.get("op") or "").strip().lower() if isinstance(op, dict) else ""
        iid = str(op.get("id") or "").strip() if isinstance(op, dict) else ""
        if kind == "keep":
            touched.add(iid) if iid in by_id else notes.append(f"keep {iid or '?'} ignored: no such item")
            continue
        old = by_id.get(iid) if kind != "add" and iid not in touched else None
        if old and kind in ("retire", "resolve") and (kind == "retire") != (old["section"] == "core"):  # a note only
            notes.append(f"{kind} {iid} refused: {iid} is a{'n' * (old['section'] == 'open')} {old['section']} item; "
                         + ("resolve it: say what settled it" if kind == "retire" else "revise or retire it"))
            continue
        tried += 1
        if kind not in EDIT_OPS or (kind != "add" and old is None):
            notes.append(f"{kind or 'op'} {iid or '?'} dropped: unknown op, unknown item, or an item already edited")
            continue
        got = checked_item(op, allowed, notes, "core" if kind == "resolve" else old["section"] if old else None,
                           text=kind != "retire")
        if got is None:
            continue
        if kind == "add":
            seq += 1
            iid = f"c{seq}"
            order.append(iid)
        if kind == "retire":
            del by_id[iid]
        else:
            by_id[iid] = {"id": iid, "section": got[0], "text": got[1], "cite": got[2], "since": now,
                          **({"resolved_from": old["section"]} if kind == "resolve" and old else {})}
        touched.add(iid)
        edits.append({"op": kind, "id": iid, "old": old["text"] if old else None,
                      "new": None if kind == "retire" else got[1], "cite": got[2],
                      "why": _recall.oneline(str(op.get("why") or ""), WHY_MAX)})
    if 2 * len(edits) < tried:
        raise ValueError(f"only {len(edits)} of {tried} edits are valid: {'; '.join(notes[:3])}")
    if max_edits is not None and len(edits) > max_edits:
        raise ValueError(f"{len(edits)} edits, at most {max_edits}: keep the rest")
    items = [by_id[i] for i in order if i in by_id]
    if len(items) > ITEMS_MAX:
        raise ValueError(f"{len(items)} items, at most {ITEMS_MAX}: retire or merge some")
    return enough(items), edits, sum(i["id"] not in touched for i in items), seq


def enough(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The §6.3 floor on a crystal's items: at least ITEMS_MIN, two of them core."""
    if len(items) < ITEMS_MIN or sum(i["section"] == "core" for i in items) < 2:
        raise ValueError(f"{len(items)} valid items; a crystal needs at least {ITEMS_MIN}, 2 of them core")
    return items


def prior_items(prior: Delta) -> list[dict[str, Any]]:
    """The prior crystal's meta.items, or, for a prose crystal, its h2 facets as core items p1…pn: uncited and whole
    (exempt from TEXT_MAX, since an unmentioned item is kept and rendered as it stands)."""
    items = (prior.meta or {}).get("items") if isinstance(prior.meta, dict) else None
    if isinstance(items, list) and items:  # a host-written crystal row may carry anything: keep well-formed items
        return [{**i, "id": str(i["id"]), "cite": [str(c) for c in i["cite"]]} for i in items if isinstance(i, dict)
                and i.get("section") in SECTIONS and isinstance(i.get("text"), str) and isinstance(i.get("cite"), list)
                and i.get("id")]
    facets = [f for f in re.split(r"^##[ \t]+", prior.content, flags=re.M) if f.strip()]
    return [{"id": f"p{k}", "section": "core", "cite": [], "since": prior.timestamp,
             "text": _recall.oneline(f.strip().replace("\n", ": ", 1), len(f) + 1)} for k, f in enumerate(facets, 1)]


def growth_edits(meta: object) -> list[dict[str, Any]]:
    """A crystal's meta.edits, well-formed entries only (an object with a known op; cite a list of strings): a
    host-written or imported row may carry anything."""
    edits = meta.get("edits") if isinstance(meta, dict) else None
    return [{**e, "cite": [str(c) for c in e["cite"]] if isinstance(e.get("cite"), list) else []}
            for e in edits if isinstance(e, dict) and e.get("op") in EDIT_OPS] if isinstance(edits, list) else []


def growth_log(lk: Any, limit: int) -> list[dict[str, Any]]:
    """§6.3 growth log: meta.edits walked back from the current crystal through meta.drift.prior_id, newest first
    (`lk` is a Lake or a RemoteLake: only crystal() and get() are used)."""
    d, seen, log = lk.crystal(), set(), []  # type: tuple[Any, set[str], list[dict[str, Any]]]
    while d is not None and d.id not in seen and len(log) < limit:
        seen.add(d.id)
        log += [{"at": d.timestamp, "crystal": d.id, **e} for e in growth_edits(d.meta)]
        drift = (d.meta or {}).get("drift") if isinstance(d.meta, dict) else None
        prior = drift.get("prior_id") if isinstance(drift, dict) else None
        d = lk.get(prior, include_expired=True) if isinstance(prior, str) and prior else None
    return log[:limit]


def render_crystal(
    items: Sequence[Mapping[str, Any]], changed: Sequence[Mapping[str, Any]], lab: Mapping[str, str]
) -> tuple[str, int]:
    """§6.3 rendering: one `## {label}` block per non-empty section (core, tension, open), each item a paragraph,
    then the "What changed" block; over RENDER_CAP, the oldest changes go first (the growth log keeps them), then
    trailing open items, then tension items (history yields to what is held now). Returns (text, items cut)."""
    def sent(x: object) -> str:  # a stance position has no final period: end each part as a sentence
        x = _recall.oneline(str(x), 100)
        return x if x.endswith((".", "!", "?", "…")) else x + "."
    lines = [lab[f"crystal_{e['op']}"].format(old=sent(e.get("old")), new=sent(e.get("new")))
             + (lab["crystal_why"].format(why=e["why"]) if e.get("why") else "") for e in changed]
    shown = list(items)

    def text() -> str:
        blocks = [(lab[f"crystal_{s}"], [i["text"] for i in shown if i["section"] == s]) for s in SECTIONS]
        return "\n\n".join(f"## {h}\n\n" + "\n\n".join(b) for h, b in [*blocks, (lab["crystal_changed"], lines)] if b)
    out = text()
    for sec in (None, "open", "tension"):
        while len(out) > RENDER_CAP and (sec is None and lines or any(i["section"] == sec for i in shown)):
            if sec is None:
                lines.pop()
            else:
                shown.pop(max(k for k, i in enumerate(shown) if i["section"] == sec))
            out = text()
    return out, len(items) - len(shown)


def scoped_containers(run: Run) -> tuple[list[Delta], int]:
    """Live containers of this run's scope by level DESC, timestamp DESC: (the first 40, the total)."""
    sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE d.kind = 'container' AND {_store.LIVE} ORDER BY d.level DESC, d.timestamp DESC, d.seq DESC"
    rows = [r for r in run.lake.conn.execute(sql, (run.now_s,)) if in_scope(run, r["meta"]) and not automated(run, r["id"])]
    return _db.rows_to_deltas(run.lake.conn, rows[:40]), len(rows)


def automation(lake: Lake, prefix: str) -> tuple[list[str], list[str]]:
    """§6 automation rows (Lake(automation=...)): the sources to leave out, and the tags: the `tag:` rules plus the
    session tags of unlabelled rows starting with a `prefix:` rule, so the whole session of such a row stays out."""
    rules = {k: [v for kk, v in lake.automation if kk == k] for k in ("source", "tag", "prefix")}
    tags, starts = rules["tag"], rules["prefix"]
    if starts and prefix:
        match = " OR ".join(["substr(d.content, 1, ?) = ?"] * len(starts))
        labelled = f"NOT EXISTS (SELECT 1 FROM delta_tags a WHERE a.delta_id = d.id AND a.tag IN ({_db.qmarks(len(tags))}))"
        sql = (f"SELECT DISTINCT t.tag FROM deltas d JOIN delta_tags t ON t.delta_id = d.id WHERE ({match})"
               f" AND {labelled if tags else '1'} AND t.tag >= ? AND t.tag < ? ORDER BY t.tag")
        bound = [*(x for v in starts for x in (len(v), v)), *tags, prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)]
        tags = [*tags, *(str(r[0]) for r in lake.conn.execute(sql, bound))]
    return rules["source"], tags


def automated(run: Run, cid: str) -> bool:
    """§6.3: a container most of whose parents are automation rows (containered before the host named them) is not
    crystal material."""
    if not run.lake.automation:
        return False
    auto = _recall.Filters(exclude_sources=run.auto[0], exclude_tags=run.auto[1], include_expired=True)
    where, bound = _recall.filter_sql(run.lake, auto, run.now)
    sql = f"SELECT count(*), sum({where}) FROM derived_from e JOIN deltas d ON d.id = e.parent_id WHERE e.delta_id = ?"
    n, kept = run.lake.conn.execute(sql, [*bound, cid]).fetchone()
    return 2 * int(kept or 0) < int(n)


def take(
    budget: int, used: int, items: Sequence[Delta], make: Callable[[Delta], str]
) -> tuple[list[Delta], list[str], int]:
    """Append lines while the prompt stays under budget; a cut section closes with the truncation line."""
    kept: list[Delta] = []
    lines: list[str] = []
    for d in items:
        line = make(d)
        if used + len(line) + 1 > budget:
            lines.append(TRUNC)
            break
        used += len(line) + 1
        kept.append(d)
        lines.append(line)
    return kept, lines, used


def mood_block(row: Delta) -> str:
    """The mood section body: `{headline} — {subtext}` then the carrier wave, from the mood's JSON content."""
    obj = mood_json(row)
    if not obj:
        return _recall.oneline(row.content, 400)
    return f"{obj.get('headline', '')} — {obj.get('subtext', '')}\n{obj.get('carrier_wave', '')}"


def leaks(lake: Lake, text: str, material: str) -> list[str]:
    """§6.3 operator guard: the think's `operator` names (the claude adapter's account, SPEC §8) found in `text`
    as a whole word, case-insensitively, that `material` never names."""
    found = [(n, re.compile(rf"(?<![\w@.]){re.escape(n)}(?![\w@])", re.I)) for n in getattr(lake.think, "operator", ())]
    return [n for n, rx in found if rx.search(text) and not rx.search(material)]


def crystal_distance(lake: Lake, text: str, prior_content: str) -> float:
    """§6.3/A2: 1 − cos(embed(candidate), embed(prior)) over the two crystal texts, rounded to 4 places.
    The per-rewrite cap checks this quantity; the accepted candidate's value is reused as the row's drift."""
    a, b = lake.call_embed([text, prior_content])
    return round(1.0 - _vectors.cosine(a, b), 4)


def crystal_drift_value(lake: Lake, text: str, prior: Delta | None, dist: float | None) -> tuple[float | None, str]:
    """§6.3 recorded drift: the cap-check cosine distance when it applies (no second embedding), else the
    Jaccard token distance without `embed`; `None` for the first crystal."""
    if prior is None:
        return None, "cosine" if lake.embed is not None else "token"
    if lake.embed is not None:
        return dist, "cosine"
    sa, sb = set(TOKEN_RE.findall(text.lower())), set(TOKEN_RE.findall(prior.content.lower()))
    return (round(1.0 - len(sa & sb) / len(sa | sb), 4) if sa | sb else 0.0), "token"


def external_evidence(d: Delta) -> bool:
    """§6.5: a source grounds a sediment externally only when it is genuine outside evidence — not the
    lake's own writes (`lake:` source) and not the assistant's own reply (`assistant` tag). The
    assistant's words are self-report whatever channel carries them, so a lake conclusion echoed back
    through a captured reply cannot launder itself into external standing."""
    return not d.source.startswith("lake:") and "assistant" not in d.tags


def sediment(
    lake: Lake, plan: Sequence[Mapping[str, Any]], hits: Sequence[Hit], flag: bool | None, warnings: list[str]
) -> Delta | None:
    """§6.5: the automatic pass after a deep recall — gates, the prompt over the first 20 final hits,
    one retry, the lake:sediment row. A gate that stays shut writes nothing without a warning; a think
    exception or a twice-empty answer becomes a warning, never a failed recall. No lease is taken."""
    if flag is False or lake.think is None or lake.readonly:
        return None
    rows = [h.delta for h in hits[:SEDIMENT_MAX_SOURCES]]
    if len({d.source for d in rows}) < 2:  # counted over the rows actually rendered (§12.11)
        return None
    searches = [str(step["search"]) for step in plan if "search" in step]
    q = _recall.WS_RE.sub(" ", " · ".join(searches)).strip() if searches else None
    user = "Memories that surfaced:\n" + "\n".join(row_line(lake, d, 400) for d in rows)
    if q is not None:
        user = f'Query: "{q}"\n\n{user}'
    now = lake.now()
    user = f"{CLOCK.format(_time.dt_to_ts(now)[:16])}\n\n{user}"
    system, prose = prompt("sediment"), ""
    try:
        for retry in range(2):
            message = user if retry == 0 else f"{user}\n\n{REJECTED}empty answer."
            answer = parse_answer(lake.call_think(message, system=system, json=False), json=False)
            prose = answer if isinstance(answer, str) else ""
            if prose:
                break
        else:
            warnings.append("sediment rejected twice: empty answer")
            return None
    except Exception as exc:  # unlike everywhere else (§4.2): a failed sediment must never fail the recall
        warnings.append(f"sediment failed: {exc}")
        return None
    content = prose if len(prose) <= 4000 else prose[:4000] + "…"
    meta: dict[str, Any] = {"model": lake.model_name}
    if q is not None:
        meta["query"] = q
    meta["grounding"] = "external" if any(external_evidence(d) for d in rows) else "self-referential"
    ids = [d.id for d in rows]
    with lake.tx() as conn:
        prior = _store.prior_row(lake, conn, "lake:sediment", "", now)
        if prior is not None and prior["content"] == content:  # §4.4 step 5: surface the first row
            return _db.rows_to_deltas(conn, [prior])[0]
        did, now_s = _db.new_id(conn), _time.dt_to_ts(now)
        _store.insert_row(conn, delta_id=did, timestamp=now_s, content=content, source="lake:sediment", kind="sediment",
                          level=0, tags=[], derived_from=ids, expires_at=None, media_hash=None, meta=meta)
    delta = Delta(
        id=did, timestamp=now_s, content=content, source="lake:sediment", kind="sediment", level=0, tags=[],
        derived_from=ids, expires_at=None, media_hash=None,
        meta=json.loads(_store.dump_meta(meta) or "null"), engagement=None,
    )
    if lake.embed is not None and lake.embed_on_write:
        try:
            _store.embed_and_store(lake, delta)
        except EmbedError as exc:  # §6.5: the row stays committed; embed_missing() reaches it later
            warnings.append(str(exc))
    return delta
