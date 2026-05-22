"""Slice 11B-fix — analyze helper + OpportunityMiner deep migration.

Closes the empirical wedge from bt-2026-05-22-055230 (post-Slice-11B
acceptance soak): 31 ``[ControlPlaneStarvation]`` events with max lag
37.7 s and 171.7 s of cumulative loop-block time. Root cause:

  * ``parse_python_source`` returned ``ast.AST`` across the IPC
    boundary (large serialize+deserialize, GIL-held in parent), AND
  * OpportunityMiner ran ``_analyze_file`` (six AST walks) on the
    main asyncio thread after the await.

11B-fix ships:

  * ``analyze_python_source_for_opportunity_miner`` — worker does
    parse + all six dimension calcs, parent receives a small
    primitive ``OpportunityAnalysisPayload``. NO ``ast.AST`` crosses
    the IPC boundary.
  * Telemetry truth: process path no longer wraps the await in
    ``measure(AST_PARSE)`` — those records reported misleading
    ``on_loop=True ast_parse`` for IPC roundtrip time.
  * Default ``max_workers=1``.

## Test surface

### Closed taxonomies
  * ``AnalyzeOutcome`` exactly 5 values
  * ``OpportunityAnalysisPayload`` frozen + 7 fields
  * ``AnalysisResult`` frozen

### Helper AST cage extension
  * ``ast.walk`` + ``ast.iter_child_nodes`` only inside ``_worker_*``
    helpers — heavy AST walks live across the process boundary
  * Default ``max_workers`` is 1

### Behavioural
  * OK path on tiny + large sources, both modes
  * Syntax error / too-large / timeout fail-closed
  * Metrics parity: analyze helper returns the same six dimensions
    as legacy ``_analyze_file`` on a representative source

### Migration pins
  * OpportunityMiner async methods do NOT call ``_analyze_file``
  * OpportunityMiner async methods do NOT access ``.tree`` attr
    on the helper result (no ast.AST consumption)
  * OpportunityMiner async methods route through
    ``analyze_python_source_for_opportunity_miner``

### Telemetry truth
  * Process-mode analyze does NOT emit ``[CompileProvenance]``
    measure() records (would falsely label on-loop ast_parse)
"""

from __future__ import annotations

import ast as _ast
import os
import pathlib
import unittest
from typing import List

