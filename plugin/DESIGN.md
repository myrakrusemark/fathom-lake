# lake plugin for Claude Code — design

Decisions fixed 2026-09-04. The plugin is a HOST of the lake library
(SPEC.md): it owns capture, cadence, and the model; the library owns the
file. Nothing here changes `lake/`.

## What it does

Install it and every Claude Code session remembers. Passively: each prompt
and each finished turn becomes a row in one SQLite lake per user. Actively:
each prompt gets relevant memory injected, and every session starts with
the identity crystal. Deliberately: MCP tools for recall / write / engage /
lineage. In the background: `lake digest` (from a systemd user timer, or
`lake serve --digest` where the file lives) consolidates (containers, the
catch-up, moods, crystal) with whatever model the user names — `claude -p`
on their own subscription by default.

## Layout (`plugin/` in this repo; plugin name `lake`)

```
plugin/
├── .claude-plugin/plugin.json     name lake, description, version 0.1.0
├── hooks/
│   ├── hooks.json                 the three events below
│   ├── session-start.sh           SessionStart → crystal as additionalContext
│   ├── prompt.sh                  UserPromptSubmit → write prompt + recall
│   └── stop.sh                    Stop → write last_assistant_message
├── .mcp.json                      lake-memory → python3 ${CLAUDE_PLUGIN_ROOT}/mcp/server.py
├── mcp/server.py                  stdio MCP server over the lake file
├── skills/setup/SKILL.md          /lake:setup — install timer + env, guided
├── scripts/
│   ├── install-timer.sh           writes ~/.lake/env + systemd user units, enables timer
│   ├── lake-consolidate.sh        what the timer runs: `lake digest` (maps the old knobs)
│   ├── lake-catchup.py            the old catch-up tool's flags onto `lake digest --since`
│   └── uninstall-timer.sh
└── README.md                      install, config, revert
```

## Configuration (env, all optional)

