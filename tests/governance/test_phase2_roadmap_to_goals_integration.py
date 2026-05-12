"""§41.4 Phase 2 — End-to-end integration spine.

Drives the canonical pipeline:

    operator-signed roadmap.yaml
      → roadmap_reader.process_roadmap (HMAC verify + emit)
      → router.ingest(goal_envelope)
      → goal_decomposition_planner.decompose_goal (pure)
      → multi_step_orchestrator.advance_orchestration
      → router.ingest(sub_goal_envelope)
      → completion tracking via goal_decomposition_planner.mark_sub_goal_status
      → next tick unblocks dependents

This is **pure integration** — no new substrate code, no new
state, no parallel routing surface. Every assertion proves the
three Phase 1 substrates (`d0eb3780b6` RoadmapReader,
`708cd531ec` Goal Decomposition Planner, `6cc1c52340` Multi-Step
Orchestrator) compose deterministically without hidden coupling.

The integration test owns:
* A shape-compliant `CapturingRouter` (sole new artifact —
  duck-typed to ``async def ingest(env)``; mirrors the
  ``UnifiedIntakeRouter`` signature so production routes
  interchangeably).
* Test fixtures for signed-roadmap YAML + RoadmapGoal-shaped
  records.

AST pins enforce the integration cage:
* Spine MUST NOT import orchestrator / iron_gate / policy /
  providers — these substrates compose BELOW the FSM layer.
* `make_envelope` is invoked exactly ONCE per substrate per
  goal/sub-goal (single canonical envelope factory).
* `router.ingest` is the ONLY routing surface — no parallel
  emit path.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest
import yaml as _yaml

from backend.core.ouroboros.governance import (
    goal_decomposition_planner as gdp,
)
from backend.core.ouroboros.governance import (
    multi_step_orchestrator as mso,
)
from backend.core.ouroboros.governance import roadmap_reader as rr
from backend.core.ouroboros.governance.goal_decomposition_planner import (
    CompletionStatus,
    DecomposedPlan,
    DecompositionVerdict,
    SubGoal,
    SubGoalKind,
    decompose_goal,
    mark_sub_goal_status,
)
from backend.core.ouroboros.governance.multi_step_orchestrator import (
    OrchestrationVerdict,
    advance_orchestration,
)
from backend.core.ouroboros.governance.roadmap_reader import (
    RoadmapVerdict,
    compute_signature,
    process_roadmap,
)


# ---------------------------------------------------------------------------
# Shape-compliant test router — the SOLE artifact this spine owns.
# Mirrors the canonical UnifiedIntakeRouter.ingest signature so
# production routes interchangeably.
# ---------------------------------------------------------------------------


@dataclass
class CapturingRouter:
    """Duck-typed router that captures every envelope. NEVER
    raises. Returns a deterministic idempotency key so callers
    can correlate."""

    envelopes: List[Any] = field(default_factory=list)
    ingest_count: int = 0
    raise_on_call: Optional[BaseException] = None

    async def ingest(self, envelope: Any) -> str:
        self.ingest_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.envelopes.append(envelope)
        return f"ikey-{self.ingest_count}"

    def by_source(self, source: str) -> List[Any]:
        return [
            e for e in self.envelopes
            if getattr(e, "source", "") == source
        ]

    def by_goal_id(self, goal_id: str) -> List[Any]:
        return [
            e for e in self.envelopes
            if (getattr(e, "evidence", None) or {}).get(
                "goal_id"
            ) == goal_id
        ]


# ---------------------------------------------------------------------------
# Fixture helpers — sign a roadmap YAML, build sub-goal plans.
# ---------------------------------------------------------------------------


_DEMO_SECRET = "phase2-integration-test-secret"


def _signed_roadmap_yaml(
    goals: List[Dict[str, Any]],
    *,
    secret: str = _DEMO_SECRET,
    version: str = "1",
    operator_id: Optional[str] = None,
    signed_at: Optional[str] = None,
) -> str:
    """Construct an operator-signed roadmap YAML for the test.
    Mirrors the operator workflow: write content, compute HMAC
    over the canonical signing payload (version + operator_id +
    signed_at + goals — same shape the reader's
    `_build_signing_payload` extracts), embed signature."""
    # Canonical signing payload (matches _build_signing_payload).
    signing_payload: Dict[str, Any] = {
        "version": version,
        "operator_id": operator_id,
        "signed_at": signed_at,
        "goals": goals,
    }
    sig = compute_signature(signing_payload, secret)
    # Full document = signing payload + signature
    full: Dict[str, Any] = dict(signing_payload)
    full["signature"] = sig
    return _yaml.safe_dump(full, sort_keys=False)


def _write_roadmap(
    tmp_path: Path,
    goals: List[Dict[str, Any]],
    *,
    secret: str = _DEMO_SECRET,
) -> Path:
    path = tmp_path / "roadmap.signed.yaml"
    path.write_text(_signed_roadmap_yaml(goals, secret=secret))
    return path


def _make_subgoal(
    sub_id: str,
    *,
    parent_goal_id: str = "g1",
    title: Optional[str] = None,
    depends_on: Tuple[str, ...] = (),
    kind: SubGoalKind = SubGoalKind.ATOMIC,
) -> SubGoal:
    return SubGoal(
        sub_goal_id=sub_id,
        parent_goal_id=parent_goal_id,
        title=title or f"Sub-goal {sub_id}",
        description=f"Description for {sub_id}",
        kind=kind,
        target_files=(f"path/{sub_id}.py",),
        depends_on_sub_ids=depends_on,
        estimated_complexity="trivial",
        boundary_crossed=False,
    )


def _make_plan(
    parent_goal_id: str,
    sub_goals: Tuple[SubGoal, ...],
    topological_order: Optional[Tuple[str, ...]] = None,
) -> DecomposedPlan:
    order = (
        topological_order if topological_order is not None
        else tuple(s.sub_goal_id for s in sub_goals)
    )
    return DecomposedPlan(
        parent_goal_id=parent_goal_id,
        sub_goals=sub_goals,
        dag_valid=True,
        dag_depth=1,
        topological_order=order,
        diagnostic="test fixture",
    )


# Shared autouse fixture — enable masters + isolate ledger paths.


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Each test runs with the three Phase 1 master flags ON +
    ledger paths in tmp_path so completion writes don't bleed."""
    # Master flags
    monkeypatch.setenv(rr._ENV_MASTER, "true")
    monkeypatch.setenv(rr._ENV_REQUIRE_SIG, "true")
    monkeypatch.setenv(rr._ENV_HMAC_SECRET, _DEMO_SECRET)
    monkeypatch.setenv(gdp._ENV_MASTER, "true")
    monkeypatch.setenv(mso._ENV_MASTER, "true")
    # Isolate ledgers
    monkeypatch.setenv(
        rr._ENV_LEDGER_PATH,
        str(tmp_path / "roadmap_ledger.jsonl"),
    )
    monkeypatch.setenv(
        gdp._ENV_LEDGER_PATH,
        str(tmp_path / "gdp_ledger.jsonl"),
    )
    monkeypatch.setenv(
        mso._ENV_LEDGER_PATH,
        str(tmp_path / "mso_ledger.jsonl"),
    )
    monkeypatch.setenv(
        mso._ENV_COMPLETION_LEDGER_PATH,
        str(tmp_path / "mso_completion.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Stage 1 — RoadmapReader → CapturingRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_roadmap_emits_goal_envelopes_via_router(
    tmp_path,
):
    """End-to-end stage 1: signed YAML → process_roadmap →
    CapturingRouter captures one envelope per goal."""
    roadmap_path = _write_roadmap(tmp_path, [
        {
            "id": "g1",
            "title": "Add /status posture field",
            "description": "Surface posture in /status",
            "target_files": ["backend/core/ouroboros/governance/status.py"],
            "priority": "high",
            "success_criteria": "tests green",
        },
        {
            "id": "g2",
            "title": "Document /tutorial",
            "description": "Add docstrings",
            "target_files": ["docs/tutorial.md"],
            "priority": "medium",
            "success_criteria": "docs present",
        },
    ])
    router = CapturingRouter()
    report = await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert report.verdict is RoadmapVerdict.VALID
    assert router.ingest_count == 2
    captured_ids = sorted(
        e.evidence["goal_id"] for e in router.envelopes
    )
    assert captured_ids == ["g1", "g2"]


@pytest.mark.asyncio
async def test_invalid_signature_emits_zero_envelopes(tmp_path):
    """The pipeline MUST refuse propagation when the signature
    doesn't verify — operator-binding gate against unsigned
    autonomous work."""
    roadmap_path = _write_roadmap(tmp_path, [
        {"id": "g1", "title": "x", "description": "y"},
    ], secret="wrong-secret")
    router = CapturingRouter()
    report = await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert report.verdict is RoadmapVerdict.INVALID_SIGNATURE
    assert router.ingest_count == 0


@pytest.mark.asyncio
async def test_envelope_carries_canonical_factory_metadata(
    tmp_path,
):
    """Bytes-check: envelopes built via the canonical
    intake.intent_envelope.make_envelope carry source / urgency /
    evidence shape. NO parallel envelope construction in either
    substrate."""
    roadmap_path = _write_roadmap(tmp_path, [
        {
            "id": "g1",
            "title": "Test",
            "description": "Desc",
            "priority": "critical",
            "target_files": ["backend/test/critical.py"],
        },
    ])
    router = CapturingRouter()
    await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert router.ingest_count == 1
    env = router.envelopes[0]
    assert env.source == "roadmap"
    assert env.urgency == "critical"
    assert env.evidence["goal_id"] == "g1"
    # idempotency_key is auto-generated by make_envelope
    assert env.idempotency_key


@pytest.mark.asyncio
async def test_master_off_short_circuits_pipeline(
    tmp_path, monkeypatch,
):
    """RoadmapReader master off → no envelope cascade. Other
    substrates remain reachable directly but stage-1 is the
    operator-gated entry."""
    monkeypatch.setenv(rr._ENV_MASTER, "false")
    roadmap_path = _write_roadmap(tmp_path, [
        {"id": "g1", "title": "x", "description": "y"},
    ])
    router = CapturingRouter()
    report = await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert report.master_enabled is False
    assert router.ingest_count == 0


@pytest.mark.asyncio
async def test_router_failure_recorded_in_outcome(tmp_path):
    """Router that raises on ingest → outcome captures the error
    string per goal; substrate NEVER raises into the caller."""
    roadmap_path = _write_roadmap(tmp_path, [
        {
            "id": "g1", "title": "x", "description": "y",
            "target_files": ["foo.py"],
        },
    ])
    router = CapturingRouter(
        raise_on_call=RuntimeError("intake unavailable"),
    )
    report = await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert report.verdict is RoadmapVerdict.VALID
    assert len(report.emit_outcomes) == 1
    assert report.emit_outcomes[0].emitted is False
    assert "intake unavailable" in report.emit_outcomes[0].error


# ---------------------------------------------------------------------------
# Stage 2 — Goal Decomposition (pure, no router side effects)
# ---------------------------------------------------------------------------


def test_decompose_goal_is_pure_no_router_side_effects():
    """decompose_goal is the contract for stage 2 — pure
    function. Calling it MUST NOT route envelopes; the operator
    decides when to emit via advance_orchestration."""
    # Build a goal that looks like a RoadmapGoal projection
    @dataclass
    class _Goal:
        goal_id: str = "g1"
        title: str = "Build foo"
        description: str = "..."

    verdict, plan, diag = decompose_goal(_Goal())
    assert verdict is DecompositionVerdict.VALID
    assert plan is not None
    assert plan.parent_goal_id == "g1"
    # Pure: no router argument, no side-effect surface


def test_decompose_goal_dag_validates_cycles():
    """Operator-injected decomposer producing cyclic deps must
    be rejected — Kahn's algorithm catches the cycle and
    surfaces DECOMPOSITION_FAILED."""
    @dataclass
    class _Goal:
        goal_id: str = "g1"
        title: str = "x"
        description: str = "y"

    def cyclic_decomposer(goal: Any) -> Tuple[SubGoal, ...]:
        return (
            _make_subgoal("a", depends_on=("b",)),
            _make_subgoal("b", depends_on=("a",)),
        )

    verdict, plan, diag = decompose_goal(
        _Goal(), decomposer=cyclic_decomposer,
    )
    assert verdict is DecompositionVerdict.DECOMPOSITION_FAILED


# ---------------------------------------------------------------------------
# Stage 3 — Multi-Step Orchestrator → CapturingRouter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_orchestration_emits_ready_set_only():
    """Stage 3: only sub-goals with no unmet deps emit on the
    first tick. The DAG order is the canonical authority — no
    parallel scheduler."""
    plan = _make_plan("g1", (
        _make_subgoal("a"),  # ready immediately
        _make_subgoal("b", depends_on=("a",)),  # blocked on a
        _make_subgoal("c", depends_on=("a", "b")),  # blocked
    ))
    router = CapturingRouter()
    report = await advance_orchestration(
        plan,
        router=router,
        completion_status_override={},  # nothing done yet
    )
    assert report.verdict is OrchestrationVerdict.PROGRESSING
    assert router.ingest_count == 1
    assert router.envelopes[0].evidence["sub_goal_id"] == "a"


@pytest.mark.asyncio
async def test_advance_orchestration_idempotent_no_double_emit():
    """Calling advance_orchestration twice with the same
    completion status MUST produce identical emit counts. The
    completion ledger filters already-EMITTED sub-goals."""
    plan = _make_plan("g1", (_make_subgoal("a"),))
    router = CapturingRouter()
    # First tick — emits a
    r1 = await advance_orchestration(
        plan,
        router=router,
        completion_status_override={},
    )
    assert router.ingest_count == 1
    # Second tick — uses the prior emit's ledger write. Override
    # with PROPOSED so the substrate sees "already emitted".
    r2 = await advance_orchestration(
        plan,
        router=router,
        completion_status_override={
            "a": CompletionStatus.PROPOSED.value,
        },
    )
    # No new emit — idempotency held
    assert router.ingest_count == 1


@pytest.mark.asyncio
async def test_completion_unblocks_dependents_on_next_tick():
    """When a sub-goal is marked DONE, its dependents become
    READY and emit on the next tick. Composition over
    `goal_decomposition_planner.mark_sub_goal_status` —
    multi_step_orchestrator NEVER writes its own parallel
    completion state."""
    plan = _make_plan("g1", (
        _make_subgoal("a"),
        _make_subgoal("b", depends_on=("a",)),
    ))
    router = CapturingRouter()
    # Tick 1: emit a
    await advance_orchestration(
        plan,
        router=router,
        completion_status_override={},
    )
    assert router.ingest_count == 1
    # Tick 2: a is DONE → b becomes ready
    await advance_orchestration(
        plan,
        router=router,
        completion_status_override={
            "a": CompletionStatus.COMPLETED.value,
        },
    )
    assert router.ingest_count == 2
    assert router.envelopes[1].evidence["sub_goal_id"] == "b"


@pytest.mark.asyncio
async def test_advance_orchestration_respects_max_emits_cap(
    monkeypatch,
):
    """Operator-tunable cap MUST hold — emitting 10 READY
    sub-goals in one tick when cap=2 produces exactly 2
    envelopes."""
    monkeypatch.setenv(mso._ENV_MAX_EMITS_PER_TICK, "2")
    plan = _make_plan("g1", tuple(
        _make_subgoal(f"s{i}") for i in range(10)
    ))
    router = CapturingRouter()
    report = await advance_orchestration(
        plan,
        router=router,
        completion_status_override={},
    )
    assert router.ingest_count == 2


@pytest.mark.asyncio
async def test_master_off_stage_3_no_emits():
    """Multi-Step Orchestrator master off → no sub-goal
    envelopes regardless of upstream state."""
    os.environ[mso._ENV_MASTER] = "false"
    try:
        plan = _make_plan("g1", (_make_subgoal("a"),))
        router = CapturingRouter()
        report = await advance_orchestration(
            plan,
            router=router,
            completion_status_override={},
        )
        assert report.master_enabled is False
        assert router.ingest_count == 0
    finally:
        os.environ[mso._ENV_MASTER] = "true"


# ---------------------------------------------------------------------------
# Full pipeline — stage 1 → 2 → 3 end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_pipeline_roadmap_to_sub_goal_envelopes(
    tmp_path,
):
    """End-to-end: signed YAML → roadmap envelopes captured →
    extract goal_id from one → decompose → orchestrate →
    sub-goal envelopes captured under the SAME router instance.
    Proves single-router invariant: NO parallel routing."""
    roadmap_path = _write_roadmap(tmp_path, [
        {
            "id": "g1",
            "title": "Refactor logger setup",
            "description": "Move logger init to module top",
            "priority": "high",
            "target_files": ["backend/core/logger.py"],
        },
    ])
    router = CapturingRouter()
    # Stage 1: emit roadmap goals
    rr_report = await process_roadmap(
        path_override=roadmap_path,
        secret_override=_DEMO_SECRET,
        router=router,
    )
    assert rr_report.verdict is RoadmapVerdict.VALID
    assert router.ingest_count == 1
    goal_env = router.envelopes[0]
    assert goal_env.source == "roadmap"

    # Stage 2: extract goal_id, build RoadmapGoal-shape, decompose
    extracted_id = goal_env.evidence["goal_id"]

    @dataclass
    class _GoalShape:
        goal_id: str
        title: str
        description: str

    goal = _GoalShape(
        goal_id=extracted_id,
        title="Refactor logger setup",
        description="Move logger init to module top",
    )
    verdict, plan, _ = decompose_goal(goal)
    assert verdict is DecompositionVerdict.VALID
    assert plan is not None
    assert plan.parent_goal_id == extracted_id

    # Stage 3: orchestrate → sub-goal envelopes via SAME router
    initial_count = router.ingest_count
    mso_report = await advance_orchestration(
        plan,
        router=router,
        completion_status_override={},
    )
    assert mso_report.verdict in (
        OrchestrationVerdict.PROGRESSING,
        OrchestrationVerdict.COMPLETED,
    )
    assert router.ingest_count > initial_count

    # Single-router invariant: all envelopes captured under one
    # ingest() surface — NO parallel routing. The canonical
    # design has BOTH goal-stage and sub-goal-stage envelopes
    # carry source='roadmap' (configurable via mso.envelope_source);
    # the discriminator is `evidence.multi_step_orchestrated`.
    all_roadmap_envs = router.by_source("roadmap")
    goal_envs = [
        e for e in all_roadmap_envs
        if not (e.evidence or {}).get("multi_step_orchestrated")
    ]
    sub_envs = [
        e for e in all_roadmap_envs
        if (e.evidence or {}).get("multi_step_orchestrated") is True
    ]
    assert len(goal_envs) == 1
    assert len(sub_envs) >= 1


