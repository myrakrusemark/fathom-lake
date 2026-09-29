#!/usr/bin/env bash
# install-timer.sh — write the timer env file (if absent) and the systemd user
# units for background consolidation (`lake digest`), then enable
# lake-consolidate.timer. Optional: a host running `lake serve --digest nightly`
# needs no timer (SPEC §13.2).
#
# Env overrides (used by the test suite so it never touches the real home):
#   LAKE_HOME          where the env file lives; default ~/.lake
#   SYSTEMD_USER_DIR   where the units go; default ~/.config/systemd/user
set -u

LAKE_HOME="${LAKE_HOME:-$HOME/.lake}"
SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || exit 1
consolidate_sh="$script_dir/lake-consolidate.sh"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "install-timer: systemctl not found; this installer needs a systemd user session" >&2
  exit 1
fi

mkdir -p -- "$LAKE_HOME" "$SYSTEMD_USER_DIR" || exit 1

env_file="$LAKE_HOME/env"
if [ ! -f "$env_file" ]; then
  cat > "$env_file" <<'EOF' || exit 1
# lake configuration (SPEC §13.8). The CLI (and so lake-consolidate.sh, which
# runs `lake digest`), the three hooks, the MCP server, `lake serve` and
# lake.open() read it with one resolver, lake.resolve(). Each key: the process
# environment first, then this file. The first source that names a lake wins
# whole: when the process environment names one, this file's lake and LAKE_BIN
# are not used, and its LAKE_TOKEN is sent only to its own URL.
#
# LAKE: the lake, a file path or a `lake serve` URL (http:// or https://).
# Default: ~/.lake/claude.lake. (LAKE_FILE and LAKE_URL still work, with a
# deprecation note.) LAKE_TOKEN: the bearer for a URL.
#LAKE=
#LAKE_TOKEN=
#
# LAKE_THINK: the model consolidation runs on (SPEC §8 --think forms):
#   claude                    claude -p on your own subscription, the account's default model
#   claude:<args>             the same, plus claude CLI args, e.g. claude:--model sonnet
#                             (default: Opus 5.5 with 1M context, pinned so an account default can't change it)
#   ollama:<model>@<url>      a local Ollama model
#   cmd:<shell command>       any command; prompt on stdin, answer on stdout, the system
#                             prompt only in $LAKE_SYSTEM (cmd:claude … without it is refused)
LAKE_THINK='claude:--model claude-opus-5-5[1m]'
# LAKE_EMBED: embeddings for the consolidated rows; leave unset for FTS-only.
#LAKE_EMBED=ollama:nomic-embed-text@http://localhost:11434
# LAKE_AUTOMATION: rows kept out of consolidation but still searchable (SPEC
# §4.3): comma-separated tag:/source:/prefix: rules. Unset: tag:automation, the
# label an automated caller gives its rows with LAKE_TAGS=automation.
#LAKE_AUTOMATION=tag:automation
# LAKE_BIN: the lake CLI, when it is not on systemd's PATH.
#LAKE_BIN=
EOF
  echo "wrote $env_file"
fi

cat > "$SYSTEMD_USER_DIR/lake-consolidate.service" <<EOF || exit 1
[Unit]
Description=lake digest (whatever consolidation is due: containers, catch-up, mood, crystal)

[Service]
Type=oneshot
Environment="LAKE_HOME=$LAKE_HOME"
ExecStart=/bin/bash "$consolidate_sh"
EOF
echo "wrote $SYSTEMD_USER_DIR/lake-consolidate.service"

cat > "$SYSTEMD_USER_DIR/lake-consolidate.timer" <<EOF || exit 1
[Unit]
Description=Run lake consolidation every six hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
EOF
echo "wrote $SYSTEMD_USER_DIR/lake-consolidate.timer"

systemctl --user daemon-reload || exit 1
systemctl --user enable --now lake-consolidate.timer || exit 1
echo "enabled lake-consolidate.timer"
