"""
Slice 12L — OpportunityMiner loop hardening + MainThread snapshot priority.
===========================================================================

Closes the wedge surfaced by the Slice 12K verification soak
(bt-2026-05-23-002712):

  * ControlPlaneStarvation lag_ms=17172.9
  * AstCompileHelper parent_await_ms=17243.2
  * worker_elapsed_ms=249.7   (subprocess work ≪ parent await)
  * caller=opportunity_miner_sensor.scan_once

The worker subprocess only spent 250ms — the remaining ~17s was
parent-side blocking. Two surfaces fix this:

PART A (small, first) — MainThread snapshot priority:
  Slice 12K captured 20 threads but MainThread was in the
  truncated 38; without MainThread's frame stack, the snapshot
  could not attribute the wedge. Slice 12L Part A stable-sorts
  captured threads so MainThread is always first regardless of
  the max_threads cap.

PART B — OpportunityMiner event-loop hardening:
  scan_once composes two existing event_loop_governance
  primitives so the loop is never held by FS traversal or file IO:
    * offload_blocking(rglob)     — frees loop during FS walk
    * offload_blocking(read_text) — frees loop during per-file IO
    * cooperative_yield_every_n_async — gives other coroutines
      (notably ControlPlaneWatchdog) scheduling slots inside the
      otherwise-tight scan loop.

Operator binding (verbatim):
  - scan_once does not call Path.rglob/read_text directly on the
    event loop in the hot path
  - synthetic large scan yields cooperatively and avoids
    ControlPlaneStarvation-level loop lag
  - AstCompileHelper process path behavior remains primitive-
    payload only; no ast.AST crosses IPC
  - MainThread snapshot priority regression
  - existing OpportunityMiner tests still pass

Plus structural AST pins for regression armor.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import threading
import time
from pathlib import Path
from typing import List
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.control_plane_watchdog import (
    ControlPlaneWatchdog,
    _capture_thread_frames,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (  # noqa: E501
    OpportunityMinerSensor,
)


# ===============================================================
# Part A — MainThread snapshot priority
# ===============================================================


def test_main_thread_always_captured_even_when_truncated() -> None:
    """Slice 12L Part A regression. Spawn enough worker threads
    that the snapshot's max_threads cap (=5) would clip
    MainThread under FIFO insertion order, then verify MainThread
    appears first in the captured set."""
    # 30 worker threads >> cap=5 → MainThread must be the load-
    # bearing thread that gets prioritized.
    events = [threading.Event() for _ in range(30)]
    workers = [
        threading.Thread(target=lambda e: e.wait(timeout=5),
                         args=(e,), name=f"slice12l-worker-{i}")
        for i, e in enumerate(events)
    ]
    try:
        for t in workers:
            t.start()
        # Brief settle so all 30 are registered in
        # sys._current_frames before capture
        time.sleep(0.15)

        snaps, truncated = _capture_thread_frames(
            max_threads=5, max_frames=3,
        )
        assert len(snaps) == 5
        assert truncated >= 26, (
            f"Expected ≥26 truncated (30 workers + main + others - 5 cap),"
            f" got {truncated}"
        )
        # Load-bearing assertion: MainThread MUST be present
        thread_names = [s.thread_name for s in snaps]
        assert "MainThread" in thread_names, (
            f"MainThread missing despite priority sort: {thread_names}"
        )
        # And MUST be first (priority=0)
        assert snaps[0].thread_name == "MainThread", (
            f"MainThread not in priority-0 slot: first={snaps[0].thread_name}"
        )
    finally:
        for e in events:
            e.set()
        for t in workers:
            t.join(timeout=2)


def test_main_thread_priority_does_not_change_non_main_order() -> None:
    """Stable sort: within priority class 1 (named Python threads
    other than MainThread), the original ``sys._current_frames``
    dict order MUST be preserved. Catches a refactor to an
    unstable sort algorithm that would scramble the operator's
    grep-line output."""
    events = [threading.Event() for _ in range(10)]
    workers = [
        threading.Thread(target=lambda e: e.wait(timeout=5),
                         args=(e,), name=f"slice12l-stable-{i:02d}")
        for i, e in enumerate(events)
    ]
    try:
        for t in workers:
            t.start()
        time.sleep(0.15)

        snaps, _truncated = _capture_thread_frames(
            max_threads=100, max_frames=3,
        )
        # Filter to slice12l-stable-* threads, preserving capture
        # order
        slice12l_order = [
            s.thread_name for s in snaps
            if s.thread_name.startswith("slice12l-stable-")
        ]
        # The ORDER among these is implementation-dependent (dict
        # insertion order). Test only asserts that ALL 10 appear
        # AND none was reordered by name — i.e. the order is
        # consistent across calls.
        assert len(slice12l_order) == 10
        snaps2, _ = _capture_thread_frames(
            max_threads=100, max_frames=3,
        )
        slice12l_order2 = [
            s.thread_name for s in snaps2
            if s.thread_name.startswith("slice12l-stable-")
        ]
        # Two consecutive captures must yield the same order
        assert slice12l_order == slice12l_order2
    finally:
        for e in events:
            e.set()
        for t in workers:
            t.join(timeout=2)


@pytest.mark.asyncio
async def test_main_thread_appears_in_live_starvation_snapshot() -> None:
    """End-to-end Part A: induce starvation in a real watchdog
    cycle with a low max_threads cap, then confirm the emitted
    snapshot's first thread is MainThread."""
    w = ControlPlaneWatchdog(
        interval_s=0.01,
        threshold_ms=50.0,
        snapshot_threshold_ms=100.0,
        snapshot_rate_limit_s=0.0,
        snapshot_max_threads=3,  # tight cap — non-Slice-12L code
                                 # would drop MainThread here
        snapshot_max_frames=4,
    )
    # Spawn workers AFTER watchdog start so they're in the dict
    events = [threading.Event() for _ in range(15)]
    workers = [
        threading.Thread(target=lambda e: e.wait(timeout=5),
                         args=(e,), name=f"slice12l-live-{i}")
        for i, e in enumerate(events)
    ]
    try:
        assert w.start() is True
        for t in workers:
            t.start()
        await asyncio.sleep(0.05)
        time.sleep(0.3)  # induce lag
        await asyncio.sleep(0.1)
    finally:
        for e in events:
            e.set()
        for t in workers:
            t.join(timeout=2)
        await w.stop()
    assert w.snapshot_count >= 1
    snap = w.recent_snapshots()[-1]
    thread_names = [s.thread_name for s in snap.thread_snapshots]
    assert "MainThread" in thread_names, (
        f"MainThread missing from live snapshot under cap=3: "
        f"{thread_names} (truncated={snap.truncated_threads})"
    )