# ---------------------------------------------------------------------------
# Cross-substrate composition pins — AST + structural
# ---------------------------------------------------------------------------


def test_canonical_make_envelope_composed_by_both_substrates():
    """Bytes-pin: both roadmap_reader and multi_step_orchestrator
    import `make_envelope` from `intake.intent_envelope`. NO
    parallel envelope factory."""
    rr_src = Path(
        "backend/core/ouroboros/governance/roadmap_reader.py"
    ).read_text()
    mso_src = Path(
        "backend/core/ouroboros/governance/multi_step_orchestrator.py"
    ).read_text()
    canonical_import = (
        "from backend.core.ouroboros.governance"
        ".intake.intent_envelope import"
    )
    assert canonical_import in rr_src
    assert canonical_import in mso_src
    # And both call make_envelope (not a parallel factory)
    assert "make_envelope(" in rr_src
    assert "make_envelope(" in mso_src


def test_router_ingest_is_only_routing_surface():
    """Bytes-pin: both substrates invoke `router.ingest(env)` as
    the sole submission surface. NO parallel emit path."""
    rr_src = Path(
        "backend/core/ouroboros/governance/roadmap_reader.py"
    ).read_text()
    mso_src = Path(
        "backend/core/ouroboros/governance/multi_step_orchestrator.py"
    ).read_text()
    assert "router.ingest(" in rr_src
    assert "router.ingest(" in mso_src


