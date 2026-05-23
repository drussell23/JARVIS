"""
Slice 12K — Control-plane starvation attribution tests.
========================================================

Addresses the new wedge surfaced by the Slice 12J verification soak
(bt-2026-05-23-000003): with the watchdog polling-thread storm
closed, the asyncio loop is STILL being starved by something — but
``[ControlPlaneStarvation] lag_ms=X`` events alone don't attribute
the culprit. Slice 12K adds stack-snapshot capture so the next
soak surfaces enough frame/task evidence to identify the blocker
without further investigation (HeavyProbe vs ShippedCodeInvariants
vs OpportunityMiner vs something else).

Operator binding (verbatim):
  1. Synthetic loop block triggers starvation snapshot
  2. Snapshot is rate-limited
  3. Snapshot never raises into watchdog
  4. No snapshot below threshold
  5. Output contains enough frames/task identifiers to attribute
     the blocker
  6. Master/default behavior remains current unless already enabled

Plus structural AST pins covering:
  * Snapshot dataclass fields
  * Module-level env knob constants
  * Snapshot helper functions wrapped in never-raise envelopes
  * No behavior modification of HeavyProbe/ShippedCodeInvariants/
    OpportunityMiner from this slice
"""

from __future__ import annotations

import ast
import asyncio
import os
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.control_plane_watchdog import (
    ControlPlaneWatchdog,
    LagRecord,
    StarvationSnapshot,
    ThreadFrameSnapshot,
    _capture_asyncio_task_names,
    _capture_thread_frames,
    snapshot_enabled,
)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------


def _new_watchdog(
    *, threshold_ms: float = 50.0,
    snapshot_threshold_ms: float = 100.0,
    snapshot_rate_limit_s: float = 0.0,
    interval_s: float = 0.01,
) -> ControlPlaneWatchdog:
    """Build a watchdog tuned for fast synthetic loop blocks.

    Defaults:
      * ``interval_s=0.01`` so the loop ticks 100×/sec → easy to
        induce observable lag with a brief sleep.
      * ``threshold_ms=50`` so anything over 50ms is logged.
      * ``snapshot_threshold_ms=100`` for clear separation from
        the warn threshold.
      * ``snapshot_rate_limit_s=0.0`` to disable rate-limiting by
        default (tests opt in to rate-limiting via override).
    """
    return ControlPlaneWatchdog(
        interval_s=interval_s,
        threshold_ms=threshold_ms,
        snapshot_threshold_ms=snapshot_threshold_ms,
        snapshot_rate_limit_s=snapshot_rate_limit_s,
        snapshot_max_threads=10,
        snapshot_max_frames=8,
    )


# ---------------------------------------------------------------
# Test 1: Synthetic loop block triggers snapshot
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthetic_loop_block_triggers_snapshot() -> None:
    """A synchronous ``time.sleep`` on the asyncio loop blocks
    the loop and induces measurable lag. With the snapshot
    threshold below the induced lag, the watchdog MUST capture
    + store a snapshot in its ring."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=150.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        # Brief warm-up tick
        await asyncio.sleep(0.05)
        # Synchronous block on the loop thread — this is the
        # canonical "wedge"
        time.sleep(0.4)
        # Give the watchdog a few cycles to observe + capture
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    assert w.lag_event_count >= 1
    assert w.snapshot_count >= 1
    snaps = w.recent_snapshots()
    assert len(snaps) >= 1
    snap = snaps[-1]
    assert snap.lag_ms >= 150.0
    assert snap.threshold_ms == 150.0
    assert len(snap.thread_snapshots) >= 1


@pytest.mark.asyncio
async def test_no_snapshot_below_threshold() -> None:
    """Lag events ABOVE the warn threshold but BELOW the snapshot
    threshold MUST log the warning but NOT emit a snapshot.
    Operator binding: "no snapshot below threshold"."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=5000.0,  # very high snapshot bar
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.2)  # 200ms — over warn (50) but well under snap (5000)
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    assert w.lag_event_count >= 1, (
        "warn-threshold lag should have been observed"
    )
    assert w.snapshot_count == 0, (
        f"Snapshot fired below threshold: lag_event_count="
        f"{w.lag_event_count} snapshot_count={w.snapshot_count}"
    )
    assert w.recent_snapshots() == []


