"""Harness Epic Slice 4 — graduation pin tests.

Closes the harness reliability epic. Pins the post-graduation contract
across all 4 slices:

A. Master flag defaults — `JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED` and
   `JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED` both default `True`.
B. Sub-flag defaults — `JARVIS_BATTLE_SHUTDOWN_DEADLINE_S=30`,
   `JARVIS_INTAKE_LOCK_STALE_TTL_S=7200`.
C. Hot-revert per safety mechanism — each can be disabled individually
   via env var without touching others.
D. Authority invariants — clean shutdown is unchanged (no os._exit fires
   on graceful termination); single-flight allows the current process.
E. Source-grep pins for every Slice 1–3 surface so unintended drift
   surfaces in CI rather than in production.

These tests run on every commit going forward. If a pin breaks, either
the change was an unintentional regression or the contract was
intentionally widened — in the latter case, update the pin AND the
runbook so the operator-facing source of truth stays aligned.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.shutdown_watchdog import (
    BoundedShutdownWatchdog,
    EXIT_CODE_HARNESS_WEDGED,
    bounded_shutdown_enabled,
    default_deadline_s,
)


# ---------------------------------------------------------------------------
# (A) Master flag defaults — graduated post-epic
# ---------------------------------------------------------------------------


def test_bounded_shutdown_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED defaults true post-Slice-1.

    The watchdog is disarm()-able, so default-true is safe — clean
    shutdowns never trigger os._exit.
    """
    monkeypatch.delenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", raising=False)
    assert bounded_shutdown_enabled() is True


def test_single_flight_default_true_in_launcher_source():
    """Source-grep: launcher uses default `true` for the single-flight
    env knob. No way to test the launcher's main() code path in isolation
    without subprocess machinery; the source-pin catches drift."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    assert 'JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED", "true"' in src


# ---------------------------------------------------------------------------
# (B) Sub-flag defaults
# ---------------------------------------------------------------------------


def test_shutdown_deadline_default_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", raising=False)
    assert default_deadline_s() == 30.0


def test_intake_lock_stale_ttl_default_7200s_in_source():
    """Source-grep: stale-TTL default is 7200 (2h) in the lock cleanup."""
    src = Path(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    ).read_text()
    # Used in the staleness check default fallback
    assert 'JARVIS_INTAKE_LOCK_STALE_TTL_S", "7200"' in src
    # Default 7200s
    assert "_stale_ttl = 7200.0" in src


def test_exit_code_harness_wedged_constant():
    assert EXIT_CODE_HARNESS_WEDGED == 75


# ---------------------------------------------------------------------------
# (C) Hot-revert — each safety mechanism disable-able individually
# ---------------------------------------------------------------------------


def test_hot_revert_bounded_shutdown_disabled_returns_false_from_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED=false → arm() is no-op."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "false")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        result = wdg.arm(reason="test", deadline_s=10.0)
        assert result is False
        assert wdg.is_armed is False
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_hot_revert_single_flight_disabled_in_main_flow():
    """Source-grep: single-flight check is gated by env var so operators
    can opt out without code patching."""
    src = Path("scripts/ouroboros_battle_test.py").read_text()
    # Gated check
    assert "JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED" in src
    # The check is wrapped in env-disable conditional
    assert (
        '"JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED", "true").lower() not in '
        '("false", "0", "no", "off")'
    ) in src


def test_hot_revert_lock_ttl_can_be_extended(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_INTAKE_LOCK_STALE_TTL_S can be set to a huge value to
    effectively disable the wedged-but-alive TTL detection."""
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter,
    )
    monkeypatch.setenv("JARVIS_INTAKE_LOCK_STALE_TTL_S", "999999999")
    artifact = tmp_path / "intake_router.lock"
    # PID 1 (alive), lock 1h old — well within the huge TTL
    artifact.write_text(json.dumps({
        "pid": 1,
        "ts": time.time() - 3600,
    }))
    result = UnifiedIntakeRouter._cleanup_stale_lock(artifact)
    assert result is False
    assert artifact.exists()


# ---------------------------------------------------------------------------
# (D) Authority invariants — clean shutdown unchanged
# ---------------------------------------------------------------------------


def test_clean_shutdown_disarms_before_deadline_no_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authority pin: graceful arm → disarm cycle does NOT call exit_fn.
    This is the contract that makes default-true safe."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="clean_shutdown_pin", deadline_s=2.0)
        time.sleep(0.05)
        wdg.disarm()
        time.sleep(0.3)
        assert exit_calls == []
        assert wdg.fired is False
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (E) Source-grep pins for every Slice 1–3 surface
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_slice_1_pin_watchdog_module_exists():
    """Slice 1 — BoundedShutdownWatchdog primitive lives in the canonical
    location."""
    p = Path("backend/core/ouroboros/battle_test/shutdown_watchdog.py")
    assert p.is_file()


def test_slice_1_pin_watchdog_uses_os_underscore_exit():
    """Slice 1 — os._exit (NOT sys.exit) is THE invariant; sys.exit runs
    cleanup handlers which is exactly what we're trying to bypass when
    the asyncio path is wedged."""
    src = _read("backend/core/ouroboros/battle_test/shutdown_watchdog.py")
    assert "exit_fn: Callable[[int], None] = os._exit" in src
    assert "sys.exit" not in src


def test_slice_1_pin_watchdog_thread_is_daemon():
    """Slice 1 — daemon=True is the THE invariant that prevents
    Py_FinalizeEx deadlock on join."""
    src = _read("backend/core/ouroboros/battle_test/shutdown_watchdog.py")
    assert "daemon=True" in src


