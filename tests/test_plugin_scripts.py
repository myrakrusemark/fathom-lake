"""Plugin timer scripts (plugin/DESIGN.md, Timer section): syntax, install-timer.sh
output under LAKE_HOME/SYSTEMD_USER_DIR overrides, lake-consolidate.sh's one
`lake digest` with LAKE_HOOKS_OFF exported, uninstall-timer.sh cleanup. Real home is never
touched: every run gets a synthetic HOME plus both overrides, and stub `systemctl`
and `lake` executables on PATH record what the scripts do."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = Path(__file__).resolve().parent.parent / "plugin" / "scripts"
INSTALL = PLUGIN_SCRIPTS / "install-timer.sh"
CONSOLIDATE = PLUGIN_SCRIPTS / "lake-consolidate.sh"
UNINSTALL = PLUGIN_SCRIPTS / "uninstall-timer.sh"

SYSTEMCTL_STUB = '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$SYSTEMCTL_LOG"\n'
LAKE_STUB = (
    "#!/usr/bin/env bash\n"
    "{\n"
    "  printf 'argv: %s\\n' \"$*\"\n"
    "  printf 'hooks_off: %s\\n' \"${LAKE_HOOKS_OFF:-unset}\"\n"
    "  printf 'lake: %s\\n' \"${LAKE:-unset}\"\n"
    "  printf 'env_file: %s\\n' \"${LAKE_ENV_FILE:-unset}\"\n"
    '} >> "$LAKE_LOG"\n'
)


def make_stub(bin_dir: Path, name: str, body: str) -> Path:
    """Write an executable stub script into bin_dir."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def script_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Environment for a script run: stub bin first on PATH, synthetic HOME, both overrides."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(home),
        "LAKE_HOME": str(tmp_path / "lakehome"),
        "SYSTEMD_USER_DIR": str(tmp_path / "systemd-user"),
        "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        "LAKE_LOG": str(tmp_path / "lake.log"),
        "PYTHONPATH": str(PLUGIN_SCRIPTS.parents[1]),  # hook.py lake-bin finds this checkout's lake
    }
    env.update(extra)
    return env


def run_script(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, timeout=30
    )


def log_lines(path_str: str) -> list[str]:
    path = Path(path_str)
    return path.read_text().splitlines() if path.exists() else []


@pytest.mark.parametrize("script", [INSTALL, CONSOLIDATE, UNINSTALL], ids=lambda p: p.name)
def test_bash_syntax(script: Path) -> None:
    proc = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr


def test_install_writes_env_and_units(tmp_path: Path) -> None:
    make_stub(tmp_path / "bin", "systemctl", SYSTEMCTL_STUB)
    env = script_env(tmp_path)
    proc = run_script(INSTALL, env)
    assert proc.returncode == 0, proc.stderr

    env_file = tmp_path / "lakehome" / "env"
    assert env_file.is_file()
    env_text = env_file.read_text()
    assert "LAKE_THINK='claude:--model claude-opus-5-5[1m]'" in env_text
    assert "LAKE_EMBED" in env_text  # documented, commented out (default unset)
    assert "#LAKE=" in env_text and "#LAKE_TOKEN=" in env_text  # the one target key (§13.8), commented out
    assert "#LAKE_AUTOMATION=tag:automation" in env_text and "LAKE_CATCHUP" not in env_text and "LAKE_PYTHON" not in env_text

    service = (tmp_path / "systemd-user" / "lake-consolidate.service").read_text()
    assert "Type=oneshot" in service
    assert f'Environment="LAKE_HOME={tmp_path / "lakehome"}"' in service
    assert "lake-consolidate.sh" in service
    assert "ExecStart=/bin/bash" in service

    timer = (tmp_path / "systemd-user" / "lake-consolidate.timer").read_text()
    assert "OnBootSec=10min" in timer
    assert "OnUnitActiveSec=6h" in timer
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer

    assert log_lines(env["SYSTEMCTL_LOG"]) == [
        "--user daemon-reload",
        "--user enable --now lake-consolidate.timer",
    ]
    # Overrides respected: nothing landed under the synthetic home.
    assert list((tmp_path / "home").iterdir()) == []