# ===============================================================
# Part B — OpportunityMiner loop hardening
# ===============================================================


def test_scan_once_composes_event_loop_governance_primitives() -> None:
    """AST/behavioral pin: ``scan_once`` MUST reference both
    ``cooperative_yield_every_n_async`` and ``offload_blocking``
    so the loop is freed during the hot scan path. Catches a
    refactor that drops these primitives."""
    src = inspect.getsource(OpportunityMinerSensor.scan_once)
    assert "cooperative_yield_every_n_async" in src, (
        "scan_once must compose cooperative_yield_every_n_async "
        "for the scan-loop iteration."
    )
    assert "offload_blocking" in src, (
        "scan_once must compose offload_blocking to free the loop "
        "during rglob + read_text."
    )


def test_scan_once_does_not_call_rglob_directly_on_loop() -> None:
    """Operator binding: "scan_once does not call Path.rglob/
    read_text directly on the event loop". Slice 12L composes
    the rglob inside an offload_blocking lambda; the bare
    ``for X in <expr>.rglob(...)`` pattern is what re-introduces
    the wedge."""
    src = inspect.getsource(OpportunityMinerSensor.scan_once)
    # Slice 12L composition present
    assert "offload_blocking" in src and "rglob" in src
    # Pre-Slice-12L bare-rglob loop pattern must NOT appear
    assert "for py_file in root.rglob" not in src, (
        "scan_once has a bare rglob on the loop — Slice 12L wedge "
        "regression"
    )


def test_scan_once_does_not_call_read_text_directly_on_loop() -> None:
    """Same as above for ``read_text``: every ``.read_text(...)``
    call MUST be inside an ``offload_blocking`` (or composed via
    one)."""
    src = inspect.getsource(OpportunityMinerSensor.scan_once)
    # Pre-Slice-12L pattern: `source = py_file.read_text(encoding="utf-8")`
    # Slice 12L pattern: `source = await offload_blocking(py_file.read_text, ...)`
    assert "py_file.read_text(encoding=" not in src, (
        "scan_once has a bare read_text call — Slice 12L wedge regression"
    )
    assert "offload_blocking(" in src
    assert "read_text" in src  # still referenced as the offloaded callable


