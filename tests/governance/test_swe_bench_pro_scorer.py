"""Regression spine - SWE-Bench-Pro Phase C scorer.

Phase C scorer reproduces an evaluation's fix in a fresh isolated
worktree from (captured_patch, problem) alone, runs the failing tests
added by problem.test_patch, and classifies the outcome into a closed
5-value ScoreOutcome taxonomy.

Spine invariants
----------------

  1. All 5 ScoreOutcomes return correctly under stub scenarios:
     PASS / PARTIAL / FAIL / SCORING_ERROR / SKIPPED.
  2. Master-flag OFF returns SKIPPED with diagnostic "master_flag_off".
  3. Non-RESOLVED EvaluationResult returns SKIPPED with the upstream
     outcome stamped into the diagnostic.
  4. Empty captured_patch returns SCORING_ERROR diagnostic "no_patch".
  5. Patches modifying test files return FAIL diagnostic
     "patch_modified_tests:<paths>" by default (canonical SWE-Bench
     cheat-detection); override with reject_test_modifications=False.
  6. prepare_problem failure cascades to SCORING_ERROR diagnostic
     "prepare_failed:<harness_outcome>".
  7. git-apply failure cascades to SCORING_ERROR diagnostic
     "apply_failed:<stderr_tail>".
  8. TestRunner exception cascades to SCORING_ERROR diagnostic
     "test_runner_raised".
  9. All-tests-pass -> PASS with pass_rate=1.0.
 10. Some-pass-some-fail -> PARTIAL with 0 < pass_rate < 1.0.
 11. All-tests-fail -> FAIL with pass_rate=0.0.
 12. cleanup_prepared invoked in finally even on SCORING_ERROR.
 13. ScoringResult schema round-trips (to_dict / from_dict).
 14. Closed 5-value ScoreOutcome taxonomy.

AST pins (composition discipline)
---------------------------------

 15. Scorer composes canonical extract_diff_targets (substring
     presence - drift to a homegrown regex would re-introduce the
     edge-case bugs extract_diff_targets has already solved).
 16. Scorer composes canonical prepare_problem / cleanup_prepared
     / TestRunner.
 17. No while-True polling loop in scorer body.
 18. cleanup_prepared inside try/finally block.
 19. FlagRegistry seeds 3 specs (timeout / reject_test_mods /
     git_op_timeout); reject_test_mods default TRUE per
     canonical SWE-Bench rule.
"""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    MASTER_FLAG_ENV_VAR,
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
    EvaluationResult,
)
from backend.core.ouroboros.governance.swe_bench_pro.per_problem_harness import (
    HarnessOutcome,
    PreparedProblem,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    SCORE_GIT_OP_TIMEOUT_ENV_VAR,
    SCORE_REJECT_TEST_MODS_ENV_VAR,
    SCORE_TEST_TIMEOUT_ENV_VAR,
    ScoreOutcome,
    ScoringResult,
    register_flags,
    score_evaluation,
)
from backend.core.ouroboros.governance.test_runner import TestResult


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _fake_test_result(
    passed_count: int, failed_count: int,
    failed_tests: tuple = (),
) -> TestResult:
    total = passed_count + failed_count
    return TestResult(
        passed=(failed_count == 0 and total > 0),
        total=total,
        failed=failed_count,
        failed_tests=failed_tests,
        duration_seconds=0.5,
        stdout="",
        flake_suspected=False,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    monkeypatch.delenv(SCORE_TEST_TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delenv(SCORE_REJECT_TEST_MODS_ENV_VAR, raising=False)
    monkeypatch.delenv(SCORE_GIT_OP_TIMEOUT_ENV_VAR, raising=False)
    yield


@pytest.fixture
def problem() -> ProblemSpec:
    return ProblemSpec(
        instance_id="bench__fix-001",
        repo="bench/repo",
        base_commit="abc123",
        problem_statement="Fix the broken parser",
        test_patch=(
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
        gold_patch="",
        repo_url="",
    )


@pytest.fixture
def prepared(problem: ProblemSpec, tmp_path: Path) -> PreparedProblem:
    wt = tmp_path / "score-wt"
    wt.mkdir()
    return PreparedProblem(
        problem_instance_id=problem.instance_id,
        worktree_path=wt,
        base_commit=problem.base_commit,
        repo_url=problem.repo_url,
        branch_name="swebp/bench__fix-001",
        target_paths=("tests/test_parser.py", "src/parser.py"),
        elapsed_s=1.0,
    )


@pytest.fixture
def resolved_result() -> EvaluationResult:
    return EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id="bench__fix-001",
        op_id="op-bench-001",
        terminal_state="applied",
        captured_patch=(
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -1 +1 @@\n"
            "-broken\n"
            "+fixed\n"
        ),
    )


@pytest.fixture
def patch_prepare(
    monkeypatch: pytest.MonkeyPatch, prepared: PreparedProblem,
) -> None:
    async def _fake_prepare(_problem):
        return prepared, HarnessOutcome.READY

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "prepare_problem", _fake_prepare,
    )


@pytest.fixture
def patch_cleanup(monkeypatch: pytest.MonkeyPatch) -> Any:
    counter = {"calls": 0}

    async def _fake_cleanup(_prepared):
        counter["calls"] += 1
        return True

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "cleanup_prepared", _fake_cleanup,
    )
    return counter


