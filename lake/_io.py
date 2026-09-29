"""sweep, stats, export, import_, embed_missing, media_path (SPEC §4.8, §3.5)."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import _db, _store, _time, _vectors
from ._types import Delta, EmbedError, LakeError

if TYPE_CHECKING:
    from .lake import Lake

ID_RE = re.compile(r"^[0-9a-f]{12}$")
MEDIA_STEM_RE = re.compile(r"^[0-9a-f]{16,64}$")
KINDS = frozenset({"container", "mood", "crystal", "sediment", "engagement"})
ENGAGE_KINDS = frozenset({"affirm", "refute", "reply"})
LIVE = "(d.expires_at IS NULL OR d.expires_at > ?)"
ORPHAN_AGE_S = 600.0
EMBED_FAILED_MAX = 1000
EXPORT_CHUNK = 1000


def sweep(lake: Lake) -> dict[str, int]:
    """§4.8 sweep: delete expired rows in one transaction (bump vectors_gen, prune embed_failed),
    then orphan media under the §3.5 rules. Returns {"deleted", "orphan_media"}."""
    lake.require_writable()
    now = lake.now()
    ts = _time.dt_to_ts(now)
    with lake.tx() as conn:
        vectored = conn.execute(
            "SELECT count(*) FROM vectors v JOIN deltas d ON d.id = v.delta_id WHERE d.expires_at IS NOT NULL AND d.expires_at <= ?",
            (ts,),
        ).fetchone()[0]
        deleted = conn.execute("DELETE FROM deltas WHERE expires_at IS NOT NULL AND expires_at <= ?", (ts,)).rowcount
        if vectored:
            _db.bump_vectors_gen(conn)
        failed = embed_failed(conn)
        alive = existing_ids(conn, failed)
        kept = [i for i in failed if i in alive]
        if len(kept) != len(failed):
            _db.meta_set(conn, "embed_failed", json.dumps(kept))
    return {"deleted": int(deleted), "orphan_media": sweep_media(lake, now)}


def sweep_media(lake: Lake, now: datetime) -> int:
    """§3.5: unlink `<hex stem>.*` files no row references whose mtime is over 10 minutes old."""
    if not lake.media_dir.is_dir():
        return 0
    files = [p for p in lake.media_dir.iterdir() if p.is_file() and MEDIA_STEM_RE.match(p.name.split(".")[0])]
    stems = [p.name.split(".")[0] for p in files]
    referenced: set[str] = set()
    for chunk in _db.chunks(stems):
        sql = f"SELECT media_hash FROM deltas WHERE media_hash IN ({_db.qmarks(len(chunk))})"
        referenced.update(r[0] for r in lake.conn.execute(sql, chunk))
    cutoff = now.timestamp() - ORPHAN_AGE_S
    removed = 0
    for path, stem in zip(files, stems, strict=True):
        try:
            if stem not in referenced and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:  # another process swept it first
            pass
    return removed


def existing_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _db.chunks(ids):
        found.update(r[0] for r in conn.execute(f"SELECT id FROM deltas WHERE id IN ({_db.qmarks(len(chunk))})", chunk))
    return found


def embed_failed(conn: sqlite3.Connection) -> list[str]:
    """The §3.6 `embed_failed` id list ([] when absent)."""
    raw = _db.meta_get(conn, "embed_failed")
    return [str(i) for i in json.loads(raw)] if raw else []


def stats(lake: Lake) -> dict[str, Any]:
    """§4.8 stats: the named counts; live rows only except where named."""
    conn, ts = lake.conn, lake.now_str()

    def one(sql: str, *params: object) -> Any:
        return conn.execute(sql, params).fetchone()[0]

    rows = int(one("SELECT count(*) FROM deltas"))
    live = int(one(f"SELECT count(*) FROM deltas d WHERE {LIVE}", ts))
    by_kind = {
        (k or "plain"): n
        for k, n in conn.execute(f"SELECT d.kind, count(*) FROM deltas d WHERE {LIVE} GROUP BY d.kind ORDER BY 2 DESC, 1", (ts,))
    }
    by_source = dict(
        conn.execute(f"SELECT d.source, count(*) FROM deltas d WHERE {LIVE} GROUP BY d.source ORDER BY 2 DESC, 1 LIMIT 50", (ts,))
    )
    engagements = {"affirm": 0, "refute": 0, "reply": 0}
    sql = f"SELECT e.kind, count(*) FROM engagements e JOIN deltas d ON d.id = e.delta_id WHERE {LIVE} GROUP BY e.kind"
    engagements.update(conn.execute(sql, (ts,)))
    dim, drift = _db.meta_get(conn, "embed_dim"), _db.meta_get(conn, "crystal_drift")
    crystal = _store.newest(lake, "crystal")
    return {
        "rows": rows,
        "live_rows": live,
        "expired_unswept": rows - live,
        "by_kind": by_kind,
        "by_source": by_source,
        "tags": int(one(f"SELECT count(DISTINCT t.tag) FROM delta_tags t JOIN deltas d ON d.id = t.delta_id WHERE {LIVE}", ts)),
        "vectors": int(one(f"SELECT count(*) FROM vectors v JOIN deltas d ON d.id = v.delta_id WHERE {LIVE}", ts)),
        "embed_dim": None if dim is None else int(dim),
        "engagements": engagements,
        "last_consolidate": {k: _db.meta_get(conn, f"last_consolidate:{k}") for k in ("container", "mood", "crystal")},
        "crystal_id": None if crystal is None else crystal.id,
        "crystal_drift": None if drift is None else json.loads(drift),
        "digest": None if (cu := _db.meta_get(conn, "digest")) is None else json.loads(cu),  # §6.6 catch-up state
        "digest_last": None if (last := _db.meta_get(conn, "digest_last")) is None else json.loads(last),
        "file_bytes": lake.path.stat().st_size,
        "schema_version": _db.meta_get(conn, "schema_version"),
    }


def export(lake: Lake, path: str | os.PathLike[str], *, include_expired: bool = False, vectors: bool = False) -> int:
    """§4.8 export: JSONL ascending (timestamp, seq), one export_line() per row; returns the row count."""
    where, params = ("1", []) if include_expired else (LIVE, [lake.now_str()])
    cur = lake.conn.execute(f"SELECT {_db.DELTA_COLS} FROM deltas d WHERE {where} ORDER BY d.timestamp, d.seq", params)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        while chunk := cur.fetchmany(EXPORT_CHUNK):
            deltas = _db.rows_to_deltas(lake.conn, chunk)
            vecs = load_vectors(lake.conn, [d.id for d in deltas]) if vectors else {}
            fh.writelines(export_line(d, vecs.get(d.id)) for d in deltas)
            count += len(deltas)
    return count


def load_vectors(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for chunk in _db.chunks(ids):
        for did, blob in conn.execute(f"SELECT delta_id, vec FROM vectors WHERE delta_id IN ({_db.qmarks(len(chunk))})", chunk):
            out[did] = _vectors.unpack(blob)
    return out


def export_line(delta: Delta, vector: Sequence[float] | None = None) -> str:
    """§4.8 canonical JSON line (key order fixed, ensure_ascii=False, compact separators, allow_nan=False)."""
    e = delta.engagement
    obj: dict[str, Any] = {
        "id": delta.id,
        "timestamp": delta.timestamp,
        "content": delta.content,
        "source": delta.source,
        "kind": delta.kind,
        "level": delta.level,
        "tags": list(delta.tags),
        "derived_from": list(delta.derived_from),
        "engagement": None if e is None else {"target_id": e.target_id, "kind": e.kind, "by": e.by, "note": e.note},
        "expires_at": delta.expires_at,
        "media_hash": delta.media_hash,
        "meta": delta.meta,
    }
    if vector is not None:
        obj["vector"] = [float(x) for x in vector]
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


def import_(lake: Lake, path: str | os.PathLike[str], *, include_expired: bool = False) -> dict[str, int]:
    """§4.8 import_: import_lines() over the file; returns {"written", "skipped", "errors"}."""
    lake.require_writable()
    with open(path, encoding="utf-8", errors="surrogateescape") as fh:
        return import_lines(lake, fh, include_expired=include_expired)


def import_lines(lake: Lake, lines: Iterable[str], *, include_expired: bool = False) -> dict[str, int]:
    """import_'s per-line path: lake format, one transaction per line, watermark.seq advanced when any row was
    written. A line that is not lake-format JSONL is an error."""
    lake.require_writable()
    now = lake.now()
    ts = _time.dt_to_ts(now)
    counts = {"written": 0, "skipped": 0, "errors": 0}
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
            row = parse_line(obj, now)
            if not include_expired and row["expires_at"] is not None and row["expires_at"] <= ts:
                counts["skipped"] += 1
                continue
            with lake.tx() as conn:
                if conn.execute("SELECT 1 FROM deltas WHERE id = ?", (row["delta_id"],)).fetchone() is not None:
                    counts["skipped"] += 1
                    continue
                insert_line(conn, row)
            counts["written"] += 1
        except (ValueError, TypeError, sqlite3.IntegrityError):
            counts["errors"] += 1
    if counts["written"]:
        with lake.tx() as conn:
            raw = _db.meta_get(conn, "container_watermark")
            pos = json.loads(raw).get("pos") if raw else None
            _db.meta_set(conn, "container_watermark", json.dumps({"seq": _db.max_seq(conn), "pos": pos}, separators=(",", ":")))
    return counts


def str_list(value: object, what: str) -> list[str]:
    """A JSON list's string items (non-strings skipped, §7); None is empty; anything else is a bad line."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{what} must be a list")
    return [v for v in value if isinstance(v, str)]


