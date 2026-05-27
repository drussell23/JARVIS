"""Slice 32 — Oracle process-pool isolation (closes v25 wedge).

Closes the v25 (``bt-2026-05-27-194342``) control-plane wedge:
25-minute asyncio loop freeze between 13:34 and 14:00 caused by GIL
contention from N ``asyncio.to_thread`` workers each running
``CodeStructureVisitor.visit`` on pure-Python CPU-bound AST walks.
LoopDeadman fired ``os._exit(75)`` after 1531.6s without heartbeat.

# Architectural fix (composition, not duplication)

Slice 32 routes ``TheOracle._index_file``'s heavy parse + visitor
walk through the existing ``ast_compile_helper`` module-singleton
``ProcessPoolExecutor`` (spawn context). Oracle becomes a second
consumer alongside OpportunityMiner — sharing the pool's lifecycle,
its closed taxonomies, its fail-closed semantics. No parallel pool.

# Payload discipline

  * Worker lazily imports ``CodeStructureVisitor`` etc. inside its
    body (spawn process — runs once per worker, cached afterward).
  * Worker returns ``list[NodeData] + list[Tuple[NodeID, NodeID,
    EdgeData]] + content_hash + worker_elapsed_ms``. NodeID is a
    frozen dataclass; NodeData/EdgeData are plain dataclasses;
    NodeType/EdgeType are enums. All transitively IPC-safe.
  * **NO ``ast.AST`` ever crosses the IPC boundary** (operator
    binding).

# Escape hatch

``JARVIS_ORACLE_LEGACY_THREAD_MODE=1`` restores the pre-Slice-32
threadpool path byte-identically — emergency rollback only. Default
**off** (new path active).

# Test surface (4 AST pins + 7 spine = 11 tests)
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "ast_compile_helper.py"
)
ORACLE_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "oracle.py"


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 4
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_oracle_helper_public_coro_present() -> None:
    """``analyze_python_source_for_oracle`` MUST be an async public
    coroutine in ``ast_compile_helper.py``. Without it the Slice 32
    composition is broken and oracle._index_file can't dispatch."""
    src = HELPER_FILE.read_text()
    tree = ast.parse(src, filename=str(HELPER_FILE))
    coro_found = False
    worker_found = False
    result_dc_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "analyze_python_source_for_oracle":
                coro_found = True
        if isinstance(node, ast.FunctionDef):
            if node.name == "_worker_analyze_for_oracle_in_process":
                worker_found = True
        if isinstance(node, ast.ClassDef):
            if node.name == "OracleAnalysisResult":
                result_dc_found = True
    assert coro_found, (
        "analyze_python_source_for_oracle async coro missing — "
        "Slice 32 surface reverted"
    )
    assert worker_found, (
        "_worker_analyze_for_oracle_in_process worker fn missing — "
        "spawn pool can't resolve the symbol"
    )
    assert result_dc_found, (
        "OracleAnalysisResult dataclass missing — Slice 32 surface incomplete"
    )
    assert '"analyze_python_source_for_oracle"' in src, (
        "analyze_python_source_for_oracle not added to __all__"
    )
    assert '"OracleAnalysisResult"' in src, (
        "OracleAnalysisResult not added to __all__"
    )
    assert "Slice 32" in src, (
        "ast_compile_helper missing Slice 32 attribution"
    )


