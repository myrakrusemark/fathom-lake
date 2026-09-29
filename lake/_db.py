"""SQLite open/create, transactions, meta, ids, batch row loading (SPEC §3.1–§3.3, §3.6, §4.3)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ._types import Delta, Engagement, LakeError, Refutation, SchemaError, Supersession

SCHEMA_VERSION = "1"
TABLES = ("meta", "deltas", "delta_tags", "derived_from", "engagements", "vectors", "deltas_fts")
CHUNK = 500  # ids per IN (...) list; well under SQLite's variable limit
DELTA_COLS = "seq, id, timestamp, content, source, kind, level, tag_key, expires_at, media_hash, meta"

# §3.2 verbatim, one statement per element, every statement IF NOT EXISTS.
DDL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS deltas (
  seq        INTEGER PRIMARY KEY,   -- rowid alias; insertion order; stable across VACUUM
  id         TEXT NOT NULL UNIQUE,  -- 12 lowercase hex chars
  timestamp  TEXT NOT NULL,         -- 'YYYY-MM-DDTHH:MM:SS.mmmZ', 24 chars, UTC
  content    TEXT NOT NULL,
  source     TEXT NOT NULL,
  kind       TEXT,                  -- NULL | 'container' | 'mood' | 'crystal' | 'sediment' | 'engagement'
  level      INTEGER NOT NULL DEFAULT 0,
  tag_key    TEXT NOT NULL,         -- the row's tags sorted by code point, joined by char(10); '' for none
  expires_at TEXT,                  -- same format as timestamp, or NULL
  media_hash TEXT,                  -- lowercase hex, 16..64 chars, or NULL
  meta       TEXT,                  -- JSON object, or NULL
  CHECK (kind IS NULL OR kind IN ('container','mood','crystal','sediment','engagement')),
  CHECK (level >= 0),
  CHECK (length(id) = 12 AND id NOT GLOB '*[^0-9a-f]*')
)""",
    "CREATE INDEX IF NOT EXISTS idx_deltas_timestamp ON deltas(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_deltas_source_ts ON deltas(source, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_deltas_kind_ts   ON deltas(kind, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_deltas_dedupe    ON deltas(source, tag_key, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_deltas_expires   ON deltas(expires_at) WHERE expires_at IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_deltas_media     ON deltas(media_hash) WHERE media_hash IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS delta_tags (
  delta_id TEXT NOT NULL REFERENCES deltas(id) ON DELETE CASCADE,
  tag      TEXT NOT NULL,
  pos      INTEGER NOT NULL,        -- 0-based position in the row's tag list
  PRIMARY KEY (delta_id, tag))""",
    "CREATE INDEX IF NOT EXISTS idx_delta_tags_tag ON delta_tags(tag, delta_id)",
    """CREATE TABLE IF NOT EXISTS derived_from (
  delta_id  TEXT NOT NULL REFERENCES deltas(id) ON DELETE CASCADE,
  parent_id TEXT NOT NULL,          -- not a foreign key: may dangle after sweep or import
  pos       INTEGER NOT NULL,       -- 0-based position in the row's derived_from list
  PRIMARY KEY (delta_id, parent_id))""",
    "CREATE INDEX IF NOT EXISTS idx_derived_from_parent ON derived_from(parent_id, delta_id)",
    """CREATE TABLE IF NOT EXISTS engagements (
  delta_id   TEXT PRIMARY KEY REFERENCES deltas(id) ON DELETE CASCADE,
  target_id  TEXT NOT NULL,         -- not a foreign key: target may be swept
  kind       TEXT NOT NULL CHECK (kind IN ('affirm','refute','reply')),
  engaged_by TEXT,
  note       TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_engagements_target ON engagements(target_id, kind)",
    """CREATE TABLE IF NOT EXISTS vectors (
  delta_id TEXT PRIMARY KEY REFERENCES deltas(id) ON DELETE CASCADE,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL            -- dim little-endian IEEE-754 float32 values, L2-normalised
)""",
    "CREATE VIRTUAL TABLE IF NOT EXISTS deltas_fts USING fts5(content, content='deltas', content_rowid='seq',"
    " tokenize='porter unicode61')",
    "CREATE TRIGGER IF NOT EXISTS deltas_ai AFTER INSERT ON deltas BEGIN\n"
    "  INSERT INTO deltas_fts(rowid, content) VALUES (new.seq, new.content);\nEND",
    "CREATE TRIGGER IF NOT EXISTS deltas_ad AFTER DELETE ON deltas BEGIN\n"
    "  INSERT INTO deltas_fts(deltas_fts, rowid, content) VALUES ('delete', old.seq, old.content);\nEND",
    "CREATE TRIGGER IF NOT EXISTS deltas_au AFTER UPDATE OF content ON deltas BEGIN\n"
    "  INSERT INTO deltas_fts(deltas_fts, rowid, content) VALUES ('delete', old.seq, old.content);\n"
    "  INSERT INTO deltas_fts(rowid, content) VALUES (new.seq, new.content);\nEND",
)


