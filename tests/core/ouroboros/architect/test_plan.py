"""Tests for ArchitecturalPlan, PlanStep, AcceptanceCheck and compute_plan_hash."""
from __future__ import annotations

import dataclasses

import pytest

from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
    compute_plan_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(
    step_index: int = 0,
    description: str = "Create the package init",
    intent_kind: StepIntentKind = StepIntentKind.CREATE_FILE,
    target_paths: tuple = ("backend/core/ouroboros/architect/__init__.py",),
    repo: str = "jarvis",
    ancillary_paths: tuple = (),
    tests_required: tuple = (),
) -> PlanStep:
    return PlanStep(
        step_index=step_index,
        description=description,
        intent_kind=intent_kind,
        target_paths=target_paths,
        repo=repo,
        ancillary_paths=ancillary_paths,
        tests_required=tests_required,
    )


def _make_check(
    check_id: str = "chk-001",
    check_kind: CheckKind = CheckKind.EXIT_CODE,
    command: str = "pytest tests/core/ouroboros/architect/",
    expected: str = "0",
) -> AcceptanceCheck:
    return AcceptanceCheck(
        check_id=check_id,
        check_kind=check_kind,
        command=command,
        expected=expected,
    )


def _make_plan(**overrides) -> ArchitecturalPlan:
    defaults = dict(
        parent_hypothesis_id="hyp-aabbccdd",
        parent_hypothesis_fingerprint="fp123",
        title="Add Architect Plan Schemas",
        description="Implement the core plan schemas for the Architecture Reasoning Agent.",
        repos_affected=("jarvis",),
        non_goals=("Do not implement the reasoning agent itself",),
        steps=(_make_step(),),
        acceptance_checks=(_make_check(),),
        model_used="claude-sonnet-4-6",
        created_at=1_700_000_000.0,
        snapshot_hash="abc123",
    )
    defaults.update(overrides)
    return ArchitecturalPlan.create(**defaults)


# ---------------------------------------------------------------------------
# StepIntentKind
# ---------------------------------------------------------------------------


class TestStepIntentKinds:
    def test_all_values_accessible(self):
        assert StepIntentKind.CREATE_FILE.value == "create_file"
        assert StepIntentKind.MODIFY_FILE.value == "modify_file"
        assert StepIntentKind.DELETE_FILE.value == "delete_file"

    def test_enum_count(self):
        assert len(StepIntentKind) == 3


# ---------------------------------------------------------------------------
# CheckKind
# ---------------------------------------------------------------------------


class TestCheckKinds:
    def test_all_values_accessible(self):
        assert CheckKind.EXIT_CODE.value == "exit_code"
        assert CheckKind.REGEX_STDOUT.value == "regex_stdout"
        assert CheckKind.IMPORT_CHECK.value == "import_check"

    def test_enum_count(self):
        assert len(CheckKind) == 3


# ---------------------------------------------------------------------------
# PlanStep
# ---------------------------------------------------------------------------


class TestPlanStep:
    def test_plan_step_frozen(self):
        step = _make_step()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            step.description = "mutated"  # type: ignore[misc]

    def test_defaults(self):
        step = _make_step()
        assert step.ancillary_paths == ()
        assert step.interface_contracts == ()
        assert step.tests_required == ()
        assert step.risk_tier_hint == "safe_auto"
        assert step.depends_on == ()

    def test_custom_fields(self):
        step = PlanStep(
            step_index=2,
            description="Modify router",
            intent_kind=StepIntentKind.MODIFY_FILE,
            target_paths=("backend/core/prime_router.py",),
            repo="jarvis",
            ancillary_paths=("backend/core/prime_client.py",),
            interface_contracts=("route() signature unchanged",),
            tests_required=("tests/core/test_router.py",),
            risk_tier_hint="needs_review",
            depends_on=(0, 1),
        )
        assert step.step_index == 2
        assert step.intent_kind == StepIntentKind.MODIFY_FILE
        assert step.ancillary_paths == ("backend/core/prime_client.py",)
        assert step.risk_tier_hint == "needs_review"
        assert step.depends_on == (0, 1)


# ---------------------------------------------------------------------------
# AcceptanceCheck
# ---------------------------------------------------------------------------