- `LAKE` — the lake: a file (default `~/.lake/claude.lake`, never a syncing
  folder; SPEC §9) or a `lake serve` URL, with `LAKE_TOKEN`. With a URL, hook
  writes and recalls ride `RemoteLake` through the CLI, the MCP server opens
  `lake.open(url, token=token)`, offline prompts spool to
  `~/.lake/spool.jsonl`, and the timer's `lake digest` goes to `/v1/digest`
  (the model specs are the server's). `LAKE_FILE` / `LAKE_URL` are deprecated
  aliases (SPEC §13.8). `lake.open` creates `~/.lake` on first write.
- `LAKE_BIN` — the `lake` CLI. Default `lake` on PATH. Everything shells
  out to the CLI; the plugin never imports the package except in
  `mcp/server.py` (which needs `fathom-lake[mcp]` importable by `python3`)
  and `hooks/hook.py`, which loads only the leaf `lake/_env.py` (from an
  installed lake, else by path from the checkout) to read `LAKE_BIN`.
- `LAKE_HOOKS_OFF=1` — every hook exits 0 immediately (SPEC §8: the CLI's
  `claude` think-adapter and `lake-consolidate.sh` set it so consolidation
  traffic is not re-ingested).
- `~/.lake/env` — the shared config file (SPEC §13.8; `LAKE_ENV_FILE`
  overrides the path for every reader, the timer included). Every reader
  resolves the lake with `lake.resolve()` (the hooks through the CLI they
  run, with `--default ~/.lake/claude.lake`; the MCP server once at
  startup); the timer resolves only `LAKE_BIN` (`hooks/hook.py lake-bin`)
  and lets `lake digest` read the rest. When
  the process environment names a lake, nothing target-bound (the lake,
  `LAKE_BIN`) comes from the file, and the file's `LAKE_TOKEN` is sent only
  to the file's own URL. So a process-env lake is never overridden by the
  file's, and a scratch URL never receives the file's bearer.
  Timer keys: `LAKE_THINK` (default `claude:--model claude-opus-5-5[1m]`),
  `LAKE_EMBED` (default unset → FTS-only), `LAKE_AUTOMATION` (default
  `tag:automation`).

## Hooks (hooks/hooks.json)

All three: `type: command`, guard `[ -n "$LAKE_HOOKS_OFF" ] && exit 0`,
fail-open (any error → exit 0, empty output; a broken memory must never
block a session), commands referenced as
`${CLAUDE_PLUGIN_ROOT}/hooks/<script>`. Each `.sh` runs `hooks/hook.py
<event>`; every CLI call below also carries `--default ~/.lake/claude.lake`.

1. **SessionStart** (matcher `startup|resume|clear|compact`, timeout 8):
   `lake system-prompt --budget 4000` → the crystal, positions and mood
   blocks (SPEC §5.7; or nothing on a fresh lake) emitted as
   `{"hookSpecificOutput": {"hookEventName": "SessionStart",
   "additionalContext": ...}}`. This is how the crystal survives
   compaction: the `compact` matcher re-injects it.
2. **UserPromptSubmit** (timeout 8): read stdin JSON (`prompt`,
   `session_id`). Skip the whole hook when the prompt starts with `/` (a
   slash command is a CLI command, not speech: it is neither written nor
   used as a recall query). Otherwise (a) `lake write - --source
   claude-code --tag user --tag session:<id>` with the prompt on stdin;
   (b) `lake context --no-crystal --exclude-tag session:<id> --budget 24000
   -- <prompt>` → additionalContext. The exclude keeps the session's own
   rows out (they are already in the model's context); the crystal is off
   because SessionStart placed it.
3. **Stop** (timeout 8, async not needed — one insert): read stdin JSON
   (`last_assistant_message`, `session_id`); when non-empty,
   `lake write - --source claude-code --tag assistant --tag session:<id>`
   with the text on stdin. Dedupe in the library absorbs repeats.

Not captured in v0.1: tool results (noise; the assistant's turn is the
synthesis), subagent traffic (`stop_hook_active` / SubagentStop ignored).

## MCP (.mcp.json → mcp/server.py)

Server name `lake-memory`, stdio, `mcp` python package (FastMCP). Tools,
thin over one read-only-where-possible `lake.open(target)`, the target
resolved once at startup by `lake.resolve(default="~/.lake/claude.lake")`:

- `remember(query, source?, tags?, limit=20)` → recall hits, rendered one
  line each (score, id, timestamp, source, content).
- `deep_recall(plan, limit?)` → the last step's hits of a §5.5 plan,
  rendered like remember, with the §6.5 sediment row printed first when the
  deep pass wrote one; the tool description teaches the plan DSL. Opens the
  file writable; a local file gets no think, so there it stays model-free
  and the sediment pass runs only on a `lake serve` that has one.
- `context(query, budget=24000)` → the rendered block (for deliberate deep
  pulls; the hook already injects a smaller one).
- `write(content, tags?, source="claude-code", kind?, derived_from?)` → id.
  Deliberate notes; `kind` (container, crystal, mood, or sediment) with
  `derived_from` marks a deliberate take over the rows it cites.
- `engage(delta_id, kind, note?)` → id. affirm | refute | reply.
- `lineage(delta_id)` → the provenance walk, one line per ancestor.
- `crystal()` → the current crystal text.
- `stats()` → the §4.8 dict, JSON.

Startup failures must not take the session with them, and a lake that is down
at startup must not be sticky. The tools are always served, and each re-opens
the lake per call (like the hooks), so a call that can't reach it returns a
retry-safe message and the tools recover on their own once the lake is back.
The startup probe only logs a one-line stderr diagnostic; it no longer freezes
the server into a `lake_status`-only degraded state. The one hard stop is a
missing `mcp` package, which prints to stderr and exits 1 — Claude Code shows a
failed MCP server and the session continues without the tools.

## Timer

`install-timer.sh`: writes `~/.lake/env` (if absent), installs
`lake-consolidate.service` + `lake-consolidate.timer` (user units,
`OnBootSec=10min`, `OnUnitActiveSec=6h`, `Persistent=true`), enables the
timer. `lake-consolidate.sh` sets `LAKE_ENV_FILE` to `$LAKE_HOME/env` when
unset, finds `LAKE_BIN` by the hooks' rule and runs one command (SPEC §6.6),
every step of it due-gated:

```
LAKE_HOOKS_OFF=1 lake digest --default "$LAKE_HOME/claude.lake" [--max-units N --since D --max-tokens N]
```

The CLI takes `LAKE_THINK` (default `claude:--model claude-opus-5-5[1m]`) and
`LAKE_EMBED` for a local file; on a remote lake the digest runs on the server
with its own specs (SPEC §13.9). The bracketed flags come only from the
deprecated `LAKE_MAX_UNITS` / `LAKE_CATCHUP_SINCE` / `LAKE_CATCHUP_MAX_TOKENS`.
The timer is optional where `lake serve --digest` runs (SPEC §13.2).

`claude` as think uses `claude -p` with hooks disabled and
`LAKE_HOOKS_OFF=1` (SPEC §8), so consolidation runs on the user's own
subscription and never loops through the plugin.

## Install / test / revert

- Dev/local: `claude --plugin-dir ./plugin` (from a checkout); reload with
  `/reload-plugins`; `claude plugin validate plugin/` must pass.
- Real install: `claude plugin init`-style copy into
  `~/.claude/skills/lake/` (skills-dir plugins auto-load) or a marketplace
  later. Prereq: `pip install fathom-lake` (`[mcp]` extra for the tools).
- Revert: remove the plugin dir, `scripts/uninstall-timer.sh`; the lake
  file stays (it is the user's memory; deleting it is theirs to do).

## Tests (repo suite: `tests/test_plugin_hooks.py`, `tests/test_plugin_mcp.py`, `tests/test_plugin_scripts.py`)

Hook scripts are pipe-tested exactly as Claude Code runs them: subprocess
with synthetic stdin JSON, `LAKE_FILE` in a tmp dir, `LAKE_BIN` set to
`<venv python> -m lake.cli`. Pin: prompt write lands with the right tags;
`/command` prompts are not written; recall output is valid
hookSpecificOutput JSON with the right event name; session's own rows are
excluded from its recall; Stop writes the message and skips empty; every
hook exits 0 and prints nothing harmful with LAKE_HOOKS_OFF=1, with a
missing LAKE_BIN, and with an unwritable LAKE_FILE (fail-open). MCP:
spawn the server, do the stdio initialize handshake, list tools, call
remember and write, assert the row landed. Marked `plugin`; skipped when
`mcp` is not importable. Timer scripts: bash -n syntax check plus a run
against a stub `lake` on PATH recording its argv and environment.