def open_db(path: Path, *, readonly: bool, created_at: str) -> sqlite3.Connection:
    """Open (or create) a lake file per §4.3: pragmas, empty-file DDL, schema check, WAL switch."""
    if readonly and not path.exists():
        raise LakeError(f"{path}: no such file (readonly open)")
    uri = path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        empty = conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    except sqlite3.DatabaseError as exc:  # "file is not a database": not a lake (§4.2)
        conn.close()
        raise SchemaError(f"not a lake: {exc}") from exc
    if empty:
        if readonly:
            conn.close()
            raise SchemaError("not a lake: empty file")
        create_schema(conn, created_at)
    else:
        try:
            check_schema(conn)
        except SchemaError:
            conn.close()
            raise
    if not readonly and conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def create_schema(conn: sqlite3.Connection, created_at: str) -> None:
    """Run the §3.2 DDL and the three meta inserts inside one BEGIN IMMEDIATE."""
    with tx(conn):
        for stmt in DDL:
            conn.execute(stmt)
        conn.executemany(
            "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
            [("schema_version", SCHEMA_VERSION), ("created_at", created_at), ("vectors_gen", "0")],
        )


def check_schema(conn: sqlite3.Connection) -> None:
    """Raise SchemaError unless the file is a v1 lake with all seven tables (§4.3 rule 2)."""
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "meta" not in names:
        raise SchemaError("not a lake: no meta table")
    version = meta_get(conn, "schema_version")
    if version is None:
        raise SchemaError("not a lake: no schema_version")
    if version != SCHEMA_VERSION:
        raise SchemaError(f"schema_version {version} is not supported by this library (v1)")
    for table in TABLES:
        if table not in names:
            raise SchemaError(f"not a lake: missing table {table}")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE / COMMIT, ROLLBACK on any exception (a failed COMMIT included); never nests."""
    if bool(conn.in_transaction):  # bool(): keeps mypy from narrowing the property for the except branch
        raise LakeError("tx(): a transaction is already open on this connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    """Value of any meta key, or None."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def meta_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """Set any meta key; None deletes it. Runs in the caller's transaction (or autocommit)."""
    if value is None:
        conn.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))


def bump_vectors_gen(conn: sqlite3.Connection) -> None:
    """The one §3.4 statement; call inside the transaction that inserts or deletes vectors."""
    conn.execute("UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'vectors_gen'")


def max_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT coalesce(max(seq), 0) FROM deltas").fetchone()[0])


def new_id(conn: sqlite3.Connection) -> str:
    """uuid4().hex[:12], regenerated on collision."""
    while True:
        cid = uuid.uuid4().hex[:12]
        if conn.execute("SELECT 1 FROM deltas WHERE id = ?", (cid,)).fetchone() is None:
            return cid


def qmarks(n: int) -> str:
    return ",".join("?" * n)


