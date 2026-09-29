"""Plan DSL: validation, step executors, timelines, neighbors, aggregate (SPEC §5.5, §5.6.1)."""

from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from . import _consolidate, _db, _recall, _time
from ._recall import Cand, Filters
from ._types import Bucket, CollapsedRun, Delta, Hit, PlanError, PlanResult, StepResult, Timeline, TimelineRow

if TYPE_CHECKING:
    from .lake import Lake

ACTIONS = ("search", "filter", "intersect", "union", "diff", "bridge", "aggregate", "chain", "neighbors", "timeline")
LIST_REFS = frozenset({"intersect", "union", "diff", "bridge"})
STR_REFS = frozenset({"chain", "aggregate", "neighbors", "timeline"})
GROUP_BYS = frozenset({"hour", "day", "week", "month", "tag", "source", "kind"})
NUMERIC = (("limit", 1), ("radius_minutes", 1), ("limit_per_seed", 1), ("max_per_side", 1), ("gap_minutes", 1), ("merge_gap_seconds", 0))
LIST_KEYS = (("tags", ("tags", "tags_include")), ("exclude_tags", ("exclude_tags", "tags_exclude")), ("exclude_sources", ("exclude_sources",)))
TIME_KEYS = (("since", ("since", "time_start")), ("until", ("until", "time_end")))
TOKEN_RE = re.compile(r"[^\W_]+")
BRIDGE_CAP = 1000  # §5.5 bridge: cut at min(limit, 1000)
GROUP_TOKENS = 32  # §5.5 FTS form of bridge/chain: the most frequent tokens per input
FETCH_FACTOR = 40  # §5.6.1 T1: fetch_per_side = 40 × max_per_side


@dataclass(frozen=True)
class TimelineParams:
    """§5.5 timeline step parameters; context() passes (20, 6, 15, 300)."""

    radius_minutes: int = 30
    max_per_side: int = 15
    gap_minutes: int = 30
    merge_gap_seconds: int = 300
    collapse_sources: tuple[str, ...] | None = None  # None: the Lake's source: automation rules


@dataclass(frozen=True)
class NeighborParams:
    """§5.5 neighbors step parameters."""

    radius_minutes: int = 30
    source_match: bool = True
    limit_per_seed: int = 6


def action_of(step: Mapping[str, Any]) -> str:
    """The step's single action key (validate() guarantees exactly one)."""
    return next(a for a in ACTIONS if a in step)


def validate(
    steps: Sequence[Mapping[str, Any]], filters: Mapping[str, object] | None = None, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """§5.5 validation before any step runs; §12.6 index ids; PlanError messages verbatim."""
    now = now or datetime.now(UTC)
    actions: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(steps):
        step = dict(raw)
        sid = step["id"] = str(i) if raw.get("id") is None else str(raw["id"])
        if sid in actions:
            raise PlanError(f"Duplicate step id: '{sid}'")
        found = [a for a in ACTIONS if a in step]
        if not found:
            raise PlanError(f"Step '{sid}' has no action — must set exactly one of: {', '.join(ACTIONS)}")
        if len(found) > 1:
            raise PlanError(f"Step '{sid}' sets more than one action")
        action, value = found[0], step[found[0]]
        refs: list[str] = []
        if action in LIST_REFS:
            if not isinstance(value, (list, tuple)) or not all(isinstance(r, str) for r in value):
                raise PlanError(f"Step '{sid}' has invalid {action}: {value!r}")
            if len(value) < 2:
                raise PlanError(f"Step '{sid}' needs at least two references")
            refs = list(value)
        elif action in STR_REFS:
            if not isinstance(value, str):
                raise PlanError(f"Step '{sid}' has invalid {action}: {value!r}")
            refs = [value]
        elif (action == "search" and not isinstance(value, str)) or (action == "filter" and not isinstance(value, Mapping)):
            raise PlanError(f"Step '{sid}' has invalid {action}: {value!r}")
        for ref in refs:
            if ref not in actions:
                raise PlanError(f"Step '{sid}' references '{ref}' which is not defined (or comes later in the plan)")
            if actions[ref] == "aggregate":
                raise PlanError(f"Step '{sid}' references aggregate step '{ref}' which produces buckets, not deltas")
        if action == "aggregate" and not (isinstance(gb := step.get("group_by", "week"), str) and gb in GROUP_BYS):
            raise PlanError(f"Step '{sid}' has unknown group_by '{step.get('group_by')}'")
        for key, floor in NUMERIC:
            v = step.get(key)
            low = 0 if action == "timeline" and key == "limit" else floor
            if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < low):
                raise PlanError(f"Step '{sid}' has invalid {key}: {v!r}")
        check_times(f"Step '{sid}'", [step, value] if action == "filter" else [step], now)
        actions[sid] = action
        out.append(step)
    check_times("filters", [filters or {}], now)
    return out


