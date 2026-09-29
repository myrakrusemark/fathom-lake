#!/usr/bin/env bash
# lake-consolidate.sh — what lake-consolidate.timer runs: `lake digest` (SPEC §6.6), whatever consolidation is
# due (containers, the catch-up, mood, crystal), each step due-gated, so running this often is cheap. The CLI
# resolves the lake (SPEC §13.8; default $LAKE_HOME/claude.lake) and the model (LAKE_THINK, default Opus 5.5);
# on a `lake serve` URL the digest runs on the server with the server's think. Env file: $LAKE_ENV_FILE, else
# $LAKE_HOME/env. Deprecated knobs still map to flags: LAKE_MAX_UNITS, LAKE_CATCHUP_SINCE, LAKE_CATCHUP_MAX_TOKENS.
set -u
LAKE_HOME="${LAKE_HOME:-$HOME/.lake}"
export LAKE_ENV_FILE="${LAKE_ENV_FILE:-$LAKE_HOME/env}"
here="$(dirname "$0")"
# LAKE_BIN by the one rule (hook.py lake-bin, SPEC §13.8); a multi-word command stays split.
mapfile -t lake_cmd < <(python3 "$here/../hooks/hook.py" lake-bin </dev/null 2>/dev/null)
[ "${#lake_cmd[@]}" -gt 0 ] || read -r -a lake_cmd <<< "${LAKE_BIN:-lake}"
args=()
for pair in LAKE_MAX_UNITS:--max-units LAKE_CATCHUP_SINCE:--since LAKE_CATCHUP_MAX_TOKENS:--max-tokens; do
  key="${pair%%:*}" flag="${pair#*:}" value="${!key:-}"
  [ -z "$value" ] && [ -f "$LAKE_ENV_FILE" ] && value="$(set +u; . "$LAKE_ENV_FILE" >/dev/null 2>&1; printf '%s' "${!key:-}")"
  [ -n "$value" ] || continue
  echo "lake-consolidate: $key is deprecated; use lake digest $flag" >&2
  args+=("$flag" "$value")
done
# Consolidation traffic must never re-enter the lake through the plugin's hooks (SPEC §8).
export LAKE_HOOKS_OFF=1
exec "${lake_cmd[@]}" digest --default "$LAKE_HOME/claude.lake" ${args[@]+"${args[@]}"}