def chunks(items: Sequence[str], size: int = CHUNK) -> Iterator[Sequence[str]]:
    items = list(dict.fromkeys(items))
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_sides(
    conn: sqlite3.Connection, ids: Sequence[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, Engagement]]:
    """Tags, derived_from, and engagement for many ids: one IN (...) query per table (§3.2)."""
    tags: dict[str, list[str]] = {}
    edges: dict[str, list[str]] = {}
    eng: dict[str, Engagement] = {}
    for chunk in chunks(ids):
        q = qmarks(len(chunk))
        sql = f"SELECT delta_id, tag FROM delta_tags WHERE delta_id IN ({q}) ORDER BY delta_id, pos"
        for did, tag in conn.execute(sql, chunk):
            tags.setdefault(did, []).append(tag)
        sql = f"SELECT delta_id, parent_id FROM derived_from WHERE delta_id IN ({q}) ORDER BY delta_id, pos"
        for did, pid in conn.execute(sql, chunk):
            edges.setdefault(did, []).append(pid)
        sql = f"SELECT delta_id, target_id, kind, engaged_by, note FROM engagements WHERE delta_id IN ({q})"
        for did, tid, kind, by, note in conn.execute(sql, chunk):
            eng[did] = Engagement(tid, kind, by, note)
    return tags, edges, eng


def load_refutations(
    conn: sqlite3.Connection, ids: Sequence[str], now_ts: str
) -> dict[str, list[Refutation]]:
    """§4.5/§5.2 receipts: the live `refute` engagement rows targeting each id, newest first, as
    Refutation(refuter id, source, timestamp, note). One grouped query per CHUNK over the same
    idx_engagements_target the valence sums use, joined to the refuter `deltas` for source/timestamp."""
    out: dict[str, list[Refutation]] = {}
    for chunk in chunks(ids):
        sql = (
            "SELECT e.target_id, e.delta_id, d.source, d.timestamp, e.note"
            " FROM engagements e JOIN deltas d ON d.id = e.delta_id"
            f" WHERE e.target_id IN ({qmarks(len(chunk))}) AND e.kind = 'refute'"
            " AND (d.expires_at IS NULL OR d.expires_at > ?) ORDER BY d.timestamp DESC, d.seq DESC"
        )
        for tid, did, source, ts, note in conn.execute(sql, [*chunk, now_ts]):
            out.setdefault(tid, []).append(Refutation(did, source, ts, note))
    return out


def has_live_refute(conn: sqlite3.Connection, now_ts: str) -> bool:
    """Whether any live `refute` engagement exists — the cheap gate before the closure walks below.
    In a lake with no refutations (the common case) every dep-refuted read stops here."""
    row = conn.execute(
        "SELECT EXISTS(SELECT 1 FROM engagements e JOIN deltas d ON d.id = e.delta_id"
        " WHERE e.kind = 'refute' AND (d.expires_at IS NULL OR d.expires_at > ?))",
        (now_ts,),
    ).fetchone()
    return bool(row[0])


# §5.3 forward closure over derived_from from every live-refuted row, carrying the refuted root id.
DEP_REFUTED_CTE = (
    "WITH RECURSIVE dep(root, id) AS ("
    " SELECT e.target_id, e.target_id FROM engagements e JOIN deltas d ON d.id = e.delta_id"
    "  WHERE e.kind = 'refute' AND (d.expires_at IS NULL OR d.expires_at > ?)"
    " UNION SELECT dep.root, f.delta_id FROM derived_from f JOIN dep ON f.parent_id = dep.id"
    ")"
)


def load_dep_refuted(conn: sqlite3.Connection, ids: Sequence[str], now_ts: str) -> dict[str, list[str]]:
    """§4.5/§5.3 dependency receipts: for each of `ids` whose derived_from ancestry reaches a live-refuted
    row, the refuted ancestor ids. Empty (one EXISTS) when nothing is refuted, so ordinary reads pay
    almost nothing; otherwise one recursive walk forward from the refuted rows over derived_from."""
    want = set(ids)
    if not want or not has_live_refute(conn, now_ts):
        return {}
    out: dict[str, list[str]] = {}
    for did, root in conn.execute(f"{DEP_REFUTED_CTE} SELECT id, root FROM dep WHERE id != root", (now_ts,)):
        if did in want:
            out.setdefault(did, []).append(root)
    return out


def dep_refuted_set(conn: sqlite3.Connection, now_ts: str) -> frozenset[str]:
    """§5.3: every row transitively derived from a live-refuted row — the set score() suspends the
    summary boost for. Empty (one EXISTS) when nothing is refuted."""
    if not has_live_refute(conn, now_ts):
        return frozenset()
    return frozenset(
        did for (did,) in conn.execute(f"{DEP_REFUTED_CTE} SELECT DISTINCT id FROM dep WHERE id != root", (now_ts,))
    )


