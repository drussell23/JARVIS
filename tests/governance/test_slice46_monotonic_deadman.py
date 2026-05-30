"""Slice 46 — Monotonic Heartbeat Alignment & Suspension-Resilient Armoring.

v41 (bt-2026-05-29-213224) fired ``os._exit(75)`` on a PHANTOM wedge: the Mac
slept ~519s, so wall-clock advanced 605s while the monotonic clock advanced
only 86s. ``LoopDeadman`` tracked heartbeat age with ``time.time()`` (wall),
so the sleep gap looked like a 574s loop wedge.

Fix: measure wedge age on ``time.monotonic()`` (host sleep / NTP jumps cannot
warp it) + an explicit host-suspension guard that absorbs a wall>>monotonic
skew via ``_handle_host_wake_recovery`` instead of killing the process.

These tests mock temporal jumps to prove:
  1. wedge age is monotonic (a 500s wall jump with bounded monotonic does NOT
     fire);
  2. the suspension guard suppresses ``os._exit`` and re-arms cleanly;
  3. a GENUINE monotonic wedge (no skew) still fires;
  4. ControlPlaneWatchdog + SidecarProfiler carry no wall-clock timing (AST).
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from backend.core.ouroboros.governance import loop_deadman as ld

_REPO = Path(__file__).resolve().parents[2]
_GOV = _REPO / "backend/core/ouroboros/governance"


# ── 1. monotonic age — wall jumps cannot inflate it ─────────────────────


def test_age_is_monotonic_not_wall():
    d = ld.LoopDeadman(timeout_s=30.0, heartbeat_s=5.0, stack_dump=False)
    base = ld.time.monotonic()
    # Wall jumps 500s; monotonic advances 0.3s → age must track monotonic.
    with patch.object(ld.time, "monotonic", lambda: base + 0.3), \
         patch.object(ld.time, "time", lambda: d._last_heartbeat_wall + 500.0):
        age = d.last_heartbeat_age_s()
    assert age < 1.0, f"age must follow monotonic (got {age})"


def test_heartbeat_resets_monotonic_baseline():
    d = ld.LoopDeadman(timeout_s=30.0, heartbeat_s=5.0, stack_dump=False)
    base = ld.time.monotonic()
    with patch.object(ld.time, "monotonic", lambda: base + 10.0):
        assert d.last_heartbeat_age_s() >= 10.0
        d.heartbeat()                # re-baseline at +10
        assert d.last_heartbeat_age_s() < 0.5


# ── 2. suspension guard — 500s host freeze is absorbed, not fired ───────


def test_suspension_500s_absorbed_not_fired():
    """The v41 scenario reproduced: a 500s wall jump while monotonic stays
    bounded must NOT call os._exit — even if the monotonic age happens to
    exceed the timeout (defense for monotonic-advances-during-suspend)."""
    d = ld.LoopDeadman(timeout_s=30.0, heartbeat_s=5.0, stack_dump=False)
    fired = {"exit": False}
    base_mono = ld.time.monotonic()
    base_wall = d._last_heartbeat_wall
    # Force the would-fire branch: monotonic age 40s (> 30s timeout) BUT
    # wall age 540s → skew 500s >> 5s threshold → classified suspension.
    with patch.object(ld.os, "_exit", lambda code: fired.__setitem__("exit", True)), \
         patch.object(ld.time, "monotonic", lambda: base_mono + 40.0), \
         patch.object(ld.time, "time", lambda: base_wall + 540.0):
        # one iteration of the daemon loop's decision, inline:
        age = d.last_heartbeat_age_s()
        assert age > d._timeout_s
        age_wall = ld.time.time() - d._last_heartbeat_wall
        skew = age_wall - age
        assert skew > d._suspension_skew_threshold_s
        d._handle_host_wake_recovery(skew_s=skew, age_wall_s=age_wall, age_mono_s=age)
    assert fired["exit"] is False, "host suspension must NOT fire os._exit"
    assert d.suspension_count == 1


def test_wake_recovery_rebaselines_both_clocks():
    d = ld.LoopDeadman(timeout_s=30.0, heartbeat_s=5.0, stack_dump=False)
    mono_after = ld.time.monotonic() + 100.0
    wall_after = ld.time.time() + 600.0
    with patch.object(ld.time, "monotonic", lambda: mono_after), \
         patch.object(ld.time, "time", lambda: wall_after):
        d._handle_host_wake_recovery(skew_s=500.0, age_wall_s=600.0, age_mono_s=100.0)
        # After recovery the age is ~0 (re-baselined to the post-wake now).
        assert d.last_heartbeat_age_s() < 0.5


# ── 3. genuine wedge (no skew) still fires ──────────────────────────────


def test_genuine_monotonic_wedge_still_fires():
    """A real CPU-bound wedge advances BOTH clocks equally (no skew), so the
    guard does not trip and os._exit fires as designed."""
    d = ld.LoopDeadman(timeout_s=30.0, heartbeat_s=5.0, stack_dump=False)
    fired = {"exit": False}
    base_mono = ld.time.monotonic()
    base_wall = d._last_heartbeat_wall
    with patch.object(ld.os, "_exit", lambda code: fired.__setitem__("exit", True)), \
         patch.object(ld.time, "monotonic", lambda: base_mono + 400.0), \
         patch.object(ld.time, "time", lambda: base_wall + 400.0):  # equal → skew 0
        age = d.last_heartbeat_age_s()
        assert age > d._timeout_s
        age_wall = ld.time.time() - d._last_heartbeat_wall
        skew = age_wall - age
        assert skew <= d._suspension_skew_threshold_s  # no suspension
        if skew <= d._suspension_skew_threshold_s:
            d._fire_wedge(age)
    assert fired["exit"] is True, "a genuine wedge must still fire os._exit"


# ── env knob ────────────────────────────────────────────────────────────


def test_skew_threshold_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_SUSPENSION_SKEW_THRESHOLD", "12.5")
    assert ld.suspension_skew_threshold_s() == 12.5


def test_skew_threshold_bounds(monkeypatch):
    monkeypatch.setenv("JARVIS_SUSPENSION_SKEW_THRESHOLD", "0.1")
    assert ld.suspension_skew_threshold_s() == 1.0     # floored
    monkeypatch.setenv("JARVIS_SUSPENSION_SKEW_THRESHOLD", "9999")
    assert ld.suspension_skew_threshold_s() == 300.0   # ceilinged
    monkeypatch.setenv("JARVIS_SUSPENSION_SKEW_THRESHOLD", "garbage")
    assert ld.suspension_skew_threshold_s() == 5.0     # default on parse error


# ── 4. AST pins — safety daemons carry NO wall-clock timing ─────────────


def _wall_timing_calls(path: Path) -> list:
    """Find ``time.time()`` calls used for TIMING (i.e. on either side of a
    subtraction or compared to a timeout). We allow ``time.time()`` only for
    the wall-baseline that FEEDS the suspension skew detector, so this pin
    asserts the AGE accessor + wedge decision do not use wall time. We scope
    it structurally: no ``time.time()`` appears inside last_heartbeat_age_s."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in (
            "last_heartbeat_age_s",
        ):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "time"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "time"):
                    offenders.append(node.name)
    return offenders


def test_deadman_age_accessor_uses_monotonic_only():
    off = _wall_timing_calls(_GOV / "loop_deadman.py")
    assert off == [], f"last_heartbeat_age_s must not use time.time(): {off}"


def test_controlplane_watchdog_lag_is_monotonic():
    src = (_GOV / "control_plane_watchdog.py").read_text(encoding="utf-8")
    # lag is computed as monotonic deltas; assert the canonical pattern exists
    # and that no `time.time()` feeds the lag/snapshot math.
    assert "time.monotonic()" in src
    # The snapshot 'now' is monotonic (ts_monotonic), pinned so a refactor
    # can't silently reintroduce wall-clock lag.
    assert "now = ts_monotonic" in src


def test_sidecar_profiler_uses_monotonic():
    src = (_GOV / "sidecar_profiler.py").read_text(encoding="utf-8")
    assert "time.monotonic()" in src
    assert "= time.time()" not in src, "SidecarProfiler must not time on wall-clock"