def parse_line(obj: dict[str, Any], now: datetime) -> dict[str, Any]:
    """§4.8 lake-format checks and normalisation; returns insert_line() fields; ValueError on a bad line."""
    delta_id, ts, source = obj.get("id"), obj.get("timestamp"), obj.get("source")
    if not isinstance(delta_id, str) or not ID_RE.match(delta_id):
        raise ValueError(f"bad id {delta_id!r}")
    if not isinstance(ts, str) or _time.is_relative(ts):  # §12.8
        raise ValueError(f"timestamp must be absolute: {ts!r}")
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise ValueError("source must be a non-empty string")
    content = obj.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    kind, level = obj.get("kind"), obj.get("level")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"bad kind {kind!r}")
    if level is None:
        level = 0
    if isinstance(level, bool) or not isinstance(level, int):
        raise ValueError("level must be an integer")
    tags = _store.check_tags(_store.normalise_tags(str_list(obj.get("tags"), "tags")))
    parents = _store.normalise_tags(str_list(obj.get("derived_from"), "derived_from"))
    if source.startswith("lake:") and not parents:
        raise ValueError("a lake: source needs a non-empty derived_from")
    engagement = None
    eng = obj.get("engagement")
    if kind == "engagement" and eng is not None:
        if not isinstance(eng, dict):
            raise ValueError("engagement must be an object")
        target, ekind, by, note = eng.get("target_id"), eng.get("kind"), eng.get("by"), eng.get("note")
        if not isinstance(target, str) or not target or ekind not in ENGAGE_KINDS:
            raise ValueError("engagement needs a target_id and a kind of affirm, refute, or reply")
        if not (by is None or isinstance(by, str)) or not (note is None or isinstance(note, str)):
            raise ValueError("engagement by and note must be strings or null")
        engagement = (target, ekind, by, note)
    expires, media, meta, vec = obj.get("expires_at"), obj.get("media_hash"), obj.get("meta"), obj.get("vector")
    if expires is not None and (not isinstance(expires, str) or _time.is_relative(expires)):
        raise ValueError(f"expires_at must be absolute: {expires!r}")
    if media is not None and not isinstance(media, str):
        raise ValueError("media_hash must be a string or null")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("meta must be an object or null")
    _store.dump_meta(meta)  # finite numbers only
    vector = None
    if isinstance(vec, list) and vec and all(isinstance(x, (int, float)) and math.isfinite(x) for x in vec):
        vector = [float(x) for x in vec]
    return {
        "delta_id": delta_id,
        "timestamp": _time.render_timespec(ts, now),
        "content": content.replace("\x00", ""),
        "source": source,
        "kind": kind,
        "level": min(3, max(0, level)),
        "tags": tags,
        "derived_from": parents,
        "expires_at": None if expires is None else _time.render_timespec(expires, now),
        "media_hash": media,
        "meta": meta,
        "engagement": engagement,
        "vector": vector,
    }