def dep_corrected_set(conn: sqlite3.Connection, now_ts: str, links: Mapping[str, Sequence[Link]]) -> frozenset[str]:
    """§5.3 correction propagation: dep_refuted_set plus every row transitively derived from a superseded row (a
    key of `links`). The walk never enters a container that asserts one of the links: it records the change, so it
    is not a summary built on the stale value. Empty (one EXISTS, no links) in the common case."""
    out, frontier = set(dep_refuted_set(conn, now_ts)), list(links)
    seen = {*links, *(x.receipt.by for group in links.values() for x in group)}  # roots and asserting containers
    while frontier:
        sql = "SELECT DISTINCT delta_id FROM derived_from WHERE parent_id IN ({})"
        found = {i for c in chunks(frontier) for (i,) in conn.execute(sql.format(qmarks(len(c))), c)} - seen
        seen |= found
        out |= found
        frontier = list(found)
    return frozenset(out)


# §3.2 meta.supersedes / §5.3 supersession links. A link lives on a container the library wrote (source
# lake:container) whose derived_from holds `new`; it is live while that container is live and not live-refuted, both
# rows exist, are live host rows (kind NULL), `new` is not live-refuted, and old.timestamp < new.timestamp. The only
# other state is the §3.6 `has_supersedes` meta key, set by insert_row with the first such container: the cheap gate.
SUPERSEDES_MAX = 16  # §3.2: entries read per container (the writer caps at the same number)
HAS_LINKS = "has_supersedes"
LINK_ROWS = "kind = 'container' AND source = 'lake:container' AND meta LIKE '%\"supersedes\"%'"
LINK_KEYS = ("old", "new", "old_value", "new_value")


@dataclass(frozen=True)
class Link:
    """One live supersession link (score() and the receipts); `receipt` is the public Supersession."""

    old: str
    new_ts: str
    receipt: Supersession
    at: str = ""  # when the link was asserted: the container's (or the newer stance's) timestamp, §6.4


def supersessions(conn: sqlite3.Connection, now_ts: str) -> dict[str, list[Link]]:
    """§5.3 live links grouped by the superseded (`old`) id, newest superseder first. One meta-key lookup when the lake
    has never held a link (constant cost, whatever the container count); otherwise one scan of the link-carrying
    containers (JSON parsed in Python, so JSON1 is not required), the refute and derived_from checks on them, and one
    IN query each for the referenced rows and their refutes. Entries that are not objects of four strings, whose `new`
    is not in the container's derived_from, or past SUPERSEDES_MAX, are ignored: the library never trusts meta."""
    out: dict[str, list[Link]] = {}
    gates = {k for (k,) in conn.execute("SELECT key FROM meta WHERE key IN (?, ?)", (HAS_LINKS, HAS_STANCES))}
    for chain in stance_chains(conn, now_ts, HAS_STANCES in gates).values():  # each older same-slug row -> every newer
        for k, (o, _, o_pos, _) in enumerate(chain):  # a drop's receipt says so, not the position it retired
            out[o] = [Link(o, n_ts, Supersession(n, n, o_pos, "no longer held" if n_st.get("retired") else n_pos), n_ts)
                      for n, n_ts, n_pos, n_st in chain[:k]]
    if HAS_LINKS not in gates:
        return {o: g for o, g in out.items() if g}
    sql = f"SELECT id, meta, timestamp FROM deltas WHERE {LINK_ROWS} AND (expires_at IS NULL OR expires_at > ?) ORDER BY seq"
    found = conn.execute(sql, (now_ts,)).fetchall()
    owners, when = [(str(i), json.loads(m).get("supersedes")) for i, m, _ in found], {str(i): str(t) for i, _, t in found}
    refuted = load_refutations(conn, [i for i, _ in owners], now_ts)
    _, edges, _ = load_sides(conn, [i for i, _ in owners])
    raw = [(by, *(e[k] for k in LINK_KEYS)) for by, es in owners if by not in refuted and isinstance(es, list)
           for e in es[:SUPERSEDES_MAX] if isinstance(e, dict) and all(isinstance(e.get(k), str) for k in LINK_KEYS)
           and e["new"] in edges.get(by, ())]
    rows: dict[str, tuple[str, str | None, str | None]] = {}
    for chunk in chunks([x for r in raw for x in r[1:3]]):
        sql = f"SELECT id, timestamp, kind, expires_at FROM deltas WHERE id IN ({qmarks(len(chunk))})"
        rows.update((i, (ts, k, exp)) for i, ts, k, exp in conn.execute(sql, chunk))
    bad_new = load_refutations(conn, [r[2] for r in raw], now_ts)
    live = {i for i, (_, k, exp) in rows.items() if k is None and (exp is None or exp > now_ts)}
    for by, o, n, ov, nv in raw:
        if o in live and n in live and n not in bad_new and rows[o][0] < rows[n][0]:
            if all(x.receipt.id != n for x in out.get(o, ())):
                out.setdefault(o, []).append(Link(o, rows[n][0], Supersession(n, by, ov, nv), when[by]))
    for group in out.values():
        group.sort(key=lambda x: (x.new_ts, x.receipt.id), reverse=True)
    return {o: g for o, g in out.items() if g}


