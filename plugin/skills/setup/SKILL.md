---
name: setup
description: Install and verify the lake consolidation timer (the systemd user units plus ~/.lake/env), guided step by step.
---

# lake setup

Install the background consolidation timer for the lake plugin. Work through
the steps in order and show the user what each one found before moving on.

## 1. Check the lake CLI is present

Run:

```bash
lake --help
```

If that fails, check `LAKE_BIN` in the environment (it may name the CLI as a
multi-word command such as `python3 -m lake.cli`; try that form). If there is
no CLI at all, stop and tell the user to install it first:

```bash
pip install fathom-lake
```

Do not continue until a working CLI is confirmed.

## 2. Run the installer

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/install-timer.sh"
```

This writes `~/.lake/env` (only if it does not exist yet), writes
`lake-consolidate.service` and `lake-consolidate.timer` into
`~/.config/systemd/user/`, and enables the timer. It prints each file it
wrote. If it reports that `systemctl` is missing, the machine has no systemd
user session and the timer cannot be installed; tell the user they can run
`bash "${CLAUDE_PLUGIN_ROOT}/scripts/lake-consolidate.sh"` from their own
scheduler instead.

## 3. Verify

```bash
systemctl --user status lake-consolidate.timer
```

Expect `Active: active (waiting)` and a `Trigger` time. For the next-run
schedule:

```bash
systemctl --user list-timers lake-consolidate.timer
```

Report both to the user.

## 4. Point out the configuration

Tell the user consolidation runs on `claude -p` (their own subscription) by
default, and that `~/.lake/env` is where to change that: `LAKE_THINK` for the
model (`claude`, `ollama:<model>@<url>`, or `cmd:<shell command>`),
`LAKE_EMBED` for embeddings (unset means FTS-only), `LAKE` to move the
lake (a file path, or a `lake serve` URL with `LAKE_TOKEN`). The timer runs
`lake digest`, which picks up edits on its next run; nothing needs re-enabling.
To finish history that fell behind, `lake digest --since YYYY-MM-DD
--dry-run` estimates the cost first and the same command without `--dry-run`
starts the catch-up (later timer runs continue it). On a machine that runs
`lake serve`, `lake serve --digest nightly` does the same job and the timer is
optional.

To undo everything later:

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/uninstall-timer.sh"
```
