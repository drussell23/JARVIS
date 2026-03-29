"""
Tests for PlanStore — immutable plan persistence keyed by plan_hash.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.architect.plan import (
    AcceptanceCheck,
    ArchitecturalPlan,
    CheckKind,
    PlanStep,
    StepIntentKind,
)
from backend.core.ouroboros.architect.plan_store import PlanStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(title: str = "Test Plan") -> ArchitecturalPlan:
    step = PlanStep(
        step_index=0,
        description="Create the module",
        intent_kind=StepIntentKind.CREATE_FILE,
        target_paths=("backend/core/foo.py",),
        repo="jarvis",
        ancillary_paths=("backend/core/__init__.py",),
        interface_contracts=("foo() -> None",),
        tests_required=("tests/core/test_foo.py",),
        risk_tier_hint="safe_auto",
        depends_on=(),
    )
    check = AcceptanceCheck(
        check_id="chk-001",
        check_kind=CheckKind.EXIT_CODE,
        command="python -m pytest tests/core/test_foo.py",
        expected="0",
        cwd=".",
        timeout_s=60.0,
        run_after_step=0,
        sandbox_required=True,
    )
    return ArchitecturalPlan.create(
        parent_hypothesis_id="hyp-abc123",
        parent_hypothesis_fingerprint="fp-xyz",
        title=title,
        description="A test plan for unit testing PlanStore.",
        repos_affected=("jarvis",),
        non_goals=("No UI changes",),
        steps=(step,),
        acceptance_checks=(check,),
        model_used="claude-test",
        created_at=1_700_000_000.0,
        snapshot_hash="snap-001",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_store_and_load(tmp_path: Path) -> None:
    """Round-trip: store a plan, load it back, verify key fields."""
    store = PlanStore(tmp_path)
    plan = _make_plan()

    store.store(plan)
    loaded = store.load(plan.plan_hash)

    assert loaded is not None
    assert loaded.plan_hash == plan.plan_hash
    assert loaded.title == plan.title
    assert loaded.plan_id == plan.plan_id
    assert loaded.parent_hypothesis_id == plan.parent_hypothesis_id
    assert loaded.parent_hypothesis_fingerprint == plan.parent_hypothesis_fingerprint
    assert loaded.description == plan.description
    assert loaded.model_used == plan.model_used
    assert loaded.created_at == plan.created_at
    assert loaded.snapshot_hash == plan.snapshot_hash
    assert loaded.repos_affected == plan.repos_affected
    assert loaded.non_goals == plan.non_goals
    assert loaded.file_allowlist == plan.file_allowlist

    # steps round-trip
    assert len(loaded.steps) == 1
    s = loaded.steps[0]
    orig = plan.steps[0]
    assert s.step_index == orig.step_index
    assert s.description == orig.description
    assert s.intent_kind == orig.intent_kind
    assert s.target_paths == orig.target_paths
    assert s.repo == orig.repo
    assert s.ancillary_paths == orig.ancillary_paths
    assert s.interface_contracts == orig.interface_contracts
    assert s.tests_required == orig.tests_required
    assert s.risk_tier_hint == orig.risk_tier_hint
    assert s.depends_on == orig.depends_on

    # acceptance_checks round-trip
    assert len(loaded.acceptance_checks) == 1
    c = loaded.acceptance_checks[0]
    orig_c = plan.acceptance_checks[0]
    assert c.check_id == orig_c.check_id
    assert c.check_kind == orig_c.check_kind
    assert c.command == orig_c.command
    assert c.expected == orig_c.expected
    assert c.cwd == orig_c.cwd
    assert c.timeout_s == orig_c.timeout_s
    assert c.run_after_step == orig_c.run_after_step
    assert c.sandbox_required == orig_c.sandbox_required


def test_load_missing_returns_none(tmp_path: Path) -> None:
    """Loading a non-existent hash returns None."""
    store = PlanStore(tmp_path)
    result = store.load("deadbeef" * 8)
    assert result is None


def test_exists(tmp_path: Path) -> None:
    """exists() returns False before store, True after."""
    store = PlanStore(tmp_path)
    plan = _make_plan()

    assert store.exists(plan.plan_hash) is False
    store.store(plan)
    assert store.exists(plan.plan_hash) is True


def test_immutable_no_overwrite(tmp_path: Path) -> None:
    """Calling store() twice with the same plan must be a no-op (no error, data preserved)."""
    store = PlanStore(tmp_path)
    plan = _make_plan()

    store.store(plan)
    plan_file = tmp_path / f"{plan.plan_hash}.json"
    mtime_after_first = plan_file.stat().st_mtime

    # second store must not raise and must not touch the file
    store.store(plan)
    mtime_after_second = plan_file.stat().st_mtime

    assert mtime_after_first == mtime_after_second

    # data is still intact
    loaded = store.load(plan.plan_hash)
    assert loaded is not None
    assert loaded.plan_hash == plan.plan_hash


def test_persists_across_instances(tmp_path: Path) -> None:
    """A new PlanStore opened on the same directory can read what the first one wrote."""
    plan = _make_plan()

    store_a = PlanStore(tmp_path)
    store_a.store(plan)

    store_b = PlanStore(tmp_path)
    assert store_b.exists(plan.plan_hash)
    loaded = store_b.load(plan.plan_hash)
    assert loaded is not None
    assert loaded.plan_hash == plan.plan_hash
    assert loaded.title == plan.title
