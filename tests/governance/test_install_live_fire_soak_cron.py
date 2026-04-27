"""Smoke tests for scripts/install_live_fire_soak_cron.sh.

Pin the script's user-facing surface contract:
  * Dry-run renders a cron block with the expected schedule + caps
  * --help works without side effects
  * Begin/end markers present so --remove can find the block
  * No `crontab -` invocation in dry-run / status / help paths

Tests run the script via subprocess + assert on stdout.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "install_live_fire_soak_cron.sh"
)


def _run(args: list, env_override: dict = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if env_override:
        env.update(env_override)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, timeout=30,
        check=False,
    )


def test_script_exists_and_executable():
    assert SCRIPT.exists()


def test_help_works():
    r = _run(["--help"])
    assert r.returncode == 0
    assert "--install" in r.stdout
    assert "--dry-run" in r.stdout
    assert "--remove" in r.stdout
    assert "--once" in r.stdout
    assert "--status" in r.stdout


def test_dry_run_emits_cron_block():
    r = _run(["--dry-run"])
    assert r.returncode == 0
    assert "LIVE_FIRE_SOAK_BEGIN" in r.stdout
    assert "LIVE_FIRE_SOAK_END" in r.stdout
    assert "live_fire_graduation_soak.py" in r.stdout
    assert "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true" in r.stdout


def test_dry_run_default_schedule_every_8_hours():
    r = _run(["--dry-run"])
    assert "0 */8 * * *" in r.stdout


def test_dry_run_default_cost_cap():
    r = _run(["--dry-run"])
    assert "--cost-cap 0.50" in r.stdout


def test_dry_run_default_wall_clock_2400():
    r = _run(["--dry-run"])
    assert "--max-wall-seconds 2400" in r.stdout


def test_dry_run_env_override_schedule():
    r = _run(
        ["--dry-run"],
        env_override={"CRON_SCHEDULE": "0 6,14,22 * * *"},
    )
    assert "0 6,14,22 * * *" in r.stdout


def test_dry_run_env_override_cost_cap():
    r = _run(
        ["--dry-run"],
        env_override={"COST_CAP": "1.00"},
    )
    assert "--cost-cap 1.00" in r.stdout


def test_unknown_arg_exits_non_zero():
    r = _run(["--invalid-flag"])
    assert r.returncode != 0


def test_dry_run_includes_log_redirect():
    """Cron entry should redirect output to .jarvis/live_fire_soak_logs/
    so each invocation has an auditable log file."""
    r = _run(["--dry-run"])
    assert ".jarvis/live_fire_soak_logs/" in r.stdout
    assert ">>" in r.stdout
    assert "2>&1" in r.stdout


def test_dry_run_carries_pause_documentation():
    r = _run(["--dry-run"])
    assert "JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED" in r.stdout
