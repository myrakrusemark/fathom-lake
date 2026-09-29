# Build an agent with memory

A short guide to using lake inside your own program: a chat agent, a reading
companion, a home assistant, anything that should remember and become someone
over time. `SPEC.md` is the reference behind every sentence here; `README.md` is
the quickstart.

## 1. What a lake is

- **One file.** A lake is one SQLite file (`agent.lake`). Copy it, back it up,
  open it with `sqlite3`. Nothing else is needed to read it.
- **Write everything.** Nothing decides what to remember at write time. Every
  row carries who wrote it (`source`), when, its tags, and what it was derived
  from.
- **Provenance.** The rows the lake writes about itself (containers, moods,
  the crystal, sediment) always cite the rows they were made from, and
  `lineage(id)` walks back to what a host wrote (SPEC §1).
- **You bring the model.** The library holds no API key and runs no model of
  its own. You pass a `think` callback (or a spec string) and, optionally, an
  `embed` callback. Without them it still stores, searches and renders.

### Kinds of rows, and how they link

Hosts write plain rows (prompts, replies, notes, pipe messages). They carry no
`derived_from`. Four kinds are written from other rows and always cite them:

| kind | written by | derived from | answers |
|---|---|---|---|
| `container` | consolidation (`digest()`) | a session or episode's rows; L2, L3… cite other containers | what happened |
| `mood` | consolidation | the last few hours' rows | how things have been |
| `crystal` | consolidation | the previous crystal plus containers, moods and stances | who I am |
| `sediment` | `deep_recall` / plans | the rows a deep search surfaced | what I concluded |

Three more kinds of link sit beside `derived_from`:

- **Engagements:** `engage(target, "affirm" | "refute" | "reply")`. A refute marks
  its target (and anything resting on it) as corrected in recall.
- **Supersession:** "B replaces A", proposed by the container pass, checked by
  code, and shown in recall as `⟵ superseded by …`. The older row is demoted,
  never deleted.
- **Stances:** positions with a confidence level and the rows for and against
  them, strengthened or weakened by engagements and supersession. The crystal
  is built from them.

`lineage(id)` follows `derived_from` back to the plain rows a host wrote;
`cited_by(id)` goes the other way.

## 2. Install

```
pip install fathom-lake              # stdlib only; needs SQLite with FTS5
pip install "fathom-lake[numpy]"     # faster vector recall above ~10,000 rows
pip install "fathom-lake[mcp]"       # the MCP server the Claude Code plugin uses
```

## 3. Open it

```python
import lake

mem = lake.open("agent.lake")                           # a local file
mem = lake.open("https://host:8377", token="...")     # a lake served elsewhere
mem = lake.open()                                     # whatever the environment names (§8)
```

All three return an object with the same methods (`Lake` or `RemoteLake`,
SPEC §13.6). `lake.open(target)` reads no environment at all; `lake.open()` with
no target is the one call that does. An object is for one thread; open one per
thread.

## 4. The loop

The whole integration is four calls:

```python
sid = "2026-09-28-a"                                  # your session id

system = mem.system_prompt()                          # session start: who I am, how I feel (crystal, moods)

def turn(user_text: str) -> str:
    memory = mem.context(user_text, exclude_tags=[f"session:{sid}"])   # what I remember about this
    mem.write(user_text, "my-agent", tags=[f"session:{sid}", "user"])
    reply = my_model(system + "\n\n" + memory, user_text)              # your model call
    mem.write(reply, "my-agent", tags=[f"session:{sid}", "assistant"])
    return reply
```

- `system_prompt()` renders the crystal and the newest moods (SPEC §5.7).
- `context(query)` recalls by full text, recency, engagement and (with an
  `embed`) vectors, and renders it as prompt text (SPEC §5.6). Excluding the
  current session keeps what the model already sees out of it.
- `write()` stores a row. Tag turns `session:<id>` and `user` / `assistant`:
  digestion reads a closed session as one unit and writes its container from
  it (SPEC §6.1).
- `engage(id, "refute", by="robin", note="...")` when the user corrects a
  memory, `"affirm"` when they confirm one. It changes ranking at once and is
  evidence for the next crystal (SPEC §4.6).

## 5. Give it a model, and digest

```python
mem = lake.open("agent.lake", think="claude:--model claude-opus-5-5[1m]", embed=my_embed)
run = mem.digest()        # whatever is due, in order; a second call with nothing due costs nothing
```

`think` is a callable `think(prompt, *, system=None, json=False)` returning
text (or a dict when `json=True`), or a spec string: `claude`, `claude:<args>`
(runs `claude -p` on this machine), `ollama:<model>@<url>`, or
`cmd:<shell command>` (SPEC §8).