# §5.3 stance rows (§6.3.1): `lake:stance` sediment rows tagged `stance:<slug>`; the newest live row of a slug is the
# stance, every older one is superseded by the newer ones. HAS_STANCES (§3.6) is set by insert_row with the first.
HAS_STANCES = "has_stances"


def stance_chains(
    conn: sqlite3.Connection, now_ts: str, gate: bool | None = None
) -> dict[str, list[tuple[str, str, str, dict[str, Any]]]]:
    """{slug: [(id, timestamp, position, meta.stance), ...] newest first} over live stance rows; {} (one meta lookup,
    or none when the caller read the gate) while the lake has never held a stance. Rows whose meta.stance is not an
    object with a string position are skipped."""
    if not (meta_get(conn, HAS_STANCES) is not None if gate is None else gate):
        return {}
    sql = ("SELECT t.tag, d.id, d.timestamp, d.meta FROM deltas d JOIN delta_tags t ON t.delta_id = d.id AND t.tag"
           " >= 'stance:' AND t.tag < 'stance;' WHERE d.source = 'lake:stance' AND (d.expires_at IS NULL OR"
           " d.expires_at > ?) ORDER BY d.timestamp DESC, d.seq DESC")
    out: dict[str, list[tuple[str, str, str, dict[str, Any]]]] = {}
    for tag, i, ts, m in conn.execute(sql, (now_ts,)):
        st = (json.loads(m) if m else {}).get("stance")
        if isinstance(st, dict) and isinstance(st.get("position"), str):
            out.setdefault(tag[7:], []).append((str(i), str(ts), st["position"], st))
    return out


def rests_on_newer_link(conn: sqlite3.Connection, crystal_id: str, crystal_ts: str, links: Mapping[str, Sequence[Link]]) -> bool:
    """§6.4: whether a live supersession link asserted after the crystal was written names as `old` a row in the
    crystal's derived_from ancestry (the supersession twin of crystal_rests_on_newer_refute)."""
    olds = [o for o, group in links.items() if any(x.at > crystal_ts for x in group)]
    if not olds:
        return False
    sql = ("WITH RECURSIVE anc(id) AS (SELECT parent_id FROM derived_from WHERE delta_id = ?"
           " UNION SELECT f.parent_id FROM derived_from f JOIN anc ON f.delta_id = anc.id)"
           " SELECT EXISTS(SELECT 1 FROM anc WHERE id IN ({}))")
    return any(conn.execute(sql.format(qmarks(len(c))), [crystal_id, *c]).fetchone()[0] for c in chunks(olds))


CONSOLIDATED = ("container", "mood", "crystal", "sediment")  # §5.3: kinds whose recency is their evidence's