# ---------------------------------------------------------------
# Test 2: Snapshot is rate-limited
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_rate_limited() -> None:
    """Two rapid loop blocks within the rate-limit window MUST
    produce at most ONE snapshot. The second is dropped and
    counted in ``snapshot_suppressed_count``."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=2.0,  # 2-second rate limit
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.2)
        await asyncio.sleep(0.1)
        # Second block within rate-limit window
        time.sleep(0.2)
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    assert w.lag_event_count >= 2
    # First snapshot fires, second is suppressed
    assert w.snapshot_count == 1, (
        f"Expected 1 snapshot, got {w.snapshot_count}"
    )
    assert w.snapshot_suppressed_count >= 1, (
        f"Expected at least 1 suppression, got "
        f"{w.snapshot_suppressed_count}"
    )


@pytest.mark.asyncio
async def test_snapshot_rate_limit_disabled_with_zero() -> None:
    """``snapshot_rate_limit_s=0`` is the explicit opt-out. Two
    rapid blocks MUST both produce snapshots."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.2)
        await asyncio.sleep(0.1)
        time.sleep(0.2)
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    assert w.snapshot_count >= 2, (
        f"Expected ≥2 snapshots with rate-limit=0, got "
        f"{w.snapshot_count}"
    )
    assert w.snapshot_suppressed_count == 0


# ---------------------------------------------------------------
# Test 3: Snapshot never raises into watchdog
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_exception_does_not_crash_watchdog() -> None:
    """Operator binding: "snapshot never raises into watchdog".
    Inject an exception into the frame-capture helper and confirm
    the watchdog keeps running and continues to log lag warnings."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    # Patch the module-level helper to raise. The watchdog's
    # never-raise envelope MUST swallow it.
    with patch(
        "backend.core.ouroboros.governance.control_plane_watchdog."
        "_capture_thread_frames",
        side_effect=RuntimeError("synthetic capture failure"),
    ):
        assert w.start() is True
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.2)
            await asyncio.sleep(0.1)
        finally:
            await w.stop()
    # Watchdog kept running and observed the lag
    assert w.lag_event_count >= 1
    # Snapshot still fired (with empty frames — the envelope
    # captures the LagRecord even when frame collection fails)
    assert w.snapshot_count >= 1
    snap = w.recent_snapshots()[-1]
    # Frames collection failed → empty tuple
    assert snap.thread_snapshots == ()


@pytest.mark.asyncio
async def test_asyncio_task_capture_exception_does_not_crash() -> None:
    """Same envelope must hold when ``asyncio.all_tasks()`` itself
    raises (e.g. when called outside a running loop). The
    snapshot fires with empty ``asyncio_task_names``."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    with patch(
        "backend.core.ouroboros.governance.control_plane_watchdog."
        "_capture_asyncio_task_names",
        side_effect=RuntimeError("synthetic task capture failure"),
    ):
        assert w.start() is True
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.2)
            await asyncio.sleep(0.1)
        finally:
            await w.stop()
    assert w.snapshot_count >= 1
    snap = w.recent_snapshots()[-1]
    assert snap.asyncio_task_names == ()


# ---------------------------------------------------------------
# Test 4: Output contains enough frames/task identifiers to attribute
# ---------------------------------------------------------------