class TestAcceptanceCheck:
    def test_acceptance_check_frozen(self):
        check = _make_check()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            check.command = "mutated"  # type: ignore[misc]

    def test_defaults(self):
        check = _make_check()
        assert check.cwd == "."
        assert check.timeout_s == 120.0
        assert check.run_after_step is None
        assert check.sandbox_required is True

    def test_check_kind_regex(self):
        check = AcceptanceCheck(
            check_id="chk-002",
            check_kind=CheckKind.REGEX_STDOUT,
            command="python -c \"print('hello world')\"",
            expected="hello world",
            timeout_s=30.0,
            run_after_step=1,
            sandbox_required=False,
        )
        assert check.check_kind == CheckKind.REGEX_STDOUT
        assert check.run_after_step == 1
        assert check.sandbox_required is False

    def test_check_kind_import(self):
        check = AcceptanceCheck(
            check_id="chk-003",
            check_kind=CheckKind.IMPORT_CHECK,
            command="backend.core.ouroboros.architect.plan",
        )
        assert check.check_kind == CheckKind.IMPORT_CHECK
        assert check.expected == ""


# ---------------------------------------------------------------------------
# compute_plan_hash
# ---------------------------------------------------------------------------


class TestComputePlanHash:
    def _base_args(self):
        return dict(
            title="Add Architect Plan Schemas",
            description="Implement core plan schemas.",
            repos_affected=("jarvis",),
            non_goals=("No reasoning agent",),
            steps=(_make_step(),),
            acceptance_checks=(_make_check(),),
        )

    def test_plan_hash_is_deterministic(self):
        args = self._base_args()
        h1 = compute_plan_hash(**args)
        h2 = compute_plan_hash(**args)
        assert h1 == h2
        assert len(h1) == 64

    def test_plan_hash_excludes_provenance(self):
        """Same structure + different model/time must yield the same hash."""
        args = self._base_args()
        h1 = compute_plan_hash(**args)

        # These provenance fields are NOT part of compute_plan_hash signature —
        # we simply confirm the hash stays the same when only plan structure is
        # identical.
        h2 = compute_plan_hash(**args)
        assert h1 == h2

    def test_plan_hash_via_plan_create_excludes_provenance(self):
        """Plans with identical structure but different model/created_at have the same hash."""
        plan_a = _make_plan(model_used="claude-sonnet-4-6", created_at=1_700_000_000.0)
        plan_b = _make_plan(model_used="doubleword-397b", created_at=1_800_000_000.0)
        assert plan_a.plan_hash == plan_b.plan_hash

    def test_plan_hash_changes_with_steps(self):
        args = self._base_args()
        h_original = compute_plan_hash(**args)

        modified_step = PlanStep(
            step_index=0,
            description="A completely different step",
            intent_kind=StepIntentKind.DELETE_FILE,
            target_paths=("backend/some/other/file.py",),
            repo="jarvis",
        )
        args_modified = dict(args, steps=(modified_step,))
        h_modified = compute_plan_hash(**args_modified)

        assert h_original != h_modified

    def test_plan_hash_changes_with_title(self):
        args = self._base_args()
        h1 = compute_plan_hash(**args)
        h2 = compute_plan_hash(**dict(args, title="Different Title"))
        assert h1 != h2

    def test_plan_hash_changes_with_acceptance_checks(self):
        args = self._base_args()
        h1 = compute_plan_hash(**args)
        different_check = _make_check(command="pytest tests/ -x")
        h2 = compute_plan_hash(**dict(args, acceptance_checks=(different_check,)))
        assert h1 != h2


# ---------------------------------------------------------------------------
# ArchitecturalPlan
# ---------------------------------------------------------------------------


