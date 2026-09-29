"""Filters, candidates, noise rules, scoring, recall(), and shared text helpers (SPEC §5.1–§5.4)."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from . import _db, _time, _vectors
from ._types import Hit, LakeError, NoiseRules, TimeSpec

if TYPE_CHECKING:
    from .lake import Lake

# §5.2 step 1 stoplist (121 words), matched against the lower-cased token before stemming.
STOPLIST: frozenset[str] = frozenset(
    "a an the and or but if then else so of to in on at by for with from as into onto over under about after "
    "before between through is are was were be been being am do does did done have has had having will would "
    "shall should can could may might must it its this that these those i me my mine you your yours he him his "
    "she her hers we us our ours they them their theirs who whom whose which what when where why how not no nor "
    "all any some each every both few more most other such only own same than too very just also here there now "
    "up down out off again once".split()
)
FTS_POOL = 500  # §5.2: the FTS pool and the vector pool each hold at most 500 rows
VEC_FLOOR = 0.3  # §5.2 step 2: a row reached by the vector pass alone needs rel_vec >= this
QUERY_EMBED_WAIT = 3.0  # §5.2: a query embed slower than this (a busy embedder) falls back to FTS only
FUSE_FTS = 0.3  # §5.2 step 3: a hybrid row's relevance = FUSE_FTS * rel_fts + (1 - FUSE_FTS) * rel_vec
KINDS = frozenset({"plain", "container", "mood", "crystal", "sediment", "engagement"})
DCOLS = ", ".join("d." + c for c in _db.DELTA_COLS.split(", "))  # DELTA_COLS qualified for joins
LIVE = "(d.expires_at IS NULL OR d.expires_at > ?)"
TOKEN_RE = re.compile(r"[^\W_]+")
SPLIT_RE = re.compile(r"[\s,;]+")
ID_PIECE_RE = re.compile(r"^(?:[a-z]{2,10}_[A-Za-z0-9_-]{8,}|[0-9a-f]{12,}|[A-Za-z0-9+/=_-]{32,})$")
HOOK_KEYS = frozenset({"hook_event_name", "session_id", "transcript_path", "tool_use_id", "tool_name", "tool_input", "tool_response", "cwd"})
TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
ENTITY_RE = re.compile(r"&(?:nbsp|amp|lt|gt|quot|apos|#\d+);")
WS_RE = re.compile(r"\s+")
SENTENCE_RE = re.compile(r"[.!?]\s|\n")
VALENCE: MappingProxyType[str, float] = MappingProxyType({"affirm": 1.0, "refute": -1.0, "reply": 0.25})
RECENCY_FLOOR = 0.5  # §5.3: recency = RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (age / half_life)
QUESTION_DROP = 1.5  # §5.3: a plain row that is only a question scores 1 / QUESTION_DROP (query present)
VALENCE_LIFT = 0.05  # §5.3: +5% per point of a net-positive engagement sum
VALENCE_LIFT_CAP = 0.30  # §5.3: the lift saturates at +30%
VALENCE_REFUTE_DROP = 0.50  # §5.3: the first net refute halves the score (must beat the max boost 1.176)
VALENCE_DROP_CAP = 0.70  # §5.3: the drop floors the multiplier at 0.30
SUPERSEDE_DROP = 0.50  # §5.3: a row some live link names as `old` scores ×0.50, once, beside valence
STANCE_FIRM, STANCE_HELD = 0.6, 0.4  # §6.3.1: confidence at or above these reads "firm" / "held", below "tentative"


def _valence_mult(s: float) -> float:
    """§5.3 asymmetric map on the sign of the signed engagement sum `s`. Net-zero-or-positive keeps
    the +5%/point lift capped at +30%; net-negative drops steeply (one refute ×0.50, floor ×0.30)."""
    if s >= 0:
        return 1.0 + min(VALENCE_LIFT_CAP, VALENCE_LIFT * s)
    return 1.0 - min(VALENCE_DROP_CAP, VALENCE_REFUTE_DROP * -s)


@dataclass(frozen=True)
class Filters:
    """§5.1 filters plus `has_media` (plan steps), `exclude_kinds` (context() anchors, consolidate
    candidates: `(d.kind IS NULL OR d.kind NOT IN (...))`, so a plain row always passes) and
    `no_ttl` (`d.expires_at IS NULL`, §6.1/§6.2/§12.3 candidates). A `str` source is one source;
    test `isinstance(str)` before treating it as a sequence. `kind` accepts `"plain"` (§5.1)."""

    source: str | Sequence[str] | None = None
    exclude_sources: Sequence[str] | None = None
    tags: Sequence[str] | None = None
    any_tags: Sequence[str] | None = None
    exclude_tags: Sequence[str] | None = None
    kind: str | Sequence[str] | None = None
    exclude_kinds: Sequence[str] = ()
    since: TimeSpec | None = None
    until: TimeSpec | None = None
    include_expired: bool = False
    has_media: bool | None = None
    no_ttl: bool = False


@dataclass(frozen=True)
class Cand:
    """One candidate before scoring: the `deltas` row (DELTA_COLS), its relevance, and how it matched."""

    row: sqlite3.Row
    relevance: float
    matched: str


def filter_sql(lake: Lake, filters: Filters, now: datetime) -> tuple[str, list[object]]:
    """§5.1 WHERE fragment over alias `d` (`1` when empty) and its bound parameters. `now` is the
    call's single clock reading: expiry binds `dt_to_ts(now)`, `since`/`until` parse against it."""
    conds: list[str] = []
    params: list[object] = []

    def tag_in(tags: Sequence[str], select: str) -> str:
        params.extend(tags)
        return f"{select} FROM delta_tags t WHERE t.delta_id = d.id AND t.tag IN ({_db.qmarks(len(tags))})"

    if not filters.include_expired:
        conds.append(LIVE)
        params.append(_time.dt_to_ts(now))
    if isinstance(filters.source, str):
        conds.append("d.source = ?")
        params.append(filters.source)
    elif filters.source:
        conds.append(f"d.source IN ({_db.qmarks(len(filters.source))})")
        params.extend(filters.source)
    if filters.exclude_sources:
        conds.append(f"d.source NOT IN ({_db.qmarks(len(filters.exclude_sources))})")
        params.extend(filters.exclude_sources)
    if filters.tags:
        tags = list(dict.fromkeys(filters.tags))
        conds.append(f"({tag_in(tags, 'SELECT count(*)')}) = ?")
        params.append(len(tags))
    if filters.any_tags:
        conds.append(f"EXISTS ({tag_in(list(filters.any_tags), 'SELECT 1')})")
    if filters.exclude_tags:
        conds.append(f"NOT EXISTS ({tag_in(list(filters.exclude_tags), 'SELECT 1')})")
    kinds = [filters.kind] if isinstance(filters.kind, str) else list(filters.kind or ())
    if kinds:
        named = [k for k in kinds if k != "plain"]
        parts = [f"d.kind IN ({_db.qmarks(len(named))})"] if named else []
        if "plain" in kinds:
            parts.append("d.kind IS NULL")
        params.extend(named)
        conds.append(f"({' OR '.join(parts)})")
    if filters.exclude_kinds:
        conds.append(f"(d.kind IS NULL OR d.kind NOT IN ({_db.qmarks(len(filters.exclude_kinds))}))")
        params.extend(filters.exclude_kinds)
    for spec, op in ((filters.since, ">="), (filters.until, "<=")):
        if spec is not None:
            conds.append(f"d.timestamp {op} ?")
            params.append(_time.render_timespec(spec, now))
    if filters.has_media is not None:
        conds.append("d.media_hash IS NOT NULL" if filters.has_media else "d.media_hash IS NULL")
    if filters.no_ttl:
        conds.append("d.expires_at IS NULL")
    return " AND ".join(conds) or "1", params