def test_slice_1_pin_harness_wires_three_sites():
    """Slice 1 — harness wires watchdog at __init__ + signal handler +
    WallClockWatchdog + clean-shutdown disarm."""
    src = _read("backend/core/ouroboros/battle_test/harness.py")
    # 2 arm sites (signal handler + wall clock alarm)
    assert src.count("_wdg.arm(") >= 2
    # 1 disarm site (clean shutdown)
    assert "_wdg.disarm()" in src
    # __init__ construction
    assert "BoundedShutdownWatchdog as _BoundedShutdownWatchdog" in src


def test_slice_2_pin_lock_writer_emits_new_schema():
    """Slice 2 — lock writer includes monotonic_ts, wall_iso, session_id."""
    src = _read(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    )
    assert '"monotonic_ts": time.monotonic()' in src
    assert '"session_id": _session_id' in src


def test_slice_2_pin_wedged_but_alive_ttl_branch():
    """Slice 2 — the wedged-but-alive branch in _cleanup_stale_lock
    references the env knob and the canonical phrase for log audits."""
    src = _read(
        "backend/core/ouroboros/governance/intake/unified_intake_router.py"
    )
    assert "JARVIS_INTAKE_LOCK_STALE_TTL_S" in src
    assert "wedged-but-alive" in src


def test_slice_2_pin_single_flight_helper_exists():
    """Slice 2 — single-flight launcher helper exists with canonical
    pgrep + sys.exit(75)."""
    src = _read("scripts/ouroboros_battle_test.py")
    assert "def _single_flight_preflight()" in src
    assert r'"python3? scripts/ouroboros_battle_test\.py"' in src
    assert "sys.exit(75)" in src


def test_slice_2_pin_single_flight_runs_after_zombie_reap():
    """Slice 2 — single-flight check runs AFTER the zombie reap so it
    doesn't false-trip on dead-PID lockholders the reaper can clean."""
    src = _read("scripts/ouroboros_battle_test.py")
    reap_idx = src.find("_reap_zombies()")
    sf_idx = src.find("_single_flight_preflight()")
    assert reap_idx > 0
    assert sf_idx > reap_idx


def test_slice_3_pin_runbook_exists():
    """Slice 3 — operator runbook at canonical path."""
    p = Path("docs/operations/battle_test_runbook.md")
    assert p.is_file()


def test_slice_3_pin_ci_guard_exists_and_executable():
    """Slice 3 — CI guard script exists and is executable."""
    p = Path("scripts/check_no_stdin_guard.sh")
    assert p.is_file()
    mode = p.stat().st_mode
    assert mode & stat.S_IXUSR


def test_slice_3_pin_canonical_pgrep_consistent_runbook_vs_launcher():
    """Slice 3 — runbook + launcher use the same pgrep regex. If either
    drifts, false-positive/negative bugs surface."""
    runbook = _read("docs/operations/battle_test_runbook.md")
    launcher = _read("scripts/ouroboros_battle_test.py")
    canonical = r'python3? scripts/ouroboros_battle_test\.py'
    assert canonical in runbook
    assert canonical in launcher


def test_slice_3_pin_codebase_clean_of_banned_pattern():
    """Slice 3 — no banned 'tail -f /dev/null | python' pattern outside
    the documented exemptions (guard script + runbook)."""
    EXEMPT_PATHS = {
        "scripts/check_no_stdin_guard.sh",
        "docs/operations/battle_test_runbook.md",
    }
    for scope in (Path("docs"), Path("scripts")):
        for f in scope.rglob("*"):
            if not f.is_file():
                continue
            relpath = str(f.as_posix())
            if relpath in EXEMPT_PATHS:
                continue
            try:
                content = f.read_text(errors="ignore")
            except (OSError, UnicodeDecodeError):
                continue
            assert "tail -f /dev/null | python" not in content, (
                f"banned pattern found in {f} — use --headless instead"
            )


# ---------------------------------------------------------------------------
# (F) End-to-end: ci guard exits clean on the current tree
# ---------------------------------------------------------------------------


def test_ci_guard_exits_zero_on_current_tree():
    """The current main HEAD passes the CI guard. Any future commit
    that adds the banned pattern will fail this test in pytest BEFORE
    it reaches CI — fast-feedback for local development."""
    result = subprocess.run(
        ["bash", "scripts/check_no_stdin_guard.sh"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"CI guard fails on current tree; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# (G) Combined hot-revert recipe — full epic disable
# ---------------------------------------------------------------------------


def test_combined_hot_revert_recipe_documented_in_runbook():
    """The runbook mentions all hot-revert env knobs so operators have
    a single source of truth for "how do I turn off the new safety nets"."""
    runbook = _read("docs/operations/battle_test_runbook.md")
    # Each Slice 1–2 hot-revert env knob mentioned
    assert "JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED" in runbook
    assert "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S" in runbook
    assert "JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED" in runbook


# ---------------------------------------------------------------------------
# (H) Wave-3 contract preservation — graduated harness epic doesn't
#     accidentally disable the Wave 3 (7) cancel infrastructure that
#     graduated yesterday.
# ---------------------------------------------------------------------------


def test_wave_3_7_cancel_infrastructure_still_graduated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: harness epic graduation didn't somehow regress W3(7)."""
    monkeypatch.delenv("JARVIS_MID_OP_CANCEL_ENABLED", raising=False)
    from backend.core.ouroboros.governance.cancel_token import (
        mid_op_cancel_enabled,
    )
    assert mid_op_cancel_enabled() is True, (
        "W3(7) graduated default must remain True post-harness-epic"
    )