class TestArchitecturalPlan:
    def test_plan_is_frozen(self):
        plan = _make_plan()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            plan.title = "mutated"  # type: ignore[misc]

    def test_plan_id_is_16_hex_chars(self):
        plan = _make_plan()
        assert len(plan.plan_id) == 16
        assert all(c in "0123456789abcdef" for c in plan.plan_id)

    def test_plan_hash_is_64_hex_chars(self):
        plan = _make_plan()
        assert len(plan.plan_hash) == 64
        assert all(c in "0123456789abcdef" for c in plan.plan_hash)

    def test_file_allowlist_computed_from_target_paths(self):
        step = PlanStep(
            step_index=0,
            description="Create module",
            intent_kind=StepIntentKind.CREATE_FILE,
            target_paths=("backend/core/ouroboros/architect/plan.py",),
            repo="jarvis",
        )
        plan = _make_plan(steps=(step,), acceptance_checks=())
        assert "backend/core/ouroboros/architect/plan.py" in plan.file_allowlist

    def test_file_allowlist_includes_ancillary_paths(self):
        step = PlanStep(
            step_index=0,
            description="Modify module with ancillary",
            intent_kind=StepIntentKind.MODIFY_FILE,
            target_paths=("backend/core/prime_router.py",),
            repo="jarvis",
            ancillary_paths=("backend/core/prime_client.py",),
        )
        plan = _make_plan(steps=(step,), acceptance_checks=())
        assert "backend/core/prime_client.py" in plan.file_allowlist
        assert "backend/core/prime_router.py" in plan.file_allowlist

    def test_file_allowlist_includes_tests_required(self):
        step = PlanStep(
            step_index=0,
            description="Add feature with tests",
            intent_kind=StepIntentKind.CREATE_FILE,
            target_paths=("backend/new_feature.py",),
            repo="jarvis",
            tests_required=("tests/test_new_feature.py",),
        )
        plan = _make_plan(steps=(step,), acceptance_checks=())
        assert "tests/test_new_feature.py" in plan.file_allowlist

    def test_file_allowlist_union_across_steps(self):
        step_a = PlanStep(
            step_index=0,
            description="Step A",
            intent_kind=StepIntentKind.CREATE_FILE,
            target_paths=("backend/a.py",),
            repo="jarvis",
            ancillary_paths=("backend/shared.py",),
        )
        step_b = PlanStep(
            step_index=1,
            description="Step B",
            intent_kind=StepIntentKind.MODIFY_FILE,
            target_paths=("backend/b.py",),
            repo="jarvis",
            tests_required=("tests/test_b.py",),
        )
        plan = _make_plan(steps=(step_a, step_b), acceptance_checks=())
        assert plan.file_allowlist == frozenset({
            "backend/a.py",
            "backend/shared.py",
            "backend/b.py",
            "tests/test_b.py",
        })

    def test_file_allowlist_is_frozenset(self):
        plan = _make_plan()
        assert isinstance(plan.file_allowlist, frozenset)

    def test_created_at_defaults_to_now(self):
        import time
        before = time.time()
        plan = ArchitecturalPlan.create(
            parent_hypothesis_id="hyp-1",
            parent_hypothesis_fingerprint="fp",
            title="T",
            description="D",
            repos_affected=("jarvis",),
            non_goals=(),
            steps=(_make_step(),),
            acceptance_checks=(),
            model_used="claude-test",
            snapshot_hash="snap",
        )
        after = time.time()
        assert before <= plan.created_at <= after

    def test_provenance_fields_stored_correctly(self):
        plan = _make_plan(
            model_used="doubleword-397b",
            created_at=1_700_000_000.0,
            snapshot_hash="deadbeef",
        )
        assert plan.model_used == "doubleword-397b"
        assert plan.created_at == 1_700_000_000.0
        assert plan.snapshot_hash == "deadbeef"

    def test_unique_plan_ids(self):
        plan_a = _make_plan()
        plan_b = _make_plan()
        assert plan_a.plan_id != plan_b.plan_id

    def test_acceptance_check_kinds_all_work(self):
        checks = tuple(
            AcceptanceCheck(
                check_id=f"chk-{i:03d}",
                check_kind=kind,
                command=f"cmd-{kind.value}",
            )
            for i, kind in enumerate(CheckKind)
        )
        plan = _make_plan(acceptance_checks=checks)
        stored_kinds = {c.check_kind for c in plan.acceptance_checks}
        assert stored_kinds == set(CheckKind)

    def test_step_intent_kinds_all_work(self):
        steps = tuple(
            PlanStep(
                step_index=i,
                description=f"Step {i}",
                intent_kind=kind,
                target_paths=(f"backend/file_{i}.py",),
                repo="jarvis",
            )
            for i, kind in enumerate(StepIntentKind)
        )
        plan = _make_plan(steps=steps)
        stored_intents = {s.intent_kind for s in plan.steps}
        assert stored_intents == set(StepIntentKind)
