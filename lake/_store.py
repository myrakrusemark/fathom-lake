"""Row writes and reads: write, get, engage, lineage, cited_by, host meta (SPEC §4.4–§4.6)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from . import _db, _time, _vectors
from ._types import ClosedLoopError, Delta, Duration, EmbedError, Engagement, Lineage, NotFoundError, TimeSpec

if TYPE_CHECKING:
    from .lake import Lake

ID_RE = re.compile(r"^[0-9a-f]{8,12}$")
MEDIA_RE = re.compile(r"^[0-9a-f]{16,64}$")
WRITE_KINDS = frozenset({"container", "mood", "crystal", "sediment"})
ENGAGE_KINDS: MappingProxyType[str, str] = MappingProxyType(
    {"affirm": "affirm", "affirms": "affirm", "refute": "refute", "refutes": "refute", "reply": "reply", "reply-to": "reply"}
)
SNAPSHOT_CHARS = 4000
LIVE = "(d.expires_at IS NULL OR d.expires_at > ?)"


def write(
    lake: Lake, content: str, source: str, *, tags: Sequence[str] | None = None, kind: str | None = None,
    derived_from: Sequence[str] | None = None, expires: Duration | TimeSpec | None = None, media: str | None = None,
    meta: dict[str, Any] | None = None, timestamp: TimeSpec | None = None, dedupe: bool = True,
    embed: bool | None = None
) -> Delta:
    """§4.4 steps 1–8: validate, normalise, closed loop, level, dedupe, insert, embed after commit."""
    lake.require_writable()
    now = lake.now()
    if not content.strip() or "\x00" in content:
        raise ValueError("content must be non-empty and NUL-free")
    check_source(source)
    if kind is not None and kind not in WRITE_KINDS:
        raise ValueError(f"kind must be None or one of {sorted(WRITE_KINDS)}, not {kind!r}")
    if media is not None and not MEDIA_RE.match(media):
        raise ValueError(f"media must be 16 to 64 lowercase hex characters, not {media!r}")
    meta_json = dump_meta(meta)
    ts = _time.dt_to_ts(now) if timestamp is None else _time.render_timespec(timestamp, now)
    expires_at = None
    if expires is not None:
        exp = _time.parse_expires(expires, now)
        if exp <= now:
            raise ValueError(f"expires must be after now: {expires!r}")
        expires_at = _time.dt_to_ts(exp)
    tag_list, parents = check_tags(normalise_tags(tags)), normalise_tags(derived_from)
    if kind is not None and not parents:
        raise ClosedLoopError(f"kind={kind!r} needs a non-empty derived_from")
    with lake.tx() as conn:
        level = parent_level(conn, kind, parents)
        if dedupe and media is None:
            prior = prior_row(lake, conn, source, tag_key(tag_list), now)
            if prior is not None and prior["content"] == content:
                return refresh_ttl(conn, _db.rows_to_deltas(conn, [prior])[0], expires_at)
        delta_id = _db.new_id(conn)
        insert_row(
            conn, delta_id=delta_id, timestamp=ts, content=content, source=source, kind=kind, level=level,
            tags=tag_list, derived_from=parents, expires_at=expires_at, media_hash=media, meta=meta,
        )
        delta = Delta(
            id=delta_id, timestamp=ts, content=content, source=source, kind=kind, level=level, tags=tag_list,
            derived_from=parents, expires_at=expires_at, media_hash=media,
            meta=None if meta_json is None else json.loads(meta_json), engagement=None,
        )
    if lake.embed is not None and (lake.embed_on_write if embed is None else embed):
        embed_and_store(lake, delta)
    return delta


def get(lake: Lake, delta_id: str, *, include_expired: bool = False) -> Delta | None:
    """§4.5 get: exact id or a unique prefix of 8–12 hex chars; expired rows hidden unless asked."""
    if not ID_RE.match(delta_id):
        return None
    where, arg = ("d.id = ?", delta_id) if len(delta_id) == 12 else ("d.id LIKE ?", delta_id + "%")
    sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE {where} LIMIT 2"
    rows: list[sqlite3.Row] = lake.conn.execute(sql, (arg,)).fetchall()
    if len(rows) != 1 or (not include_expired and expired(rows[0]["expires_at"], lake.now_str())):
        return None
    return _db.rows_to_deltas(lake.conn, rows, now_ts=lake.now_str())[0]


def resolve(lake: Lake, delta_id: str, *, include_expired: bool = False) -> Delta:
    """get() that raises NotFoundError instead of returning None (engage, lineage, cited_by, inputs=)."""
    delta = get(lake, delta_id, include_expired=include_expired)
    if delta is None:
        raise NotFoundError(f"no live delta matches {delta_id!r}")
    return delta


def newest(lake: Lake, kind: str) -> Delta | None:
    """The newest live row of `kind` by ORDER BY timestamp DESC, seq DESC LIMIT 1 (§4.7 crystal(),
    context() block 1, stats()); SQL, not a scored recall."""
    sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE d.kind = ? AND {LIVE} ORDER BY d.timestamp DESC, d.seq DESC LIMIT 1"
    rows: list[sqlite3.Row] = lake.conn.execute(sql, (kind, lake.now_str())).fetchall()
    return _db.rows_to_deltas(lake.conn, rows)[0] if rows else None


def newest_n(lake: Lake, kind: str, n: int) -> list[Delta]:
    """The newest `n` live rows of `kind`, newest first (timestamp DESC, seq DESC); [] for n < 1.
    The LIMIT-n sibling of newest() used by system_prompt() (§5.7). Guards n < 1 before the query
    because SQLite treats LIMIT -1 (any negative) as unlimited."""
    if n < 1:
        return []
    sql = f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE d.kind = ? AND {LIVE} ORDER BY d.timestamp DESC, d.seq DESC LIMIT ?"
    rows: list[sqlite3.Row] = lake.conn.execute(sql, (kind, lake.now_str(), n)).fetchall()
    return _db.rows_to_deltas(lake.conn, rows)


def engage(
    lake: Lake, delta_id: str, kind: str, *, by: str | None = None, note: str | None = None,
    tags: Sequence[str] | None = None, snapshot: bool = True, dedupe: bool = True
) -> Delta:
    """§4.6 steps 1–6: resolve target, build the snapshot, dedupe, insert row + engagements, embed."""
    lake.require_writable()
    now = lake.now()
    ekind = ENGAGE_KINDS.get(kind)
    if ekind is None:
        raise ValueError(f"engagement kind must be affirm, refute, or reply, not {kind!r}")
    source = by if by else "engagement"
    check_source(source)
    tag_list = check_tags(normalise_tags(tags))
    target = resolve(lake, delta_id)
    quoted, footer = snapshot_block(target)
    block = f"{quoted}\n\n{note}".strip() if note else quoted
    content, meta = (block, None) if snapshot else ((note.strip() if note else "") or footer, {"snapshot": quoted})
    with lake.tx() as conn:
        if dedupe:
            prior = prior_row(lake, conn, source, tag_key(tag_list), now, target=target.id, kind=ekind)
            if prior is not None and prior["content"] == content:
                return _db.rows_to_deltas(conn, [prior])[0]
        new_id, ts = _db.new_id(conn), _time.dt_to_ts(now)
        insert_row(
            conn, delta_id=new_id, timestamp=ts, content=content, source=source, kind="engagement", level=0,
            tags=tag_list, derived_from=[target.id], expires_at=None, media_hash=target.media_hash, meta=meta,
        )
        conn.execute(
            "INSERT INTO engagements(delta_id, target_id, kind, engaged_by, note) VALUES (?, ?, ?, ?, ?)",
            (new_id, target.id, ekind, by, note),
        )
        delta = Delta(
            id=new_id, timestamp=ts, content=content, source=source, kind="engagement", level=0, tags=tag_list,
            derived_from=[target.id], expires_at=None, media_hash=target.media_hash, meta=meta,
            engagement=Engagement(target.id, ekind, by, note),
        )
    if lake.embed is not None and lake.embed_on_write:
        embed_and_store(lake, delta)
    return delta


def snapshot_block(target: Delta) -> tuple[str, str]:
    """§4.6 step 2: (the quoted target text with its footer line, the footer line alone)."""
    footer = f"> — {target.source} · {target.timestamp[:16]} · {target.id[:8]}" + (" · [image]" if target.media_hash else "")
    text = target.content.strip()
    if not text:
        return footer, footer
    lines = [f"> {line}" if line else ">" for line in text[:SNAPSHOT_CHARS].split("\n")]
    if len(text) > SNAPSHOT_CHARS:
        lines[-1] += "…"
    return "\n".join([*lines, footer]), footer


def lineage(lake: Lake, delta_id: str, *, depth: int | None = None, include_expired: bool = True) -> Lineage:
    """§4.5 lineage: breadth-first over the row's own derived_from, nearest first, dangling collected."""
    root = resolve(lake, delta_id, include_expired=include_expired)
    now, seen = lake.now_str(), {root.id}
    rows: list[Delta] = []
    dangling: list[str] = []
    frontier, hops = root.derived_from, 0
    while frontier and (depth is None or hops < depth):
        wanted = [pid for pid in dict.fromkeys(frontier) if pid not in seen]
        seen.update(wanted)
        found = _db.load_map(lake.conn, wanted)
        frontier, hops = [], hops + 1
        for pid in wanted:
            parent = found.get(pid)
            if parent is None or (not include_expired and expired(parent.expires_at, now)):
                dangling.append(pid)
            else:
                rows.append(parent)
                frontier.extend(parent.derived_from)
    return Lineage(rows, dangling)


