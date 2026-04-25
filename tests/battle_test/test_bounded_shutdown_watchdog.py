"""Harness Epic Slice 1 — BoundedShutdownWatchdog tests.

Pins the contract:

* Daemon thread spawns at construction (does not block test teardown).
* ``arm(reason, deadline_s)`` triggers the deadline.
* ``disarm()`` cancels before fire.
* Deadline elapse without disarm → ``os._exit(75)`` fires (verified via
  injected ``exit_fn`` recorder).
* First-arm-wins (no accidental deadline extension).
* Master flag default true; ``=false`` disables arm/fire entirely.
* Re-arm after disarm works cleanly.
* Forensic stderr line emitted before the exit call.
* Concurrent arm/disarm is thread-safe.

Test affordances injected via constructor:
* ``exit_fn`` — recorder that captures the exit code without exiting.
* ``sleep_fn`` — fast-clock that returns immediately for deadline tests.
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from backend.core.ouroboros.battle_test.shutdown_watchdog import (
    BoundedShutdownWatchdog,
    EXIT_CODE_HARNESS_WEDGED,
    bounded_shutdown_enabled,
    default_deadline_s,
)


# ---------------------------------------------------------------------------
# (A) Flag defaults + env knobs
# ---------------------------------------------------------------------------


def test_master_flag_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED defaults true post-Slice-1."""
    monkeypatch.delenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", raising=False)
    assert bounded_shutdown_enabled() is True


def test_master_flag_explicit_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "false")
    assert bounded_shutdown_enabled() is False


def test_default_deadline_default_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", raising=False)
    assert default_deadline_s() == 30.0


def test_default_deadline_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", "5.5")
    assert default_deadline_s() == 5.5


def test_default_deadline_garbage_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", "not-a-number")
    assert default_deadline_s() == 30.0


def test_exit_code_constant():
    assert EXIT_CODE_HARNESS_WEDGED == 75


# ---------------------------------------------------------------------------
# (B) Construction + daemon thread
# ---------------------------------------------------------------------------


def test_construction_starts_daemon_thread() -> None:
    """The watchdog thread MUST be daemon — otherwise Py_FinalizeEx
    deadlock (the original problem this whole epic exists to solve)."""
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        assert wdg._thread.is_alive()
        assert wdg._thread.daemon is True
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_initial_state_idle() -> None:
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        assert wdg.is_armed is False
        assert wdg.reason is None
        assert wdg.fired is False
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (C) arm() / disarm() lifecycle
# ---------------------------------------------------------------------------