def fts_tokens(query: str) -> list[str]:
    """§5.2 step 1: [^\\W_]+ tokens, lower-cased, >= 2 chars, not in STOPLIST, deduped, first 64."""
    toks = (t for t in TOKEN_RE.findall(query.lower()) if len(t) >= 2 and t not in STOPLIST)
    return list(dict.fromkeys(toks))[:64]


def fts_match(tokens: Sequence[str]) -> str:
    """Each token double-quoted, joined by ` OR ` (bound as a parameter, never interpolated)."""
    return " OR ".join(f'"{t}"' for t in tokens)


def is_noise(rules: NoiseRules, content: str, source: str, kind: str | None, media_hash: str | None) -> bool:
    """§5.4 hard rules N1–N5 with the three exemptions (non-null kind, media_hash, exempt source)."""
    if kind is not None or media_hash is not None or source in rules.exempt_sources:
        return False
    text = content.strip()
    if not text or len(text) < rules.drop_chars or text.lower() in rules.phrases:
        return True
    pieces = [p for p in SPLIT_RE.split(text) if p]
    if pieces and 5 * sum(1 for p in pieces if ID_PIECE_RE.match(p)) >= 4 * len(pieces):
        return True
    return payload_noise(text)


def payload_noise(text: str) -> bool:
    """N5: a JSON object/array carrying a hook key, or without any string value of 40+ characters."""
    if text[:1] not in "{[":
        return False
    try:
        obj = json.loads(text)
    except ValueError:
        return False
    if not isinstance(obj, (dict, list)):
        return False
    if isinstance(obj, dict) and not HOOK_KEYS.isdisjoint(obj):
        return True
    return not any(len(s) >= 40 for s in _strings(obj))


