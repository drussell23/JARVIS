"""Tests for TestWatcher — pytest polling and stable failure detection.

Validates that the TestWatcher correctly:
1. Parses pytest output for FAILED lines
2. Returns empty list on exit_code == 0
3. Requires two consecutive failures for stability
4. Resets streak on passing tests
5. Runs pytest as a subprocess
6. Extracts file paths from test IDs
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_PYTEST_OUTPUT = textwrap.dedent("""\
    tests/test_utils.py::test_edge_case PASSED
    tests/test_utils.py::test_boundary PASSED
    FAILED tests/test_core.py::test_parse - AssertionError: expected 3, got 4
    FAILED tests/test_net.py::test_timeout - TimeoutError: connection timed out
    2 failed, 2 passed in 1.23s
""")


# ---------------------------------------------------------------------------
# 1. test_parse_pytest_output_detects_failures
# ---------------------------------------------------------------------------

class TestParsePytestOutputDetectsFailures:
    """Parsing real FAILED lines should yield TestFailure objects."""

    def test_detects_two_failures(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=1)
        assert len(failures) == 2

        ids = {f.test_id for f in failures}
        assert "tests/test_core.py::test_parse" in ids
        assert "tests/test_net.py::test_timeout" in ids

    def test_failure_fields_populated(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=1)
        core_fail = next(f for f in failures if "test_core" in f.test_id)
        assert core_fail.file_path == "tests/test_core.py"
        assert "AssertionError" in core_fail.error_text


# ---------------------------------------------------------------------------
# 2. test_parse_pytest_output_no_failures
# ---------------------------------------------------------------------------

class TestParsePytestOutputNoFailures:
    """exit_code == 0 should always produce an empty failure list."""

    def test_returns_empty_on_exit_code_zero(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        # Even if the output *looks* like failures, exit_code=0 overrides.
        failures = watcher.parse_pytest_output(_SAMPLE_PYTEST_OUTPUT, exit_code=0)
        assert failures == []


# ---------------------------------------------------------------------------
# 3. test_stability_requires_two_consecutive_failures
# ---------------------------------------------------------------------------

class TestStabilityRequiresTwoConsecutiveFailures:
    """A test must fail in two consecutive runs to be declared stable."""

    def test_first_failure_not_stable(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        signals = watcher.process_failures(failures)
        assert len(signals) == 0

    def test_second_consecutive_failure_is_stable(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        failures = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        # First run — not stable yet
        watcher.process_failures(failures)
        # Second run — same test fails again → stable
        signals = watcher.process_failures(failures)
        assert len(signals) == 1
        assert signals[0].stable is True
        assert signals[0].source == "intent:test_failure"


# ---------------------------------------------------------------------------
# 4. test_stability_resets_on_pass
# ---------------------------------------------------------------------------

class TestStabilityResetsOnPass:
    """Streak resets when a test passes, so subsequent failure is not stable."""

    def test_pass_resets_streak(self) -> None:
        watcher = TestWatcher(repo="jarvis")
        fail = [
            TestFailure(
                test_id="tests/test_core.py::test_parse",
                file_path="tests/test_core.py",
                error_text="AssertionError: bad",
            ),
        ]
        # Run 1: fail → streak = 1
        watcher.process_failures(fail)
        # Run 2: pass (empty list) → streak resets
        watcher.process_failures([])
        # Run 3: fail again → streak = 1 (not 2), so NOT stable
        signals = watcher.process_failures(fail)
        assert len(signals) == 0


# ---------------------------------------------------------------------------
# 5. test_run_pytest_subprocess
# ---------------------------------------------------------------------------

class TestRunPytestSubprocess:
    """Actually invoke pytest on a trivial test file and verify exit_code."""

    @pytest.mark.asyncio
    async def test_trivial_passing_test(self, tmp_path: Path) -> None:
        # Create a minimal test file that passes
        test_file = tmp_path / "test_trivial.py"
        test_file.write_text("def test_one():\n    assert 1 + 1 == 2\n")

        watcher = TestWatcher(
            repo="jarvis",
            test_dir=str(tmp_path),
            repo_path=str(tmp_path),
        )
        output, exit_code = await watcher.run_pytest()
        assert exit_code == 0
        assert "passed" in output.lower()


# ---------------------------------------------------------------------------
# 6. test_extracts_file_path_from_test_id
# ---------------------------------------------------------------------------

class TestExtractsFilePathFromTestId:
    """extract_file() splits on :: and returns the first component."""

    def test_simple_test_id(self) -> None:
        result = TestWatcher.extract_file("tests/test_utils.py::test_edge_case")
        assert result == "tests/test_utils.py"

    def test_class_method_test_id(self) -> None:
        result = TestWatcher.extract_file(
            "tests/test_utils.py::TestClass::test_method"
        )
        assert result == "tests/test_utils.py"


# ---------------------------------------------------------------------------
# Repair Context Bridge (Slice 1) — traceback enrichment wiring
# ---------------------------------------------------------------------------

import textwrap as _textwrap  # noqa: E402

_TB_OUTPUT = _textwrap.dedent("""\
    =================================== FAILURES ===================================
    _______________________________ test_parse ____________________________________
    tests/test_core.py:12: in test_parse
        assert parse("x") == 3
    src/calc.py:42: in parse
        raise ValueError("boom")
    E   ValueError: boom
    =========================== short test summary info ============================
    FAILED tests/test_core.py::test_parse - ValueError: boom
