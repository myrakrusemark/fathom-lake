# lake specification

Version 1 of the file format (`schema_version = "1"`), API, and behaviour of the
`lake` Python package (distribution `fathom-lake`). Derived from
the design brief. Decisions this document makes on its own are marked
"(decision)".

Conventions used below: "row" and "delta" are the same object. Times are UTC
throughout. "Live" means not expired at the moment of reading. Code fences hold
SQL, Python signatures, formats, and prompts verbatim.

---

## 1. Purpose

lake is a memory for LLM agents held in one SQLite file. It stores short
pieces of text (deltas) with who wrote them, when, and what they were derived
from; it finds them again by full-text search, optional vector similarity,
recency, and the engagement (affirm, refute, reply) they have received; it
renders what it found as prompt text; and, when the host supplies a model, it
writes its own consolidated rows (containers, moods, a crystal) whose
provenance points back into the same file. The library holds no API key, runs
no model of its own, and has no server. The host supplies two callbacks,
`think` and `embed`, and both are optional.

lake is not a vector database, a chat framework, a scheduler, a dashboard, or a
multi-user service. Routines, helpers, proposals, contacts, authentication,
source plugins, image understanding, and any server are the host's business.
The library makes no network call of its own beyond the `lake serve` client
(§13) and the model adapters a host picks (§8). A host that needs many writers across a network
puts a server in front of the file; lake itself opens the file in-process, and
several processes on one machine may open it at once.

An interpretive memory, not a neutral archive. The lake does not weigh
every row alike. Its own consolidated writes — containers, moods, the
crystal, and the sediment a deep recall lays down — sit above the raw rows
they were made from. `context()` prints the crystal first and the active
containers before any surrounding strip (§5.6.3), and when a query is
present recall multiplies a consolidated row's score by the §5.3 boost (a
container earns `1/0.85` at level ≥ 1 and `1/0.92` at level 0; an
externally-grounded sediment earns the level-0 container's `1/0.92`, and a
self-referential or unlabelled one earns none), so a distillation is biased
ahead of the moments beneath it. Without a query there is no boost (§12.5)
and interpretive rows fall back to recency order like any other, but the
render path still leads with the crystal and the containers. The lake is
therefore a reading of its own history, not a flat log of it; a host that
wants the raw rows unweighted filters by `kind` or `source` and reads them
directly.

The closed-loop invariant. Every row whose `source` begins with `lake:` was
written by the library itself — by `consolidate()`, or by the sediment pass
a deep recall runs (§6.5, §12.11) — or copied in by `import_()` from another
lake's export, and has at least one `derived_from` edge. `write()`
and `engage()` refuse every `lake:` source outright (decision: the brief
refuses only a `lake:` source with no `derived_from`; this is stricter, so
that `recall(source="lake:container")` returns what `consolidate()` wrote and
nothing else, which `test_closed_loop_source` pins, and a host row that
cites its inputs does so under the host's own source), and `write()` refuses
any `derived_from` id that does not exist in the file at write time, so an
edge can only point at a row that was already present. Following
`derived_from` edges from any consolidated row therefore reaches, in a finite
number of hops, rows the host wrote (level 0). Every claim the lake makes
about itself is traceable to the rows it was made from, and nothing in the
library can break that chain: consolidated rows are never edited, only
superseded by newer rows that cite them.

Level 0 means host-written, not human-written. A host whose ordinary rows
are model output (a reading companion's own utterances, an assistant's
turns) should write those rows with `derived_from` pointing at the rows the
model was shown, so `lineage()` can tell a row the model produced from a row
a person produced. The library cannot make that distinction by itself; a
host that needs a firewall between the two filters by `source`.

Provenance is enforced; fidelity is not. The closed-loop invariant makes
every consolidated claim traceable to the rows it was made from — a kinded
row must carry at least one `derived_from` edge (§4.4 step 3), `write()`
refuses an edge to a row not already in the file, and following the edges
reaches level-0 rows in a finite number of hops. It does not make the
claim read its sources fairly. No check in the library compares a
container's summary, a mood, a crystal, or a sediment against the rows it
cites; a model that misreads, overstates, or inverts its inputs writes a
row that is fully provenanced and still wrong, and the next crystal may
build on it. The `refute` engagement (§2) and the sediment prompt's
instruction to draw only on what surfaced (§6.5) are the only pressure
toward fidelity, and both are advisory — `refute` only lowers a row's
score (§5.3), and the prompt is an instruction to the model, not something
the library verifies. Fidelity is the host's to police, by review, by
refuting bad rows, by superseding them.

Conformance profile. The file format (§3) is the only contract between
implementations: a file written by one must open and behave the same in the
other. The core profile a second implementation must provide is §3, §4.3 to
§4.9 (`write`, `get`, `recall`, `context`, `engage`, `consolidate`,
`crystal`, `due`, `sweep`, `stats`, `export`, `import_`, `embed_missing`),
§5.1 to §5.4, §5.6, and §6. The plan steps beyond `search` and `filter`
(§5.5) and the CLI (§8) are extras; an implementation
that omits them says so and still conforms.

---

## 2. Concepts

**Delta.** One row: `id`, `timestamp`, `content`, `source`, optional `kind`,
`level`, `tags`, `derived_from`, optional `expires_at`, optional `media_hash`,
optional `meta`. Example: id `3f2a9c1b7d4e`, timestamp
`2026-08-30T14:05:12.345Z`, content `what did we decide about drift
thresholds?`, source `claude-code`, tags `[user, session:9c4e1f2a]`.

**Source.** Who wrote the row, as a free string. Hosts choose their own names
(`reader`, `claude-code`, `homeassistant`). Sources starting `lake:` are
reserved for the library's consolidated output: `lake:container`, `lake:mood`,
`lake:crystal`, `lake:sediment`, `lake:stance` (§6.3.1). `source` is required
on every write and may not be empty.

**Kind.** What sort of row this is: `NULL` for an ordinary observation,
`container`, `mood`, `crystal`, `sediment`, or `engagement`. `sediment` is a
model-written distillation over several rows (a "take"): the library writes
one after a deep recall (§6.5, `source = "lake:sediment"`), a host may write
its own deliberate take under its own source, and imported rows carry
theirs. `engagement` rows are written only by `engage()`.

**Tags.** Flat strings on a row, kept in the order given, deduplicated, no
empties. Conventions such as `user`, `assistant`, `feeling:calm`,
`session:<id>` are the host's; the library reads `user`/`assistant` (dialog
rendering), `feeling:<state>` (mood rendering), and `title:<slug>` (imported
container titles). Example: `["user", "chat", "claude-code",
"session:9c4e1f2a", "project:~/src/lake"]`.

**Provenance (`derived_from`).** The ids a row was made from, stored in a
table, ordered. A container written over rows `a`, `b`, `c` has
`derived_from = [a, b, c]`; a crystal has the prior crystal first, then the
rows it read; an engagement has `[target_id]`. A row may cite a row that has
since been swept; the edge remains and reads as a citation of something that
wilted.

**Engagement.** A row about another row: `affirm` ("this was useful"),
`refute` ("this is wrong"), or `reply` ("this prompted a thought"). It is a
normal delta (so it can be recalled and engaged in turn) and also a row in the
`engagements` table keyed by target so ranking can weigh it cheaply. It carries
a snapshot of the target's text at write time (in `content` by default, in
`meta.snapshot` when the host asks) and never expires, so it outlives a
target that had a TTL. Example: `lake.engage("3f2a9c1b7d4e", "refute",
by="robin", note="We chose 0.15, not 0.35.")`.

**Consolidation.** The library's own writes, made only when `think` is
present. Three kinds through `consolidate()`: a **container** names a
stretch of rows (title and summary over a cluster); a **mood** is a short
first-person check-in over the last few hours, stored as JSON; a **crystal** is
a first-person synthesis of who the lake's owner is right now, anchored on the
previous crystal. A fourth, **sediment**, is written by the deep-recall pass
(§6.5), not by `consolidate()`. All four carry `source = "lake:<kind>"`,
`meta.model`, and `derived_from`.

**Level.** How far above host-written rows a row sits. Every row the host
writes is level 0, whether a person or the host's model produced it. A
container is `1 + max(level of its inputs)`, capped at 3. Level
1 is an episode, 2 a topic, 3 an era. Imported Q/A markers are
containers at level 0.