@pytest.fixture
def patch_apply_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_apply(_wt, _patch, *, timeout_s=None):
        return True, ""

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "_git_apply_patch", _fake_apply,
    )


@pytest.fixture
def patch_apply_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_apply(_wt, _patch, *, timeout_s=None):
        return False, "patch does not apply (line 5)"

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "_git_apply_patch", _fake_apply,
    )


def _patch_test_runner(monkeypatch: pytest.MonkeyPatch, result: TestResult) -> None:
    class _FakeRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _test_files):
            return result

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "TestRunner", _FakeRunner,
    )


# ---------------------------------------------------------------------------
# 1. Master flag OFF -> SKIPPED
# ---------------------------------------------------------------------------


def test_master_flag_off_returns_skipped(
    monkeypatch: pytest.MonkeyPatch, resolved_result: EvaluationResult,
    problem: ProblemSpec,
) -> None:
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.SKIPPED
    assert result.diagnostic == "master_flag_off"
    assert result.problem_instance_id == problem.instance_id


# ---------------------------------------------------------------------------
# 2. Non-RESOLVED EvaluationResult -> SKIPPED
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("upstream", [
    EvaluationOutcome.UNRESOLVED,
    EvaluationOutcome.PREPARE_FAILED,
    EvaluationOutcome.INGEST_FAILED,
    EvaluationOutcome.TERMINAL_TIMEOUT,
    EvaluationOutcome.CANCELLED,
    EvaluationOutcome.MASTER_FLAG_OFF,
])
def test_non_resolved_eval_returns_skipped(
    upstream: EvaluationOutcome,
    env_enabled: None, problem: ProblemSpec,
) -> None:
    eval_result = EvaluationResult(
        outcome=upstream,
        problem_instance_id=problem.instance_id,
    )
    result = asyncio.run(score_evaluation(eval_result, problem))
    assert result.outcome == ScoreOutcome.SKIPPED
    assert f"evaluation_outcome={upstream.value}" in result.diagnostic


# ---------------------------------------------------------------------------
# 3. Empty captured_patch -> SCORING_ERROR
# ---------------------------------------------------------------------------


