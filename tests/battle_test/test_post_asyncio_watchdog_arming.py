"""Phase 9.1c (Fix A) — post-asyncio teardown watchdog arming.

Pins the script-level fix that arms the `BoundedShutdownWatchdog`
BEFORE `loop.shutdown_default_executor()`, closing the gap that
caused the once-run (`bt-2026-04-27-085300`) to hang for 1h 50m+
after `_generate_report` completed cleanly.

Pre-fix: watchdog only armed by signal-handler. Clean shutdowns
(idle_timeout / budget_exhausted / wall_clock_cap) had no escape
hatch if the executor shutdown wedged on a non-daemon
ThreadPoolExecutor worker.

Post-fix: every shutdown path arms the watchdog with reason
``post_asyncio_teardown``. If the post-asyncio teardown wedges
past `default_deadline_s` (default 30s), `os._exit(75)` fires.
"""
from __future__ import annotations

import inspect
import os
from typing import List

import pytest

import scripts.ouroboros_battle_test as bt
from backend.core.ouroboros.battle_test.shutdown_watchdog import (
    EXIT_CODE_HARNESS_WEDGED,
    BoundedShutdownWatchdog,
    bounded_shutdown_enabled,
    default_deadline_s,
)


# ---------------------------------------------------------------------------
# Pin: script source contains the post-asyncio arm
# ---------------------------------------------------------------------------


def test_main_finally_block_arms_watchdog():
    """The post-fix script must contain a watchdog.arm() call in
    main()'s finally block — keyed off ``post_asyncio_teardown``
    reason — BEFORE shutdown_asyncgens.

    Source-level pin (not a runtime test) because main() is a
    long-lived synchronous routine and we don't want to fire it
    from the test process."""
    src = inspect.getsource(bt)
    assert 'reason="post_asyncio_teardown"' in src, (
        "main() must arm the BoundedShutdownWatchdog with reason="
        "'post_asyncio_teardown' before the shutdown_asyncgens path"
    )
    # arm() is called BEFORE shutdown_asyncgens.
    arm_idx = src.index('reason="post_asyncio_teardown"')
    asyncgens_idx = src.index("shutdown_asyncgens")
    assert arm_idx < asyncgens_idx, (
        "watchdog.arm() must run BEFORE shutdown_asyncgens; the "
        "executor-wedge happens during shutdown_default_executor "
        "and the watchdog deadline must already be running"
    )


def test_main_finally_uses_default_deadline_s():
    """The arm() call uses `default_deadline_s()` so the deadline
    follows the operator-tunable env var
    `JARVIS_BATTLE_SHUTDOWN_DEADLINE_S`."""
    src = inspect.getsource(bt)
    # The fix imports default_deadline_s from shutdown_watchdog AND
    # passes it as deadline_s.
    assert "default_deadline_s" in src
    # And not a hardcoded number.
    assert "deadline_s=30" not in src


def test_main_finally_arm_is_defensive():
    """The arm() must be wrapped in try/except so a watchdog raise
    cannot block the script's clean-exit path."""
    src = inspect.getsource(bt)
    # Find the arm() line and verify it sits inside a try block.
    lines = src.splitlines()
    arm_line_idx = next(
        i for i, ln in enumerate(lines)
        if 'reason="post_asyncio_teardown"' in ln
    )
    # Walk up to find the enclosing try.
    has_enclosing_try = False
    for i in range(arm_line_idx, max(0, arm_line_idx - 20), -1):
        if "try:" in lines[i]:
            has_enclosing_try = True
            break
    assert has_enclosing_try


# ---------------------------------------------------------------------------
# Pin: watchdog arm-then-fire behavior on simulated wedge
# ---------------------------------------------------------------------------


def test_watchdog_fires_when_post_asyncio_wedges():
    """Simulate the once-run wedge: arm watchdog, then sleep PAST
    the deadline. Verify the watchdog fires `os._exit(75)`."""
    if not bounded_shutdown_enabled():
        pytest.skip("watchdog disabled by env")
    fired_codes: List[int] = []

    def fake_exit(code: int) -> None:
        fired_codes.append(code)

    wdg = BoundedShutdownWatchdog(
        exit_fn=fake_exit,
        sleep_fn=__import__("time").sleep,
    )
    try:
        ok = wdg.arm(
            reason="post_asyncio_teardown",
            deadline_s=0.2,  # tiny — fires fast
        )
        assert ok is True
        # Wait past deadline to let watchdog fire.
        import time as _t
        _t.sleep(0.5)
        assert wdg.fired is True
        assert fired_codes == [EXIT_CODE_HARNESS_WEDGED]
    finally:
        wdg.stop()


def test_watchdog_does_not_fire_when_disarmed_in_time():
    """If post-asyncio teardown completes within deadline, disarm()
    cancels the deadline and os._exit doesn't fire."""
    if not bounded_shutdown_enabled():
        pytest.skip()
    fired_codes: List[int] = []

    def fake_exit(code: int) -> None:
        fired_codes.append(code)

    wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
    try:
        wdg.arm(reason="post_asyncio_teardown", deadline_s=2.0)
        import time as _t
        _t.sleep(0.1)
        wdg.disarm()
        _t.sleep(2.5)  # past original deadline
        assert wdg.fired is False
        assert fired_codes == []
    finally:
        wdg.stop()


def test_watchdog_re_arm_after_signal_handler_disarm():
    """Signal handler arms-then-disarms; main()'s finally block
    re-arms. Verify the second arm() succeeds (first-arm-wins is
    reset by disarm())."""
    if not bounded_shutdown_enabled():
        pytest.skip()
    fired_codes: List[int] = []

    def fake_exit(code: int) -> None:
        fired_codes.append(code)

    wdg = BoundedShutdownWatchdog(exit_fn=fake_exit)
    try:
        # Signal-handler-style first arm.
        wdg.arm(reason="sigterm", deadline_s=10.0)
        wdg.disarm()
        # main()'s finally block re-arms.
        ok2 = wdg.arm(
            reason="post_asyncio_teardown", deadline_s=0.2,
        )
        assert ok2 is True
        assert wdg.reason == "post_asyncio_teardown"
        import time as _t
        _t.sleep(0.5)
        assert wdg.fired is True
    finally:
        wdg.stop()


# ---------------------------------------------------------------------------
# Pin: env override flows through default_deadline_s
# ---------------------------------------------------------------------------


def test_default_deadline_s_env_override(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S", "15.0",
    )
    assert default_deadline_s() == 15.0


def test_default_deadline_s_default():
    if "JARVIS_BATTLE_SHUTDOWN_DEADLINE_S" in os.environ:
        pytest.skip("env override active")
    assert default_deadline_s() == 30.0