def check_times(label: str, layers: Sequence[Mapping[str, Any]], now: datetime) -> None:
    """Parse every since/until (and alias) so a bad TimeSpec is a PlanError before any step runs."""
    for layer in layers:
        for _, aliases in TIME_KEYS:
            for key in aliases:
                if layer.get(key) is not None:
                    try:
                        _time.parse_timespec(layer[key], now)
                    except ValueError as exc:
                        raise PlanError(f"{label}: {exc}") from exc


def as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):  # a PlanError message, never a TypeError traceback (§5.5)
        raise PlanError(f"expected a string or a list of strings, not {value!r}")
    return [str(v) for v in value]


def merge_filters(layers: Sequence[Mapping[str, Any]], now: datetime) -> Filters | None:
    """§5.5: the plan-level mapping ANDed with a step's fields (and a `filter` step's dict): list keys
    union, `source`/`kind` intersect, `since`/`until` narrow, `any_tags`/`has_media` take the last
    layer. None when the intersection can match no row."""
    acc: dict[str, Any] = {}
    for layer in layers:
        for key, aliases in LIST_KEYS:
            for alias in aliases:
                if layer.get(alias):
                    acc[key] = list(dict.fromkeys([*acc.get(key, []), *as_list(layer[alias])]))
        for key in ("source", "kind"):
            if layer.get(key) is not None:
                vals = as_list(layer[key])
                acc[key] = vals if key not in acc else [v for v in acc[key] if v in vals]
                if not acc[key]:
                    return None
        if layer.get("any_tags"):
            acc["any_tags"] = as_list(layer["any_tags"])
        if layer.get("has_media") is not None:
            acc["has_media"] = bool(layer["has_media"])
        for key, aliases in TIME_KEYS:
            for alias in aliases:
                if layer.get(alias) is not None:
                    dt = _time.parse_timespec(layer[alias], now)
                    acc[key] = dt if key not in acc else (max(acc[key], dt) if key == "since" else min(acc[key], dt))
    return Filters(
        source=acc.get("source"), exclude_sources=acc.get("exclude_sources"), tags=acc.get("tags"),
        any_tags=acc.get("any_tags"), exclude_tags=acc.get("exclude_tags"), kind=acc.get("kind"),
        since=acc.get("since"), until=acc.get("until"), has_media=acc.get("has_media"),
    )


def row_filters(f: Filters) -> Filters:
    """The neighbors/timeline subset: source, exclude_sources, tags, any_tags, exclude_tags (§5.5, T1)."""
    return replace(f, kind=None, since=None, until=None, has_media=None)


def run(
    lake: Lake, steps: Sequence[Mapping[str, Any]], filters: Mapping[str, object] | None = None,
    *, sediment: bool | None = None,
) -> PlanResult:
    """§4.5 plan(): validate, run every step in order with plan-level filters ANDed in, set
    lake.last_warnings, then the §6.5 sediment pass (never for an aggregate last step)."""
    now = lake.now()
    plan = validate(steps, filters, now=now)
    result, hits_of = execute(lake, plan, filters, now)
    if not plan or action_of(plan[-1]) == "aggregate":  # §6.5: an aggregate last step has no final hits
        return result
    row = _consolidate.sediment(lake, plan, hits_of[plan[-1]["id"]], sediment, result.warnings)
    return result if row is None else replace(result, sediment=row)


def recall_hits(
    lake: Lake, steps: Sequence[Mapping[str, Any]], filters: Mapping[str, object] | None = None,
    *, sediment: bool | None = None,
) -> list[Hit]:
    """recall(plan=...): run() then the last step's hits (PlanError for aggregate; timeline flattens);
    the §6.5 sediment pass runs on those final hits, which the pass never changes."""
    now = lake.now()
    plan = validate(steps, filters, now=now)
    if plan and action_of(plan[-1]) == "aggregate":
        raise PlanError(f"Step '{plan[-1]['id']}' is an aggregate: recall(plan=...) needs a last step that produces deltas")
    result, hits_of = execute(lake, plan, filters, now)
    if not plan:
        return []
    hits = hits_of[plan[-1]["id"]]
    _consolidate.sediment(lake, plan, hits, sediment, result.warnings)  # warnings is lake.last_warnings
    return hits


