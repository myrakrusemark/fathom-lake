# lake plugin for Claude Code

This plugin gives every Claude Code session a memory backed by the lake
library. Each prompt and each finished turn becomes a row in one SQLite file
per user. Each prompt also gets relevant memory injected before Claude sees
it, and every session starts with the identity crystal. In the background,
`lake digest` consolidates the lake (containers, moods, the crystal) with
whatever model you name, from a systemd user timer or from `lake serve
--digest` on the machine that holds the file. The default is `claude -p`, on
your own subscription.

## Requirements

The `lake` CLI, which everything here shells out to:

```bash
pip install fathom-lake
```

Add the `[mcp]` extra if you want the MCP tools:

```bash
pip install 'fathom-lake[mcp]'
```

## Install for development

```bash
claude --plugin-dir /path/to/lake/plugin
```

Reload after edits with `/reload-plugins`. `claude plugin validate plugin/`
must pass before shipping a change.

## Install for real

Copy the `plugin/` directory to `~/.claude/skills/lake/`; skills-dir plugins
load automatically. A marketplace listing comes later.

## Set up the timer

Run `/lake:setup` inside Claude Code and it walks through the checks. Or by
hand:

```bash
bash plugin/scripts/install-timer.sh
systemctl --user status lake-consolidate.timer
```

## Configuration

All optional.

| variable | default | what it does |
|---|---|---|
| `LAKE` | `~/.lake/claude.lake` | the lake: a file path, or the URL of a remote lake server (`lake serve` on another machine, `http://` or `https://`). With a URL, hooks and MCP tools go over HTTP. Keep a file out of live-syncing folders; Dropbox mangles the `-wal` sidecar. `LAKE_FILE` and `LAKE_URL` are the old names: they still work and print a one-line deprecation note (the hooks discard it). |
| `LAKE_TOKEN` | unset | the bearer token a remote server expects. |
| `LAKE_BIN` | `lake` on PATH | the CLI the hooks and timer run. May be multi-word, such as `python3 -m lake.cli`. A copy of `plugin/` outside the lake checkout, with lake installed only in a venv, needs `lake` on `PATH` or `LAKE_BIN` in the hook's environment (the hook reads the env file through the checkout's `lake/_env.py`). A `LAKE_BIN` older than the plugin (no `--default`) still gets the writes: the hook repeats the call without the flag. |
| `LAKE_SOURCE` | `claude-code` | the source name hook and MCP `write` rows are stored under. Set per host (e.g. `codex`); process environment only, not read from `~/.lake/env`. |
| `LAKE_HOOKS_OFF` | unset | set to `1` and every hook exits 0 immediately. The CLI's `claude` think-adapter sets it during consolidation so that traffic is never re-ingested. |
| `LAKE_TAGS` | unset | extra tags (comma-separated) on every row the prompt and stop hooks write; process environment only. An automated caller that runs `claude -p` with the plugin loaded sets `LAKE_TAGS=automation` (and its own `LAKE_SOURCE`), so its rows stay searchable but are never consolidated (`LAKE_AUTOMATION` below). |

The hooks, the MCP server, the CLI, `lake.open()` and the timer all read
`~/.lake/env` (one `KEY=VALUE` line per variable; SPEC §13.8), so one file
repoints a whole machine, and they all resolve the lake with the same
function, `lake.resolve()`: the process environment wins over the file, and
the first source that names a lake wins whole. When the process environment
names one, the file's lake and `LAKE_BIN` are not used, and the file's
`LAKE_TOKEN` is only ever sent to the file's own URL. `install-timer.sh`
writes the file once, with comments. Keys for the model live there too:

| variable | default | what it does |
|---|---|---|
| `LAKE_THINK` | `claude:--model claude-opus-5-5[1m]` | the model consolidation runs on: `claude` (the account's default model), `claude:<args>`, `ollama:<model>@<url>`, or `cmd:<shell command>` |
| `LAKE_EMBED` | unset | embeddings for consolidated rows; unset means FTS-only recall |
| `LAKE_AUTOMATION` | `tag:automation` | comma-separated rules naming automation rows, kept out of every consolidation pass and still searchable (SPEC §4.3): `tag:<tag>`, `source:<source>`, or `prefix:<text>` for unlabelled rows (and their whole session) that start with the text. A `source:` rule also collapses that source's runs in timelines. Quote the value in the env file when a prefix has spaces or `#`: `LAKE_AUTOMATION='tag:automation,prefix:# My job'`. Set but empty means no rule. The CLI, `lake serve` (once, at startup) and `lake.open()` read it. `LAKE_COLLAPSE_SOURCES` and `LAKE_EXCLUDE_SOURCES` are deprecated names for `source:` rules (an excluded source is now searchable; pass `exclude_sources` per call to hide it). |

`LAKE_MAX_UNITS`, `LAKE_CATCHUP_SINCE` and `LAKE_CATCHUP_MAX_TOKENS` are the
old timer knobs: the timer script still maps them onto `lake digest
--max-units`, `--since` and `--max-tokens`, with a deprecation line.
`LAKE_PYTHON` is no longer read.

## Remote lake

Point `LAKE` at the URL of a machine running `lake serve` (plus
`LAKE_TOKEN`), and the plugin needs nothing else. (`LAKE_URL`, the old name,
still works.) Hooks write and recall over HTTP; the MCP
tools ride the same server. While the server is unreachable, recalls come
back empty and hooks stay silent, and each prompt is spooled to
`~/.lake/spool.jsonl` instead of lost. The next prompt after the server
returns flushes the spool. The MCP server serves its tools whether or not the
lake is reachable at session start; a tool called while the server is down
returns a clear, retry-safe message and starts working again on its own once
the server is back.

## Codex

The hooks and MCP server are host-agnostic; Codex speaks the same hook
protocol. Point Codex at this checkout, with `LAKE_SOURCE=codex` so its rows
are distinguishable. Both hosts share one lake and recall each other's rows.

`~/.codex/config.toml`:

```toml
[mcp_servers.lake]
command = "/usr/bin/python3"
args = ["/path/to/lake/plugin/mcp/server.py"]
default_tools_approval_mode = "approve"

[mcp_servers.lake.env]
LAKE_SOURCE = "codex"
```

`~/.codex/hooks.json`: the same three events as `hooks/hooks.json`, each
command prefixed with `env LAKE_SOURCE=codex` and given an absolute path
(Codex has no `${CLAUDE_PLUGIN_ROOT}`). Codex skips new or changed hooks until
you trust them: run `/hooks` in the Codex TUI and trust all three.

## What each hook does

- **SessionStart**: injects the identity crystal as additional context, and
  again after every compaction.
- **UserPromptSubmit**: writes the prompt into the lake (skipping `/commands`),
  then injects the memory relevant to it.
- **Stop**: writes the assistant's finished turn into the lake.

All three are `hooks/hook.py` (one Python file), run by the `.sh` file of
the event's name. The hook never resolves the lake itself: it runs the CLI
with `--default ~/.lake/claude.lake`, and the CLI resolves with
`lake.resolve()` like every other host. Every hook fails open. Any error
exits 0 with no output, so a broken memory never blocks a session.

## Digestion

`lake digest` (SPEC §6.6) does whatever consolidation is due, in order: the
session containers (and a few old sessions with no container), the catch-up,
the mood, the crystal. Each step acts only when due, so a run with nothing
due writes no rows and calls no model. There is no cap unless you pass
`--max-units` or `--max-tokens`: every model call stays bounded by its
prompt budget, and a digest takes all the work that is due. On a remote lake
it runs on the server, with the server's model.

History that fell behind is the **catch-up**: `lake digest --since
2026-09-07` walks every UTC day since then, one day per step. The date and
the progress are kept in the lake, so every later digest continues where the
last one stopped (a day whose model call fails is retried by the next digest
and given up after three). `lake digest --since D --dry-run` digests a copy
beside the lake with no model call and reports the calls and estimated input
tokens per day; check the free disk first.

Two ways to run it:

- **The timer.** `lake-consolidate.timer` fires 10 minutes after boot and
  every 6 hours after that, with `Persistent=true` so a missed window catches
  up at the next boot. It runs `plugin/scripts/lake-consolidate.sh`, which
  exports `LAKE_HOOKS_OFF=1` and runs `lake digest`; the CLI reads the lake
  and the model from the environment and `~/.lake/env`.
- **`lake serve --digest nightly`** (or `HH:MM`, or `every:6h`) on the
  machine that holds the file: one service serves the lake and digests it
  with one model config; a missed slot runs at startup. Then no timer is
  needed. A laptop that only talks to that server needs neither.

`lake stats` shows `digest_last` (the last digest's calls, tokens, errors)
and `digest` (the catch-up's `since`, `through` and any days given up).

Rows that have no vector (a write whose embed timed out) are embedded with
`lake embed-missing --embed <spec>`. Run it against a copy first (`sqlite3
claude.lake ".backup copy.lake"`).

## MCP tools

The `lake-memory` server exposes eight tools for deliberate use, on top of
what the hooks do passively:

| tool | what it does |
|---|---|
| `remember` | search the lake; scored hits, one line each |
| `deep_recall` | run a recall plan (SPEC §5.5): search, filter, set operations, bridge, chain, aggregate, neighbors, timeline. Returns the last step's hits, and prints the sediment row first when the deep pass wrote one (on a served lake with a server-side think; a local file stays model-free). The tool description teaches the calling model to author plans. |
| `context` | render the full memory context block for a query |
| `write` | write one deliberate note; returns the row's id. Optional `kind` (`container`, `crystal`, `mood`, or `sediment`) with `derived_from`, the ids the row was made from |
| `engage` | affirm, refute, or reply to a row |
| `lineage` | walk a row's provenance, one line per ancestor |
| `crystal` | the current identity crystal text |
| `stats` | lake counts and coverage, as JSON |

## Revert

Remove the plugin directory and run:

```bash
bash plugin/scripts/uninstall-timer.sh
```

That disables the timer and deletes the two systemd units. The lake file
stays; delete `~/.lake` yourself if you want the memory gone.

## Codex package

The same shared implementations have a verified Codex package and normal hook
trust flow; see [Codex installation and coverage](codex/README.md). Prompt/final
capture is not a complete transcript archive.