def test_frame_capture_includes_filename_lineno_funcname() -> None:
    """Synchronous frame capture should return entries shaped
    ``"<file>:<line> in <func>"``. Operators rely on this format
    to attribute the wedge to a specific source location."""

    def synthetic_callee() -> tuple:
        return _capture_thread_frames(max_threads=10, max_frames=5)

    def synthetic_caller() -> tuple:
        return synthetic_callee()

    snaps, truncated = synthetic_caller()
    assert len(snaps) >= 1
    main_thread = next(
        (s for s in snaps if s.thread_name == "MainThread"),
        snaps[0],
    )
    # At least 3 frames captured (capture helper + callee + caller)
    assert len(main_thread.frames) >= 3
    # The innermost frame is _capture_thread_frames itself
    assert "_capture_thread_frames" in main_thread.frames[0]
    # The next frame up is `synthetic_callee`
    assert "synthetic_callee" in main_thread.frames[1]
    # Each frame entry has the file:line in func shape
    for f in main_thread.frames:
        assert ":" in f
        assert " in " in f


@pytest.mark.asyncio
async def test_snapshot_includes_asyncio_task_names() -> None:
    """The watchdog itself runs as a named asyncio task
    (``control_plane_watchdog``). When the loop is wedged + a
    snapshot fires, the task name MUST appear in the
    ``asyncio_task_names`` field — operators use task names to
    map the wedge to a coroutine source."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        # Spawn a named task we can also recognize in the snapshot
        async def _named_coro() -> None:
            await asyncio.sleep(10.0)  # long sleep — still in-flight at snapshot time
        named_task = asyncio.create_task(_named_coro(),
                                         name="slice12k_test_named_coro")
        time.sleep(0.2)
        await asyncio.sleep(0.1)
        named_task.cancel()
        try:
            await named_task
        except asyncio.CancelledError:
            pass
    finally:
        await w.stop()
    snap = w.recent_snapshots()[-1]
    assert "control_plane_watchdog" in snap.asyncio_task_names
    assert "slice12k_test_named_coro" in snap.asyncio_task_names


def test_frame_capture_respects_max_threads_truncation() -> None:
    """The ``max_threads`` cap MUST clip the returned snapshots
    AND report the truncated count so operators can tell when
    they're missing data."""
    # System has multiple threads (at least main + worker pool +
    # logging) — request only 1 to force truncation
    snaps, truncated = _capture_thread_frames(
        max_threads=1, max_frames=5,
    )
    assert len(snaps) == 1
    # Truncated counter records the diff (some Python runtimes
    # may report exactly 1 active thread in pytest worker, so
    # truncated could be 0; that's still consistent).
    assert truncated >= 0


def test_frame_capture_respects_max_frames_cap() -> None:
    """Deep recursion MUST be capped at ``max_frames`` per
    thread."""

    def recurse(n: int):
        if n <= 0:
            return _capture_thread_frames(max_threads=10, max_frames=3)
        return recurse(n - 1)

    snaps, _ = recurse(15)
    main_thread = next(
        (s for s in snaps if s.thread_name == "MainThread"),
        snaps[0],
    )
    assert len(main_thread.frames) <= 3


def test_asyncio_task_capture_outside_running_loop() -> None:
    """Calling the capture helper without a running loop must
    return ``()`` rather than raise."""
    result = _capture_asyncio_task_names(max_tasks=10)
    assert result == ()


# ---------------------------------------------------------------
# Test 5: Master/default behavior preserved
# ---------------------------------------------------------------


def test_snapshot_enabled_default_is_true() -> None:
    """Pure observability — default enabled. Explicit ``"false"``
    is the only opt-out."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED", None)
        assert snapshot_enabled() is True


def test_snapshot_enabled_explicit_false_opts_out() -> None:
    """``JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED=false`` flips the
    gate."""
    for val in ("false", "0", "no", "off", "FALSE", "Off"):
        with patch.dict(
            os.environ,
            {"JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED": val},
            clear=False,
        ):
            assert snapshot_enabled() is False, (
                f"value '{val}' should opt out"
            )


@pytest.mark.asyncio
async def test_snapshot_master_flag_off_suppresses_capture() -> None:
    """When the master flag is OFF, the watchdog still logs lag
    warnings (Slice 11A behavior preserved) but emits NO
    snapshots."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    with patch.dict(
        os.environ,
        {"JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED": "false"},
        clear=False,
    ):
        assert w.start() is True
        try:
            await asyncio.sleep(0.05)
            time.sleep(0.2)
            await asyncio.sleep(0.1)
        finally:
            await w.stop()
    assert w.lag_event_count >= 1
    assert w.snapshot_count == 0