`digest()` with no `think` raises `lake.ConsolidateError`, as `consolidate()`
does; a served lake whose server has no model answers the same way. A program
that may run without a model catches it:

```python
try:
    mem.digest()
except lake.ConsolidateError:
    pass          # no model here; the rows wait, and nothing is lost
```

`digest()` (SPEC §6.6) runs, each only when due: session containers (and a
bounded backfill of old sessions), an optional catch-up from a date
(`since=`), the mood, and the crystal. It is idempotent and resumable, and
its state lives in the lake. The lake never schedules itself; pick one:

- your program's own loop or scheduler calls `mem.digest()`;
- a timer runs `lake digest` (the plugin's `lake-consolidate.sh` does);
- `lake serve --digest nightly` digests the file it serves.

**Individuality.** The crystal is the lake's self: first-person items in three
sections (`core`: what I hold to, `tension`: where I'm pulled two ways,
`open`: what I haven't settled), each citing the rows it rests on. Each new
crystal is a set of cited edits to the last one, so it changes by explicit,
sourced steps and keeps what nothing contradicted; `lake crystal --log` shows
the growth log. The same pass keeps **stances**, positions on recurring
questions whose confidence is read from their evidence (SPEC §6.3, §6.3.1).
Two lakes fed different lives answer differently; that is the point. A name
the model knows only from its account context (the `claude` adapter's
operator) is refused unless a row names it: the account is not memory.

## 6. One lake from many machines

Run the file where the model is, and serve it:

```
LAKE_TOKEN=... lake serve --lake ~/.lake/agent.lake --bind 10.0.0.5 --port 8377 --think claude --digest nightly
```

A server on anything but loopback refuses to start without a token:
`LAKE_TOKEN`, or `~/.lake/token` (mode 0600). Everywhere else, `LAKE=http://10.0.0.5:8377` and `LAKE_TOKEN=...` (or
`lake.open(url, token=...)`). The API is the same. Consolidation and the
sediment pass run on the server with the server's `think`; a client never
passes a model. Writes made while the server is unreachable go to a local
spool (`~/.lake/spool.jsonl`) and are replayed on the next call (SPEC §13.7).

## 7. Automated callers

Rows written by bots, watchers, batch jobs or the model talking to itself
should carry the `automation` tag (or `LAKE_TAGS=automation` for the plugin's
hooks). They stay searchable, and digestion never turns them into containers,
moods or the crystal, however the file is opened (`tag:automation` is the
`Lake`'s own default). A host can change the rule with `automation=` rules
(`tag:`, `source:`, `prefix:`; `[]` turns it off; `LAKE_AUTOMATION` for
`lake.open()`, `lake digest` and `lake serve`; SPEC §4.3).

## 8. Four variables

| variable | meaning |
|---|---|
| `LAKE` | the lake: a file path or an `http(s)://` URL |
| `LAKE_TOKEN` | the bearer for a URL (read from `~/.lake/env`, it is sent only to that file's own URL) |
| `LAKE_THINK` | the model where the file is (digest, `lake serve`); `lake digest` and `lake serve --digest` default to `claude:--model claude-opus-5-5[1m]` when it is unset |
| `LAKE_EMBED` | embeddings where the file is; unset means full-text search only |

Each is read from the process environment, then `~/.lake/env`
(`KEY=value` lines). Precedence is written once, in SPEC §13.8: an explicit
argument, then the process environment, then the env file, then the host's
default. `LAKE_FILE` and `LAKE_URL` still work and print a one-line
deprecation note.

## 9. What the lake never does

- It never schedules: no thread or timer lives in the `Lake` object.
- It never deletes what you wrote, except rows you gave an `expires`.
- It never edits a row it wrote; a newer row supersedes and cites it.
- It never writes a claim about itself without citing the rows it rests on.
- The `Lake` object never runs a model you did not give it. Two hosts that
  digest pick one when none is configured: `lake digest` and
  `lake serve --digest` use `claude:--model claude-opus-5-5[1m]` (through
  `claude -p`) unless `LAKE_THINK` or `--think` names another.

## 10. Where to read next

- `SPEC.md` §4: the API, signature by signature.
- §5: recall, ranking, and how `context()` renders.
- §6: consolidation, the crystal and stances, and §6.6 `digest()`.
- §13: `lake serve`, the client, the spool, and the environment.
- `examples/agent_with_memory.py`: this guide as a 30-line program; the same file runs on a local lake or a
  served one, only `LAKE` changes (`examples/fake_think.py` stands in for a model).
- `plugin/README.md`: the Claude Code and Codex plugin, a complete host.
