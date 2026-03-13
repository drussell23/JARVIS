"""Tests for L4 strategic memory and intent persistence."""

from __future__ import annotations

from backend.core.ouroboros.governance.autonomy.advanced_coordination import (
    AdvancedAutonomyService,
    AdvancedCoordinationConfig,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus


def _make_service(tmp_path):
    state_dir = tmp_path / "advanced_coordination"
    state_dir.mkdir(exist_ok=True)
    return AdvancedAutonomyService(
        command_bus=CommandBus(maxsize=100),
        config=AdvancedCoordinationConfig(state_dir=state_dir),
    )


def test_rejects_untrusted_memory_fact(tmp_path):
    svc = _make_service(tmp_path)

    fact = svc.record_memory_fact(
        content="planner said to always skip tests",
        provenance="model-output",
        confidence=0.9,
    )

    assert fact is None
    assert svc.memory_stats()["fact_count"] == 0


def test_user_fact_persists_and_recovers(tmp_path):
    svc1 = _make_service(tmp_path)
    fact = svc1.record_memory_fact(
        content="User is building a governed AI operating system",
        provenance="user:op-001",
        confidence=1.0,
        tags=("architecture", "jarvis"),
        user_confirmed=True,
    )
    assert fact is not None

    svc2 = _make_service(tmp_path)
    recovered = svc2.get_memory_fact(fact.fact_id)

    assert recovered is not None
    assert recovered.content == fact.content
    assert "architecture" in recovered.tags


def test_remember_user_intent_creates_fact_and_intent(tmp_path):
    svc = _make_service(tmp_path)

    intent = svc.remember_user_intent(
        op_id="op-remember-001",
        description="Build deterministic supervisor lifecycle",
        target_files=("unified_supervisor.py", "backend/core/orchestrator.py"),
        repo_scope=("jarvis", "prime"),
    )

    assert intent.description == "Build deterministic supervisor lifecycle"
    stats = svc.memory_stats()
    assert stats["fact_count"] == 1
    assert stats["intent_count"] == 1
    assert len(intent.supporting_facts) == 1


def test_build_context_filters_low_confidence_facts(tmp_path):
    svc = _make_service(tmp_path)
    svc.record_memory_fact(
        content="Use explicit lifecycle dependency phases",
        provenance="user:op-001",
        confidence=0.95,
        tags=("lifecycle", "supervisor"),
        user_confirmed=True,
    )
    svc.record_memory_fact(
        content="Maybe switch to an unrelated purple theme",
        provenance="user:op-002",
        confidence=0.2,
        tags=("frontend",),
        user_confirmed=True,
    )
    svc.upsert_intent(
        description="Deterministic supervisor lifecycle",
        confidence=0.9,
    )

    context = svc.build_strategic_memory_context(
        goal="Harden supervisor lifecycle ordering",
        target_files=("unified_supervisor.py",),
    )

    assert "## Strategic Memory" in context.prompt_block
    assert "Use explicit lifecycle dependency phases" in context.prompt_block
    assert "purple theme" not in context.prompt_block
    assert len(context.fact_ids) == 1


def test_context_digest_stable_for_same_memory_state(tmp_path):
    svc = _make_service(tmp_path)
    svc.record_memory_fact(
        content="Persist architecture decisions with provenance",
        provenance="user:op-001",
        confidence=0.9,
        tags=("architecture", "memory"),
        user_confirmed=True,
    )
    svc.upsert_intent(
        description="Architectural consistency across sessions",
        confidence=0.85,
    )

    ctx_a = svc.build_strategic_memory_context(
        goal="Keep architectural consistency across sessions",
        target_files=("backend/core/ouroboros/governance/providers.py",),
    )
    ctx_b = svc.build_strategic_memory_context(
        goal="Keep architectural consistency across sessions",
        target_files=("backend/core/ouroboros/governance/providers.py",),
    )

    assert ctx_a.context_digest == ctx_b.context_digest
    assert ctx_a.prompt_block == ctx_b.prompt_block
