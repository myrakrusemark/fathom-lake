#!/usr/bin/env bash
# uninstall-timer.sh — disable the timer and remove the units. Leaves the
# lake file and $LAKE_HOME/env alone: the lake is the user's memory, and
# deleting it is theirs to do.
#
# Env override (used by the test suite): SYSTEMD_USER_DIR, default
# ~/.config/systemd/user.
set -u

SYSTEMD_USER_DIR="${SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now lake-consolidate.timer >/dev/null 2>&1 || true
fi

rm -f -- "$SYSTEMD_USER_DIR/lake-consolidate.timer" "$SYSTEMD_USER_DIR/lake-consolidate.service"

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

echo "removed lake-consolidate.timer and lake-consolidate.service; the lake file and env are untouched"
