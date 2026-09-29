# Lake in Codex

This is the Codex packaging of the same Lake plugin used by Claude Code. It
reuses `hooks/prompt.sh`, `hooks/stop.sh`, `hooks/session-start.sh`, and
`mcp/server.py`; the Codex variant supplies `LAKE_SOURCE=codex`.

## Install or update

Prerequisites: working Lake CLI/Python package, existing `~/.lake/env` with the
shared `LAKE_URL`, and the Codex plugin-creator helpers installed with Codex.
From this checkout:

```sh
python3 plugin/codex/install.py
```

The installer uses the personal marketplace at
`~/.agents/plugins/marketplace.json`, stages this plugin at `~/plugins/lake`,
adds a version cachebuster, validates it, and invokes `codex plugin add
lake@personal`. It preserves unrelated marketplace entries and configuration.
It does not alter Claude configuration or trust hooks.

The source includes a portable root manifest and a `.codex-plugin` compatibility
manifest. The currently installed CLI recognizes the compatibility form but
omits components from the portable form. Therefore the installer materializes
the Codex `hooks/hooks.json` and `.mcp.json` from the Codex definitions and omits
the portable root in the staged copy. It also materializes the final installed absolute MCP script path because this CLI does not expand plugin-root variables in MCP arguments. Claude's source definitions are unchanged.

## Trust and verify

A plugin installation is not evidence of active capture. In a fresh Codex CLI,
open `/hooks`, review the three `lake@personal` plugin handlers, and trust those
exact definitions. They load the Lake memory context at session start, store
user prompts and recall relevant memory, and store the final assistant reply.
Do not use a trust bypass or hand-edit trust hashes. New versions may require
another review because installed paths change.

If the earlier manual installation exists, its three user-level hooks in
`~/.codex/hooks.json` are duplicates. Leave those untrusted while verifying the
plugin. The earlier direct MCP server may also coexist until the plugin MCP is
verified. Remove only the known Lake duplicate entries after successful checks,
preserving all unrelated configuration.

For read-only diagnosis, the Codex app-server `hooks/list` response reports the
actual source, plugin ID, enabled status, current hash, and trust status. The
`plugin/read` response should list three hooks and `lake-memory`.

After trust, run a small real Codex session with a clearly labelled verification
prompt and no tool calls. Query the lake for the new `source=codex` records tagged
`session:<actual-new-session-id>`, and compare the stored prompt and reply with
the actual session. Running hook scripts manually is only a unit check; it does
not establish runtime capture.

## Coverage and provenance

These shared hooks capture submitted prompt text and the final assistant message.
They do not store every conversation item: intermediate commentary, tool calls
and results, voice transcript segments, delegation wrappers, and earlier turns
are not automatically backfilled by these hooks. A task already running before
installation or trust may require a fresh runtime/session to pick up changes.

The hooks attach `source=codex`, role tags, and `session:<id>`. They do not attach
turn IDs or machine-readable `derived_from` links. Prose mentioning a file or
source is not equivalent to a provenance edge. The separate nightly activity
review can collect scoped transcript/file evidence and create linked summaries.
It must not infer whole-conversation capture from hook installation alone.

References: [official plugin packaging](https://developers.openai.com/plugins/build/plugins)
and [official hook trust documentation](https://learn.chatgpt.com/docs/hooks).