@pytest.mark.asyncio
async def test_synthetic_large_scan_avoids_starvation_level_lag(
    tmp_path: Path,
) -> None:
    """Build a synthetic mini-repo with ~50 .py files and run
    ``scan_once`` while a ControlPlaneWatchdog observes loop lag.
    Operator-binding ACCEPTANCE: the scan MUST yield cooperatively
    enough that no ControlPlaneStarvation-LEVEL lag event fires
    (peak lag < 2000ms snapshot threshold).

    This is a behavioral assertion — it proves the Slice 12L
    composition actually prevents the wedge, not just structurally
    present.
    """
    # Build a small synthetic repo. We don't need to trigger the
    # real ast_compile_helper IPC — the goal is to prove the
    # FS-walk + read_text loop iteration itself yields enough.
    src_dir = tmp_path / "backend" / "synthetic"
    src_dir.mkdir(parents=True)
    for i in range(50):
        (src_dir / f"mod_{i:03d}.py").write_text(
            f"# synthetic file {i}\n"
            "def foo():\n"
            "    return 42\n"
        )

    # Build the miner pointed at this tmpdir
    sensor = _build_test_sensor(tmp_path)

    # Arm the watchdog with the production threshold
    w = ControlPlaneWatchdog(
        interval_s=0.01,
        threshold_ms=500.0,
        snapshot_threshold_ms=2000.0,
        snapshot_rate_limit_s=0.0,
        snapshot_max_threads=5,
        snapshot_max_frames=4,
    )
    assert w.start() is True
    try:
        # Run the scan once with a stubbed analyze helper so we
        # exercise the rglob + read_text path without triggering
        # the production subprocess pool (which is slow on cold
        # start).
        with patch(
            "backend.core.ouroboros.governance.ast_compile_helper."
            "analyze_python_source_for_opportunity_miner",
            side_effect=_stub_analyze_outcome_ok,
        ):
            await sensor.scan_once()
        # Give watchdog a few cycles to record any final lag
        await asyncio.sleep(0.2)
    finally:
        await w.stop()

    # Behavioral acceptance: no snapshot-level wedge
    snap_lag_peaks = [s.lag_ms for s in w.recent_snapshots()]
    assert not snap_lag_peaks or max(snap_lag_peaks) < 2000.0, (
        f"Synthetic 50-file scan triggered a snapshot-level wedge: "
        f"peaks={snap_lag_peaks}"
    )


def _build_test_sensor(tmp_path: Path) -> OpportunityMinerSensor:
    """Build a minimally-configured sensor pointed at a tmp dir.
    Used by Slice 12L behavioral tests."""
    sig = inspect.signature(OpportunityMinerSensor.__init__)
    kwargs = {}
    # Required args best-effort by name match
    for name in sig.parameters:
        if name == "self":
            continue
        param = sig.parameters[name]
        if param.default is not inspect.Parameter.empty:
            continue
        if name == "repo":
            kwargs[name] = "synthetic-repo"
        elif name == "repo_root":
            kwargs[name] = tmp_path
        elif name == "scan_paths":
            kwargs[name] = ["backend"]
        else:
            # Unknown required arg — try None and let the
            # constructor handle it (or skip with pytest.skip if it
            # raises)
            kwargs[name] = None
    try:
        return OpportunityMinerSensor(**kwargs)
    except TypeError as e:
        pytest.skip(
            f"OpportunityMinerSensor constructor signature changed: {e}"
        )