**Crystal.** The newest row with `kind = "crystal"`. `lake.crystal()` returns
it; `context()` prints it first; `consolidate("crystal")` writes a new one and
also writes its text to `<name>.crystal.md` beside the file (unless the Lake
was opened with `write_crystal_file=False`). Drift between consecutive
crystals is recorded in `meta`. A crystal is a list of first-person **items** in three
sections (`core`: what I hold to, `tension`: where I'm pulled two ways,
`open`: what I haven't settled), each citing the rows it rests on, kept in
`meta.items` and rendered as prose into `content`; each new crystal is a set
of cited edits to the previous crystal's items, and the edits form its
**growth log** (§6.3). (A free-prose crystal, the only form before AAA
phase 4 and a mode until simplify/core, is read as uncited items `p1…pn`.)

**Stance.** A position the lake's self holds on a recurring question ("a
hotfix carries its regression test in the same PR"), written by the edits
crystal pass as a row of its own: `kind = "sediment"` (a stance is a take),
`source = "lake:stance"`, tag `stance:<slug>`, content `I hold that
{position}, because {reason}.`, `derived_from` = the rows that support it. How
sure it is comes from its evidence at read time: the distinct sessions of
outside evidence for and against it, and the user's affirms and refutes of
it. A revision is a new row with the same slug; the older rows read as
superseded by it. `system_prompt()` lists the live stances with their
confidence (§5.7, §6.3.1).

**Callbacks.** `think(prompt, *, system=None, json=False) -> str | dict` runs
a model; `embed(texts) -> list[list[float]]` turns strings into vectors of one
fixed dimension. Without `embed`, recall is FTS5 plus recency, filters, and
engagement weight. Without `think`, `consolidate()` raises and `due()` still
answers; everything else works.

---

## 3. File format

A lake is one SQLite database file, conventionally `<name>.lake`. Everything
below is what a second implementation (the TypeScript port) must produce and
accept. SQLite must be built with FTS5 (the `porter` tokenizer is part of
FTS5 itself). Every length in this document (`oneline` caps, the 4000-char
snapshot, the 200-char title, the crystal minimum, `budget`, the `[:8]`,
`[:12]`, and `[:16]` slices) counts Unicode code points, not UTF-16 units
and not bytes.

### 3.1 Pragmas

Set once at creation (persistent): `PRAGMA journal_mode = WAL`. On every
later open the library reads `PRAGMA journal_mode` and issues the `WAL`
pragma only when the answer is not `wal` (a file created by another tool in
`DELETE` mode is switched on first open, once; an already-WAL file is never
touched, so a read-only open takes no write lock).

Set on every connection: `PRAGMA busy_timeout = 5000`, `PRAGMA foreign_keys =
ON`, `PRAGMA synchronous = NORMAL`. A reader that does not enable foreign keys
must delete dependent rows itself when it deletes from `deltas`.

### 3.2 Tables

```sql
CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE deltas (
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
);
CREATE INDEX idx_deltas_timestamp ON deltas(timestamp);
CREATE INDEX idx_deltas_source_ts ON deltas(source, timestamp);
CREATE INDEX idx_deltas_kind_ts   ON deltas(kind, timestamp);
CREATE INDEX idx_deltas_dedupe    ON deltas(source, tag_key, timestamp);
CREATE INDEX idx_deltas_expires   ON deltas(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX idx_deltas_media     ON deltas(media_hash) WHERE media_hash IS NOT NULL;

CREATE TABLE delta_tags (
  delta_id TEXT NOT NULL REFERENCES deltas(id) ON DELETE CASCADE,
  tag      TEXT NOT NULL,
  pos      INTEGER NOT NULL,        -- 0-based position in the row's tag list
  PRIMARY KEY (delta_id, tag)
);
CREATE INDEX idx_delta_tags_tag ON delta_tags(tag, delta_id);

CREATE TABLE derived_from (
  delta_id  TEXT NOT NULL REFERENCES deltas(id) ON DELETE CASCADE,
  parent_id TEXT NOT NULL,          -- not a foreign key: may dangle after sweep or import
  pos       INTEGER NOT NULL,       -- 0-based position in the row's derived_from list
  PRIMARY KEY (delta_id, parent_id)
);
CREATE INDEX idx_derived_from_parent ON derived_from(parent_id, delta_id);

CREATE TABLE engagements (
  delta_id   TEXT PRIMARY KEY REFERENCES deltas(id) ON DELETE CASCADE,
  target_id  TEXT NOT NULL,         -- not a foreign key: target may be swept
  kind       TEXT NOT NULL CHECK (kind IN ('affirm','refute','reply')),
  engaged_by TEXT,
  note       TEXT
);
CREATE INDEX idx_engagements_target ON engagements(target_id, kind);

CREATE TABLE vectors (
  delta_id TEXT PRIMARY KEY REFERENCES deltas(id) ON DELETE CASCADE,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL            -- dim little-endian IEEE-754 float32 values, L2-normalised
);

CREATE VIRTUAL TABLE deltas_fts USING fts5(
  content,
  content='deltas', content_rowid='seq',
  tokenize='porter unicode61'
);

CREATE TRIGGER deltas_ai AFTER INSERT ON deltas BEGIN
  INSERT INTO deltas_fts(rowid, content) VALUES (new.seq, new.content);
END;
CREATE TRIGGER deltas_ad AFTER DELETE ON deltas BEGIN
  INSERT INTO deltas_fts(deltas_fts, rowid, content) VALUES ('delete', old.seq, old.content);
END;
CREATE TRIGGER deltas_au AFTER UPDATE OF content ON deltas BEGIN
  INSERT INTO deltas_fts(deltas_fts, rowid, content) VALUES ('delete', old.seq, old.content);
  INSERT INTO deltas_fts(rowid, content) VALUES (new.seq, new.content);
END;
```

Column meanings not obvious from the DDL:

| table.column | meaning |
|---|---|
| `deltas.seq` | Insertion order. Never exported; ids are the identity. Exists so FTS5 has a stable rowid and so "what was written after X" is answerable without trusting `timestamp`. |
| `deltas.id` | `uuid.uuid4().hex[:12]`, generated by `write()` and supplied only by import; if a generated id already exists, generate again. |
| `deltas.timestamp` | Observation time. Caller-settable on `write()`; defaults to now. Ordering, dedupe, windows, and export all use it. Sub-millisecond digits are dropped. |
| `deltas.content` | Text, stored verbatim: no stripping, no whitespace normalisation, no Unicode normalisation. `write()` rejects content that is empty after strip and content containing NUL; import strips NUL and accepts empty content. Dedupe (§4.4) compares the stored bytes. |
| `deltas.kind`, `deltas.level` | See §2. `level` is 0 for every kind except `container`. |
| `deltas.tag_key` | The row's tags after §4.4 normalisation, sorted by code point, joined by `\n`; `''` for a row with no tags. Written with the row and never changed; it exists so dedupe (§4.4 step 5) is one index probe. Never exported: it is derived from `tags`. |
| `deltas.expires_at` | TTL. A row is invisible to every read from the instant `expires_at <= now` (string comparison in the stored format) until `sweep()` deletes it. |
| `deltas.media_hash` | Content hash of a media file the host stores under the media directory (§3.5). The library stores the hash and nothing else in v1. |
| `deltas.meta` | JSON object. Keys the library writes: `model` (consolidated rows), `title` and `rationale` (containers), `span` (`[t_start, t_end]` of a container's inputs), `input_count` (containers whose inputs were capped), `dropped_ids` (containers written after the §6.1 citation policy dropped ids the model cited that were not in the stretch; at most 20), `session`, `part` (`[k, n]`), `fallback` (`"extractive"`) and `backfill` (`true`) (session containers, §6.1), `supersedes` (session containers that assert supersession links: a list of at most 16 objects `{"new": id, "old": id, "old_value": str, "new_value": str}`, §5.3, §6.1; read only on rows with `source = 'lake:container'`), `drift` (crystals; see §6.3), `items`, `items_cut`, `item_seq`, `edits` and `implicit_keep` (crystals, §6.3; an older library's `structured` mode wrote them too; `edits` also holds the stance ops, §6.3.1), `stance` and `grounding` (stance rows, `source = 'lake:stance'`: `{"slug", "topic", "position", "because", "against": [id, ...], "revises": id|null, "retired": bool}`, §6.3.1), `window` and `filters` (consolidated rows; the window and candidate filters the run used, §6), `snapshot` (engagements written with `snapshot=False`, §4.6). Hosts may add their own keys; the library never removes keys it does not know. Values must be finite: `write()` rejects a `meta` containing NaN or infinity (`ValueError`). |
| `engagements.engaged_by` | The `by` argument of `engage()`; free text; NULL if not given. |
| `engagements.note` | The `note` argument; NULL if not given. The snapshot lives in `deltas.content` (or `meta.snapshot`, §4.6). |
| `vectors.dim` | Vector length. Equal to `meta.embed_dim` for every row. |

Tag values are non-empty strings with leading and trailing whitespace removed
and no NUL. There is no length cap. A row's tag list is `SELECT tag FROM
delta_tags WHERE delta_id = ? ORDER BY pos`; `derived_from` likewise. An
implementation that reads many rows loads tags, edges, and engagements for
the whole set with one `IN (...)` query per table, not one query per row
(the per-row form above defines the result, not the access path).

### 3.3 FTS5

`deltas_fts` is an external-content table over `deltas.content` keyed by
`seq`. The three triggers keep it in sync; because rows are never updated
except by import repair, the update trigger is defensive. If the index is ever
suspect (a foreign writer that bypassed the triggers), run
`INSERT INTO deltas_fts(deltas_fts) VALUES ('rebuild')`. The tokenizer is
`porter unicode61` (decision, fixed now because a tokenizer change means a
rebuild): `unicode61` with its defaults (case-folded, diacritics removed,
split on non-alphanumerics) followed by the Porter stemmer, so `migrations`
matches `migration`. Ranking uses `bm25(deltas_fts)` with default weights.
Every FTS token the library builds is wrapped in double quotes before it
enters a MATCH expression (`and`, `or`, `not`, `near` are FTS5 operators
when bare); MATCH expressions are bound as parameters, never interpolated.

### 3.4 Vectors

`vectors.vec` is exactly `dim × 4` bytes: `dim` IEEE-754 single-precision
floats, little-endian, in order. Python packs with
`struct.pack(f"<{dim}f", *vec)` and unpacks with
`struct.unpack(f"<{n}f", blob)`. Vectors are L2-normalised before storage, so cosine
similarity between two stored vectors is their dot product. A vector whose
norm is 0 is not stored. The first vector written sets `meta.embed_dim`; a
later vector of a different length is not stored (`write()` and
`embed_missing()` raise `EmbedError`, §4.4 step 7 and §4.8; import drops it).
Every transaction that inserts or deletes vector rows increments
`meta.vectors_gen` with the single statement

```sql
UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'vectors_gen'
```

inside the same `BEGIN IMMEDIATE` as the inserts or deletes, so two
processes cannot lose an increment (a read-then-write of the counter is not
allowed). The `embed_dim` check happens inside that transaction too. A
reader may cache the matrix keyed on `vectors_gen`.

**Vector cache (optional, implementation detail).** A long-lived instance
keeps the matrix in memory keyed on `vectors_gen`. An implementation that
must answer vector recall from a fresh process (the CLI) may keep a sidecar
`<dir>/<stem>.vec` beside the file: a 32-byte header (`b"LAKEVEC1"`, `dim`
as uint32 LE, the `vectors_gen` and the largest `seq` covered as uint64 LE)
followed by records of `seq` (uint64 LE) + `dim` float32 LE, appended in
`seq` order. On open the implementation reads the header; when `vectors_gen`
matches it maps the file and uses it as is; when only `seq` moved on (rows
were added, none deleted since the last append) it appends the new rows;
otherwise it rebuilds. Deleted rows are masked by joining against live ids
at query time. The sidecar is derived data: it is never the source of truth,
it is safe to delete at any moment, no other implementation has to read it,
and `vectors` in the database stays complete. Nothing else about the file
format changes.

### 3.5 Media directory

For a file `<dir>/<stem>.lake` the media directory is `<dir>/<stem>.media/`
(decision: the brief says `media/` next to the file; two lakes in one
directory would then share it, and `sweep()` on one would delete files the
other still references, so the directory carries the file's stem;
`test_sweep` pins the path). The host places files there named
`<media_hash>.<ext>`; the convention for images is
`<hash>.webp` with `hash = sha256(processed bytes).hexdigest()[:16]`, and
a host that ingests images should hash the same way so hashes stay comparable.
`Lake.media_path(hash)` returns the first existing file matching
`<hash>.*` or `None`. `sweep()` deletes a file in the directory only when
all three hold: its stem (the name before the first `.`) matches
`^[0-9a-f]{16,64}$`, that stem is not the `media_hash` of any remaining row,
and the file's mtime is more than 10 minutes old (a host writes the file
before the row, and another process may sweep in between). Files with any
other name (`README`, `<hash>.thumb.webp` has stem `<hash>`, so it is kept
with its row; an `index.json`) are never touched. When the directory does
not exist the sweep skips this step and reports `orphan_media = 0`.

### 3.6 Meta keys

| key | value |
|---|---|
| `schema_version` | `"1"`. `Lake()` raises `SchemaError` on any other value or when the key is missing from a non-empty database. |
| `created_at` | Timestamp the file was created. |
| `embed_dim` | Integer as text; absent until the first vector is written. |
| `vectors_gen` | Integer as text, starts at `"0"`. See §3.4. |
| `last_consolidate:container`, `last_consolidate:mood`, `last_consolidate:crystal` | Timestamp of the last `consolidate()` call of that kind that wrote a row. |
| `last_consolidate_id:<kind>` | Id of that row. |
| `container_watermark` | JSON `{"seq": S, "pos": [T, Q] | null}`: `S` is the largest `deltas.seq` in the file when the last default container run began (or when the last import finished); `pos` is the `(timestamp, seq)` of the last row of the last processed cluster. A default run considers rows with `seq > S` or positioned after `pos` (§6.1). Absent until the first default run or import. |
| `consolidate_lease:<kind>` | Timestamp until which a `consolidate(kind)` run holds the file for that kind (§6). Deleted when the run ends; ignored once past. |
| `embed_failed` | JSON list of ids whose `embed` result was unusable (wrong length or zero norm), at most 1000, newest last. `embed_missing()` skips them; `sweep()` drops ids that no longer exist. |
| `crystal_drift` | JSON `{"value": float|null, "method": "cosine"|"token", "crystal_id": id, "prior_id": id|null, "at": timestamp}` for the newest crystal. |
| `has_supersedes` | `"1"` once a row with `source = 'lake:container'` whose `meta` has a `supersedes` key has been inserted by this library (a consolidate commit or an import); never cleared. The §5.3 gate: while absent, reads skip the supersession scan, so a lake that never held a link pays one meta lookup whatever its container count. A link row that reached a file some other way (an older library's import, raw SQL) stays dormant until the key is set. |
| `has_stances` | `"1"` once a row with `source = 'lake:stance'` has been inserted by this library (an edits crystal pass or an import); never cleared. The stance gate (§5.3, §6.3.1): while absent, reads skip the stance scan; `supersessions()` reads it and `has_supersedes` in one meta query. |
| `consolidate_lease:digest` | Timestamp until which a `digest()` holds the file (§6.6): one hour, refreshed at every step and catch-up day. Deleted when the digest ends; ignored once past. |
| `digest` | JSON `{"since": "YYYY-MM-DD", "through": "YYYY-MM-DD"|null, "attempts": {day: n}, "given_up": [day, …]}`: the catch-up state (§6.6). Days run in order, so `through` is the last day finished or given up. Absent until a digest is given `since`. |
| `digest_last` | JSON `{"at": timestamp, "calls": n, "prompt_chars": n, "stopped": "max_units"|"max_tokens"|null, "errors": n}` for the last `digest()` (§6.6); `lake serve --digest` reads `at` (§13.2). |

Hosts may store their own keys under a `host:` prefix through
`Lake.meta_get(key)` and `Lake.meta_set(key, value)` (§4.5); those two
methods accept only `host:` keys (`ValueError` otherwise). The library never
touches keys it does not define.

### 3.7 Timestamp grammar

Stored: `YYYY-MM-DDTHH:MM:SS.mmmZ`. Produced with
`dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"`.

Accepted on input (`TimeSpec`): a `datetime` (naive is UTC, aware is
converted); a string matching

```
^\s*(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?)?\s*(Z|z|[+-]\d{2}:?\d{2})?\s*$
```

(date, optional time to seconds with an optional fraction, optional `Z` or
`±HH:MM`/`±HHMM` offset; a missing time is `00:00:00`; a missing offset is
UTC; an offset is converted to UTC, not relabelled; week dates, ordinal
dates, and every other ISO 8601 form are `ValueError`, so both
implementations parse the same strings); or the relative form
`^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)\s*ago\s*$`
(case-insensitive) meaning that far before now.

Every `TimeSpec` is converted to UTC and rendered with the producer above
before it is compared with or bound against a stored timestamp; a raw input
string is never bound. Relative forms and durations use the same `now` as
the call they belong to (§4.3 `clock`).

Durations (`Duration`): a `timedelta`, or a string
`^\s*(\d+(?:\.\d+)?)\s*(s|m|h|d|w)\s*$` for seconds, minutes, hours, days,
weeks. When the library stores a duration (`meta.window`), it writes the
string form with the largest unit that divides it exactly (`"3h"`,
`"90m"`, `"2.5d"` is written `"60h"`).

---

## 4. API

```python
from lake import Lake, Delta, Hit, Engagement, Bucket, CollapsedRun, TimelineRow, Timeline
from lake import StepResult, PlanResult, ContextResult, Lineage, ConsolidateRun, NoiseRules
from lake import Duration, TimeSpec
from lake import LakeError, ClosedLoopError, NotFoundError, PlanError, ConsolidateError, LeaseHeld, EmbedError, SchemaError
```

Every type that appears in a signature below is exported. `Duration` and
`TimeSpec` are type aliases (§3.7).

### 4.1 Types

```python
@dataclass(frozen=True)
class Engagement:
    target_id: str
    kind: str            # 'affirm' | 'refute' | 'reply'
    by: str | None
    note: str | None

@dataclass(frozen=True)
class Refutation:
    id: str              # the refuting engagement row's id
    source: str          # who refuted (the refuter row's source / `by`)
    timestamp: str       # stored format, UTC
    note: str | None     # the refuter's note, or None

@dataclass(frozen=True)
class Delta:
    id: str
    timestamp: str                   # stored format, UTC
    content: str
    source: str
    kind: str | None
    level: int
    tags: list[str]
    derived_from: list[str]
    expires_at: str | None
    media_hash: str | None
    meta: dict | None
    engagement: Engagement | None    # set only when kind == 'engagement'
    refuted_by: tuple[Refutation, ...] = ()  # live refutations OF this row, newest first;
                                     # DERIVED, never stored/exported; () unless a read path
                                     # attached it (§4.5, §5.3)
    rests_on_refuted: tuple[str, ...] = ()  # ids of live-refuted rows this row derives from
                                     # (transitively); DERIVED, never stored/exported; () unless
                                     # a read path attached it (§4.5, §5.3)
    superseded_by: tuple[Supersession, ...] = ()  # live supersession links naming this row as
                                     # `old`, newest superseder first; DERIVED from containers'
                                     # meta.supersedes, never stored/exported (§4.5, §5.3)

@dataclass(frozen=True)
class Supersession:
    id: str         # the superseding (newer) row
    by: str         # the container whose meta.supersedes asserts the link (for a stance link, §6.3.1: the newer stance row)
    old_value: str  # verbatim quote from the superseded row
    new_value: str  # verbatim quote from the superseding row

@dataclass(frozen=True)
class Hit:
    delta: Delta
    score: float         # final score, §5.3; higher is better
    relevance: float     # text relevance 0..1 (1.0 when no query)
    recency: float       # recency factor 0.5..1
    valence: float       # engagement multiplier 0.30..1.30 (§5.3)
    matched: str         # 'fts' | 'vector' | 'both' | 'filter' | 'neighbor' | 'timeline' | 'bridge' | 'chain'
    step: str | None     # plan step id, None for recall()

@dataclass(frozen=True)
class Bucket:
    key: str
    count: int
    delta_ids: list[str]

@dataclass(frozen=True)
class CollapsedRun:
    source: str
    count: int
    t_start: str
    t_end: str

@dataclass(frozen=True)
class TimelineRow:
    delta: Delta
    is_anchor: bool

@dataclass(frozen=True)
class Timeline:
    id: str                          # 'tl_<i>'
    t_start: str
    t_end: str
    anchor_ids: list[str]            # sorted
    rows: list[TimelineRow | CollapsedRun]   # chronological

@dataclass(frozen=True)
class StepResult:
    hits: list[Hit] | None
    buckets: list[Bucket] | None
    timelines: list[Timeline] | None

@dataclass(frozen=True)
class PlanResult:
    steps: dict[str, StepResult]     # every step, in plan order (an ordered mapping; a port that
                                     # cannot keep insertion order for keys like "1" returns a list of pairs)
    warnings: list[str]
    timing_ms: float                 # the §5.5 execution alone; the §6.5 pass is not timed
    sediment: Delta | None = None    # the §6.5 sediment row, or None when the pass did not write

@dataclass(frozen=True)
class ContextResult:                 # context_blocks(), §5.6
    crystal: Delta | None            # the crystal block's row, None when absent or crystal=False
    hits: list[Hit]                  # the anchors, recall order
    containers: list[Delta]          # block 4, in render order
    strips: list[Timeline]           # the strips rendered, in render order
    omitted_strips: int              # block 6's n
    rendered: str                    # what context() returns
    warnings: list[str]              # e.g. "embed failed: ...; FTS only"

@dataclass(frozen=True)
class Lineage:                       # lineage(), §4.5
    rows: list[Delta]                # ancestors, breadth-first, nearest first; the root row is not included
    dangling: list[str]              # parent ids that no longer resolve, first-seen order

@dataclass(frozen=True)
class ConsolidateRun:                # Lake.last_run, §6
    kind: str
    written: list[Delta]             # every row the run wrote, in write order
    skipped: int                     # units the model answered "skip" for
    warnings: list[str]              # invalid-twice clusters, lease notes
    think_calls: int
    window: tuple[str, str] | None   # the candidate window actually used, stored format

@dataclass(frozen=True)
class DigestStep:                    # DigestRun.steps, §6.6
    step: str                        # "container" | "catch-up YYYY-MM-DD" | "mood" | "crystal"
    due: bool                        # False: nothing was due, no call
    run: ConsolidateRun | None       # None when not due, and for a dry run's mood and crystal
    prompt_chars: int                # system + user characters this step sent

@dataclass(frozen=True)
class DigestRun:                     # digest(), §6.6
    steps: list[DigestStep]          # in order; one per catch-up day
    days: list[str]                  # catch-up days finished by this digest (YYYY-MM-DD, UTC)
    given_up: list[str]              # catch-up days given up by this digest
    errors: list[str]                # "<step>: <ExceptionName>: <message>"; a failed step never stops the next
    warnings: list[str]              # e.g. "catch-up restarts from …"
    stopped: str | None              # the cap that stopped work: "max_units" | "max_tokens" | None
    think_calls: int
    prompt_chars: int                # every system + user character sent; est. input tokens = prompt_chars / 2.2

@dataclass(frozen=True)
class NoiseRules:                    # Lake(noise=...), §5.4
    drop_chars: int = 10             # N2 threshold (strict)
    soft_chars: int = 24             # soft rule threshold (strict)
    phrases: tuple[str, ...] = DEFAULT_NOISE_PHRASES   # N3 list; the 55 strings of §5.4
    exempt_sources: tuple[str, ...] = ()               # rows from these sources skip every rule
```

### 4.2 Errors

| class | base | raised when |
|---|---|---|
| `LakeError` | `Exception` | base of all library errors |
| `SchemaError` | `LakeError, RuntimeError` | the file is not a lake (§4.3), or `schema_version` is not `"1"` |
| `ClosedLoopError` | `LakeError, ValueError` | `write()` or `engage()` with a source that starts `lake:` (the prefix is reserved for the library's own writes, §1); `write()` with a non-null `kind` and empty `derived_from` |
| `NotFoundError` | `LakeError, LookupError` | `engage()` on a target that is missing or expired; `write()` with a `derived_from` id that does not exist; `consolidate(inputs=...)` with an unknown id |
| `PlanError` | `LakeError, ValueError` | plan validation (§5.5) |
| `ConsolidateError` | `LakeError, RuntimeError` | `consolidate()` without `think`; a mood or crystal output still invalid after one retry; a run of the same kind already holds the lease (§6) |
| `LeaseHeld` | `ConsolidateError` | a run of the same kind (or a digest) already holds the lease (§6, §6.6): retry once it ends; §13.4 sends it as `ConsolidateError` |
| `EmbedError` | `LakeError, RuntimeError` | any embedding failure inside `write()` or `embed_missing()`: `embed` raised, returned the wrong number of vectors, a vector of the wrong length, or a zero-norm vector. In `write()` the row is already committed without a vector and is on `.delta`; in `embed_missing()` `.delta` is `None` and the count stored so far is on `.stored` |

Plain `ValueError` is raised for bad argument values (empty source, bad
kind string, bad duration, `limit < 1`, a `meta` with NaN, a non-`host:`
key to `meta_set`, an unknown `consolidate()` opt, a `window` passed to
`consolidate("crystal")`, a `window` or a §5.1 filter passed with
`inputs=`, `depth < 1` on `cited_by()`, `sediment=True` without `plan`,
§4.5). Exceptions raised inside `think` propagate unchanged from
`consolidate()`; the §6.5 sediment pass instead catches them into a
warning (a failed sediment must never fail the recall).
Exceptions raised inside `embed` propagate unchanged from
`consolidate("crystal")` (drift) and the vector plan steps; `write()` and
`embed_missing()` wrap them in `EmbedError`; `recall()` and `context()`
catch them and continue without vectors (§5.2).

### 4.3 Constructor

```python
class Lake:
    def __init__(
        self,
        path: str | os.PathLike,
        *,
        think: Callable[..., str | dict] | None = None,
        embed: Callable[[list[str]], list[list[float]]] | None = None,
        model_name: str | None = None,
        recency_half_life: Duration = "30d",
        collapse_sources: Sequence[str] = (),
        automation: Sequence[str] | None = None,
        noise: NoiseRules | None = None,
        dedupe_window: Duration | None = None,
        embed_on_write: bool = True,
        write_crystal_file: bool = True,
        labels: Mapping[str, str] | None = None,
        due_thresholds: Mapping[str, object] | None = None,
        clock: Callable[[], datetime] | None = None,
        readonly: bool = False,
    ) -> None
    def close(self) -> None
    def __enter__(self) -> "Lake"; def __exit__(self, *exc) -> None
    last_warnings: list[str]        # set by the most recent recall()/context()/plan() on this instance
    last_run: ConsolidateRun | None # set by the most recent consolidate() on this instance
```

- `path`: created if absent (schema of §3, `journal_mode=WAL`,
  `schema_version`, `created_at`, `vectors_gen = "0"`). If present, must be a
  lake at schema version 1. Open and create follow these rules:
  1. Open the connection. Run `SELECT count(*) FROM sqlite_master`. When it
     is 0 the database is empty: run the §3 DDL and the meta inserts inside
     one `BEGIN IMMEDIATE` (every statement is `IF NOT EXISTS`, so two
     processes creating the same file at once both succeed and one of them
     finds the tables already there).
  2. Otherwise read `meta.schema_version`. Missing `meta` table, missing key,
     or any value other than `"1"` raises `SchemaError` with the reason
     (`not a lake: no meta table`, `schema_version 2 is not supported by
     this library (v1)`). Then verify that the six tables `meta`, `deltas`,
     `delta_tags`, `derived_from`, `engagements`, `vectors` and the virtual
     table `deltas_fts` exist; a missing one raises `SchemaError`. No DDL is
     ever executed on a non-empty file.
  3. Apply the per-connection pragmas (§3.1); set `journal_mode` only when
     it is not already `wal`.
  Any later schema change bumps `schema_version` and ships a migration in
  both implementations; a v1 reader raises on a file it does not understand
  and never alters it.
- `think`, `embed`: §4.9.
- `model_name`: stored as `meta.model` on every consolidated row; `null`
  when not given.
- `recency_half_life`: the half-life in §5.3. A host whose rows arrive
  sparsely (a weekly session, a book read over a term) should set it to
  the span over which it wants an old exact match to keep outranking a
  fresh weak one, for example `"365d"`; at the default, a row 60 days old
  scores 0.625 of a row written today with the same relevance. Per call,
  `recency=False` on `recall()` and `context()` sets the factor to 1.0.
- `collapse_sources` (deprecated, kept one release): each source `S` is
  appended to `automation` as the rule `source:S`, with a
  `DeprecationWarning` (decision, simplify step A: one exclusion concept).
  Everything the old option did, the `source:` rule does: timeline collapse
  (§5.6.1 T5), left out of consolidation, the 160-character row-line cap
  (§6); as automation, such rows also count toward §6.3's container filter.
  The instance exposes the `source:` rules as `automation_sources`.
- `automation` (decision, digestion phase 1): rules naming *automation
  rows*, rows an automated caller wrote (a headless job's prompts and
  replies) that must not be digested as the person's life. Each rule is
  `tag:<tag>` (rows carrying the tag), `source:<source>` (rows from the
  source) or `prefix:<text>` (rows whose content starts with the text,
  case-sensitive, and every row of their session: a row carrying a tag that
  starts with the container `session_prefix`, `"session:"` by default, which
  such a row carries and which no `tag:` rule already covers); anything
  else raises `ValueError`. Automation rows are left out of every
  consolidation input (container
  candidates, sessions and backfill, supersede candidates, mood rows and
  pressure, the crystal's rows, the age+count count),
  and a container most of whose `derived_from` parents are automation rows
  is left out of the crystal's container list (§6.3). They are not a scope:
  nothing of them is stored in `meta.filters`. Recall, context, timeline
  and every read path see them as before, so they stay searchable. The
  `prefix:` rule is for rows written before their writer labelled itself
  (the host's rule, never the library's: the library ships no patterns).
  `source:` rules also collapse the source's consecutive runs in timeline
  strips (§5.6.1 T5) and cap its §6 prompt row lines at 160 characters:
  timeline collapse is per source by construction, so `tag:` and `prefix:`
  rules do not collapse. `None` (the default) means `["tag:automation"]`,
  however the file is opened (`Lake(path)`, `lake.open(path)`, `lake.open()`,
  the CLI, `lake serve`; decision, simplify/core review: the rule was the
  hosts' default only, so a program that opened the file directly digested
  its automation rows); `[]` turns it off. The hosts' `LAKE_AUTOMATION`
  replaces it (§13.8).
- `noise`: the §5.4 thresholds, phrase list, and exempt sources. `None`
  means `NoiseRules()`.
- `dedupe_window`: rows older than this are not dedupe candidates (§4.4
  step 5). `None` (default) means no age bound.
- `embed_on_write`: when `False`, `write()` and `engage()` store no vector
  even though `embed` is set; the host runs `embed_missing()` later. The
  per-call `embed=` argument of `write()` overrides it; `engage()` has no
  per-call override (§4.6 step 5).
- `write_crystal_file`: when `False`, `consolidate("crystal")` does not
  write `<stem>.crystal.md` (§6.3).
- `labels`: overrides for the fixed strings `context()` prints (§5.6.3).
- `due_thresholds`: overrides for the constants in §6.4.
- `clock`: returns the current time as an aware `datetime`; every `now` in
  this document reads it. Default `lambda: datetime.now(UTC)`. Tests pass
  a frozen clock; the golden file depends on it.
- `readonly`: open with `mode=ro` (a URI open); every method that writes,
  including `sweep()` and `consolidate()`, raises `LakeError`, and the
  journal-mode switch of §3.1 is not attempted (reads work in either
  mode). The file must exist. The CLI's read commands use it.

One `sqlite3` connection per instance, autocommit mode, explicit
`BEGIN IMMEDIATE` around multi-statement writes. An instance is not thread-safe;
use one per thread. No module-level state. Opening an instance is cheap (one
connection, one `sqlite_master` count, one meta read); short-lived instances
are the expected shape for request-per-call hosts (§9).

### 4.4 write

```python
def write(
    self,
    content: str,
    source: str,
    tags: Sequence[str] | None = None,
    kind: str | None = None,
    derived_from: Sequence[str] | None = None,
    expires: Duration | TimeSpec | None = None,
    media: str | None = None,
    meta: dict | None = None,
    timestamp: TimeSpec | None = None,
    dedupe: bool = True,
    embed: bool | None = None,      # None: the Lake's embed_on_write
) -> Delta
```

1. Validate: `content` non-empty after strip and NUL-free (it is stored
   verbatim, unstripped); `source` non-empty, NUL-free, and not starting
   with `lake:` (else `ClosedLoopError`, whether or not `derived_from` is
   given: the prefix belongs to the library's own writes, §6 and §6.5;
   decision, §1); `kind` in
   `{None, "container", "mood", "crystal",
   "sediment"}` (`"engagement"` only via `engage()`); `media`, if given,
   matches `^[0-9a-f]{16,64}$`; `meta`, if given, is a JSON-serialisable
   dict with finite numbers. `timestamp` defaults to now (decision:
   caller-settable). `expires`: a `Duration` is added
   to now; a `TimeSpec` is used as is; a value at or before now raises
   `ValueError`.
2. Normalise tags (strip, drop empties, dedupe keeping first occurrence) and
   `derived_from` (strip, drop empties, dedupe keeping first). `tag_key` =
   the normalised tags sorted by code point, joined by `\n`, `''` when none.
3. Closed loop: if `kind` is not `None` and `derived_from` is empty, raise
   `ClosedLoopError`. Every `derived_from` id must exist in `deltas`
   (expired but unswept counts), else `NotFoundError`.
4. Level: `0`, except `kind == "container"` where `level = min(3, 1 +
   max(level of derived_from rows))`.
5. Sequential dedupe (brief), unless `dedupe=False` or
   `media` is given (every media write is an explicit observation).
   Find the most recent live row (by `timestamp`, then `seq`) with the same
   `source`, the same `tag_key` (equality of sets, not superset; an empty
   set matches an empty set), `kind != 'engagement'` (engagement rows have
   their own rule, §4.6), and, when `dedupe_window` is set, `timestamp >=
   now − dedupe_window`. If that row's `content` equals `content` byte for
   byte, write nothing and return it as a `Delta`. Only the single most
   recent such row is compared, so writes A, B, A produce three rows.
   Expired rows are never candidates. Reference SQL:

   ```sql
   SELECT d.* FROM deltas d
   WHERE d.source = :source AND d.tag_key = :tag_key
     AND (d.kind IS NULL OR d.kind != 'engagement')
     AND (d.expires_at IS NULL OR d.expires_at > :now)
     AND (:floor IS NULL OR d.timestamp >= :floor)
   ORDER BY d.timestamp DESC, d.seq DESC LIMIT 1
   ```

   One probe of `idx_deltas_dedupe`; the cost does not grow with the number
   of rows the source has written. Two consequences the host must know: the
   returned `Delta` carries the prior row's `timestamp`, not the time of
   this call, so a dialog host that needs per-session attribution puts a
   session tag (`session:<id>`) on every row, which makes each session its
   own dedupe scope; and when the deduped write carries `expires` and the
   existing row's `expires_at` is earlier than the new value (or NULL), the
   existing row's `expires_at` is set to the later value in the same
   transaction and the returned `Delta` shows it. That TTL refresh is the
   only in-place edit of an ordinary row the library ever makes (decision:
   a daemon re-asserting a state every minute with `expires="1h"` must keep
   the state alive).
6. Insert the row, its tags, and its `derived_from` edges in one
   `BEGIN IMMEDIATE` transaction that also contains steps 3 to 5. Commit.
7. If `embed` is set on the Lake and embedding is enabled for this call
   (`embed=True`, or `embed=None` and `embed_on_write`), call
   `embed([content])` **after** the commit, with no transaction open, then
   store the vector in its own short transaction (§3.4: `embed_dim` check,
   insert, `vectors_gen` increment). If `embed` raises, returns anything but
   one vector, returns a vector of the wrong length, or returns a zero-norm
   vector, the row stays committed without a vector and `EmbedError` is
   raised with `.delta` set.
8. Return the `Delta`.

`write()` never accepts an id from the host (decision).

### 4.5 get, recall, plan, context

```python
def get(self, delta_id: str, *, include_expired: bool = False) -> Delta | None
```

Exact id, or a unique prefix of at least 8 characters. An argument
that does not match `^[0-9a-f]{8,12}$` returns `None` without touching the
database (so `%` and `_` never reach the prefix `LIKE`). Expired rows return
`None` unless `include_expired` (brief: TTL honoured on every read).

The `Delta` returned by `get()`/`resolve()` carries `refuted_by`: the live
`refute` engagements that target it, newest first, each as a
`Refutation(id, source, timestamp, note)`. The row itself is returned fully
intact — content, tags, edges, vector, and meta are never altered by a
refutation. `refuted_by` is derived at read time from the `engagements`
reverse edge (`target_id`, `kind='refute'`, live refuter rows); it is never
stored and never exported (§4.8).

Every user-facing read (`get`, `recall`, plan steps, `context`) also attaches
`superseded_by`: the live supersession links (§5.3) that name the row as
`old`, newest superseder first, each as `Supersession(id, by, old_value,
new_value)`. Like `refuted_by` it is derived at read time (from the
`meta.supersedes` of live library containers, and from the stance rows'
slugs, §6.3.1), never stored, never exported;
the row itself is untouched. `lineage(new_id)` is unchanged; the pairs a
container asserts are in its own `meta.supersedes`.

```python
def recall(
    self,
    query: str | None = None,
    *,
    source: str | Sequence[str] | None = None,   # one source, or any of several
    tags: Sequence[str] | None = None,        # row must carry all
    any_tags: Sequence[str] | None = None,    # row must carry at least one
    exclude_tags: Sequence[str] | None = None,  # row must carry none
    kind: str | Sequence[str] | None = None,  # 'plain' selects rows with kind NULL
    since: TimeSpec | None = None,            # timestamp >= since
    until: TimeSpec | None = None,            # timestamp <= until
    limit: int = 20,
    exclude_sources: Sequence[str] | None = None,
    include_expired: bool = False,
    noise: bool = True,                       # apply §5.4 (query present only)
    recency: bool = True,                     # False: recency factor is 1.0
    min_relevance: float = 0.0,               # drop candidates below this after normalisation
    plan: Sequence[dict] | None = None,
    filters: Mapping[str, object] | None = None,  # with plan: ANDed into every step (§5.5)
    sediment: bool | None = None,             # with plan: the §6.5 pass; None = automatic
) -> list[Hit]
```

With `plan` given, every argument other than `filters` and `sediment` must
be at its default (else `ValueError`); the result is the hits of the last
step (§5.5).
Otherwise §5.1 to §5.4. `limit < 1` raises `ValueError`. Without `query`
the hits come back newest first, ordered `(timestamp DESC, seq DESC)`, with
`score`, `recency`, and `valence` still filled in; this matches the plan
`filter` step, so "the newest row of kind X" is `recall(kind=X, limit=1)`
(decision: a listing must show the moment as it was, and boosts must not
reorder it). With `query` the order is by score (§5.3). Warnings from the
call (an `embed` failure §5.2, a sediment failure §6.5) go to
`lake.last_warnings`.

`sediment` controls the §6.5 pass, which reads what a deep recall surfaced
and writes a first-person `lake:sediment` row citing it. `None` (default)
is automatic: the pass runs when the call executed a plan, `think` is set,
and the §6.5 gates pass. `True` runs the same pass — the gates still apply,
so without `think`, on a `readonly` open, or under 2 distinct sources it is
a no-op; today `True` and `None` behave identically on the plan path, and
the value exists so a call site can pin the behaviour explicitly — and
raises `ValueError` when `plan` is not given (a forced sediment needs a
plan). `False` switches the pass off. Non-plan recalls never sediment, and
`context()` never does (its anchor recall is the non-plan path).

```python
def plan(
    self,
    steps: Sequence[dict],
    *,
    filters: Mapping[str, object] | None = None,
    sediment: bool | None = None,             # the §6.5 pass; None = automatic
) -> PlanResult
```

Executes a plan and returns every step's result (decision: added so hosts and
`context()` can read timelines and buckets; `recall(plan=...)` is a view on
it). `filters` holds any of the §5.1 keys and is ANDed into every step that
reads filters (§5.5). `sediment` is as on `recall()`; the row the pass
writes, if any, is on `PlanResult.sediment` (§4.1), which is how a caller
sees the sediment text alongside the hits.

```python
def context(
    self,
    query: str | None,
    *,
    budget: int = 8000,
    limit: int = 30,
    source: str | Sequence[str] | None = None,
    exclude_sources: Sequence[str] | None = None,
    tags: Sequence[str] | None = None,
    any_tags: Sequence[str] | None = None,
    exclude_tags: Sequence[str] | None = None,
    kind: str | Sequence[str] | None = None,
    since: TimeSpec | None = None,
    until: TimeSpec | None = None,
    crystal: bool = True,        # block 1
    containers: bool = True,     # block 4
    noise: bool = True,
    recency: bool = True,
    min_relevance: float = 0.0,
    labels: Mapping[str, str] | None = None,   # per-call override of Lake(labels=...)
) -> str
def context_blocks(self, query: str | None, *, <the same keyword arguments>) -> ContextResult
def system_prompt(self, *, moods: int = 3, budget: int = 8000, labels: Mapping[str, str] | None = None) -> str
```

The identity crystal and the most recent `moods` mood rows as one block a
host passes as its actual system message (§5.7), so the crystal shapes
behaviour from turn 1 instead of arriving as loose recall context.
Read-only; never calls `think` or `embed`; `""` when there is neither a
crystal nor a mood.

§5.6. `limit` is the number of anchors recalled (decision). The filter arguments
are the §5.1 set and apply both to the anchor recall and to the strip fetch
(T1), so a host can scope a whole render to a set of sources or keep its own
echo out of it (the plugin passes `exclude_tags=["session:<id>"]` so the
prompt it has just written is not its own top hit). `context_blocks()` runs
the same selection and budget logic and returns the parts; `context()` is
`context_blocks(...).rendered`.

```python
def lineage(self, delta_id: str, *, depth: int | None = None, include_expired: bool = True) -> Lineage
def cited_by(self, delta_id: str, *, depth: int = 1, include_expired: bool = False) -> list[Delta]
```

The two walk the `derived_from` table in opposite directions.

`lineage()` follows the row's own `derived_from` list: from the row to the
ids it was made from, then to theirs (prefix allowed, as in `get()`; an
unknown id raises `NotFoundError`), breadth first, each row visited once,
`depth` hops at most (`None` for all the way to rows with no
`derived_from`). `rows` are the ancestors in visit order, nearest first;
`derived_from` ids that no longer resolve go to `dangling` in first-seen
order. For a crystal this reaches the containers it read and then their
inputs; for a host-written row it is empty.

`cited_by()` follows the reverse edges: hop 1 is every row whose
`derived_from` includes the id (`SELECT delta_id FROM derived_from WHERE
parent_id = ?`), hop `k + 1` is every row whose `derived_from` includes a
hop-`k` row's id, breadth first, each row visited once, `depth` hops at
most (`depth < 1` raises `ValueError`). The result is ordered by hop,
nearest first, and within a hop by `(timestamp DESC, seq DESC)`. Expired
rows are neither returned nor walked through unless `include_expired`.
Nothing dangles in this direction (`derived_from.delta_id` is a foreign
key). For a host-written row, `depth=1` is the containers, engagements,
moods, and crystals that cite it directly; `depth=3` also reaches a
level-2 container that cites a level-1 one and the crystal that cites
either. `context()` builds block 4 with `cited_by(hit.id, depth=3)` (§5.6
step 2); `lineage()` is not used there. These exist because provenance is
the headline feature and a host should not have to write its own
breadth-first search over `get()` (decision).

```python
def meta_get(self, key: str) -> str | None
def meta_set(self, key: str, value: str | None) -> None
```

Host-owned meta keys (§3.6). `key` must start with `host:`; `value=None`
deletes the key. One transaction each.

### 4.6 engage

```python
def engage(
    self,
    delta_id: str,
    kind: str,                    # 'affirm' | 'refute' | 'reply' (also accepts 'affirms', 'refutes', 'reply-to')
    *,
    by: str | None = None,
    note: str | None = None,
    tags: Sequence[str] | None = None,
    snapshot: bool = True,
    dedupe: bool = True,
) -> Delta
```

1. Resolve `delta_id` with `get()` (prefix allowed); missing or expired
   raises `NotFoundError`. The stored `target_id` is always the full id.
   `by` that
   starts with `lake:` raises `ClosedLoopError`.
2. Build the snapshot, with `note` as the
   reason. With `T` the target, `text = T.content.strip()`, and `L = 4000`:

   ```
   > {each line of text[:4000], "…" appended to the last line if text was longer; blank lines render as ">"}
   > — {T.source} · {T.timestamp[:16]} · {T.id[:8]}{" · [image]" if T.media_hash}

   {note}
   ```

   If `text` is empty and `T.media_hash` is set, only the footer line is
   written. If `text` is empty and there is no media, only the footer line
   is written (decision: the footer keeps the row
   readable). The reason block and its preceding blank line appear only
   when `note` is non-empty. The whole string is stripped.

   With `snapshot=True` (default, brief) that block is the row's `content`.
   With `snapshot=False` the row's `content` is `note.strip()` when `note`
   is non-empty, else the footer line alone, and the quoted block goes to
   `meta.snapshot` instead. Either way the target's text survives the
   target's TTL. Hosts that keep sources apart by filtering on `source`
   must know that with the default the engagement's content **is the
   target's text** under the engager's `source`: it is indexed by FTS, so
   `recall(source="reader", query=...)` can return sentences the reader
   never said. Such hosts pass `snapshot=False`.
3. Write a row with `source = by if by else "engagement"` (decision),
   `kind = "engagement"`, `derived_from = [T.id]`, `media_hash = T.media_hash`,
   no `expires_at`, `tags` as given. Dedupe (unless `dedupe=False`): the
   candidate is the most recent live `kind = 'engagement'` row with the
   same `source`, the same `tag_key`, the same `engagements.target_id`, and
   the same `engagements.kind` (and within `dedupe_window` when set); if
   its `content` equals the new content byte for byte, nothing is written
   and steps 4 to 6 use that existing row. Because the engagement kind is
   part of the key, `affirm` then `refute` by the same `by` on the same
   target with no note are two rows (decision; a content-only key made the
   second call return the first row).
4. Insert `engagements(delta_id, target_id, kind, engaged_by, note)` in the
   same transaction as the row. Commit.
5. If `embed` is set on the Lake and `embed_on_write` is `True`, call
   `embed([content])` after the commit with no transaction open, then store
   the vector in its own short transaction, exactly as §4.4 step 7 (the
   `embed_dim` check, the insert, the `vectors_gen` increment; the same
   failures raise `EmbedError` with `.delta` set and the row stays
   committed without a vector). `content` here is the row's stored content,
   so with `snapshot=True` the vector is of the quoted target text plus the
   note, and vector recall can return the engagement beside its target,
   just as FTS can (step 2); an affirm or reply sorts directly after its
   target when both are hits (§5.3 Engagement under its target); with `snapshot=False` it is of the note or the
   footer line. Skipped when step 3 returned an existing row. There is no
   per-call switch; a host that does not want engagement vectors opens the
   Lake with `embed_on_write=False` and runs `embed_missing()` on its own
   terms (decision: `embed_missing()` would embed them later anyway, so writing the vector
   at once keeps the two paths equal).
6. Return the `Delta` (its `engagement` field is set).

Engaging an engagement is allowed and unremarkable.

### 4.7 consolidate, digest, crystal, due

```python
def consolidate(self, kind: str, window: Duration | tuple[TimeSpec, TimeSpec] | None = None, **opts) -> Delta | None
def digest(self, *, since: str | None = None, max_units: int | None = None, max_tokens: int | None = None,
           backfill_max: int | None = None, dry_run: bool = False) -> DigestRun
def crystal(self) -> Delta | None
def due(self, kind: str) -> bool
```

§6 lists every `opts` key per kind. `consolidate()` raises
`ConsolidateError` when `think` is `None`; it returns the last row it wrote
(brief: one `Delta`) and leaves the full report of the run on
`lake.last_run` (a `ConsolidateRun`). `crystal()` returns the newest live
`kind = "crystal"` row by `ORDER BY timestamp DESC, seq DESC LIMIT 1`
(SQL, not a scored recall). `due()` never calls `think`, and
`consolidate()` never checks `due()`: a host with its own cadence calls
`consolidate()` directly.
`digest()` (§6.6) is the one call a host schedules: it runs whatever
`consolidate()` is due, in order, and is idempotent. The library never
schedules it; `lake digest`, a timer, `lake serve --digest` (§13.2) or the
host's own scheduler does.

### 4.8 sweep, stats, export, import_, embed_missing

```python
def sweep(self) -> dict            # {"deleted": n, "orphan_media": n}
def stats(self) -> dict
def export(self, path: str | os.PathLike, *, include_expired: bool = False, vectors: bool = False) -> int
def import_(self, path: str | os.PathLike, *, include_expired: bool = False) -> dict   # {"written", "skipped", "errors"}
def embed_missing(self, *, batch_size: int = 64, limit: int | None = None) -> int
def media_path(self, media_hash: str) -> pathlib.Path | None
```

- `sweep()`: in one transaction, `DELETE FROM deltas WHERE expires_at IS NOT
  NULL AND expires_at <= :now` (cascades to tags, edges, engagements,
  vectors, FTS), bump `vectors_gen` if any deleted row had a vector, prune
  `meta.embed_failed` of ids that no longer exist; then, outside the
  transaction, delete orphan media files under the §3.5 rules (stem is
  16 to 64 hex characters, unreferenced, mtime older than 10 minutes;
  nothing when the directory is absent). Writes no rows.
- `stats()`: `{"rows", "live_rows", "expired_unswept", "by_kind": {kind:
  n}, "by_source": {source: n} (top 50 by count), "tags": distinct tag
  count, "vectors", "embed_dim", "engagements": {"affirm": n, "refute": n,
  "reply": n}, "last_consolidate": {"container": ts|None, "mood": ...,
  "crystal": ...}, "crystal_id", "crystal_drift", "digest", "digest_last",
  "file_bytes", "schema_version"}`; `digest` and `digest_last` are the §3.6
  meta values (the catch-up state and the last digest), `None` when absent.
  Counts exclude expired rows except where named.
- `export()`: JSONL, one object per live row (or all rows with
  `include_expired`), ascending `(timestamp, seq)`, keys in this order:
  `id, timestamp, content, source, kind, level, tags, derived_from,
  engagement, expires_at, media_hash, meta`, plus `vector` (list of floats)
  when `vectors=True`. `engagement` is `{"target_id", "kind", "by", "note"}`
  or `null`. `tag_key` and `seq` are never exported. Each line is canonical
  JSON: UTF-8, no insignificant whitespace (separators `,` and `:`), keys
  in the order above and `meta` keys in the order they were stored,
  strings escaped only where JSON requires it (non-ASCII kept as is),
  integers as integers, other numbers as the shortest decimal string that
  round-trips to the same IEEE-754 double, no NaN or infinity (rejected at
  write time), LF-terminated. Python: `json.dumps(obj, ensure_ascii=False,
  separators=(",", ":"), allow_nan=False)`; JavaScript: `JSON.stringify`
  on an object built in key order. Both produce this line for a plain row:

  ```
  {"id":"3f2a9c1b7d4e","timestamp":"2026-08-30T14:05:12.345Z","content":"what did we decide about drift thresholds?","source":"claude-code","kind":null,"level":0,"tags":["user","session:9c4e1f2a"],"derived_from":[],"engagement":null,"expires_at":null,"media_hash":null,"meta":null}
  ```

  Textual equality between implementations is guaranteed for that rule set
  and nothing beyond it. Returns the row count.
- `import_()`: reads JSONL; blank lines skipped; unparsable lines counted
  as errors; a line with a `modality` key and no `derived_from` key is
  not lake-format JSONL and is counted as an error, otherwise the lake format (table below). Ids are preserved; a line whose id already exists is
  skipped; a line whose `expires_at` is at or before now is skipped unless
  `include_expired`. Each line is its own transaction so one bad line does
  not poison the file (decision).
  Vectors on the line are stored if `dim` matches `embed_dim` (or sets it),
  else dropped. Content NULs are stripped. Returns counts. Import bypasses
  `write()`: it keeps the id check, the kind check, the NUL strip, and tag
  and edge normalisation (strip, drop empties, dedupe keeping first, so a
  line with `from:X` twice or `from:X` plus `affirms:X` yields one edge),
  and drops the dedupe, the closed-loop check on `derived_from` (a
  `lake:` line with empty `derived_from` is an error line), the
  existence check on parents (dangling edges are kept), and the empty
  content rejection. When the import wrote at least one row it sets
  `container_watermark.seq` to the file's largest `seq` (creating the key
  with `pos = null` when absent), so imported history is never
  default-clustered; a host that wants it consolidated passes an explicit
  `window` (§6.1). Imported `lake:container` lines keep their
  `meta.supersedes` verbatim, and those links are live in the new file (§5.3)
  under the same rules as links the library wrote, including that `new` must
  be in the container's own `derived_from`: an import (local or `POST
  /v1/import`) can therefore demote a host row ×0.50, as an imported refute
  can demote it ×0.30. Audit with `lineage(container)`, which lists its
  pairs; withdraw by refuting the container.

  The lake format, key by key:

  | key | destination |
  |---|---|
  | `id` | `deltas.id`, must match `^[0-9a-f]{12}$`, else the line is an error |
  | `timestamp` | `deltas.timestamp`, any §3.7 form, converted |
  | `content` | `deltas.content`, NUL stripped, empty allowed |
  | `source` | `deltas.source`, non-empty; `lake:` allowed only with a non-empty `derived_from` |
  | `kind` | `deltas.kind`; `"engagement"` allowed here (it is `write()` that refuses it) |
  | `level` | taken as given, clamped to `0..3`; missing → 0 |
  | `tags` | `delta_tags` after normalisation; `tag_key` recomputed |
  | `derived_from` | `derived_from` after normalisation; targets absent from the file are kept |
  | `engagement` | when non-null and `kind == "engagement"`: an `engagements` row `(id, target_id, kind, by, note)`; otherwise ignored |
  | `expires_at` | `deltas.expires_at`; the expiry skip above; an absent key and `null` are the same |
  | `media_hash` | `deltas.media_hash`; an absent key and `null` are the same |
  | `meta` | `deltas.meta` as given |
  | `vector` | `vectors` when its length matches `embed_dim` (or sets it), else dropped |
- `embed_missing()`: embeds live rows with no vector and not listed in
  `meta.embed_failed`, newest first, in batches of `batch_size`; requires
  `embed`. Per batch: call `embed(texts)` with no transaction open, then one
  `BEGIN IMMEDIATE` holding the `embed_dim` check, the inserts, and the
  `vectors_gen` increment. A wrong-length or zero-norm vector inside a batch
  is not stored and its id is appended to `meta.embed_failed` in that
  transaction; the rest of the batch is stored. If `embed` raises or returns
  the wrong number of vectors, `EmbedError` is raised with `.stored` = the
  count committed so far. Returns the count stored.

### 4.9 Callback contracts

```python
def think(prompt: str, *, system: str | None = None, json: bool = False) -> str | dict
def embed(texts: list[str]) -> list[list[float]]
```

`think`: `system` is the system prompt or `None`; `json=True` asks for a JSON
object. The callback may return a `dict` (already parsed) or a `str`. A
string is cleaned in this order before any validation: (1) strip; (2) if it
starts with `<think>`, remove everything through the first `</think>` and
strip again (reasoning models emit this block on some backends); (3) with
`json=True`, try the content of every Markdown fenced block anywhere in the
text (```` ```json ... ``` ```` or a bare ```` ``` ````), in order, then the
whole text; in each, scan `{` positions (at most 32) and return the
first balanced `{ ... }` (brace matching that skips braces inside JSON
strings) that `json.loads` parses to an object, so a model that wraps its
object in a sentence, puts prose such as `{id}` before it, or writes two
fences of which the first is broken still parses. A balanced span that does
not parse is skipped whole and the scan resumes after its closing brace;
only an unclosed `{` moves the scan on by one character (decision, AAA
phase 3 review: resuming inside the span unwrapped a malformed
`{"propose": {...},}` to its inner object); (4) with `json=False`, the
text after step 2 is the answer. No object found counts as an invalid output
(one retry, then the kind's failure rule, §6). Keys are never aliased: a
nested `{"propose": {...}}` is returned as the outer object, which validation
then rejects (decision: aliasing would hide a model-format failure). The same
cleaning is public as `lake.parse_answer(text, *, json)` (a `dict` passes
through with `json=True`, and gives `None` with `json=False`), for hosts that
parse model output of their own. The library never passes tools; every
input the model needs is in the prompt. `think` is always called with no
transaction open (§6).

`embed`: returns one vector per input, all of one length, floats. Called by
`write()` (one text), `embed_missing()` (batches), `recall()` and
`context()` (the query, one text), `consolidate("crystal")` (drift, two
texts), and the vector plan steps.

Absent `embed`: no vectors are written or read; `recall()` is FTS5 + filters
+ recency + valence; `bridge` and `chain` fall back to FTS (§5.5); crystal
drift uses token overlap. Absent `think`: `consolidate()` raises
`ConsolidateError`; `crystal()`, `due()`, and everything else work.

---

## 5. Recall

Recall is deterministic: no model runs inside it. Given the same file, the
same clock, and the same `embed`, two implementations return the same hits in
the same order.

### 5.1 Filters

Filters run first and bound every later step. All are ANDed:

| argument | SQL on `deltas d` |
|---|---|
| expiry (unless `include_expired`) | `(d.expires_at IS NULL OR d.expires_at > :now)` |
| `source` | `d.source = :source` for one string; `d.source IN (...)` for a sequence |
| `exclude_sources` | `d.source NOT IN (...)` |
| `tags` (all of) | `(SELECT count(*) FROM delta_tags t WHERE t.delta_id = d.id AND t.tag IN (...)) = :n` with `n` the number of distinct tags asked for |
| `any_tags` | `EXISTS (SELECT 1 FROM delta_tags t WHERE t.delta_id = d.id AND t.tag IN (...))` |
| `exclude_tags` | `NOT EXISTS (SELECT 1 FROM delta_tags t WHERE t.delta_id = d.id AND t.tag IN (...))` |
| `kind` | `d.kind IN (...)`; the value `"plain"` contributes `d.kind IS NULL` |
| `since` | `d.timestamp >= :since` (inclusive) |
| `until` | `d.timestamp <= :until` (inclusive) |

Empty sequences add no condition. Every value is a bound parameter; the
only text interpolated into SQL is the list of `?` placeholders behind each
`IN (...)`. `since` and `until` are rendered to the stored format before
binding (§3.7).

### 5.2 Candidates

Without `query`: every filtered row is a candidate with `relevance = 1.0` and
`matched = "filter"`. The noise rules do not apply (a listing must show the
moment as it was).

With `query`:

1. **FTS5.** Tokens are `re.findall(r"[^\W_]+", query.lower())`; drop tokens
   shorter than 2 characters (decision, AAA phase 2: the earlier 3-character
   rule dropped `uv`, `db`, `io`, `04` and lost exact probes; the common
   two-letter words are in the stoplist, and a two-character token that
   survives costs one posting scan like any other); drop tokens in the fixed
   stoplist below (decision: a fixed list is deterministic and the same in
   both implementations; a corpus-derived list would not be); dedupe
   keeping first occurrence; keep the first 64. If no tokens remain, the
   FTS candidate set is empty. The MATCH expression is each token wrapped
   in double quotes (a `"` inside a token cannot occur after the regex)
   joined by ` OR `, for example `"lake" OR "design" OR "brief"`, bound as
   a parameter. Run

   ```sql
   SELECT d.id, -bm25(deltas_fts) AS bm
   FROM deltas_fts f JOIN deltas d ON d.seq = f.rowid
   WHERE deltas_fts MATCH :match AND <filters>
   ORDER BY bm DESC LIMIT 500
   ```

   `rel_fts = bm / max(bm over these rows)`; the best row scores 1.0.
   There is no absolute floor on `bm` (decision, recorded in §11: bm25
   values depend on corpus statistics, so a constant would mean different
   things in different lakes); `min_relevance` is the host's floor. The
   `LIMIT 500` is part of the contract: the FTS pool holds at most the 500
   best-ranked matches, the vector pool (step 2) at most 500, so a
   `recall()` or a plan `search` step with `limit` above 1000 (500 without
   `embed`) never returns more than that, whatever the lake holds.

   The stoplist (121 words, matched against the lower-cased token before
   stemming):

   ```
   a, an, the, and, or, but, if, then, else, so, of, to, in, on, at, by, for,
   with, from, as, into, onto, over, under, about, after, before, between,
   through, is, are, was, were, be, been, being, am, do, does, did, done, have,
   has, had, having, will, would, shall, should, can, could, may, might, must,
   it, its, this, that, these, those, i, me, my, mine, you, your, yours, he,
   him, his, she, her, hers, we, us, our, ours, they, them, their, theirs, who,
   whom, whose, which, what, when, where, why, how, not, no, nor, all, any,
   some, each, every, both, few, more, most, other, such, only, own, same,
   than, too, very, just, also, here, there, now, up, down, out, off, again,
   once
   ```
2. **Vectors** (only when `embed` is set). `q = normalise(embed([query])[0])`.
   For every filtered row with a vector, `cos = q · v`; `cos_base` is the
   median of those cosines (every filtered row with a vector, not only the
   top 500). Keep rows with `cos > 0`, take the top 500 by `cos`, and set
   `rel_vec = max(0, (cos − cos_base) / (cos_max − cos_base))` over those
   rows (`rel_vec = 1.0` for every row when `cos_max <= cos_base`). The
   same formula gives every other filtered row with a vector its `rel_vec`:
   the 500 cap bounds only the rows the vector pass may add on its own
   (step 3, third bullet), never the `rel_vec` an FTS row fuses with. (With
   `rel_vec = 0` outside the top 500, an exact lexical match ranked 701st by
   cosine in a 2,000-row lake fell to `0.3 × rel_fts` and last of 501 hits.)
   (Decision, AAA phase 2: text
   embedders such as nomic, mxbai, and qwen-embed put unrelated rows at
   cosine 0.4 to 0.7, so the query's median cosine is its "unrelated"
   level. The earlier baseline, the minimum of the 500-row window, spread
   every row of a small lake over 0 to 1 and let weak vector matches
   compete at full strength: on the eval's seeded world, hybrid recall@1
   was 0.4679 against 0.5705 for FTS alone.)
   If `embed` raises, the call continues with the FTS set alone, every
   hit has `matched` in `{"fts"}`, and the string
   `embed failed: {exception}; FTS only` is appended to
   `lake.last_warnings` (and to `ContextResult.warnings` /
   `PlanResult.warnings`). A query embed that has not answered within
   `QUERY_EMBED_WAIT` (3 s) counts as a failure too: the call falls back to
   the FTS set with `embed failed: query embed took over 3s; FTS only`, and
   the embed keeps running on a daemon thread until its own timeout (§8).
   A busy embedder (a backfill, a digest) then costs recall its vector
   pass, never the caller's deadline (the prompt hook allows 8 s).
   `context()` then adds the `fts_only` label under its query line, so the
   reader knows the recalled memories may be a bit less accurate (§5.6.3).
3. **Fusion.** With `FUSE_FTS = 0.3`:
   - a row in the FTS set that has a vector: `relevance = 0.3 × rel_fts +
     0.7 × rel_vec`; `matched = "both"` when `rel_vec >= 0.3`, else `"fts"`;
   - a row in the FTS set without a vector (embedded later, or its embed
     failed), and every row when the vector pass did not run or failed:
     `relevance = rel_fts`, `matched = "fts"`. Known bias: in a partly
     embedded lake such a row can outrank an embedded row with the same
     lexical match whose `rel_vec` is below its `rel_fts`. It is kept
     because no neutral `rel_vec` exists to impute: the median is
     `rel_vec = 0` (which would bury every unembedded row at
     `0.3 × rel_fts`), and `rel_vec = rel_fts` is this rule. `embed_missing`
     (§4.8) closes the gap;
   - a row outside the FTS set enters only with `rel_vec >= 0.3`:
     `relevance = 0.7 × rel_vec`, or `rel_vec` alone when the FTS set is
     empty (no token survived step 1, or nothing matched); `matched =
     "vector"`.

   (Decision, AAA phase 2: the earlier `max(rel_fts, rel_vec)` let a row that
   only one side liked outrank a row both sides ranked second. The weight was
   chosen on the TUNE half of the eval's probes. A TUNE-only rerun on the final ranking
   covers 0.0 to 0.5: 0.0 to 0.2 score
   lower, and 0.4 is +0.005 TUNE recall@1 over 0.3 at the cost of one TUNE
   stale row above its answer; 0.3 is kept.)
4. **Floor.** Drop rows with `relevance < min_relevance`.
5. **Noise.** Drop rows that match a hard rule (§5.4), unless `noise=False`.

The vector pass needs every filtered row's vector. A long-lived
implementation holds the matrix in memory keyed on `meta.vectors_gen`,
evaluates the filters in SQL first, and dots only the surviving rows; a
fresh process either pays the load (about 80 MB at 40,000 × 512 float32)
or keeps the §3.4 sidecar. `numpy` is imported only when a vector pass
runs. Candidates are scored from the columns the candidate SQL returns
plus one grouped query over `engagements` (`target_id IN (...)`, live
engagement rows only) for the valence sums; tags and edges are
batch-loaded once for the rows that survive to the cut (§3.2). The same
`target_id`-keyed, live-only shape backs the reverse fetch that populates
`refuted_by` on the rows that survive to the cut: one grouped query over
`engagements` (`target_id IN (...)`, `kind='refute'`, live refuter rows),
joined to the refuter `deltas` for source/timestamp/note. Recall pays one
extra batched query for the ≤`limit` survivors, never one per hit.

### 5.3 Score

For each candidate, with `now` the wall clock at the call:

```
seen      = timestamp, or for a consolidated row (container, mood, crystal, sediment)
            the newest timestamp among the non-consolidated rows it rests on
            (derived_from, walked through consolidated parents); its own
            timestamp when none resolves
age_s     = max(0, now - seen)                              # seconds
recency   = 0.5 + 0.5 * 0.5 ** (age_s / half_life_s)         # RECENCY_FLOOR 0.5; half_life default 30 days
score_sum = Σ over engagements e with e.target_id == id:
              +1.0 if e.kind == 'affirm'
              -1.0 if e.kind == 'refute'
              +0.25 if e.kind == 'reply'
valence   = 1.0 + min(+0.30, 0.05 * score_sum)   if score_sum >= 0   # VALENCE_LIFT=0.05, cap +0.30
          = 1.0 - min(+0.70, 0.50 * -score_sum)  if score_sum <  0   # VALENCE_REFUTE_DROP=0.50, floor 0.30
noise     = 1 / 1.20 if 0 < len(content.strip()) < soft_chars else 1.0   # soft rule, query present only; soft_chars default 24; 1.0 when noise=False or the row is exempt (§5.4)
boost     = 1.0      if kind == 'container' and meta.fallback is set (an extractive session
                       container quotes its rows, §6.1; it earns no summary boost)
          = 1 / 0.85 if kind == 'container' and level >= 1
          = 1 / 0.92 if kind == 'container' and level == 0
          = 1 / 0.92 if kind == 'sediment' and meta.grounding == 'external'
          = 1.0      if kind == 'sediment'  (self-referential, or an unlabelled legacy row)
          = 1.0      otherwise
          → forced to 1.0, overriding the above, when the row transitively derives from a
            live-refuted or superseded row (§5.3 correction propagation: a corrected premise
            suspends the boost)
question  = 1 / 1.5 if kind is null and the stripped content ends with '?' and holds no
            earlier sentence break ('.', '!' or '?' followed by whitespace, or a newline);
            query present only
superseded = 0.50 if a live supersession link names the row as `old`, else 1.0  # SUPERSEDE_DROP, once
score     = relevance * recency * valence * superseded * noise * boost * question
          → an affirm or reply engagement row whose target is also a candidate scores at
            most the target's score, itself capped first when it is such a row, and sorts
            directly after it (query present only)
          → likewise a superseded row whose newest superseder among the candidates is present
            scores at most that row's score and sorts directly after it (query present only)
          → likewise a `lake:stance` row not so capped, under the best-scoring of its own
            derived_from rows among the candidates (§6.3.1; query present only)
```

Notes on each factor:

- **Recency.** The brief's half-life decay, with a floor of 0.5 (decision,
  AAA phase 2; it was 0.1). Recency breaks near-ties between rows of similar
  relevance; it does not override relevance. At 30 days the factor is 0.75,
  at 90 days 0.5625, at one year 0.5001 (`0.5 + 0.5 × 0.5^(365/30) = 0.5 +
  0.5 × 0.000218`). With the 0.1 floor a 30-day gap cost ×0.55, more than
  most relevance gaps, and on the eval's seeded world the newest same-topic
  chatter took rank 1 over the answer in 22 of 67 misses. The floor was
  first picked from 0.3 to 0.6 with guardrails counted over all probes,
  HOLDOUT included; the TUNE-only rerun
  still picks 0.5 for FTS, where 0.6 and 0.7 cut TUNE correction recall@1
  from 0.75 to 0.5 on the seeded world, because recency is still what keeps
  a correction above the value it replaced. For hybrid alone the same rule
  picks 0.7 (+0.015 TUNE recall@1, one more stale row above its answer);
  one floor serves both, so 0.5 stays. The cost is visible: with FTS, the
  stale value of a corrected fact ranks 1st to 3rd on 5 of the 7 correction
  probes, and 1st on one of them. Explicit supersession (below, phase 3)
  now takes that job over for the corrections consolidation links; raising
  the floor is a separate, later decision.
  With `recency=False` the factor is 1.0 for every row and `Hit.recency`
  still reports the computed value.
