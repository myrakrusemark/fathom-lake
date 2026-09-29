# lake internals

The contract between the modules of the `lake` package. SPEC.md is the
behaviour; this file is who does what, who may call whom, and what the `Lake`
instance hands each module. §12 of the spec overrides its body; this file
overrides nothing in the spec.

## 1. Modules

| module | owns | lines (original max; now, at AAA phase 5 B) | tests it owns |
|---|---|---|---|
| `_types.py` | every public dataclass, error, alias, `DEFAULT_NOISE_PHRASES`, `DEFAULT_LABELS`, `DEFAULT_DUE_THRESHOLDS` | types + time + db + facade ≤ 850; now 1162 | `tests/test_core.py`: `test_open_existing`, `test_schema_version`, `test_wal_two_processes`, `test_code_limits` |
| `_time.py` | §3.7 parse/render of TimeSpec and Duration | (same pool) | |
| `_db.py` | §3.1–3.3, §3.6, §4.3 open/create, `tx`, meta, ids, batch loaders | (same pool) | |
| `lake.py` | the `Lake` facade: exact §4 signatures, delegation only | (same pool) | |
| `_store.py` | §4.4 write, §4.5 get/lineage/cited_by/host meta, §4.6 engage, `insert_row`, `embed_and_store` | 450; now 345 | `tests/test_store.py`: closed loop, derived_from, dedupe (all), ttl visibility/refresh, engage (all), lineage, meta host keys, get prefix, export canonical's NaN half |
| `_recall.py` | §5.1 filters, §5.2 candidates, §5.3 score, §5.4 noise, `recall`, `oneline`, §6.3.1 stance confidence (`stances`, `evidence_sessions`) | recall + vectors ≤ 550; now 682 | `tests/test_recall.py`: valence, fts, noise, no-query order, no embed, embed failure, vector relevance, vector blob layout |
| `_vectors.py` | §3.4 pack/unpack/normalise, `store_vectors`, `VectorCache` | (same pool) | |
| `_plan.py` | §5.5 validation and every step, §5.6.1 `timeline`, `neighbors`, `aggregate` | 650; now 489 | `tests/test_plan.py`: every `test_plan_*` |
| `_context.py` | §5.6 `context_blocks`, block 4, strip selection, §5.6.2 renderers, §5.6.3 budget | 650; now 296 | `tests/test_context.py` + `tests/fixtures/context.jsonl` + `tests/golden/context.txt`: every `test_context_*` |
| `_consolidate.py` | §6 consolidate/crystal/due, lease, watermark, session groups, clustering, supersession, the items crystal (edits, rendering), stance ops (`stance_ops`), the operator guard (`leaks`), prompts | 650; now 1409 (+ `_answer.py` 85) | `tests/test_consolidate.py`: every `test_consolidate_*`, `test_session_*`, `test_due`; `tests/test_digestion.py`: automation rows, the material gate, idempotent windows; `tests/test_crystal.py`: the crystal, its golden prompt and the operator guard; `tests/test_stances.py`: stances |
| `_digest.py` | §6.6 `digest()`: the four steps in order, the catch-up state (meta `digest`, `digest_last`), the caps and the prompt counter, the digest lease, `dry_run` on a backup copy reopened with the Lake's own options (`Lake.init`), `summary` (the one-line report); a held lease (`LeaseHeld`) is not an attempt at a day | new at simplify step B: 164 | `tests/test_digest.py` |
| `_answer.py` | §4.9 cleaning (`parse_answer`, public), the §6.1 citation policy (`check_citations`) | (same pool) | `tests/test_consolidate.py`: `test_parse_answer_table`, `test_container_citations_*` |
| `prompts/*.txt` | the §6 system prompts and directives, loaded with `importlib.resources` | not counted | |
| `_io.py` | §4.8 sweep/stats/export/import_/embed_missing/media_path | 550; now 341 | `tests/test_io.py`: sweep, import lake format, export canonical, sqlite cli reads file, `test_scale` (slow) |
| `cli.py`, `adapters.py` | §8; `host_config` (the flags over `config()`), `old_flags` (the `--file`/`--url` notes); the `claude` adapter's `operator` names (`operator_names`) | 600; now 697 | `tests/test_cli.py` |
| `_env.py` | §13.8: the env-file grammar (`read_env_file`), the one target resolver (`resolve` → `Target`), per-key `setting`, the one host rule `config` → `Config` (think, embed, automation, notes), `DEFAULT_THINK` and `DEFAULT_AUTOMATION`. A leaf: stdlib only and no package import, so `plugin/hooks/hook.py` and the MCP server can load it by path | new at simplify step A: 138 | `tests/test_env.py` |
| `__init__.py` | the exports and `lake.open()`: with a target, the §13.6 factory; with none, the one library entry point that reads the environment (through `_env`) | 58 | `tests/test_env.py` |
| `tests/conftest.py` | `make_lake`, `clock` (`FrozenClock`), `fake_think`, `fake_embed`, `raising_embed`, `write_rows`, `hash_embed` | | |