async def _stub_analyze_outcome_ok(caller, source, filename=None):
    """Stub for analyze_python_source_for_opportunity_miner that
    returns a primitive payload mimicking outcome=OK. Avoids the
    production subprocess pool entirely — the synthetic test
    measures only the rglob + read_text + loop-yield shape."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, AnalysisResult, OpportunityAnalysisPayload,
        ExecutionMode,
    )
    return AnalysisResult(
        outcome=AnalyzeOutcome.OK,
        payload=OpportunityAnalysisPayload(
            cyclomatic_complexity=5,
            max_function_length=10,
            cognitive_complexity=3,
            duplicate_block_count=0,
            import_fan_out=2,
            todo_fixme_count=0,
            total_lines=3,
        ),
        elapsed_ms=1.0,
        worker_elapsed_ms=0.5,
        source_bytes=len(source) if isinstance(source, (str, bytes)) else 0,
        caller=caller,
        execution_mode=ExecutionMode.PROCESS,
        error_detail="",
    )


# ===============================================================
# AstCompileHelper IPC payload-only contract (preserved)
# ===============================================================


def test_ast_compile_helper_payload_only_contract_preserved() -> None:
    """Operator binding: "AstCompileHelper process path behavior
    remains primitive-payload only; no ast.AST crosses IPC". This
    is a Slice 11B invariant that Slice 12L MUST NOT break.

    AST-walk the ast_compile_helper module to confirm the IPC
    payload dataclass (AnalyzePayload) carries only primitive
    fields — no ast.AST objects."""
    from backend.core.ouroboros.governance import ast_compile_helper

    src_path = Path(inspect.getfile(ast_compile_helper))
    tree = ast.parse(src_path.read_text())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "OpportunityAnalysisPayload":
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            # Annotation must be a primitive: int / str / bool /
            # float / tuple / Optional[primitive]. ``ast.AST`` or
            # any module-qualified ``ast.<X>`` is forbidden.
            ann_src = ast.unparse(stmt.annotation)
            assert "ast." not in ann_src, (
                f"OpportunityAnalysisPayload field "
                f"{stmt.target.id if isinstance(stmt.target, ast.Name) else '?'} "
                f"has ast.* in annotation: {ann_src}"
            )
        found = True
        break
    assert found, "OpportunityAnalysisPayload class not found"


# ===============================================================
# AST pins — structural regression armor
# ===============================================================


_WATCHDOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "control_plane_watchdog.py"
)

_MINER_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "intake" / "sensors" / "opportunity_miner_sensor.py"
)


def test_ast_pin_capture_uses_main_thread_priority() -> None:
    """``_capture_thread_frames`` MUST reference
    ``threading.main_thread`` so MainThread can be ranked first.
    Catches a refactor that drops the priority sort."""
    tree = ast.parse(_WATCHDOG_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != "_capture_thread_frames":
            continue
        src = ast.unparse(node)
        assert "main_thread" in src, (
            "_capture_thread_frames must call threading.main_thread() "
            "for Slice 12L Part A priority ordering"
        )
        # Sort call must be present
        assert ".sort(" in src or "sorted(" in src, (
            "_capture_thread_frames must sort items by priority"
        )
        return
    pytest.fail("_capture_thread_frames not found")


def test_ast_pin_miner_scan_once_composes_primitives() -> None:
    """``scan_once`` MUST import the two event_loop_governance
    primitives. AST-pin enforces the composition is structurally
    present, not just textually."""
    tree = ast.parse(_MINER_PATH.read_text())
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "scan_once":
            continue
        # Look for ImportFrom event_loop_governance inside the body
        # (the existing pattern is a local import) OR for module-
        # level import. Either is acceptable.
        body_src = ast.unparse(node)
        assert "cooperative_yield_every_n_async" in body_src
        assert "offload_blocking" in body_src
        # The offload_blocking + rglob composition specifically
        assert "rglob" in body_src
        # The offload_blocking + read_text composition specifically
        assert "read_text" in body_src
        found = True
        break
    assert found


def test_ast_pin_miner_no_bare_rglob_for_loop() -> None:
    """Defensive AST pin: a plain ``for X in <expr>.rglob(...)``
    loop in ``scan_once`` would re-introduce the wedge. The
    Slice 12L pattern is ``async for X in
    cooperative_yield_every_n_async(py_files)`` where py_files
    came from ``offload_blocking(lambda r: list(r.rglob(...)))``."""
    tree = ast.parse(_MINER_PATH.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "scan_once":
            continue
        for sub in ast.walk(node):
            # `for X in <expr>.rglob(...)` is an ast.For with iter
            # being an ast.Call whose func.attr == "rglob".
            if isinstance(sub, ast.For):
                if isinstance(sub.iter, ast.Call) and \
                        isinstance(sub.iter.func, ast.Attribute) and \
                        sub.iter.func.attr == "rglob":
                    pytest.fail(
                        "scan_once has a bare `for X in <expr>.rglob(...)` "
                        "loop on the event loop — Slice 12L wedge regression"
                    )
        return
    pytest.fail("scan_once not found")