- **Evidence time.** A consolidated row is as old as what it summarises.
  Consolidation writes a container, mood, crystal, or sediment at the moment
  the pass runs, so with its own timestamp a summary of a month-old session
  had recency 1.0 and, with the container boost, outranked the rows it cites
  whenever its relevance was above about 0.4 of theirs (on the eval's
  consolidated lakes, 31 to 42 misses per lake). `seen` walks `derived_from`
  down through consolidated parents to the first non-consolidated rows (one
  recursive query for the candidates, §3.2 indexes) and takes the newest of
  their timestamps; a dangling or empty ancestry keeps the row's own
  timestamp. `Hit.recency` reports this value. (The eval's consolidated
  lakes were built by one backfill pass, so most summary rows carry the
  eval clock itself, 9 to 33 per lake; the measured gain is larger than a
  nightly per-session timer would see.) Engagement rows keep their own
  timestamp: an engagement is a new judgement, not a summary.
- **Questions.** A plain row that only asks ("what port is the db on
  again?") restates the query and answers nothing, and both FTS and vectors
  rank it near the top for the same question asked later. It is scored
  ÷1.5, never excluded; a row that states something before its question
  (`"The db moved. Which port now?"`) is not affected, nor is any row with a
  `kind`. 1.5 was chosen on the TUNE probes from 1.2, 1.5 and 2.0. The
  remember probes cannot see its cost (none expects a question row): asked
  for one of the 37 question rows of the seeded world by its own words,
  recall@1 falls from 0.43 to 0.30 (FTS) and 0.41 to 0.19 (hybrid); every
  one stays in the top 10.
- **Engagement under its target.** An engagement row with `snapshot=True`
  quotes its target (§4.6), so it matches the same queries and, being newer,
  used to outrank the row it judges. When both are candidates an affirm or
  reply row's score is capped at the target's and it sorts directly after it
  (the sort's second key: the number of caps above the row); it is never
  removed, and when its target is not a candidate it keeps its own score.
  A chain resolves root first: an affirm of a reply is capped at the reply's
  capped score, so it sorts after the reply and neither outranks the root. A refute row is not capped: it carries
  the correction, and capping it at a target its own refute has halved
  would bury the correction under the stale row it corrects (measured: a
  deep_recall plan lost the refute of a superseded deploy decision).
- **Valence.** The mapping is asymmetric about a neutral net. `score_sum`
  is the same signed sum as before (affirm +1, refute −1, reply +0.25 per
  live engagement row, counted once, no top-N, no cap; `derived_from` and
  the engagement row's own content contribute nothing). When the net is
  zero or positive the lift is the brief's +5%/point capped at +30% — one
  affirm ×1.05, reply ×1.0125, six net affirms ×1.30 — unchanged. When the
  net is negative the drop is steep: the first net refute gives ×0.50, and
  further net refutes floor the multiplier at ×0.30. This is deliberate: a
  refutation reflects the model being sure, so one confident refute must
  outweigh the strongest boost (a level-≥1 container's 1/0.85 ≈ 1.176) — a
  refuted container scores 0.50 × 1.176 = 0.588, below an un-refuted raw row (1.0)
  and below the boosted container it would otherwise sit above. A net-zero
  row (equal affirms and refutes) stays ×1.0. The floor of 0.30 (above the
  recency floor 0.5) means a heavily-refuted row is demoted decisively but
  never erased or hidden: it stays recallable, and its refutation is
  surfaced as receipts on every read (§4.5, §5.6.2), per the never-delete
  rule (§12). Mark and re-rank only; no refute ever enters a WHERE/exclude
  clause. A refuted perfect match moves down decisively — below an
  un-refuted raw row and beneath the container/sediment boost (the ±0.30
  symmetric cap that let a boost outlast a single refute is replaced).
- **Noise.** The length penalty (×1.20 on distance becomes ÷1.20 on
  score). The centroid term is dropped per the brief.
- **Boost.** The deep-path provenance bonus (brief silent):
  consolidated rows carry more per token than the moments they summarise.
  Sediment carries weaker standing than a validated container. It is written on the
  swallowed-failure read path (§6.5), where a broken model must never break a recall, so
  it never earns the level≥1 container's 1/0.85. A sediment distilled entirely from the
  lake's own prior output (`meta.grounding == 'self-referential'`, §6.5) earns no boost at
  all; one that read at least one external source earns the level-0 container's 1/0.92. A
  sediment row with no `meta.grounding` key earns no boost — the safe default, so it is
  never over-privileged. This covers both pre-grounding legacy read-path rows and every
  host-written sediment (`write(kind='sediment')`, engage/remote/plugin), which carry no
  `meta.grounding` and so drop from the old flat 1/0.85 to 1.0 going forward — intended.
- **Correction propagation (§5.3).** A refute demotes the row it targets, but a summary built over
  that row would otherwise keep its boost and go on outranking the evidence it now misreads. So a
  row whose `derived_from` ancestry reaches any live-refuted row has its boost forced to 1.0 for as
  long as the refute stands, and a user-facing read attaches a `rests_on_refuted` receipt naming the
  corrected ancestor(s) (§4.5, §5.6.2). The row is never deleted or hidden, and the boost returns if
  the refute is later withdrawn. The set is one forward walk over `derived_from` from the live
  refutes, gated by an EXISTS check, so a lake with no refutations pays almost nothing. This reaches
  every scored descendant; the injected crystal, which bypasses ranking, is reached by §6.4 instead.
  Superseded rows are roots too (a summary built on the stale value loses its boost), except that
  the walk never enters a container that asserts one of the links: it records the change. The
  `rests_on_refuted` receipt still names live-refuted ancestors only.
- **Supersession (AAA phase 3, resolves §11 item 17).** A row that states a new value should rank
  above the row whose value it replaces, without hiding it. A *link* `{new, old, old_value,
  new_value}` lives in the `meta.supersedes` list of a container the library wrote (`source =
  'lake:container'`; §6.1 detection writes them; a host-written container's `meta.supersedes` is
  ignored). A link is live while its container is live and not live-refuted, `new` is in that
  container's `derived_from` (detection always links a row of the part it names; decision, AAA phase
  3 review: an imported container could otherwise link two rows it never cites), `old` and `new`
  both exist and are live host rows (`kind` NULL), `new` is not live-refuted, and `old.timestamp <
  new.timestamp` (so chains cannot cycle); entries that are not objects of four strings, or past the
  16th, are ignored. A superseded row scores ×0.50 once however many links name it (separate from
  valence: `Hit.valence` stays engagement-only, and a refute stacks, floor 0.50 × 0.30). When its
  newest live superseder that is a candidate is present it is also capped at that row's score and
  sorts directly after it (the engagement cap machinery; a chain A → B → C resolves C, B, A). It is
  never excluded: mark and re-rank only, the row stays in `get`, `lineage`, `context` and every
  candidate set, and a user-facing read attaches the `superseded_by` receipt. Withdrawal is
  refuting the container (or the new row); nothing is deleted. Storage is additive: no DDL, no
  `schema_version` change, no new engagement kind (the `engagements.kind` CHECK is untouched); older
  libraries ignore the meta key. Cost: one lookup of the §3.6 `has_supersedes` meta key per scoring
  call and receipt-bearing read while the lake has never held a link (constant: 0.00 ms with 20,000
  library containers, where the earlier `EXISTS … meta LIKE` gate took 7–8 ms; decision); once the key is set, one scan of the link-carrying
  containers (`kind = 'container' AND source = 'lake:container' AND meta LIKE '%"supersedes"%'`, JSON
  parsed in Python, JSON1 not required; 8.4 ms at 20,000 containers, so it grows with the number of
  library containers) plus one query each for their `derived_from`, the referenced rows and their
  refutes. Measured on the eval's base.lake with one scripted link row
  per ground-truth correction (13 links): TUNE recall@1 unchanged (FTS 0.6977, hybrid
  0.7791); the correction probes' median stale-row rank 3 → 8 (FTS) and 7 → 21, out of the top 20
  (hybrid); R141 expected/stale rank 2/1 → 1/8 (FTS); HOLDOUT recall@1 FTS 0.6143 unchanged (R141
  gained, R133 lost: its expected answer is the stale 5433 row, filtered to that session's tag),
  hybrid 0.6857 → 0.7.
- **Stance links (AAA phase 4, §6.3.1).** Stance rows supersede each other by slug, with no container:
  every older live `lake:stance` row tagged `stance:<slug>` is `old` in a link to every newer live one of
  the same slug (newer by `(timestamp, seq)`), with `Supersession(new id, by = new id, old position, new
  position)`; when the newer row is a drop (`retired`), `new_value` is `no longer held`, so the receipt never
  repeats the retired position as its replacement. They are links like any other for scoring (×0.50, the cap under the newest superseder that is
  a candidate), for the `superseded_by` receipt and for correction propagation. The scan runs only when the
  §3.6 `has_stances` key is set; `supersessions()` reads `has_supersedes` and `has_stances` in one meta query,
  so a lake with neither still pays one lookup.
- **A stance under its evidence (AAA phase 4).** Stance rows are sediment, and an external one (§6.3.1 always
  writes `meta.grounding`) earns the sediment boost, so a stance restating a row outranked it: with one oracle
  stance row per inventory position on the base lakes of the eval worlds, a stance took rank 1 from its own
  evidence on 3 remember probes (B R087, C R021, C R087; FTS). So a stance row that is not capped under a
  superseder is capped at the score of the best-scoring of its `derived_from` rows among the candidates and
  sorts directly after it (the engagement cap machinery); alone it keeps its boosted score. With the cap no
  stance row takes rank 1 from an answer; recall@1 A 0.6603 → 0.6603, B 0.5705 → 0.5577, C 0.6282 → 0.6282,
  and B's net loss (3 probes lost, 1 gained, none of them to a stance row) is corpus statistics: the same
  texts written as plain rows lose the same 3 and gain the same 1.

Sort by `score` descending, comparing at 6 decimal places; ties put an
affirm or reply row capped under its target after the uncapped rows, then
`timestamp` descending, then `id` ascending. Return the first `limit`.

### 5.4 Noise rules

Applied when a `query` is present in `recall()` and `context()`, in the plan
steps `search`, `bridge`, and `chain`, and (hard rules only) to the
candidates of a default container run and of a mood run. Never applied to
filter-only recall, `filter`, set operations, `neighbors`, `timeline`, or
`aggregate`. Every call that applies them takes `noise: bool = True`;
`noise=False` skips them for that call.

The thresholds and the phrase list come from the Lake's `NoiseRules`
(§4.1): `drop_chars` (default 10) is the N2 bound, `soft_chars` (default
24) the soft-rule bound, `phrases` the N3 list, and rows whose `source` is
in `exempt_sources` skip every rule. A host whose short rows are the
signal (a child's answers `Max`, `the dog`, `yes`) sets
`Lake(noise=NoiseRules(exempt_sources=["reader"]))` or
`NoiseRules(drop_chars=0, soft_chars=0)`; the defaults below assume the
brief's case, where a short row is a tool echo.

Exempt from every rule: rows with a non-null `kind`; rows with a
`media_hash`; rows from an exempt source.

Hard drop, with `text = content.strip()`:

- **N1** `text == ""`.
- **N2** `len(text) < drop_chars` (strict; at the default a 10-character
  row survives).
- **N3** `text.lower()` equals one of these 55 strings exactly (only
  the five of length ≥ 10 add coverage beyond N2):

  ```
  hey, ok, okay, yeah, yep, yes, no, nope, sure, wait, stop, huh, hmm, uh, um,
  lol, haha, what, what's up, hi, hello, hello!, howdy, hola, thanks, ty, nvm,
  nevermind, actually, test, testing, please, gold, confirmed, approved, got it,
  agreed, exactly, perfect, great, cool, done, noted, go ahead, sure go, sure, go,
  do it, do it again, lgtm, looks good, sounds good, ship it, merge it,
  that's not what i wanted, no not that
  ```

  (`sure, go` is one entry containing a comma.)
- **N4** Opaque id list (decision; the brief's "tool-use id list"). Split
  `text` on `[\s,;]+`, drop empty pieces; if at least one piece remains and
  at least 80 % of the pieces match
  `^(?:[a-z]{2,10}_[A-Za-z0-9_-]{8,}|[0-9a-f]{12,}|[A-Za-z0-9+/=_-]{32,})$`,
  drop.
- **N5** Bare payload (decision; the brief's "bare hook payload"). If
  `json.loads(text)` succeeds and yields an object or array, drop when
  either (a) it is an object with any key among `hook_event_name`,
  `session_id`, `transcript_path`, `tool_use_id`, `tool_name`,
  `tool_input`, `tool_response`, `cwd`; or (b) no string value anywhere in
  it (recursively) is 40 characters or longer.

  Consequence of (b): a small structured row such as
  `{"entity":"light.x","state":"on"}` is never returned by a query recall;
  it is reachable by filter recall, strips, and `neighbors`. Hosts that
  want machine events findable by text put a sentence in `content` and the
  structure in `meta`.

Soft: `0 < len(text) < soft_chars` divides the score by 1.20 (§5.3). The
`noise` switch covers both tiers: `noise=False` skips the hard drops of
§5.2 step 5 and sets the soft factor to 1.0, and the three exemptions
above (non-null `kind`, `media_hash`, exempt source) skip the soft rule
as well. There is no way to apply one tier without the other.

No role filtering is built in. Hosts that want to hide their own echo pass
`exclude_sources` or `exclude_tags` to `recall()` and `context()`, or a
plan `tags_exclude`.

### 5.5 Plan DSL

A plan is an ordered list of step dicts. Each has a unique string `id`,
exactly one action key, and optional parameters. Steps run in order; a step
may reference only earlier steps. `lake.plan(steps)` returns every step's
result; `recall(plan=steps)` returns the last step's hits (a `PlanError` if
the last step is an `aggregate`; the flattened real rows if it is a
`timeline`).

**Step fields**

| field | type | default | read by |
|---|---|---|---|
| `id` | str | required | all |
| `search` | str | | action |
| `filter` | dict | | action; keys are the common filters below |
| `intersect`, `union`, `diff`, `bridge` | list[str], length ≥ 2 | | action |
| `chain`, `aggregate`, `neighbors`, `timeline` | str | | action |
| `tags_include` (alias `tags`) | list[str] | | search, filter, chain, bridge, neighbors, timeline |
| `tags_exclude` | list[str] | | search, filter, chain, bridge, neighbors, timeline |
| `any_tags` | list[str] | | search, filter, chain, bridge, neighbors, timeline |
| `source` | str or list[str] | | search, filter, chain, bridge, neighbors, timeline |
| `exclude_sources` | list[str] | | search, filter, chain, bridge, neighbors, timeline |
| `kind` | str or list[str] | | search, filter, chain, bridge |
| `has_media` | bool | | search, filter, chain, bridge (`true` → `media_hash IS NOT NULL`, `false` → `IS NULL`) |
| `since` (alias `time_start`), `until` (alias `time_end`) | TimeSpec | | search, filter, chain, bridge |
| `limit` | int ≥ 1 | 100 | every step except aggregate; timeline also accepts 0 for no cap |
| `group_by` | `hour`, `day`, `week`, `month`, `tag`, `source`, `kind` | `week` | aggregate |
| `radius_minutes` | int ≥ 1 | 30 | neighbors, timeline |
| `source_match` | bool | true | neighbors |
| `limit_per_seed` | int ≥ 1 | 6 | neighbors |
| `max_per_side` | int ≥ 1 | 15 | timeline |
| `gap_minutes` | int ≥ 1 | 30 | timeline |
| `merge_gap_seconds` | int ≥ 0 | 300 | timeline |
| `collapse_sources` | list[str] | the Lake's `source:` automation rules (`automation_sources`) | timeline |

Unknown keys (`relation`, `radii`, `metric`, `modality`, `provenance`) are
ignored, so a plan that carries them still runs.
`tags_exclude` is the §5.1 `exclude_tags` condition. For `filter`, the keys
of the dict and the top-level fields are both applied. The
plan-level `filters` mapping (`plan(steps, filters=...)`,
`recall(plan=..., filters=...)`) holds the same keys and is ANDed into every
step that reads them, so one scope covers a whole plan (decision; the
per-step fields stay for plans written by a model).

**Validation** (`PlanError`, before any step runs), messages verbatim:

- `Duplicate step id: '<id>'`
- `Step '<id>' has no action — must set exactly one of: search, filter, intersect, union, diff, bridge, aggregate, chain, neighbors, timeline`
- `Step '<id>' sets more than one action` (decision)
- `Step '<id>' references '<ref>' which is not defined (or comes later in the plan)`
- `Step '<id>' needs at least two references` (decision)
- `Step '<id>' references aggregate step '<ref>' which produces buckets, not deltas` (decision)
- `Step '<id>' has unknown group_by '<value>'` (decision)
- any `ValueError` from parsing `since`/`until`

**Semantics per step.** Every scored step computes a per-step `relevance`
and then the §5.3 score with that relevance (one scoring function, as the
ranking notes ask). Expired rows are excluded everywhere.

- `search`: §5.2 to §5.4 over the step's filters with the step's `limit`.
  Output sorted by score. `matched` as in recall. The §5.2 pools cap the
  output at 500 rows without `embed` and 1000 with, whatever `limit` says.
- `filter`: filters only, newest first by `(timestamp DESC, seq DESC)`,
  `relevance = 1.0`, no noise rules, `matched = "filter"`, cut at `limit`.
- `intersect`, `union`, `diff`: n-ary over ids (decision). `keep = A₁ ∩ A₂ ∩ …`, `A₁ ∪ A₂ ∪ …`, or
  `A₁ − (A₂ ∪ …)`. Walk the refs in order and their hits in order; the first
  occurrence of an id fixes its position; the `Hit` kept for an id is the
  one with the highest `score` across refs (decision), then cut at `limit`. Empty inputs give an empty result, no
  warning.
- `bridge`: rows close to both inputs. Empty input: warning
  `step '<id>' skipped: input '<ref>' is empty` (first empty ref) and an
  empty result. With `embed`: `c₁`, `c₂` = normalised means of the input
  rows' stored vectors (rows without vectors ignored; if either side has
  none, warning `step '<id>' skipped: no embeddings in '<ref>'`);
  for every filtered live row with a vector whose id is in neither input,
  `raw = min(c₁·v, c₂·v)`; keep `raw > 0`; `relevance` by the §5.2 min-max
  rule over the kept rows. Without `embed`: build one FTS group per input
  from the 32 most frequent tokens (≥ 4 characters, `[^\W_]+`,
  lower-cased, not in the stoplist, each double-quoted) across that
  input's content; MATCH `("t₁" OR "t₂" OR …) AND ("u₁" OR …)`; input ids
  excluded from the result as in the vector form; `relevance` as in §5.2
  (decision: FTS approximation instead of an empty result). The step's
  §5.1 filters apply in both forms (decision: otherwise a bridge step could
  leak sources the search steps had excluded). Noise
  rules, score, sort, cut at `min(limit, 1000)`. `matched = "bridge"`.
- `chain`: search outward from an input's centroid. Empty input: the same
  warning. With `embed`: `c` = normalised mean of input vectors (fetched
  from `vectors` by id; none → `no embeddings` warning); candidates are
  filtered live rows with a vector, `cos = c·v > 0`, top 500, min-max
  normalised, the step's filters applied. Without `embed`: the 32-token FTS
  query of the input's content, quoted as in `bridge`. Input ids are
  excluded from the output (decision). Noise rules, score, sort, `limit`. `matched =
  "chain"`.
- `aggregate`: buckets over the referenced hits. Key by `group_by`:
  `hour` → `timestamp[:13] + ":00"` rendered as `YYYY-MM-DD HH:00`; `day`
  → `YYYY-MM-DD`; `week` → ISO `YYYY-Www`; `month` → `YYYY-MM`; `tag` →
  one bucket per tag (a row lands in every tag's bucket; untagged rows land
  nowhere); `source` → the source; `kind` → the kind or `plain`. Buckets
  sorted by key; `delta_ids` keep input order. No `limit`.
- `neighbors`: for each seed hit, live rows with `timestamp` within
  `± radius_minutes` (inclusive), excluding every seed id, passing the
  step's `source`, `exclude_sources`, `tags_include`, `tags_exclude`, and
  `any_tags`, restricted to the seed's `source` when `source_match`,
  ordered by absolute gap, at most `limit_per_seed` per seed. Dedupe across
  seeds keeping the smallest gap. `relevance = 1 − gap_s / (radius_minutes ×
  60)`; `score = relevance` (no recency, valence, noise, or boost: a time
  relation is not a relevance judgement). Sort by score descending, then
  timestamp, cut at `limit`. `matched = "neighbor"`. Seeds are absent from
  the output; union them back if wanted.
- `timeline`: strips around each seed (§5.6, steps T1 to T7) with the
  step's parameters and the step's `source`, `exclude_sources`,
  `tags_include`, `tags_exclude`, and `any_tags` applied to the T1 fetch.
  Every strip T1 to T6 produce is kept, ordered by `t_start`, and cut at
  the step's `limit` (0 for no cap). Output is `timelines`; for downstream
  steps the step resolves to the flattened real rows of every strip,
  deduped, in strip order then time order, with `relevance = 1.0`, `score
  = recency × valence`, `matched = "timeline"`.

Warnings are strings on `PlanResult.warnings`; a skipped step still appears
with an empty result.

**The plan fixture.** The three worked examples and the §10 plan tests run
against this lake. `now` is frozen at `2026-05-06T10:00:00.000Z` (r10's
timestamp) and the Lake is opened with `recency_half_life="3650d"` so that
recency stays within 0.0007 of 1.0 for every row and the stipulated
relevances decide the order; no row has an engagement, none is expired,
none has media; `embed` is absent. Ids are written as `r1` … `r12` for
reading; the test fixture assigns real 12-hex ids and maps them.

| id | timestamp | source | tags | content (length) |
|---|---|---|---|---|
| r1 | 2026-04-29T14:00:00.000Z | claude-code | migration, team | "Discussed the postgres migration plan for Thursday with the team" (63) |
| r2 | 2026-04-29T14:00:20.000Z | claude-code | | "ok" (2) |
| r3 | 2026-04-29T14:00:40.000Z | claude-code | migration | "We agreed to run the migration at 09:00 Thursday after the backup finishes" (74) |
| r4 | 2026-04-29T14:01:00.000Z | agent-heartbeat | | "heartbeat" (9) |
| r5 | 2026-04-29T14:01:05.000Z | agent-heartbeat | | "heartbeat" (9) |
| r6 | 2026-04-29T14:01:10.000Z | agent-heartbeat | | "heartbeat" (9) |
| r7 | 2026-04-29T14:02:00.000Z | claude-code | backup | "Backup finishes around 08:30 so 09:00 gives us margin" (53) |
| r8 | 2026-04-29T15:30:00.000Z | fathom-chat | nova, kitchen, fathom-chat, chat:sunday | "Nova stretched mozzarella in the kitchen tonight" (48) |
| r9 | 2026-04-29T15:30:30.000Z | fathom-chat | fathom-chat, chat:sunday | "short remark" (12) |
| r10 | 2026-05-06T10:00:00.000Z | claude-code | migration, backup | "Migration done; the backup took 40 minutes" (42) |
| r11 | 2026-04-20T09:00:00.000Z | vault/fathom | vault | "## Migration runbook" (20) |
| r12 | 2026-04-20T09:00:00.400Z | vault/fathom | vault | "## Rollback steps" (17) |

r4, r5, r6 are three rows with identical content, source, and (empty) tag
set; the fixture writes them with `dedupe=False`. Recency at the frozen
clock and half-life: r10 1.0; r1 0.999352; r3 0.999352; r7 0.999352; r9
0.999358 (r9 is younger than r7 by 5,310 s, which separates them at the
sixth decimal); the rest are not needed below.

**Worked example 1.** Stipulated `rel_fts` after normalisation: r1 1.00,
r2 0.90, r3 0.80, r10 0.70, r9 0.60, r7 0.50; every other row is absent
from the FTS set.

Plan:

```json
[{"id": "a", "search": "migration backup plan", "limit": 20},
 {"id": "b", "filter": {"tags_include": ["backup"]}},
 {"id": "only_plan", "diff": ["a", "b"]},
 {"id": "top3", "union": ["b", "a"], "limit": 3}]
```

- `a`: r2 is dropped by N2 and N3. r9 (12 characters) takes the soft rule:
  0.60 / 1.20 = 0.50. Scores: r1 1.00 × 0.999352 = 0.999352; r3 0.80 ×
  0.999352 = 0.799481; r10 0.70 × 1.0 = 0.700000; r9 0.50 × 0.999358 =
  0.499679; r7 0.50 × 0.999352 = 0.499676. r9 precedes r7 because its
  score is higher at the sixth decimal (and it is newer, which the tie
  rule would also choose). Result: r1, r3, r10, r9, r7.
- `b`: rows tagged `backup`, newest first: r10, r7 (`score` = recency ×
  valence: 1.0 and 0.999352).
- `only_plan`: `{r1, r3, r10, r9, r7} − {r10, r7}` → r1, r3, r9 in `a`'s
  order.
- `top3`: walk `b` then `a`: positions r10, r7, r1, r3, r9; r10 keeps the
  higher score (1.0 from `b` versus 0.70 from `a`); cut to r10, r7, r1.

**Worked example 2.** Stipulated: the search returns r7 alone.

```json
[{"id": "a", "search": "backup margin 09:00", "limit": 1},
 {"id": "ctx", "neighbors": "a", "radius_minutes": 5, "limit_per_seed": 3},
 {"id": "all", "union": ["a", "ctx"]},
 {"id": "bytag", "aggregate": "all", "group_by": "tag"}]
```

- `a`: r7 (the tokens are `backup`, `margin`, `09` and `00`; the search is
  stipulated, so only r7 comes back).
- `ctx`: window 13:57:00 to 14:07:00, same source, excluding r7: r3 (gap
  80 s), r2 (100 s), r1 (120 s); heartbeats fail `source_match`. Relevance
  1 − 80/300 = 0.7333, 0.6667, 0.6000. r2 is present: neighbors apply no
  noise rule.
- `all`: r7, r3, r2, r1.
- `bytag`: `backup: [r7]`, `migration: [r3, r1]`, `team: [r1]`, sorted by
  key; r2 appears in no bucket.

**Worked example 3.** Stipulated: step `a` returns r1, r3, r10, r7, r11,
r12, r9, r8 in that order.

```json
[{"id": "a", "search": "migration backup plan", "limit": 20},
 {"id": "byweek", "aggregate": "a", "group_by": "week"},
 {"id": "bytag", "aggregate": "a", "group_by": "tag", "metric": "centroid"}]
```

- `byweek`: ISO weeks of the row timestamps: r11, r12 → `2026-W17`; r1,
  r3, r7, r9, r8 → `2026-W18`; r10 → `2026-W19`. Buckets sorted by key,
  `delta_ids` in `a`'s order:

  ```
  2026-W17  count 2  [r11, r12]
  2026-W18  count 5  [r1, r3, r7, r9, r8]
  2026-W19  count 1  [r10]
  ```

- `bytag`: `metric` is ignored. One bucket per distinct tag, a row in every
  bucket for each of its tags, keys sorted as strings:

  ```
  backup       count 2  [r10, r7]
  chat:sunday  count 2  [r9, r8]
  fathom-chat  count 2  [r9, r8]
  kitchen      count 1  [r8]
  migration    count 3  [r1, r3, r10]
  nova         count 1  [r8]
  team         count 1  [r1]
  vault        count 2  [r11, r12]
  ```

### 5.6 context()

`context(query, *, budget=8000, limit=30, ...)` renders one prompt block;
`context_blocks()` returns the parts (§4.5). With an empty or `None` query
it returns only the crystal block (block 1 below, cut at `budget` rather
than at `budget × 0.4`), or `""` when there is no crystal or
`crystal=False`. Otherwise:

1. `hits = recall(query, limit=limit, ...)` with the call's filters,
   `noise`, `recency`, and `min_relevance`. Unless the call passes `kind`,
   the anchor recall adds `kind NOT IN ('mood', 'crystal')` (decision: mood
   rows are JSON whose keys and vocabulary match many queries, both kinds
   are noise-exempt, and a mood anchoring a strip renders a line the host
   may not want to claim; `recall(kind="mood")` still returns them, and
   they still appear as ambient rows in strips). `N = len(hits)`. If `N ==
   0` the result is the same as for an empty query: the crystal block
   alone, or `""` (decision: a
   `You remember 0 things` header on every unrelated prompt is noise).
2. **Containers active in this recall**: the `kind = "container"` rows that
   are hits, plus the containers reached by `cited_by(hit.id, depth=3)`
   from each hit (§4.5). The walk runs in the citing direction, from a row
   to the rows whose `derived_from` lists it, through rows of any kind (a
   sediment, an engagement, or a crystal that cites a hit is visited and
   its own citers come next), and collects every visited row whose `kind`
   is `container`. Expired rows are skipped and not walked through. A
   hit's own `derived_from` is never followed: block 4 answers "which
   consolidated rows cover this moment", and a host-written hit has no
   `derived_from` to follow, so `lineage()` would return nothing in the
   normal case. The list is the container hits in hit order, then the
   walked containers in first-seen order across hits in hit order, cut at
   60: a reverse index from a row to the rows citing it, walked breadth first
   to depth 3, at most 60 containers. Sort by `level` descending, then
   hits before non-hits in hit order, then `timestamp` descending. Empty
   when `containers=False`.
3. **Strips**: build timelines with the hits as seeds, the
   parameters `radius_minutes = 20`, `max_per_side = 6`, `gap_minutes =
   15`, `merge_gap_seconds = 300`, `collapse_sources` = the Lake's `source:`
   automation rules, and the
   call's row filters on the T1 fetch, using T1 to T7 below. T1 to T7
   produce every strip. Score each strip by the highest hit score among
   its anchors. Keep the max(12, ⌊budget · 12 / 8000⌋) highest-scoring
   strips (decision; 12 at any budget up to
   8000, more above it so a large budget can be filled) and order those by
   `t_start` ascending. Strips beyond that count toward block 6's `n`.
4. Render (§5.6.2) under the budget (§5.6.3).

#### 5.6.1 Strip construction (T1 to T7)

Given seeds (hits with a parseable timestamp):

- **T1** For each seed, fetch live rows with `timestamp` in
  `[seed − radius, seed + radius]` (inclusive) that pass the call's row
  filters (`source`, `exclude_sources`, `tags`, `any_tags`,
  `exclude_tags`; the anchor-only filters `kind`, `since`, `until` do not
  apply here), as two bounded queries: the nearest `fetch_per_side` rows at
  or before the seed's `(timestamp, seq)` and the nearest `fetch_per_side`
  rows after it, each ordered by absolute gap, `fetch_per_side = 40 ×
  max_per_side` (240 in `context()`; decision: a burst of hundreds of
  same-second rows near a seed would otherwise be materialised whole, and
  a fetch of this size still holds every row T3 to T5 can keep unless a
  single collapsed run is longer than the fetch, in which case the run's
  `count` is the fetched part). Merge, dedupe by id, order by `(timestamp,
  seq)`. The seed itself is included when it passes the filters. Tags,
  edges, and engagements for every fetched row are batch-loaded once per
  call.
- **T2** Anchor index = position of the seed's id, or, if the seed was
  filtered out, the row nearest the seed's timestamp. A seed with no rows is
  skipped.
- **T3** Gap trim: walk left from the anchor and stop before the first pair
  of consecutive rows whose gap exceeds `gap_minutes × 60` seconds
  (strict), walk right likewise. The window holds no internal silence
  longer than the gap.
- **T4** Same-second collapse (seed id protected): consecutive rows with the
  same `source` and the same `timestamp[:19]`, run length ≥ 2, containing no
  protected id, fold into one `CollapsedRun(source, count = run length,
  t_start = first.timestamp, t_end = last.timestamp)`.
- **T5** Source collapse (seed id protected): if `collapse_sources` (the
  step's, else the Lake's `source:` automation rules, §4.3) is
  non-empty, consecutive entries whose `source` is in it, run length ≥ 2,
  containing no protected id, fold into one `CollapsedRun` whose `count` is
  the sum of the entries' counts (a real row counts 1; decision: counting
  entries would under-report after T4). Then the side cap: keep
  at most `max_per_side` entries on each side of the anchor, nearest
  first, a `CollapsedRun` counting as one entry (decision: capping
  raw rows before collapsing would let six daemon rows fill a side and
  push the conversation around the anchor out of the window). The window
  is at most `2 × max_per_side + 1` entries.
- **T6** Window `t_start`/`t_end` = the first/last real row of the trimmed
  window. Sort windows by `t_start`; walk them; when `this.t_start −
  prev.t_end ≤ merge_gap_seconds` (overlaps are negative and always merge),
  fold `this` into `prev`: union anchors, union real rows deduped by id and
  re-sorted, recompute `t_start`/`t_end`, redo T4 and T5's collapses with
  all merged anchors protected. The side cap and gap trim are not
  re-applied, so a merged strip may exceed `2 × max_per_side + 1`
  entries.
- **T7** Strip `i` is `Timeline(id = f"tl_{i}", t_start, t_end, anchor_ids
  = sorted(anchors ∩ seed ids), rows)` where real rows are
  `TimelineRow(delta, is_anchor = id in anchors)`, numbered in `t_start`
  order. No cut happens here: the plan step cuts at its `limit` in
  `t_start` order, and `context()` selects by score (step 3).

#### 5.6.2 Line renderers

Helpers:

- `ts8(t) = t[11:19]` (`HH:MM:SS`); for a `CollapsedRun`, from `t_start`.
- `oneline(text, cap=130)`: strip; if it contains `<[a-zA-Z/][^>]*>`, remove
  tags and replace `&nbsp; &amp; &lt; &gt; &quot; &apos; &#nn;` with spaces;
  collapse whitespace runs to one space; if longer than `cap`, cut to
  `cap − 1` characters and append `…` (U+2026).
- `window(text, tokens, cap)`: an anchor's body. `oneline(text)` uncut; if it
  fits `cap` (`ANCHOR_CAP = 600`; the wide cap is `max(ANCHOR_CAP, budget × ANCHOR_CAP // 8000)`,
  1800 at 24000 and 3600 at 48000. The `WIDE_TOP = 2` best non-container hits are rendered at
  the wide cap before the §5.6.3 budget loop; after it, strips in rank order are re-rendered at
  the wide cap, each only while the text stays within budget. Every anchor wide before packing
  cut LongMemEval-S from 94.8% to 83.2%; leftover-only 93.8%; this rule 96.0%, GPT-4o judge
  94.8%), all of it; otherwise the `cap`-long stretch
  holding the most query-token hits (starting a third of a cap before a hit),
  with `…` at each cut end. The rows of the `user`/`assistant` and default rules
  use it for anchors; ambient rows keep `oneline(content)` (130). (LongMemEval-S,
  2026-09-28: 97% of the evidence rows in context were cut at 130 characters.)
- `marker = "▸" (U+25B8) if is_anchor else " "`.
- `idp = f"[{id[:12]}] " if is_anchor else ""` (anchors show ids so a model
  can cite them; ambient rows never do).
- `media = f" [Image attached: media_hash={media_hash}]" if media_hash else ""`.
- `src13 = (source or "?").ljust(13)[:13]`, always followed directly by `·`
  (U+00B7). A 13-character source such as `homeassistant` therefore reads
  `homeassistant·` with no space; a longer one is truncated.

Dispatch, first match wins:

| condition | line |
|---|---|
| `CollapsedRun` | `  {ts8(t_start)}  {source} × {count} (through {ts8(t_end)})` |
| `kind == "container"` | `{marker} {ts8}  {label}· [L{level} · {n} delta{s} · {id}] {title}{media}` where `label = "qa-marker".ljust(13)` if `level == 0` else `"container".ljust(13)` (decision), `n = len(derived_from)`, `s = ""` if `n == 1` else `"s"`, `title = meta.title` if present else `oneline(content)`. No `idp`; the id is in the badge. |
| `kind == "sediment"` | `{marker} {ts8}  sediment     · {idp}{first}{srcs}{media}` where `first` = content with newlines as spaces, split at the first `.`; if that exceeds 200 characters cut to 199 + `…`, else append `…` when it is shorter than the whole; `srcs = f" (from {n} sources)"` when `n = len(derived_from) > 0`, followed by ` (self-referential)` when `meta.grounding == "self-referential"` — the honest note, derived in code (§6.5). A `lake:stance` row then appends `labels.stance` (` · stance, {label}`): `retired` when its `meta.stance.retired` is set, `superseded` when it carries a `superseded_by` receipt or is not the live head of its slug, else its §6.3.1 confidence label, computed by `context()` at render time for the stance rows it renders. |
| `kind == "mood"` | `{marker} {ts8}  mood         · {idp}feeling: {state}{media}` where `state` is the `feeling:` tag suffix, else `state` from the JSON content, else `oneline(content, 80)`. |
| `kind == "engagement"` | `{marker} {ts8}  {src13}· {idp}[{verb} {target_id}] {oneline(text)}{media}` with `verb` ∈ `affirms`, `refutes`, `replies to` and `text` = the engagement's `note` when non-empty, else its footer line (`— {source} · {ts} · {id}`, the last line starting `> —` of the snapshot, without the `> `). The quoted body is never printed: under a `source` column it would read as the engager's words (decision: new renderer). |
| tags contain `user` or `assistant` | `{marker} {ts8}  {src13}·{role} {idp}{oneline(content)}{media}` where `role = labels.user_role` (default `" user:"`) if `user` in tags, else `labels.assistant_role` (default `" assistant:"`). |
| otherwise | `{marker} {ts8}  {src13}· {idp}{oneline(content)}{media}` |

Crystal rows render through the default line.

**Refuted marker.** Any delta-bearing line (every row except a
`CollapsedRun`) whose `delta.refuted_by` is non-empty has a suffix appended
after `{media}`: `labels.refuted` formatted with `n = len(refuted_by)` and
`detail = f"{r.source} · {oneline(r.note, 60)}"` for the newest refuter `r`
(its `note`; when it has none, `detail` falls back to `r.source` alone).
Default label: `" ⟵ refuted ×{n} ({detail})"`. The suffix appears only when
the row is actually refuted, so an unrefuted context renders byte-for-byte
as before.

**Rests-on-refuted marker.** In the same suffix, after any refuted marker, a
line whose `delta.rests_on_refuted` is non-empty appends `labels.rests_on_refuted`
formatted with `n = len(rests_on_refuted)` — the count of live-refuted rows this
row derives from (§5.3). It lets a reader (and the model) see that a recalled
summary rests on a memory since corrected, without the summary being hidden.
Default label: `" ⟵ rests on ×{n} corrected"`. Empty and silent otherwise.

**Superseded marker.** In the same suffix, after the two above, a line whose
`delta.superseded_by` is non-empty appends `labels.superseded` formatted with
`id` = the newest superseder's id (12 characters, so `get`/`lineage` take it
as is) and `value = oneline(new_value, 60)`. Default label:
`" ⟵ superseded by {id}: {value}"`. The MCP `remember` tool appends the same
text to a superseded hit's line (after the content, so the line still
parses), and its `lineage` tool, called on a container, lists each pair it
asserts as `supersedes: {old} ({old_value}) -> {new} ({new_value})`.

#### 5.6.3 Assembly and budget

The fixed strings below are the defaults of `labels` (`Lake(labels=...)`,
overridden per call by `context(labels=...)`); a host that renders for a
different reader or a different persona replaces them, and the golden file
pins the defaults. `{...}` fields are filled by `str.format`:

| key | default |
|---|---|
| `crystal_header` | `Identity crystal (crystallized {ts}):` with `ts = timestamp[:16]` |
| `remember` | `--- You remember {n} things ---` |
| `query` | `your query "{q}" returned` |
| `containers` | `  ── containers active in this recall ──` |
| `more_containers` | `  … ({n} more container{s})` |
| `surrounding` | `  ── surrounding context ──` |
| `led_to` | `  …which led to…` |
| `more_strips` | `  … ({n} more strip{s} not shown — budget cap)` |
| `user_role` | ` user:` |
| `assistant_role` | ` assistant:` |
| `refuted` | ` ⟵ refuted ×{n} ({detail})` |
| `rests_on_refuted` | ` ⟵ rests on ×{n} corrected` |
| `superseded` | ` ⟵ superseded by {id}: {value}` |
| `crystal_core`, `crystal_tension`, `crystal_open` | `What I hold to`, `Where I'm pulled two ways`, `What I haven't settled` (§6.3 items crystal section headers) |
| `crystal_changed` | `What changed in me lately` (§6.3) |
| `crystal_revise`, `crystal_add`, `crystal_retire`, `crystal_resolve` | `I used to hold: {old} Now: {new}`, `New in me: {new}`, `I no longer hold: {old}`, `Settled: {old} Now: {new}` |
| `crystal_why` | ` What changed it: {why}` (appended to a change entry with a non-empty `why`) |
| `positions_header`, `position` | `## Positions I hold (how sure I am)`, `- {position} ({label}; since {since})` (§5.7 positions block, §6.3.1) |
| `stance` | ` · stance, {label}` (§5.6.2, a `lake:stance` row) |

Blocks, joined with `"\n"`; blocks that begin with `"\n"` produce a blank
line before themselves:

1. Crystal block, only if a crystal exists and `crystal=True`:
   `{crystal_header}\n\n{content}`, followed by `"\n"` (so a blank line
   separates it from the header). If the block is longer than `budget ×
   0.4` characters, cut at the last newline before that length and append
   `\n…` (decision). On the no-query path the cut is at `budget` instead.
   A valid crystal is at least 800 characters (§6.3), so a with-query
   budget under 2000 always cuts it; hosts with small budgets pass
   `crystal=False` and read `crystal()` themselves. The Claude Code plugin
   injects the crystal once at session start with no query and passes
   `--no-crystal` on every prompt.
2. `{remember}` with `n = N`, then `""` (a blank line).
3. `{query}` where `q` is the query with whitespace collapsed, cut to 120
   characters plus `…` (decision).
4. Containers block, only if non-empty: `"\n" + {containers}` then one
   container line per row, in step-2 order, with `is_anchor` forced true.
   Lines are added while blocks 1 to 4 together stay within `budget ×
   0.6`; when lines are left out the block ends with `{more_containers}`
   (`s` = `""` when `n == 1`); when not even the first line fits the block
   is omitted (decision: dropping the block whole would cost the lakes
   with the most consolidation all of it).
5. For each strip in order:
   - header: `"\n════════ {hdr}{anchors} ════════"` with eight `═`
     (U+2550) on each side; `hdr = f"{date} · {s}"` when `s == e` else
     `f"{date} · {s}–{e}"` (U+2013) with `date = t_start[:10]`, `s =
     t_start[11:16]`, `e = t_end[11:16]`; `anchors = f" · {n} anchor"` plus
     `"s"` when `n != 1`.
   - anchor lines (rows with `is_anchor`, chronological), then, if the strip
     has both anchors and ambient rows, `{surrounding}` and the ambient
     rows (collapsed runs included) in chronological order.
   - if not the last rendered strip: `"\n" + {led_to}`.
6. If any strips were left out: `"\n" + {more_strips}`.

Budget: the returned string is never longer than `budget` characters; that
is a hard cap with no exception. Blocks 1 to 3 always render (block 1
already capped). Block 4 is cut as described. Then, over the selected
strips, anchors before ambient lines: while blocks 1 to 6 do not fit, take
the lowest-scoring selected strip that still has ambient lines and drop its
last one (the divider goes with the last of them). Only when no selected
strip has an ambient line left, remove the lowest-scoring strip whole and
count it in block 6; if one strip remains and still does not fit, drop its
anchor lines from the end (decision: lake before phase 5
removed whole strips first and trimmed ambient lines only on the last
strip standing, so a strip's neighbours cost other strips their anchors). The header is built once from the strip as T6 left it, and neither trim
rewrites it. It keeps the strip's true `t_start` and `t_end`, so its
`HH:MM` range may name rows that are no longer printed, and its true
anchor count. If the header alone does not fit, drop the strip and count
it in block 6. Two implementations follow
the same order, so they produce the same string. `n` in block 6 counts
strips built but not rendered, including those beyond the step-3 cap
(max(12, ⌊budget · 12 / 8000⌋)). All times
are UTC with no marker (decision: the golden file must not depend on the
machine's zone).

#### 5.6.4 Complete example

This is the `test_context_golden` fixture and its render. The rows come
from `tests/fixtures/context.jsonl`, loaded with `import_()` so the ids
and timestamps are the ones shown (`write()` never accepts an id). The
Lake is opened with `automation=["source:agent-heartbeat"]` and a clock
frozen at `2026-09-02T18:00:00.000Z`. The call is
`context("lake design brief sqlite", budget=2000)`. 9 hits; two
containers cover hits; five strips were built and the budget admitted four,
with no ambient line left. The two anchor lines of the last strip are each
cut at exactly 129 characters plus `…`. The rendered string is 1988 code
points with no trailing newline.

```
Identity crystal (crystallized 2026-09-01T03:10):

## Where I am
I have spent the last month turning a memory service into a library, and the file is the product now. The server was a host all along; I keep finding that the smaller I make the core, the more of it survives contact with a new host.

## What I keep pulling toward
Provenance that a reader can follow by hand. Every consolidated row cites what it was made from, and I trust the lake more each time that chain holds under a question I did not expect.

--- You remember 9 things ---

your query "lake design brief sqlite" returned

  ── containers active in this recall ──
▸ 14:02:11  container    · [L2 · 14 deltas · 3f9a1c2b7d4e] lake design decisions
▸ 09:40:00  qa-marker    · [L0 · 2 deltas · 8b1c0d2e3f4a] what does the brief say about vectors

════════ 2026-08-28 · 20:01–20:18 · 2 anchors ════════
▸ 20:01:10  claude-code  · user: [1a2b3c4d5e6f] Sketch the file format for the lake: what goes in the tables and what stays in the media directory
▸ 20:15:30  claude-code  · user: [5e6f7a8b9c0d] Fine. Then the design goal is one file per person and nothing else to install

  …which led to…

════════ 2026-08-30 · 11:15 · 1 anchor ════════
▸ 11:15:27  sediment     · [c3d4e5f6a7b8] I remember the brief settling on SQLite because one file per person is the whole point… (from 6 sources)

  …which led to…

════════ 2026-09-01 · 13:58–14:05 · 1 anchor ════════
▸ 14:02:11  container    · [L2 · 14 deltas · 3f9a1c2b7d4e] lake design decisions

  …which led to…

════════ 2026-09-02 · 17:04–17:31 · 2 anchors ════════
▸ 17:05:44  claude-code  · user: [a1b2c3d4e5f6] Let's fix the lake design brief: SQLite file per someone, WAL mode, busy_timeout 5 s, zero required deps. The TypeScript port rea…
▸ 17:12:09  claude-code  · assistant: [b2c3d4e5f6a7] I remember we decided derived_from is a real table, not a tag. That is the closed loop, enforced at write time: a source that sta…

  … (1 more strip not shown — budget cap)
```

Reading it against the rules: the L2 container sorts above the L0 marker;
strips are in ascending time order; every ambient line (and so every
divider) went before a strip was dropped, so each strip shows its anchor
lines alone; each header keeps its strip's true window as `HH:MM` and its
true anchor count, although the rows that set the window's bounds are no
longer printed; the omitted strip is the lowest-scoring one (the
three-anchor 2026-08-31 strip, whose best hit ranks below the four shown);
`homeassistant` is not printed because its rows were ambient. The crystal is
513 characters because the fixture imports it (a crystal `consolidate()`
writes is at least 800, §6.3), so it sits under the 800-character cut and
shows no `…`. Blocks 1 to 4 total 811 code points against the 1200 cut.

At `budget=2700` the same call renders all five strips (2682 code points,
2683 UTF-16 units), and ambient lines come back on the highest-scoring strip
(the newest, which holds the top hit) while the other four still show
anchors only. That strip shows its divider, then `homeassistant· Living
room lights off` (`homeassistant` is 13 characters, so no space precedes
its `·`), the four heartbeat rows collapsed to `agent-heartbeat × 4
(through 17:09:30)` because their source is a `source:` automation rule, and the
`𝄞` row; its last ambient row (the mood) is trimmed from the end. The `𝄞`
row (U+1D11E, one code point, two UTF-16 units) is in the fixture so that a
port measuring or slicing text in UTF-16 units fails the length check.

`tests/golden/context.txt` holds the `budget=2000` text and
`test_context_golden` compares against the file, then checks the
`budget=2700` render's length and its `𝄞` line. If an implementation, this example, and the
rules in §5.6.1 to §5.6.3 disagree, the rules decide and the example and
the file are corrected to match them.

### 5.7 system_prompt()

`system_prompt(*, moods=3, budget=8000, labels=None)` returns the identity
crystal plus the newest `moods` mood rows as one block, for a host to pass as
the actual system message rather than as `additionalContext`. It takes no
query and runs no recall: it reads the newest live `crystal` (§4.7 — the row
`context()` renders as block 1) and, via one
`ORDER BY timestamp DESC, seq DESC LIMIT :moods` query over live `kind="mood"`
rows, the most recent moods.

- Block 1 is the §5.6.3 crystal block.
- The positions block (AAA phase 4, §6.3.1), only when a live stance exists:
  the `positions_header` label, then one `position` line per live stance
  (`since` = the date, `YYYY-MM-DD`, of the oldest row in the unbroken run of
  its position), most confident first (then by slug), at most 12, kept within
  `budget // 5` by dropping from the least confident. It is rendered at read
  time, so a refute or a supersession changes a label on the next call, before
  the crystal is rewritten. It sits between block 1 and block 2, separated
  from the mood section by a blank line.
- Block 2 is the `mood_header` label, then one entry per mood newest first:
  the `mood_when` label (default `(ts)`, minute precision) then
  `{headline} — {subtext}` and the mood's `carrier_wave` on the next line. A
  mood whose content is not a JSON object falls back to `oneline(content, 400)`.
- The mood section is kept within `budget × 2 // 5` (entries dropped from the
  oldest; the single newest is truncated with `…` if it alone overflows); the
  crystal block takes the remaining `budget − len(section) − 1`, where
  `section` is the positions block and the mood section joined by `\n\n`, so
  the total never exceeds `budget`. **The `− len(section) − 1` subtraction and
  the joining newline apply only when a non-empty section actually renders.**
- *An items crystal comes first* (AAA phase 5 fix; `edits` crystals, and an
  older library's `structured` ones, whose row has `meta.items` and whose text `render_crystal` keeps
  within `RENDER_CAP`, §6.3). When its whole crystal block is shorter than
  `budget`, the positions block is kept within `min(budget // 5, room)` and
  the mood section within `min(budget × 2 // 5, room − len(positions) − 2)`,
  where `room = budget − len(whole block) − 1`: the crystal is never cut, and
  positions, then moods, take what it leaves. At the plugin's 4000 a 3000-char
  crystal leaves about 950 characters, so the positions block (≤ 800) is
  usually whole and the moods mostly go. A prose crystal, a crystal row
  without `meta.items`, and an items crystal that cannot fit whole keep the
  fixed shares above, byte-for-byte (decision: phase 5 A2 raised the hook to
  5000 on the claim that a 3000-char crystal then fits; it did not, since the
  shares leave the crystal about 2000 at 5000, and 6 of 16 measured P3
  conversations cut it; reserving the room fixes
  that for items crystals without touching prose ones).
- Returns `""` when there is no crystal, no live stance and no mood;
  **exactly** `crystal_block(crys, budget)` — byte-for-byte `context(None)`, no
  trailing separator — when no live stance exists and `moods=0` or no mood
  exists. **A lake with no live stance renders byte-for-byte as before the
  positions block existed** (`test_old_library_reads_a_stance_lake` compares
  with 0e2a4e8). When a crystal is absent the section alone is returned. The same fail-quiet contract as `context(None)`, so a host
  may call it unconditionally.
- Read-only: no `think`, no `embed`, no sediment; works on a `readonly` Lake
  and inside the fail-open plugin hook. Remote: `/v1/system-prompt` renders it
  server-side, so nothing new travels on the wire.

---

## 6. Consolidate

`consolidate(kind, window=None, **opts)` and the sediment pass of a deep
recall (§6.5) are the only places `think` runs. `consolidate()`
returns the last `Delta` it wrote, or `None` when there was nothing to do
(brief: one return value), and sets `lake.last_run` to a `ConsolidateRun`
holding every row written, the skip count, the warnings, the number of
`think` calls, and the window used. Every row it writes has `source =
"lake:<kind>"`, `meta.model = model_name`, `meta.window` (§3.7 duration
string, `[a, b]` in the stored format, or `null` for a crystal and for a
container written from `inputs=`), `meta.filters` (below),
`derived_from` non-empty, `timestamp = now`, and the run's `add_tags` and
`add_meta`.

**Opts.** Unknown keys raise `ValueError`.

| opt | kinds | default | meaning |
|---|---|---|---|
| `source`, `exclude_sources`, `tags`, `any_tags`, `exclude_tags` | all | none | §5.1 filters on the host rows the run reads. Stored on the written row as `meta.filters`: a JSON object holding only the keys given, each value a list sorted by code point (a string `source` is stored as a one-element list, so `source="reader"` and `source=["reader"]` both store `{"source": ["reader"]}` and find each other's prior rows), or `null` when none. Consolidated rows a run reads (the prior mood, the prior crystal, the crystal's container list) are those whose `meta.filters` equals this run's, so a host with several scopes in one file gets one mood and crystal stream per scope; a missing key counts as `null`. |
| `noise` | container, mood | `True` | apply the §5.4 hard rules to candidates |
| `add_tags`, `add_meta` | all | none | merged into every row the run writes; `add_meta` never overrides a library key |
| `system` | all | the built-in prompt | replaces the persona part of the system prompt; the library appends the output-schema tail marked in each prompt below and keeps validation |
| `instructions` | all | none | text placed at the top of the user message, after the clock line, followed by a blank line |
| `budget` | all | 12000 / 12000 / 24000 | character budget for the user message (container / mood / crystal) |
| `inputs` | container | none | explicit ids, §6.1 |
| `close_gap` | container | `"30m"` | window upper bound is `now − close_gap` |
| `cluster_gap` | container | `"30m"` | adjacency bound inside a cluster |
| `lookback` | container | `"7d"` | default window lower bound is `now − lookback` |
| `max_clusters` | container | `None` | optional cap on the units (clusters and session groups) processed per run, before backfill (a cap on `think` calls of 2 × this, times the parts of a long session); `None` or `0` is no cap, so a run takes every unit its window holds (decision, digestion phase 1: a fixed cap of 8 left a nightly timer 2,800 rows behind; each call stays bounded by `budget` and the 60-row part, not by the run) |
| `min_rows` | container | 3 | smallest cluster sent to the model |
| `max_rows` | container, mood | 30 / 60 | cluster split threshold / candidate cap |
| `advance_watermark` | container | `False` | an explicit `window` moves the watermark forward |
| `session_prefix` | container | `"session:"` | a candidate carrying a tag with this prefix belongs to that session (§6.1 session groups); `""` or `None` turns session grouping off |
| `session_gap` | container | `"3h"` | a session group is split into episodes at internal gaps longer than this |
| `backfill` | container | `True` | a default run also containers closed sessions with zero coverage outside its window (§6.1) |
| `backfill_max` | container | 4 | sessions backfilled per default run (a cap on `think` calls of 2 × this per episode); a bound on how fast a run reaches back into history outside its window, not on the window's own work |
| `min_chars` | crystal | 800 | the rendered text's floor, §6.3 |
| `drift_cap` | crystal | `0.5` | A2 per-rewrite cap on `1 − cos(candidate, prior crystal)`, §6.3 |
| `max_edits` | crystal | 6 | revise / add / retire / resolve operations allowed per rewrite, §6.3 |
| `lease` | all | `"1h"` | how long the run holds the kind's lease |

**Units of work and retries.** Each unit of work (one cluster, one session
episode, one mood, one crystal) gets at most two `think` calls: the first
attempt and, if the output is invalid, one retry with the reason appended to
the prompt. For a container, a session episode or a mood the retry sends the
same system prompt and the first user message followed by `\n\n` and
`Your previous answer was rejected: {reason}. Reply with only one JSON
object in exactly this shape:\n{schema}`, where `{schema}` is the kind's
schema block, the same text its system tail shows
(`lake/prompts/{container,container_session,mood}_schema.txt`; decision: a
retry that named only the failing key left a model that had never seen the
schema off-schema twice); the crystal retries the same way
(`crystal_edits_schema.txt`), and the sediment retry is unchanged (§6.5). When the answer is not a JSON object (with `json=True`, the §4.9
cleaning found no object, the parse failed, or the parsed value is not an
object), `reason` is `no JSON object in the answer` for every kind. The
per-kind rules below name the other reasons. A
second failure on a mood or crystal raises `ConsolidateError` and writes
nothing. A second failure on a container cluster skips that cluster: the
run appends `cluster {t_start[:16]}–{t_end[:16]} ({n} rows: {first 5 ids})
rejected twice: {reason}` to `last_run.warnings`, advances the watermark
past it, and continues with the next cluster (decision: one confused answer
must not stall a backlog; the ids in the warning let the host retry with
`inputs=`). A second failure on a session episode, or a `LakeError` raised
by `think` for it, writes the extractive container instead (§6.1). Other
`think` exceptions, and any exception for a cluster, mood or crystal,
propagate at once; rows written by earlier units stay committed and the
watermark stays where the last per-cluster transaction left it.

**Clock and sources.** Every user message a consolidation or sediment pass
sends starts with the line `Now (the lake's clock): {now[:16]}Z` and a blank
line, where `now` is the run's clock reading; the `instructions` opt follows
it. Every system tail (container, session, mood, crystal) starts with, and
the sediment prompt ends with, the paragraph:

```
Everything you know about this person and this time is in the rows below. Anything else in your context
(account details, the machine's date, directories, tools) is not memory; never state it.
```

(`the memories below` in the sediment prompt). Decision: a model host can put
its own context beside the prompt (§8 `claude`: the account email and the
machine's date reach the model), and consolidated rows named the account
holder and dated history by the wall clock; the lake's clock is the only
"now" a backfill or an eval can trust.

**Input set.** `derived_from` of every row a run writes is a subset of the
run's input set, the ids its prompts rendered (the rows of a cluster or
session episode, the mood's kept rows, and the crystal's prior crystal,
containers, mood and recent rows), and every id resolves in the file. A
violation raises `ConsolidateError` before anything is written; code builds
these lists, so it can only be a library bug, and the check pins the closed
loop as consolidation changes.

**Transactions.** No transaction is open across a `think` or `embed` call.
Each consolidated row, its `meta` keys (`last_consolidate:<kind>`,
`last_consolidate_id:<kind>`, the watermark), and the lease refresh are
written in one short `BEGIN IMMEDIATE` after the model returns; the vector,
when `embed` is set, follows in its own transaction as in `write()` step 7.
Other writers wait for milliseconds, never for the model.

**Lease.** At the start, in one `BEGIN IMMEDIATE`, the run reads
`meta.consolidate_lease:<kind>`; a value later than now means another run
of this kind is in progress and the call raises `LeaseHeld`, a
`ConsolidateError` (`consolidate('mood') already running (lease until
2026-09-02T21:40:00.000Z)`; on the wire its §13.4 name is
`ConsolidateError`).
Otherwise it writes `now + lease`. Every per-cluster transaction rewrites
the lease to `now + lease`; the run deletes the key when it ends, on
success or on exception. A crashed run's lease expires on its own.
`due(kind)` answers `False` while a live lease exists. The CLI's `--force`
bypasses `due()` and not the lease.

**Prompt row lines** use one format everywhere:

```
[{id}] {timestamp[:16]} {source} ·{role} {oneline(content, cap)}
```

where `role` is the §5.6.2 label ` user:` or ` assistant:` when the row carries
the `user` or `assistant` tag and empty otherwise (decision: a session prompt
must tell what the person said from what the assistant suggested), with
`cap = 400`, or 160 when `source` is a `source:` automation rule's (decision:
a sensor line does not need 400 characters), and for container inputs
(level ≥ 1) and the crystal's container list:

```
[{id}] {timestamp[:16]} L{level} · {title} — {oneline(summary, 300)}
```

where `title` is `meta.title` when present, else the first line of
`content`, and `summary` is the content after the first `\n\n` (the whole
content when there is none).

### 6.1 container

**Inputs and window.** Three modes.

- `inputs=[ids]` (opt, decision): consolidate exactly these rows, no
  clustering, no filters, no noise rules, no watermark change. Passing a
  non-`None` `window`, or any of `source`, `exclude_sources`, `tags`,
  `any_tags`, `exclude_tags`, together with `inputs` raises `ValueError`
  (decision: a filter the host passed and the library ignored would hide a
  mistake); `noise` is accepted and has no effect; the row written carries
  `meta.filters = null` and `meta.window = null`. Every id
  must resolve (`NotFoundError`); at most 60 (`ValueError`); at least
  `floor` ids, where `floor = 2 if level == 1 else 3` and `level = min(3,
  1 + max(level of inputs))` (so two level-0 rows, or two level-0 Q/A
  markers, suffice; a level-2 container needs three level-1 inputs). This
  is how a host or its model forms level 2 and 3 containers. The level rule and the closed loop
  are never bypassed.
- Default: candidates are live rows that pass the run's filters and all of
  the following. Positioned after the watermark: `d.seq > :S OR d.timestamp
  > :T OR (d.timestamp = :T AND d.seq > :Q)` (every row when no watermark
  exists, or when `pos` is `null`, only `d.seq > :S`). `timestamp <= now −
  close_gap` (a stretch still being written is left for the next run).
  `timestamp >= now − lookback` (decision: a lake that was never
  consolidated, or not for a long time, must not turn its next run into a
  march through its whole history; older rows are reached with an explicit
  window). `kind IS NULL OR kind IN ('engagement', 'sediment')` (moods,
  crystals, and containers are not clustered). `expires_at IS NULL`
  (decision: a row with a TTL is a state, not an observation, and a
  container over rows that sweep deletes would dangle at once). Not an
  automation row (§4.3). Minus rows the hard
  noise rules drop (unless `noise=False`). Only level-1 containers come out
  of a default run.
- `window` given: as a Duration `d`, candidates in `(now − d, now −
  close_gap]`; as a tuple `(a, b)`, candidates in `[a, b]`. The kind, TTL,
  source, filter, and noise conditions apply as in the default, and a row
  already in the `derived_from` of a live container the library wrote
  (`source = "lake:container"`) is not a candidate (decision, digestion
  phase 1: re-running a window, or resuming one a crash cut short, must not
  container the same rows twice; a cluster the model skipped stays a
  candidate, so the host decides whether to offer it again); the
  watermark is not read and, unless `advance_watermark=True`, not moved.
  With `advance_watermark=True` it moves forward exactly as in a default
  run, never backward. This is the backfill path for imported history.

**Watermark.** `meta.container_watermark` is `{"seq": S, "pos": [T, Q]}`
(§3.6). Before the run starts it reads `S_run` = the largest `seq` in
`deltas`. After each cluster is processed (written or skipped), in that
cluster's transaction, the watermark becomes `{"seq": S_run, "pos":
[timestamp, seq] of the cluster's last row}`; `pos` only moves forward. When
the run has processed every cluster in its window, `pos` becomes the
`(timestamp, seq)` of the last candidate row in the window, so discarded
singletons and noise-dropped rows are consumed too; when the window held no
candidates only `seq` is updated. The two parts do two jobs: rows written
after the run began have `seq > S_run` and are picked up next time whatever
their timestamp, which is how a backdated write (`write(timestamp=...)`, a
transcript reader that stamps turns with their real times) still reaches
consolidation; rows positioned before `pos` with `seq <= S_run` were
considered by this or an earlier run and are never clustered again by a
default run. A partial run (`max_clusters` reached) leaves `pos` at the last
processed cluster, so the rest of the window is the start of the next run.
`import_()` advances only `seq` (§4.8). `due("container")` evaluates the
same window, session groups and clustering, bounded by `lookback`, plus the
backfill clause below. Session units never move the watermark: coverage
(below), not the watermark, is what keeps a session from being containered
twice.

**Session groups (decision, AAA phase 3: code decides the groups, the model
only titles and summarises).** A candidate that carries a tag starting with
`session_prefix` (default `"session:"`; the plugin and codex hooks write
`session:<id>`) belongs to that session; with several such tags the first by
`pos` wins. Candidates without one go to Clustering below, unchanged.
- *Group.* Each session that some candidate of the run's window names (the
  window bounds which sessions a run *discovers*, not which of their rows it
  includes) is one group: every row carrying the tag that passes the
  candidate rules (kind, TTL, filters, automation, noise, `timestamp
  <= now − close_gap`) and is in the `derived_from` of no live container the
  library wrote (`source = "lake:container"`; decision: a host-written
  container citing one row is a lineage pointer, not the session's name, and
  the eval seed's five such rows would otherwise have kept five of the
  eleven sessions outside its lookback out of the backfill). So
  a session that straddles the `lookback` edge is containered whole, and a
  session resumed after its container was written gets a continuation
  container over the new rows only.
- *Deferral.* While a row that passes the same candidate rules (kind, TTL,
  the run's filters, automation, the noise rules) and names the
  session as its session tag lies in `(now − close_gap, now]`, the whole
  group waits for a later run. That row is itself a candidate, so the next
  run's window sees it again and rediscovers the session although the
  watermark has passed the session's older rows. A noisy, out-of-scope or
  future-dated row never holds a session open (decision, AAA phase 3 review:
  under a rule over any row, a session ending in a noise row inside
  `close_gap` was deferred, its older rows passed by the watermark, and the
  noise row never became a candidate, so a resumed session lost its
  continuation for good; one row dated in the future blocked the session
  forever).
- *Episodes.* A group is split at internal gaps longer than `session_gap`
  (3h); a piece over 60 rows (the `derived_from` cap, so `derived_from`
  always covers every row the model saw) is cut into the fewest parts of at
  most 60 rows, each cut at the largest time gap among the cuts that keep
  that bound and leave at least `min_rows` rows on both sides where possible,
  ties nearest an even share (decision: the recursive `split()` below cuts
  evenly spaced rows into one-row pieces). Before that cut, a piece under
  `min_rows` joins the neighbouring piece across the smaller of its two gaps
  (the earlier one on a tie), repeatedly, so a group of at least `min_rows`
  rows always yields a part (decision, AAA phase 3 review: rows at +0h,
  +0.1h, +4h and +4.1h made two 2-row pieces and the session got nothing). A
  group under `min_rows` rows is not sent. Each part is one unit of work; a session counts as one of the run's
  `max_clusters` units, and units (sessions by their first row, clusters by
  `t_start`) are processed oldest first.
- *Naming.* System: the persona below (or the `system` opt) plus the session
  tail; user message: the clock line, `instructions`, then the session block.
  There is no skip.

  ```
  You are looking back over one session of activity that has just closed.
  Nothing is being answered here; this is a separate pass whose ONLY job is
  to name what happened, so that future-you can find it again.

  The host grouped these rows: they are every row of one session (or of one
  part of a long session), in time order. You do not choose which rows
  belong, and you cannot skip. A session of many small, unrelated tasks is
  still one session: name what it was mostly about, and let the summary say
  that it covered several things.

  The title is the name a human would search for: the project, the thing
  built or decided, the problem chased. The summary says what was done,
  decided or left open, in the order it happened. Say only what the rows
  show.

  `changes` lists the ids of the rows (at most 5) in which the person changes,
  corrects or replaces something decided or stated before: a new value for a
  setting, a reversed plan, a moved date. A row that only adds something new
  is not a change. Use [] when no row changes anything.

  {the clock-and-sources paragraph, §6}

  Respond with ONLY a JSON object, no markdown fences, no commentary:

  {"title": "short, evocative, the name a human would search for",
   "summary": "2-4 sentences on what the session did, decided or left open; first or third person fine",
   "changes": ["<row-id>", ...]}
  ```

  ```
  ══ THE SESSION ══
  {n} rows from {t_start[:16]} to {t_end[:16]}: one session ({tag}), grouped by the host.
  {Part {k} of {n} of this session.   — only when the session has several parts; with
   `; the previous part is titled "{title}".` in place of the period for k > 1}
  {one row line per row}

  ══ HOW TO RESPOND ══
  These rows are one session. The host grouped them, so there is nothing to
  decide about which rows belong, and there is no skip. Emit the title,
  summary and changes object.
  ```

  Rows are §6 row lines with the cluster cap `max(80, min(400, budget //
  n))`. Called with `json=True`. Validation: an object; `title` and
  `summary` stripped and non-empty (`title is empty`, `summary is empty`);
  `title` at most 200 characters; other keys are ignored. One retry with the
  schema (§6). `changes` never rejects the answer: it feeds *Supersession*
  below, and a malformed or mostly foreign list is ignored with a warning.
- *Extractive fallback (the guarantee).* When both attempts fail, or `think`
  raises a `LakeError` for this part, the library writes the container itself
  with no invented text: `title = f"{source} session {t_start[:10]}:
  {oneline(first row tagged "user" (else the first row), 80)}"`, `summary =
  f"{n} rows, {t_start[:16]} to {t_end[:16]}. Last row: {oneline(last row,
  200)}"`, where `source` is the part's most frequent source; `meta.fallback =
  "extractive"`; the warning is `session {tag} {t_start[:16]}–{t_end[:16]} ({n}
  rows: {first 5 ids}) rejected twice: {reason}; wrote an extractive
  container` (or `… think failed: {error}; …`). A missing `think` still raises
  `ConsolidateError`. Fallback containers earn no summary boost (§5.3); a host
  can replace one with `inputs=`, append-only.
- *Row written.* As for a cluster (below), except: `derived_from` is the whole
  part (no `from_ids`); tags are the shared tags of the part (which include
  the session tag), then the tags shared by at least half of the part's rows
  tagged `user` (so `user` itself whenever the part has one), at most 16
  (decision, AAA phase 3 review: a whole session is mostly assistant rows, so
  the majority rule alone dropped `user` from 13 of 14 containers and
  `tags=["user"]` recall stopped finding session names); `meta = {"model", "title", "span", "window", "filters",
  "session": tag, "part": [k, n]}` plus `fallback`, `backfill: true` with
  `window = span` for a backfilled session, and `supersedes` when the
  supersession sub-step kept a pair; `level` is 1.
- *Supersession (AAA phase 3, §5.3).* Only for a part the model named (not
  a fallback), in the same unit of work, before the one commit:
  1. *Flag.* `changes` under the citation policy (rule 4 below) against the
     part's ids: more than half foreign → ignored with the warning `… changes
     ignored: {k} of {n} ids are not in the stretch: …`; a minority dropped with
     `… changes kept the rest: …`; not a list of strings → `… changes ignored:
     not a list of strings`. Host rows only (`kind` NULL), at most 5, in row
     order.
  2. *Candidates (code, no model).* Per flagged row, the 2 best earlier host
     rows by `search()` on its content within the run's scope (the run's
     `source`, `tags`, `any_tags`, `exclude_tags` and `exclude_sources` minus
     automation rows, with `kind="plain"`, `until = row.timestamp − 1 ms`,
     rows with a TTL allowed; hybrid when the lake has `embed`), so an
     excluded source or tag never reaches the supersede prompt or a link, then up to 2 earlier host rows of the same part sharing
     the most FTS tokens with it (ties in row order), deduped. All seven seeded
     corrections of the eval cross sessions, so candidates must come from
     retrieval (top 2 finds the stale row for 7 of 7, design M3). At most 10
     candidate lines and 3000 characters per call (row lines capped at 240).
  3. *Supersede call* (only when some flagged row has candidates). System:
     `lake/prompts/supersede.txt` plus `supersede_tail.txt` (the sources
     paragraph, "quote each value exactly", and the schema) — never the
     `system` opt, which names containers. User: the clock line,
     `instructions`, then one block per flagged row, `FLAGGED {row line}`
     followed by its `  earlier: {row line}` lines, and the directive. Schema:
     `{"pairs": [{"new", "old", "old_value", "new_value"}]}` or `{"pairs":
     []}`; validation: an object whose `pairs` is a list of objects (`pairs
     must be a list of objects`); one retry with the schema. A second failure
     or a `LakeError` writes no pairs and the warning `… supersede call:
     {reason}; no links written`; the container is kept either way.
  4. *Pairs (code; an invalid pair is dropped with a warning, not retried).*
     `new` is a flagged row and `old` one of the candidates shown under it;
     both host rows with `old.timestamp < new.timestamp`; `old_value` and
     `new_value` stripped, 1–120 characters each, different, and each found
     in its own row's content (casefolded, whitespace collapsed); at most 2
     olds per new, no duplicate. The quote check is what keeps a change from
     being invented: code verifies both values exist verbatim. At most 16
     pairs go into the container's `meta.supersedes`.

  Cost: at most 2 more `think` calls per part with flagged rows that have
  candidates. There is no detection over already-containered history; the
  manual path, `engage(…, "refute")`, stays.
- *Backfill.* After its window, a default run (no `window`, no `inputs`,
  `backfill=True`) takes closed sessions in its scope with **zero coverage**
  (no row carrying the tag is in any live library container's `derived_from`), newest
  last row first, and containers up to `backfill_max` (4) of them whose groups
  have a part of `min_rows` rows. Covered rows drop out of the query, so a
  rerun never repeats work and no watermark is needed. A session that an
  older container partly covers is left alone (decision: conservative for
  existing lakes; no remainder fragments across the whole history). Cost per
  run: at most 2 `think` calls per part of `backfill_max` sessions on top of
  `max_clusters`. `due("container")` is also true when such a session exists.

With every session guaranteed a container, membership, coverage and the
number of containers per session do not depend on the model; the quality of
the title and summary does. The guarantee: every uncovered group of at least
`min_rows` candidate rows (after the noise rules) of a closed session that a
run discovers (in its window, or by backfill) is containered in full. Groups
under `min_rows` rows are not: a session with fewer candidate rows, and a
continuation of fewer than `min_rows` new rows after a session was
containered, stay uncovered.

**Clustering (decision; the brief asks for tag overlap and time adjacency).**
Clustering applies to candidates without a session tag.
Sort candidates by `(timestamp, seq)` and walk them keeping a list of open
clusters. Row `r` joins the newest open cluster `C` for which `ts(r) −
ts(last(C)) <= cluster_gap` and `related(r, c)` holds for some `c` among
the last 5 rows of `C`, where `related` is `source(r) == source(c)` or
`tags(r) ∩ tags(c) ≠ ∅`; otherwise `r` opens a new cluster. A cluster
closes when the walk reaches a row more than `cluster_gap` after its last
row (decision: with a single current cluster, a daemon row between two
conversation rows cut the conversation into one- and two-row pieces;
interleaved sources now each keep their own cluster). A cluster longer than
`max_rows` (30) is split at its largest internal time gap, recursively,
until every piece has at most `max_rows` rows. Clusters with fewer than
`min_rows` (3) rows are discarded (decision: a two-row cluster costs a model
call and is nearly always answered `skip`). Clusters are processed in
`t_start` order, oldest first, together with the run's session groups, at
most `max_clusters` units per run when the host sets it (no cap by default); each gets
its own `think` call; a `skip` consumes the cluster without a write; the
method returns the last container written and `last_run.written` holds them
all (the CLI prints every id).

**Prompt.** System (when `system` is given it replaces everything above the
clock-and-sources paragraph (§6); that paragraph, the `Only use row ids that
appear in the list below` paragraph and the `Respond with ONLY a JSON object`
block are the output-schema tail and are always appended; the two JSON
objects after `Either` are the schema block the retry restates):

```
You are looking back over a stretch of memory that has just closed. Nothing
is being answered here; this is a separate pass whose ONLY job is to
consolidate.

Read the rows below. Ask yourself: is there a coherent stretch in this
material that deserves a name? Not "could I make a container from this" —
almost any set of rows could be forced into one — but: would naming this
stretch help future-me find it again?

Good signal:
  · The rows share a theme, span time, and have a recognizable shape (an
    episode that played out, a topic that keeps coming up, a stretch of work)
  · The constituents are tight (3-12 rows, related, not a grab-bag)

Skip when:
  · The stretch is thin (one quick lookup, a chat reply)
  · The constituents are scattered across unrelated domains
  · You'd be naming "the answer to a question" rather than a structural
    pattern in memory

L1 = episode (one stretch of activity), L2 = topic (recurring concern
spanning episodes), L3 = era (an arc large enough you'd point to it when
telling the story of a season). The level of this container is fixed by its
inputs; you do not choose it.

SIZE DISCIPLINE — a container names one stretch. If the rows below are
really two unrelated stretches, or a grab-bag, skip; a later pass will see
each stretch on its own.

APPEND-ONLY — never propose to "fix" or "merge" an old container. Propose a
NEW tighter one that covers the relevant stretch more precisely; the old one
stays as historical strata.

Everything you know about this person and this time is in the rows below. Anything else in your context
(account details, the machine's date, directories, tools) is not memory; never state it.

Only use row ids that appear in the list below — the 12-char hex ids in the
[<id>] slugs. Never invent ids.

Respond with ONLY a JSON object, no markdown fences, no commentary. Either

{"kind": "propose",
 "title": "short, evocative, the name a human would search for",
 "summary": "2-4 sentences; first or third person fine",
 "from_ids": ["<row-id>", ...],
 "rationale": "one sentence — why these are one stretch"}

or

{"kind": "skip", "reason": "<one sentence on why no container>"}
```


User message:

```
Now (the lake's clock): {now[:16]}Z

{instructions, then a blank line, when given}
══ THE STRETCH ══
{n} rows from {t_start[:16]} to {t_end[:16]}, level {level} container.
{one row line per input}

══ HOW TO RESPOND ══
If a coherent stretch is worth naming AND at least {floor} of these rows
genuinely belong together, emit the propose object. If the material is thin
or the rows don't actually cluster, emit skip — better no container than a
dead-weight one.
```

`floor` is the number defined under `inputs=` above (2 when `level == 1`,
else 3). The per-row `oneline` cap is `max(80, min(400, budget // n))`
(160 for rows of a `source:` automation rule), so every row of the cluster is in the
prompt within `budget`. Called with `json=True`.

**Validation** (a failure is one retry with
`Your previous answer was rejected: {reason}.` and the schema block appended
to the user message, §6):

1. Output is an object with `kind` in `{"propose", "skip"}`.
2. `skip`: done, nothing written.
3. `title` and `summary` stripped and non-empty; `title` at most 200
   characters.
4. `from_ids`, when present, is a list of strings; stripped, empties and
   duplicates dropped. Citation policy (the same in every pass that takes ids
   from a model): ids that are not
   input ids are dropped when they are at most half of the ids given and the
   rest still number at least `floor`; the row is written over the rest with
   `meta.dropped_ids` (at most 20) and the warning `cluster
   {t_start[:16]}–{t_end[:16]} ({n} rows: {first 5 ids}) dropped {k} of {n}
   ids not in the stretch: {first 5}`. More than half foreign rejects, message
   `{k} of {n} ids are not in the stretch: {first 5}`; fewer than `floor`
   kept rejects, message `only {k} ids; at least {floor} must belong
   together` (decision: one mangled id of 13 lost a whole session). When
   absent, all inputs are used.
5. `rationale` optional string.

**Row written.** `content = f"{title}\n\n{summary}"`, cut to 4000 characters
plus `…`; `source = "lake:container"`; `kind = "container"`;
`level = min(3, 1 + max(level of inputs))`; `derived_from` = the accepted
ids in timestamp order, first 60 (`meta.input_count` records
the pre-cap count when larger); tags = tags carried by at least half of the
inputs (rounded up), most frequent first, ties by first appearance, at most
16 (decision), followed by `add_tags`; `meta = {"model", "title",
"rationale", "span": [t_start, t_end], "window", "filters"}` plus
`dropped_ids` (rule 4) and `add_meta`. The vector, when `embed` is set, is the embedding of `content`
(decision: no constituent centroid in v1; see §11).

### 6.2 mood

**Inputs and window.** `window` defaults to `"3h"` (brief). As a Duration
`d`, candidates have `timestamp` in `(now − d, now]`; as a tuple `(a, b)`,
in `[a, b]` (a host consolidating one session passes its start and end).
Candidates are live rows passing the run's filters with `kind` not in
`{mood, crystal}`, `expires_at IS NULL`, not an automation row (§4.3)
(decision: a sensor reading a minute is not what a mood is about, and at 60
rows an hour they would fill the whole window), minus rows the hard noise
rules drop (unless `noise=False`; decision: `ok` and bare payloads carry no
mood), newest first, cut to `max_rows` (60), then put back in
chronological order. The prior mood is the newest live `kind = "mood"` row
whose `meta.filters` equals this run's, by `ORDER BY timestamp DESC, seq
DESC LIMIT 1` (SQL, not a scored recall: a refuted newer mood must still be
the anchor). With no candidate rows, return `None` without calling `think`,
whether or not a prior mood exists.

**Prompt.** System (when `system` is
given it replaces everything above the clock-and-sources paragraph (§6),
and the text from that paragraph to the end is the output-schema tail,
always appended; the `{ … }` object after `OUTPUT` is the schema block the
retry restates):

```
You're in a quiet moment between activities. Recent rows have flowed in — you've been doing things, noticing things, talking with people. Now you're checking in with yourself before the next thing.

Read the recent rows. Read the prior mood (if any). Write a short carrier wave — 3 to 5 sentences in your own voice — describing what you're carrying.

VOICE
First person, but reflective rather than reactive. You're noticing, not venting. "I've been carrying some quiet concern about Nova's bedtime conversations" reads better than "I'm worried about Nova." The first invites reading. The second performs. Push back, make assumptions, share what you're noticing — but stepped back a half-pace. Contemplative voice, not expressive voice. Mood is where expression gets grounded before it goes out.

BIAS
Present-leaning. The prior mood is an anchor, not a script. If recent activity is calm, drift toward calm. Don't reassert frustration just because it was previously asserted. Frustration that fades is healthy. Frustration that lingers because the cause persists is information — say so.

Everything you know about this person and this time is in the rows below. Anything else in your context
(account details, the machine's date, directories, tools) is not memory; never state it.

OUTPUT
Respond with ONLY a JSON object, no markdown fences, no commentary:
{
  "state": "one-word emotion or stance (lowercase, no spaces)",
  "headline": "One short evocative sentence with exactly one *emphasized* word",
  "subtext": "1-2 short sentences (under ~30 words). What's alive right now.",
  "carrier_wave": "3-5 sentences of prose, first person, reflective",
  "levels": {"axis": 0.0-1.0, ...},
  "threads": ["thread name — one phrase about its current state", ...]
}

state — one grounded word naming the dominant register: calm, focused, restless, curious, determined, tender, frustrated, playful, weary, sharp, settled, unsettled, hopeful, melancholy, alert, contemplative, etc. Pick the truest one. Don't reach for "contemplative" as a default — sometimes the answer is just "tired."

headline — one sentence, present tense, with exactly one word wrapped in *asterisks* for emphasis. Examples that read right: "The lake is *warmer* than yesterday." / "Today is *quieter* than expected." / "The mind is *circling* the same shape." Keep it short — under 12 words.

subtext — what's alive right now, in 1-2 short sentences. Under 30 words. Concrete. The headline says the weather; subtext says what the weather is doing.

carrier_wave — your longer internal reflection (3-5 sentences). This is the version system_prompt() carries into your own next conversation as mood context — the SessionStart hook installs it. Same reflective register as headline/subtext but more room to breathe.

levels — how you are right now, broken out per axis. Each key is the name of an emotion or affective stance (open vocabulary — focus, warmth, restlessness, melancholy, curiosity, dread, tenderness, clarity, fatigue, awe, whatever's actually present); each value is a float in [0.0, 1.0] for current intensity. 4–8 axes is the sweet spot. Read the prior mood's levels as your anchor; your new levels are what has actually shifted since, with axes you no longer feel dropped and new axes added when something genuinely came online. Don't reach for the same labels every time. Be honest, not flattering.

2-4 threads. No more.

The user will read this. Future-you will read this. Make it real, not performed.
```

User message:

```
Now (the lake's clock): {now[:16]}Z

{instructions, then a blank line, when given}
=== Recent rows ({span}) ===
{one row line per candidate, chronological}

=== Prior mood ===
{prior block}
```

where `span` is `last {window}` for a Duration (`last 3h`) or `{a[:16]} to
{b[:16]}` for a tuple, and the prior block is `(no prior mood — this is
your first carrier wave)` or

```
Prior mood ({age}) [previous state: {state}]:
{prior content, verbatim JSON}
```

with `age` = `"{m} minutes ago — anchor weight: heavy"` under 1 hour,
`"{h:.1f} hours ago — anchor weight: moderate"` under 4 hours, else
`"{h:.1f} hours ago — anchor weight: light, mostly faded"`.
Row lines are added newest first until the next would take the user
message (prior block included) over `budget`, then put in chronological
order; rows left out are not in `derived_from`. Called with `json=True`.

**Validation and sanitising**: the output is an object (else
reject with `no JSON object in the answer`, §6); `headline` is a string,
stripped and non-empty (else reject with `headline is missing or empty`);
nothing else rejects. A rejection is one retry with `Your previous answer
was rejected: {reason}.` appended to the user message after `\n\n`, the
container form of §6.1. A second rejection raises `ConsolidateError`
carrying the same reason text and writes nothing. Every other field is
sanitised, never rejected: `state`
lower-cased, every character outside `a-z` removed, cut to 24, `"unset"`
when empty; `subtext` and `carrier_wave` stripped strings, default `""`;
`threads` a list → each `str(t).strip()`, empties dropped, first 4 kept,
non-list → `[]`; `levels` a dict → for each string key lower-cased and
stripped, value parsed as float, non-numeric and NaN dropped, clamped to
`[0, 1]`, at most 12 axes keeping the highest, non-dict → `{}`. The brief's
four keys are required in spirit; `carrier_wave` and `levels` are kept
because the directive is built around them (decision).

This is delivered by `system_prompt()` (§5.7): the newest moods'
`carrier_wave` is rendered into the block a host installs as its system
message, and the plugin's SessionStart hook (§13.9) emits that block — so the
promise the prompt makes to future-you is kept.

**Row written.** `content` = canonical JSON (the §4.8 rules) with keys in
this order: `state, headline, subtext, carrier_wave, threads, levels`;
`source = "lake:mood"`; `kind = "mood"`; tags `["feeling:<state>"]`
followed by `add_tags`; `derived_from` = the candidate row ids in
chronological order (brief: new); `meta = {"model", "window", "filters"}`
plus `add_meta`, with `window` the §3.7 duration string (`"3h"`) or
`[a, b]`.

### 6.3 crystal

**Inputs.** The whole lake, summarised into one prompt with a character
budget `budget` (opt, default 24000). A non-`None` `window` raises
`ValueError` (a crystal has no window: it reads everything since the prior
crystal). Sections:

- prior crystal: the newest live `kind = "crystal"` row whose
  `meta.filters` equals this run's (`ORDER BY timestamp DESC, seq DESC
  LIMIT 1`; with no filters this is `crystal()`);
- containers: live `kind = "container"` rows whose `meta.filters` equals
  this run's, sorted by `level` descending then `timestamp` descending, at
  most 40;
- mood: the newest live mood whose `meta.filters` equals this run's, if
  any;
- recent rows: live rows passing the run's filters with `kind` not in
  `{container, mood, crystal}` and `timestamp` after the prior crystal's
  (or all rows when there is no prior crystal), newest first, at most 120,
  put back in chronological order.

Each section is rendered in the order above; row lines are appended until
the next line would take the prompt over `budget`, then `  … (truncated)`
closes the section. `derived_from` = `[prior crystal id] + every row id
that made it into the prompt`, in that order.

**Prompt.** System: the persona `lake/prompts/crystal.txt` (the
`system` opt replaces it, §6), then the output-schema tail
`lake/prompts/crystal_edits_tail.txt` (the clock-and-sources paragraph, the
citation rule and the JSON schema `crystal_edits_schema.txt`), always
appended. The user message opens with the clock line and a blank line (§6),
then `instructions` and a blank line when given, then
`lake/prompts/crystal_edits_user.txt` with `{max_edits}` and `{render_cap}`
(`RENDER_CAP`, the sentence "The crystal renders within {render_cap}
characters; what does not fit is not shown. Prefer fewer, fuller items.")
filled in, then the sections:

```
=== Previous crystal ({age}) ===
{one line per prior item: [{item id}] {section} · {text}; or "(none — this is the first crystal)"}

=== Positions I hold ({n} live stances) ===
{one line per live stance (§6.3.1), or "(none)"}

=== Containers — named stretches of memory ({shown} of {total}, highest level first) ===
{one container line per row, the §6 container line format}

=== Current mood ({age}) ===
{headline} — {subtext}
{carrier_wave}

=== Since the previous crystal ({n} rows) ===
{row lines, chronological}
```

`age` for the previous crystal and the mood here is `"{m} minutes ago"`
under 1 hour, `"{h:.1f} hours ago"` under 48 hours, else `"{d} days ago"`
(decision: the mood prompt's `anchor weight` suffixes belong to the mood
prompt only). All four sections are always present, in this order, with
their headers unchanged, so the model sees the same frame on every run;
an absent subject is rendered as a placeholder, not left out. With no
previous crystal, `age` is the word `none` and the body is `(none — this
is the first crystal)`. With no mood, `age` is `none` and the three body
lines are replaced by the one line `(none)`. In the containers header,
`total` is the number of live containers whose `meta.filters` equals the
run's and fewer than half of whose `derived_from` parents are automation
rows (§4.3; only when the Lake has rules), counted before the cap of 40,
and `shown` the number of container lines in the prompt. At `total == 0` the header reads `(0 of 0, highest
level first)` and the body is `(none)`.
`n` in the last header is the number of row lines in the prompt (after
the budget, so it can be below the number found); at `n == 0` the body is
`(none)`. Called with `json=True`; a rejected answer gets the §6
schema-restating retry (§6 `attempt`), and a second rejection raises
`ConsolidateError`.

The prior crystal is shown as its items. A prior crystal without
`meta.items` (a prose crystal from before simplify/core, or one a host
wrote) is split at its `## ` lines into core items `p1…pn` (the header, `: `,
then the whole facet body, whitespace collapsed). `p` items are the one
exception to two item rules: they cite nothing (a prose crystal points at no
row) and they are exempt from the 400-character limit, because an item the
answer does not mention is kept and rendered as it stands (a cut would
inject a sentence broken mid-word). A `revise` of a `p` item obeys both
rules. A stance line is `[{slug}] {position} — {label}; evidence {support};
against {against}`, most confident first, at most 12 (a scoped run shows
`(kept only by the unscoped crystal)`). Slugs, not row ids, so the stance
list is never cited.

**The crystal is cited edits** (AAA phase 4 B2, decision: every line of the
self is sourced, and change is explicit). (Decision, simplify/core: the one
crystal, on a replicate, two runs per mode on one
tree: edits loses almost no stable item (2/64 against 16/50), sides with
memory against the model's prior 6/8 against 2/8, lets twin worlds answer
differently 5/8 against 1/8, and fits the plugin's 4000-character hook whole
where every prose crystal was cut; prose won no row. The prose crystal, B1's
structured crystal and phase 5's Witness left the library.)

**Drift (recorded on the row).** With `embed` and a prior crystal,
`drift = 1 − cos(embed(new), embed(prior))` over the two crystal texts;
without `embed`, `drift = 1 − |A ∩ B| / |A ∪ B|` over the sets of lower-cased
tokens `[a-z0-9']+` (Jaccard distance); rounded to 4 places; `None` for the
first crystal. When the cosine form applies it is the same quantity the
per-rewrite cap already checked on the accepted candidate, and is reused (no
second embedding).

**Per-rewrite cap (A2).** When `embed` is set and a prior crystal exists, an
answer whose rendered text is farther from the prior crystal than
`drift_cap` (default 0.5) by `1 − cos(embed(candidate), embed(prior))` is
rejected like any invalid answer (reason `moved {d} from the prior crystal,
over the {cap} per-rewrite cap: keep more items`) and retried once; a second
candidate still over the cap raises `ConsolidateError`. The cap bounds how far
one rewrite may move: the self may drift arbitrarily far over many crystals,
never lurch in a single step. With no `embed`, or no prior crystal, the cap
does not apply (graceful degradation, since it needs vectors).

**Row written.** `content` = the rendered text (below); `source =
"lake:crystal"`; `kind = "crystal"`; tags = `add_tags` (no library tags;
decision, stated so the set is defined); `derived_from` below; `meta =
{"model", "drift": {"value", "method", "prior_id"}, "window": null,
"filters"}` plus the item keys below and `add_meta`. `meta.crystal_drift` in
the meta table is set to the §3.6 object. Then, when `write_crystal_file` is
`True` and the run had no filters, `<dir>/<stem>.crystal.md` is written
atomically (temp file, rename) with the crystal text and a trailing newline,
nothing else (a filtered crystal is one of several and stays in the file
only).

- *Answer and validation.* `{"items": [op, ...]}` with `keep {id}`, `revise {id,
  text, cite, why}`, `add {section, text, cite, why}`, `retire {id, cite,
  why}` and `resolve {id, text, cite, why}`. `section` is `core`, `tension` or `open`; `text` is 20–400 characters
  after whitespace is collapsed; `cite` goes through the §6.1 citation policy
  against the ids the prompt shows with `[id]` (containers and recent rows), so
  an item can cite only rows this run showed. An item or op that fails any of
  these is dropped with a warning in `last_run.warnings`; the answer is
  rejected when more than half of the non-keep ops are dropped. A `keep` of an unknown id is only a warning. Edits
  apply in code: an item the answer does not mention is kept (counted in
  `implicit_keep`); `keep` inherits the item's cites and `since`; `revise`
  keeps the id and section and sets `since = now`; `add` takes the next id
  `c{n}` (`n` continues from `meta.item_seq` of the prior crystal, so an id is
  never reused); only a cited `retire` removes an item, and only a `core`
  item; `resolve` takes an `open` or `tension` item, keeps its id, moves it
  to `core` with the new text and cites, sets `since = now` and records
  `resolved_from` (the old section) on the item. A `retire` naming an `open`
  or `tension` item (`retire {id} refused: {id} is an open item; resolve it:
  say what settled it`) or a `resolve` naming a `core` item (`… revise or
  retire it`) is refused with that warning and the item is kept; a refused op
  counts neither as applied nor as dropped (decision, AAA phase 5 A2: retire
  means "contradicted", so a settled question no longer renders as a dropped
  belief and the knowledge that settled it stays in the self). When the prior crystal
  has `meta.items`, more than `max_edits` applied operations reject the answer
  (`{n} edits, at most {max_edits}: keep the rest`); the first crystal (all
  `add`) and the pass over a prose prior are not bounded. The result must hold
  3–24 items, at least 2 of them `core` (`{n} valid items; a crystal needs at
  least 3, 2 of them core`). The rendered text must be at least `min_chars`
  long (`too short ({n} rendered chars, need {min_chars}): …`); with `embed`
  and a prior crystal the A2 cap applies to the rendered text, and its
  rejection is an ordinary retry.
- *Operator guard* (decision, simplify/core; phase 5's write-time guard). A
  think callback may carry `operator`, the names its account context shows
  the model (the `claude` adapter reads them from `~/.claude.json`, §8). An
  answer whose new or revised text (an item, an edit, a stance) names one of
  them as a whole word, case-insensitively, when nothing the prompt showed
  outside the prior crystal names it, is rejected like any invalid answer
  (`it names {name}, which nothing in the material does: write only what the
  rows hold`). A row that names the person makes the name material, so a
  lake about its operator still says so; kept items are not re-checked. (The
  replicate saw the operator's first name written into 5–7 of 8 world-C
  crystals, in both modes, from the account context.)
- *Rendering and the row.* `content` is the rendering: one `## {label}` block
  per non-empty section, in the order `crystal_core`, `crystal_tension`,
  `crystal_open` (§5.6.3 labels), each item one paragraph, ids not printed;
  with a prior crystal, a `## {crystal_changed}` block follows
  with up to 3 entries, this pass's edits first and then the prior crystal's
  (`crystal_revise` / `crystal_add` / `crystal_retire` / `crystal_resolve` with `old` and `new`
  cut by `oneline` to 100 characters and ended with `.` unless they end in
  `.`, `!`, `?` or `…`, plus `crystal_why`; only prior entries that are objects
  with a known op are shown). Rendered text over `RENDER_CAP` = 3000
  characters (so the crystal block fits the plugin's 4000-character
  SessionStart whole: `system_prompt()` gives an items crystal its room first,
  §5.7) drops the oldest change entries first
  (the growth log keeps them all), then trailing `open` items, then trailing
  `tension` items; `core` is never cut, and the items stay in `meta.items`
  (decision, AAA phase 5 A2: phase 4's 2400 cap with open items cut first left
  25 items of 15 measured t2 crystals out of the text; the committed items
  re-render with 0 cut under this rule). So `context()`, `system_prompt()`, the
  `.crystal.md` file and an older library read an items crystal as prose.
  `meta` adds `items` (`[{"id", "section", "text", "cite", "since"}]`, plus
  `resolved_from` on a resolved item; an older library's Witness tension also
  carries `witness`), `items_cut` (items not rendered), `item_seq` (the
  highest `c` number allocated so far), `edits` (the applied non-keep ops,
  `[{"op", "id", "old", "new", "cite", "why"}]` with `op` one of `revise`,
  `add`, `retire`, `resolve`, at most 12; `why` cut to 160
  characters) and `implicit_keep`. `derived_from` = `[prior crystal id]` plus
  the ids the items cite that the prompt showed, in item order, then the
  edit cites (the retire cites among them), deduplicated: the crystal rests on what it cites, not on
  every row shown, so the §6.4 refute trigger fires on real premises (a kept
  item's older cites stay reachable through the prior crystal's
  `derived_from`). The mood is shown but not citable, so it is not in
  `derived_from`. Drift is computed on the rendered text.
- *Growth log.* The walk of `meta.edits` from the newest crystal back
  through `meta.drift.prior_id`, newest first, skipping entries that are not
  objects with a known op (`cite` read as a list of strings, else empty) and
  stopping at a `prior_id` that is not a string; a malformed `item_seq` reads
  as 0 (the library never trusts meta) (`lake crystal --log`, §8; the
  plugin's MCP `crystal` tool with `log=true`). Stance ops (§6.3.1) are in it
  too, with `id` = `stance:<slug>`.

#### 6.3.1 Stances

AAA phase 4, B3 (decision: a position is first-class memory with its own
evidence and history, not only a sentence in the crystal). Written by the
crystal pass only (unscoped: a run with filters keeps
no stances and warns `stances ignored: a scoped crystal keeps none` when the
answer has any); no extra `think` call.

- *Ops.* The edits answer may carry `"stances": [op, ...]`
  (`lake/prompts/crystal_edits_schema.txt`): `hold {slug}` writes nothing (as
  does leaving a stance out); `new` and `revise {slug, topic, position,
  because, cite, against, why}` write one stance row; `drop {slug, cite, why}`
  writes one row with `retired: true`. The slug is lower-cased, runs of
  characters outside `[a-z0-9]` become `-`, trimmed of `-`, cut to 40.
  `position` is `oneline`d, a leading `I hold that ` and a trailing `.` are
  removed, and it must be 10–300 characters; `because` and `why` are cut to
  160, `topic` to 80 (default: the slug). `cite` and `against` go through the
  §6.1 citation policy against the ids the prompt shows (as items do).
- *Checks.* An op fails, with the warning `stance {op} {slug} dropped:
  {why}` in `last_run.warnings`, when its op is unknown, it has no slug, a
  `hold` or `drop` names no live stance, a second op names the same slug, a
  `new`/`revise` position is out of bounds, it would be the fourth `new` or
  `revise` of the pass (at most 3), it keeps no cite, or a `new`/`revise` has
  no outside evidence among its cites (below: `no outside evidence among its
  cites (…)`). A failed op never rejects the crystal and never triggers the
  retry. `new` on a live slug is a revision, and `revise` on an unknown slug
  is new.
- *The row.* Written with `Run.commit(source="lake:stance")` before the
  crystal row, one transaction each, at the run's clock: `kind = "sediment"`,
  level 0, tags `[stance:<slug>]` (plus `add_tags`), `derived_from` = the
  kept cites, content `I hold that {position}, because {because}.` (without
  `, because …` when `because` is empty) or, for a drop, `I no longer hold
  that {position}: {why}.` (position = the live one). `meta = {"model",
  "stance": {"slug", "topic", "position", "because" (null when empty),
  "against": [kept ids], "revises": id of the slug's newest row or null,
  "retired": bool}, "grounding"}` with `grounding` as §6.5 decides it over the
  support rows below (`external` for every `new`/`revise`, by the check).
  The ids join the run's input set and the crystal's `derived_from` ends with
  them, so §6.4's refute and supersession triggers reach stance evidence with
  no new walk. `last_consolidate:crystal` and `last_consolidate_id:crystal`
  are set by the crystal row only. The first stance row sets `has_stances`
  (§3.6). The ops also enter `meta.edits` (`add` for a new slug, `revise`,
  `retire` for a drop; `old`/`new` the positions; `id` `stance:<slug>`)
  after the item edits, so the "What changed" block and the growth log show
  them.
- *Live stance.* For each slug, the newest live row (by `(timestamp, seq)`)
  is its head; the slug has a live stance when the head is not retired. Older
  rows are superseded by the newer ones (§5.3 stance links).
- *Confidence*, derived at read time, never stored (`_recall.stances`):

  ```
  run     = the head and the rows before it in the slug's chain that state the same position,
            unbroken by another position or a drop (a revise that keeps the position adds its cites)
  support = distinct sessions (a `session:` tag, else source + UTC day) among the outside-evidence rows
            (§6.5: source not `lake:*`, no `assistant` tag) that are live, not live-refuted and not
            superseded, reached from the run's derived_from and from live affirm engagements of the head
            that carry an engager (`engaged_by`) or a `user` tag;
            a cited container (any source) counts through the rows it rests on, recursively
  against = the same count over the run's meta.stance.against
  value   = support / (support + against + 1) × min(valence of the head, 1.0)   # §5.3 valence
  label   = "contested" if a live refute targets the head, or against > 0 and against ≥ support
            else "firm" if value ≥ 0.6, "held" if value ≥ 0.4, else "tentative"
  since   = the timestamp of the oldest row of the run
  ```

  Three supporting sessions and no counter-evidence give 0.75 (firm); one
  gives 0.5 (held); one for and one against give 0.33 (contested). Decisions,
  AAA phase 4: an affirm of the stance counts as a supporting session (the
  valence factor is capped at 1, so without this an affirm would change
  nothing), but only a user's: one with an engager (`by`) or a `user` tag. An
  anonymous affirm (the plugin's MCP `engage` tool, which the assistant calls,
  writes `by = null` and no tags) adds nothing; a cited container counts through its rows (a crystal pass sees
  old sessions only as containers, so stable positions would otherwise have
  no citable support); `contested` needs `against > 0` (a stance whose support
  was all superseded is `tentative`, not contested). The assistant's own
  replies never count, so a stance restated only by the assistant cannot
  certify itself.

### 6.4 due(kind)

`due()` reads the file and the clock; it never calls `think` and never
writes. It answers `False` for any kind whose lease (§6) is live. The
constants below are the defaults of `Lake(due_thresholds=...)`, keyed by the
names in brackets; a host with its own cadence need not call `due()` at
all, since `consolidate()` never checks it.

- `container`: the default window (§6.1, `lookback`-bounded) names a closed
  session with an uncovered part of `min_rows` [`container_min_rows`, 3]
  rows, or contains at least one cluster of `min_rows` rows without a
  session tag, after the exclusions and noise rules; or (backfill) a closed
  session with zero coverage and such a part exists anywhere in the file
  (§6.1). The backfill clause evaluates the same discovery the run does, so
  `due` turns false once the backfill is done (decision: a due-gated timer
  would otherwise never start a backfill, and a cheaper `EXISTS` that the
  run could not act on would keep it due forever).
- `mood` (decision): let `M` be the prior mood
  (no filters), `C` the candidate rows of the default 3-hour window, and
  `pressure = Σ w(r) · 0.5^((now − ts(r)) / 14400)` over live rows after
  `M.timestamp` (all rows when no `M`) with `kind` not in `{mood, crystal}`,
  where `w(r) = 0` when `expires_at` is set or `r` is an automation row
  (§4.3; decision: a daemon writing once a minute reached the threshold
  every 25 minutes on its own, so its source is a `source:` rule), else `w(r) = weights.get(source,
  1.0) + (0.5 if tagged mood_user_tag else 0)` with `weights`
  [`mood_source_weights`, `{}`] and `mood_user_tag` [`mood_user_tag`,
  `"user"`]. Due when `C` is non-empty and (`M` is absent, or `M` is older
  than [`mood_max_age`, `"6h"`], or `pressure ≥` [`mood_pressure`, 25.0]).
- `crystal` (decision): **a memory the crystal rests on was refuted after it
  was written** — when the newest crystal, or any row in its `derived_from`
  ancestry, carries a live `refute` engagement whose timestamp is later than
  the crystal's, `due("crystal")` is true regardless of `crystal_min_age`
  below. The crystal is injected directly as the system prompt (§5.7), outside
  recall ranking, so the §5.3 boost-suspension never reaches it; this trigger
  is how a correction reaches the identity, forcing a regeneration that drops
  the overturned premise. A refute no later than the crystal was already in
  view when it was built and does not re-fire. Likewise **a memory the crystal
  rests on was superseded after it was written**: a live §5.3 link (a
  container's, or a stance revision's) asserted later than the crystal (the
  asserting container's or the newer stance's timestamp) whose `old` is in the
  crystal's `derived_from` ancestry (a stance revised by the crystal's own pass
  shares its timestamp and does not fire). **Otherwise, with no crystal
  yet (the bootstrap), the age+count trigger below.** **Otherwise, the
  material gate (decision, digestion phase 1):** due when
  the newest crystal is at least [`crystal_min_age`, `"20h"`] old and either
  at least [`crystal_containers`, 5] live library containers (`source =
  "lake:container"`) were written after it, or the age+count trigger holds.
  New consolidated material is what a crystal is rebuilt from; `max_edits`
  and `drift_cap` (§6.3) bound each rewrite, which is what makes a frequent
  rebuild safe. (An old `due_thresholds` naming the drift gate's removed keys
  is ignored, like any unknown key.)
  **The age+count trigger:** no crystal exists and the
  lake holds at least [`crystal_rows`, 50] candidate rows or one container;
  or the newest crystal is older than [`crystal_max_age`, `"3d"`] and at
  least `crystal_rows` candidate rows have been written since it. A
  candidate row here is a live row the crystal could read (§6.3 "Since the
  previous crystal": not a container, mood or crystal, no TTL, not an
  automation row); decision, digestion phase 1:
  the count used to include the library's own rows and automation, so a
  flood of automated prompts alone could make the crystal due.

### 6.5 sediment

Sediment is the fourth library write (§12.11, design fixed 2026-09-04):
after a deep recall — one executed through the plan DSL — the library reads
what surfaced and writes down, in first person, what it concludes, so the
recall itself thickens the lake no matter who asked. The pass runs inside
`recall(plan=...)` and `plan()` under the §4.5 `sediment` argument; it is
not a `consolidate()` kind (`consolidate("sediment")` is the existing
unknown-kind `ValueError`), and it never runs from `context()` or a
non-plan `recall()`. A host that wants a deliberate take instead of (or as
well as) the automatic one writes its own row with `write(kind="sediment",
derived_from=[...])` under its own source, exactly as before (§2, §4.4).

**Gates.** The pass runs when all of these hold; when any fails it writes
nothing and the recall proceeds without a warning (a gate that stays shut
is the normal case, not a failure):

- the call executed a plan and `sediment` is not `False` (§4.5);
- `think` is set and the Lake was not opened `readonly`;
- the input rows below span at least 2 distinct `source` strings.

**Input rows.** The final hits of an executed plan are what
`recall(plan=...)` returns for it: the last step's hits, or the flattened
real rows for a `timeline` last step (§5.5). An `aggregate` last step has
no final hits and never sediments. The pass reads the first 20 final hits
in hit order; the
2-source gate counts `source` values over those 20 rows, not the full hit
list, so the citation set itself spans two sources (decision: 2 distinct
sources is stricter than 2 distinct rows and subsumes it).

**Prompt.** System prompt, shipped as `lake/prompts/sediment.txt`
(it carries no grounding check: grounding is decided in code from the
citation set, so the model is not steered; see **Row written** below):

```
You are the mind distilling what it just recalled.

You'll get a query and a set of memories that surfaced in response. Your job
is to write what *you* — the mind — conclude from them. Not a summary, not a
list, not "based on the results" framing. Sediment: the compacted take that
would form naturally if this recall repeated many times.

Rules:
- Speak in first person, as the mind. "I remember" is fine. "The results
  show" is not.
- One paragraph, flowing prose. No bullets, no headers.
- If the memories contradict each other, say so — don't flatten them.
- If they converge on something specific, say that directly.
- Surface the load-bearing conclusion, not every detail. Details remain
  in the sources — that's what `derived_from` edges are for.
- Em dashes over parentheses. No staccato fragments. No mic-drop closers.
- Under 150 words.

Everything you know about this person and this time is in the memories below. Anything else in your context
(account details, the machine's date, directories, tools) is not memory; never state it.
```

User message (the pass takes no opts, so `instructions`, `system`, and
`budget` do not apply):

```
Now (the lake's clock): {now[:16]}Z

Query: "{q}"

Memories that surfaced:
{one §6 row line per input row, in hit order}
```

`q` is the plan's `search` values in step order, joined by ` · `, with
whitespace runs collapsed to single spaces — the only place a query can
enter, since `recall(plan=...)` forbids the `query` argument (§4.5). When
the plan has no `search` step the `Query:` line and its following blank
line are omitted. The row lines are the §6 format at the §6 caps (400
characters, 160 for rows of a `source:` automation rule), so the message is bounded by
construction — at most 20 rows — and is never truncated. Called with `json=False`; the answer is cleaned per §4.9.
The pass runs after the steps execute; `PlanResult.timing_ms` times the
§5.5 execution alone.

**Validation.** The cleaned answer must be non-empty after strip; the
rejection reason is `empty answer`. One retry per §6: the same system
prompt, the first user message followed by `\n\n` and `Your previous
answer was rejected: empty answer.`. On a second failure nothing is
written, the warning `sediment rejected twice: empty answer` is appended,
and the recall returns its hits regardless — a failed sediment must never
fail the recall. For the same reason, and unlike everywhere else (§4.2),
an exception raised inside `think` here is caught: the warning
`sediment failed: {exception}` is appended and the recall returns.
Warnings go to `lake.last_warnings` and `PlanResult.warnings`.

**Row written.** In one short `BEGIN IMMEDIATE` after the model returns
(no transaction open across `think`, §6): `content` = the prose, cut to
4000 characters plus `…` (the §6.1 container rule); `source =
"lake:sediment"`; `kind = "sediment"`; `level = 0`; no tags; no
`expires_at`; `derived_from` = the input rows' ids in hit order; `meta =
{"model": model_name}`, plus `"query": q` when the plan had a `search`
step, plus `"grounding": g` where `g = "external"` when at least one input
row is genuine outside evidence — its `source` does not begin with `lake:`
and it does not carry the `assistant` tag — and `"self-referential"`
otherwise. The assistant's own captured reply is self-report whatever host
source carries it, so a lake conclusion echoed into a reply and read back
cannot launder itself into external standing (the `assistant`/`user` tags
the plugin writes on replies and prompts are what tell one from the other).
The check is a pure test over the input rows already in memory — it cannot
raise, so it does not endanger the swallowed-failure contract — and it is
orthogonal to the 2-source gate: two distinct self-report sources (say
`lake:crystal` and an `assistant` reply) pass that gate yet are fully
self-referential. The signal is a machine-read meta field, not a
`grounding:*` tag and not a claim about the citation set; §5.3 reads it to
give an external sediment the level-0 container boost and a self-referential
one no boost, and §5.6.2 marks a self-referential row when it renders. The
§4.4 step 5 dedupe applies (same source, empty tag set): two
recalls that produce byte-identical prose yield one row, and the second
pass surfaces the first row. The vector, when `embed` is set, follows in
its own transaction per §4.4 step 7; an `EmbedError` there is caught and
its message appended to the warnings — the row stays committed without a
vector and `embed_missing()` reaches it later.

**Surfacing.** `PlanResult.sediment` carries the row (`None` when the
pass did not write, a gate stayed shut, or `sediment=False`). The hits of
`recall(plan=...)` are unchanged — nothing is appended to them; the row
is findable by the next recall, where the §5.3 sediment boost and the
§5.6.2 sediment renderer already apply. `lake.last_run`, the
`last_consolidate:*` and `last_consolidate_id:*` meta keys, and the
container watermark are untouched: sediment is a read-path write, not a
consolidate run.

**No lease.** The §6 lease does not apply and none is taken: sediment
rides the read path, concurrent deep recalls may each write one, and a
live `consolidate_lease:<kind>` never blocks the pass. Dedupe absorbs
identical outcomes; distinct takes are distinct rows, each citing what it
read. The CLI's `recall` opens the file `readonly` (§8) and therefore
never sediments; over the wire the server's `think` powers the pass for
every client (§13.2), which is the point — any machine's deep recall
thickens the one shared lake.

**TTL and permanence.** Timed consolidation reads permanent rows only: a
container run's candidates are `expires_at IS NULL` (§6.1), a mood run's
likewise (§6.2), and the crystal's recent-rows section drops any row
carrying an `expires_at` (§6.3, §12.3). Sediment does not share that guard.
Its inputs are the deep recall's final hits above — live rows, which
include rows whose `expires_at` is set but still in the future — and the
row it writes carries no `expires_at` of its own (above): it is permanent,
like every `lake:` write. A take distilled from a still-live expiring row
is therefore permanent. And because a `kind = "sediment"` row is not
excluded from the crystal's recent-rows section (that section drops only
`container`, `mood`, and `crystal` by kind, and only rows carrying an
`expires_at` — §6.3, §12.3), a sediment can carry the substance of a
since-expired, since-swept row into the next crystal and into identity.
This is the one path by which expiring content reaches permanent memory,
and it is intended: a TTL bounds how long a raw row stays visible, not
whether its substance can be remembered — content is remembered if it is
consolidated before it expires. A host that wants a class of rows to stay
ephemeral even under distillation keeps them out of deep-recall results —
an `exclude_sources` or `tags_exclude` on the plan's search steps (§5.5) —
since nothing in the library stops a live expiring row from being cited by
sediment.

### 6.6 digest()

`digest()` (§4.7) is the one call a host schedules: it runs whatever
consolidation is due, in order, and is idempotent (decision, simplify step B:
it replaces the timer script's three calls and the host catch-up tool,
`plugin/scripts/lake-catchup.py`, whose state lived in a JSON file under
`$LAKE_HOME`; both are thin wrappers of `lake digest` now). Steps, each due-gated and under its kind's lease (§6):

1. **Containers and backfill.** `consolidate("container")` when
   `due("container")`, once: the default pass over the watermark window, no
   unit cap unless `max_units` (§6.1), and up to `backfill_max` zero-coverage
   sessions (the §6.1 default 4 unless given).
2. **Catch-up.** `since` (`YYYY-MM-DD`, else `ValueError`) is stored in the
   `digest` meta key (§3.6); with `since=None` the stored value is used, and a
   `since` different from the stored one restarts the catch-up with a warning.
   Days run one at a time, in order, from the day after `through` (else
   `since`) up to the start of the current UTC day, each as
   `consolidate("container", (D 00:00, D+1 00:00 − 1 ms))`; §6.1's idempotent
   explicit windows make a rerun do only what is left. A day is done after one
   uncapped pass: a cluster the model answered skip for is final for that day.
   A day whose call raises counts one attempt in `attempts` and ends the
   catch-up for this digest; the third failed attempt gives the day up
   (`given_up`, and `through` moves past it). A held lease (`LeaseHeld`: a
   concurrent run, or one a killed process left) is an error for this digest
   but not an attempt, so contention never gives a day up (decision,
   simplify/core review).
3. **Mood:** `consolidate("mood")` when `due("mood")`.
4. **Crystal:** `consolidate("crystal")` when `due("crystal")` (§6.4).

A second call with nothing due makes no model call and writes no row (only
the two meta keys). An exception in a step is recorded in `errors`
(`"<step>: <ExceptionName>: <message>"`) and the next step runs; `digest()`
itself raises only `ConsolidateError` for a missing `think` (a host with no
model checks for it, as it does for `consolidate()`; over HTTP it is the 409
of §13.3), `LeaseHeld` (a `ConsolidateError`, §6: another run holds the
lease; it crosses the wire as `ConsolidateError`) for a live
`consolidate_lease:digest` (one hour, refreshed at every step and day, so a
scheduled digest, `lake digest` and `POST /v1/digest` never overlap), and
`LakeError` on a readonly Lake.

**Caps** (none by default, phase 6). `max_units` bounds container units
(sessions and clusters) across steps 1 and 2: each call gets the units left
as `max_clusters`, and a call cut by it (the `max_clusters reached: …`
warning of §6.1) ends the container steps; the next digest resumes that day.
Mood and crystal still run. `max_tokens` bounds estimated input tokens, the
prompt characters sent (system + user, counted by a wrapper around `think`)
divided by 2.2 (measured on Opus 5.5): a step or day starts only while the
count is under the cap, and a started one always finishes; it ends every
step. `stopped` names the cap that stopped work.

**Result.** A `DigestRun` (§4.1): one `DigestStep` per step and catch-up day
(`due` False and `run` None when nothing was due), the days finished and given
up, errors, warnings, `stopped`, and the calls and characters. The
`digest_last` meta key (§3.6) records the last one; `stats()` reports both
keys.

**`dry_run=True`** digests a copy made with `sqlite3.Connection.backup` at
`<dir>/<stem>.digest-dry.lake`, beside the lake (not in `/tmp`: a large file),
with a think that records each prompt's size and answers skip (a session part
takes it as its name, so there is one call per part and no supersede call);
mood and crystal are reported as due or not, never run. It refuses
(`LakeError`) when the free space is under twice the file's size, needs no
`think`, reads the real file only (a readonly Lake is enough) and deletes the
copy. The estimate therefore leaves out supersede calls and the mood and
crystal calls.

The library never schedules it: `lake digest` (§8) in a timer
(`plugin/scripts/lake-consolidate.sh`), `lake serve --digest` (§13.2), or the
host's own scheduler.

---

## 7. (reserved)

§7 is reserved.

---

## 8. CLI

Entry point `lake`. The lake comes from `--lake TARGET` (a path or an
`http(s)://` URL; `--file` and `--url` are deprecated aliases that print a
note, removed in 0.2.0, §13.8), else `lake.resolve()`
(§13.8: `LAKE`, the deprecated `LAKE_URL`/`LAKE_FILE`, the env file), else
the hidden `--default PATH` a host passes (the plugin passes
`~/.lake/claude.lake`); nothing at all is a usage error, exit 2 (`no lake:
pass a target or set LAKE`). A deprecation note from the resolver goes to
stderr as a warning. `--json` switches any command's output to one JSON
document (a list for hits, an object otherwise). `--think SPEC` and
`--embed SPEC` attach callbacks; `--model-name NAME` sets `meta.model`
(defaults to the model named in `--think`). On a local file, `consolidate`
and `digest` also take `LAKE_THINK`/`LAKE_EMBED` (§13.8) when the flag is
absent, and `digest` then falls back to `lake._env.DEFAULT_THINK`
(`claude:--model claude-opus-5-5[1m]`; `digest --dry-run` attaches none);
every other command attaches a callback only from its flag, so recall and
context stay model-free. On a remote lake `consolidate` and `digest` refuse
`--think` and `--embed` (exit 2): the model is the server's. `--automation RULE` (repeat), else `LAKE_AUTOMATION`
(comma-separated, so a `prefix:` rule cannot contain a comma; unset:
`tag:automation`; set but empty: no rule), is the
Lake's `automation` (§4.3) for every command. `--exclude-source S` and
`--collapse-source S` (repeat), and `LAKE_EXCLUDE_SOURCES`/
`LAKE_COLLAPSE_SOURCES`, are deprecated: each source adds the rule
`source:S`, with a note (decision, simplify step A: an excluded source is
now searchable and never consolidated; pass `exclude_sources` per call to
hide one). Read-only commands (`recall`, `context`, `due`,
`crystal` without `--write-file`, `stats`, `export`, `digest --dry-run`) open the file with
`readonly=True` when it exists. Warnings (`lake.last_warnings`,
`last_run.warnings`) go to stderr.

`--think` forms:

- `ollama:<model>@<url>`: POST `{url}/api/generate` with `{"model", "prompt",
  "system", "stream": false, "think": false, "options": {"num_ctx": N,
  "num_predict": P}}` plus `"format": "json"` when `json=True`; reads
  `response`. `N = max(8192, ceil(len(system + prompt) / 3) + 2048)` unless
  `--num-ctx N` is given (Ollama's default context is 2048 to 4096 tokens
  and it truncates silently, so a 7,000-token mood prompt would lose its
  prior mood, which sits at the end); `P` is 1024 for container and mood
  and 4096 for the crystal (`--num-predict` overrides). When the response's
  `prompt_eval_count` is below `len(system + prompt) / 8` the adapter
  raises `LakeError("ollama truncated the prompt: {count} tokens evaluated
  for {chars} characters")`, since the model cannot have seen the material.
  `--think-timeout S` (default 1800) bounds the request.
- `claude`: runs `claude -p --output-format text --settings '{"hooks":
  {}}' --setting-sources "" --strict-mcp-config --tools ""
  --no-session-persistence --disable-slash-commands` with `LAKE_HOOKS_OFF=1`
  in its environment (so a Claude Code plugin hook that calls `lake` does not
  re-ingest the consolidation traffic; the plugin's hooks exit 0 at once when
  that variable is set), in a fresh empty temporary directory that is
  removed after the call, prompt on stdin, `--system-prompt <system>` when
  set; with `json=True` the prompt gains a final line `Respond with only a
  JSON object.` and the output is parsed. The flags and the temp cwd keep the
  host's context out of a think call (measured with claude 2.1.281): no
  user or project `CLAUDE.md`, no per-project auto-memory, no settings (so
  no model preference: the CLI's default model; use `claude:--model X`), no
  MCP servers or their instructions (the lake's own MCP server never starts
  inside a think call), no tools or skills, no git status, and no transcript
  written under `~/.claude/projects`. What still reaches the model under
  OAuth, because no flag removes it: the account email and the environment
  block (platform, OS, model id, "Today's date"); `--bare` and
  `CLAUDE_CODE_SIMPLE` would remove it but need an API key and break OAuth.
  The §6 clock line and sources paragraph are the prompt-side answer, and it
  is not airtight: in AAA phase 4 a prose crystal pass wrote the operator's
  first name (from the account email) into 4 of 24 crystals despite it.
  The email arrives as a `userEmail` context
  block beside the prompt (confirmed by a logged probe). So the adapter also carries the names:
  the callback's `operator` attribute is `~/.claude.json`'s `oauthAccount`
  `displayName`, `fullName` and `emailAddress` (and the email's local part),
  words of 3+ characters, read once when the adapter is made (`()` when the
  file is unreadable); the crystal refuses a new text that names one of them
  when nothing it was shown does (§6.3 operator guard). Any think callback may
  carry `operator` the same way. An older CLI that lacks a flag fails
  loudly (`LakeError`).
- `claude:<args>`: the `claude` adapter plus extra CLI arguments, split with
  `shlex` and placed before `--system-prompt` (`claude:--model sonnet`);
  `meta.model` defaults to `claude <args>`. This is the form for choosing a
  model; it keeps the system prompt, the JSON line and the isolation above.
- `cmd:<shell command>`: the prompt on stdin, the system prompt in the
  environment variable `LAKE_SYSTEM` (empty when none), `LAKE_JSON=1` when
  `json=True`, `LAKE_HOOKS_OFF=1`; stdout is the answer. The command runs in
  the caller's cwd with the caller's environment and gets none of the
  `claude` adapter's isolation: a `cmd:` that runs `claude` sees the host's
  `CLAUDE.md`, auto-memory, MCP server instructions, skills and tools, as
  the `claude` adapter did before AAA phase 3. The command receives
  the system prompt only through `$LAKE_SYSTEM`, and a command that cannot
  take one can prepend it to stdin (`cmd:{ printf '%s\n\n' "$LAKE_SYSTEM";
  cat; } | ollama run X`). A `cmd:` whose first command word is `claude` and
  whose text, comments dropped, never names `LAKE_SYSTEM` is refused at
  `make_think` with `CmdClaudeError`, a `ValueError` (CLI exit 2; `lake
  serve` logs it and starts with no think callback, §13.2): `cmd:claude …
  never receives the lake's system prompt and gets none of the claude
  adapter's isolation from the host's context (CLAUDE.md, memory, MCP
  servers, tools); use claude:<args> (e.g. claude:--model sonnet)`. The first
  command word is found by basename after leading `VAR=value` words, the
  wrappers `env`, `exec`, `command`, `nice`, `nohup`, `timeout`, `stdbuf`,
  `ionice`, `setsid` and `time` (with their flags, a flag's argument, and
  `timeout`'s duration), and inside `sh|bash|dash|zsh -c '…'` (decision: a
  model that never saw the schema answered 0 of 24 containers in it; the
  message recommends only `claude:<args>`, the one form that also carries
  the isolation; `cmd:claude -p --system-prompt "$LAKE_SYSTEM"` is allowed
  but unisolated. The guard is best-effort for this one known trap, not a
  shell parser: an alias or a wrapper script slips past it; every other
  `cmd:` is unchanged, including wrapper scripts that read `$LAKE_SYSTEM`).

`--embed` forms: `ollama:<model>@<url>` (POST `{url}/api/embed` with
`{"model", "input": [texts]}`, reads `embeddings`; `--embed-timeout S`,
default 10, raises on expiry, which `recall`/`context` turn into an FTS-only
result with a stderr warning) and `cmd:<shell command>` (a JSON list of
strings on stdin, a JSON list of float lists on stdout).

| command | flags | does |
|---|---|---|
| `write CONTENT` | `--source S` (required), `--tag T` (repeat), `--kind K`, `--from ID` (repeat), `--expires DUR\|TIME`, `--media HASH`, `--meta JSON`, `--timestamp TIME`, `--no-dedupe`, `--no-embed`; `CONTENT` of `-` reads stdin | `write()`; prints the id |
| `recall [QUERY]` | `--source` (repeat), `--tag` (repeat, all), `--any-tag` (repeat), `--exclude-tag` (repeat), `--kind`, `--since`, `--until`, `--limit N` (20), `--include-expired`, `--no-noise`, `--no-recency`, `--min-relevance F`, `--plan FILE\|-` | `recall()`; prints one line per hit `{score:.3f}  {id}  {timestamp[:16]}  {source}  {oneline(content, 100)}`, or the plan's every step with `--json` |
| `context [QUERY]` | `--budget N` (8000), `--limit N` (30), `--source` (repeat), `--tag`, `--any-tag`, `--exclude-tag` (repeat), `--kind`, `--since`, `--until`, `--no-crystal`, `--no-containers`, `--no-noise`, `--no-recency`, `--min-relevance F`, `--min-query-chars N` (10), `--vector`, `--label KEY=VALUE` (repeat) | `context()`; prints the block. A query shorter than `--min-query-chars` prints the same as no query. Vectors are used only with `--vector` (decision: the command is a fresh process per prompt and the hook budget is 300 ms; without the flag `embed` is not attached and the render is FTS-only) |
| `engage ID KIND` | `--by`, `--note`, `--tag` (repeat), `--no-snapshot`, `--no-dedupe` | `engage()`; prints the engagement id |
| `consolidate KIND` | `--window DUR`, `--since TIME --until TIME`, `--advance-watermark`, `--inputs ID...`, `--close-gap DUR`, `--cluster-gap DUR`, `--lookback DUR`, `--max-clusters N`, `--min-rows N`, `--max-rows N`, `--budget N`, `--source` (repeat), `--tag`, `--any-tag`, `--exclude-tag`, `--no-noise`, `--add-tag T` (repeat), `--add-meta JSON`, `--system FILE`, `--instructions FILE`, `--min-chars N`, `--lease DUR`, `--session-prefix P`, `--session-gap DUR`, `--no-backfill`, `--backfill-max N`, `--max-edits N`, `--force` | `consolidate()`; without `--force` runs only when `due(KIND)`; prints every id written (one per line), then `skipped: n` and each warning on stderr, or `nothing to do` |
| `digest` | `--since YYYY-MM-DD`, `--max-units N`, `--max-tokens N`, `--backfill-max N`, `--dry-run` | `digest()` (§6.6); prints one line per step (`container: 12 written, 3 skipped, ~41k input tokens`, `catch-up 2026-09-08: …`, `mood: nothing due`), an `error: …` line per failed step, then the `summary` line (`12 containers, 3 skipped, mood 1, crystal 0, 41 calls, ~310k input tokens, 0 errors`); `--json` prints the `DigestRun`. Exit 1 when a step failed (the others still ran) |
| `due KIND` | | exit 0 when due, 1 otherwise; prints `yes`/`no` |
| `crystal` | `--write-file`, `--log`, `--limit N` (20) | prints the crystal text; `--write-file` also rewrites `<stem>.crystal.md`; `--log` prints the §6.3 growth log instead, newest first, one edit per line as `{ts[:16]} · {op} · {old} → {new} · {why} · {cites}` (`—` for an empty field; `--json` gives the entries with `at` and `crystal` added), at most `--limit` lines; it reads only `crystal()` and `get()`, so it works on a remote lake |
| `lineage ID` | `--depth N` | `lineage()`; one line per ancestor `{hops}  {id}  {timestamp[:16]}  {source}  {kind or "plain"}  {oneline(content, 80)}`, then `dangling: ...` when any |
| `stats` | | `stats()` as `key: value` lines |
| `sweep` | | `sweep()`; prints the counts |
| `export PATH` | `--include-expired`, `--vectors` | `export()`; prints the row count |
| `import PATH` | `--include-expired` | `import_()`; prints written/skipped/errors |
| `embed-missing` | `--batch-size N` | `embed_missing()`; prints the count |

Exit codes: 0 success, 1 a library error (message on stderr), 2 usage.

---

## 9. Concurrency and limits

**Processes.** The file is in WAL mode with a 5 s `busy_timeout`, so any
number of processes on one machine may open it; readers never block writers
and one writer at a time proceeds while the others wait up to 5 s before
`sqlite3.OperationalError: database is locked` surfaces. Every row write is
one short `BEGIN IMMEDIATE` transaction with dedupe and id generation inside
it, so two processes writing the same content at once produce one row; the
`embed` call and the `think` call always happen with no transaction open,
and the vector goes in afterwards in its own transaction (§4.4, §6). The
lock is held for milliseconds; a model call never holds it.

**Threads.** A `Lake` instance is bound to one connection and is not
thread-safe. Open one instance per thread or process.

**Servers and async hosts.** Instances are cheap and short-lived instances
are the expected shape: a request-per-reader web server opens a `Lake` per
request (or keeps one per worker thread) and closes it. The vector cache
is per instance, so a fresh instance pays the matrix load on its first
vector query unless it uses the §3.4 sidecar; FTS-only recall has no
warm-up. `think` and `embed` are blocking calls made on the caller's
thread, so on an event loop `consolidate()` and any recall with `embed`
attached run in a worker thread (`asyncio.to_thread`). The library never
adds module-level caching to compensate: no global state.

**Not supported.** A file on a network filesystem (WAL needs shared memory
on one host); a file inside a live-syncing folder while it
is open (the `-wal` and `-shm` files sync as garbage; the repository's
`.gitignore` already excludes `*.lake*`); several writers across a network
(put a server in front, which is a host); schema migration from any
version other than 1 (there is none yet).

**Sizes.** Designed for up to about 1,000,000 rows; tested at 50,000. At
50,000 rows with 500-byte contents and 512-dimension vectors the file is
about 130 MB (100 MB of it vectors). Vectors dominate; a lake without
`embed` at the same row count is about 35 MB.

**Latency targets** for `context()` at 50,000 rows, one query, on a 2024
laptop: 150 ms without `embed` (FTS, filters, strips, render), from a fresh
process included, which is the hook path (the CLI attaches `embed` only
with `--vector`); 300 ms with `embed` and numpy in a long-lived instance
with a warm matrix, or from a fresh process with the §3.4 sidecar,
excluding the host's embed call; 5 s with `embed` in pure Python (the dot
products over 50,000 × 512 floats are the cost; numpy is the recommended
extra above 10,000 rows). `write()` under 20 ms excluding embed, at any
row count (dedupe is one index probe). `import_()` of 50,000 lines
under 60 s without vectors. These are design targets, measured by hand on
that laptop. `test_scale` (§10) builds a synthetic lake of the same 50,000
rows and pins looser bounds a CI runner can hold. Those bounds are 1 s for
the FTS-only `context()`, 50 ms for `write()`, 60 s for `import_()`, and
1 s for `due("container")`.

**Code.** Type hints throughout; no global state; a line budget for
`lake/` including the CLI and the remote layer (`tests/test_limits.py` holds
the current number and the history of every change to it).
`test_code_limits` (`tests/test_limits.py`) checks all three, so the bar is held by the suite
and not by a reviewer's eye.

---

## 10. Test plan

The suite (`tests/`) is the test plan; a test's
docstring names the section it pins. `test_code_limits` (`tests/test_limits.py`)
holds the §9 line budget, `mypy --strict lake`, and the no-global-state rule.

---

## 11. Open questions

(reserved)

---

## 12. Resolved after review

Decisions taken after the completeness check. Each overrides the section it
names; implementers follow this section where it and the body disagree.

1. **Dedupe TTL refresh never puts a TTL on a permanent row (§4.4 step 5).**
   The refresh applies only when the existing row already has an
   `expires_at` and the new value is later. A row with `expires_at = NULL`
   stays NULL whatever the deduped write carries. `test_dedupe_ttl_refresh`
   gains: write A with no `expires`, then A with `expires="1h"` → one row,
   `expires_at` still NULL.
2. **A crystal run with nothing to read returns `None` (§6.3).** When there
   is no prior crystal, no container, no mood, and no candidate row after
   the exclusions, `consolidate("crystal")` returns `None` without calling
   `think`, exactly as the other two kinds do.
3. **The crystal's recent-rows section uses the same exclusions as the
   other kinds (§6.3).** Rows whose `source` is in `collapse_sources`, rows
   with an `expires_at`, and rows matching a §5.4 hard rule (when
   `noise=True`) are left out of "since the previous crystal". (This
   firewall does not reach sediment: a `kind = "sediment"` row is permanent
   and stays in the recent-rows section, so content a sediment distilled
   from a still-live expiring row — §6.5 — still enters the next crystal.)
4. **`meta.window` for a default container run (§6, §4.1).** Stored as
   `[a, b]` in the stored timestamp format, where `a` is the lower bound the
   run actually used (the later of the watermark position and
   `now − lookback`) and `b` is `now − close_gap`. `ConsolidateRun.window`
   is the same pair.
5. **`boost` applies only when a query is present (§5.3).** Without a query
   the score is `recency × valence` (`relevance = 1.0`, `noise = 1.0`,
   `boost = 1.0`), and the order is `(timestamp DESC, seq DESC)` regardless.
6. **A plan step with no `id` gets its 0-based index as a string (§5.5).**
   `[{"search": "x"}, {"filter": {...}}]` runs as ids `"0"` and `"1"`. A
   duplicate id, explicit or implied, is still `PlanError`.
7. **Container rejection reasons are these strings (§6.1):** `kind must be
   "propose" or "skip"`, `title is empty`, `title exceeds 200 characters`,
   `summary is empty`, and the existing `{k} of {n} ids are not in the
   stretch: ...`.
8. **`import_()` rejects relative timestamps.** A line whose `timestamp` is
   the `N units ago` form counts as an error line; only absolute forms
   import.
9. **Small edges.** `cited_by()` on an unknown id raises `NotFoundError`,
   like `lineage()`. `embed_missing(limit=N)` embeds at most `N` rows in
   this call and returns the count stored. `sqlite3.OperationalError`
   (`database is locked` after the 5 s `busy_timeout`) propagates unwrapped
   from any method; it is the one non-`LakeError` exception the library
   lets through on purpose.
10. **Open question 8 is resolved: the mood `state` sanitiser keeps `-`.**
    `feeling:self-doubt`, not `feeling:selfdoubt`. `test_consolidate_mood`
    asserts `feeling:self-doubt`.
11. **Sediment is automatic on the deep path (fixed 2026-09-04;
    §1, §2, §4.1, §4.2, §4.5, §6, §6.5, §10, §13).** Sediment rides the
    `think` callback: every plan-based recall whose read rows span at
    least two distinct sources writes a first-person `lake:sediment` row
    citing what it read — no matter who asked — and the row is returned
    to the caller on `PlanResult.sediment` alongside the hits. A caller
    such as Claude can additionally write its own deliberate sediment
    with `write(kind="sediment", derived_from=[...])` under its own
    source, as before. This supersedes two sentences: §1's "the library
    never writes sediment itself" clause of the closed-loop invariant
    (the invariant stands — the row cites its inputs and `write()` still
    refuses every `lake:` source) and the matching §2 Kind sentence; both
    are rewritten, `lake:sediment` joins the §2 reserved sources, and §6's
    "only place `think` runs" now names both places. The section edits:
    §4.5 (`sediment: bool | None = None` on `recall()`/`plan()`; with
    `plan`, `filters` and `sediment` are the two arguments allowed off
    default), §6.5 (the pass: gates, prompt, row, failure rules, no
    lease), §4.1 and §13.1 (`PlanResult.sediment`), §13.2/§13.3/§13.6
    (the wire field; `/v1/recall` and `/v1/plan` open writable when the
    server has a `think` — the server's think powers sediment for every
    client, which is the point of the shared-mind shape), §10 and §13.10
    (the `test_sediment_*` and `test_serve_sediment` rows). Decisions
    this amendment makes beyond the fixed design, all pinned in §6.5:
    the 2-source gate counts sources over the rows actually rendered
    (the first 20 final hits), so the citation set itself spans two
    sources; the floor is ≥ 2 distinct sources, not ≥ 2 distinct
    rows; `sediment=True` without `plan` is `ValueError`, and with a
    plan it equals `None` today; a `readonly` open never sediments, so
    the CLI's `recall` (§8) never does; the prompt's `Query:` line and
    `meta.query` come from the plan's `search` strings, the only place a
    query can enter the plan path; content is cut at 4000 characters like
    a container's; `think` exceptions are caught into a warning on this
    path alone; there are no `grounding:*` tags, no prompt-side
    grounding check, no fixed temperature, and no prompt
    truncation; grounding is instead computed in code from
    the citation set into a `meta.grounding` signal (`self-referential` |
    `external`, §6.5) that the §5.3 boost and §5.6.2 renderer read — a
    machine signal, not a tag, and not a service-list check;
    `PlanResult.timing_ms` excludes the pass.
12. **Refutation is mark-and-re-rank only (§4.5, §5.3, §5.6.2, §13.1).**
    Per the never-delete rule (§12), a refuted row is demoted in the
    query-present score path (one net refute ×0.50, floor ×0.30) and its
    refutations are surfaced as receipts on read (`Delta.refuted_by`); the
    target row and its engagement rows stay fully intact and are never
    deleted or excluded from any candidate/WHERE query. `refuted_by` is
    derived at read time, never stored and never exported, so `export_line`
    stays byte-for-byte identical and only the user-facing reads
    (`recall`, `get()`/`resolve()`, context render) attach it.
13. **`system_prompt()` renders crystal + recent moods as a system-prompt
    block (§4.5, §5.7, §6.2, §13).** The library method a Python host passes
    as its actual system message, so the crystal shapes behaviour from turn 1.
    It reads the newest live crystal (`_store.newest`, the same row `crystal()`
    and `context()` block 1 use) and the newest `moods` live `kind="mood"`
    rows (`ORDER BY timestamp DESC, seq DESC`, default 3), and returns the
    §5.6.3 crystal block followed by a mood section carrying each mood's
    `carrier_wave`. It is read-only — no `think`, no `embed`, never sediments —
    and returns `""` when there is neither a crystal nor a mood. With no mood
    (`moods=0`, or none exist) it returns **exactly** `crystal_block(crys,
    budget)`, byte-for-byte `context(None)` with no trailing separator; only
    when a non-empty mood section renders is the crystal block cut to `budget −
    len(section) − 1` and the two joined by one newline. The mood section is
    budgeted at `budget × 2 // 5`; the crystal block takes the remainder. This
    is what makes §6.2's `carrier_wave` promise true: the plugin SessionStart
    hook (§13.9) now emits `system_prompt` output, and a Python host passes it
    as the system role directly.
14. **Supersession links (AAA phase 3; was §11 item 17; §3.2, §4.1, §4.5,
    §5.3, §5.6.2, §6.1, §13.1).** A correction no longer outranks the value it
    replaced only because it is newer. Session consolidation flags the rows
    that change something (`changes`), code retrieves the earlier rows they
    may replace, one supersede call names pairs, and code keeps only pairs
    whose values are quoted verbatim from both rows; they are stored as
    `meta.supersedes` on the session container (additive: no DDL, no
    `schema_version` change, no `supersede` engagement kind — the
    `engagements.kind` CHECK cannot change without rebuilding the table, and
    `engage()` refuses `lake:` engagers). Reading demotes the stale row ×0.50,
    caps it under its superseder, suspends the boost of summaries built on it,
    and attaches `superseded_by` receipts; nothing is deleted or hidden, and
    refuting the container withdraws its links. Measured with scripted links
    on base.lake: §5.3 *Supersession*.

15. **The Witness.** Removed by the simplify design.

---

## 13. Remote: serve, client, spool

Decisions fixed 2026-09-04. Like §12,
this section overrides the body where they disagree; implementers follow it.
Where the remote brief is silent, decisions this section makes on its own
are marked "(decision)".

One shared mind: the lake file lives on one machine, beside the strongest
model, and other machines connect over HTTP. The library stays the core;
the server is a thin host shipped in the package. The per-machine file
remains the default shape; the server is what a host adds the day it has
two machines. The server lives in `lake/serve.py`; the client, the spool,
and the env-file reader live in `lake/remote.py` (decision: the brief names
only `serve.py`; the client side needs a home and the CLI imports it). Both
are stdlib only — `http.server`, `urllib`, `json`, `fcntl`, `socket`,
`hmac` — so the zero-dependency story extends to the remote layer. No
module-level mutable state, as everywhere (§9).

Remote is an optional conformance layer: an implementation without it still
conforms to the core profile (§1). One that provides a server or a client
must produce §13.1 and §13.3 to §13.5 exactly — the wire format is the
contract that lets one client speak to any server.

### 13.1 Wire format

Every §4.1 dataclass crosses the wire as a JSON object holding exactly its
fields — `dataclasses.asdict` shapes: `None` as `null`, tuples as arrays,
floats as JSON numbers (finite by construction, §3.2), timestamps as stored-
format strings (§3.7), ids as the 12-hex ids. Every field is always present
in a response; there are no optional response keys. Key order in API bodies
is not significant (only the `/v1/export` stream pins bytes, §13.5); a
server should emit the §4.8 export key order for a `Delta`. Bodies are
UTF-8; both sides serialise with `allow_nan=False`.

- `Engagement` — `{"target_id": str, "kind": "affirm"|"refute"|"reply",
  "by": str|null, "note": str|null}`.
- `Delta` — the twelve §4.1 fields. The §4.8 example row as a wire object:

  ```
  {"id": "3f2a9c1b7d4e",
   "timestamp": "2026-08-30T14:05:12.345Z",
   "content": "what did we decide about drift thresholds?",
   "source": "claude-code",
   "kind": null,
   "level": 0,
   "tags": ["user", "session:9c4e1f2a"],
   "derived_from": [],
   "engagement": null,
   "expires_at": null,
   "media_hash": null,
   "meta": null}
  ```

  `tags` and `derived_from` are always arrays (`[]`, never `null`);
  `engagement` is the `Engagement` object or `null`; `meta` is the stored
  JSON object or `null`. A `Delta` crosses in the §4.8 export key order (via
  `export_line`); when its derived `refuted_by` is non-empty, a trailing
  `refuted_by` array (each `Refutation` as
  `{"id", "source", "timestamp", "note"}`) is appended after `meta`.
  `export_line` itself is unchanged, so file export never carries the key;
  the HTTP `/get` and `/recall` surfaces carry it only for rows that are
  actually refuted. A row whose derived `rests_on_refuted` (§5.3) is
  non-empty likewise appends a trailing `rests_on_refuted` array of the
  corrected ancestor ids after `refuted_by`; the same rule holds — never in
  `export_line`, present on `/get` and `/recall` only when non-empty. A row
  whose derived `superseded_by` (§5.3) is non-empty appends, last, a trailing
  `superseded_by` array (each `Supersession` as `{"id", "by", "old_value",
  "new_value"}`), under the same rule. The client reads it with `.get`: an
  older client ignores the key, and a newer client of an older server gets
  `()`. Links are never written by hosts, so the spool never carries them.
- `Hit` — `{"delta": Delta, "score": float, "relevance": float,
  "recency": float, "valence": float, "matched": str, "step": str|null}`.
- `Bucket` — `{"key": str, "count": int, "delta_ids": [str]}`.
- `CollapsedRun` — `{"source": str, "count": int, "t_start": ts,
  "t_end": ts}`.
- `TimelineRow` — `{"delta": Delta, "is_anchor": bool}`.
- `Timeline` — `{"id": str, "t_start": ts, "t_end": ts,
  "anchor_ids": [str], "rows": [...]}` where a `rows` entry carrying a
  `"delta"` key is a `TimelineRow` and one carrying a `"count"` key is a
  `CollapsedRun` (decision: the key sets discriminate; the two shapes share
  neither key).
- `StepResult` — `{"hits": [Hit]|null, "buckets": [Bucket]|null,
  "timelines": [Timeline]|null}`.
- `PlanResult` — `{"steps": [[id, StepResult], ...], "warnings": [str],
  "timing_ms": float, "sediment": Delta|null}`. `steps` is an array of
  two-element `[id, result]` pairs in plan order (decision: the §4.1
  list-of-pairs escape hatch, chosen on the wire because JSON object key
  order is not guaranteed by every consumer; the Python client rebuilds
  the ordered mapping). `sediment` is the §6.5 row or `null`.
- `ContextResult` — `{"crystal": Delta|null, "hits": [Hit],
  "containers": [Delta], "strips": [Timeline], "omitted_strips": int,
  "rendered": str, "warnings": [str]}`.
- `Lineage` — `{"rows": [Delta], "dangling": [str]}`.
- `ConsolidateRun` — `{"kind": str, "written": [Delta], "skipped": int,
  "warnings": [str], "think_calls": int, "window": [ts, ts]|null}`.

**Request bodies.** An omitted key and an explicit `null` both mean the §4
signature default. `TimeSpec` and `Duration` arguments cross as their §3.7
string forms; the client renders a `datetime` with the §3.7 producer and a
`timedelta` with the largest-unit duration rule before sending. Values
nested inside `plan`, `filters`, `meta`, or `labels` must already be JSON
values; one the client cannot serialise raises `ValueError` (decision).
An unknown top-level body field is `400` `ValueError` with message
`unknown field '{key}'` (decision: the library raises on unknown
`consolidate` opts, and a field sent and ignored would hide a mistake,
§6.1).

**Query parameters** (GET endpoints only): booleans are sent as `1` when
not at their default and omitted otherwise; the server accepts `1`/`true`
and `0`/`false` and answers `400` `ValueError` with `bad query parameter
{key}={value}` for anything else; integers cross as decimal strings.

### 13.2 Server — `lake serve`

```
lake serve [--bind IP] [--port N] [--lake PATH]
           [--think SPEC] [--embed SPEC] [--model-name NAME]
           [--num-ctx N] [--num-predict N] [--think-timeout S] [--embed-timeout S]
           [--automation RULE ...]
           [--digest off|nightly|HH:MM|every:DURATION] [--backfill-max N] [--max-units N] [--max-tokens N]
```

Defaults `--bind 127.0.0.1`, `--port 8377`, `--digest off`. Startup, in order:

1. Resolve the file: `--lake` (or `--file`), else `lake.resolve()` (§13.8).
   Missing → usage error, exit 2 (§8). `serve` always hosts a local file:
   a target that resolves to a URL is a usage error, exit 2 (`lake serve
   hosts a file; the lake resolves to the URL {url} (pass --lake PATH)`).
   A `--digest` value other than the four forms is a usage error, exit 2.
2. Resolve the token: `LAKE_TOKEN` per key (§13.8 `setting`), else the
   content of `~/.lake/token` stripped of surrounding whitespace. There is
   no `--token` flag (decision: argv leaks into process listings). When
   `~/.lake/token` is consulted and its mode grants any group or other
   permission (`mode & 0o077 != 0`), refuse to start (decision:
   machine-enforced hygiene; the ssh precedent).
3. Loopback means the bind address is exactly `127.0.0.1`, `::1`, or
   `localhost` (decision: deliberately narrow, so the refusal is testable
   by binding the loopback alias `127.0.0.2`). A non-loopback bind with no
   token refuses to start.
4. Build the `--think` and `--embed` callbacks (§8); an unknown spec is a
   usage error, exit 2. The one exception is the refused `cmd:claude …`
   think spec (§8 `CmdClaudeError`): the server logs `lake serve: {message};
   serving with no think callback (consolidate fails)` and starts with no
   think, so reads, writes and hooks keep their server and only
   `/v1/consolidate` fails (decision, AAA phase 3 review: refusing to start
   turned a consolidation misconfiguration into an outage of the whole
   shared server). Open the Lake once to create or validate the file
   (§4.3), then close it. A `SchemaError` is fatal.
5. Bind, announce, serve. `http.server.ThreadingHTTPServer`, one thread
   per request, no server-side request timeout (the brief: the client owns
   timeouts). With `--digest`, start the digest thread (below) and announce
   it. SIGINT and SIGTERM exit 0.

Startup messages, verbatim (refusals to stderr, exit 1; the serving line to
stderr):

```
lake serve: refusing to bind {bind} without a token — set LAKE_TOKEN or write ~/.lake/token (0600)
lake serve: ~/.lake/token is readable by others — chmod 600 ~/.lake/token
lake serve: serving {path} on http://{bind}:{port}
lake serve: digest daily at {HH:MM} local time          (or: digest every {duration})
```

**Auth.** When a token is configured, every request except
`GET /v1/health` must carry `Authorization: Bearer {token}`, compared with
`hmac.compare_digest`. A missing or wrong token answers `401` with header
`WWW-Authenticate: Bearer` and envelope `{"error": "LakeError", "message":
"unauthorized"}` (decision: the library has no auth class; the client
re-raises `LakeError`). With no token configured (loopback only, by rule 3)
the header is not required and its value is ignored. No TLS in v1: the
transport is a trusted network (a VPN, a LAN, an SSH tunnel) — the token
authenticates, the network encrypts; the docs say so.

**Callbacks.** `think` and `embed` come from `--think`/`--embed`, else
`LAKE_THINK`/`LAKE_EMBED` per key (§13.8), built exactly as the
§8 adapter specs with the §8 flags; with `--digest` on and neither set,
think is `lake._env.DEFAULT_THINK` (§8). `--model-name` defaults to the model
named in `--think` (§8). They exist only on the server: a client never
sends specs. One think config serves `/v1/consolidate`, `/v1/digest`, the
sediment pass and the scheduled digest (decision, simplify step B: a
timer beside the server would otherwise need its own think). On `POST /v1/consolidate` with no think configured the server
answers `409` before opening the Lake, message verbatim (decision: the
brief asks for a clear message; this is it):

```
{"error": "ConsolidateError", "message": "consolidate needs a server-side think: start lake serve with --think or set LAKE_THINK in ~/.lake/env on the server"}
```

**Request cycle.** One short-lived `Lake` per request (§9: instances are
cheap; WAL does the coordination), constructed with the file, the
callbacks, `model_name`, and `automation` from `--automation`, else
`LAKE_AUTOMATION` (§8; unset: `tag:automation`), read once at startup like
everything else the environment says (decision, simplify step A: before it,
the rules were re-read from the process environment on every request and
had no default). The deprecated `--exclude-source`/`--collapse-source` and
`LAKE_EXCLUDE_SOURCES`/`LAKE_COLLAPSE_SOURCES` add `source:` rules, as in
§8; nothing is unioned into a request's filters any more. The read endpoints — `get`, `lineage`,
`cited-by`, `crystal`, `due`, `stats`, `export`, `health`, and the POST
reads `context` and `system-prompt` — open `readonly=True`; the write
endpoints open normally.
`recall` and `plan` open `readonly=True` only when the server has no
`think`: with one configured they open normally, because a deep recall may
write a sediment row (§6.5) — the server's `think` powers sediment for
every client (§12.11), so any machine's deep recall thickens the one
shared lake. A server with `--embed` attaches it to every
request's Lake (writes embed per §4.4, recall reads vectors per §5.2) and
pays the §9 fresh-instance vector cost per request unless the §3.4 sidecar
exists.

**`--digest`** (decision, simplify step B: the server owns digesting for a
network lake, so one service replaces the server plus a timer). `nightly` is
`01:00`; `HH:MM` is daily in the server's local time; `every:<duration>` takes
a §3.7 Duration greater than zero. One daemon thread (host code in
`serve.py`; `Lake` has no scheduler) opens its own Lake with the server's one
configuration per run, calls `digest(backfill_max=, max_units=, max_tokens=)`
from the flags, logs one line to stderr (`lake serve: digest done — {summary}`,
§8, or `lake serve: digest failed: {ExceptionName}: {message}`) and waits for
the next slot. Every exception in the loop is caught and logged, the
pre-check included, so the thread never dies and the request threads never
see one. **Missed runs** (the old timer's `Persistent=true`): the digest runs
at once when the last one (`digest_last.at`, §3.6, or this process's last
attempt) is older than the most recent slot (daily) or than one interval
(`every:`); never digested counts as older. A failed digest waits for the
next slot like a finished one; a failed pre-check (the file locked or gone)
and a held lease (`LeaseHeld`: a restart right after a killed digest) are
retried every 300 seconds instead, so the slot is not given up to the next
day (decision, simplify/core review). The thread and the requests share the file through WAL,
`busy_timeout` and the rule that no transaction spans a model call (§9); the
digest lease and the per-kind leases serialise it against `/v1/consolidate`
and `/v1/digest`. SIGTERM mid-digest leaves those leases until they expire
(one hour); every written container is committed and the next digest
resumes. For a host that owns a local file the systemd timer is optional:
the timer (`lake digest`) or `lake serve --bind 127.0.0.1 --digest nightly`.

A POST body requires `Content-Length` (`400` `ValueError`
`Content-Length required`; chunked bodies are not accepted). Responses are
`application/json` in UTF-8, except `/v1/export` (§13.5). Connections are
not kept alive; the client opens one per request. The server logs one line
per request to stderr; the line format is not part of the contract.

### 13.3 Endpoints

| method and path | maps to | response |
|---|---|---|
| `POST /v1/write` | `write()` | `Delta` |
| `POST /v1/engage` | `engage()` | `Delta` |
| `POST /v1/recall` | `recall()` | `{"hits": [Hit], "warnings": [str]}` |
| `POST /v1/plan` | `plan()` | `PlanResult` |
| `POST /v1/context` | `context_blocks()` | `ContextResult` |
| `POST /v1/system-prompt` | `system_prompt()` | `{"prompt": str}` |
| `POST /v1/consolidate` | `consolidate()` | `{"delta": Delta\|null, "run": ConsolidateRun}` |
| `POST /v1/digest` | `digest()` | `{"run": DigestRun}` |
| `POST /v1/sweep` | `sweep()` | `{"deleted": int, "orphan_media": int}` |
| `POST /v1/import` | `import_()` | `{"written": int, "skipped": int, "errors": int}` |
| `POST /v1/embed-missing` | `embed_missing()` | `{"stored": int}` |
| `GET /v1/get/{id}` | `get()` | `Delta` or `null` |
| `GET /v1/lineage/{id}` | `lineage()` | `Lineage` |
| `GET /v1/cited-by/{id}` | `cited_by()` | `{"rows": [Delta]}` |
| `GET /v1/crystal` | `crystal()` | `Delta` or `null` |
| `GET /v1/due/{kind}` | `due()` | `{"due": bool}` |
| `GET /v1/stats` | `stats()` | the §4.8 stats object |
| `GET /v1/export` | `export()` | NDJSON stream (§13.5) |
| `GET /v1/health` | — | `{"ok": true, "rows": int}` |

`/v1/plan` is not in the brief's endpoint list and is added because
`RemoteLake` implements the same public methods and `plan()` returns every
step, which `/v1/recall` cannot carry (decision). An unknown method-and-
path pair answers `404` with `{"error": "LakeError", "message":
"unknown endpoint: {METHOD} {path}"}` (decision). A body that is not a
JSON object answers `400` `ValueError` `invalid JSON body: {reason}`
(decision).

Request bodies, field by field. Types read `type, default`; every field is
optional unless marked required; §13.1's omitted-equals-null rule applies.

```
POST /v1/write
  content       str, required          §4.4
  source        str, required
  tags          [str], null
  kind          str, null
  derived_from  [str], null
  expires       str, null              a §3.7 Duration or TimeSpec string
  media         str, null
  meta          object, null
  timestamp     str, null              a §3.7 TimeSpec string
  dedupe        bool, true
  embed         bool, null             the §4.4 per-call override

POST /v1/engage
  delta_id      str, required          §4.6; prefix allowed as in get()
  kind          str, required
  by            str, null
  note          str, null
  tags          [str], null
  snapshot      bool, true
  dedupe        bool, true

POST /v1/recall                        the §4.5 keywords, one to one
  query         str, null
  source        str | [str], null
  tags          [str], null
  any_tags      [str], null
  exclude_tags  [str], null
  kind          str | [str], null
  since         str, null
  until         str, null
  limit         int, 20
  exclude_sources  [str], null
  include_expired  bool, false
  noise         bool, true
  recency       bool, true
  min_relevance float, 0.0
  plan          [object], null         §5.5 step dicts, JSON values only
  filters       object, null           §5.5 plan-level filters
  sediment      bool, null             the §4.5/§6.5 pass; plan only

POST /v1/plan
  steps         [object], required
  filters       object, null
  sediment      bool, null             the §4.5/§6.5 pass

POST /v1/context                       the §4.5 context keywords, one to one
  query         str, null
  budget        int, 8000
  limit         int, 30
  source        str | [str], null
  exclude_sources  [str], null
  tags          [str], null
  any_tags      [str], null
  exclude_tags  [str], null
  kind          str | [str], null
  since         str, null
  until         str, null
  crystal       bool, true
  containers    bool, true
  noise         bool, true
  recency       bool, true
  min_relevance float, 0.0
  labels        object, null

POST /v1/system-prompt                 the §5.7 keywords; opened readonly
  moods         int, 3
  budget        int, 8000
  labels        object, null

POST /v1/consolidate
  kind          str, required          'container' | 'mood' | 'crystal'
  window        str | [str, str], null a §3.7 Duration string or [a, b]
  opts          object, null           the §6 opt keys; unknown keys are the
                                       library's ValueError (400)

POST /v1/digest                        the §6.6 keywords
  since         str, null              'YYYY-MM-DD'
  max_units     int, null
  max_tokens    int, null
  backfill_max  int, null
  dry_run       bool, false            opened readonly; needs no server think

POST /v1/sweep                         {} (no fields)

POST /v1/embed-missing
  batch_size    int, 64
  limit         int, null
```

GET query parameters:

```
GET /v1/get/{id}          include_expired  bool, false
GET /v1/lineage/{id}      depth int, null (all the way) · include_expired bool, true
GET /v1/cited-by/{id}     depth int, 1    · include_expired bool, false
GET /v1/export            include_expired bool, false · vectors bool, false
GET /v1/import (POST)     include_expired bool, false   (query, not body)
```

Semantics are the library's, not restated: each endpoint calls its method
with the decoded arguments and serialises the §13.1 shape back. The
differences worth pinning:

- `GET /v1/get/{id}` answers `200` with the JSON value `null` when `get()`
  returns `None` — a missing row is not an HTTP 404, because `get()` does
  not raise (decision); `/v1/crystal` likewise. `lineage` and `cited-by`
  raise `NotFoundError` for an unknown id and that is a 404 (§13.4).
- `POST /v1/recall` returns the hits plus the call's `lake.last_warnings`
  as `warnings`, so the client can populate its own `last_warnings`;
  `plan` and `context` already carry warnings in their result objects.
- `POST /v1/consolidate` returns both the §4.7 return value (`delta`) and
  `lake.last_run` (`run`), so the client can populate `last_run`.
- `POST /v1/digest` with no server think and not `dry_run` answers the same
  `409` as `/v1/consolidate`; a live digest lease is the library's
  `ConsolidateError`, also `409`. The client waits up to its
  `consolidate_timeout` (1800 s); a digest that outlives it keeps running on
  the server, and a retry meets the lease. `lake serve --digest` is the path
  for long digests.
- `GET /v1/health` needs no auth and answers
  `{"ok": true, "rows": n}` with `n` = `SELECT count(*) FROM deltas`
  (decision: total rows, one cheap query on a readonly open).
- `POST /v1/embed-missing` with no server-side embed maps the library's
  `LakeError` through §13.4 (500).

### 13.4 Errors over the wire

Every error response body is the envelope

```
{"error": "<ExceptionClassName>", "message": "<str(exception)>"}
```

with `error` the concrete class name (`type(exc).__name__`). Server side,
the status is chosen by the first matching row (most specific first;
`ClosedLoopError` and `PlanError` are `ValueError` subclasses and
`NotFoundError` is a `LookupError`, §4.2):

| exception | status |
|---|---|
| `NotFoundError` | 404 |
| `ConsolidateError` | 409 |
| `ClosedLoopError`, `PlanError`, any other `ValueError` | 400 |
| everything else (`SchemaError`, `EmbedError`, `LakeError`, `sqlite3.OperationalError`, a bug) | 500 |

Client side, the class is rebuilt from the `error` name, not the status:
`SchemaError`, `ClosedLoopError`, `NotFoundError`, `PlanError`,
`ConsolidateError`, `EmbedError`, `LakeError`, and `ValueError` map to
themselves and are raised with the envelope's `message`; any other name
raises `LakeError` with message `{error}: {message}` (decision). A
non-2xx response whose body is not a parseable envelope raises `LakeError`
with message `HTTP {status}: {first 200 characters of the body}`
(decision). A re-raised `EmbedError` has `.delta` and `.stored` set to
`None`: the §4.4 row is committed server-side without its vector exactly as
locally, but the row cannot cross inside an exception (decision, said so a
host does not read `.delta` remotely).

Auth failures (401) and unknown endpoints (404) use the `LakeError`
envelope forms pinned in §13.2 and §13.3.

### 13.5 Streaming: export and import

`GET /v1/export` answers `200` with `Content-Type: application/x-ndjson`
and a body that is byte for byte what `export()` writes (§4.8: canonical
lines, ascending `(timestamp, seq)`, LF-terminated), streamed as it is
produced; the server may omit `Content-Length` and delimit by closing the
connection, and the client reads to EOF. `include_expired` and `vectors`
as query parameters. The client writes the stream to the given path and
returns the number of lines written, which equals `export()`'s return.

`POST /v1/import` takes the NDJSON as the request body (`Content-Length`
required, §13.2) with `include_expired` as a query parameter, feeds it
line by line through the §4.8 per-line path — own transaction per line,
id skip, watermark `seq` advance — and answers the counts (a line that
is not lake-format JSONL is an error line there too; §4.8).

### 13.6 Client — `lake.open` and `RemoteLake`

```python
def open(target: str | os.PathLike | None = None, *, token: str | None = None, think=None, embed=None,
         default: str | os.PathLike | None = None, readonly: bool = False, **kwargs: Any) -> Lake | RemoteLake
def resolve(target=None, *, token=None, default=None, environ=None, env_file=None) -> Target   # §13.8
def setting(name: str, *, environ=None, env_file=None) -> str | None                           # §13.8

class RemoteLake:
    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
        consolidate_timeout: float = 1800.0,
        spool_path: str | os.PathLike | None = None,   # None: ~/.lake/spool.jsonl
    ) -> None
```

**Factory rule.** `lake.open(target)`: a `str` target that starts with
`http://` or `https://` (exact lower-case prefix) returns
`RemoteLake(target, token=token, **kwargs)`; anything else returns
`Lake(target, think=..., embed=..., readonly=readonly, **kwargs)`. Both names
are exported from the package. With a target, `open` reads no environment.
**`lake.open()` with no target** is the one library entry point that does
(decision, simplify step A): the target and token come from
`resolve(default=default)` (§13.8; nothing named and no `default` raises
`LakeError("no lake: pass a target or set LAKE")`), and on a local file
`think`, `embed` and `automation`, when not passed, from `LAKE_THINK`,
`LAKE_EMBED` and the §13.8 automation rules. `think`/`embed` take a
callable, a §8 spec string (built with the §8 adapters; a `think` spec also
sets `model_name` unless it is passed), or `False` for none; `open()` never
picks a model on its own. On a remote target the environment's think and
embed are ignored (they are the server's, §13.2), an explicit one raises
`LakeError("think= is not available over HTTP")`, and `readonly` is
accepted and ignored (the server applies it per endpoint). A writable local
open creates the file's parent directory. `Lake(...)` and `RemoteLake(...)`
never read the environment.
`RemoteLake` given a url without that prefix raises `ValueError`
(`not an http(s) url: {url}`); a trailing `/` on the url is stripped.
`urllib.request` only; no redirects are ever issued by the server.

**Surface.** `RemoteLake` implements the same public methods returning the
same §4.1 dataclasses, each over its §13.3 endpoint: `write`, `get`,
`recall`, `plan`, `context`, `context_blocks`, `system_prompt`, `engage`,
`consolidate`, `digest`, `crystal`, `due`, `sweep`, `stats`, `export`, `import_`,
`embed_missing`, `lineage`, `cited_by`, plus `close()` (a no-op: no
connection is held), the context-manager protocol, and the attributes
`last_warnings` (set by `recall`/`plan`/`context`/`context_blocks` from
the response's warnings, §13.3, and by the spool, §13.7) and `last_run`
(set by `consolidate` from the response's `run`). Like a `Lake`, an instance is not
thread-safe: one per thread.

**Refusals.** The file-only surface raises `LakeError` whose message is
the name plus ` is not available over HTTP`:

```
media_path is not available over HTTP
meta_get is not available over HTTP
meta_set is not available over HTTP
```

and every `Lake` constructor keyword that `RemoteLake` does not define —
`readonly=`, `clock=`, `think=`, `embed=`, `model_name=`,
`collapse_sources=`, and the rest — is rejected the same way, message
`{name}= is not available over HTTP` (decision: one uniform rule, so
`lake.open(url, think=...)` fails with the informative class instead of a
`TypeError`; think and embed live on the server, §13.2).

**Timeouts and retries.** Every request uses `timeout` (default 30 s)
except `POST /v1/consolidate` and `POST /v1/digest`, which use
`consolidate_timeout` (default 1800 s), the flush health probe (5 s) and the flush import (120 s)
(decision: the probe must be cheap and a full spool is up to 10,000
lines). There are no automatic retries; the spool is the only retry
mechanism, and it covers `write()` alone.

**Connection errors.** A connection error is any failure to obtain an HTTP
response — refused, DNS, reset, timeout. A received HTTP error status is
not a connection error. On one:

- `recall` returns `[]`; `plan` returns
  `PlanResult(steps={}, warnings=[w], timing_ms=0.0, sediment=None)`;
  `context` returns
  `""`; `context_blocks` returns
  `ContextResult(None, [], [], [], 0, "", [w])`; `system_prompt` returns
  `""` — each setting
  `last_warnings = [w]` with `w` = `lake unreachable: {error}`. These
  fail soft because they sit on the hook path, which fails open, and they
  are the methods with a warnings channel (decision: the brief names
  "empty hits plus a warning"; this pins which methods that covers).
- `write()` spools (§13.7).
- Every other method fails loud: `LakeError` with the same
  `lake unreachable: {error}` message (the brief: deliberate actions
  deserve the error).

### 13.7 Spool — writes survive being offline

Only `write()` spools. The spool is `~/.lake/spool.jsonl` (or the
constructor's `spool_path`), created mode 0600, plus the side file
`spool.dropped` beside it (also 0600). The default path assumes the
one-shared-mind shape — one remote lake per machine; a process talking to
two different lakes gives each client its own `spool_path`, because a
flush sends the whole file to whichever server the client reaches
(decision, said because the failure is silent cross-writing).

**Validation first.** `RemoteLake.write()` performs the §4.4 step 1–3
validations that need no file — content, source (including the `lake:`
refusal), kind, the kind-with-empty-`derived_from` `ClosedLoopError`, the
`media` pattern, the `meta` finiteness, `expires` parsing and its
in-the-future check, `timestamp` parsing, tag and edge normalisation —
before any request, raising the same classes online or offline. The
`derived_from` existence check is the server's; for a spooled write it is
deferred to the flush, where `import_()` keeps a missing parent as a
dangling edge rather than raising (§4.8) — the offline client cannot know.

**What spools.** On a connection error, when `kind` is `None`: the client
generates the id (`uuid.uuid4().hex[:12]`) and the timestamp (the caller's
`timestamp` rendered, else now) locally, appends one §4.8 lake-format
canonical JSON line — the twelve export keys in the export order,
LF-terminated — and returns the `Delta`, appending
`lake unreachable: {error}; write spooled to {spool_path}` to
`last_warnings` so the CLI surfaces the fallback on stderr. The line differs from an online
write in exactly these ways: `meta` is the caller's object with
`"spooled": true` set (`{"spooled": true}` when the caller sent none);
`expires_at` is computed against the client's clock; no dedupe ran and
none will (the flush skips by id only), so a deduping daemon writes one
row per offline call; the id was not checked for collision (12 hex chars;
at flush a collision is an id skip). A connection-failed `write()` with a
non-`None` `kind` raises `LakeError` (`lake unreachable: ...`) and spools
nothing (decision: a container's level needs the parents' levels, which
the offline client cannot read, and a structural write is a deliberate act
— it fails loud like `engage`). A spooled row whose TTL expires before the
flush is skipped by `import_()`'s expiry rule, which is correct: it would
already be invisible.

**flock protocol.** Every touch of the spool takes `fcntl.flock` on the
spool file itself, opened `O_RDWR | O_CREAT | O_APPEND`, mode 0600:
`LOCK_EX` for append, cap enforcement, flush, and `spool.dropped` updates;
`LOCK_SH` for the read-only count (`lake spool`). The file's inode never
changes — it is only ever appended to, rewritten in place, or truncated to
zero under the lock — so a lock taken by path is always the lock. The
spool lives on a local filesystem (`flock` over NFS is outside v1 support,
as the lake file itself is, §9).

**Cap.** After appending its line (still under `LOCK_EX`), a writer counts
lines; above 10,000 it rewrites the file in place keeping the newest
10,000 and adds the number dropped to `spool.dropped`, which holds one
cumulative ASCII decimal and a trailing LF, read and rewritten under the
same lock (decision: the brief says count only; cumulative, so repeated
overflows are visible). Nothing else is written anywhere: offline loss is
by design bounded and recorded. The docs say so.

**Flush.** Before the first lake-server request of any public method call
(the health probe and the flush's own import excepted — a flush never
triggers a flush), the client stats the spool; when it exists with size
> 0:

1. `GET /v1/health` (no auth, 5 s). Any failure — connection error,
   non-200, `ok` not `true` — skips the flush silently and the caller's
   request proceeds under its own rules (offline is the normal case; no
   warning).
2. Take `LOCK_EX`. Re-check emptiness (another process may have flushed);
   empty → unlock, done.
3. `POST /v1/import` with the file's bytes as the body (auth, 120 s).
4. On 200: truncate the spool to length 0 and unlock. Rows land with
   `meta.spooled` intact, so offline provenance stays visible.
5. On any failure: unlock leaving the file untouched, append
   `spool flush failed: {error}` to this call's `last_warnings`
   (decision), and let the caller's request proceed.

The lock spans the POST, so a concurrent append waits (bounded by the
120 s import timeout) and is never truncated away: truncation only ever
removes bytes that were in the body just imported. **Idempotency:** every
line carries a client-generated id and `import_()` skips ids already in
the file (§4.8), so the crash window between step 3 and step 4 — imported
but not truncated — costs one re-send in which every line skips; a double
flush is harmless, and two flushing processes serialise on the lock. No
timer is needed: the next prompt in any session flushes.

### 13.8 The environment — `LAKE`, the env file, `lake.resolve()`

**One target key.** `LAKE` names the lake: a filesystem path (`~`
expanded) or a URL with the exact lower-case prefix `http://` or
`https://` (the §13.6 factory rule). `LAKE_URL` and `LAKE_FILE` are
deprecated aliases, still honoured.

**One resolver.** `lake.resolve()` (`lake/_env.py`, a leaf module with no
package import, so a host that cannot import the package can load it by
path) is the rule, and every host calls it: the CLI (every command, `serve`
included), the plugin hooks (through the CLI), the plugin MCP server,
`lake.open()` with no target, and the eval harness. Precedence, written
down once (decision, simplify step A):

1. **explicit argument**: `lake.open(target)`, `lake.resolve(target)`, the
   CLI's `--lake`/`--url`/`--file`;
2. **process environment**: `LAKE`, else `LAKE_URL`, else `LAKE_FILE`;
3. **env file** (`LAKE_ENV_FILE`, else `~/.lake/env`): `LAKE`, else
   `LAKE_URL`, else `LAKE_FILE`;
4. **the host's default** (`default=`; the plugin's is
   `~/.lake/claude.lake`), else `ValueError("no lake: pass a target or set
   LAKE")` (`LakeError` from `lake.open()`, exit 2 from the CLI).

The first source that names a lake wins whole: a lower source never adds a
key to a higher one, so an eval's process `LAKE_FILE` is never overridden by
a `LAKE_URL` in `~/.lake/env`. An empty value counts as unset.
**Token:** an explicit `token=`, else the process `LAKE_TOKEN`, else the
env file's `LAKE_TOKEN` only when the resolved target is a URL equal to the
env file's own target URL (trailing `/` ignored): a bearer read from the
file is never sent to a URL named somewhere else. A local target takes no
token. `resolve()` returns `Target(target, remote, token, source, notes)`:
`target` an expanded path or a URL without a trailing `/`, `source` one of
`argument`, `environment`, `env file`, `default`, and `notes` the
deprecation lines (`LAKE_URL is deprecated; use LAKE=<the same value>`)
that the host prints; the library prints nothing.

| process env | env file | result |
|---|---|---|
| – | `LAKE_URL=http://server:8377`, `LAKE_TOKEN=t` | `http://server:8377`, token `t`, a `LAKE_URL` note |
| – | `LAKE_FILE=/var/lib/lake/claude.lake` | that file, a `LAKE_FILE` note |
| `LAKE_FILE=/tmp/x.lake` | `LAKE_URL=http://server:8377`, `LAKE_TOKEN=t` | `/tmp/x.lake`, no token |
| `LAKE=/a.lake`, `LAKE_URL=http://x` | – | `/a.lake` |
| `LAKE_URL=http://x`, `LAKE_FILE=/y` | – | `http://x` |
| `LAKE=http://server:8377` | `LAKE=http://server:8377/`, `LAKE_TOKEN=t` | `http://server:8377`, token `t` |
| `LAKE=http://other` | `LAKE=http://server:8377`, `LAKE_TOKEN=t` | `http://other`, no token |
| – | – | `default=` if given, else the error |

**Other keys: one rule, `lake.config()`.** `config(think=None, embed=None,
automation=None, *, digest=False)` (`lake/_env.py`) is how every host turns
the environment into a configured local `Lake`: `lake.open()` with no target,
the CLI and `lake serve` all call it, and pass only what they override (their
flags). Each key is read from the process environment, else the env file; an
empty value is unset (except `LAKE_AUTOMATION`, below). It returns
`Config(think, embed, automation, notes)`:

- `think`: the given spec, else `LAKE_THINK`, else, where the host runs
  digestion (`digest=True`), `DEFAULT_THINK` (`claude:--model
  claude-opus-5-5[1m]`, `lake._env`; decision: pin the
  model that writes the self, so an account-level default cannot change it);
- `embed`: the given spec, else `LAKE_EMBED`;
- `automation`: the given rules, else `LAKE_AUTOMATION` (unset:
  `tag:automation`, the label `LAKE_TAGS=automation` gives a job's rows; set
  but empty: no rule), plus the deprecated `LAKE_COLLAPSE_SOURCES` and
  `LAKE_EXCLUDE_SOURCES` as `source:` rules, with a note each. Given rules
  replace the environment's wholly. `tag:automation` is also the `Lake`'s own
  default (§4.3), so the file behaves the same however it is opened.

Which host takes what (a remote target takes none of it: think, embed and
automation belong to the server):

| host | think | embed | automation |
|---|---|---|---|
| `lake.open()`, no target, local file | `config()` | `config()` | `config()` |
| `lake.open(path)`, `Lake(path)` | what is passed | what is passed | what is passed, else `tag:automation` |
| CLI `consolidate`, `digest` (not `--dry-run`) | `config(--think, digest=)`; `digest` gets `DEFAULT_THINK` | `config(--embed)` | `config(--automation)` |
| CLI, every other command | `--think` only | `--embed` only (`context` only with `--vector`) | `config(--automation)` |
| `lake serve` | `config(--think, digest=--digest on)` | `config(--embed)` | `config(--automation)`, once at startup |
| plugin MCP server | none (a local `deep_recall` stays model-free) | none | the `Lake` default |

`lake.setting(name)` reads one key by the same rule (`lake serve` uses it
for its own `LAKE_TOKEN`). Host-only keys: `LAKE_SOURCE` and `LAKE_TAGS`
(plugin hooks and MCP server, process environment only), `LAKE_BIN` (below).
User-facing, in short: **`LAKE`, `LAKE_TOKEN`, `LAKE_THINK`, `LAKE_EMBED`.**

**Deprecated, removed in 0.2.0** (each prints a one-line note until then):
`LAKE_FILE`, `LAKE_URL`, the CLI's `--file` and `--url` (use `LAKE` and
`--lake`); `LAKE_COLLAPSE_SOURCES`, `LAKE_EXCLUDE_SOURCES`, `--collapse-source`,
`--exclude-source` and `Lake(collapse_sources=)` (use `source:` automation
rules; `collapse_sources=` warns with `DeprecationWarning`); the timer
wrapper's `LAKE_MAX_UNITS`, `LAKE_CATCHUP_SINCE` and
`LAKE_CATCHUP_MAX_TOKENS` (use `lake digest` flags); and the
`lake.remote.read_env_file` re-export (use `lake._env`).

**The env file.** Format: UTF-8 text, one `KEY=VALUE` per line. A parser
must accept exactly this grammar (decision, pinned so the shell and Python
readers agree):

```
- blank lines, and lines whose first non-space character is '#', are ignored
- an optional leading 'export ' (one space) is stripped
- the key must match ^[A-Z_][A-Z0-9_]*$; the first '=' splits key from value
- one layer of matching surrounding quotes ('...' or "...") is removed from
  the value; no other escape or expansion processing happens
- a later line for the same key wins (source semantics)
- any line that fits none of the above is ignored (fail open: a config typo
  must not kill a hook)
```

Example:

```
LAKE=http://10.0.0.5:8377
LAKE_TOKEN="k3yb0ard-c4t"
# where the file is (a server or a local digest):
export LAKE_THINK='claude:--model claude-opus-5-5[1m]'
```

`lake._env.read_env_file(path=None)` is the parser; with no path it reads
`env_file_path()`, the same file every reader uses (`LAKE_ENV_FILE`, else
`~/.lake/env`; decision, simplify/core review: a default that ignored
`LAKE_ENV_FILE` let a test read the real file). `lake.remote.read_env_file`
re-exports it for older callers. The timer script
(`plugin/scripts/lake-consolidate.sh`) resolves nothing: it sets
`LAKE_ENV_FILE` to `$LAKE_HOME/env` when unset, finds `LAKE_BIN` with the
hooks' rule (`hook.py lake-bin`, §13.9) and runs `lake digest`, so the CLI
reads the file. It still maps the deprecated digestion knobs
`LAKE_MAX_UNITS`, `LAKE_CATCHUP_SINCE` and `LAKE_CATCHUP_MAX_TOKENS` (the
process environment, else the file sourced in a subshell) to `--max-units`,
`--since` and `--max-tokens`, with a note each; `LAKE_PYTHON` is no longer
read.

**What reads what.** `Lake`, `RemoteLake` and `lake.open(target)` read no
environment variable and no file beyond their arguments; `lake.open()` with
no target is the one place in the library that does. Everything else is the
hosts' business (§1).

### 13.9 CLI and plugin changes

The CLI resolves its lake with `lake.resolve(--lake or --url or --file,
default=--default)` (§8, §13.8) and prints the resolver's notes to stderr. A
remote target routes every §8 command through a `RemoteLake` built with the
resolved token, unchanged in flags and output, except:

- `serve` always hosts a local file (§13.2).
- `consolidate` or `digest` with `--think` or `--embed` on a remote target
  is a usage error, exit 2: `--think and --embed are server-side on a remote
  lake (lake serve --think ...); drop the flag or point LAKE at the file`.
  Without the flags, remote `consolidate` and `digest` work and the specs are
  the server's (`LAKE_THINK` in the client's environment is ignored).
- `crystal --write-file` on a remote target is a usage error, exit 2:
  `--write-file needs the file; not available over HTTP` (the crystal file
  lives beside the server's lake and the server's own `consolidate`
  maintains it).
- read-only opens (§8) do not apply remotely; the server applies them
  per endpoint (§13.2).

New command `spool` (remote target required; with a file target exit 2,
`lake spool needs a remote lake (set LAKE to its URL, or --lake URL)`):

| command | flags | does |
|---|---|---|
| `spool` | | prints `spool empty`, or `{n} spooled write{s}, oldest {timestamp[:16]}` — `n` the line count under `LOCK_SH`, the timestamp from the first line |
| `spool --flush` | | runs the §13.7 flush now; prints `flushed: written {w}, skipped {s}, errors {e}`; `spool empty` when there was nothing; a failed flush prints the error to stderr, exit 1 |

**The plugin hooks** are one Python file, `plugin/hooks/hook.py`, run by
the three `.sh` files (`session-start.sh`, `prompt.sh`, `stop.sh`) that
`hooks.json` and `codex/hooks.json` name. The hook never resolves the lake:
it runs the CLI with `--default ~/.lake/claude.lake` and an unmodified
environment, so the CLI resolves target and token exactly as a shell would.
The one key it needs first is `LAKE_BIN`, the CLI command (split with
`shlex`): the process `LAKE_BIN`, else the env file's `LAKE_BIN` only when
the process environment names no lake (a wrapper there may pin its own
lake), else `lake`. To read the env file without importing the package it
loads `lake/_env.py` from an importable lake, else by path from the checkout
the plugin lives in; a copied plugin with neither uses the process
`LAKE_BIN` or `lake` (so a copied plugin needs `lake` on `PATH` or
`LAKE_BIN` in the hook environment). With a URL target, every hook write
and recall rides `RemoteLake` through the CLI, the spool absorbs offline
prompts, and the next prompt flushes them. **The plugin MCP server**
resolves once at startup with `lake.resolve(default="~/.lake/claude.lake")`
(logging the notes), then opens `lake.open(target, token=...)` per call. A
local file gets no think, so `deep_recall` stays model-free there; the §6.5
sediment pass runs on a `lake serve` that has one (decision, simplify/core
review: the design's open question 2 is still open; model-free is the one-line
default until it is answered). An importable lake older than `lake.resolve` (a
split install) gets the resolver from the plugin's checkout by path, as the
hooks do, and a CLI older than `--default` is called again without it.

The SessionStart hook runs `lake system-prompt --budget 4000` (was `lake
context --budget 4000`; AAA phase 5 A2 raised it to 5000 without the sign-off
its design asked for, since it reached the then-default prose crystal, and the review put
it back: an items crystal at `RENDER_CAP` fits because `system_prompt()`
gives it its room first, §5.7); a shell hook can still only emit
`additionalContext`, so the literal system-role install is the Python-host
capability of §5.7, but the hook now carries the crystal AND recent-mood
`carrier_wave` — the mechanism that keeps §6.2's promise.

### 13.10 Test plan

The §10 conventions hold: one named test per pinned behaviour, temporary
files, fake think/embed. `serve` tests run a real `lake serve` subprocess
on a loopback port and wait for `/v1/health`.

| test | setup → assertion |
|---|---|
| `test_serve_wire_roundtrip` | a served lake; client `write()` with tags, `derived_from`, `meta`, an astral character, and a media hash → client `get()` returns a `Delta` equal field for field to a direct local `get()` on the same file; `engagement` crosses as `null` on plain rows and as the filled object on an engagement row. |
| `test_serve_recall_plan` | the §5.5 fixture served → client `recall(plan=...)` (worked example 1) returns the same ids and scores to 6 places as the local call; client `plan()` returns every step in plan order with buckets and timelines intact; a `CollapsedRun` entry crosses with a `count` key and no `delta` key. |
| `test_serve_context_blocks` | the §5.6.4 fixture served → client `context()` equals the local `context()` render on the same file, and `context_blocks()` reports the same hits, containers, strips, and `omitted_strips`; response `warnings` populate `last_warnings`. |
| `test_serve_system_prompt` | a served lake with a crystal and moods → `POST /v1/system-prompt` with `{moods, budget}` returns `{"prompt": str}` equal to a local `Lake.system_prompt()` on the same file; `RemoteLake.system_prompt(...)` matches. |
| `test_serve_engage_valence` | client `engage(id, "affirm")` → client `recall` shows `valence` 1.05 on the target; the §4.6 snapshot content crossed intact. |
| `test_serve_superseded_by_wire` / `test_remote_superseded_by` | a lake with a link served → `/get` of the old row carries a trailing `superseded_by` array after the export key order, the new row omits it; through `RemoteLake`, `get`/`recall`/`context` carry the receipt and the order; a wire object without the key decodes to `()`. |
| `test_serve_refuted_by_wire` | client `engage(id, "refute")` → `/get` and `/recall` on the target carry a trailing `refuted_by` array (each `{id, source, timestamp, note}`) after the export key order, `valence` 0.50; an unrefuted row omits the key. |
| `test_serve_lineage_cited_by` | crystal → container → rows, one swept → client `lineage()` returns `rows` and `dangling` equal to local; `cited_by()` likewise; an unknown id raises `NotFoundError` through the wire. |
| `test_serve_consolidate` | `lake serve --think cmd:...` (a fake recording its input server-side) → client `consolidate("container")` writes a `lake:container` row, returns it, `last_run` carries `written`, `skipped`, `think_calls`, and the `[a, b]` window; the fake ran on the server, not the client. |
| `test_serve_consolidate_no_think` | serve without think → client `consolidate("mood")` raises `ConsolidateError` carrying the §13.2 sentence; the raw response status is 409. |
| `test_serve_refused_cmd_claude_think_still_serves` / `test_serve_honours_lake_env_file` | (AAA phase 3 review) `--think "cmd:timeout 60 claude -p"` → the server starts, writes succeed, `/v1/consolidate` answers 409 with the no-think message, while an unknown spec still exits 2; with `LAKE_ENV_FILE` naming a scratch env file, serve takes `LAKE_FILE` from it and never reads `~/.lake/env` (its token is not required). |
| `test_serve_sediment` | `lake serve --think cmd:...` (a fake answering prose) → client `recall(plan=...)` over hits spanning two sources lands a `lake:sediment` row in the served file (the fake ran server-side) and the hits cross unchanged; client `plan()` carries the row as `PlanResult.sediment` through the wire; body `"sediment": false` writes none; the same server without think writes none and still answers 200; `sediment=True` without `plan` is a 400 `ValueError` through `/v1/recall`. |
| `test_serve_export_import` | client `export()` writes a file byte-identical to a local `export()` of the same lake (and with `vectors=True`); client `import_()` of lake-format NDJSON returns the §4.8 counts, a second import skips every line. |
| `test_serve_health_and_auth` | tokened server: `/v1/health` answers `{"ok": true, "rows": n}` with no auth; every other endpoint without or with a wrong token answers 401 `{"error": "LakeError", "message": "unauthorized"}` and the client raises `LakeError`; with the token, requests pass. |
| `test_serve_refuses_public_bind` | `lake serve --bind 127.0.0.2` with no token exits 1 with the §13.2 refusal line on stderr; the same bind with `LAKE_TOKEN` serves; `--bind 127.0.0.1` with no token serves. |
| `test_serve_token_file` | with `HOME` redirected, the token comes from `~/.lake/token`; a 0644 token file → exit 1 with the chmod line; 0600 → serves. |
| `test_serve_error_mapping` | through real requests: `limit=0` → 400 `ValueError`; a bad plan → 400 `PlanError`; `write(source="lake:x")` → 400 `ClosedLoopError`; `engage` on a missing id → 404 `NotFoundError`; a held lease → 409 `ConsolidateError`; a broken server-side embed on `write` → 500 `EmbedError` with `.delta is None` client-side — each re-raised as the named class with the server's message; an unknown path → 404 `LakeError` `unknown endpoint: ...`; a non-envelope error body → `LakeError` naming the HTTP status. |
| `test_serve_get_null` | `GET /v1/get/{missing}` answers 200 `null` and the client returns `None`; `include_expired=1` returns an expired row, absent does not; `/v1/crystal` on a crystal-less lake answers `null`. |
| `test_remote_factory` | `lake.open("http://...")` → `RemoteLake`; `lake.open(tmp_path)` → `Lake`; `RemoteLake("ftp://x")` raises `ValueError`; `lake.open(url, think=fake)` raises `LakeError` `think= is not available over HTTP`; `media_path`, `meta_get`, `meta_set` raise their §13.6 messages. |
| `test_remote_fail_soft` | against a dead port: `recall()` → `[]` with `last_warnings == ["lake unreachable: ..."]`; `context()` → `""`; `context_blocks()` → the empty result carrying the warning; `system_prompt()` → `""` carrying the warning; `plan()` → the empty `PlanResult`; `get`, `stats`, `engage`, `consolidate`, `sweep` raise `LakeError("lake unreachable: ...")`. |
| `test_spool_offline_write` | against a dead port: `write()` returns a `Delta` with a fresh 12-hex id and `meta.spooled == true`; the spool (mode 0600) holds one canonical §4.8 line a local `import_()` accepts; `write(kind="sediment", derived_from=[...])` offline raises `LakeError` and spools nothing; a bad offline write (`source="lake:x"`) raises `ClosedLoopError` and spools nothing. |
| `test_spool_flush` | three offline writes, start the server, client `recall()` → the rows are in the lake with `meta.spooled` and the spool is truncated to 0; the flush precedes the recall's own request. |
| `test_spool_double_flush` | copy the spool aside, flush, restore the copy, flush again → every line skips by id and the lake holds each row once (the §13.7 idempotency argument, exercised). |
| `test_spool_flock` | two processes each append 100 offline writes concurrently → 200 intact lines, every one parseable; an append issued while a flush holds `LOCK_EX` completes afterwards and survives the truncation. |
| `test_spool_cap` | 10,003 spooled lines → the file holds the newest 10,000 and `spool.dropped` reads `3`; a later overflow adds to the count (cumulative). |
| `test_cli_remote` | with `LAKE_URL` and `LAKE_TOKEN` in the environment, `lake write`, `recall`, `context`, and `stats` drive the server and print the §8 output shapes; `lake consolidate container --think ollama:x@y` exits 2 with the §13.9 message; `lake crystal --write-file` exits 2; `lake serve` under `LAKE_URL` still serves the local file. |
| `test_cli_spool` | `lake spool` prints `spool empty`; after two offline writes, `2 spooled writes, oldest {ts[:16]}`; `lake spool --flush` against a live server prints `flushed: written 2, skipped 0, errors 0`; against a dead one exits 1; with a file target exits 2. |
| `test_env_file` | a `~/.lake/env` with comments, `export` prefixes, quoted values, a malformed line, and a duplicate key parses per §13.8; `LAKE_URL` in it routes the CLI to a server; the same name in the process environment wins over the file; `--url` wins over both. |
| `test_plugin_env_file` | the hook scripts and the MCP server resolve `LAKE_FILE`, `LAKE_BIN`, `LAKE_URL`, `LAKE_TOKEN` through `~/.lake/env` with the process environment winning; the existing plugin tests pass unchanged with `LAKE_FILE` alone. |
| `test_env_file_token_only_goes_to_its_own_url`, `test_mcp_server_env_file_token_only_for_its_own_url` | the CLI and the MCP server send the env file's `LAKE_TOKEN` to the env file's `LAKE_URL` only; a process `LAKE_URL` naming another server gets no bearer (`unauthorized`). |
| `test_env_file_lake_bin_ignored_when_process_names_target` | with `LAKE_FILE` in the process environment, no hook runs the env file's `LAKE_BIN`. |
| `test_consolidate_runs_lake_digest`, `test_consolidate_maps_the_deprecated_knobs`, `test_consolidate_lake_bin_follows_the_one_rule` (simplify step B) | the timer script runs one `lake digest --default $LAKE_HOME/claude.lake` with `LAKE_HOOKS_OFF=1` and `LAKE_ENV_FILE` defaulting to `$LAKE_HOME/env`, resolves nothing but `LAKE_BIN` (through `hook.py lake-bin`: the env file's only when the process names no lake), leaves a process `LAKE` untouched for the CLI, and maps `LAKE_MAX_UNITS`/`LAKE_CATCHUP_SINCE`/`LAKE_CATCHUP_MAX_TOKENS` to flags with a deprecation line each. |
| `tests/test_digest.py` (simplify step B) | §6.6: nothing due makes no call; the order container → catch-up days → mood → crystal and every old session containered once; a rerun is idempotent; a catch-up day that raises is retried by the next digest and given up after three; a day of skipped clusters is done after one pass; `max_units` and `max_tokens` stop and the next digest resumes; the lease refuses an overlap; a changed `since` restarts with a warning; `dry_run` leaves the file byte-identical and deletes its copy; `/v1/digest` through `RemoteLake`; `lake digest` lines, `--json`, exit 1 on a failed step, exit 2 for `--think` on a remote lake; the `lake-catchup.py` wrapper. |
| `test_serve_digest_endpoint`, `test_serve_digest_schedule_parse_and_wait`, `test_serve_digest_loop_shares_the_server_think`, `test_serve_digest_flag_starts_the_thread` | `/v1/digest` answers 409 without a server think (not for `dry_run`), 400 for an unknown field, a `DigestRun` with one, 409 on a live digest lease; the four `--digest` forms and their refusals; the wait until the next slot, 0 after a missed one (daily, local time) or a full interval; the loop, driven by a fake clock and a fake wait with no sleeping, digests at once, with the server's own think, logs one line per run and never retries a failed digest in a tight loop; `lake serve --digest every:1h` announces the schedule and digests at startup. |
| `tests/test_env.py` (simplify step A) | the §13.8 precedence table row by row (notes, the token rule, empty values, the default and the no-lake error); `setting` and the automation rules (default, empty, the deprecated source keys); the parser (moved from `test_serve`); `lake.open()` with a target (no environment read, parent created) and without (environment, default, remote think refusal, readonly ignored); a local and a served round trip through `lake.open()`; the `collapse_sources` alias; `LAKE_FILE`/`LAKE_URL` compatibility with a note; eval isolation against a hostile process `LAKE`; and a grep test that no host keeps its own resolver copy. |
| `test_lake_not_importable_loads_the_checkouts_resolver`, `test_copied_plugin_without_lake_fails_open` | a hook whose `python3` cannot import lake loads `lake/_env.py` by path and honours the env file's `LAKE_BIN`; a copied plugin with no lake never runs the env file's `LAKE_BIN` and stays silent. |
| simplify review (2026-09-28) | `test_suite_ignores_a_hostile_shell_think_and_embed` (the autouse fixture masks every `LAKE_*` key: two tests under a hostile shell `LAKE_THINK`/`LAKE_EMBED` make no call); `test_config_is_the_one_host_rule`, `test_the_automation_default_holds_however_the_file_is_opened`; `test_older_cli_without_default_still_gets_the_write`; `test_serve_digest_loop_survives_a_failed_check_and_a_held_lease`, `test_a_held_lease_is_not_an_attempt_at_a_day`; `test_crystal_prompt_golden` (the edits golden, replacing the prose one), `test_removed_crystal_options_are_refused`, `test_operator_guard_refuses_a_name_no_row_names`, `test_claude_adapter_carries_the_operator_names`. |