The whole package, `cli.py` and the §13 remote layer (`remote.py` 713, `serve.py` 502) included, stays
under the line budget `tests/test_limits.py` holds (7,100 at the simplify review, at 7,091 lines; the test's
docstring keeps the history of every change). The per-module maxima above are the original targets and are no longer enforced; only the package
total is. Every module starts with `from __future__ import
annotations`, targets Python 3.11 (no `type X = ...`, no PEP 695), has a type
hint on every function, and binds nothing mutable at module level: only
`str`/`int`/`float`/`bool`/`tuple`/`frozenset`/`re.Pattern`/`MappingProxyType`
constants named `UPPER_CASE`, plus type aliases. Long text (prompts) lives in
`prompts/*.txt`, never in a `.py`.

## 2. Import graph (allowed edges)

Base modules anyone may import: `_types`, `_time`, `_db`, and `_vectors` (a
leaf: pure math and blob I/O over `_db`).

```
lake.py (facade)  → every module
_store            → _db, _time, _types, _vectors
_recall           → _vectors, _db, _time, _types
_plan             → _recall, _vectors, _db, _time, _types
_context          → _recall, _plan, _store, _db, _time, _types
_consolidate      → _answer, _recall, _store, _db, _time, _types, _vectors (cosine), prompts/*.txt
_digest           → _consolidate (CUT, via lake.consolidate), _db, _time, _types
_answer           → nothing in the package (a leaf; a host may import it too)
_io               → _store, _db, _vectors, _time, _types, _recall (is_noise, Filters — nothing else)
cli               → lake (the public API, lake.open), adapters, _env, _digest (summary), _recall.oneline
serve             → _env, cli (host_config, old_flags, warn), adapters, _digest (state, summary), _io, _time, lake.py
__init__ (open)   → _env, adapters, lake.py, remote
_env              → nothing in the package (a leaf; plugin/hooks/hook.py loads it by path)
remote            → _io, _store, _time, _types, _env (only to re-export read_env_file)
adapters          → _types only
```

Nothing imports `cli` except `serve`; `adapters` only the hosts and `lake.open()`. No module imports `lake.py` at runtime:
type hints use `if TYPE_CHECKING: from .lake import Lake`. No module keeps
state between calls; per-instance state lives on the `Lake` (`last_warnings`,
`last_run`, `vectors`).

## 3. What the `Lake` instance exposes

Set by the constructor (§4.3) and read by the modules:

| attribute | type | meaning |
|---|---|---|
| `conn` | `sqlite3.Connection` | autocommit mode, `row_factory = sqlite3.Row`, pragmas applied |
| `path` | `Path` | the file as given |
| `stem` | `str` | `path.stem`; `<dir>/<stem>.lake` |
| `media_dir` | `Path` | `<dir>/<stem>.media` (§3.5) |
| `crystal_path` | `Path` | `<dir>/<stem>.crystal.md` (§6.3) |
| `think`, `embed` | callables or `None` | §4.9 |
| `model_name` | `str \| None` | `meta.model` on consolidated rows |
| `half_life_s` | `float` | §5.3, seconds, > 0 |
| `automation_sources` | `tuple[str, ...]` | the `source:` values of `automation`: §5.6.1 T5 collapse, the §6 160-character row lines (`collapse_sources=` is the deprecated alias that appends such rules) |
| `automation` | `tuple[tuple[str, str], ...]` | §4.3, §6: `(kind, value)` per rule, kind `tag`, `source` or `prefix` (validated in the constructor); `automation=None` is `DEFAULT_AUTOMATION`, `tag:automation` |
| `init` | `dict[str, Any]` | the constructor's options (automation resolved, `collapse_sources` folded in): the dry run reopens its copy with them, overriding think, embed, the crystal file and readonly |
| `noise` | `NoiseRules` | §5.4 |
| `dedupe_window_s` | `float \| None` | §4.4 step 5 |
| `embed_on_write`, `write_crystal_file`, `readonly` | `bool` | |
| `labels` | `dict[str, str]` | `DEFAULT_LABELS` merged with the host's; `context(labels=)` merges per call on top |
| `due_thresholds` | `dict[str, object]` | `DEFAULT_DUE_THRESHOLDS` merged with the host's (§6.4 keys; `due("crystal")` fires first when a memory in the crystal's `derived_from` ancestry was refuted or superseded after it was written (§6.4 correction propagation), then applies the material gate: `crystal_min_age` old and `crystal_containers` library containers since it, or the age+count trigger; the bootstrap is always age+count. The drift gate and its keys are gone since simplify step A) |
| `clock` | `Callable[[], datetime]` | every `now` |
| `last_warnings` | `list[str]` | assigned (a fresh list) by `_recall.recall`, `_plan.run`, `_context.context_blocks` |
| `last_run` | `ConsolidateRun \| None` | assigned by `_consolidate.consolidate` |
| `vectors` | property → `VectorCache \| None` | lazily created; `None` when `embed` is `None` |

Helpers: `now()` (aware UTC datetime), `now_str()` (stored format), `tx()`
(the `_db.tx` context manager on `conn`), `require_writable()` (raises
`LakeError` when `readonly`), `call_embed(texts)` and `call_think(prompt, *,
system, json)` (the only entry points to the two callbacks: both raise
`LakeError` when `conn.in_transaction`, `call_embed` raises `LakeError`
without an `embed`, `call_think` raises `ConsolidateError` without a
`think`; exceptions from the callbacks themselves pass through unchanged),
`close()`, context-manager protocol. No module touches `lake.embed` or
`lake.think` directly except to test for `None`.

The facade validates only what belongs to no module: the `host:` prefix of
`meta_get`/`meta_set` (`ValueError`), and the "no other argument with
`plan=`" rule of `recall()` (`ValueError`). Everything else (empty source,
bad kind, `limit < 1`, unknown opts, ...) is validated in the module that
owns the call.

## 4. Module functions

Signatures are in the stub files; this lists responsibility. `lake` is
always the `Lake` instance.

### `_time` (done)

`to_utc(dt)`, `dt_to_ts(dt)`, `ts_to_dt(ts)`, `now_str(clock)`,
`parse_timespec(spec, now)`, `render_timespec(spec, now)`,
`is_relative(spec)` (for §12.8), `parse_duration(d) -> seconds`,
`render_duration(seconds)` (largest exact unit),
`parse_expires(value, now)` (Duration added to now, TimeSpec as is),
`age_seconds(now, ts)`. Raise plain `ValueError` on bad input. Never bind a
raw input string against a stored timestamp: render first.

### `_db` (done)

`open_db(path, *, readonly, created_at)`, `create_schema`, `check_schema`,
`tx(conn)`, `meta_get(conn, key)`, `meta_set(conn, key, value|None)` (any
key), `bump_vectors_gen(conn)` (the §3.4 statement), `max_seq(conn)`,
`new_id(conn)`, `qmarks(n)`, `chunks(ids)`, `load_sides(conn, ids)`,
`row_to_delta(row, tags, derived_from, engagement)`, `rows_to_deltas(conn,
rows)`, `load_map(conn, ids)`. `DELTA_COLS` is the
column list every `SELECT ... FROM deltas d` should use so `rows_to_deltas`
can attach the side tables. Loaders never filter on expiry; the caller does.