""")


class _FakeResolver:
    """GraphBackend-shaped fake: maps src/calc.py:42 -> a 'parse' node."""

    def nodes_in_file(self, file_path):  # noqa: ANN001, ANN201
        if file_path == "src/calc.py":
            return ["src/calc.py::parse"]
        return []

    def get_node(self, key):  # noqa: ANN001, ANN201
        if key == "src/calc.py::parse":
            return {"node_id": {"line_number": 40}, "line_count": 10}
        return None


def _make_failure() -> TestFailure:
    return TestFailure(
        test_id="tests/test_core.py::test_parse",
        file_path="tests/test_core.py",
        error_text="ValueError: boom",
    )


class TestBridgeOffByteIdentical:
    """With the bridge flag OFF, enrichment is a no-op and the signal evidence
    is byte-identical to pre-bridge behavior (no traceback keys)."""

    @pytest.mark.asyncio
    async def test_off_leaves_evidence_unenriched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # graduated default-ON → set the kill-switch explicitly to exercise the OFF path
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "false")
        watcher = TestWatcher(repo="jarvis", node_resolver=_FakeResolver())
        f = _make_failure()
        await watcher._enrich_failures([f], _TB_OUTPUT)
        assert f.traceback_evidence is None  # untouched
        # streak 2 → stable signal carries only the legacy evidence keys
        watcher.process_failures([f])
        signals = watcher.process_failures([_make_failure()])
        ev = signals[0].evidence
        assert set(ev) == {"signature", "test_id", "streak", "error_text"}


class TestBridgeOnEnriches:
    """With the bridge ON, the failure is enriched and the keys flow into the
    emitted signal's evidence additively."""

    @pytest.mark.asyncio
    async def test_on_maps_traceback_to_node(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")
        watcher = TestWatcher(repo="jarvis", node_resolver=_FakeResolver())
        f = _make_failure()
        await watcher._enrich_failures([f], _TB_OUTPUT)
        assert f.traceback_evidence is not None
        assert "src/calc.py::parse" in f.traceback_evidence["fault_node_keys"]

    @pytest.mark.asyncio
    async def test_enriched_keys_merge_into_signal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")
        watcher = TestWatcher(repo="jarvis", node_resolver=_FakeResolver())
        # streak 1 (not stable yet) — enrich the second-run failure
        watcher.process_failures([_make_failure()])
        f2 = _make_failure()
        await watcher._enrich_failures([f2], _TB_OUTPUT)
        signals = watcher.process_failures([f2])
        assert len(signals) == 1
        ev = signals[0].evidence
        assert "fault_node_keys" in ev and "traceback_frames" in ev
        assert ev["signature"] == "ValueError: boom:tests/test_core.py"  # legacy intact


class TestBridgeFailSoft:
    """Enrichment must never raise — a broken resolver degrades to None."""

    @pytest.mark.asyncio
    async def test_broken_resolver_degrades(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "true")

        class _Boom:
            def nodes_in_file(self, file_path):  # noqa: ANN001, ANN201
                raise RuntimeError("backend down")

            def get_node(self, key):  # noqa: ANN001, ANN201
                raise RuntimeError("backend down")

        watcher = TestWatcher(repo="jarvis", node_resolver=_Boom())
        f = _make_failure()
        await watcher._enrich_failures([f], _TB_OUTPUT)  # must not raise
        # frames still parse + record (in_repo) even though node mapping failed
        assert f.traceback_evidence is not None
        assert f.traceback_evidence["fault_node_keys"] == []
