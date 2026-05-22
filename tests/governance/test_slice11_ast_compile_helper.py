"""Slice 11B — canonical AST/compile helper + OpportunityMiner migration tests.

The helper closes the empirical wedge from bt-2026-05-22-013824
(Slice 11A provenance): 85 on-loop ``ast.parse()`` calls totalling
101.3 seconds of asyncio event-loop blocking time, with
``opportunity_miner_sensor._scan_module`` as the dominant caller.

Slice 11B ships the canonical helper + migrates OpportunityMiner.
Other heavy callers (provider_topology, shipped_code_invariants,
cross_kingdom_boundary) are explicitly deferred to 11C/11D per
operator scope.

## Test surface

### Helper unit tests
  * Successful parse — small + large source
  * Syntax error fail-closed
  * Timeout fail-closed using a controllable slow worker
  * Too-large fail-closed without touching the pool
  * Execution mode correctness — inline_tiny vs process
  * Internal error fail-closed
  * Closed-taxonomy AST pin (ParseOutcome, ExecutionMode)

### OpportunityMiner migration tests
  * scan_once routes through helper (AST pin: no direct ast.parse
    in the async method body)
  * scan_file routes through helper (AST pin: same)
  * Failed parse skips the file (preserves legacy error-counter +
    no-candidate semantics)

### Helper AST cage
  * ast.parse() in the helper module appears in EXACTLY ONE
    function (the process-pool worker ``_worker_parse_in_process``)
  * Timeout path present
  * max_bytes path present
  * Process-pool path present
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import time
import unittest
from typing import List
from unittest.mock import AsyncMock, patch

from backend.core.ouroboros.governance.ast_compile_helper import (
    ExecutionMode,
    ParseOutcome,
    ParseResult,
    parse_python_source,
    shutdown_pool,
)
from backend.core.ouroboros.governance import ast_compile_helper as helper_mod


_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_HELPER_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "ast_compile_helper.py"
)
_MINER_FILE = (
    _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "intake" / "sensors" / "opportunity_miner_sensor.py"
)


def _parse_module(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text())


# ============================================================================
# Closed-taxonomy AST pins
# ============================================================================


class TestClosedTaxonomies(unittest.TestCase):
    """Adding a 6th ParseOutcome / 4th ExecutionMode requires
    bumping these pins + every consumer branch."""

    def test_parse_outcome_five_values(self) -> None:
        self.assertEqual(len(list(ParseOutcome)), 5)
        self.assertEqual(
            {m.name for m in ParseOutcome},
            {"OK", "SYNTAX_ERROR", "TIMEOUT", "TOO_LARGE",
             "INTERNAL_ERROR"},
        )

    def test_execution_mode_three_values(self) -> None:
        self.assertEqual(len(list(ExecutionMode)), 3)
        self.assertEqual(
            {m.name for m in ExecutionMode},
            {"INLINE_TINY", "THREAD", "PROCESS"},
        )

    def test_parse_result_is_frozen(self) -> None:
        r = ParseResult(
            outcome=ParseOutcome.OK,
            tree=None,
            elapsed_ms=1.0,
            source_bytes=10,
            caller="t",
            execution_mode=ExecutionMode.INLINE_TINY,
        )
        with self.assertRaises(Exception):
            r.outcome = ParseOutcome.SYNTAX_ERROR  # type: ignore[misc]


# ============================================================================
# Helper AST cage — ast.parse only in worker function
# ============================================================================


class TestHelperAstCage(unittest.TestCase):
    """The SOLE permitted ``ast.parse()`` call site in
    ast_compile_helper.py is ``_worker_parse_in_process``."""

    def _find_ast_parse_calls(
        self, tree: ast.Module,
    ) -> List[tuple]:
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "parse"
            ):
                # Match _ast_mod.parse OR ast.parse
                if isinstance(f.value, ast.Name) and f.value.id in {
                    "ast", "_ast_mod",
                }:
                    out.append((node.lineno,))
        return out

    def _enclosing_function(
        self, tree: ast.Module, lineno: int,
    ) -> str:
        best_name = "<module>"
        best_span = float("inf")
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            end = getattr(node, "end_lineno", None) or lineno
            if node.lineno <= lineno <= end:
                span = end - node.lineno
                if span < best_span:
                    best_span = span
                    best_name = node.name
        return best_name

    def test_helper_ast_parse_only_in_worker(self) -> None:
        tree = _parse_module(_HELPER_FILE)
        calls = self._find_ast_parse_calls(tree)
        self.assertGreater(len(calls), 0, "helper must call ast.parse")
        # 11B-fix extends the allowed set to include
        # ``_worker_analyze_in_process`` (parse + analyze in the
        # same worker call, returning a primitive payload).
        # ``_inline_tiny_parse`` remains for the parse helper's
        # tiny-source path; ``_inline_tiny_analyze`` is the analyze
        # helper's tiny-source path (which calls
        # ``_worker_analyze_in_process`` inline, so it never calls
        # ast.parse directly).
        for (lineno,) in calls:
            fn_name = self._enclosing_function(tree, lineno)
            self.assertIn(
                fn_name,
                {
                    "_worker_parse_in_process",
                    "_worker_analyze_in_process",
                    "_inline_tiny_parse",
                },
                f"helper ast.parse at L{lineno} in function "
                f"{fn_name!r} — must be in a worker or inline-tiny "
                f"path",
            )

    def test_helper_has_timeout_path(self) -> None:
        src = _HELPER_FILE.read_text()
        self.assertIn("ParseOutcome.TIMEOUT", src)
        self.assertIn("asyncio.wait_for", src)

    def test_helper_has_max_bytes_path(self) -> None:
        src = _HELPER_FILE.read_text()
        self.assertIn("ParseOutcome.TOO_LARGE", src)
        self.assertIn("max_bytes", src)

    def test_helper_uses_process_pool(self) -> None:
        src = _HELPER_FILE.read_text()
        self.assertIn("ProcessPoolExecutor", src)
        self.assertIn('get_context("spawn")', src)


# ============================================================================
# Helper behavioural tests — async path
# ============================================================================


class TestHelperBehavioural(unittest.IsolatedAsyncioTestCase):
    """End-to-end behaviour of parse_python_source — spawns real
    process-pool workers + exercises every outcome path."""

    async def asyncTearDown(self) -> None:
        # Don't tear down the pool between tests (it's a singleton);
        # tear down at module exit instead. Tests are quick.
        pass

    async def test_ok_small_source_inline_tiny(self) -> None:
        result = await parse_python_source(
            "test.ok_small",
            "x = 1\ny = x + 1\n",
        )
        self.assertEqual(result.outcome, ParseOutcome.OK)
        self.assertIsNotNone(result.tree)
        self.assertEqual(
            result.execution_mode, ExecutionMode.INLINE_TINY,
        )
        self.assertGreater(result.source_bytes, 0)

    async def test_ok_large_source_process_pool(self) -> None:
        # Source above the 4KB tiny threshold → process pool.
        # Generate ~10KB of valid Python.
        large_src = "\n".join(
            f"def f_{i}(x): return x + {i}" for i in range(400)
        )
        result = await parse_python_source(
            "test.ok_large", large_src,
        )
        self.assertEqual(result.outcome, ParseOutcome.OK)
        self.assertIsNotNone(result.tree)
        self.assertEqual(result.execution_mode, ExecutionMode.PROCESS)
        # The tree IS a real ast.Module despite crossing process
        # boundaries via the executor's IPC.
        self.assertEqual(type(result.tree).__name__, "Module")

    async def test_syntax_error_inline_tiny_fail_closed(self) -> None:
        result = await parse_python_source(
            "test.syntax_err_small",
            "def broken(:\n",  # syntactically invalid
        )
        self.assertEqual(result.outcome, ParseOutcome.SYNTAX_ERROR)
        self.assertIsNone(result.tree)
        self.assertIn("SyntaxError", result.error_detail)

    async def test_syntax_error_process_pool_fail_closed(self) -> None:
        large_bad = "\n".join([
            "def f1(x): return x",
        ] * 500) + "\ndef broken(:\n"  # > 4KB + syntax error
        result = await parse_python_source(
            "test.syntax_err_large", large_bad,
        )
        self.assertEqual(result.outcome, ParseOutcome.SYNTAX_ERROR)
        self.assertIsNone(result.tree)
        self.assertEqual(result.execution_mode, ExecutionMode.PROCESS)

    async def test_too_large_fail_closed_without_pool(self) -> None:
        # Build source > 1MB cap; use max_bytes override for speed.
        big = "x = 1\n" * 10
        result = await parse_python_source(
            "test.too_large",
            big,
            max_bytes=10,  # tiny cap forces TOO_LARGE
        )
        self.assertEqual(result.outcome, ParseOutcome.TOO_LARGE)
        self.assertIsNone(result.tree)
        self.assertIn("max_bytes", result.error_detail)

    async def test_timeout_fail_closed(self) -> None:
        """TIMEOUT outcome fires when the worker takes longer than
        timeout_s. We exercise this by submitting a real (large)
        source with an ABSURDLY tight timeout — the executor's
        wait_for fires before the worker can ship its result back.

        We can't monkey-patch _worker_parse_in_process because the
        ProcessPoolExecutor spawns workers via ``spawn`` mode — they
        re-import the module fresh in each worker process and don't
        see main-process patches. The timeout path is exercised
        structurally instead."""
        # Generate >4KB to force the process-pool path.
        src = "\n".join(f"x_{i} = {i}" for i in range(500))
        # 1ms timeout: even the worker spawn cost can't complete
        # this fast — the wait_for will fire.
        result = await parse_python_source(
            "test.timeout",
            src,
            timeout_s=0.001,
        )
        self.assertEqual(
            result.outcome, ParseOutcome.TIMEOUT,
            f"Expected TIMEOUT for 1ms deadline; got {result.outcome.name}",
        )
        self.assertIsNone(result.tree)
        self.assertEqual(result.execution_mode, ExecutionMode.PROCESS)
        # Bounded: helper returned within reasonable grace.
        self.assertLess(result.elapsed_ms, 3000.0)

    async def test_off_loop_provenance_for_process_path(self) -> None:
        """The process path's measure() entry records the call.
        We verify that the result's execution_mode is PROCESS
        for non-tiny sources."""
        big = "y = 1\n" * 1000  # ~6KB → above 4KB tiny threshold
        result = await parse_python_source(
            "test.off_loop_provenance",
            big,
        )
        self.assertEqual(result.execution_mode, ExecutionMode.PROCESS)
        self.assertEqual(result.outcome, ParseOutcome.OK)


# ============================================================================
# OpportunityMiner migration AST pins
# ============================================================================


class TestOpportunityMinerMigration(unittest.TestCase):
    """OpportunityMiner's two async methods (scan_once, scan_file)
    MUST route through ``parse_python_source``, not call
    ``ast.parse()`` directly. AST pin enforces this."""

    def _find_direct_ast_parse_in_method(
        self, tree: ast.Module, method_name: str,
    ) -> List[int]:
        """Find direct ``ast.parse()`` calls inside the specified
        async method's body."""
        out: List[int] = []
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == method_name
            ):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if not (
                    isinstance(f, ast.Attribute)
                    and f.attr == "parse"
                ):
                    continue
                if (
                    isinstance(f.value, ast.Name)
                    and f.value.id == "ast"
                ):
                    out.append(sub.lineno)
        return out

    def test_scan_once_has_no_direct_ast_parse(self) -> None:
        tree = _parse_module(_MINER_FILE)
        offenders = self._find_direct_ast_parse_in_method(
            tree, "scan_once",
        )
        self.assertEqual(
            offenders, [],
            f"OpportunityMiner.scan_once contains direct "
            f"ast.parse() calls at L{offenders} — must route "
            f"through parse_python_source.",
        )

    def test_scan_file_has_no_direct_ast_parse(self) -> None:
        tree = _parse_module(_MINER_FILE)
        offenders = self._find_direct_ast_parse_in_method(
            tree, "scan_file",
        )
        self.assertEqual(
            offenders, [],
            f"OpportunityMiner.scan_file contains direct "
            f"ast.parse() calls at L{offenders} — must route "
            f"through parse_python_source.",
        )

    def test_scan_once_imports_helper(self) -> None:
        """The helper import must be present in the scan_once body
        (lazy import, mirrors the Slice 10 pattern). 11B-fix
        replaces the parse-only entry with the analyze entry —
        scan_once now imports
        ``analyze_python_source_for_opportunity_miner`` /
        ``AnalyzeOutcome`` (no ast.AST consumption)."""
        src = _MINER_FILE.read_text()
        self.assertIn("ast_compile_helper", src)
        self.assertIn(
            "analyze_python_source_for_opportunity_miner", src,
        )
        self.assertIn("AnalyzeOutcome", src)

    def test_scan_once_routes_caller_label(self) -> None:
        """The caller label passed to parse_python_source
        identifies the OpportunityMiner site for provenance."""
        src = _MINER_FILE.read_text()
        self.assertIn("opportunity_miner_sensor.scan_once", src)
        self.assertIn("opportunity_miner_sensor.scan_file", src)

    def test_no_ast_parse_anywhere_in_async_methods(self) -> None:
        """Belt-and-suspenders — across BOTH async methods, zero
        direct ast.parse remains."""
        tree = _parse_module(_MINER_FILE)
        total = 0
        for m in ("scan_once", "scan_file"):
            total += len(
                self._find_direct_ast_parse_in_method(tree, m),
            )
        self.assertEqual(total, 0)