from backend.core.ouroboros.governance.ast_compile_helper import (
    AnalysisResult,
    AnalyzeOutcome,
    ExecutionMode,
    OpportunityAnalysisPayload,
    analyze_python_source_for_opportunity_miner,
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


def _parse_module(path: pathlib.Path) -> _ast.Module:
    return _ast.parse(path.read_text())


# ============================================================================
# Closed taxonomy pins
# ============================================================================


class TestClosedTaxonomies(unittest.TestCase):

    def test_analyze_outcome_five_values(self) -> None:
        self.assertEqual(len(list(AnalyzeOutcome)), 5)
        self.assertEqual(
            {m.name for m in AnalyzeOutcome},
            {"OK", "SYNTAX_ERROR", "TIMEOUT", "TOO_LARGE",
             "INTERNAL_ERROR"},
        )

    def test_opportunity_analysis_payload_is_frozen(self) -> None:
        p = OpportunityAnalysisPayload()
        with self.assertRaises(Exception):
            p.cyclomatic_complexity = 99  # type: ignore[misc]

    def test_opportunity_analysis_payload_seven_fields(self) -> None:
        fields = list(OpportunityAnalysisPayload.__dataclass_fields__.keys())
        self.assertEqual(
            set(fields),
            {
                "cyclomatic_complexity", "max_function_length",
                "cognitive_complexity", "duplicate_block_count",
                "import_fan_out", "todo_fixme_count", "total_lines",
            },
        )
        # Strict count guard — adding a 8th field requires bumping
        # this pin AND the worker's return tuple shape.
        self.assertEqual(len(fields), 7)

    def test_analysis_result_is_frozen(self) -> None:
        r = AnalysisResult(
            outcome=AnalyzeOutcome.OK,
            payload=OpportunityAnalysisPayload(),
            elapsed_ms=1.0,
            worker_elapsed_ms=0.5,
            source_bytes=10,
            caller="t",
            execution_mode=ExecutionMode.INLINE_TINY,
        )
        with self.assertRaises(Exception):
            r.outcome = AnalyzeOutcome.SYNTAX_ERROR  # type: ignore[misc]


# ============================================================================
# Helper AST cage extension — ast.walk + ast.iter_child_nodes only in workers
# ============================================================================


class TestAstWalkCage(unittest.TestCase):
    """11B-fix invariant: ``ast.walk`` + ``ast.iter_child_nodes`` may
    only appear inside functions whose name starts with
    ``_worker_``. Heavy AST traversal happens across the process
    boundary, never on the main asyncio thread."""

    def _find_calls_to(
        self, tree: _ast.Module, attr_names: set,
    ) -> List[tuple]:
        out = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            f = node.func
            if not isinstance(f, _ast.Attribute):
                continue
            if f.attr not in attr_names:
                continue
            if not (
                isinstance(f.value, _ast.Name)
                and f.value.id in {"ast", "_ast_mod", "_ast"}
            ):
                continue
            out.append((node.lineno,))
        return out

    def _enclosing_function(
        self, tree: _ast.Module, lineno: int,
    ) -> str:
        best_name = "<module>"
        best_span = float("inf")
        for node in _ast.walk(tree):
            if not isinstance(
                node, (_ast.FunctionDef, _ast.AsyncFunctionDef),
            ):
                continue
            end = getattr(node, "end_lineno", None) or lineno
            if node.lineno <= lineno <= end:
                span = end - node.lineno
                if span < best_span:
                    best_span = span
                    best_name = node.name
        return best_name

    def test_ast_walk_only_in_workers(self) -> None:
        tree = _parse_module(_HELPER_FILE)
        calls = self._find_calls_to(tree, {"walk"})
        self.assertGreater(
            len(calls), 0, "helper must use ast.walk somewhere",
        )
        for (lineno,) in calls:
            fn_name = self._enclosing_function(tree, lineno)
            self.assertTrue(
                fn_name.startswith("_worker_"),
                f"helper ast.walk at L{lineno} in function "
                f"{fn_name!r} — heavy walks must live in "
                f"_worker_* helpers (process boundary)",
            )

    def test_iter_child_nodes_only_in_workers(self) -> None:
        tree = _parse_module(_HELPER_FILE)
        calls = self._find_calls_to(tree, {"iter_child_nodes"})
        self.assertGreater(
            len(calls), 0,
            "helper must use ast.iter_child_nodes for cognitive complexity",
        )
        for (lineno,) in calls:
            fn_name = self._enclosing_function(tree, lineno)
            self.assertTrue(
                fn_name.startswith("_worker_"),
                f"helper ast.iter_child_nodes at L{lineno} in "
                f"function {fn_name!r} — must live in _worker_* "
                f"helpers (process boundary)",
            )

    def test_default_pool_max_workers_is_one(self) -> None:
        """11B-fix: default to one worker — two CPU-burners can
        starve the parent's I/O on a laptop-class control plane."""
        self.assertEqual(helper_mod._DEFAULT_POOL_MAX_WORKERS, 1)


# ============================================================================
# Helper behavioural tests — exercise real ProcessPoolExecutor workers
# ============================================================================


class TestAnalyzeHelperBehavioural(unittest.IsolatedAsyncioTestCase):

    async def asyncTearDown(self) -> None:
        # Don't tear down the pool between tests (it's a singleton);
        # module-level teardown is enough.
        pass

    async def test_ok_small_source_inline_tiny(self) -> None:
        src = (
            "def f(x):\n"
            "    if x > 0:\n"
            "        for i in range(x):\n"
            "            print(i)\n"
            "    return x\n"
        )
        result = await analyze_python_source_for_opportunity_miner(
            "test.ok_small", src,
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)
        self.assertEqual(
            result.execution_mode, ExecutionMode.INLINE_TINY,
        )
        # Function-length, complexity counters populated.
        self.assertGreater(
            result.payload.cyclomatic_complexity, 1,
        )
        self.assertGreaterEqual(
            result.payload.max_function_length, 5,
        )
        self.assertGreaterEqual(result.payload.total_lines, 5)
        # No ast.AST anywhere on the result (payload is primitives).
        self.assertNotIsInstance(result.payload, _ast.AST)

    async def test_ok_large_source_process_pool(self) -> None:
        # Generate >4KB of valid Python with branching + functions.
        body_lines = [
            f"def f_{i}(x):\n"
            f"    if x > {i}:\n"
            f"        for j in range({i}):\n"
            f"            if j % 2 == 0:\n"
            f"                yield j\n"
            for i in range(300)
        ]
        src = "".join(body_lines)
        self.assertGreater(len(src.encode("utf-8")), 4096)

        result = await analyze_python_source_for_opportunity_miner(
            "test.ok_large", src,
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)
        self.assertEqual(
            result.execution_mode, ExecutionMode.PROCESS,
        )
        # Worker reports its own elapsed time (positive but bounded).
        self.assertGreater(result.worker_elapsed_ms, 0.0)
        self.assertLess(result.worker_elapsed_ms, 30_000.0)
        # Per-dimension sanity.
        self.assertGreater(
            result.payload.cyclomatic_complexity, 100,
        )
        self.assertGreater(result.payload.import_fan_out, -1)
        self.assertGreater(result.payload.total_lines, 100)

    async def test_syntax_error_inline_tiny_fail_closed(self) -> None:
        result = await analyze_python_source_for_opportunity_miner(
            "test.syntax_err_small", "def broken(:\n",
        )
        self.assertEqual(
            result.outcome, AnalyzeOutcome.SYNTAX_ERROR,
        )
        # Zero-value payload on failure.
        self.assertEqual(result.payload, OpportunityAnalysisPayload())
        self.assertIn("SyntaxError", result.error_detail)

    async def test_syntax_error_process_pool_fail_closed(self) -> None:
        large_bad = "\n".join(
            ["def f1(x): return x"] * 500
        ) + "\ndef broken(:\n"
        result = await analyze_python_source_for_opportunity_miner(
            "test.syntax_err_large", large_bad,
        )
        self.assertEqual(
            result.outcome, AnalyzeOutcome.SYNTAX_ERROR,
        )
        self.assertEqual(
            result.execution_mode, ExecutionMode.PROCESS,
        )

    async def test_too_large_fail_closed_without_pool(self) -> None:
        result = await analyze_python_source_for_opportunity_miner(
            "test.too_large", "x = 1\n" * 100, max_bytes=10,
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.TOO_LARGE)
        self.assertIn("max_bytes", result.error_detail)

    async def test_timeout_fail_closed(self) -> None:
        src = "\n".join(f"x_{i} = {i}" for i in range(500))
        result = await analyze_python_source_for_opportunity_miner(
            "test.timeout", src, timeout_s=0.001,
        )
        self.assertEqual(
            result.outcome, AnalyzeOutcome.TIMEOUT,
            f"Expected TIMEOUT for 1ms deadline; got "
            f"{result.outcome.name}",
        )
        self.assertEqual(
            result.execution_mode, ExecutionMode.PROCESS,
        )
        self.assertLess(result.elapsed_ms, 3000.0)


# ============================================================================
# Metrics parity: analyze helper matches legacy _analyze_file
# ============================================================================


class TestMetricsParity(unittest.IsolatedAsyncioTestCase):
    """Acceptance gate: the analyze helper must return byte-equivalent
    metrics to the legacy ``_analyze_file`` for a representative
    real-world source. Drift here would be a behaviour regression."""

    async def test_metrics_match_legacy_analyze_file(self) -> None:
        # Use a real source from the codebase: the helper itself is
        # ~800 LOC with mixed branching + functions + imports +
        # TODOs — broad coverage across all six dimensions.
        source = _HELPER_FILE.read_text()

        # Helper path.
        result = await analyze_python_source_for_opportunity_miner(
            "test.metrics_parity", source,
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)

        # Legacy path (forces sync ast.parse + _analyze_file in the
        # test process; that's fine — we're computing the reference).
        from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (  # noqa: E501
            _analyze_file as _legacy_analyze_file,
        )
        legacy_tree = _ast.parse(source)
        legacy = _legacy_analyze_file(str(_HELPER_FILE), source, legacy_tree)

        p = result.payload
        self.assertEqual(
            p.cyclomatic_complexity, legacy.cyclomatic_complexity,
            "cyclomatic_complexity drift",
        )
        self.assertEqual(
            p.max_function_length, legacy.max_function_length,
            "max_function_length drift",
        )
        self.assertEqual(
            p.cognitive_complexity, legacy.cognitive_complexity,
            "cognitive_complexity drift",
        )
        self.assertEqual(
            p.duplicate_block_count, legacy.duplicate_block_count,
            "duplicate_block_count drift",
        )
        self.assertEqual(
            p.import_fan_out, legacy.import_fan_out,
            "import_fan_out drift",
        )
        self.assertEqual(
            p.todo_fixme_count, legacy.todo_fixme_count,
            "todo_fixme_count drift",
        )
        self.assertEqual(
            p.total_lines, legacy.total_lines,
            "total_lines drift",
        )


# ============================================================================
# OpportunityMiner migration pins
# ============================================================================


class TestOpportunityMinerMigration(unittest.TestCase):
    """11B-fix: scan_once + scan_file must route through
    ``analyze_python_source_for_opportunity_miner``. They must NOT:
      * call ``_analyze_file`` (heavy on-loop walk)
      * touch ``.tree`` on the helper result (no ast.AST consumption)
      * call ``parse_python_source`` (returns ast.AST across IPC)
    """

    def _miner_body(self, method_name: str) -> _ast.AsyncFunctionDef:
        tree = _parse_module(_MINER_FILE)
        for node in _ast.walk(tree):
            if (
                isinstance(node, _ast.AsyncFunctionDef)
                and node.name == method_name
            ):
                return node
        raise AssertionError(f"no async method {method_name!r} found")

    def _find_calls_to_named(
        self, body: _ast.AsyncFunctionDef, name: str,
    ) -> List[int]:
        out = []
        for sub in _ast.walk(body):
            if not isinstance(sub, _ast.Call):
                continue
            f = sub.func
            if isinstance(f, _ast.Name) and f.id == name:
                out.append(sub.lineno)
            elif (
                isinstance(f, _ast.Attribute) and f.attr == name
            ):
                out.append(sub.lineno)
        return out

    def _find_attribute_access(
        self, body: _ast.AsyncFunctionDef, attr: str,
    ) -> List[int]:
        out = []
        for sub in _ast.walk(body):
            if (
                isinstance(sub, _ast.Attribute) and sub.attr == attr
            ):
                out.append(sub.lineno)
        return out

    def test_scan_once_does_not_call_analyze_file(self) -> None:
        body = self._miner_body("scan_once")
        offenders = self._find_calls_to_named(body, "_analyze_file")
        self.assertEqual(
            offenders, [],
            f"scan_once still calls _analyze_file at L{offenders} — "
            f"must use AnalysisResult.payload instead",
        )

    def test_scan_file_does_not_call_analyze_file(self) -> None:
        body = self._miner_body("scan_file")
        offenders = self._find_calls_to_named(body, "_analyze_file")
        self.assertEqual(
            offenders, [],
            f"scan_file still calls _analyze_file at L{offenders} — "
            f"must use AnalysisResult.payload instead",
        )

    def test_scan_once_does_not_use_tree_attribute(self) -> None:
        """No `.tree` attribute access in scan_once — that would
        indicate ast.AST consumption from a parse helper result."""
        body = self._miner_body("scan_once")
        offenders = self._find_attribute_access(body, "tree")
        self.assertEqual(
            offenders, [],
            f"scan_once accesses .tree at L{offenders} — must not "
            f"consume ast.AST from helper",
        )

    def test_scan_file_does_not_use_tree_attribute(self) -> None:
        body = self._miner_body("scan_file")
        offenders = self._find_attribute_access(body, "tree")
        self.assertEqual(
            offenders, [],
            f"scan_file accesses .tree at L{offenders} — must not "
            f"consume ast.AST from helper",
        )

    def test_miner_imports_analyze_helper(self) -> None:
        src = _MINER_FILE.read_text()
        self.assertIn(
            "analyze_python_source_for_opportunity_miner", src,
            "OpportunityMiner must import the 11B-fix analyze helper",
        )
        self.assertIn("AnalyzeOutcome", src)

    def test_miner_does_not_import_parse_python_source(self) -> None:
        """Belt-and-suspenders — the parse-only helper returns
        ast.AST across IPC and is the wrong tool here. Pin against
        accidental regression to the 11B (pre-fix) shape."""
        src = _MINER_FILE.read_text()
        # Search for the actual import name; substring will match
        # even when imported with an alias.
        self.assertNotIn("parse_python_source", src)
        self.assertNotIn("ParseOutcome", src)


# ============================================================================
# Telemetry truth — process mode does NOT emit measure() AST_PARSE records
# ============================================================================


class TestProcessModeTelemetryDoesNotMislead(
    unittest.IsolatedAsyncioTestCase,
):
    """The pre-fix 11B helper wrapped the process-mode await in
    ``measure(CallKind.AST_PARSE)``. That produced
    ``[CompileProvenance] on_loop=True ast_parse`` records whose
    elapsed_ms equalled the IPC roundtrip — misleading operators
    into believing the loop was blocked when in fact the parse ran
    in a worker.

    11B-fix removes that wrap. This test exercises the process
    path and asserts the telemetry ring receives NO new
    ``ast_parse`` record for the call.
    """

    async def test_process_mode_no_ast_parse_provenance_record(
        self,
    ) -> None:
        from backend.core.ouroboros.governance import (
            ast_compile_telemetry as _tel,
        )
        # Force provenance ON for this test (master flag default-true
        # but env-controllable).
        os.environ.pop("JARVIS_COMPILE_PROVENANCE_ENABLED", None)

        # Snapshot the ring before + after; the analyze helper's
        # process path must add zero ast_parse records.
        baseline = len(_tel._ring)

        big = "\n".join(
            f"def f_{i}(x): return x + {i}" for i in range(500)
        )
        self.assertGreater(len(big.encode("utf-8")), 4096)

        result = await analyze_python_source_for_opportunity_miner(
            "test.process_telemetry", big,
        )
        self.assertEqual(result.outcome, AnalyzeOutcome.OK)
        self.assertEqual(
            result.execution_mode, ExecutionMode.PROCESS,
        )

        after = list(_tel._ring)[baseline:]
        # ZERO ast_parse records should land here. The process path
        # emits structured log lines only, not provenance ring
        # entries.
        ast_parse_records = [
            r for r in after
            if r.kind == _tel.CallKind.AST_PARSE
            and r.caller == "test.process_telemetry"
        ]
        self.assertEqual(
            ast_parse_records, [],
            "process-mode analyze must NOT add measure(AST_PARSE) "
            "records — those falsely label IPC roundtrip time as "
            "on-loop ast_parse. Found: "
            f"{[(r.caller, r.elapsed_ms) for r in ast_parse_records]}",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