def evidence_times(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, str]:
    """§5.3 evidence time: for each of `ids` that is a consolidated row (CONSOLIDATED kinds), the newest
    timestamp among the rows it rests on, walking derived_from through consolidated parents down to the
    first non-consolidated ancestors. Rows with no resolvable ancestor are absent (they keep their own)."""
    out: dict[str, str] = {}
    kinds = qmarks(len(CONSOLIDATED))
    for chunk in chunks(ids):
        sql = (
            "WITH RECURSIVE anc(root, id) AS ("
            " SELECT f.delta_id, f.parent_id FROM derived_from f JOIN deltas d ON d.id = f.delta_id"
            f"  WHERE f.delta_id IN ({qmarks(len(chunk))}) AND d.kind IN ({kinds})"
            " UNION SELECT anc.root, f.parent_id FROM anc JOIN deltas d ON d.id = anc.id"
            f"  JOIN derived_from f ON f.delta_id = anc.id WHERE d.kind IN ({kinds})"
            ") SELECT anc.root, max(d.timestamp) FROM anc JOIN deltas d ON d.id = anc.id"
            f" WHERE d.kind IS NULL OR d.kind NOT IN ({kinds}) GROUP BY anc.root"
        )
        out.update(conn.execute(sql, [*chunk, *CONSOLIDATED, *CONSOLIDATED, *CONSOLIDATED]).fetchall())
    return out


def agreeing_targets(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, str]:
    """{engagement row id: target id} for those of `ids` that are affirm or reply engagement rows."""
    out: dict[str, str] = {}
    for chunk in chunks(ids):
        sql = f"SELECT delta_id, target_id FROM engagements WHERE delta_id IN ({qmarks(len(chunk))}) AND kind != 'refute'"
        out.update(conn.execute(sql, chunk).fetchall())
    return out


def crystal_rests_on_newer_refute(conn: sqlite3.Connection, crystal_id: str, crystal_ts: str, now_ts: str) -> bool:
    """§6.4: whether the crystal, or a row in its derived_from ancestry, carries a live refute written
    AFTER the crystal was — the trigger that regenerates the injected crystal so it drops a premise a
    later correction overturned. Refutes older than the crystal were already in view when it was built."""
    sql = (
        "WITH RECURSIVE dep(id) AS ("
        " SELECT e.target_id FROM engagements e JOIN deltas d ON d.id = e.delta_id"
        "  WHERE e.kind = 'refute' AND (d.expires_at IS NULL OR d.expires_at > ?) AND d.timestamp > ?"
        " UNION SELECT f.delta_id FROM derived_from f JOIN dep ON f.parent_id = dep.id"
        ") SELECT EXISTS(SELECT 1 FROM dep WHERE id = ?)"
    )
    return bool(conn.execute(sql, (now_ts, crystal_ts, crystal_id)).fetchone()[0])


def row_to_delta(
    row: sqlite3.Row, tags: Sequence[str], derived_from: Sequence[str], engagement: Engagement | None,
    refuted_by: Sequence[Refutation] = (), rests_on_refuted: Sequence[str] = (),
    superseded_by: Sequence[Supersession] = (),
) -> Delta:
    """Build a Delta from a `deltas` row (DELTA_COLS) plus its side-table values."""
    meta = row["meta"]
    return Delta(
        id=row["id"], timestamp=row["timestamp"], content=row["content"], source=row["source"],
        kind=row["kind"], level=row["level"], tags=list(tags), derived_from=list(derived_from),
        expires_at=row["expires_at"], media_hash=row["media_hash"],
        meta=None if meta is None else json.loads(meta), engagement=engagement,
        refuted_by=tuple(refuted_by), rests_on_refuted=tuple(rests_on_refuted),
        superseded_by=tuple(superseded_by),
    )


def rows_to_deltas(
    conn: sqlite3.Connection, rows: Sequence[sqlite3.Row], *, now_ts: str | None = None,
    links: Mapping[str, Sequence[Link]] | None = None,
) -> list[Delta]:
    """Attach batch-loaded side tables to already-selected `deltas` rows, keeping their order.
    With `now_ts` (a stored-format string), attach `refuted_by`, `rests_on_refuted` and `superseded_by` receipts
    too — the user-facing reads pass it; internal/structural reads leave it None so Delta equality and export
    stay stable. `links` passes an already-loaded supersessions() map (score() has one) to skip the reload."""
    ids = [r["id"] for r in rows]
    tags, edges, eng = load_sides(conn, ids)
    refs = {} if now_ts is None else load_refutations(conn, ids, now_ts)
    dep = {} if now_ts is None else load_dep_refuted(conn, ids, now_ts)
    sup = {} if now_ts is None else supersessions(conn, now_ts) if links is None else links
    return [
        row_to_delta(
            r, tags.get(r["id"], ()), edges.get(r["id"], ()), eng.get(r["id"]),
            refs.get(r["id"], ()), dep.get(r["id"], ()), [x.receipt for x in sup.get(r["id"], ())],
        )
        for r in rows
    ]