def execute(
    lake: Lake, plan: Sequence[Mapping[str, Any]], filters: Mapping[str, object] | None, now: datetime
) -> tuple[PlanResult, dict[str, list[Hit]]]:
    """Run validated steps in order: every StepResult, plus the hits each step resolves to downstream."""
    t0 = time.perf_counter()
    base: dict[str, Any] = dict(filters or {})
    warnings: list[str] = []
    results: dict[str, StepResult] = {}
    hits_of: dict[str, list[Hit]] = {}
    for step in plan:
        sid, action = step["id"], action_of(step)
        limit = int(step.get("limit", 100))
        hits: list[Hit] = []
        buckets: list[Bucket] | None = None
        strips: list[Timeline] | None = [] if action == "timeline" else None
        f = merge_filters([base, step, step["filter"]] if action == "filter" else [base, step], now)
        if action in ("intersect", "union", "diff"):
            hits = setop(action, [hits_of[r] for r in step[action]], limit)
        elif action == "aggregate":
            buckets = aggregate(hits_of[step[action]], str(step.get("group_by", "week")))
        elif f is None:
            pass  # the ANDed scopes match no row: an empty result
        elif action in ("search", "filter"):
            hits = _recall.search(lake, step.get("search"), f, limit=limit, now=now, step=sid, warnings=warnings)
        elif action in ("bridge", "chain"):
            refs = list(step[action]) if action == "bridge" else [step[action]]
            hits = related(lake, sid, action, [(r, hits_of[r]) for r in refs], f, limit, now, warnings)
        elif action == "neighbors":
            p = NeighborParams(
                int(step.get("radius_minutes", 30)), bool(step.get("source_match", True)), int(step.get("limit_per_seed", 6))
            )
            hits = neighbors(lake, hits_of[step[action]], p, row_filters(f), limit=limit, now=now, step=sid)
        else:
            cs = step.get("collapse_sources")
            p2 = TimelineParams(
                int(step.get("radius_minutes", 30)), int(step.get("max_per_side", 15)), int(step.get("gap_minutes", 30)),
                int(step.get("merge_gap_seconds", 300)), None if cs is None else tuple(as_list(cs)),
            )
            strips = timeline(lake, hits_of[step[action]], p2, row_filters(f), now=now)
            strips = strips[:limit] if limit else strips
            hits = flatten(lake, strips, now=now, step=sid)
        results[sid] = StepResult(None if action in ("aggregate", "timeline") else hits, buckets, strips)
        hits_of[sid] = hits
    lake.last_warnings = warnings
    return PlanResult(results, warnings, (time.perf_counter() - t0) * 1000), hits_of


def setop(op: str, inputs: Sequence[Sequence[Hit]], limit: int) -> list[Hit]:
    """§5.5 n-ary intersect/union/diff over ids: the first occurrence fixes position, the best score is kept."""
    best: dict[str, Hit] = {}
    for hits in inputs:
        for h in hits:
            cur = best.get(h.delta.id)
            if cur is None or h.score > cur.score:
                best[h.delta.id] = h
    sets = [{h.delta.id for h in hits} for hits in inputs]
    if op == "intersect":
        keep = sets[0].intersection(*sets[1:])
    elif op == "diff":
        keep = sets[0].difference(*sets[1:])
    else:
        keep = sets[0].union(*sets[1:])
    return [h for i, h in best.items() if i in keep][:limit]


def fts_group(texts: Iterable[str]) -> str:
    """§5.5 FTS form: the 32 most frequent tokens (>= 4 chars, not in the stoplist) as ("a" OR "b" …)."""
    counts = Counter(
        t for text in texts for t in TOKEN_RE.findall(text.lower()) if len(t) >= 4 and t not in _recall.STOPLIST
    )
    return "(" + " OR ".join(f'"{t}"' for t, _ in counts.most_common(GROUP_TOKENS)) + ")" if counts else ""


