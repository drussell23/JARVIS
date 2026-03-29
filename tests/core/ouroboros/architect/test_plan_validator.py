"""
Tests for PlanValidator — 10 deterministic structural rules.

TDD: tests are written before the implementation.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.plan_validator import PlanValidator, ValidationResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _step(
    index: int,
    repo: str = "jarvis",
    target_paths: tuple[str, ...] = ("backend/foo.py",),
    depends_on: tuple[int, ...] = (),
) -> PlanStep:
    return PlanStep(
        step_index=index,
        description=f"Step {index}",
        intent_kind=StepIntentKind.MODIFY_FILE,
        target_paths=target_paths,
        repo=repo,
        depends_on=depends_on,
    )


def _plan(
    steps: tuple[PlanStep, ...],
    repos_affected: tuple[str, ...] = ("jarvis",),
    acceptance_checks: tuple[AcceptanceCheck, ...] = (),
) -> ArchitecturalPlan:
    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-001",
        parent_hypothesis_fingerprint="fp-001",
        title="Test Plan",
        description="A test plan",
        repos_affected=repos_affected,
        non_goals=(),
        steps=steps,
        acceptance_checks=acceptance_checks,
        model_used="test-model",
    )


# ---------------------------------------------------------------------------
# Rule 1: at least one step
# ---------------------------------------------------------------------------


def test_empty_plan_fails() -> None:
    plan = _plan(steps=())
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("empty" in r.lower() or "least one step" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 2: step count <= max_steps
# ---------------------------------------------------------------------------


def test_too_many_steps_fails() -> None:
    steps = tuple(_step(i) for i in range(11))
    plan = _plan(steps=steps)
    result = PlanValidator(max_steps=10).validate(plan)
    assert result.passed is False
    assert any("max" in r.lower() or "exceed" in r.lower() or "too many" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 3: no duplicate step indices
# ---------------------------------------------------------------------------


def test_duplicate_step_index_fails() -> None:
    steps = (
        _step(0),
        _step(0),  # duplicate
        _step(1),
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("duplicate" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 4: indices are 0..N-1 with no gaps
# ---------------------------------------------------------------------------


def test_orphan_step_index_fails() -> None:
    # steps 0 and 2, but no step 1 — a gap
    steps = (
        _step(0),
        _step(2),
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("gap" in r.lower() or "contiguous" in r.lower() or "0..n-1" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 5: all depends_on references are valid step indices
# ---------------------------------------------------------------------------


def test_invalid_depends_on_fails() -> None:
    steps = (
        _step(0),
        _step(1, depends_on=(99,)),  # 99 does not exist
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("depends_on" in r.lower() or "invalid" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 6: DAG is acyclic
# ---------------------------------------------------------------------------


def test_cyclic_dag_fails() -> None:
    steps = (
        _step(0, depends_on=(1,)),  # 0 depends on 1
        _step(1, depends_on=(0,)),  # 1 depends on 0  → cycle
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("cycl" in r.lower() or "acycl" in r.lower() or "circular" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 7: every step has at least one target_path
# ---------------------------------------------------------------------------


def test_empty_target_paths_fails() -> None:
    steps = (
        PlanStep(
            step_index=0,
            description="No targets",
            intent_kind=StepIntentKind.MODIFY_FILE,
            target_paths=(),  # empty
            repo="jarvis",
        ),
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("target_path" in r.lower() or "target path" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 8: no ".." in target_paths (no path escape)
# ---------------------------------------------------------------------------


def test_dotdot_path_fails() -> None:
    steps = (
        _step(0, target_paths=("../escape.py",)),
    )
    plan = _plan(steps=steps)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any(".." in r or "escape" in r.lower() or "repo-relative" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 9: repos_affected matches union of step repos
# ---------------------------------------------------------------------------


def test_repos_mismatch_fails() -> None:
    # step uses repo "reactor" but repos_affected only lists "jarvis"
    steps = (
        _step(0, repo="reactor"),
    )
    plan = _plan(steps=steps, repos_affected=("jarvis",))
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any("repos_affected" in r.lower() or "mismatch" in r.lower() for r in result.reasons)


# ---------------------------------------------------------------------------
# Rule 10: acceptance check run_after_step references are valid
# ---------------------------------------------------------------------------


def test_invalid_run_after_step_fails() -> None:
    checks = (
        AcceptanceCheck(
            check_id="chk-001",
            check_kind=CheckKind.EXIT_CODE,
            command="pytest",
            expected="0",
            run_after_step=99,  # step 99 does not exist
        ),
    )
    steps = (_step(0),)
    plan = _plan(steps=steps, acceptance_checks=checks)
    result = PlanValidator().validate(plan)
    assert result.passed is False
    assert any(
        "run_after_step" in r.lower() or "acceptance" in r.lower() or "invalid" in r.lower()
        for r in result.reasons
    )


# ---------------------------------------------------------------------------
# Happy path — valid plan passes all rules
# ---------------------------------------------------------------------------


def test_valid_plan_passes() -> None:
    steps = (
        _step(0),
        _step(1, depends_on=(0,)),
        _step(2, depends_on=(0, 1)),
    )
    checks = (
        AcceptanceCheck(
            check_id="chk-001",
            check_kind=CheckKind.EXIT_CODE,
            command="pytest tests/",
            expected="0",
            run_after_step=2,
        ),
    )
    plan = _plan(steps=steps, repos_affected=("jarvis",), acceptance_checks=checks)
    result = PlanValidator(max_steps=10).validate(plan)
    assert result.passed is True
    assert result.reasons == []


# ---------------------------------------------------------------------------
# ValidationResult smoke test
# ---------------------------------------------------------------------------


def test_validation_result_fields() -> None:
    ok = ValidationResult(passed=True, reasons=[])
    assert ok.passed is True
    assert ok.reasons == []

    fail = ValidationResult(passed=False, reasons=["something wrong"])
    assert fail.passed is False
    assert len(fail.reasons) == 1
