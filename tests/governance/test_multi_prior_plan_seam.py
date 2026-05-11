"""Regression spine for Move 6.5 PLAN-phase canonical integration seam.

Pins the load-bearing invariants for the shared async helper that
wires multi-prior dispatch into the orchestrator's PLAN phase.

The helper is invoked from BOTH:
  * ``phase_runners.plan_runner.PLANRunner`` (Slice 3-extracted path)
  * ``orchestrator`` inline PLAN block (legacy fallback when
    JARVIS_PHASE_RUNNER_SLICE3_FULLY_EXTRACTED=false)

One helper, two callers — operator binding 2026-05-10 clarification
#5 ("factor one shared async helper used by both paths").
"""
from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.verification.multi_prior_plan_seam import (
    MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION,
    dispatch_plan_with_multi_prior,
)


_SEAM_SRC = Path(
    inspect.getfile(dispatch_plan_with_multi_prior),
).read_text(encoding="utf-8")


_PLANNING_FLAG = "JARVIS_MULTI_PRIOR_PLANNING_ENABLED"
_RUNNER_FLAG = "JARVIS_MULTI_PRIOR_RUNNER_ENABLED"
_DISPATCH_FLAG = "JARVIS_MULTI_PRIOR_DISPATCH_ENABLED"
_OBSERVER_FLAG = "JARVIS_MULTI_PRIOR_OBSERVER_ENABLED"


@dataclasses.dataclass
class _CtxStub:
    op_id: str = "op-test-1"
    provider_route: str = "complex"
    posture: str = "EXPLORE"
    description: str = "test op description"
    target_files: tuple = ("foo.py",)
    context_hash: str = "h"
    implementation_plan: str = ""


class _PlanResultStub:
    def __init__(self, plan_json="", approach="", ui_affected=False):
        self.plan_json = plan_json
        self.approach = approach
        self.ordered_changes = []
        self.complexity = "moderate"
        self.skipped = False
        self.skip_reason = ""
        self.ui_affected = ui_affected
        self.planning_duration_s = 0.0