def test_empty_patch_returns_scoring_error(
    env_enabled: None, problem: ProblemSpec,
) -> None:
    eval_result = EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id=problem.instance_id,
        captured_patch=None,
    )
    result = asyncio.run(score_evaluation(eval_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert result.diagnostic == "no_patch"


def test_whitespace_only_patch_returns_scoring_error(
    env_enabled: None, problem: ProblemSpec,
) -> None:
    eval_result = EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id=problem.instance_id,
        captured_patch="   \n\n",
    )
    result = asyncio.run(score_evaluation(eval_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert result.diagnostic == "no_patch"


# ---------------------------------------------------------------------------
# 4. Cheat detection: patches touching test files -> FAIL
# ---------------------------------------------------------------------------


def test_patch_modifying_tests_returns_fail_by_default(
    env_enabled: None, problem: ProblemSpec,
) -> None:
    cheat_patch = (
        "--- a/tests/test_parser.py\n"
        "+++ b/tests/test_parser.py\n"
        "@@ -1 +1 @@\n"
        "-assert parse(x) == 1\n"
        "+assert True  # cheat\n"
    )
    eval_result = EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id=problem.instance_id,
        captured_patch=cheat_patch,
    )
    result = asyncio.run(score_evaluation(eval_result, problem))
    assert result.outcome == ScoreOutcome.FAIL
    assert "patch_modified_tests" in result.diagnostic
    assert "tests/test_parser.py" in result.diagnostic


def test_reject_test_mods_can_be_disabled_via_argument(
    env_enabled: None, problem: ProblemSpec, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(monkeypatch, _fake_test_result(2, 0))
    cheat_patch = (
        "--- a/tests/test_parser.py\n"
        "+++ b/tests/test_parser.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    eval_result = EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id=problem.instance_id,
        captured_patch=cheat_patch,
    )
    result = asyncio.run(score_evaluation(
        eval_result, problem,
        reject_test_modifications=False,
    ))
    # With cheat-detection off, scorer proceeds to run tests.
    assert result.outcome != ScoreOutcome.FAIL or "patch_modified_tests" not in result.diagnostic


def test_reject_test_mods_can_be_disabled_via_env(
    env_enabled: None, problem: ProblemSpec, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(monkeypatch, _fake_test_result(2, 0))
    monkeypatch.setenv(SCORE_REJECT_TEST_MODS_ENV_VAR, "false")
    cheat_patch = (
        "--- a/tests/test_parser.py\n"
        "+++ b/tests/test_parser.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    eval_result = EvaluationResult(
        outcome=EvaluationOutcome.RESOLVED,
        problem_instance_id=problem.instance_id,
        captured_patch=cheat_patch,
    )
    result = asyncio.run(score_evaluation(eval_result, problem))
    # Env disabled cheat-detection - scorer proceeded.
    assert "patch_modified_tests" not in result.diagnostic


# ---------------------------------------------------------------------------
# 5. prepare_failed cascades -> SCORING_ERROR
# ---------------------------------------------------------------------------


def test_prepare_failed_cascades_to_scoring_error(
    monkeypatch: pytest.MonkeyPatch, env_enabled: None,
    problem: ProblemSpec, resolved_result: EvaluationResult,
) -> None:
    async def _failing_prepare(_problem):
        return None, HarnessOutcome.CLONE_FAILED

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "prepare_problem", _failing_prepare,
    )
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert "prepare_failed:clone_failed" in result.diagnostic


# ---------------------------------------------------------------------------
# 6. apply_failed cascades -> SCORING_ERROR
# ---------------------------------------------------------------------------


def test_apply_failed_cascades_to_scoring_error(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_fail: None,
) -> None:
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert "apply_failed" in result.diagnostic
    # Cleanup STILL runs (worktree was prepared).
    assert patch_cleanup["calls"] == 1


# ---------------------------------------------------------------------------
# 7. PASS - all tests passed
# ---------------------------------------------------------------------------


def test_all_tests_pass_returns_pass(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(monkeypatch, _fake_test_result(3, 0))
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.PASS
    assert result.tests_passed == 3
    assert result.tests_failed == 0
    assert result.tests_total == 3
    assert result.pass_rate == 1.0
    assert patch_cleanup["calls"] == 1


# ---------------------------------------------------------------------------
# 8. PARTIAL - some pass, some fail
# ---------------------------------------------------------------------------


def test_some_tests_pass_returns_partial(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(
        monkeypatch,
        _fake_test_result(2, 3, failed_tests=("t1", "t2", "t3")),
    )
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.PARTIAL
    assert result.tests_passed == 2
    assert result.tests_failed == 3
    assert result.tests_total == 5
    assert 0.0 < result.pass_rate < 1.0
    assert "failed_tests=" in result.diagnostic


# ---------------------------------------------------------------------------
# 9. FAIL - all tests fail
# ---------------------------------------------------------------------------


def test_all_tests_fail_returns_fail(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(
        monkeypatch,
        _fake_test_result(0, 2, failed_tests=("t1", "t2")),
    )
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.FAIL
    assert result.tests_passed == 0
    assert result.tests_failed == 2
    assert result.pass_rate == 0.0


def test_zero_tests_returns_fail(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tests in target_paths that look like test files - the
    earlier 'no_test_files_in_test_patch' branch fires.

    To exercise the zero-total path through the classifier, we
    override the test-file resolver - this branch is the
    SCORING_ERROR/no_test_files path.
    """
    # Replace prepared with target_paths having no test files.
    prepared_no_tests = PreparedProblem(
        problem_instance_id=resolved_result.problem_instance_id,
        worktree_path=Path("/tmp/x"),
        base_commit="abc",
        repo_url="",
        branch_name="b",
        target_paths=("src/foo.py",),
        elapsed_s=0.1,
    )

    async def _prep(_p):
        return prepared_no_tests, HarnessOutcome.READY

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "prepare_problem", _prep,
    )
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert result.diagnostic == "no_test_files_in_test_patch"


# ---------------------------------------------------------------------------
# 10. TestRunner exception -> SCORING_ERROR
# ---------------------------------------------------------------------------


def test_test_runner_exception_returns_scoring_error(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingRunner:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self, _test_files):
            raise RuntimeError("pytest exploded")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.swe_bench_pro.scorer."
        "TestRunner", _ExplodingRunner,
    )
    result = asyncio.run(score_evaluation(resolved_result, problem))
    assert result.outcome == ScoreOutcome.SCORING_ERROR
    assert result.diagnostic == "test_runner_raised"
    # Cleanup ran even after exception.
    assert patch_cleanup["calls"] == 1


# ---------------------------------------------------------------------------
# 11. Cleanup runs in finally for every post-prepare path
# ---------------------------------------------------------------------------


def test_cleanup_runs_after_success(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_ok: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_test_runner(monkeypatch, _fake_test_result(2, 0))
    asyncio.run(score_evaluation(resolved_result, problem))
    assert patch_cleanup["calls"] == 1


def test_cleanup_runs_after_apply_failure(
    env_enabled: None, problem: ProblemSpec,
    resolved_result: EvaluationResult, patch_prepare: None,
    patch_cleanup: Any, patch_apply_fail: None,
) -> None:
    asyncio.run(score_evaluation(resolved_result, problem))
    assert patch_cleanup["calls"] == 1


# ---------------------------------------------------------------------------
# 12. ScoringResult schema roundtrip
# ---------------------------------------------------------------------------


def test_scoring_result_to_dict_from_dict_roundtrip() -> None:
    r = ScoringResult(
        outcome=ScoreOutcome.PASS,
        problem_instance_id="inst-X",
        tests_passed=5,
        tests_failed=0,
        tests_total=5,
        pass_rate=1.0,
        diagnostic="",
        elapsed_s=12.5,
    )
    payload = r.to_dict()
    serialized = json.dumps(payload)
    restored = ScoringResult.from_dict(json.loads(serialized))
    assert restored.outcome == r.outcome
    assert restored.problem_instance_id == r.problem_instance_id
    assert restored.tests_passed == r.tests_passed
    assert restored.tests_total == r.tests_total
    assert restored.pass_rate == r.pass_rate
    assert restored.elapsed_s == r.elapsed_s


# ---------------------------------------------------------------------------
# 13. Closed taxonomy
# ---------------------------------------------------------------------------


def test_score_outcome_closed_five_value_taxonomy() -> None:
    values = {o.value for o in ScoreOutcome}
    assert values == {"pass", "partial", "fail", "scoring_error", "skipped"}


# ---------------------------------------------------------------------------
# 14. AST pins - composition discipline
# ---------------------------------------------------------------------------


def _scorer_source() -> str:
    from backend.core.ouroboros.governance.swe_bench_pro import scorer
    return Path(scorer.__file__).read_text()


def test_ast_pin_imports_extract_diff_targets() -> None:
    """Operator binding: composes canonical extract_diff_targets
    (Treefinement v3.4) for unified-diff parsing - no homegrown regex."""
    src = _scorer_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "repair_tree_production" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "extract_diff_targets":
                        found = True
    assert found, (
        "scorer.py does not import extract_diff_targets - "
        "risk of parallel diff parser"
    )


def test_ast_pin_imports_prepare_and_cleanup_prepared() -> None:
    """Composes canonical B.1 surfaces - no parallel worktree mgmt."""
    src = _scorer_source()
    tree = ast.parse(src)
    needed = {"prepare_problem", "cleanup_prepared"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "per_problem_harness" in (node.module or ""):
                for alias in node.names:
                    needed.discard(alias.name)
    assert not needed, (
        f"scorer.py does not import {sorted(needed)} from "
        f"per_problem_harness"
    )


def test_ast_pin_imports_test_runner() -> None:
    """Composes canonical TestRunner - no parallel pytest invocation."""
    src = _scorer_source()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "test_runner" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "TestRunner":
                        found = True
    assert found, "scorer.py does not import TestRunner"


def test_ast_pin_no_polling_loop_in_scorer() -> None:
    """No while-True loop in scorer body."""
    src = _scorer_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            cond = node.test
            if isinstance(cond, ast.Constant) and cond.value is True:
                raise AssertionError(
                    "scorer.py contains while-True polling loop"
                )


def test_ast_pin_cleanup_in_try_finally_block() -> None:
    """cleanup_prepared MUST run in a finally block so every
    post-prepare path leaves the filesystem clean."""
    src = _scorer_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "score_evaluation":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Try):
                continue
            if not sub.finalbody:
                continue
            finally_text = ast.unparse(ast.Module(
                body=list(sub.finalbody), type_ignores=[],
            ))
            if "cleanup_prepared" in finally_text:
                return
        raise AssertionError(
            "score_evaluation has no try/finally with cleanup_prepared"
        )
    raise AssertionError("score_evaluation not found")


def test_ast_pin_no_naked_asyncio_wait() -> None:
    """Defensive: no bare asyncio.wait(...) without timeout."""
    src = _scorer_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "wait"
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "asyncio"
            ):
                raise AssertionError(
                    "scorer.py calls asyncio.wait(...) - use wait_for"
                )


# ---------------------------------------------------------------------------
# 15. FlagRegistry seeds
# ---------------------------------------------------------------------------


def test_register_flags_seeds_three_specs() -> None:
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    count = register_flags(_Capturer())
    assert count == 3
    names = {s.name for s in captured}
    assert names == {
        SCORE_TEST_TIMEOUT_ENV_VAR,
        SCORE_REJECT_TEST_MODS_ENV_VAR,
        SCORE_GIT_OP_TIMEOUT_ENV_VAR,
    }


def test_register_flags_reject_test_mods_default_true() -> None:
    """Canonical SWE-Bench rule: cheat detection ON by default."""
    captured: list = []

    class _Capturer:
        def register(self, spec) -> None:
            captured.append(spec)

    register_flags(_Capturer())
    reject_spec = next(
        s for s in captured if s.name == SCORE_REJECT_TEST_MODS_ENV_VAR
    )
    assert reject_spec.default is True


def test_register_flags_never_raises_on_capturer_failure() -> None:
    class _Boom:
        def register(self, spec) -> None:
            raise RuntimeError("kaboom")

    assert register_flags(_Boom()) == 0


# ---------------------------------------------------------------------------
# 16. Timeout env override
# ---------------------------------------------------------------------------


def test_test_timeout_env_resolves_via_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
        _resolve_test_timeout_s,
    )
    monkeypatch.setenv(SCORE_TEST_TIMEOUT_ENV_VAR, "45")
    assert _resolve_test_timeout_s(None) == 45.0
    # Explicit argument wins over env.
    assert _resolve_test_timeout_s(90.0) == 90.0


def test_test_timeout_env_invalid_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
        _resolve_test_timeout_s, _DEFAULT_TEST_TIMEOUT_S,
    )
    monkeypatch.setenv(SCORE_TEST_TIMEOUT_ENV_VAR, "not-a-number")
    assert _resolve_test_timeout_s(None) == _DEFAULT_TEST_TIMEOUT_S