def cited_by(lake: Lake, delta_id: str, *, depth: int = 1, include_expired: bool = False) -> list[Delta]:
    """§4.5 cited_by: breadth-first over reverse derived_from edges, by hop then (timestamp DESC, seq DESC)."""
    if depth < 1:
        raise ValueError(f"depth must be at least 1, not {depth}")
    root = resolve(lake, delta_id, include_expired=include_expired)
    now, seen = lake.now_str(), {root.id}
    out: list[Delta] = []
    frontier = [root.id]
    live, extra = ("1", []) if include_expired else (LIVE, [now])
    for _ in range(depth):
        if not frontier:
            break
        hop: dict[str, sqlite3.Row] = {}
        for chunk in _db.chunks(frontier):
            sql = (
                f"SELECT {_db.DELTA_COLS} FROM deltas d JOIN derived_from e ON e.delta_id = d.id"
                f" WHERE e.parent_id IN ({_db.qmarks(len(chunk))}) AND {live}"
            )
            for row in lake.conn.execute(sql, [*chunk, *extra]):
                if row["id"] not in seen:
                    hop[row["id"]] = row
        ordered = sorted(hop.values(), key=lambda r: (r["timestamp"], r["seq"]), reverse=True)
        seen.update(hop)
        out.extend(_db.rows_to_deltas(lake.conn, ordered))
        frontier = [r["id"] for r in ordered]
    return out