def _strings(obj: object) -> Iterator[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def fts_pool(lake: Lake, match: str, where: str, params: Sequence[object]) -> list[tuple[sqlite3.Row, float]]:
    """§5.2 step 1 SQL only: (DELTA_COLS row, bm = -bm25) for the FTS_POOL best matches of `match`
    under the `where` fragment; `match` is bound, never interpolated. Kept separate so
    `test_plan_search_limit` can monkeypatch it (rel_fts). CROSS JOIN pins the MATCH as the outer loop: with a kind
    filter and long tag lists the planner drove from deltas, one MATCH per row (27 s vs 0.17 s at 118k rows)."""
    sql = (
        f"SELECT {DCOLS}, -bm25(deltas_fts) AS bm FROM deltas_fts f CROSS JOIN deltas d ON d.seq = f.rowid"
        f" WHERE deltas_fts MATCH ? AND {where} ORDER BY bm DESC, d.seq DESC LIMIT {FTS_POOL}"
    )
    return [(row, float(row["bm"])) for row in lake.conn.execute(sql, [match, *params])]


def minmax(pairs: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    """§5.2 step 2 normalisation: (x - min) / (max - min) over the pairs; 1.0 for all when max == min."""
    if not pairs:
        return []
    lo, hi = min(v for _, v in pairs), max(v for _, v in pairs)
    return [(i, 1.0 if hi == lo else (v - lo) / (hi - lo)) for i, v in pairs]


def vector_pool(lake: Lake, query: str, where: str, params: Sequence[object]) -> tuple[list[tuple[str, float]], dict[str, float]]:
    """§5.2 step 2 before the floor: ((id, rel_vec) for the FTS_POOL best filtered rows by cosine, {id: rel_vec} for
    every filtered row with a vector). rel_vec = (cos - base) / (cos_max - base) clamped at 0, where base is the
    median cosine over every filtered row with a vector: the query's "unrelated" level. The 500 cap bounds only the
    rows the vector pass may add; an FTS hit ranked lower by cosine still fuses with its real rel_vec (step 3).
    Filters run in SQL first; only the surviving rows are dotted. Exceptions propagate to the caller."""
    cache = lake.vectors
    if cache is None:
        return [], {}
    q = _vectors.normalise(embed_query(lake, query))
    if q is None:
        return [], {}
    sql = f"SELECT d.id FROM deltas d JOIN vectors v ON v.delta_id = d.id WHERE {where}"
    pairs = cache.cosines(lake, q, [r[0] for r in lake.conn.execute(sql, params)])
    top = _vectors.top_k(pairs, FTS_POOL)
    if not top:
        return [], {i: 0.0 for i, _ in pairs}
    base, hi = statistics.median(c for _, c in pairs), top[0][1]
    rel = {i: 1.0 if hi <= base else max(0.0, (c - base) / (hi - base)) for i, c in pairs}
    return [(i, rel[i]) for i, _ in top], rel


def embed_query(lake: Lake, query: str) -> list[float]:
    """§5.2: the query's vector within QUERY_EMBED_WAIT; a timeout raises, so candidates() falls back to FTS only."""
    embed = lake.embed  # call_embed's guards, checked here: the embed itself runs on a daemon thread
    if embed is None or lake.conn.in_transaction:
        raise LakeError("embed needs an embed callback and no open transaction")
    out: list[list[float] | Exception] = []

    def run() -> None:
        try:
            out.append(embed([query])[0])
        except Exception as exc:
            out.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(QUERY_EMBED_WAIT)
    if not out:
        raise TimeoutError(f"query embed took over {QUERY_EMBED_WAIT:g}s")
    if isinstance(out[0], Exception):
        raise out[0]
    return out[0]


def rows_by_id(lake: Lake, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
    """DELTA_COLS rows for many ids (no expiry filter; the caller's candidate SQL already applied it)."""
    out: dict[str, sqlite3.Row] = {}
    for chunk in _db.chunks(ids):
        sql = f"SELECT {DCOLS} FROM deltas d WHERE d.id IN ({_db.qmarks(len(chunk))})"
        out.update((r["id"], r) for r in lake.conn.execute(sql, chunk))
    return out


def candidates(
    lake: Lake, query: str | None, filters: Filters, *, now: datetime, noise: bool = True,
    min_relevance: float = 0.0, limit: int | None = None, warnings: list[str] | None = None
) -> list[Cand]:
    """§5.2: FTS pool ∪ vector pool (embed failure -> warning, FTS only), floor, hard noise rules.
    With query=None every filtered row, newest first, relevance 1.0, matched "filter"; the order is
    fixed (§12.5), so `limit` may be applied in SQL on that path (ignored when a query is present).
    `lake.call_embed` is the only embed entry: re-raise `LakeError` from it (the transaction guard),
    turn any other exception into the §5.2 warning."""
    where, params = filter_sql(lake, filters, now)
    if query is None:
        sql = f"SELECT {DCOLS} FROM deltas d WHERE {where} ORDER BY d.timestamp DESC, d.seq DESC"
        sql += f" LIMIT {int(limit)}" if limit is not None else ""
        return [Cand(row, 1.0, "filter") for row in lake.conn.execute(sql, params)]
    pool: dict[str, Cand] = {}
    tokens = fts_tokens(query)
    fts: dict[str, tuple[sqlite3.Row, float]] = {}
    if tokens:
        found = fts_pool(lake, fts_match(tokens), where, params)
        top = max((bm for _, bm in found), default=0.0)
        fts = {row["id"]: (row, bm / top if top > 0 else 1.0) for row, bm in found}
    vec: dict[str, float] = {}
    rel_all: dict[str, float] = {}  # rel_vec of every filtered row with a vector, pool or not
    if lake.embed is not None:
        try:
            ranked, rel_all = vector_pool(lake, query, where, params)
            vec = dict(ranked)
        except (LakeError, sqlite3.Error):  # the transaction guard and §12.9 both propagate
            raise
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"embed failed: {exc}; FTS only")
    for i, (row, rel_fts) in fts.items():  # step 3: fuse; a row without a vector keeps rel_fts alone
        if i in rel_all:
            rel_vec = rel_all[i]
            pool[i] = Cand(row, FUSE_FTS * rel_fts + (1 - FUSE_FTS) * rel_vec, "both" if rel_vec >= VEC_FLOOR else "fts")
        else:
            pool[i] = Cand(row, rel_fts, "fts")
    weight = 1 - FUSE_FTS if fts else 1.0  # no FTS pass (no token survived): rel_vec alone
    rows = rows_by_id(lake, [i for i, rel in vec.items() if i not in pool and rel >= VEC_FLOOR])
    for i, row in rows.items():
        pool[i] = Cand(row, weight * vec[i], "vector")
    return [
        c for c in pool.values()
        if c.relevance >= min_relevance
        and not (noise and is_noise(lake.noise, c.row["content"], c.row["source"], c.row["kind"], c.row["media_hash"]))
    ]


def valences(lake: Lake, ids: Sequence[str], now: datetime) -> dict[str, float]:
    """§5.3 valence per id from one grouped query over live engagement rows (1.0 when none)."""
    sums: dict[str, float] = {}
    for chunk in _db.chunks(ids):
        sql = (
            "SELECT e.target_id, e.kind, count(*) FROM engagements e JOIN deltas d ON d.id = e.delta_id"
            f" WHERE e.target_id IN ({_db.qmarks(len(chunk))}) AND {LIVE} GROUP BY e.target_id, e.kind"
        )
        for tid, kind, n in lake.conn.execute(sql, [*chunk, _time.dt_to_ts(now)]):
            sums[tid] = sums.get(tid, 0.0) + VALENCE[kind] * n
    return {i: _valence_mult(s) for i, s in sums.items()}


def recency_factor(lake: Lake, age_s: float) -> float:
    """§5.3 recency for an age in seconds: RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (age / half-life)."""
    return float(RECENCY_FLOOR + (1 - RECENCY_FLOOR) * 0.5 ** (max(0.0, age_s) / lake.half_life_s))


def is_question(text: str) -> bool:
    """§5.3: a row that only asks (ends with '?' and holds no earlier sentence), so it answers nothing."""
    text = text.strip()
    return text.endswith("?") and not SENTENCE_RE.search(text[:-1])


def _exempt(rules: NoiseRules, row: sqlite3.Row) -> bool:
    return row["kind"] is not None or row["media_hash"] is not None or row["source"] in rules.exempt_sources


def _boost(row: sqlite3.Row) -> float:
    if row["kind"] == "container":
        raw = row["meta"]
        if raw and '"fallback"' in raw and json.loads(raw).get("fallback"):
            return 1.0  # §5.3: an extractive session container quotes its rows and earns no summary boost
        return 1 / 0.85 if row["level"] >= 1 else 1 / 0.92
    if row["kind"] == "sediment":
        raw = row["meta"]
        grounding = json.loads(raw).get("grounding") if raw else None
        return 1 / 0.92 if grounding == "external" else 1.0
    return 1.0


def score(
    lake: Lake, cands: Sequence[Cand], *, now: datetime, limit: int, recency: bool = True, noise: bool = True,
    query_present: bool = True, step: str | None = None
) -> list[Hit]:
    """§5.3 score, sort (6-decimal score DESC, timestamp DESC, id ASC) or the no-query order (§12.5),
    cut at limit, then batch-load tags/edges/engagement for the survivors."""
    vals = valences(lake, [c.row["id"] for c in cands], now)
    links = _db.supersessions(lake.conn, _time.dt_to_ts(now))  # §5.3: {old id: live links}, {} (one EXISTS) if none
    dep = _db.dep_corrected_set(lake.conn, _time.dt_to_ts(now), links)  # summaries resting on a corrected premise
    evidence = _db.evidence_times(lake.conn, [c.row["id"] for c in cands if c.row["kind"] in _db.CONSOLIDATED])
    scored: list[tuple[float, float, float, float, Cand]] = []
    for c in cands:
        row = c.row
        age = (now - _time.ts_to_dt(row["timestamp"])).total_seconds()
        seen = min(row["timestamp"], evidence.get(row["id"], row["timestamp"]))  # a summary is as old as its evidence
        rec = recency_factor(lake, (now - _time.ts_to_dt(seen)).total_seconds())
        val = vals.get(row["id"], 1.0)
        s = c.relevance * (rec if recency else 1.0) * val * (SUPERSEDE_DROP if row["id"] in links else 1.0)
        if query_present:
            soft = noise and not _exempt(lake.noise, row) and 0 < len(row["content"].strip()) < lake.noise.soft_chars
            boost = 1.0 if row["id"] in dep else _boost(row)  # a refuted premise suspends the boost, so the
            s *= (1 / 1.20 if soft else 1.0) * boost           # summary no longer outranks the rows beneath it
            s /= QUESTION_DROP if row["kind"] is None and is_question(row["content"]) else 1.0
        scored.append((s, rec, val, age, c))
    depth: dict[str, int] = {}
    if query_present:  # §5.3: an affirm or reply row never outranks its own target when both are candidates,
        raw = {t[4].row["id"]: t[0] for t in scored}  # and a superseded row never outranks its newest superseder
        targets = _db.agreeing_targets(lake.conn, [c.row["id"] for c in cands if c.row["kind"] == "engagement"])
        for old, group in links.items():
            if old in raw and (newest := next((x.receipt.id for x in group if x.receipt.id in raw), None)) is not None:
                targets[old] = newest
        stances = [i for t in scored if t[4].row["source"] == "lake:stance" and (i := t[4].row["id"]) not in targets]
        for sid, ev in (_db.load_sides(lake.conn, stances)[1] if stances else {}).items():  # and a stance never
            if (best := max((e for e in ev if e in raw), key=raw.__getitem__, default=None)) is not None:  # outranks
                targets[sid] = best  # the best-scoring row it rests on (§6.3.1)
        capped: dict[str, float] = {}

        def cap(i: str, seen: frozenset[str] = frozenset()) -> float:
            """Score of `i` after its target's own cap (a chain resolves root first); depth counts the caps."""
            if i not in capped:
                tid = targets.get(i)
                s = raw[i]
                if tid is not None and tid in raw and tid not in seen and s >= (top := cap(tid, seen | {i})):
                    s, depth[i] = top, depth.get(tid, 0) + 1
                capped[i] = s
            return capped[i]

        scored = [(cap(t[4].row["id"]), *t[1:]) for t in scored]
        scored.sort(key=lambda t: (-round(t[0], 6), depth.get(t[4].row["id"], 0), t[3], t[4].row["id"]))
    else:
        scored.sort(key=lambda t: (t[4].row["timestamp"], t[4].row["seq"]), reverse=True)
    kept = scored[:limit]
    deltas = _db.rows_to_deltas(lake.conn, [t[4].row for t in kept], now_ts=_time.dt_to_ts(now), links=links)
    return [
        Hit(delta=d, score=s, relevance=c.relevance, recency=rec, valence=val, matched=c.matched, step=step)
        for d, (s, rec, val, _, c) in zip(deltas, kept, strict=True)
    ]


def search(
    lake: Lake, query: str | None, filters: Filters, *, limit: int, now: datetime | None = None, noise: bool = True,
    recency: bool = True, min_relevance: float = 0.0, step: str | None = None, warnings: list[str] | None = None
) -> list[Hit]:
    """candidates() + score(): the core shared by recall(), the plan `search`/`filter` steps, and context().
    `now` defaults to one `lake.now()` reading shared by both halves."""
    now = lake.now() if now is None else now
    cands = candidates(lake, query, filters, now=now, noise=noise, min_relevance=min_relevance,
                       limit=limit if query is None else None, warnings=warnings)
    return score(lake, cands, now=now, limit=limit, recency=recency, noise=noise, query_present=query is not None, step=step)


def recall(
    lake: Lake,
    query: str | None = None,
    *,
    source: str | Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    any_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    kind: str | Sequence[str] | None = None,
    since: TimeSpec | None = None,
    until: TimeSpec | None = None,
    limit: int = 20,
    exclude_sources: Sequence[str] | None = None,
    include_expired: bool = False,
    noise: bool = True,
    recency: bool = True,
    min_relevance: float = 0.0,
) -> list[Hit]:
    """§4.5 recall without a plan: validate (limit >= 1), build Filters, search, set lake.last_warnings."""
    if limit < 1:
        raise ValueError(f"limit must be at least 1, not {limit}")
    bad = set([kind] if isinstance(kind, str) else kind or ()) - KINDS
    if bad:
        raise ValueError(f"unknown kind: {', '.join(sorted(bad))}")
    filters = Filters(
        source=source, exclude_sources=exclude_sources, tags=tags, any_tags=any_tags, exclude_tags=exclude_tags,
        kind=kind, since=since, until=until, include_expired=include_expired,
    )
    warnings: list[str] = []
    hits = search(lake, query, filters, limit=limit, now=lake.now(), noise=noise, recency=recency,
                  min_relevance=min_relevance, warnings=warnings)
    lake.last_warnings = warnings
    return hits


def oneline(text: str, cap: int = 130) -> str:
    """§5.6.2 oneline: strip, drop HTML tags/entities when tags are present, collapse whitespace,
    cut to cap-1 + '…'. Shared by _context, _consolidate (prompt lines), and the CLI."""
    text = text.strip()
    if TAG_RE.search(text):
        text = ENTITY_RE.sub(" ", TAG_RE.sub("", text))
    text = WS_RE.sub(" ", text).strip()
    return text if len(text) <= cap else text[: cap - 1] + "…"


def window(text: str, tokens: Sequence[str], cap: int) -> str:
    """§5.6.2 an anchor's line: oneline() up to `cap`, or the cap-long stretch holding the most query-token hits
    (starting a third of a cap before the first hit of the best cluster), with '…' where it was cut."""
    text = oneline(text, len(text) + 1)
    if len(text) <= cap or not tokens:
        return oneline(text, cap)
    low = text.lower()
    hits = sorted(m.start() for t in tokens for m in re.finditer(re.escape(t), low))
    starts = [max(0, min(h - cap // 3, len(text) - cap)) for h in hits] or [0]
    s = max(starts, key=lambda a: (sum(a <= h < a + cap for h in hits), -a))
    return ("…" if s else "") + text[s + (1 if s else 0): s + cap - 1] + ("…" if s + cap - 1 < len(text) else "")


@dataclass(frozen=True)
class Stance:
    """§6.3.1 one live stance (the newest non-retired row of its slug) with its read-time confidence."""

    id: str
    slug: str
    position: str
    since: str  # timestamp of the oldest row of the unbroken run of this position in the slug's chain
    value: float
    label: str  # firm | held | tentative | contested
    support: int  # distinct sessions of outside evidence for it
    against: int  # the same over meta.stance.against


def evidence_sessions(lake: Lake, ids: Sequence[str], now_ts: str, links: Mapping[str, object]) -> set[str]:
    """§6.3.1: the distinct sessions (`session:` tag, else source + day) of the outside-evidence rows (§6.5: not
    `lake:*`, no `assistant` tag) among `ids`, a cited container counting through the rows it rests on; rows that are
    expired, live-refuted or superseded (a key of `links`) count for nothing."""
    out: set[str] = set()
    seen: set[str] = set()
    frontier = list(ids)
    while frontier:
        found = _db.load_map(lake.conn, [i for i in dict.fromkeys(frontier) if i not in seen])
        seen.update(frontier)
        refuted, frontier = _db.load_refutations(lake.conn, list(found), now_ts), []
        for d in found.values():
            if d.id in refuted or d.id in links or (d.expires_at is not None and d.expires_at <= now_ts):
                continue
            if d.kind == "container":
                frontier += d.derived_from
            elif not d.source.startswith("lake:") and "assistant" not in d.tags:
                out.add(next((t for t in d.tags if t.startswith("session:")), f"{d.source} {d.timestamp[:10]}"))
    return out


def stances(lake: Lake, now: datetime, only: Collection[str] | None = None) -> list[Stance]:
    """§6.3.1 live stances, most confident first (slug breaks ties); `only` limits the work to those stance ids.
    Confidence is derived at read time: support / (support + against + 1) × min(valence, 1), counted over the rows of
    the unbroken run of the head's position (a revise that keeps the position adds its cites); a live affirm of the
    stance row with an engager (`by`) or a `user` tag is support too. [] (one meta lookup) in a lake that never held a stance."""
    now_ts = _time.dt_to_ts(now)
    chains = _db.stance_chains(lake.conn, now_ts)
    heads = {c[0][0]: (slug, c) for slug, c in chains.items() if not c[0][3].get("retired") and (only is None or c[0][0] in only)}
    if not heads:
        return []
    runs = {sid: next((c[:k] for k in range(1, len(c)) if c[k][2] != c[0][2] or c[k][3].get("retired")), c)
            for sid, (_, c) in heads.items()}  # the unbroken run of the head's position, newest first
    links, rows = _db.supersessions(lake.conn, now_ts), _db.load_map(lake.conn, [x[0] for r in runs.values() for x in r])
    vals, refuted = valences(lake, list(heads), now), _db.load_refutations(lake.conn, list(heads), now_ts)
    sql = ("SELECT e.target_id, e.delta_id FROM engagements e WHERE e.kind = 'affirm' AND (e.engaged_by IS NOT NULL OR"
           " EXISTS (SELECT 1 FROM delta_tags t WHERE t.delta_id = e.delta_id AND t.tag = 'user'))"  # a user's, not
           f" AND e.target_id IN ({_db.qmarks(len(heads))})")  # an anonymous (e.g. the assistant's MCP) affirm
    affirms: dict[str, list[str]] = {}
    for tid, eid in lake.conn.execute(sql, list(heads)):
        affirms.setdefault(tid, []).append(eid)
    out: list[Stance] = []
    for sid, (slug, chain) in heads.items():
        if sid not in rows:
            continue
        cites = [i for x in runs[sid] if x[0] in rows for i in rows[x[0]].derived_from]  # a same-position revise adds
        sup = len(evidence_sessions(lake, [*cites, *affirms.get(sid, ())], now_ts, links))
        against = len(evidence_sessions(lake, [str(i) for x in runs[sid] for i in x[3].get("against") or ()], now_ts, links))
        value = round(sup / (sup + against + 1) * min(vals.get(sid, 1.0), 1.0), 4)
        label = ("contested" if sid in refuted or (against and against >= sup) else "firm" if value >= STANCE_FIRM
                 else "held" if value >= STANCE_HELD else "tentative")
        out.append(Stance(sid, slug, chain[0][2], runs[sid][-1][1], value, label, sup, against))
    return sorted(out, key=lambda x: (-x.value, x.slug))