def related(
    lake: Lake, sid: str, action: str, inputs: Sequence[tuple[str, Sequence[Hit]]], f: Filters, limit: int,
    now: datetime, warnings: list[str],
) -> list[Hit]:
    """§5.5 bridge (min cosine over every input's centroid) and chain (one centroid): the vector form
    through lake.vectors, else the FTS approximation; inputs excluded, noise rules, §5.3 score."""
    for ref, hits in inputs:
        if not hits:
            warnings.append(f"step '{sid}' skipped: input '{ref}' is empty")
            return []
    exclude = {h.delta.id for _, hits in inputs for h in hits}
    where, params = _recall.filter_sql(lake, f, now)
    cache = lake.vectors
    if cache is not None:
        cents: list[list[float]] = []
        for ref, hits in inputs:
            c = cache.centroid(lake, [h.delta.id for h in hits])
            if c is None:
                warnings.append(f"step '{sid}' skipped: no embeddings in '{ref}'")
                return []
            cents.append(c)
        sql = f"SELECT d.id FROM deltas d JOIN vectors v ON v.delta_id = d.id WHERE {where}"
        ids = [r[0] for r in lake.conn.execute(sql, params) if r[0] not in exclude]
        cos = [dict(cache.cosine_top(lake, c, ids, _recall.FTS_POOL if action == "chain" else len(ids))) for c in cents]
        raw = [(i, min(d[i] for d in cos)) for i in ids if all(i in d for d in cos)]
        rows = _recall.rows_by_id(lake, [i for i, _ in raw])
        cands = [Cand(rows[i], rel, action) for i, rel in _recall.minmax(raw)]
    else:
        groups = [fts_group(h.delta.content for h in hits) for _, hits in inputs]
        if not all(groups):
            return []
        pool = [(row, bm) for row, bm in _recall.fts_pool(lake, " AND ".join(groups), where, params) if row["id"] not in exclude]
        top = max((bm for _, bm in pool), default=0.0)
        cands = [Cand(row, bm / top if top > 0 else 1.0, action) for row, bm in pool]
    rules = lake.noise
    cands = [c for c in cands if not _recall.is_noise(rules, c.row["content"], c.row["source"], c.row["kind"], c.row["media_hash"])]
    return _recall.score(lake, cands, now=now, limit=min(limit, BRIDGE_CAP) if action == "bridge" else limit, step=sid)


def aggregate(hits: Sequence[Hit], group_by: str) -> list[Bucket]:
    """§5.5 aggregate: buckets keyed by hour/day/week/month/tag/source/kind, sorted by key."""
    if group_by not in GROUP_BYS:
        raise PlanError(f"unknown group_by '{group_by}'")
    keyed: dict[str, list[str]] = {}
    for h in hits:
        d, ts = h.delta, h.delta.timestamp
        if group_by == "tag":
            keys = list(d.tags)
        elif group_by == "source":
            keys = [d.source]
        elif group_by == "kind":
            keys = [d.kind or "plain"]
        elif group_by == "week":
            year, week, _ = _time.ts_to_dt(ts).isocalendar()
            keys = [f"{year}-W{week:02d}"]
        else:
            keys = [{"hour": f"{ts[:10]} {ts[11:13]}:00", "day": ts[:10], "month": ts[:7]}[group_by]]
        for k in keys:
            keyed.setdefault(k, []).append(d.id)
    return [Bucket(k, len(v), v) for k, v in sorted(keyed.items())]


def recency(lake: Lake, ts: str, now: datetime) -> float:
    """§5.3 recency factor for one stored timestamp."""
    return _recall.recency_factor(lake, _time.age_seconds(now, ts))


