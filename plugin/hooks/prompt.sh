#!/usr/bin/env bash
# UserPromptSubmit hook: write the prompt into the lake and inject recall for it.
# The work is in hook.py (one file for all three hooks). Fail-open: always exit 0. LAKE_HOOKS_OFF: set by
# the think adapters so consolidation traffic is not re-ingested (SPEC §8).
[ -n "${LAKE_HOOKS_OFF:-}" ] && exit 0
command -v python3 >/dev/null 2>&1 || exit 0
python3 "$(dirname "$0")/hook.py" prompt 2>/dev/null
exit 0
