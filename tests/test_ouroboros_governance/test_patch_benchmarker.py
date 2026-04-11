"""Tests for PatchBenchmarker."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.patch_benchmarker import (
    BenchmarkResult,
    PatchBenchmarker,
    _compute_patch_hash,
    _filter_python_files,
    _infer_task_type,
    _is_python_file,
)
from backend.core.ouroboros.governance.op_context import OperationContext


def _make_ctx(description="improve auth logic", target_files=(), pre_apply_snapshots=None):
    ctx = MagicMock(spec=OperationContext)
    ctx.description = description
    ctx.target_files = target_files
    ctx.pre_apply_snapshots = pre_apply_snapshots or {}
    ctx.op_id = "op-test-001"
    return ctx


class TestInferTaskType:
    def test_test_in_description(self):
        assert _infer_task_type("add unit tests for auth", ()) == "testing"

    def test_file_under_tests_dir(self):
        assert _infer_task_type("improve logic", ("tests/test_foo.py",)) == "testing"

    def test_refactor_in_description(self):
        assert _infer_task_type("refactor the auth module", ()) == "refactoring"

    def test_bug_fix(self):
        assert _infer_task_type("fix null pointer bug", ()) == "bug_fix"

    def test_security(self):
        assert _infer_task_type("security patch for token validation", ()) == "security"

    def test_performance(self):
        assert _infer_task_type("optimize hot path", ()) == "performance"

    def test_default(self):
        assert _infer_task_type("update auth module", ()) == "code_improvement"

    def test_priority_order_test_beats_refactor(self):
        assert _infer_task_type("refactor tests", ()) == "testing"


class TestComputePatchHash:
    def test_deterministic(self):
        h1 = _compute_patch_hash({"a.py": "x", "b.py": "y"})
        h2 = _compute_patch_hash({"b.py": "y", "a.py": "x"})
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _compute_patch_hash({"a.py": "x"})
        h2 = _compute_patch_hash({"a.py": "y"})
        assert h1 != h2

    def test_returns_hex_string(self):
        h = _compute_patch_hash({"a.py": "x"})
        assert len(h) == 64
        int(h, 16)  # must be valid hex


class TestBenchmarkNeverRaises:
    async def test_benchmark_returns_result_when_tools_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)
            assert 0.0 <= result.quality_score <= 1.0

    async def test_benchmark_returns_on_subprocess_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("nonexistent_file.py",))
            # Must not raise
            result = await benchmarker.benchmark(ctx)
            assert isinstance(result, BenchmarkResult)

    async def test_timed_out_flag_set_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=0.001)
            ctx = _make_ctx()
            result = await benchmarker.benchmark(ctx)
            # With near-zero timeout, at least one step should time out
            assert isinstance(result, BenchmarkResult)
            # timed_out may or may not be set depending on OS timing, but must not raise


class TestCoveragePassRateNAGuard:
    """Regression: pytest collecting 0 tests is N/A, not pass_rate=0.0.

    bt-2026-04-11-213801 / op-019d7e7d (requirements.txt) reached APPLY but
    was rolled back because PatchBenchmarker treated `pytest -> exit 5
    (no tests collected)` as `pass_rate=0.0`, tripping the verify_regression
    gate (`pass_rate < threshold=1.00`). This must mirror the orchestrator
    scoped-verify N/A guard at orchestrator.py `_verify_test_total == 0`.
    """

    def test_exit_code_5_treated_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            fake_completed = MagicMock()
            fake_completed.returncode = 5  # pytest "no tests collected"
            fake_completed.stdout = "collected 0 items\n\n"
            fake_completed.stderr = ""
            with patch("subprocess.run", return_value=fake_completed):
                _cov, pass_rate = benchmarker._coverage_sync(["requirements.txt"])
            assert pass_rate == 1.0, "exit 5 must be N/A=pass, not regression"

    def test_no_tests_ran_string_treated_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            fake_completed = MagicMock()
            fake_completed.returncode = 1
            fake_completed.stdout = "no tests ran in 0.01s\n"
            fake_completed.stderr = ""
            with patch("subprocess.run", return_value=fake_completed):
                _cov, pass_rate = benchmarker._coverage_sync(["docs/foo.md"])
            assert pass_rate == 1.0

    def test_real_failure_still_zero(self):
        """Sanity: a real test failure must still report pass_rate < 1.0."""
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            fake_completed = MagicMock()
            fake_completed.returncode = 1
            fake_completed.stdout = "3 passed, 2 failed in 1.5s\n"
            fake_completed.stderr = ""
            with patch("subprocess.run", return_value=fake_completed):
                _cov, pass_rate = benchmarker._coverage_sync(["foo.py"])
            assert pass_rate == pytest.approx(3 / 5)


class TestQualityScoreFormula:
    def test_perfect_scores(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=1.0, radon_available=True)
        expected = 0.45 * 1.0 + 0.45 * 1.0 + 0.10 * 1.0
        assert abs(score - expected) < 1e-6

    def test_radon_unavailable_redistributes_weight(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=1.0, coverage_score=1.0, complexity_score=0.0, radon_available=False)
        # Weights: lint=0.50, coverage=0.50, complexity ignored
        assert abs(score - 1.0) < 1e-6

    def test_scores_clamped_to_0_1(self):
        from backend.core.ouroboros.governance.patch_benchmarker import _compute_quality_score
        score = _compute_quality_score(lint_score=2.0, coverage_score=-1.0, complexity_score=0.5, radon_available=True)
        assert 0.0 <= score <= 1.0


class TestIsPythonFile:
    def test_py_extension(self):
        assert _is_python_file("foo.py") is True

    def test_pyi_stub(self):
        assert _is_python_file("typings/foo.pyi") is True

    def test_uppercase_extension(self):
        assert _is_python_file("FOO.PY") is True

    def test_requirements_txt_not_python(self):
        assert _is_python_file("requirements.txt") is False

    def test_yaml_not_python(self):
        assert _is_python_file("config.yaml") is False

    def test_json_not_python(self):
        assert _is_python_file("package.json") is False

    def test_md_not_python(self):
        assert _is_python_file("README.md") is False

    def test_toml_not_python(self):
        assert _is_python_file("pyproject.toml") is False

    def test_no_extension_not_python(self):
        assert _is_python_file("Makefile") is False

    def test_pyc_compiled_not_python_source(self):
        # .pyc is bytecode, not source — must not be linted/tested
        assert _is_python_file("foo.pyc") is False


class TestFilterPythonFiles:
    def test_filters_out_non_python(self):
        result = _filter_python_files(["foo.py", "requirements.txt", "bar.py"])
        assert result == ["foo.py", "bar.py"]

    def test_preserves_order(self):
        result = _filter_python_files(["b.py", "a.py", "config.yaml", "c.py"])
        assert result == ["b.py", "a.py", "c.py"]

    def test_empty_input(self):
        assert _filter_python_files([]) == []

    def test_all_filtered(self):
        assert _filter_python_files(["requirements.txt", "Makefile", "README.md"]) == []

    def test_all_kept(self):
        assert _filter_python_files(["a.py", "b.py", "c.pyi"]) == ["a.py", "b.py", "c.pyi"]


class TestNonPythonTargetSkip:
    """Root-cause regression suite for the non-Python target skip path.

    PatchBenchmarker MUST skip pytest/ruff/radon entirely when target_files
    contains zero Python files. The bug this guards against:

      `pytest --cov=requirements.txt` does NOT filter test discovery — it
      collects the FULL project test suite. Unrelated failing tests in
      the project produce `pass_rate < 1.0`, which trips the verify_gate
      regression check (`_MIN_PASS_RATE=1.0`) and rolls back a perfectly
      good `requirements.txt` upgrade.

    This is the deterministic counterpart to the orchestrator scoped-verify
    N/A guard at orchestrator.py `_verify_test_total == 0 →
    _verify_test_passed = True`.

    Ref: bt-2026-04-11-213801 / op-019d7e7d (requirements.txt) blocked
    the first sustained APPLY here on 2026-04-11.
    """

    async def test_requirements_txt_only_returns_pass_rate_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "requirements.txt").write_text("numpy==1.0\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("requirements.txt",))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0, (
                    "subprocess.run must NEVER be invoked for non-Python "
                    "targets — pytest/ruff/radon are skipped at the _run "
                    "short-circuit"
                )
            assert result.pass_rate == 1.0
            assert result.non_python_target is True
            assert result.error is None
            assert result.timed_out is False

    async def test_yaml_only_returns_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.yaml").write_text("key: value\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("config.yaml",))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0
            assert result.pass_rate == 1.0
            assert result.non_python_target is True

    async def test_md_only_returns_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "README.md").write_text("# Title\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("README.md",))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0
            assert result.pass_rate == 1.0
            assert result.non_python_target is True

    async def test_json_only_returns_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text('{"k": 1}\n')
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("package.json",))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0
            assert result.pass_rate == 1.0
            assert result.non_python_target is True

    async def test_toml_only_returns_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text("[tool]\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("pyproject.toml",))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0
            assert result.pass_rate == 1.0
            assert result.non_python_target is True

    async def test_multiple_non_python_files_skipped_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "requirements.txt").write_text("a\n")
            (Path(tmp) / "config.yaml").write_text("k: v\n")
            (Path(tmp) / "README.md").write_text("# x\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("requirements.txt", "config.yaml", "README.md"))
            with patch("subprocess.run") as mock_run:
                result = await benchmarker.benchmark(ctx)
                assert mock_run.call_count == 0
            assert result.non_python_target is True
            assert result.pass_rate == 1.0
            assert result.lint_violations == 0
            assert result.complexity_delta == 0.0
            assert result.coverage_pct == 0.0

    async def test_quality_score_is_one_for_non_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("requirements.txt",))
            result = await benchmarker.benchmark(ctx)
            assert result.quality_score == 1.0, (
                "non-Python targets must score perfect — they passed the "
                "Python toolchain by virtue of not being subject to it"
            )

    async def test_pyi_stub_treated_as_python(self):
        """Type stubs (.pyi) ARE Python and must NOT short-circuit."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "stub.pyi").write_text("def f() -> int: ...\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("stub.pyi",))
            # Mock subprocess.run to avoid actually invoking pytest/ruff/radon
            fake = MagicMock()
            fake.returncode = 5
            fake.stdout = "collected 0 items\n"
            fake.stderr = ""
            with patch("subprocess.run", return_value=fake):
                result = await benchmarker.benchmark(ctx)
            # Sub-runners SHOULD have been invoked (subprocess called > 0)
            # Concretely, non_python_target must remain False:
            assert result.non_python_target is False

    async def test_mixed_targets_runs_python_subset(self):
        """['foo.py', 'requirements.txt'] must NOT short-circuit — the .py
        file still gets benchmarked. Hash & runner inputs reflect filtering."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo.py").write_text("def x(): pass\n")
            (Path(tmp) / "requirements.txt").write_text("numpy\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("foo.py", "requirements.txt"))
            fake = MagicMock()
            fake.returncode = 0
            fake.stdout = "1 passed in 0.01s\n"
            fake.stderr = ""
            with patch("subprocess.run", return_value=fake):
                result = await benchmarker.benchmark(ctx)
            # Mixed → NOT short-circuited
            assert result.non_python_target is False

    async def test_short_circuit_does_not_invoke_lint(self):
        """Layered defense: even if subprocess wasn't fully patched,
        _run_lint must not be called for non-Python only targets."""
        with tempfile.TemporaryDirectory() as tmp:
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("requirements.txt",))
            with patch.object(benchmarker, "_run_lint", new=AsyncMock()) as mock_lint, \
                 patch.object(benchmarker, "_run_coverage", new=AsyncMock()) as mock_cov, \
                 patch.object(benchmarker, "_run_complexity", new=AsyncMock()) as mock_cx:
                await benchmarker.benchmark(ctx)
                mock_lint.assert_not_called()
                mock_cov.assert_not_called()
                mock_cx.assert_not_called()

    async def test_python_only_invokes_all_runners(self):
        """Sanity inverse: pure Python targets DO invoke all three runners."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo.py").write_text("def x(): pass\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("foo.py",))
            with patch.object(benchmarker, "_run_lint", new=AsyncMock(return_value=(0, 1.0))) as mock_lint, \
                 patch.object(benchmarker, "_run_coverage", new=AsyncMock(return_value=(80.0, 1.0))) as mock_cov, \
                 patch.object(benchmarker, "_run_complexity", new=AsyncMock(return_value=(0.0, True))) as mock_cx:
                result = await benchmarker.benchmark(ctx)
                mock_lint.assert_called_once()
                mock_cov.assert_called_once()
                mock_cx.assert_called_once()
            assert result.non_python_target is False
            # The .py file is what got passed to each runner
            assert mock_lint.call_args.args[0] == ["foo.py"]
            assert mock_cov.call_args.args[0] == ["foo.py"]
            assert mock_cx.call_args.args[0] == ["foo.py"]

    async def test_mixed_targets_runners_receive_python_only(self):
        """Mixed [foo.py, requirements.txt]: runners get ['foo.py'], NOT both."""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo.py").write_text("def x(): pass\n")
            (Path(tmp) / "requirements.txt").write_text("numpy\n")
            benchmarker = PatchBenchmarker(project_root=Path(tmp), timeout_s=5.0)
            ctx = _make_ctx(target_files=("foo.py", "requirements.txt"))
            with patch.object(benchmarker, "_run_lint", new=AsyncMock(return_value=(0, 1.0))) as mock_lint, \
                 patch.object(benchmarker, "_run_coverage", new=AsyncMock(return_value=(80.0, 1.0))) as mock_cov, \
                 patch.object(benchmarker, "_run_complexity", new=AsyncMock(return_value=(0.0, True))) as mock_cx:
                await benchmarker.benchmark(ctx)
            assert mock_lint.call_args.args[0] == ["foo.py"]
            assert mock_cov.call_args.args[0] == ["foo.py"]
            assert mock_cx.call_args.args[0] == ["foo.py"]


class TestNonPythonTargetVerifyGate:
    """verify_gate must respect the non_python_target sentinel — its job
    is to delegate non-Python verification to InfraApplicator and the
    orchestrator scoped-verify path. None of the threshold checks
    (pass_rate, coverage, complexity, lint) carry signal in this case."""

    def _build(
        self,
        *,
        pass_rate: float = 1.0,
        lint_violations: int = 0,
        coverage_pct: float = 0.0,
        complexity_delta: float = 0.0,
        patch_hash: str = "abc",
        quality_score: float = 1.0,
        task_type: str = "code_improvement",
        timed_out: bool = False,
        error=None,
        non_python_target: bool = True,
    ):
        return BenchmarkResult(
            pass_rate=pass_rate,
            lint_violations=lint_violations,
            coverage_pct=coverage_pct,
            complexity_delta=complexity_delta,
            patch_hash=patch_hash,
            quality_score=quality_score,
            task_type=task_type,
            timed_out=timed_out,
            error=error,
            non_python_target=non_python_target,
        )

    def test_non_python_target_passes_gate(self):
        from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
        result = self._build()
        assert enforce_verify_thresholds(result, baseline_coverage=None) is None

    def test_non_python_target_passes_with_high_baseline_coverage(self):
        """Even with a high baseline (which would otherwise trip on
        coverage_pct=0.0), non_python_target must skip the check."""
        from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
        result = self._build(coverage_pct=0.0)
        assert enforce_verify_thresholds(result, baseline_coverage=85.0) is None

    def test_non_python_target_still_blocked_by_explicit_error(self):
        """non_python_target does NOT bypass the error field — if the
        benchmark itself crashed, that's still a hard fail."""
        from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
        result = self._build(error="benchmark crashed")
        assert enforce_verify_thresholds(result, baseline_coverage=None) is not None

    def test_non_python_target_still_blocked_by_timeout(self):
        """non_python_target does NOT bypass timed_out — though in
        practice the short-circuit prevents this combo from arising."""
        from backend.core.ouroboros.governance.verify_gate import enforce_verify_thresholds
        result = self._build(timed_out=True)
        assert enforce_verify_thresholds(result, baseline_coverage=None) is not None

    def test_python_target_default_field_value(self):
        """BenchmarkResult constructed without non_python_target defaults
        to False — backward compatible with existing call sites."""
        result = BenchmarkResult(
            pass_rate=1.0,
            lint_violations=0,
            coverage_pct=80.0,
            complexity_delta=0.0,
            patch_hash="x",
            quality_score=0.9,
            task_type="code_improvement",
            timed_out=False,
            error=None,
        )
        assert result.non_python_target is False