def neighbors(
    lake: Lake, seeds: Sequence[Hit], params: NeighborParams, filters: Filters, *, limit: int,
    now: datetime | None = None, step: str | None = None,
) -> list[Hit]:
    """§5.5 neighbors: rows within ± radius of each seed, seeds excluded, score = 1 - gap/radius."""
    now = now or lake.now()
    where, base = _recall.filter_sql(lake, filters, now)
    seed_ids = list(dict.fromkeys(h.delta.id for h in seeds))
    radius = timedelta(minutes=params.radius_minutes)
    best: dict[str, tuple[float, sqlite3.Row]] = {}
    for seed in seeds:
        at = _time.ts_to_dt(seed.delta.timestamp)
        sql = (
            f"SELECT {_recall.DCOLS} FROM deltas d WHERE {where} AND d.timestamp BETWEEN ? AND ?"
            f" AND d.id NOT IN ({_db.qmarks(len(seed_ids))})"
        )
        args: list[object] = [*base, _time.dt_to_ts(at - radius), _time.dt_to_ts(at + radius), *seed_ids]
        if params.source_match:
            sql += " AND d.source = ?"
            args.append(seed.delta.source)
        sql += " ORDER BY abs(julianday(d.timestamp) - julianday(?)), d.timestamp, d.seq LIMIT ?"
        args += [seed.delta.timestamp, params.limit_per_seed]
        for row in lake.conn.execute(sql, args):
            gap = abs((_time.ts_to_dt(row["timestamp"]) - at).total_seconds())
            if row["id"] not in best or gap < best[row["id"]][0]:
                best[row["id"]] = (gap, row)
    span = params.radius_minutes * 60.0
    ranked = sorted(best.values(), key=lambda t: (round(t[0], 6), -_time.ts_to_dt(t[1]["timestamp"]).timestamp(), t[1]["id"]))
    kept = ranked[:limit]
    deltas = _db.rows_to_deltas(lake.conn, [row for _, row in kept], now_ts=_time.dt_to_ts(now))
    vals = _recall.valences(lake, [d.id for d in deltas], now)
    return [
        Hit(d, 1 - gap / span, 1 - gap / span, recency(lake, d.timestamp, now), vals.get(d.id, 1.0), "neighbor", step)
        for d, (gap, _) in zip(deltas, kept, strict=True)
    ]


def secs(a: Delta, b: Delta) -> float:
    """Seconds from a's timestamp to b's (negative when b is earlier)."""
    return (_time.ts_to_dt(b.timestamp) - _time.ts_to_dt(a.timestamp)).total_seconds()


def fold(entries: list[list[Delta]], same: Callable[[list[Delta], list[Delta]], bool]) -> list[list[Delta]]:
    """Fold every maximal run (length >= 2) of consecutive entries that `same` links into one entry."""
    out: list[list[Delta]] = []
    i = 0
    while i < len(entries):
        j = i + 1
        while j < len(entries) and same(entries[j - 1], entries[j]):
            j += 1
        out.append([d for g in entries[i:j] for d in g] if j - i > 1 else entries[i])
        i = j
    return out


def collapse(rows: Sequence[Delta], protected: set[str], sources: Sequence[str]) -> list[list[Delta]]:
    """T4 same-second runs, then T5 runs of the Lake's source: automation rules, or the step's collapse_sources (counts
    summed); a protected row breaks every run."""

    def free(a: list[Delta], b: list[Delta]) -> bool:
        return a[0].id not in protected and b[0].id not in protected

    groups = fold(
        [[d] for d in rows],
        lambda a, b: free(a, b) and a[0].source == b[0].source and a[0].timestamp[:19] == b[0].timestamp[:19],
    )
    if not sources:
        return groups
    return fold(groups, lambda a, b: free(a, b) and a[0].source in sources and b[0].source in sources)