def test_arm_records_reason_and_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        result = wdg.arm(reason="sigterm", deadline_s=10.0)
        assert result is True
        assert wdg.is_armed is True
        assert wdg.reason == "sigterm"
        assert wdg.deadline_s == 10.0
        assert wdg.armed_at_monotonic is not None
    finally:
        wdg.disarm()
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_disarm_clears_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="sigterm", deadline_s=10.0)
        result = wdg.disarm()
        assert result is True
        assert wdg.is_armed is False
        # Reason is preserved for postmortem (not cleared)
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_arm_first_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple arm() calls — first wins, later are no-ops. Avoids
    accidental deadline extension."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="sigterm", deadline_s=10.0)
        result = wdg.arm(reason="sigint", deadline_s=60.0)
        assert result is False
        assert wdg.reason == "sigterm"
        assert wdg.deadline_s == 10.0
    finally:
        wdg.disarm()
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_disarm_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="sigterm", deadline_s=10.0)
        wdg.disarm()
        result = wdg.disarm()
        assert result is False  # already disarmed
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_rearm_after_disarm_works_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="sigterm", deadline_s=10.0)
        wdg.disarm()
        result = wdg.arm(reason="sigint", deadline_s=20.0)
        assert result is True
        assert wdg.reason == "sigint"
        assert wdg.deadline_s == 20.0
    finally:
        wdg.disarm()
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_arm_returns_false_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Master flag off → arm() is a no-op (returns False, doesn't set state)."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "false")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        result = wdg.arm(reason="sigterm", deadline_s=10.0)
        assert result is False
        assert wdg.is_armed is False
        assert wdg.reason is None
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (D) Deadline elapse → exit_fn fires
# ---------------------------------------------------------------------------


def test_deadline_elapse_fires_exit_fn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-time deadline test (short deadline). exit_fn is called with
    EXIT_CODE_HARNESS_WEDGED when the deadline elapses without disarm."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    fired_event = threading.Event()

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        fired_event.set()

    wdg = BoundedShutdownWatchdog(exit_fn=_record_exit)
    try:
        wdg.arm(reason="test_deadline", deadline_s=0.1)
        # Wait for the thread to fire (with generous timeout)
        fired = fired_event.wait(timeout=2.0)
        assert fired, "watchdog should have fired exit_fn within 2s"
        assert exit_calls == [EXIT_CODE_HARNESS_WEDGED]
        assert wdg.fired is True
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_disarm_before_deadline_prevents_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    try:
        wdg.arm(reason="test_disarm", deadline_s=2.0)
        time.sleep(0.05)  # let thread enter the post-arm sleep
        wdg.disarm()
        time.sleep(0.3)  # wait past where the deadline would have fired
        assert exit_calls == []
        assert wdg.fired is False
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_deadline_elapse_writes_forensic_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The forensic stderr line is critical — bypasses logging which may
    itself be wedged. Pinned so it's not accidentally removed."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    fired_event = threading.Event()

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        fired_event.set()

    wdg = BoundedShutdownWatchdog(exit_fn=_record_exit)
    try:
        wdg.arm(reason="forensic_test", deadline_s=0.05)
        fired_event.wait(timeout=2.0)
        captured = capsys.readouterr()
        assert "[BoundedShutdownWatchdog] FIRED" in captured.err
        assert "forensic_test" in captured.err
        assert "os._exit(75)" in captured.err
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (E) Multi-cycle (arm → disarm → arm → fire)
# ---------------------------------------------------------------------------


def test_arm_disarm_arm_fire_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First arm gets disarmed cleanly; second arm fires."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    fired_event = threading.Event()

    def _record_exit(code: int) -> None:
        exit_calls.append(code)
        fired_event.set()

    wdg = BoundedShutdownWatchdog(exit_fn=_record_exit)
    try:
        wdg.arm(reason="first_arm", deadline_s=2.0)
        time.sleep(0.05)
        wdg.disarm()
        time.sleep(0.05)
        # Second arm — let it fire
        wdg.arm(reason="second_arm_fires", deadline_s=0.05)
        fired_event.wait(timeout=2.0)
        assert exit_calls == [EXIT_CODE_HARNESS_WEDGED]
        assert wdg.reason == "second_arm_fires"
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (F) Thread safety — concurrent arm/disarm
# ---------------------------------------------------------------------------


def test_concurrent_arm_calls_thread_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many threads racing to arm() — first-wins invariant holds, no crashes."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)
    arm_results: list = []

    def _race_arm(idx: int) -> None:
        result = wdg.arm(reason=f"thread_{idx}", deadline_s=10.0)
        arm_results.append(result)

    try:
        threads = [
            threading.Thread(target=_race_arm, args=(i,))
            for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)

        # Exactly one True (the first arm) and 19 False
        assert sum(arm_results) == 1
        assert wdg.is_armed is True
    finally:
        wdg.disarm()
        wdg.stop()
        wdg._thread.join(timeout=1.0)


def test_concurrent_arm_and_disarm_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stress test: 50 arm/disarm pairs racing in 20 threads. Must complete
    within reasonable time (no deadlock)."""
    monkeypatch.setenv("JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED", "true")
    exit_calls: list = []
    wdg = BoundedShutdownWatchdog(exit_fn=exit_calls.append)

    def _churn(idx: int) -> None:
        for _ in range(50):
            wdg.arm(reason=f"t{idx}", deadline_s=10.0)
            wdg.disarm()

    try:
        threads = [
            threading.Thread(target=_churn, args=(i,))
            for i in range(20)
        ]
        t0 = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"churn took {elapsed:.2f}s — possible deadlock"
        # No exit fired (all disarms beat any deadline)
        assert exit_calls == []
    finally:
        wdg.stop()
        wdg._thread.join(timeout=1.0)


# ---------------------------------------------------------------------------
# (G) Source-grep pins
# ---------------------------------------------------------------------------


def test_module_uses_os_underscore_exit_not_sys_exit():
    """os._exit is the documented escape hatch — sys.exit runs cleanup
    handlers which is exactly what we're trying to bypass."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
    ).read_text()
    # Default exit_fn is os._exit
    assert "exit_fn: Callable[[int], None] = os._exit" in src
    # No sys.exit usage at module level
    assert "sys.exit" not in src


def test_thread_is_daemon_pinned():
    """Daemon=True is THE invariant — it's why Py_FinalizeEx doesn't
    deadlock waiting for this thread. Pin it source-side."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/battle_test/shutdown_watchdog.py"
    ).read_text()
    assert "daemon=True" in src


def test_harness_wires_watchdog_at_three_sites():
    """harness.py wires arm() at signal handler + WallClockWatchdog,
    and disarm() at clean-shutdown completion. Pin the wiring so it
    survives drift."""
    from pathlib import Path
    src = Path(
        "backend/core/ouroboros/battle_test/harness.py"
    ).read_text()
    # arm sites: 2 (signal handler + wall clock)
    assert src.count('_wdg.arm(') >= 2
    # disarm site: 1 (clean shutdown)
    assert "_wdg.disarm()" in src
    # Construction at __init__
    assert "BoundedShutdownWatchdog as _BoundedShutdownWatchdog" in src