Supersession (§5.3, AAA phase 3): `supersessions(conn, now_ts) -> {old id:
[Link]}` (one lookup of the `has_supersedes` meta key, `HAS_LINKS`, while
the lake has never held a link; `_store.insert_row` sets it with the first
`lake:container` row whose meta has `supersedes`. Else the live
link-carrying `lake:container` rows, their refutes and `derived_from` (a link
counts only when `new` is in it), the referenced rows and the new rows'
refutes; `Link(old, new_ts, receipt: Supersession)`),
`dep_corrected_set(conn, now_ts, links)` (`dep_refuted_set` plus a forward
walk over `derived_from` from the superseded rows that never enters an
asserting container). `rows_to_deltas(..., now_ts, links=None)` attaches
`superseded_by` beside the refute receipts; `score()` passes the map it
already loaded. Stance links (§5.3, §6.3.1) come from `stance_chains(conn,
now_ts, gate=None)` (`{slug: [(id, ts, position, meta.stance)]}` newest first
over live `lake:stance` rows; one lookup of `has_stances`, `HAS_STANCES`, set
by `_store.insert_row` with the first `lake:stance` row): every older row of a
slug gets a `Link` to every newer one, with `by` = the newer row.
`supersessions` reads both gates in one meta query. `Link.at` is when the link
was asserted (the container's timestamp, or the newer stance's);
`rests_on_newer_link(conn, crystal_id, crystal_ts, links)` is the §6.4
supersession trigger.

### `_store`

- `write(lake, content, source, *, tags, kind, derived_from, expires, media,
  meta, timestamp, dedupe, embed) -> Delta`: §4.4 steps 1–8. Steps 3–6 in
  one `lake.tx()`; then `embed_and_store` when enabled. Calls
  `lake.require_writable()` first.
- `get(lake, delta_id, *, include_expired) -> Delta | None`: §4.5; the
  `^[0-9a-f]{8,12}$` gate; prefix `LIKE` only when the length is < 12.
- `resolve(lake, delta_id, *, include_expired) -> Delta`: `get` or
  `NotFoundError` (used by engage, lineage, cited_by, `inputs=`).
- `newest(lake, kind) -> Delta | None`: the newest live row of a kind by
  `ORDER BY timestamp DESC, seq DESC LIMIT 1`. `_consolidate.crystal`,
  `_context` block 1, and `_io.stats` (`crystal_id`) all read the crystal
  through it, so `_context` never imports `_consolidate`.
- `engage(lake, ...) -> Delta`: §4.6 steps 1–6, with `insert_row` plus the
  `engagements` insert in one transaction.
- `lineage`, `cited_by`: §4.5 breadth-first walks; `cited_by(depth < 1)` is
  `ValueError`; unknown id `NotFoundError` (§12.9).
- `meta_get(lake, key)`, `meta_set(lake, key, value)`: one transaction each,
  `require_writable` on set.
- `normalise_tags(seq)`, `tag_key(tags)`: §4.4 step 2 (also for
  `derived_from` and for import).
- `insert_row(conn, *, delta_id, timestamp, content, source, kind, level,
  tags, derived_from, expires_at, media_hash, meta)`: the raw insert, no
  checks, inside the caller's transaction. `_consolidate` and `_io` use it.
- `embed_and_store(lake, delta)`: §4.4 step 7. `lake.embed([content])` with
  no transaction open; then one `lake.tx()` around
  `_vectors.store_vectors`; every failure is `EmbedError(delta=delta)`.

### `_vectors`