def timeline(
    lake: Lake, seeds: Sequence[Hit], params: TimelineParams, filters: Filters, *, now: datetime | None = None
) -> list[Timeline]:
    """§5.6.1 T1–T7 around each seed; every strip, numbered and ordered by t_start; no cut. Only the
    filters' row conditions apply to T1 (the caller passes a Filters without kind/since/until). A
    seed's `seq` is not on the Hit: fetch it by id (one IN query for all seeds) for the T1 bounds."""
    now = now or lake.now()
    sources = lake.automation_sources if params.collapse_sources is None else params.collapse_sources
    where, base = _recall.filter_sql(lake, filters, now)
    seed_ids = {h.delta.id for h in seeds}
    seq_of: dict[str, int] = {}
    for chunk in _db.chunks(list(seed_ids)):
        seq_of.update(lake.conn.execute(f"SELECT id, seq FROM deltas WHERE id IN ({_db.qmarks(len(chunk))})", chunk))
    fetch, radius = FETCH_FACTOR * params.max_per_side, timedelta(minutes=params.radius_minutes)
    pool: dict[str, sqlite3.Row] = {}
    fetched: list[tuple[Hit, list[str]]] = []
    for seed in seeds:  # T1: the nearest `fetch` rows at or before (timestamp, seq) and after it
        ts = seed.delta.timestamp
        at, seq = _time.ts_to_dt(ts), seq_of.get(seed.delta.id, 0)
        head = f"SELECT {_recall.DCOLS} FROM deltas d WHERE {where} AND "
        sides = (
            (head + "d.timestamp >= ? AND (d.timestamp < ? OR (d.timestamp = ? AND d.seq <= ?)) ORDER BY d.timestamp DESC, d.seq DESC LIMIT ?",
             [*base, _time.dt_to_ts(at - radius), ts, ts, seq, fetch]),
            (head + "d.timestamp <= ? AND (d.timestamp > ? OR (d.timestamp = ? AND d.seq > ?)) ORDER BY d.timestamp, d.seq LIMIT ?",
             [*base, _time.dt_to_ts(at + radius), ts, ts, seq, fetch]),
        )
        rows = sorted((r for sql, args in sides for r in lake.conn.execute(sql, args)), key=lambda r: (r["timestamp"], r["seq"]))
        pool.update((r["id"], r) for r in rows)
        fetched.append((seed, list(dict.fromkeys(r["id"] for r in rows))))
    deltas = {d.id: d for d in _db.rows_to_deltas(lake.conn, list(pool.values()), now_ts=_time.dt_to_ts(now))}
    seq_of.update((r["id"], r["seq"]) for r in pool.values())

    def sort_key(d: Delta) -> tuple[str, int]:
        return d.timestamp, seq_of[d.id]

    gap, m = params.gap_minutes * 60, params.max_per_side
    windows: list[tuple[set[str], list[Delta]]] = []
    for seed, ids in fetched:
        if not ids:
            continue  # T2: a seed with no rows is skipped
        rows_ = [deltas[i] for i in ids]
        sid, at = seed.delta.id, _time.ts_to_dt(seed.delta.timestamp)
        k = ids.index(sid) if sid in ids else min(range(len(rows_)), key=lambda i: abs(_time.ts_to_dt(rows_[i].timestamp) - at))
        anchors = {sid, rows_[k].id}
        lo, hi = k, k  # T3: stop before the first gap over gap_minutes on either side
        while lo > 0 and secs(rows_[lo - 1], rows_[lo]) <= gap:
            lo -= 1
        while hi + 1 < len(rows_) and secs(rows_[hi], rows_[hi + 1]) <= gap:
            hi += 1
        groups = collapse(rows_[lo : hi + 1], anchors, sources)  # T4, T5
        a = next(i for i, g in enumerate(groups) if g[0].id == rows_[k].id)
        windows.append((anchors, [d for g in groups[max(0, a - m) : a + m + 1] for d in g]))
    windows.sort(key=lambda w: w[1][0].timestamp)
    merged: list[tuple[set[str], list[Delta]]] = []
    for anchors, rows_ in windows:  # T6
        if merged and secs(merged[-1][1][-1], rows_[0]) <= params.merge_gap_seconds:
            prev = merged[-1]
            merged[-1] = (prev[0] | anchors, sorted({d.id: d for d in [*prev[1], *rows_]}.values(), key=sort_key))
        else:
            merged.append((anchors, rows_))
    out: list[Timeline] = []
    for i, (anchors, rows_) in enumerate(merged):  # T7
        entries: list[TimelineRow | CollapsedRun] = [
            TimelineRow(g[0], g[0].id in anchors) if len(g) == 1 else CollapsedRun(g[0].source, len(g), g[0].timestamp, g[-1].timestamp)
            for g in collapse(rows_, anchors, sources)
        ]
        out.append(Timeline(f"tl_{i}", rows_[0].timestamp, rows_[-1].timestamp, sorted(anchors & seed_ids), entries))
    return out


def flatten(lake: Lake, strips: Sequence[Timeline], *, now: datetime | None = None, step: str | None = None) -> list[Hit]:
    """A timeline step's rows for downstream steps: deduped, strip then time order, matched "timeline",
    score = recency × valence."""
    now = now or lake.now()
    deltas = {r.delta.id: r.delta for s in strips for r in s.rows if isinstance(r, TimelineRow)}
    vals = _recall.valences(lake, list(deltas), now)
    out: list[Hit] = []
    for d in deltas.values():
        rec, val = recency(lake, d.timestamp, now), vals.get(d.id, 1.0)
        out.append(Hit(d, rec * val, 1.0, rec, val, "timeline", step))
    return out
