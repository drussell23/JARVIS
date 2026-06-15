"""Unit C — AGENT_DEGRADATION SSE event type registration.

Step 0 discovery (recorded here for audit):
  - EVENT_TYPE_MEMORY_PRESSURE_CHANGED is defined at line 185 of
    ide_observability_stream.py alongside the other EVENT_TYPE_* constants.
  - publish() validates at line 1907 via:
        if event_type not in _VALID_EVENT_TYPES:
            return None
    _VALID_EVENT_TYPES is a module-level frozenset (line 1351) that explicitly
    enumerates every accepted event type. Adding the constant to the frozenset
    is the ONLY way to make publish() accept it — the set is NOT auto-built
    from module globals.
  - Existing publish_* helpers are module-level functions. The new helper
    follows the same pattern as publish_memory_fanout_decision_event.
"""
from __future__ import annotations

from backend.core.ouroboros.governance import ide_observability_stream as ios


def test_agent_degradation_event_type_registered():
    assert ios.EVENT_TYPE_AGENT_DEGRADATION == "agent_degradation"
    # _VALID_EVENT_TYPES is the frozenset publish() checks against (line 1907).
    # Adding the constant here is the correct — and ONLY — way to make
    # publish() accept this event type without silently dropping it.
    assert "agent_degradation" in ios._VALID_EVENT_TYPES  # noqa: SLF001


import pytest

from backend.core.ouroboros.governance.shadow_graduation_gate import (
    ShadowGraduationGate,
)


class _FakeStore:
    def __init__(self, streak):
        self._streak = streak

    async def recent_aligned_streak(self, agent):
        return self._streak


@pytest.mark.asyncio
async def test_no_promote_below_threshold(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    gate = ShadowGraduationGate(store=_FakeStore(streak=49))
    promoted = await gate.maybe_promote("plan")
    assert promoted is False
    assert persisted == []


@pytest.mark.asyncio
async def test_promote_at_threshold_persists_flags(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_THRESHOLD", "50")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)
    gate = ShadowGraduationGate(store=_FakeStore(streak=50))
    promoted = await gate.maybe_promote("plan")
    assert promoted is True
    assert ("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true") in persisted
    assert ("JARVIS_PLAN_SUBAGENT_SHADOW", "false") in persisted


@pytest.mark.asyncio
async def test_promote_idempotent(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", "true")
    gate = ShadowGraduationGate(store=_FakeStore(streak=50))
    promoted = await gate.maybe_promote("plan")
    assert promoted is False  # already authoritative -> no-op
    assert persisted == []


from backend.core.ouroboros.governance.shadow_graduation_gate import (
    PlanBreaker,
)


def test_breaker_trips_on_cyclical_dag():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": ["u2"]},
        {"id": "u2", "owned_paths": ["b.py"], "deps": ["u1"]},
    ]})
    assert decision.trip is True
    assert decision.reason == "cyclical_dag"


def test_breaker_trips_on_empty_dag():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": []})
    assert decision.trip is True
    assert decision.reason == "unparsable_or_empty_dag"


def test_breaker_critical_pressure_preempts_before_dag():
    # CRITICAL pressure trips BEFORE inspecting the DAG (pre-emptive).
    b = PlanBreaker(pressure_fn=lambda: "critical")
    decision = b.should_use_legacy(dag=None)
    assert decision.trip is True
    assert decision.reason == "critical_memory_pressure"
    assert decision.pressure_level == "critical"


def test_breaker_passes_valid_dag_under_ok_pressure():
    b = PlanBreaker(pressure_fn=lambda: "ok")
    decision = b.should_use_legacy(dag={"units": [
        {"id": "u1", "owned_paths": ["a.py"], "deps": []},
    ]})
    assert decision.trip is False


@pytest.mark.asyncio
async def test_gate_disabled_is_noop(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.setenv("JARVIS_SHADOW_GRADUATION_GATE_ENABLED", "false")
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)
    gate = ShadowGraduationGate(store=_FakeStore(streak=999))
    # Even with a streak far above threshold, a disabled gate never promotes.
    assert await gate.maybe_promote("plan") is False
    assert persisted == []


class _RaisingStore:
    async def recent_aligned_streak(self, agent):
        raise RuntimeError("store exploded")


@pytest.mark.asyncio
async def test_store_read_exception_is_fail_soft(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    monkeypatch.delenv("JARVIS_PLAN_SUBAGENT_AUTHORITATIVE", raising=False)
    gate = ShadowGraduationGate(store=_RaisingStore())
    # A store that raises must NOT break the FSM — gate returns False, no persist.
    assert await gate.maybe_promote("plan") is False
    assert persisted == []


@pytest.mark.asyncio
async def test_unknown_agent_is_noop(monkeypatch):
    persisted = []
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.shadow_graduation_gate."
        "persist_flag_to_env",
        lambda flag, value, **kw: persisted.append((flag, value)) or True,
    )
    gate = ShadowGraduationGate(store=_FakeStore(streak=999))
    assert await gate.maybe_promote("nonexistent_agent") is False
    assert persisted == []


from backend.core.ouroboros.governance.shadow_graduation_gate import (
    build_rail_evaluator,
)


def test_rail_evaluator_routes_by_agent():
    ev = build_rail_evaluator()
    # review path
    aligned, _ = ev("review",
                    {"risk_tier": "SAFE_AUTO", "semantic_guard_hard": False},
                    {"aggregate": "approve"})
    assert aligned is True
    # plan path: legacy carries {"flat": [...]}
    aligned, _ = ev("plan",
                    {"flat": ["a.py"]},
                    {"units": [{"id": "u1", "owned_paths": ["a.py"],
                                "deps": []}]})
    assert aligned is True
    # unknown agent -> conservative not-aligned
    aligned, reason = ev("bogus", {}, {})
    assert aligned is False