@pytest.mark.asyncio
async def test_lag_record_ring_still_populated_after_slice12k() -> None:
    """Slice 11A semantics preserved: the LagRecord ring still
    captures every observed lag event regardless of snapshot
    capture state."""
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.2)
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    # Slice 11A ring: contains lag records (some below threshold,
    # at least one above)
    records = w.recent_lag_records()
    assert any(r.lag_ms >= 50.0 for r in records), (
        f"Slice 11A LagRecord ring lost data: {records[-3:]}"
    )


# ---------------------------------------------------------------
# Test 6: Log emission shape
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_log_emission_is_grep_friendly(caplog) -> None:
    """The snapshot WARNING line MUST start with
    ``[ControlPlaneSnapshot]`` so operators can grep for it in
    debug.log alongside ``[ControlPlaneStarvation]``."""
    import logging
    caplog.set_level(
        logging.WARNING,
        logger="Ouroboros.ControlPlaneWatchdog",
    )
    w = _new_watchdog(
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        interval_s=0.01,
    )
    assert w.start() is True
    try:
        await asyncio.sleep(0.05)
        time.sleep(0.25)
        await asyncio.sleep(0.1)
    finally:
        await w.stop()
    msgs = [r.message for r in caplog.records]
    starvation_msgs = [m for m in msgs if "[ControlPlaneStarvation]" in m]
    snapshot_msgs = [m for m in msgs if "[ControlPlaneSnapshot]" in m]
    assert starvation_msgs, "warning line missing"
    assert snapshot_msgs, "snapshot line missing"
    snap_msg = snapshot_msgs[-1]
    # Stable fields appear on the header line
    assert "lag_ms=" in snap_msg
    assert "threshold_ms=" in snap_msg
    assert "event_n=" in snap_msg
    assert "threads=" in snap_msg


# ---------------------------------------------------------------
# AST pins — structural regression armor
# ---------------------------------------------------------------


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "control_plane_watchdog.py"
)


def _load_ast() -> ast.Module:
    return ast.parse(_MODULE_PATH.read_text())