`pack`, `unpack`, `normalise` (None on zero norm), `cosine(a, b)`,
`store_vectors(conn, [(id, vec), ...]) -> (stored, failed)` inside the
caller's transaction (embed_dim check/set, normalise, insert, one
`bump_vectors_gen` when anything was inserted). `VectorCache.matrix(lake)`
reloads when `meta.vectors_gen` moved; `cosine_top(lake, q, ids, k)` dots
only the given ids (the filtered rows) and keeps `cos > 0`;
`centroid(lake, ids)` for bridge/chain. `numpy` is imported inside these
functions only, with a pure-Python fallback.

### `_recall`

`Filters` (§5.1 plus `has_media`, `exclude_kinds`, and `no_ttl`), `Cand`,
`filter_sql(lake, filters, now)`, `fts_tokens`, `fts_match`, `STOPLIST`,
`fts_pool(lake, match, where, params)` (the §5.2 step 1 SQL and nothing
else; `test_plan_search_limit` monkeypatches it to stipulate `rel_fts`),
`is_noise(rules, content, source, kind, media_hash)` (hard rules),
`candidates(lake, query, filters, *, now, noise, min_relevance, limit,
warnings)`, `valences(lake, ids, now)`, `score(lake, cands, *, now, limit,
recency, noise, query_present, step)`, `search(...)` = candidates + score,
`recall(lake, ...)` = validate + `Filters` + `search` + `lake.last_warnings`,
and `oneline(text, cap)` (§5.6.2, shared with `_consolidate` and the CLI).
`score` loads `_db.supersessions` once per call: `SUPERSEDE_DROP` (×0.50) on
every superseded candidate, the old → newest-candidate-superseder entries
added to the engagement cap's `targets`, and `_db.dep_corrected_set` in place
of `dep_refuted_set` for the boost suspension.
Every `now` is the aware datetime of one `lake.now()` reading per public
call (§6 of this file). An `embed` exception inside `candidates` is caught:
the warning string `embed failed: {exc}; FTS only` is appended to `warnings`
and the FTS set is used alone (§5.2); a `LakeError` from `call_embed` (the
transaction guard) is re-raised, not swallowed.

`_consolidate` and `due()` read their candidates as `Filters(kind=("plain",
"engagement", "sediment") or exclude_kinds=(...), no_ttl=True,
exclude_sources=<run's> + automation sources (collapse_sources is one of them now),
exclude_tags=<run's> + automation tags, since/until=<window>,
<the run's §5.1 opts>)` through `filter_sql`, append their own watermark
condition to the fragment, and apply `is_noise` per row (§5.4 hard rules;
`candidates()` with `query=None` applies none, by spec). `Run.__init__`
computes the automation part once per run (`automation(lake, session_prefix)`:
the `source:` rules; the `tag:` rules plus the session tags of rows that start
with a `prefix:` rule and carry no `tag:` tag, one `substr(content, 1, n) = ?`
scan) and keeps it out of `run.scope`, so it never reaches `meta.filters`;
`Run.rows` also drops a sessionless row that starts with a prefix. The mood
pressure and the age+count count read through the same filters;
`scoped_containers` drops a container most of whose parents are automation
(`automated`, one `sum(<filter>)` query per container, only when rules exist).

### `_plan`

`TimelineParams`, `NeighborParams`, `validate(steps)` (§5.5 messages
verbatim, §12.6 index ids), `run(lake, steps, filters) -> PlanResult` (sets
`lake.last_warnings`), `recall_hits(lake, steps, filters)` (last step's
hits; `PlanError` for a trailing aggregate; a trailing timeline flattens),
`timeline(lake, seeds, params, filters, *, now) -> list[Timeline]` (T1–T7,
every strip, `t_start` order, no cut; a seed's `seq` is fetched by id since
`Delta` has none), `flatten(lake, strips, *, now)`, `neighbors(lake, seeds,
params, filters, *, limit, now)`, `aggregate(hits, group_by)`. `search`
and `filter` steps call `_recall.search`; `bridge`/`chain` build `Cand`
lists and call `_recall.score`; the vector forms go through `lake.vectors`
and `lake.call_embed` (exceptions propagate, §4.2). `run` reads the clock
once and passes that `now` to every step.

### `_context`

