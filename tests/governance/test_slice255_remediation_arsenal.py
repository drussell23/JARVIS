"""Slice 255 — Sovereign Remediation Matrix.

Two layers:
  * Sandbox-OK unit tests of `register_remediation_arsenal` / `build_remediation_handlers`
    against a fake SelfHealing + fake kernel/organs (no `unified_supervisor` import).
  * A sandbox-OFF integration test of the REAL chain (arm -> COMPONENT_DEGRADED -> FAILOVER
    -> shadow_guard TRAP -> /endorse executes the real kernel action); skipped where the
    kernel import is blocked (split-brain-guard).
"""
import asyncio
import enum

import pytest

from backend.core.remediation_arsenal import (
    build_remediation_handlers,
    register_remediation_arsenal,
)


class _Strategy(enum.Enum):
    RESTART = "restart"
    SCALE_DOWN = "scale_down"
    FAILOVER = "failover"
    ISOLATE = "isolate"
    ROLLBACK = "rollback"
    NOTIFY_ONLY = "notify_only"


class _FakeSelfHealing:
    RemediationStrategy = _Strategy

    def __init__(self):
        self._handlers = {}

    def register_handler(self, strategy, handler):
        self._handlers[strategy.value] = handler


class _Trinity:
    def __init__(self):
        self.calls = []

    async def restart_component(self, name):
        self.calls.append(name)
        return True


class _Kernel:
    def __init__(self, trinity):
        self._trinity = trinity


def test_arms_all_six_strategies():
    sho = _FakeSelfHealing()
    n = register_remediation_arsenal(sho, kernel=_Kernel(_Trinity()), organs={})
    assert n == 6
    assert set(sho._handlers) == {s.value for s in _Strategy}


def test_none_self_healing_is_failsoft():
    assert register_remediation_arsenal(None, kernel=None, organs={}) == 0


@pytest.mark.asyncio
async def test_restart_and_failover_bind_to_real_trinity():
    trin = _Trinity()
    handlers = build_remediation_handlers(kernel=_Kernel(trin), organs={})
    assert await handlers["restart"]("jarvis-prime") is True
    assert await handlers["failover"]("reactor-core") is True
    assert trin.calls == ["jarvis-prime", "reactor-core"]  # genuine capability invoked


@pytest.mark.asyncio
async def test_isolate_trips_the_breaker_organ():
    tripped = []

    class _Breaker:
        def record_failure(self, err=None):
            tripped.append(err)

    handlers = build_remediation_handlers(
        kernel=None, organs={"AdvancedCircuitBreaker": _Breaker()}
    )
    assert await handlers["isolate"]("vision") is True
    assert len(tripped) == 1  # breaker actually tripped


@pytest.mark.asyncio
async def test_missing_capability_is_failsoft_not_crash():
    # No kernel/_trinity → RESTART logs + returns False, never raises.
    handlers = build_remediation_handlers(kernel=None, organs={})
    assert await handlers["restart"]("x") is False
    assert await handlers["notify_only"]("x") is True  # benign always-True


# ── sandbox-OFF: the REAL end-to-end chain (skips where kernel import is blocked) ──

def _real_supervisor_or_skip():
    try:
        import unified_supervisor as us  # noqa: F401
        return us
    except Exception as exc:  # noqa: BLE001 — split-brain-guard / heavy deps in sandbox
        pytest.skip(f"unified_supervisor import unavailable (sandbox): {exc!r}")


@pytest.mark.asyncio
async def test_real_chain_traps_then_endorse_executes(monkeypatch):
    monkeypatch.setenv("JARVIS_RESILIENCE_REANIMATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "true")
    us = _real_supervisor_or_skip()
    from backend.core.cybernetic_reanimation import (
        PressureSignal, PressureSignalType, SignalEdge,
        pending_shadow_action_ids, pending_shadow_action_count,
        reset_pending_shadow_actions, handle_endorsement_choice,
    )
    trin = _Trinity()
    organs = us._instantiate_resilience_organs()
    dispatcher = us.build_resilience_dispatcher(organs)
    armed = register_remediation_arsenal(
        organs.get("SelfHealingOrchestrator"), kernel=_Kernel(trin), organs=organs
    )
    assert armed == 6
    reset_pending_shadow_actions()

    await dispatcher.dispatch(PressureSignal(
        type=PressureSignalType.COMPONENT_DEGRADED, source="reactor-core",
        edge=SignalEdge.RISING, severity="critical"))
    ids = pending_shadow_action_ids()
    assert pending_shadow_action_count() == 1          # shadow_guard trapped it
    assert trin.calls == []                            # real action NOT executed in shadow

    res = await handle_endorsement_choice(ids[0], "y")
    assert getattr(res, "status", None) == "executed"
    assert trin.calls == ["reactor-core"]              # genuine kernel action fired on endorse
