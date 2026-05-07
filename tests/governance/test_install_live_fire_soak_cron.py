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
    assert "JARVIS_GRADUATION_LEDGER_ENABLED=true" in r.stdout
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
# 2026-04-27 update — cron entry env block (soak + contract + circuit breaker)
# 2026-05-05 update — JARVIS_GRADUATION_LEDGER_ENABLED for parent harness writes
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


def test_dry_run_env_flags_in_correct_order():
    """The cron entry must arm env vars BEFORE invoking the
    HARNESS python — env-on-prefix syntax. Order: graduation
    ledger, soak, contract, circuit-breaker. Pinned for
    review-friendly diffs.

    Cadence Slice 2 (2026-05-06): the cron line now also
    invokes ``cadence_preflight.py`` (a separate ``python3``
    call) BEFORE the env block. The harness invocation is the
    one that requires the env vars; anchor to ``$HARNESS_SCRIPT
    run`` to find the harness python3 specifically."""
    r = _run(["--dry-run"])
    ledger_idx = r.stdout.index(
        "JARVIS_GRADUATION_LEDGER_ENABLED=true",
    )
    soak_idx = r.stdout.index(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED=true",
    )
    contract_idx = r.stdout.index(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true",
    )
    cb_idx = r.stdout.index(
        "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED=true",
    )
    # Anchor to the harness invocation specifically — preflight
    # is a separate (and earlier) python3 call by design. The
    # ``live_fire_graduation_soak.py run`` token is distinctive
    # enough to match both the literal ``$HARNESS_SCRIPT run``
    # form (as written in build_cron_block) AND the expanded
    # form (where shell substitutes the absolute path).
    harness_idx = r.stdout.index(
        "live_fire_graduation_soak.py run",
    )
    assert (
        ledger_idx < soak_idx < contract_idx < cb_idx < harness_idx
    )


def test_dry_run_documents_contract_in_comment_block():
    """Operators reading the crontab should see why each flag is
    set. Pin the comment block."""
    r = _run(["--dry-run"])
    assert "Contract consultation (P9.2) blocks 0-op" in r.stdout
    assert "Circuit breaker (Option C)" in r.stdout


def test_dry_run_only_one_set_of_cron_env_flags():
    """Bit-rot guard: re-running --dry-run after a refactor must
    NOT accidentally double the flag block (e.g. a buggy edit that
    appends a second cron line). Each env literal appears EXACTLY ONCE
    in the rendered cron entry."""
    r = _run(["--dry-run"])
    for flag in [
        "JARVIS_GRADUATION_LEDGER_ENABLED=true",
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


# ---------------------------------------------------------------------------
# Single-source-of-truth contract — Phase 9 env vars MUST stay in
# sync across ALL entry points (Wave 3 hygiene 2026-05-05 follow-up).
# Closes the residual crack: the cron-generator was tested for the
# 4-var ordering, but the new wrapper script + crontab example were
# not. This pin asserts ALL entry points carry ALL 4 vars.
# ---------------------------------------------------------------------------


_REQUIRED_PHASE9_ENV_VARS = (
    "JARVIS_GRADUATION_LEDGER_ENABLED",
    "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED",
    "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT",
    "JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED",
)


def _read_repo_text(rel_path):
    base = Path(__file__).resolve().parents[2]
    return (base / rel_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel_path", [
    "scripts/run_live_fire_graduation_soak.sh",
    "scripts/install_live_fire_soak_cron.sh",
    "scripts/crontab-live-fire.example",
])
def test_phase9_env_vars_present_in_every_entry_point(rel_path):
    """All 4 Phase 9 env vars MUST appear in every entry point
    (cron generator + --once + wrapper + crontab example).
    Adding a 5th env var requires updating ALL files — the
    operator binding is structural, not documentation-only."""
    text = _read_repo_text(rel_path)
    for var in _REQUIRED_PHASE9_ENV_VARS:
        assert var in text, (
            f"{rel_path} missing required Phase 9 env var "
            f"{var!r} — entry points MUST stay in sync per "
            f"the single-source-of-truth contract"
        )


def test_phase9_wrapper_script_exports_all_four_vars():
    """The wrapper script `run_live_fire_graduation_soak.sh` MUST
    `export` (not just inline) all 4 vars so subprocesses
    inherit them — the parent harness process needs the ledger
    flag for `GraduationLedger.record_session()` writes."""
    text = _read_repo_text(
        "scripts/run_live_fire_graduation_soak.sh",
    )
    for var in _REQUIRED_PHASE9_ENV_VARS:
        assert f"export {var}=true" in text, (
            f"wrapper MUST `export {var}=true` (parent harness "
            f"propagation), got just inline assignment"
        )


def test_phase9_crontab_example_uses_inline_per_entry():
    """The crontab example uses inline `VAR=true VAR=true ...
    cmd` syntax (no `export`) since cron entries are evaluated
    fresh per fire. All 4 vars must precede the python invocation
    on the SAME line so the subprocess inherits them."""
    text = _read_repo_text("scripts/crontab-live-fire.example")
    # Find the cron line containing the python3 invocation.
    cron_lines = [
        line for line in text.split("\n")
        if "python3" in line
        and "live_fire_graduation_soak.py" in line
    ]
    assert cron_lines, (
        "crontab example missing the python3 invocation line"
    )
    for line in cron_lines:
        for var in _REQUIRED_PHASE9_ENV_VARS:
            assert f"{var}=true" in line, (
                f"cron line missing {var}=true: {line[:120]}"
            )