def test_ast_pin_worker_returns_only_primitives_no_ast_object() -> None:
    """The Slice 32 worker MUST NOT return an ``ast.AST`` (or any
    parsed tree object) across the IPC boundary. Operator binding:
    "never pass a raw, un-serializable ast.AST object across IPC".

    The worker returns ``(label, payload)`` where ``payload`` on OK
    is ``(nodes_list, edges_list, content_hash, worker_elapsed_ms)``.
    No return statement inside the worker body should reference the
    parsed ``tree`` variable directly."""
    src = HELPER_FILE.read_text()
    tree = ast.parse(src, filename=str(HELPER_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_worker_analyze_for_oracle_in_process"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    ret_src = ast.unparse(sub.value)
                    # Bare 'tree' must not appear as a return-payload
                    # value (it's only the ast.parse() local).
                    tokens = ret_src.replace(",", " ").replace(
                        "(", " ").replace(")", " ").split()
                    assert "tree" not in tokens, (
                        f"worker return references ast tree: {ret_src!r} — "
                        f"violates operator no-ast-across-IPC binding"
                    )
            return
    pytest.fail("_worker_analyze_for_oracle_in_process not found in AST walk")


def test_ast_pin_oracle_index_file_dispatches_through_new_helper() -> None:
    """``TheOracle._index_file`` MUST reference both the master-flag
    resolver ``_is_oracle_legacy_thread_mode`` AND the new helper
    ``analyze_python_source_for_oracle``. Either reference missing
    means the dispatch wiring is broken and the v25 wedge re-opens."""
    src = ORACLE_FILE.read_text()
    tree = ast.parse(src, filename=str(ORACLE_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_index_file"
        ):
            body = ast.unparse(node)
            assert "_is_oracle_legacy_thread_mode" in body, (
                "_index_file doesn't consult the master flag — "
                "Slice 32 wiring incomplete"
            )
            assert "analyze_python_source_for_oracle" in body, (
                "_index_file doesn't call the new helper — "
                "Slice 32 wiring incomplete"
            )
            found = True
            break
    assert found, "TheOracle._index_file not located in AST walk"
    assert "Slice 32" in src, (
        "oracle.py missing Slice 32 attribution"
    )


def test_ast_pin_master_flag_default_false() -> None:
    """The master flag ``JARVIS_ORACLE_LEGACY_THREAD_MODE`` MUST
    default to FALSE — the new process-pool path is the active path
    per operator binding. Empty / unset / unrecognized values all
    return False from the resolver."""
    from backend.core.ouroboros.oracle import (
        _is_oracle_legacy_thread_mode,
        _ORACLE_LEGACY_THREAD_MODE_ENV,
    )
    # Default (no env)
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop(_ORACLE_LEGACY_THREAD_MODE_ENV, None)
        assert _is_oracle_legacy_thread_mode() is False
    # Explicit empty
    with mock.patch.dict(
        os.environ, {_ORACLE_LEGACY_THREAD_MODE_ENV: ""}
    ):
        assert _is_oracle_legacy_thread_mode() is False
    # Unrecognized value
    with mock.patch.dict(
        os.environ, {_ORACLE_LEGACY_THREAD_MODE_ENV: "maybe"}
    ):
        assert _is_oracle_legacy_thread_mode() is False
    # Truthy values flip it on
    for v in ("1", "true", "TRUE", "yes", "on"):
        with mock.patch.dict(
            os.environ, {_ORACLE_LEGACY_THREAD_MODE_ENV: v}
        ):
            assert _is_oracle_legacy_thread_mode() is True, (
                f"truthy value {v!r} should enable legacy mode"
            )


# ──────────────────────────────────────────────────────────────────────
# Spine — 7
# ──────────────────────────────────────────────────────────────────────


def test_spine_oracle_helper_inline_tiny_path() -> None:
    """For sources at/below the tiny threshold, the helper MUST run
    inline (no IPC cost) and return ``execution_mode=INLINE_TINY``.
    Validates the small-source bypass."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, ExecutionMode, analyze_python_source_for_oracle,
    )
    src = "def f():\n    return 1\n"  # <100 bytes
    res = asyncio.run(
        analyze_python_source_for_oracle(
            caller="test.tiny",
            source=src,
            filename="t.py",
            repo_name="testrepo",
            relative_path="t.py",
        )
    )
    assert res.outcome == AnalyzeOutcome.OK
    assert res.execution_mode == ExecutionMode.INLINE_TINY
    assert len(res.nodes) >= 1  # at least the function node
    assert res.content_hash != ""


def test_spine_oracle_helper_process_path_payload_shape() -> None:
    """For sources above the tiny threshold, the helper MUST take
    the process path and the returned ``nodes``/``edges`` MUST be
    structurally compatible with what ``TheOracle._graph.add_node``
    + ``add_edge`` consume (NodeData + (NodeID, NodeID, EdgeData)
    tuples). Validates the IPC payload contract."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, ExecutionMode, analyze_python_source_for_oracle,
        shutdown_pool,
    )
    from backend.core.ouroboros.oracle import NodeData, NodeID, EdgeData

    # Build source large enough to exceed default 4KB tiny threshold.
    src = "import os\n\nclass A:\n    def m(self): return 1\n\n" * 200
    assert len(src.encode()) > 4096

    try:
        res = asyncio.run(
            analyze_python_source_for_oracle(
                caller="test.process",
                source=src,
                filename="t.py",
                repo_name="jarvis",
                relative_path="t.py",
            )
        )
    finally:
        shutdown_pool()

    assert res.outcome == AnalyzeOutcome.OK, res.error_detail
    assert res.execution_mode == ExecutionMode.PROCESS
    assert len(res.nodes) > 0
    # Type checks survive IPC round-trip
    assert isinstance(res.nodes[0], NodeData), (
        f"node[0] is {type(res.nodes[0]).__name__}, expected NodeData"
    )
    src_id, tgt_id, edge = res.edges[0]
    assert isinstance(src_id, NodeID)
    assert isinstance(tgt_id, NodeID)
    assert isinstance(edge, EdgeData)
    assert res.worker_elapsed_ms > 0.0
    assert res.content_hash != ""


def test_spine_oracle_helper_syntax_error_clean() -> None:
    """A syntax-broken source MUST return
    ``AnalyzeOutcome.SYNTAX_ERROR`` with empty nodes/edges (sentinel
    for "skip this file"). NEVER raises."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, analyze_python_source_for_oracle,
    )
    bad_src = "def broken(:\n  pass\n"
    res = asyncio.run(
        analyze_python_source_for_oracle(
            caller="test.syntax",
            source=bad_src,
            filename="bad.py",
        )
    )
    assert res.outcome == AnalyzeOutcome.SYNTAX_ERROR
    assert res.nodes == ()
    assert res.edges == ()
    assert (
        "SyntaxError" in res.error_detail
        or "invalid" in res.error_detail.lower()
    )


def test_spine_oracle_helper_too_large_short_circuits() -> None:
    """A source exceeding ``max_bytes`` MUST short-circuit to
    ``TOO_LARGE`` without touching the pool. Validates the bound."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, ExecutionMode, analyze_python_source_for_oracle,
    )
    res = asyncio.run(
        analyze_python_source_for_oracle(
            caller="test.toolarge",
            source="x = 1\n" * 100_000,  # ~600 KB
            filename="big.py",
            max_bytes=1_000,  # 1 KB cap
        )
    )
    assert res.outcome == AnalyzeOutcome.TOO_LARGE
    assert res.nodes == ()
    assert res.execution_mode == ExecutionMode.INLINE_TINY  # nominal


def test_spine_main_loop_stays_responsive_during_process_dispatch() -> None:
    """The key Slice 32 contract: while the helper is dispatched to
    the spawn worker, the asyncio main loop MUST keep ticking. We
    verify by running a sibling coroutine that records a tick every
    50ms; over the duration of the parse the tick count MUST grow
    monotonically — proving the loop never wedged.

    This is the structural inverse of the v25 wedge."""
    from backend.core.ouroboros.governance.ast_compile_helper import (
        AnalyzeOutcome, analyze_python_source_for_oracle, shutdown_pool,
    )

    src = "import os\n\nclass A:\n    def m(self): return 1\n\n" * 500
    assert len(src.encode()) > 4096

    async def run():
        tick_count = 0

        async def heartbeat():
            nonlocal tick_count
            while True:
                tick_count += 1
                await asyncio.sleep(0.05)

        hb = asyncio.create_task(heartbeat())
        try:
            res = await analyze_python_source_for_oracle(
                caller="test.heartbeat",
                source=src,
                filename="t.py",
                repo_name="jarvis",
                relative_path="t.py",
            )
        finally:
            hb.cancel()
            try:
                await hb
            except asyncio.CancelledError:
                pass
        return res, tick_count

    try:
        res, ticks = asyncio.run(run())
    finally:
        shutdown_pool()

    assert res.outcome == AnalyzeOutcome.OK
    # If the loop were wedged, ticks would be 0 or 1. Even a slow
    # first-call spawn (~2-3s on cold pool) gives the heartbeat
    # plenty of room to fire multiple times. Threshold: at least 2
    # ticks proves the loop wasn't fully blocked during the await.
    assert ticks >= 2, (
        f"main loop wedged during process dispatch — only {ticks} "
        f"heartbeat tick(s) over {res.elapsed_ms:.1f}ms parent-await. "
        f"This is the v25 wedge re-opening."
    )


def test_spine_legacy_thread_mode_dispatches_through_old_path() -> None:
    """When ``JARVIS_ORACLE_LEGACY_THREAD_MODE=1``, ``_index_file``
    MUST call ``asyncio.to_thread`` with ``_read_parse_visit_blocking``
    (legacy path), NOT the new helper. AST-walk verifies the
    legacy-branch body wiring is intact."""
    src = ORACLE_FILE.read_text()
    tree = ast.parse(src, filename=str(ORACLE_FILE))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_index_file"
        ):
            body = ast.unparse(node)
            # Legacy path preserved verbatim
            assert "asyncio.to_thread" in body, (
                "_index_file legacy path missing asyncio.to_thread call"
            )
            assert "_read_parse_visit_blocking" in body, (
                "_index_file legacy path missing _read_parse_visit_blocking ref"
            )
            return
    pytest.fail("_index_file not located in AST walk")


def test_spine_worker_fn_is_module_level_for_spawn_resolution() -> None:
    """``_worker_analyze_for_oracle_in_process`` MUST live at module
    level (not nested inside a class/function) so the spawn context's
    qualname-based resolution can locate it in the worker process.
    A nested worker fn would break the pool."""
    from backend.core.ouroboros.governance import ast_compile_helper as h
    worker = h._worker_analyze_for_oracle_in_process
    # Module-level: __qualname__ equals __name__ (no class/fn prefix)
    assert worker.__qualname__ == worker.__name__ == (
        "_worker_analyze_for_oracle_in_process"
    ), (
        f"worker fn is not module-level: __qualname__={worker.__qualname__} "
        f"— spawn context can't resolve nested functions"
    )
    assert worker.__module__ == (
        "backend.core.ouroboros.governance.ast_compile_helper"
    )