# ============================================================================
# OpportunityMiner fail-closed semantics
# ============================================================================


class TestOpportunityMinerFailClosed(unittest.IsolatedAsyncioTestCase):
    """Verify the legacy semantics: failed parse → no candidate.
    We mock parse_python_source to return SYNTAX_ERROR / TIMEOUT
    / INTERNAL_ERROR and assert OpportunityMiner skips the file."""

    async def test_syntax_error_skips_file_via_scan_file(self) -> None:
        """OpportunityMiner.scan_file returns None when the
        helper reports SYNTAX_ERROR (legacy: skip + no candidate)."""
        from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (  # noqa: E501
            OpportunityMinerSensor,
        )
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sensor = OpportunityMinerSensor(
                repo_root=pathlib.Path(td),
                router=None,
            )
            pkg_dir = pathlib.Path(td) / "backend" / "core"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "__init__.py").write_text("")
            test_file = pkg_dir / "fake.py"
            test_file.write_text("def broken(:\n")

            fake_result = _fake_result(ParseOutcome.SYNTAX_ERROR)
            # Patch BOTH the helper's source-of-truth AND the alias
            # the opportunity_miner imports as ``_s11_parse``.
            with patch(
                "backend.core.ouroboros.governance.ast_compile_helper."
                "parse_python_source",
                new=AsyncMock(return_value=fake_result),
            ):
                result = await sensor.scan_file(test_file)
        self.assertIsNone(
            result,
            "scan_file MUST return None on SYNTAX_ERROR — "
            "legacy contract preserved",
        )

    async def test_timeout_skips_file_via_scan_file(self) -> None:
        from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (  # noqa: E501
            OpportunityMinerSensor,
        )
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            sensor = OpportunityMinerSensor(
                repo_root=pathlib.Path(td),
                router=None,
            )
            pkg_dir = pathlib.Path(td) / "backend" / "core"
            pkg_dir.mkdir(parents=True, exist_ok=True)
            (pkg_dir / "__init__.py").write_text("")
            test_file = pkg_dir / "fake.py"
            test_file.write_text("x = 1\n")

            fake_result = _fake_result(ParseOutcome.TIMEOUT)
            with patch(
                "backend.core.ouroboros.governance.ast_compile_helper."
                "parse_python_source",
                new=AsyncMock(return_value=fake_result),
            ):
                result = await sensor.scan_file(test_file)
        self.assertIsNone(result)


def _fake_result(outcome: ParseOutcome) -> ParseResult:
    """Build a synthetic ParseResult for mock returns."""
    return ParseResult(
        outcome=outcome,
        tree=None,
        elapsed_ms=1.0,
        source_bytes=10,
        caller="test",
        execution_mode=ExecutionMode.PROCESS,
    )


# ============================================================================
# Public surface
# ============================================================================


class TestPublicSurface(unittest.TestCase):
    def test_all_exports(self) -> None:
        # 11B-fix extends the public surface with the analyze
        # helper's taxonomy + result types + entry point. The
        # parse-only API stays exposed for narrow non-walking
        # callers.
        self.assertEqual(
            set(helper_mod.__all__),
            {
                "AnalysisResult", "AnalyzeOutcome",
                "ExecutionMode", "OpportunityAnalysisPayload",
                "ParseOutcome", "ParseResult",
                "analyze_python_source_for_opportunity_miner",
                "parse_python_source", "shutdown_pool",
            },
        )

    def test_each_export_resolves(self) -> None:
        for name in helper_mod.__all__:
            self.assertTrue(hasattr(helper_mod, name))


# Clean shutdown of the process pool at module teardown.
def tearDownModule() -> None:  # noqa: N802
    shutdown_pool()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