def attach_refutations(conn: sqlite3.Connection, deltas: Sequence[Delta], now_ts: str) -> list[Delta]:
    """Attach `refuted_by` receipts to already-built Deltas (context containers, plan strips)."""
    refs = load_refutations(conn, [d.id for d in deltas], now_ts)
    return [replace(d, refuted_by=tuple(refs.get(d.id, ()))) for d in deltas]


def attach_dep_refuted(conn: sqlite3.Connection, deltas: Sequence[Delta], now_ts: str) -> list[Delta]:
    """Attach `rests_on_refuted` receipts to already-built Deltas (context containers)."""
    dep = load_dep_refuted(conn, [d.id for d in deltas], now_ts)
    return [replace(d, rests_on_refuted=tuple(dep.get(d.id, ()))) for d in deltas]


def load_map(conn: sqlite3.Connection, ids: Sequence[str]) -> dict[str, Delta]:
    """Deltas by id for every id that exists (expired included); missing ids are absent."""
    rows: list[sqlite3.Row] = []
    for chunk in chunks(ids):
        rows.extend(conn.execute(f"SELECT {DELTA_COLS} FROM deltas WHERE id IN ({qmarks(len(chunk))})", chunk))
    return {d.id: d for d in rows_to_deltas(conn, rows)}


# §6.1 session groups. A row is covered when a live container the library wrote (source lake:container) derives
# from it; a host-written container citing a row is a lineage pointer, not a session's name. Whether a session is
# still open is decided in _consolidate with the run's candidate rules (session_group), not here.
D_COLS = ", ".join(f"d.{c}" for c in DELTA_COLS.split(", "))
LIVE_CONTAINER = "c.kind = 'container' AND c.source = 'lake:container' AND (c.expires_at IS NULL OR c.expires_at > ?)"


def uncovered_rows(
    conn: sqlite3.Connection, tag: str, where: str, bound: Sequence[object], now_ts: str
) -> list[sqlite3.Row]:
    """Rows carrying `tag` that pass `where` (a filter_sql fragment over alias `d`) and are in the derived_from of
    no live library container, in (timestamp, seq) order. Uses idx_delta_tags_tag and idx_derived_from_parent."""
    sql = (
        f"SELECT {D_COLS} FROM deltas d JOIN delta_tags st ON st.delta_id = d.id AND st.tag = ? WHERE {where}"
        " AND NOT EXISTS (SELECT 1 FROM derived_from e JOIN deltas c ON c.id = e.delta_id"
        f" WHERE e.parent_id = d.id AND {LIVE_CONTAINER}) ORDER BY d.timestamp, d.seq"
    )
    return list(conn.execute(sql, [tag, *bound, now_ts]))


def uncovered_sessions(conn: sqlite3.Connection, prefix: str, where: str, bound: Sequence[object], now_ts: str) -> list[str]:
    """Tags starting with `prefix` that some row passing `where` carries, of sessions with zero coverage (no row
    carrying the tag in any live library container's derived_from), ordered by their newest such row, newest first."""
    hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    sql = (
        f"SELECT st.tag FROM deltas d JOIN delta_tags st ON st.delta_id = d.id WHERE {where}"
        " AND st.tag >= ? AND st.tag < ? GROUP BY st.tag"
        " HAVING NOT EXISTS (SELECT 1 FROM delta_tags t2 JOIN derived_from e ON e.parent_id = t2.delta_id"
        f"  JOIN deltas c ON c.id = e.delta_id WHERE t2.tag = st.tag AND {LIVE_CONTAINER}) ORDER BY max(d.timestamp) DESC, st.tag"
    )
    return [str(tag) for (tag,) in conn.execute(sql, [*bound, prefix, hi, now_ts])]


def missing_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> list[str]:
    """The ids (deduped, in order) that no row in the file has."""
    found: set[str] = set()
    for chunk in chunks(ids):
        found.update(r[0] for r in conn.execute(f"SELECT id FROM deltas WHERE id IN ({qmarks(len(chunk))})", chunk))
    return [i for i in dict.fromkeys(ids) if i not in found]