`context_blocks(lake, query, *, ...)` runs §5.6 steps 1–4 (the facade's
`context()` is its `.rendered`): block 1 through `_store.newest(lake,
"crystal")`, anchors through `_recall.search` with
`Filters(exclude_kinds=("mood", "crystal"))` unless `kind` is given, block 4
through `active_containers` (`_store.cited_by(depth=3)`), strips through
`_plan.timeline(seeds, TimelineParams(20, 6, 15, 300), filters-without-
kind/since/until, now=now)`, the max(12, budget·12//8000) by score
(`max(hit.score for the strip's anchor_ids)`), then `strip_parts`/`render_line` under the §5.6.3 budget
(ambient lines go, lowest strip first, before any strip is dropped).
Labels: `{**lake.labels, **(labels or {})}`. One `now` for the whole call.

### `_consolidate`

`consolidate(lake, kind, window, **opts)` (dispatch, opts validation, lease,
`lake.last_run`), `crystal(lake)` (= `_store.newest(lake, "crystal")`),
`due(lake, kind)`, `prompt(name)` (`prompts/<name>.txt` via
`importlib.resources.files("lake") / "prompts"`; the directory needs no
`__init__.py`), `clean_answer(answer, *, json)` (§4.9), `row_line(lake,
delta, cap)` (the role from `lake.labels` `user_role`/`assistant_role`, by
the row's `user`/`assistant` tag), `container_line(delta)`. The model runs only through
`lake.call_think`. Rows are written with `_db.new_id` + `_store.insert_row`
inside one `lake.tx()` together with the meta keys and the lease refresh;
the vector follows through `_store.embed_and_store` (an `EmbedError` there
propagates after the row is committed, as in `write()`). Candidate filters
come from `_recall.Filters`/`filter_sql`; hard noise from
`_recall.is_noise`. `meta.filters` is written as canonical JSON with keys
sorted (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`, lists
sorted by code point, `null` when no filter opt was given), and "whose
`meta.filters` equals this run's" compares that canonical string, treating
a missing key as `null`.

Supersession detection (§6.1) runs inside `session_part` after a model-named
answer: `flagged` (the `changes` citation policy), `change_candidates`
(`_recall.search` with `Filters(kind="plain", until=row − 1 ms)`, top 2, plus
up to 2 same-part rows sharing FTS tokens), `supersede` (one `attempt()` with
`prompts/supersede*.txt`; its system is never the `system` opt) and
`pair_reason` (the per-pair code checks, quote check included). The pairs go
into the part's `meta.supersedes` in the same `Run.commit`.

The edits crystal (§6.3) applies ops in `apply_edits`: an unmentioned item is kept, `retire` drops only a core
item, `resolve` moves an open or tension item to core (`resolved_from`), and a retire or resolve naming the wrong
section is a warning that keeps the item (not counted toward the majority-invalid retry). `render_crystal` cuts
over `RENDER_CAP` (3000) in the order: change entries, trailing open items, trailing tension items. The edits
user prompt gets `{max_edits}` and `{render_cap}` filled in `crystal_run`. `items_answer`'s check rejects an answer
whose new or revised texts name a `think.operator` name the prompt (minus the prior crystal) never names
(`leaks`); a retry is the ordinary schema-restating one. (The structured mode's `new_items` and the Witness's
`witness()` left at simplify step B, the prose mode's `prose_answer` and `crystal_system` at the simplify review,
with their prompts; `tests/golden/crystal_edits_prompt.txt` pins the prompts now.)

### `_digest`

`digest()` validates `since`, sends `dry_run` to `dry()`, and otherwise runs `run()`: take `consolidate_lease:digest`
(one hour, refreshed before every step), load or reset the `digest` meta value, wrap `lake.think` in a `Counter`
(calls and prompt characters; restored in `finally`), then walk one plan list, `container` → one `container` per
catch-up day → `mood` → `crystal`, through the public `lake.due()` and `lake.consolidate()` (the per-kind leases and
due gates stay the §6 ones). A container call gets the units left as `max_clusters`; `_consolidate.CUT`, the warning
`container()` adds when that cap cut its units, tells `run()` the day is not done. `finally` writes `digest_last`
and deletes the lease. `dry()` backs the file up beside itself with `sqlite3.Connection.backup`, opens the copy as
`type(lake)(…)` with the same automation, noise, thresholds and clock and a think answering skip, and calls
`run(late=False)`, which reports mood and crystal instead of running them. `lake serve`'s thread reads
`state(lake, "digest_last")`; the CLI and the thread print `summary()`.

### `_io`

`sweep`, `stats`, `export`, `export_line(delta, vector)`, `import_`,
`import_lines(lake, lines, *, include_expired)` (one transaction per line,
`_store.insert_row`, watermark `seq` advance; a line that is not lake-format JSONL is an error line, §4.8), `embed_missing`
(`_vectors.store_vectors`, `embed_failed`), `media_path`.

## 5. Transaction rule (§4.3, §4.4, §6, §9)

- `conn` is in autocommit mode. Every multi-statement write runs inside
  `with lake.tx():` (`BEGIN IMMEDIATE` ... `COMMIT`, `ROLLBACK` on any
  exception, a failed `COMMIT` included). `tx()` raises `LakeError` when a
  transaction is already open, so nesting fails at once; helpers that must
  run inside a transaction (`insert_row`, `store_vectors`, `meta_set`,
  `bump_vectors_gen`) take the connection and never open one themselves.
- **No transaction is ever open across a `think` or `embed` call**, and
  the facade enforces it: `lake.call_embed` / `lake.call_think` raise
  `LakeError` when `conn.in_transaction`. Write the row, commit, call the
  model, then open a new short transaction for what the model produced.
  `write()` step 7, `engage()` step 5, `embed_missing()`, and every
  `consolidate()` unit follow this; `recall()` calls `embed` with nothing
  open at all.
- Every transaction that inserts or deletes `vectors` rows calls
  `bump_vectors_gen` once, inside that transaction (§3.4).
- Every method that writes calls `lake.require_writable()` before its first
  transaction (`sweep`, `consolidate`, `embed_missing`, `import_*`,
  `meta_set` included), so a readonly Lake fails before any model call or
  file work. `lake.tx()` calls it again itself: a `mode=ro` connection
  accepts `BEGIN IMMEDIATE` and only fails at the first INSERT, so the
  facade, not the implementer's memory, guarantees the §4.3 `LakeError`.
- `sqlite3.OperationalError` propagates unwrapped (§12.9).

## 6. Conventions

- One clock reading per public call: `now = lake.now()` at the top of the
  facade-level operation, passed down as an aware `datetime` (`now`
  parameters throughout `_recall`, `_plan`, `_context`, `_consolidate`);
  a relative `TimeSpec`, the expiry bound, recency, and a run's window all
  derive from that one value. Render with `_time.dt_to_ts(now)` where a
  string is needed.
- Timestamps: parse every `TimeSpec` with `_time.parse_timespec(spec, now)`
  and render with `dt_to_ts` before binding; compare stored strings as
  strings.
- Expiry: "live" is `(d.expires_at IS NULL OR d.expires_at > :now)` with
  `:now = dt_to_ts(now)`.
- IN lists: `_db.qmarks`, values bound, at most `_db.CHUNK` per query.
- Deltas: select `_db.DELTA_COLS` and hand rows to `_db.rows_to_deltas`;
  never one side-table query per row.
- Errors: library conditions raise the §4.2 class; argument problems raise
  plain `ValueError`; messages that the spec quotes are reproduced verbatim.
- Warnings are plain strings; the spec's wording is exact where it gives
  one.
- Docstrings: one or two lines naming the spec section. No comment bloat.

## 7. Contract review

Adversarial pass over the contract before the six implementers fanned out.
Verified without change: every public method of §4.3–§4.8 is on `Lake` with
the spec's signature and defaults (`write` positional-or-keyword after
`source`, keyword-only elsewhere as written); every §4.1 field and default
and every §4.2 error with its bases (`EmbedError.delta`/`.stored`
included); `__init__` exports the §4 list; `DEFAULT_NOISE_PHRASES` has the
55 strings with `sure go` and `sure, go` both present; `_time` accepts the
§3.7 grammar exactly (offsets converted, not relabelled; `±HHMM` and
`±HH:MM`; 1–6 fraction digits truncated to ms; the relative form
case-insensitive) and rejects week dates, ordinal dates, `T14` without
minutes, seven-digit fractions, `+5:00`, and free text with `ValueError`;
`render_duration` gives `3h`, `90m`, `60h` for 2.5 d; `_db` follows the
§4.3 open rules (create in one `BEGIN IMMEDIATE`, reopen without DDL,
`schema_version` `2` / missing key / missing `meta` / dropped `deltas_fts`
each `SchemaError` with the spec's wording, `DELETE`-mode file switched to
WAL once, readonly never switches and `require_writable` raises
`LakeError`); every stub's import list stays inside the §2 graph.

Fixed in place:

1. `_db.open_db`: a file that is not SQLite raised `sqlite3.DatabaseError`
   (`file is not a database`); now `SchemaError("not a lake: ...")` per
   §4.2, and the connection is closed on every failed open.
2. `_db.tx`: a failed `COMMIT` left the transaction open; now rolled back.
   Nesting raised SQLite's `OperationalError`; now `LakeError` before the
   `BEGIN`, so a callback-inside-transaction mistake fails at once.
3. `Lake.call_embed` / `Lake.call_think` added: the only entry points to
   the callbacks, each refusing to run while `conn.in_transaction`. This
   turns §9's "a model call never holds the lock" from a convention into a
   check. `_recall` re-raises the guard's `LakeError` instead of turning it
   into an `embed failed` warning.
4. `_context` had no allowed path to the crystal (it may not import
   `_consolidate`): `_store.newest(lake, kind)` added; `_consolidate.crystal`,
   context block 1, and `stats().crystal_id` share it. The duplicate
   `_context.context()` stub is gone; the facade calls
   `context_blocks(...).rendered`.
5. One clock reading per call: `filter_sql`, `candidates`, `valences`,
   `score`, `search`, `timeline`, `flatten`, and `neighbors` take `now` as
   an aware `datetime` (previously `filter_sql`/`valences` took a string and
   `score` would have read the clock again, so a relative `since` and the
   recency factor could disagree by a tick).
6. `_recall.fts_pool` split out as the §5.2 step-1 SQL so
   `test_plan_search_limit` has a seam to stipulate `rel_fts` by
   monkeypatch instead of reaching into `candidates`.
7. `Filters.no_ttl` (`expires_at IS NULL`) added so `_consolidate` and
   `due()` express every §6.1/§6.2/§12.3 candidate condition except the
   watermark through `filter_sql`; `exclude_kinds` documented as letting a
   NULL kind through. `candidates(limit=)` added for the fixed-order
   no-query path (§12.5) so filter-only recall on a 50,000-row lake cuts in
   SQL.
8. `_plan.timeline` docstring: a seed's `seq` is not on `Hit`/`Delta`
   (§4.1 is fixed); T1 fetches it by id.
9. `meta.filters` equality (§6) pinned to canonical sorted-key JSON so the
   three kinds and the TS port agree on "the prior row of this scope".
10. Base-pool budget raised 800 → 850 (types + time + db + facade were at
    792 before the guards); the module sum stays under the 5,000 cap.
11. `Lake.tx()` calls `require_writable()`: a `mode=ro` connection accepts
    `BEGIN IMMEDIATE` (verified by hand) and would fail only at the first
    INSERT with a raw `OperationalError`, so readonly enforcement no longer
    depends on each module remembering the call.

Known and accepted: `render_duration(0)` returns `"0w"` (the spec's
largest-exact-unit rule applied literally; no code path stores a zero
window). `PRAGMA journal_mode = WAL` runs after `create_schema`'s commit,
not inside it, because SQLite refuses the pragma inside a transaction.