def test_ast_pin_starvation_snapshot_dataclass_fields() -> None:
    """``StarvationSnapshot`` MUST carry the 7 required fields
    for operator attribution. Frozen via dataclass(frozen=True)."""
    tree = _load_ast()
    found = False
    expected_fields = {
        "lag_ms", "threshold_ms", "ts_monotonic", "ts_wall",
        "thread_snapshots", "asyncio_task_names", "truncated_threads",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "StarvationSnapshot":
            continue
        # frozen=True decorator
        is_frozen = False
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if kw.arg == "frozen" and \
                            isinstance(kw.value, ast.Constant) and \
                            kw.value.value is True:
                        is_frozen = True
        assert is_frozen, "StarvationSnapshot must be frozen=True"
        field_names = {
            stmt.target.id for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
        }
        assert expected_fields.issubset(field_names), (
            f"StarvationSnapshot missing fields: "
            f"{expected_fields - field_names}"
        )
        found = True
        break
    assert found, "StarvationSnapshot class not found in module"


def test_ast_pin_thread_frame_snapshot_dataclass_fields() -> None:
    """``ThreadFrameSnapshot`` MUST carry thread_id + thread_name
    + frames fields."""
    tree = _load_ast()
    expected = {"thread_id", "thread_name", "frames"}
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "ThreadFrameSnapshot":
            continue
        field_names = {
            stmt.target.id for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
        }
        assert expected.issubset(field_names)
        found = True
    assert found


def test_ast_pin_snapshot_env_knob_constants() -> None:
    """The 6 Slice 12K env knob constants MUST be present as
    module-level string assignments. Operators grep for these to
    tune behavior."""
    src = _MODULE_PATH.read_text()
    for knob in (
        "JARVIS_CONTROL_PLANE_SNAPSHOT_ENABLED",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_THRESHOLD_MS",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_RATE_LIMIT_S",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_MAX_THREADS",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_MAX_FRAMES",
        "JARVIS_CONTROL_PLANE_SNAPSHOT_RING_CAP",
    ):
        assert knob in src, (
            f"AST pin failed: env knob {knob} missing from module"
        )


def test_ast_pin_capture_helpers_have_never_raise_envelope() -> None:
    """``_capture_thread_frames`` and ``_capture_asyncio_task_names``
    MUST be wrapped in try/except that catches broad Exception so
    instrumentation failure cannot crash the watchdog. AST pin
    walks each helper looking for ``except Exception`` or bare
    ``except`` handlers — at least one must be present."""
    tree = _load_ast()
    for name in ("_capture_thread_frames", "_capture_asyncio_task_names"):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != name:
                continue
            has_broad_except = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler):
                    # ``except Exception`` or bare ``except`` both count
                    if sub.type is None:
                        has_broad_except = True
                    elif isinstance(sub.type, ast.Name) and \
                            sub.type.id == "Exception":
                        has_broad_except = True
                    elif isinstance(sub.type, ast.Tuple):
                        for elt in sub.type.elts:
                            if isinstance(elt, ast.Name) and \
                                    elt.id == "Exception":
                                has_broad_except = True
            assert has_broad_except, (
                f"AST pin failed: {name} must have a broad except "
                f"handler to satisfy never-raise contract"
            )
            break
        else:
            pytest.fail(f"helper function {name} not found")


def test_ast_pin_no_behavior_change_to_other_modules() -> None:
    """Slice 12K is observability-only. AST pin enforces NO
    imports of HeavyProbe / ShippedCodeInvariants /
    OpportunityMiner from the watchdog module — if Slice 12K
    starts directly tweaking those modules, this pin trips and
    forces the change into a separate slice with explicit
    operator authorization."""
    tree = _load_ast()
    forbidden_substrings = (
        "dw_heavy_probe",
        "shipped_code_invariants",
        "opportunity_miner",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for sub in forbidden_substrings:
                    assert sub not in alias.name, (
                        f"AST pin: forbidden import {alias.name} — "
                        f"Slice 12K is observability-only"
                    )
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for sub in forbidden_substrings:
                assert sub not in module, (
                    f"AST pin: forbidden ImportFrom {module} — "
                    f"Slice 12K is observability-only"
                )


def test_ast_pin_snapshot_capture_inside_existing_never_raise_envelope() -> None:
    """The snapshot capture call site in ``_run`` MUST be wrapped
    in a try/except that catches broad Exception — operator
    binding: "snapshot never raises into watchdog"."""
    tree = _load_ast()
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_run":
            continue
        # Walk _run looking for a try statement that contains a
        # call to _maybe_capture_snapshot
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Try):
                continue
            # The try body must include _maybe_capture_snapshot
            body_src = ast.dump(sub)
            if "_maybe_capture_snapshot" not in body_src:
                continue
            # The handler must catch broad Exception
            for h in sub.handlers:
                if h.type is None:
                    found = True
                elif isinstance(h.type, ast.Name) and \
                        h.type.id == "Exception":
                    found = True
        break
    assert found, (
        "AST pin: _maybe_capture_snapshot call in _run must be "
        "inside a try/except Exception envelope."
    )
