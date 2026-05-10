"""§Layer 7 closure (v2.92) — WallClockWatchdog dual-clock authority.

Closes the cadence-arc Layer 7 root cause diagnosed 2026-05-10
during the bt-2026-05-10-093428 soak:

* The watchdog used ``time.monotonic()`` exclusively as its clock
  authority.
* On macOS, ``time.monotonic()`` is backed by
  ``mach_absolute_time()``, which **pauses while the CPU is
  halted** (host sleep/suspend).
* Soak #2 ran 11h instead of its 40-min cap because the laptop
  slept ~10.5h. Monotonic advanced 1892s; wall-clock advanced
  39838s. Cap (2400s monotonic) was never reached.

The structural fix composes BOTH ``time.monotonic()`` (NTP-
rollback safe lower bound) AND ``time.time()`` (sleep/suspend
authoritative upper bound), taking the max as effective elapsed.

* Wall jumps backward (NTP rollback) → wall < monotonic → max
  picks monotonic; cap still fires on real elapsed.
* Wall jumps forward (NTP step) → wall > monotonic → max picks
  wall; cap fires earlier than intended (acceptable for soak
  semantics — operator wanted "kill after N seconds of real
  time").
* Host sleep → monotonic pauses, wall advances → max picks
  wall; cap fires correctly on wake (or earlier if wall already
  exceeded cap during sleep).

This test file pins the load-bearing STRUCTURAL invariants via
AST. The dual-clock semantic itself (max() composition) is
syntactically enforced by the source — a drift to wall-only OR
monotonic-only is caught by the AST pin sweep below. A naive
functional test that mocks the clocks would interfere with
``asyncio.sleep``'s use of ``time.monotonic`` and hang the event
loop, so structural pinning is the cleanest defense here.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    register_shipped_invariants,
)


_HARNESS_SRC = Path(
    inspect.getfile(BattleTestHarness),
).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AST pins — dual-clock authority structurally enforced
# ---------------------------------------------------------------------------


def test_ast_pin_monitor_wall_clock_calls_time_monotonic():
    """The asyncio monitor MUST call ``time.monotonic()`` — the
    NTP-rollback-safe lower bound of effective elapsed. Drift
    here regresses to wall-only enforcement, which is vulnerable
    to NTP rollback."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_monitor_wall_clock"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "time.monotonic()" in src, (
        "_monitor_wall_clock MUST call time.monotonic() — Layer 7 "
        "dual-clock authority requires monotonic as the NTP-"
        "rollback-safe floor"
    )


def test_ast_pin_monitor_wall_clock_calls_time_time():
    """The asyncio monitor MUST call ``time.time()`` — the wall-
    clock authority that catches host sleep/suspend gaps. This
    pin is the load-bearing structural defense against the
    bt-2026-05-10-093428 regression class."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_monitor_wall_clock"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "time.time()" in src, (
        "_monitor_wall_clock MUST call time.time() — Layer 7 "
        "dual-clock authority requires wall-clock to catch host "
        "sleep/suspend gaps that pause monotonic on macOS"
    )


def test_ast_pin_monitor_wall_clock_takes_max_of_clocks():
    """The asyncio monitor MUST compose both clocks via ``max()``
    — drift to ``min()`` or single-clock use silently violates
    the dual-clock contract."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_monitor_wall_clock"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    # Effective elapsed MUST be computed as max(monotonic, wall).
    assert (
        "max(elapsed_monotonic, elapsed_wall)" in src
        or "effective_elapsed = max(" in src
    ), (
        "_monitor_wall_clock MUST compute effective_elapsed via "
        "max() — Layer 7 dual-clock authority requires firing on "
        "whichever clock ticks fastest"
    )


def test_ast_pin_hard_deadline_thread_dual_clock():
    """The hard-deadline thread MUST anchor BOTH clocks at
    construction. Drift here means the safety-net path also
    fails under sleep — silent regression of the same class."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_start_wall_clock_hard_deadline_thread"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "anchor_monotonic" in src, (
        "hard-deadline thread MUST anchor on monotonic clock"
    )
    assert "anchor_wall" in src, (
        "hard-deadline thread MUST anchor on wall clock — Layer 7 "
        "(v2.92) requires dual-clock authority"
    )
    assert "deadline_monotonic" in src, (
        "hard-deadline thread MUST compute deadline_monotonic"
    )
    assert "deadline_wall" in src, (
        "hard-deadline thread MUST compute deadline_wall — "
        "Layer 7 requires both deadlines + min() over them so "
        "whichever clock advances faster wins"
    )


def test_ast_pin_hard_deadline_thread_takes_min_of_remainings():
    """The hard-deadline thread's `remaining` MUST be the min of
    monotonic-remaining and wall-remaining — whichever expires
    sooner triggers the cap."""
    tree = ast.parse(_HARNESS_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "_start_wall_clock_hard_deadline_thread"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_HARNESS_SRC, fn)
    assert src is not None
    assert "min(remaining_monotonic, remaining_wall)" in src, (
        "hard-deadline thread's `remaining` MUST be "
        "min(remaining_monotonic, remaining_wall) — whichever "
        "clock approaches the deadline first wins"
    )


def test_ast_pin_skew_threshold_env_knob_referenced():
    """The skew-warn threshold env knob MUST be referenced in
    source. Without it, the operator has no way to tune skew
    sensitivity for unusual NTP environments."""
    assert "JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S" in _HARNESS_SRC, (
        "JARVIS_WALL_CLOCK_SKEW_WARN_THRESHOLD_S env knob MUST be "
        "referenced — Layer 7 dual-clock authority needs an "
        "operator-tunable skew threshold"
    )


def test_register_shipped_invariants_validator_green():
    """The canonical AST validator MUST pass on the current
    harness source. Drift here is the operator-visible signal
    that Layer 7 is regressing."""
    invs = register_shipped_invariants()
    assert len(invs) >= 1
    inv = invs[0]
    tree = ast.parse(_HARNESS_SRC)
    violations = inv.validate(tree, _HARNESS_SRC)
    assert violations == (), (
        f"Layer 7 invariants must hold; violations: {violations}"
    )


# ---------------------------------------------------------------------------
# Provenance pins — design-doc discoverability
# ---------------------------------------------------------------------------


def test_layer7_block_cites_v2_92_in_source():
    """The Layer 7 fix MUST cite v2.92 in source so future
    readers can find the design doc + memory note."""
    assert "v2.92" in _HARNESS_SRC, (
        "Layer 7 source MUST cite v2.92 for discoverability"
    )


def test_layer7_block_cites_root_cause_session_in_source():
    """The Layer 7 fix MUST cite the diagnosing session
    bt-2026-05-10-093428 so future readers can trace the
    regression-producing observation."""
    assert "bt-2026-05-10-093428" in _HARNESS_SRC, (
        "Layer 7 source MUST cite bt-2026-05-10-093428 — the "
        "session that exposed monotonic-only enforcement under "
        "host sleep"
    )


def test_layer7_block_cites_mach_absolute_time_explanation():
    """The Layer 7 fix MUST explain WHY monotonic pauses on macOS
    so future readers understand the OS-level mechanism."""
    assert "mach_absolute_time" in _HARNESS_SRC, (
        "Layer 7 source MUST cite mach_absolute_time() — the "
        "macOS-specific reason monotonic pauses during sleep"
    )
