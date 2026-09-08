"""VERIFY judges the candidate by what it changes.

After the differential VALIDATE let a correct production candidate through,
VERIFY rolled it back as ``pass_rate=0.00``: the benchmark ran pytest on the
production file itself (collects nothing), under the repo's ``backend/
pytest.ini`` addopts (``--cov``, ``-n auto`` — plugins this venv lacks,
exit 4), and without the ambient verdict VALIDATE had just established.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import differential_validation as DV
from backend.core.ouroboros.governance import test_subprocess_helper as TSH
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.patch_benchmarker import PatchBenchmarker


def _tree(root: Path) -> None:
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "probe.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tests" / "test_probe.py").write_text("from pkg.probe import f\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_the_benchmark_runs_the_tests_that_cover_a_production_target(tmp_path, monkeypatch):
    _tree(tmp_path)
    seen = {}

    def _fake_pytest(argv, *, cwd=None, timeout_s, caller, env=None, output_cap_chars=None):
        seen["argv"] = list(argv); seen["cwd"] = cwd
        return SimpleNamespace(stdout="1 passed in 0.01s", returncode=0)
    monkeypatch.setattr(TSH, "run_pytest_subprocess_sync", _fake_pytest)
    ctx = OperationContext.create(description="x", target_files=("pkg/probe.py",)).with_ambient_red_tests(
        ("tests/test_other.py::test_ambient", ""))
    bench = PatchBenchmarker(project_root=tmp_path, timeout_s=10.0)
    cov, pass_rate = await bench._run_coverage(["pkg/probe.py"], ctx=ctx)
    argv = seen["argv"]
    assert pass_rate == 1.0
    assert list(TSH.PYTEST_ISOLATION_ARGS) == argv[3:5], "inherited addopts neutralised, like the TestRunner"
    assert "--deselect=tests/test_other.py::test_ambient" in argv
    assert argv[-1].endswith("tests/test_probe.py"), argv
    assert "pkg/probe.py" not in argv, "a production file is never handed to pytest as a target"


@pytest.mark.asyncio
async def test_a_test_module_target_is_its_own_target(tmp_path, monkeypatch):
    _tree(tmp_path)
    seen = {}

    def _fake_pytest(argv, **kw):
        seen["argv"] = list(argv); return SimpleNamespace(stdout="1 passed", returncode=0)
    monkeypatch.setattr(TSH, "run_pytest_subprocess_sync", _fake_pytest)
    bench = PatchBenchmarker(project_root=tmp_path, timeout_s=10.0)
    await bench._run_coverage(["tests/test_probe.py"])
    assert seen["argv"][-1] == "tests/test_probe.py" and "--deselect" not in " ".join(seen["argv"])


def test_the_context_baseline_is_validates_verdict():
    ctx = OperationContext.create(description="fix test_keep", target_files=("pkg/probe.py",)).with_ambient_red_tests(
        ("tests/t.py::test_old", "tests/t.py::test_keep"))
    assert ctx.ambient_red_tests == ("tests/t.py::test_old", "tests/t.py::test_keep")
    ar = SimpleNamespace(passed=False, test_result=SimpleNamespace(failed_tests=("tests/t.py::test_old",), timed_out=False), adapter="python")
    multi = SimpleNamespace(passed=False, adapter_results=(ar,), dominant_failure=ar)
    # dataclasses.replace needs a dataclass — reuse the module's own test shape
    import dataclasses as _dc

    @_dc.dataclass
    class _AR:
        passed: bool; test_result: object; adapter: str = "python"

    @_dc.dataclass
    class _Multi:
        passed: bool; adapter_results: tuple; dominant_failure: object = None
    m = _Multi(False, (_AR(False, SimpleNamespace(failed_tests=("tests/t.py::test_old",), timed_out=False)),))
    out, ignored = DV.apply_context_baseline(m, ctx)
    assert out.passed and ignored == ("tests/t.py::test_old",)
    kept = _Multi(False, (_AR(False, SimpleNamespace(failed_tests=("tests/t.py::test_keep",), timed_out=False)),))
    assert DV.apply_context_baseline(kept, ctx) == (kept, ()), "an acceptance test is never excused"
    bare = OperationContext.create(description="x", target_files=("pkg/probe.py",))
    assert DV.apply_context_baseline(kept, bare) == (kept, ())


def test_both_verify_sites_read_the_verdict_and_validate_stamps_it():
    import inspect
    from backend.core.ouroboros.governance import orchestrator, test_runner
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner
    for mod in (orchestrator, slice4b_runner):
        assert "_apply_ctx_baseline(_multi, ctx)" in inspect.getsource(mod), mod.__name__
    assert "ambient_red_tests=tuple(_dv_ignored)" in inspect.getsource(orchestrator), "VALIDATE hands VERIFY its verdict on the ValidationResult"
    assert "*PYTEST_ISOLATION_ARGS" in inspect.getsource(test_runner)


def test_the_verdict_rides_the_validation_result():
    """The validate core returns a ValidationResult, not the ctx it stamps;
    the ids reach VERIFY through ctx.validation."""
    import inspect
    from backend.core.ouroboros.governance import orchestrator
    from backend.core.ouroboros.governance.op_context import OperationPhase, ValidationResult
    assert "ambient_red_tests=tuple(_dv_ignored)" in inspect.getsource(orchestrator.GovernedOrchestrator._run_validation_core)
    vr = ValidationResult(passed=True, best_candidate={}, validation_duration_s=0.1, error=None,
                          ambient_red_tests=("tests/t.py::test_old",))
    ctx = OperationContext.create(description="x", target_files=("pkg/probe.py",))
    import dataclasses as _dc
    ctx = _dc.replace(ctx, validation=vr)
    assert DV.ambient_red_ids(ctx) == ("tests/t.py::test_old",)
    assert DV.ambient_red_ids(ctx.with_ambient_red_tests(("tests/t.py::explicit",))) == ("tests/t.py::explicit",)
    assert DV.ambient_red_ids(OperationContext.create(description="x", target_files=("a.py",))) == ()