def test_install_keeps_existing_env(tmp_path: Path) -> None:
    make_stub(tmp_path / "bin", "systemctl", SYSTEMCTL_STUB)
    lakehome = tmp_path / "lakehome"
    lakehome.mkdir()
    (lakehome / "env").write_text("LAKE_THINK=cmd:my-model\n")
    proc = run_script(INSTALL, script_env(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert (lakehome / "env").read_text() == "LAKE_THINK=cmd:my-model\n"


DIGEST = "argv: digest --default {home}/claude.lake"


def test_consolidate_runs_lake_digest(tmp_path: Path) -> None:
    """The timer runs one `lake digest` (SPEC §6.6): the CLI resolves the lake and the model, with the plugin's
    default file, $LAKE_HOME/env as the env file, and LAKE_HOOKS_OFF exported."""
    make_stub(tmp_path / "bin", "lake", LAKE_STUB)
    (tmp_path / "lakehome").mkdir()
    env = script_env(tmp_path)
    proc = run_script(CONSOLIDATE, env)
    assert proc.returncode == 0, proc.stderr
    lines = log_lines(env["LAKE_LOG"])
    assert only(lines, "argv: ") == [DIGEST.format(home=tmp_path / "lakehome")]
    assert only(lines, "hooks_off: ") == ["hooks_off: 1"]
    assert only(lines, "env_file: ") == [f"env_file: {tmp_path / 'lakehome' / 'env'}"]
    assert proc.stderr == ""


def only(lines: list[str], prefix: str) -> list[str]:
    return [line for line in lines if line.startswith(prefix)]


def test_consolidate_maps_the_deprecated_knobs(tmp_path: Path) -> None:
    """LAKE_MAX_UNITS, LAKE_CATCHUP_SINCE and LAKE_CATCHUP_MAX_TOKENS (process environment, else the env file) become
    --max-units, --since and --max-tokens, each with a deprecation line; LAKE_PYTHON is no longer read."""
    make_stub(tmp_path / "bin", "lake", LAKE_STUB)
    lakehome = tmp_path / "lakehome"
    lakehome.mkdir()
    (lakehome / "env").write_text("LAKE_MAX_UNITS=40\nLAKE_CATCHUP_SINCE=2026-09-07\nLAKE_CATCHUP_MAX_TOKENS=500000\n"
                                  "LAKE_PYTHON=py-stub\n")
    env = script_env(tmp_path, LAKE_MAX_UNITS="7")
    proc = run_script(CONSOLIDATE, env)
    assert proc.returncode == 0, proc.stderr
    assert only(log_lines(env["LAKE_LOG"]), "argv: ") == [
        DIGEST.format(home=lakehome) + " --max-units 7 --since 2026-09-07 --max-tokens 500000"]
    assert proc.stderr.count("is deprecated; use lake digest") == 3


def test_consolidate_lake_bin_follows_the_one_rule(tmp_path: Path) -> None:
    """SPEC §13.8 LAKE_BIN (hook.py lake-bin): the env file's LAKE_BIN runs the digest when the process names no
    lake, and is ignored when it does (a wrapper there may pin the env file's own lake)."""
    make_stub(tmp_path / "bin", "lake", LAKE_STUB)
    make_stub(tmp_path / "bin", "file-lake", LAKE_STUB.replace("argv: ", "file-bin: "))
    (tmp_path / "lakehome").mkdir()
    env_file = tmp_path / "scratch-env"
    env_file.write_text("export LAKE_URL=http://127.0.0.1:9\nexport LAKE_TOKEN=live-token\nLAKE_BIN=file-lake\n")
    env = script_env(tmp_path, LAKE_ENV_FILE=str(env_file))
    assert run_script(CONSOLIDATE, env).returncode == 0
    assert only(log_lines(env["LAKE_LOG"]), "file-bin: ") and not only(log_lines(env["LAKE_LOG"]), "argv: ")
    Path(env["LAKE_LOG"]).unlink()
    mine = str(tmp_path / "mine.lake")
    env = script_env(tmp_path, LAKE=mine, LAKE_ENV_FILE=str(env_file))
    assert run_script(CONSOLIDATE, env).returncode == 0
    lines = log_lines(env["LAKE_LOG"])
    assert not only(lines, "file-bin: ") and only(lines, "argv: ") == [DIGEST.format(home=tmp_path / "lakehome")]
    assert only(lines, "lake: ") == [f"lake: {mine}"], "the process target reaches the CLI untouched"


def test_consolidate_multiword_lake_bin(tmp_path: Path) -> None:
    make_stub(tmp_path / "bin", "lake-run", LAKE_STUB)
    (tmp_path / "lakehome").mkdir()
    env = script_env(tmp_path, LAKE_BIN="lake-run --flag")
    proc = run_script(CONSOLIDATE, env)
    assert proc.returncode == 0, proc.stderr
    assert only(log_lines(env["LAKE_LOG"]), "argv: ") == ["argv: --flag " + DIGEST.format(home=tmp_path / "lakehome")[6:]]


def test_consolidate_exit_status_is_the_digest_s(tmp_path: Path) -> None:
    """`lake digest` exits 1 when a step failed (the others still ran); the timer unit sees that status."""
    make_stub(tmp_path / "bin", "lake", "#!/usr/bin/env bash\nexit 1\n")
    (tmp_path / "lakehome").mkdir()
    assert run_script(CONSOLIDATE, script_env(tmp_path)).returncode == 1


def test_catchup_script_is_executable_and_parses() -> None:
    script = PLUGIN_SCRIPTS / "lake-catchup.py"
    assert os.access(script, os.X_OK)
    proc = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "--dry-run" in proc.stdout


def test_uninstall_removes_units(tmp_path: Path) -> None:
    make_stub(tmp_path / "bin", "systemctl", SYSTEMCTL_STUB)
    systemd_dir = tmp_path / "systemd-user"
    systemd_dir.mkdir()
    (systemd_dir / "lake-consolidate.service").write_text("[Service]\n")
    (systemd_dir / "lake-consolidate.timer").write_text("[Timer]\n")
    env = script_env(tmp_path)
    proc = run_script(UNINSTALL, env)
    assert proc.returncode == 0, proc.stderr
    assert not (systemd_dir / "lake-consolidate.service").exists()
    assert not (systemd_dir / "lake-consolidate.timer").exists()
    assert log_lines(env["SYSTEMCTL_LOG"]) == [
        "--user disable --now lake-consolidate.timer",
        "--user daemon-reload",
    ]


def test_install_then_uninstall_roundtrip(tmp_path: Path) -> None:
    make_stub(tmp_path / "bin", "systemctl", SYSTEMCTL_STUB)
    env = script_env(tmp_path)
    assert run_script(INSTALL, env).returncode == 0
    assert run_script(UNINSTALL, env).returncode == 0
    assert list((tmp_path / "systemd-user").iterdir()) == []
    # The env file survives an uninstall; the memory and its config are the user's.
    assert (tmp_path / "lakehome" / "env").is_file()