def insert_line(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    """One parsed line into deltas (+ engagements, + vectors when the dim fits) inside the caller's transaction."""
    _store.insert_row(conn, **{k: v for k, v in row.items() if k not in ("engagement", "vector")})
    if row["engagement"] is not None:
        conn.execute(
            "INSERT INTO engagements(delta_id, target_id, kind, engaged_by, note) VALUES (?, ?, ?, ?, ?)",
            (row["delta_id"], *row["engagement"]),
        )
    if row["vector"] is not None:
        _vectors.store_vectors(conn, [(row["delta_id"], row["vector"])])  # a mismatch is dropped, not an error


def embed_missing(lake: Lake, *, batch_size: int = 64, limit: int | None = None) -> int:
    """§4.8 embed_missing: live rows without a vector and not in embed_failed, newest first, per batch:
    lake.call_embed(texts) with no transaction open, then one lake.tx() (store_vectors; failures ->
    embed_failed). A raise or a wrong count -> EmbedError(stored=count so far). §12.9 `limit`."""
    lake.require_writable()
    if lake.embed is None:
        raise LakeError("embed_missing() needs an embed callback")
    if batch_size < 1 or (limit is not None and limit < 1):
        raise ValueError("batch_size and limit must be at least 1")
    skip = set(embed_failed(lake.conn))
    sql = f"SELECT d.id FROM deltas d LEFT JOIN vectors v ON v.delta_id = d.id WHERE v.delta_id IS NULL AND {LIVE} ORDER BY d.timestamp DESC, d.seq DESC"
    ids = [r[0] for r in lake.conn.execute(sql, (lake.now_str(),)) if r[0] not in skip][:limit]
    stored = 0
    for start in range(0, len(ids), batch_size):
        chunk = ids[start : start + batch_size]
        texts = dict(lake.conn.execute(f"SELECT id, content FROM deltas WHERE id IN ({_db.qmarks(len(chunk))})", chunk))
        batch = [i for i in chunk if i in texts]  # a row swept meanwhile is left out
        if not batch:
            continue
        try:
            vecs = lake.call_embed([texts[i] for i in batch])
        except LakeError:
            raise
        except Exception as exc:
            raise EmbedError(f"embed failed: {exc}", stored=stored) from exc
        if len(vecs) != len(batch):
            raise EmbedError(f"embed returned {len(vecs)} vectors for {len(batch)} texts", stored=stored)
        with lake.tx() as conn:
            ok, bad = _vectors.store_vectors(conn, list(zip(batch, vecs, strict=True)))
            if bad:
                _db.meta_set(conn, "embed_failed", json.dumps((embed_failed(conn) + bad)[-EMBED_FAILED_MAX:]))
        stored += len(ok)
    return stored


def media_path(lake: Lake, media_hash: str) -> Path | None:
    """§3.5: the first existing `<media_dir>/<hash>.*` (the plain extension before `<hash>.thumb.*`), or None."""
    if not MEDIA_STEM_RE.match(media_hash) or not lake.media_dir.is_dir():
        return None
    found = sorted((p for p in lake.media_dir.glob(media_hash + ".*") if p.is_file()), key=lambda p: (p.name.count("."), p.name))
    return found[0] if found else None