class _PlanGenStub:
    """Minimal PlanGenerator surface needed by the seam."""

    def __init__(self, plan_json='{"approach":"x"}'):
        self.plan_json = plan_json
        self.calls = []

    async def generate_plan(self, ctx, deadline):
        self.calls.append(
            (getattr(ctx, "description", ""), getattr(ctx, "op_id", "")),
        )
        return _PlanResultStub(
            plan_json=self.plan_json,
            approach="rehydrated approach",
        )

    def _parse_plan_response(self, raw):
        # Mirror canonical parser shape — returns PlanResult.
        return _PlanResultStub(
            plan_json=raw,
            approach="rehydrated approach",
        )

    def _validate_plan_coherence(self, plan_result):
        # Canonical validator mutates in place; doesn't raise.
        return None


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clean all 4 multi-prior flags between tests."""
    for k in (
        _PLANNING_FLAG, _RUNNER_FLAG,
        _DISPATCH_FLAG, _OBSERVER_FLAG,
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ---------------------------------------------------------------------------
# Master-off default: returns None (caller does single-shot)
# ---------------------------------------------------------------------------


def test_master_off_returns_none():
    """Default state (all 4 flags off) → helper returns None
    immediately. The caller's single-shot path runs unchanged.
    Zero-behavior-change guarantee at default-FALSE."""

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=_CtxStub(),
            plan_generator=_PlanGenStub(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )

    result = asyncio.run(_run())
    assert result is None


def test_only_some_flags_on_returns_none(monkeypatch):
    """Partial flag activation → returns None. The 3 substrate
    masters (planning + runner + dispatch) MUST all be on for
    dispatch_multi_prior to fire."""
    monkeypatch.setenv(_PLANNING_FLAG, "true")
    # runner + dispatch still off

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=_CtxStub(),
            plan_generator=_PlanGenStub(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )

    result = asyncio.run(_run())
    assert result is None


def test_op_id_blank_returns_none(monkeypatch):
    """Defensive: empty op_id → None (mirrors substrate's
    SKIP_OP_BLANK decision)."""
    for k in (_PLANNING_FLAG, _RUNNER_FLAG, _DISPATCH_FLAG):
        monkeypatch.setenv(k, "true")
    ctx = _CtxStub(op_id="")

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=ctx,
            plan_generator=_PlanGenStub(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )

    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# Wrong route / posture: returns None (substrate gates handle this)
# ---------------------------------------------------------------------------


def test_non_complex_route_returns_none(monkeypatch):
    """route=standard → Slice 1 materializer returns None
    (route gate fails). Helper passes through cleanly."""
    for k in (_PLANNING_FLAG, _RUNNER_FLAG, _DISPATCH_FLAG):
        monkeypatch.setenv(k, "true")
    ctx = _CtxStub(provider_route="standard")

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=ctx,
            plan_generator=_PlanGenStub(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )

    assert asyncio.run(_run()) is None


def test_non_explore_posture_returns_none(monkeypatch):
    """posture=MAINTAIN → Slice 1 materializer returns None
    (posture gate fails)."""
    for k in (_PLANNING_FLAG, _RUNNER_FLAG, _DISPATCH_FLAG):
        monkeypatch.setenv(k, "true")
    ctx = _CtxStub(posture="MAINTAIN")

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=ctx,
            plan_generator=_PlanGenStub(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
            posture_str="MAINTAIN",
        )

    assert asyncio.run(_run()) is None


# ---------------------------------------------------------------------------
# Exception isolation: NEVER raises into caller
# ---------------------------------------------------------------------------


def test_plan_generator_exception_isolated(monkeypatch):
    """When per-roll PlanGenerator raises, the helper logs +
    returns None — caller's single-shot path runs unchanged.
    NEVER propagates into asyncio loop."""
    for k in (_PLANNING_FLAG, _RUNNER_FLAG, _DISPATCH_FLAG):
        monkeypatch.setenv(k, "true")

    class _BrokenPlanGen:
        async def generate_plan(self, ctx, deadline):
            raise RuntimeError("simulated planner failure")
        def _parse_plan_response(self, raw):
            return _PlanResultStub()
        def _validate_plan_coherence(self, pr):
            return None

    async def _run() -> Any:
        return await dispatch_plan_with_multi_prior(
            ctx=_CtxStub(),
            plan_generator=_BrokenPlanGen(),
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=10),
        )

    # Should not raise; should return None.
    result = asyncio.run(_run())
    assert result is None


# ---------------------------------------------------------------------------
# AST pins — load-bearing structural invariants
# ---------------------------------------------------------------------------


def test_ast_pin_composes_canonical_substrate_imports():
    """The seam MUST compose canonical substrate symbols:
    dispatch_multi_prior + ConsensusActionRecommendation +
    record_dispatch_outcome. Drift to a parallel
    implementation is structurally caught."""
    tree = ast.parse(_SEAM_SRC)
    saw_dispatch = False
    saw_action_enum = False
    saw_recorder = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.name
                if name == "dispatch_multi_prior":
                    saw_dispatch = True
                elif name == "ConsensusActionRecommendation":
                    saw_action_enum = True
                elif name == "record_dispatch_outcome":
                    saw_recorder = True
    assert saw_dispatch, (
        "seam MUST import dispatch_multi_prior (Slice 3)"
    )
    assert saw_action_enum, (
        "seam MUST import ConsensusActionRecommendation enum"
    )
    assert saw_recorder, (
        "seam MUST import record_dispatch_outcome (Slice 4) — "
        "drives graduation ledger growth"
    )


def test_ast_pin_authority_asymmetry_no_forbidden_imports():
    """The seam is an INTEGRATION ADAPTER, not a policy module.
    MUST NOT import orchestrator / phase_runners / iron_gate /
    policy_engine / providers / change_engine /
    semantic_guardian / candidate_generator. Composes existing
    canonical surfaces; no parallel policy."""
    tree = ast.parse(_SEAM_SRC)
    forbidden = {
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.providers",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.candidate_generator",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, (
                f"seam MUST NOT import {node.module!r} — "
                f"authority asymmetry violation"
            )


def test_ast_pin_rehydration_via_canonical_parser():
    """Per operator binding clarification #2: the seam MUST
    rehydrate the consensus winner through
    PlanGenerator._parse_plan_response (NOT a parallel parser).
    Source-level pin."""
    assert "_parse_plan_response" in _SEAM_SRC, (
        "seam MUST compose PlanGenerator._parse_plan_response — "
        "no parallel field extraction (operator binding 2026-05-10)"
    )


def test_ast_pin_coherence_validation_via_canonical():
    """Per operator binding clarification #2: same coherence
    validation as single-shot path. Source-level pin."""
    assert "_validate_plan_coherence" in _SEAM_SRC, (
        "seam MUST compose PlanGenerator._validate_plan_coherence — "
        "same coherence discipline as single-shot generate_plan"
    )


def test_ast_pin_record_dispatch_outcome_invoked():
    """Per operator binding clarification #6: record_dispatch_outcome
    MUST be called after dispatch (observer-flag-gated internally
    by the recorder). Drives graduation ledger growth."""
    assert "record_dispatch_outcome(verdict)" in _SEAM_SRC, (
        "seam MUST call record_dispatch_outcome(verdict) — "
        "graduation observations accumulate only via this call"
    )


def test_ast_pin_prior_addendum_threaded():
    """Per operator binding clarification #4: real prior angles
    via system_prompt_addendum threading. Source-level pin
    against cosmetic-noise regression."""
    assert "system_prompt_addendum" in _SEAM_SRC, (
        "seam MUST thread Prior.system_prompt_addendum — "
        "real prior angles, not cosmetic noise"
    )


def test_ast_pin_seed_threaded():
    """Prior.seed MUST be referenced — provider-seed discipline
    for deterministic re-roll reproducibility."""
    assert "seed" in _SEAM_SRC, (
        "seam MUST reference Prior.seed for deterministic "
        "re-roll reproducibility"
    )


def test_ast_pin_two_caller_consumers():
    """The shared-helper invariant (operator binding #5): both
    callers MUST import the seam. Source-level grep on both
    files."""
    plan_runner_src = Path(
        "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
    ).read_text(encoding="utf-8")
    orchestrator_src = Path(
        "backend/core/ouroboros/governance/orchestrator.py",
    ).read_text(encoding="utf-8")
    assert "dispatch_plan_with_multi_prior" in plan_runner_src, (
        "plan_runner.py MUST import dispatch_plan_with_multi_prior — "
        "Slice 3-extracted PLAN seam"
    )
    assert "dispatch_plan_with_multi_prior" in orchestrator_src, (
        "orchestrator.py MUST import dispatch_plan_with_multi_prior — "
        "inline PLAN fallback path (legacy when Slice 3 off). "
        "Operator binding clarification #5: shared helper across "
        "both paths."
    )


def test_schema_version_canonical_literal():
    assert (
        MULTI_PRIOR_PLAN_SEAM_SCHEMA_VERSION
        == "multi_prior_plan_seam.v1"
    )