def meta_get(lake: Lake, key: str) -> str | None:
    """§4.5 host meta read (the `host:` prefix check is done by the facade)."""
    return _db.meta_get(lake.conn, key)


def meta_set(lake: Lake, key: str, value: str | None) -> None:
    """§4.5 host meta write in one transaction; None deletes; requires a writable Lake."""
    lake.require_writable()
    with lake.tx() as conn:
        _db.meta_set(conn, key, value)


def normalise_tags(tags: Sequence[str] | None) -> list[str]:
    """§4.4 step 2: strip, drop empties, dedupe keeping the first occurrence (also used for derived_from)."""
    return [] if not tags else list(dict.fromkeys(t for t in (tag.strip() for tag in tags) if t))


def tag_key(tags: Sequence[str]) -> str:
    """§3.2 tag_key: normalised tags sorted by code point joined by '\\n', '' for none."""
    return "\n".join(sorted(tags))


def check_tags(tags: list[str]) -> list[str]:
    """Normalised tags must be NUL-free (§3.2); returns them for chaining."""
    if any("\x00" in t for t in tags):
        raise ValueError("tags must be NUL-free")
    return tags


def check_source(source: str) -> None:
    """§4.4 step 1: non-empty, NUL-free, and never the `lake:` prefix (ClosedLoopError)."""
    if not source.strip() or "\x00" in source:
        raise ValueError("source must be non-empty and NUL-free")
    if source.startswith("lake:"):
        raise ClosedLoopError(f"source {source!r} is reserved for consolidate()")


def dump_meta(meta: dict[str, Any] | None) -> str | None:
    """JSON text for deltas.meta: keys in the order given, finite numbers only (ValueError otherwise)."""
    if meta is None:
        return None
    try:
        return json.dumps(meta, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"meta is not finite JSON: {exc}") from exc


def expired(expires_at: str | None, now: str) -> bool:
    """§3.2: a row is invisible from the instant expires_at <= now (string comparison)."""
    return expires_at is not None and expires_at <= now


