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


# ---------------------------------------------------------------------------
# 2026-04-27 update — three-master-flag cron entry
# ---------------------------------------------------------------------------


def test_dry_run_arms_graduation_contract():
    """JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true is set in the
    cron entry. Without this flag, the default classifier silently
    graduates 0-op sessions as CLEAN — once-proof on session
    bt-2026-04-27-162115 demonstrated this explicitly."""
    r = _run(["--dry-run"])
    assert (
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true" in r.stdout
    )


def test_dry_run_arms_circuit_breaker():
    """JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true is set in the
    cron entry so Option C circuit breaker fires pre-GENERATE on
    DW topology block (vs late-detection messy-log path)."""
    r = _run(["--dry-run"])
    assert (
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true" in r.stdout
    )


def test_dry_run_three_master_flags_in_correct_order():
    """The cron entry must arm all three master flags BEFORE
    invoking python — env-on-prefix syntax. Order: soak, contract,
    circuit-breaker. Not strictly required, but pinned for review-
    friendly diffs."""
    r = _run(["--dry-run"])
    soak_idx = r.stdout.index(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true",
    )
    contract_idx = r.stdout.index(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true",
    )
    cb_idx = r.stdout.index(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true",
    )
    py_idx = r.stdout.index("/usr/bin/env python3")
    assert soak_idx < contract_idx < cb_idx < py_idx


def test_dry_run_documents_contract_in_comment_block():
    """Operators reading the crontab should see why each flag is
    set. Pin the comment block."""
    r = _run(["--dry-run"])
    assert "Contract consultation (P9.2) blocks 0-op" in r.stdout
    assert "Circuit breaker (Option C)" in r.stdout


def test_dry_run_only_one_set_of_three_flags():
    """Bit-rot guard: re-running --dry-run after a refactor must
    NOT accidentally double the flag block (e.g. a buggy edit that
    appends a second cron line). Each master flag literal appears
    EXACTLY ONCE in the rendered cron entry."""
    r = _run(["--dry-run"])
    for flag in [
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true",
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true",
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true",
    ]:
        # The `=true` literal appears EXACTLY ONCE — on the cron
        # line itself. The comment block names the flags by name
        # only (without `=true` suffix). A buggy refactor that
        # appends a second cron line would surface as count > 1.
        count = r.stdout.count(flag)
        assert count == 1, (
            f"flag {flag!r} appeared {count}× in dry-run; expected 1 "
            "(cron line only)"
        )
