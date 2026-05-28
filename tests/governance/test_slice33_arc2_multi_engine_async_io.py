"""Slice 33 Arc 2 — Multi-Engine Asynchronous I/O Offloading Layer.

Closes the v28 (``bt-2026-05-27-235042``) LoopSink-confirmed top
sinks across 3 orthogonal axes:

  Phase 1: posture.signal.commit_ratios 18,140 ms cold-cache git
           (sync subprocess.run holds a ThreadPool slot for the
           full duration).
  Phase 2: posture.signal.postmortem_failure_rate 5,322 ms
           session-dir iteration (4 signals contend with oracle
           file reads in the DEFAULT ThreadPool).
  Phase 3: oracle._index_file.graph_write_bulk 3,580 ms peak
           (76 occurrences total — NetworkX bulk mutations inline
           on the asyncio loop).

# Phase 1 — async-native git subprocess

``SignalCollector._git_subjects_async()`` + ``commit_ratios_async()``
use ``asyncio.create_subprocess_exec`` — no ThreadPool slot consumed
even during cold-cache 18 s scans. Sibling: ``git_momentum.
compute_recent_momentum_async`` mirrors for non-posture callers.
``build_bundle_async`` calls ``commit_ratios_async`` directly (no
``to_thread`` hop).

# Phase 2 — dedicated filesystem executor

Module-level lazy singleton ``_fs_signal_executor`` (2 workers,
``JARVIS_POSTURE_FS_SIGNAL_EXECUTOR_MAX_WORKERS`` configurable).
``build_bundle_async`` routes 4 filesystem-bound signals
(postmortem/iron_gate/l2_repair/session_lessons) through
``loop.run_in_executor(_fs_exec, fn)`` so heavy session-dir scans
don't contend with the default executor's other consumers.

# Phase 3 — async graph-write queue + bg consumer

``TheOracle._graph_write_queue: asyncio.Queue`` (bounded, default
1000) + ``_graph_write_consumer_task`` drains in batches (default
50) and applies via ``asyncio.to_thread`` — NetworkX bulk mutations
move off the asyncio loop. Backpressure via ``put`` await when
queue fills. Master flag ``JARVIS_ORACLE_GRAPH_QUEUE_ENABLED``
default TRUE; explicit-false restores inline writes.

# Test surface (5 AST + 11 spine = 16 tests)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import time
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTURE_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "posture_observer.py"
)
GIT_MOMENTUM_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "git_momentum.py"
)
ORACLE_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "oracle.py"


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 5
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_phase1_git_subjects_async_present() -> None:
    """SignalCollector MUST have ``_git_subjects_async`` + ``commit_ratios_async``
    as async methods using ``asyncio.create_subprocess_exec`` (no
    ``subprocess.run`` inside)."""
    src = POSTURE_FILE.read_text()
    tree = ast.parse(src, filename=str(POSTURE_FILE))
    git_subjects_async_found = False
    commit_ratios_async_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "_git_subjects_async":
                body = ast.unparse(node)
                assert "create_subprocess_exec" in body, (
                    "_git_subjects_async must use create_subprocess_exec"
                )
                # Strip docstring before checking for forbidden tokens
                # — the docstring legitimately mentions subprocess.run
                # for context. AST-walk Call nodes specifically.
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                        if (
                            isinstance(sub.func.value, ast.Name)
                            and sub.func.value.id == "subprocess"
                            and sub.func.attr == "run"
                        ):
                            pytest.fail(
                                "_git_subjects_async makes a sync "
                                "subprocess.run call — must use "
                                "create_subprocess_exec only"
                            )
                git_subjects_async_found = True
            if node.name == "commit_ratios_async":
                body = ast.unparse(node)
                assert "_git_subjects_async" in body, (
                    "commit_ratios_async must call _git_subjects_async"
                )
                commit_ratios_async_found = True
    assert git_subjects_async_found, "_git_subjects_async missing"
    assert commit_ratios_async_found, "commit_ratios_async missing"


def test_ast_pin_phase1_git_momentum_async_present() -> None:
    """``git_momentum.compute_recent_momentum_async`` MUST exist and
    use ``create_subprocess_exec`` + the shared parser."""
    src = GIT_MOMENTUM_FILE.read_text()
    tree = ast.parse(src, filename=str(GIT_MOMENTUM_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "compute_recent_momentum_async"
        ):
            body = ast.unparse(node)
            assert "create_subprocess_exec" in body
            assert "_parse_git_log_output" in body
            found = True
            break
    assert found, "compute_recent_momentum_async missing"
    # Shared parser must exist
    assert "_parse_git_log_output" in src


def test_ast_pin_phase2_fs_signal_executor_substrate() -> None:
    """Phase 2 dedicated filesystem executor MUST be present with
    lazy singleton + shutdown helpers, AND build_bundle_async MUST
    route the 4 filesystem-bound signals through it."""
    src = POSTURE_FILE.read_text()
    assert "_get_fs_signal_executor" in src
    assert "shutdown_fs_signal_executor" in src
    assert "JARVIS_POSTURE_FS_SIGNAL_EXECUTOR_MAX_WORKERS" in src
    tree = ast.parse(src, filename=str(POSTURE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "build_bundle_async"
        ):
            body = ast.unparse(node)
            assert "_get_fs_signal_executor" in body, (
                "build_bundle_async must call _get_fs_signal_executor"
            )
            # The 4 named filesystem signals must use run_in_executor
            # (not to_thread). Count the run_in_executor calls — should
            # be 4 (postmortem, iron_gate, l2_repair, session_lessons).
            n_exec = body.count("run_in_executor")
            assert n_exec >= 4, (
                f"build_bundle_async expected ≥4 run_in_executor calls "
                f"(one per filesystem signal), found {n_exec}"
            )
            return
    pytest.fail("build_bundle_async not located")


def test_ast_pin_phase3_oracle_graph_queue_substrate() -> None:
    """TheOracle MUST have queue + consumer + apply method names
    present, AND _index_file MUST dispatch through queue when
    master flag enabled."""
    src = ORACLE_FILE.read_text()
    # Master flag + helpers
    assert "_is_oracle_graph_queue_enabled" in src
    assert "JARVIS_ORACLE_GRAPH_QUEUE_ENABLED" in src
    # Methods
    for name in (
        "_ensure_graph_write_consumer",
        "_graph_write_consumer_loop",
        "_apply_graph_batch_sync",
        "stop_graph_write_consumer",
    ):
        assert name in src, f"TheOracle missing method {name}"
    # _index_file dispatches through queue
    tree = ast.parse(src, filename=str(ORACLE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_index_file"
        ):
            body = ast.unparse(node)
            assert "_is_oracle_graph_queue_enabled" in body, (
                "_index_file must consult the queue master flag"
            )
            assert "_graph_write_queue" in body, (
                "_index_file must enqueue to _graph_write_queue"
            )
            assert "_ensure_graph_write_consumer" in body, (
                "_index_file must ensure consumer is started"
            )
            return
    pytest.fail("_index_file not located")


def test_ast_pin_phase3_master_flag_default_true() -> None:
    """``JARVIS_ORACLE_GRAPH_QUEUE_ENABLED`` defaults TRUE."""
    from backend.core.ouroboros.oracle import (
        _is_oracle_graph_queue_enabled,
        _ORACLE_GRAPH_QUEUE_ENABLED_ENV,
    )
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ORACLE_GRAPH_QUEUE_ENABLED_ENV, None)
        assert _is_oracle_graph_queue_enabled() is True
    for falsy in ("0", "false", "FALSE", "no", "off"):
        with mock.patch.dict(
            os.environ, {_ORACLE_GRAPH_QUEUE_ENABLED_ENV: falsy},
        ):
            assert _is_oracle_graph_queue_enabled() is False, (
                f"{falsy!r} should disable"
            )


# ──────────────────────────────────────────────────────────────────────
# Spine — 11
# ──────────────────────────────────────────────────────────────────────


def test_spine_phase1_commit_ratios_async_parity() -> None:
    """Sync ``commit_ratios()`` and async ``commit_ratios_async()``
    MUST return the same dict for the same input commits."""
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector,
    )
    c = SignalCollector(Path.cwd())
    sync_result = c.commit_ratios()
    async_result = asyncio.run(c.commit_ratios_async())
    assert sync_result == async_result, (
        f"sync↔async commit_ratios diverged: "
        f"sync={sync_result} async={async_result}"
    )


def test_spine_phase1_async_subprocess_loop_stays_responsive() -> None:
    """While ``commit_ratios_async`` is running, sibling coroutine
    heartbeat MUST tick (proves create_subprocess_exec genuinely
    yields to the loop)."""
    from backend.core.ouroboros.governance.posture_observer import (
        SignalCollector,
    )

    async def run() -> int:
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        hb = asyncio.create_task(heartbeat())
        try:
            c = SignalCollector(Path.cwd())
            await c.commit_ratios_async()
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
        return ticks

    ticks = asyncio.run(run())
    assert ticks >= 2, (
        f"loop wedged during commit_ratios_async — only {ticks} ticks"
    )


def test_spine_phase1_git_momentum_async_parity() -> None:
    """Sync + async ``compute_recent_momentum`` MUST produce
    equal MomentumSnapshot for the same repo."""
    from backend.core.ouroboros.governance.git_momentum import (
        compute_recent_momentum, compute_recent_momentum_async,
    )
    sync_snap = compute_recent_momentum(Path.cwd(), max_commits=10)
    async_snap = asyncio.run(
        compute_recent_momentum_async(Path.cwd(), max_commits=10),
    )
    assert sync_snap == async_snap, (
        f"compute_recent_momentum sync↔async diverged"
    )


def test_spine_phase2_fs_executor_lazy_singleton() -> None:
    """``_get_fs_signal_executor`` returns the same instance across
    calls, with the configured max_workers."""
    from backend.core.ouroboros.governance.posture_observer import (
        _get_fs_signal_executor, shutdown_fs_signal_executor,
    )
    try:
        e1 = _get_fs_signal_executor()
        e2 = _get_fs_signal_executor()
        assert e1 is e2, "fs_executor must be a singleton"
        assert e1._max_workers == 2, (
            f"default max_workers should be 2, got {e1._max_workers}"
        )
    finally:
        shutdown_fs_signal_executor()


def test_spine_phase2_fs_executor_max_workers_env_override() -> None:
    """``JARVIS_POSTURE_FS_SIGNAL_EXECUTOR_MAX_WORKERS`` env knob
    overrides the default."""
    from backend.core.ouroboros.governance.posture_observer import (
        _get_fs_signal_executor, shutdown_fs_signal_executor,
    )
    shutdown_fs_signal_executor()  # reset for env override
    with mock.patch.dict(
        os.environ,
        {"JARVIS_POSTURE_FS_SIGNAL_EXECUTOR_MAX_WORKERS": "4"},
    ):
        try:
            e = _get_fs_signal_executor()
            assert e._max_workers == 4
        finally:
            shutdown_fs_signal_executor()


def test_spine_phase2_build_bundle_async_uses_fs_executor() -> None:
    """``build_bundle_async`` MUST submit the 4 named filesystem
    signals to the dedicated executor — verified by patching
    ``_get_fs_signal_executor`` to return a mock and checking
    submit count."""
    from backend.core.ouroboros.governance import posture_observer
    fake_exec = mock.MagicMock()
    fake_exec._max_workers = 2

    # Make run_in_executor return Futures that resolve to whatever
    # the wrapped fn returns
    async def run() -> int:
        with mock.patch.object(
            posture_observer, "_get_fs_signal_executor",
            return_value=fake_exec,
        ):
            c = posture_observer.SignalCollector(Path.cwd())
            # Patch loop.run_in_executor to call the fn directly
            # (avoid actually using fake_exec for execution)
            real_loop = asyncio.get_running_loop()
            orig_rie = real_loop.run_in_executor

            calls = []

            def patched_rie(executor, fn, *args):
                if executor is fake_exec:
                    calls.append(fn.__name__)
                return orig_rie(executor if executor is not fake_exec else None, fn, *args)

            real_loop.run_in_executor = patched_rie  # type: ignore[method-assign]
            try:
                await c.build_bundle_async()
            finally:
                real_loop.run_in_executor = orig_rie  # type: ignore[method-assign]
            return len(calls)

    n_calls = asyncio.run(run())
    assert n_calls == 4, (
        f"expected 4 fs_executor dispatches "
        f"(postmortem/iron_gate/l2_repair/session_lessons), got {n_calls}"
    )


def test_spine_phase3_graph_queue_consumer_lazy_start() -> None:
    """``_ensure_graph_write_consumer`` is idempotent + starts a task."""
    from backend.core.ouroboros.oracle import TheOracle

    async def run():
        o = TheOracle()
        assert o._graph_write_consumer_started is False
        await o._ensure_graph_write_consumer()
        assert o._graph_write_consumer_started is True
        assert o._graph_write_queue is not None
        assert o._graph_write_consumer_task is not None
        # Second call is idempotent
        await o._ensure_graph_write_consumer()
        # Clean shutdown
        await o.stop_graph_write_consumer()

    asyncio.run(run())


def test_spine_phase3_graph_queue_drains_batch() -> None:
    """End-to-end: enqueue items, consumer drains via apply, graph
    state updates."""
    from backend.core.ouroboros.oracle import (
        TheOracle, NodeData, NodeID, NodeType, EdgeData, EdgeType,
    )

    async def run():
        o = TheOracle()
        await o._ensure_graph_write_consumer()

        nid = NodeID(
            repo="testrepo", file_path="t.py",
            name="foo", node_type=NodeType.FUNCTION,
        )
        nd = NodeData(node_id=nid)
        # Enqueue a single-item batch (no edges)
        assert o._graph_write_queue is not None
        await o._graph_write_queue.put(([nd], [], "testrepo:t.py", "abc"))
        # Wait briefly for consumer to drain
        await asyncio.sleep(0.2)
        # Graph should now contain the node — verified via the
        # CodebaseKnowledgeGraph wrapper's get_node accessor (the
        # _graph attribute is the wrapper, not the bare DiGraph)
        assert o._graph.get_node(nid) is not None, (
            "consumer didn't drain + apply within 200ms"
        )
        assert o._graph_writes_applied >= 1
        await o.stop_graph_write_consumer()

    asyncio.run(run())


def test_spine_phase3_apply_batch_sync_never_raises() -> None:
    """``_apply_graph_batch_sync`` MUST swallow per-item errors
    and continue processing the rest of the batch."""
    from backend.core.ouroboros.oracle import TheOracle
    o = TheOracle()
    # Mix of bad (will fail unpack) and good items
    bad_batch = [
        "not_a_tuple",  # will fail unpack
        ([], [], "key1", "hash1"),  # empty but valid
    ]
    # Must not raise
    try:
        o._apply_graph_batch_sync(bad_batch)
    except Exception as exc:
        pytest.fail(
            f"_apply_graph_batch_sync propagated error: {exc}"
        )
    # The good item should have applied its hash
    assert o._file_hashes.get("key1") == "hash1"


def test_spine_phase3_master_flag_disable_restores_inline_path() -> None:
    """When ``JARVIS_ORACLE_GRAPH_QUEUE_ENABLED=0``, ``_index_file``
    MUST take the legacy inline-write path (no queue dispatch).
    AST-walk verifies the legacy branch is reachable."""
    src = ORACLE_FILE.read_text()
    tree = ast.parse(src, filename=str(ORACLE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_index_file"
        ):
            body = ast.unparse(node)
            # Legacy branch must contain inline add_node/add_edge
            assert "self._graph.add_node" in body, (
                "_index_file legacy path missing inline add_node call"
            )
            assert "self._graph.add_edge" in body, (
                "_index_file legacy path missing inline add_edge call"
            )
            return
    pytest.fail("_index_file not located")


def test_spine_phase3_consumer_stop_idempotent() -> None:
    """``stop_graph_write_consumer`` is safe to call repeatedly + on
    a never-started consumer."""
    from backend.core.ouroboros.oracle import TheOracle

    async def run():
        o = TheOracle()
        # Stop before start — must not raise
        await o.stop_graph_write_consumer()
        await o._ensure_graph_write_consumer()
        # Stop twice — must not raise
        await o.stop_graph_write_consumer()
        await o.stop_graph_write_consumer()

    asyncio.run(run())