def parent_level(conn: sqlite3.Connection, kind: str | None, parents: Sequence[str]) -> int:
    """§4.4 steps 3–4: every parent must exist (NotFoundError); a container is min(3, 1 + max parent level)."""
    levels: dict[str, int] = {}
    for chunk in _db.chunks(parents):
        levels.update(conn.execute(f"SELECT id, level FROM deltas WHERE id IN ({_db.qmarks(len(chunk))})", chunk))
    missing = [p for p in parents if p not in levels]
    if missing:
        raise NotFoundError(f"derived_from ids not in this lake: {', '.join(missing)}")
    return min(3, 1 + max(levels.values())) if kind == "container" else 0


def prior_row(
    lake: Lake, conn: sqlite3.Connection, source: str, key: str, now: datetime, *, target: str | None = None,
    kind: str | None = None,
) -> sqlite3.Row | None:
    """§4.4 step 5 / §4.6 step 3 dedupe candidate: the newest live row of the scope, one idx_deltas_dedupe probe.
    Without `target` the scope excludes engagement rows; with it, engagements of that target and kind."""
    scope = "(d.kind IS NULL OR d.kind != 'engagement')"
    params: list[object] = [source, key]
    if target is not None:
        scope = "d.kind = 'engagement' AND EXISTS (SELECT 1 FROM engagements e WHERE e.delta_id = d.id AND e.target_id = ? AND e.kind = ?)"
        params += [target, kind]
    floor = None if lake.dedupe_window_s is None else _time.dt_to_ts(now - timedelta(seconds=lake.dedupe_window_s))
    sql = (
        f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE d.source = ? AND d.tag_key = ? AND {scope} AND {LIVE}"
        " AND (? IS NULL OR d.timestamp >= ?) ORDER BY d.timestamp DESC, d.seq DESC LIMIT 1"
    )
    row: sqlite3.Row | None = conn.execute(sql, [*params, _time.dt_to_ts(now), floor, floor]).fetchone()
    return row


def refresh_ttl(conn: sqlite3.Connection, delta: Delta, expires_at: str | None) -> Delta:
    """§4.4 step 5 with §12.1: push a deduped row's existing expires_at later; a permanent row stays permanent."""
    if expires_at is None or delta.expires_at is None or delta.expires_at >= expires_at:
        return delta
    conn.execute("UPDATE deltas SET expires_at = ? WHERE id = ?", (expires_at, delta.id))
    return replace(delta, expires_at=expires_at)


def insert_row(
    conn: sqlite3.Connection, *, delta_id: str, timestamp: str, content: str, source: str, kind: str | None,
    level: int, tags: Sequence[str], derived_from: Sequence[str], expires_at: str | None, media_hash: str | None,
    meta: dict[str, Any] | None
) -> None:
    """Insert one row, its tags, and its edges inside the caller's transaction (no checks)."""
    conn.execute(
        "INSERT INTO deltas(id, timestamp, content, source, kind, level, tag_key, expires_at, media_hash, meta)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (delta_id, timestamp, content, source, kind, level, tag_key(tags), expires_at, media_hash, dump_meta(meta)),
    )
    conn.executemany("INSERT INTO delta_tags(delta_id, tag, pos) VALUES (?, ?, ?)", [(delta_id, t, i) for i, t in enumerate(tags)])
    conn.executemany(
        "INSERT INTO derived_from(delta_id, parent_id, pos) VALUES (?, ?, ?)", [(delta_id, p, i) for i, p in enumerate(derived_from)]
    )
    if source == "lake:container" and meta and "supersedes" in meta:  # §3.6: the supersessions() gate (_db.HAS_LINKS)
        _db.meta_set(conn, _db.HAS_LINKS, "1")
    if source == "lake:stance":  # §3.6: the stance gate (_db.HAS_STANCES)
        _db.meta_set(conn, _db.HAS_STANCES, "1")


def embed_and_store(lake: Lake, delta: Delta) -> None:
    """§4.4 step 7 / §4.6 step 5: lake.call_embed([content]) after the row's commit, then
    _vectors.store_vectors in its own lake.tx(); any failure raises EmbedError with .delta set."""
    try:
        vecs = lake.call_embed([delta.content])
        count = len(vecs)
    except Exception as exc:
        raise EmbedError(f"embed failed: {exc}", delta=delta) from exc
    if count != 1:
        raise EmbedError(f"embed returned {count} vectors for 1 text", delta=delta)
    with lake.tx() as conn:
        failed = _vectors.store_vectors(conn, [(delta.id, vecs[0])])[1]
    if failed:
        raise EmbedError("embed returned a vector of the wrong length or zero norm", delta=delta)