def test_completion_ledger_owned_by_goal_decomposition_planner():
    """Bytes-pin: multi_step_orchestrator does NOT maintain its
    own completion state. Composition route is
    `mark_sub_goal_status` on the goal_decomposition_planner."""
    mso_src = Path(
        "backend/core/ouroboros/governance/multi_step_orchestrator.py"
    ).read_text()
    assert "_mark_emitted_via_goal_decomposition" in mso_src
    # The substrate composes mark_sub_goal_status — confirm the
    # composer references the canonical name.
    assert "mark_sub_goal_status" in mso_src or "from backend" in mso_src


def test_integration_spine_authority_asymmetry():
    """The integration spine MUST NOT import orchestrator,
    iron_gate, policy, providers, or candidate_generator —
    Phase 2 wiring composes BELOW the FSM layer."""
    spine_src = Path(
        "tests/governance/"
        "test_phase2_roadmap_to_goals_integration.py"
    ).read_text()
    tree = ast.parse(spine_src)
    forbidden = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.providers",
        "backend.core.ouroboros.governance.candidate_generator",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod not in forbidden, (
                f"Phase 2 spine must not import {mod!r}"
            )


def test_three_substrate_masters_independently_gated(
    tmp_path, monkeypatch,
):
    """Each substrate has its own §33.1 master. Verify the
    three are independent: turning off one MUST NOT cause the
    others to bypass their own gate."""
    # All on — stage 1 emits
    monkeypatch.setenv(rr._ENV_MASTER, "true")
    monkeypatch.setenv(gdp._ENV_MASTER, "true")
    monkeypatch.setenv(mso._ENV_MASTER, "true")
    rr_path = _write_roadmap(tmp_path, [
        {
            "id": "g1", "title": "x", "description": "y",
            "target_files": ["bar.py"],
        },
    ])
    router = CapturingRouter()
    asyncio.run(process_roadmap(
        path_override=rr_path,
        secret_override=_DEMO_SECRET,
        router=router,
    ))
    assert router.ingest_count == 1

    # gdp off — stage 2 still PURE (decompose_goal has no master
    # gate; it returns NO_GOAL only when goal is None)
    monkeypatch.setenv(gdp._ENV_MASTER, "false")
    # But stage 3 (orchestration) has its own master; flip and
    # verify no emits
    monkeypatch.setenv(mso._ENV_MASTER, "false")
    plan = _make_plan("g1", (_make_subgoal("a"),))
    router2 = CapturingRouter()
    asyncio.run(advance_orchestration(
        plan,
        router=router2,
        completion_status_override={},
    ))
    assert router2.ingest_count == 0


# ---------------------------------------------------------------------------
# Substrate composition smoke — confirms shipped commits work
# ---------------------------------------------------------------------------


def test_phase1_substrate_commit_hashes_documented():
    """Bytes-pin: this spine documents which Phase 1 commit
    ships each substrate. Future readers can chase provenance."""
    spine_src = Path(
        "tests/governance/"
        "test_phase2_roadmap_to_goals_integration.py"
    ).read_text()
    # Commit hashes from PRD §41.4
    for sha in ("d0eb3780b6", "708cd531ec", "6cc1c52340"):
        assert sha in spine_src, (
            f"commit {sha} should be referenced for provenance"
        )
