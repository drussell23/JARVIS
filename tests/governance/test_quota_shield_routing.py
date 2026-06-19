# tests/governance/test_quota_shield_routing.py
from __future__ import annotations
import dataclasses
import pytest


def test_prefer_local_field_defaults_false():
    from backend.core.ouroboros.governance.op_context import OperationContext
    assert "prefer_local" in OperationContext.__dataclass_fields__
    assert OperationContext.__dataclass_fields__["prefer_local"].default is False


@pytest.mark.asyncio
async def test_end_to_end_shield_decision_to_prefer_local(monkeypatch):
    """Shield ON + low load + OK memory -> apply_quota_shield stamps prefer_local."""
    monkeypatch.setenv("JARVIS_QUOTA_SHIELD_ENABLED", "true")
    from backend.core.ouroboros.governance.quota_shield import apply_quota_shield
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        target_files: tuple = ("x.py",)
        prefer_local: bool = False

    @dataclasses.dataclass
    class _Adv:
        risk_score: float = 0.01
        blast_radius: int = 0

    out = await apply_quota_shield(
        _Ctx(), advisory=_Adv(),
        gate=type("G", (), {"pressure": staticmethod(lambda: PressureLevel.OK)})(),
        governor=None, local_enabled=True, token_estimator=lambda c: 20)
    assert out.prefer_local is True


def test_primacy_gate_honors_prefer_local_source():
    """Static guard: the primacy gate fires on prefer_local OR jprime_primacy_enabled."""
    import backend.core.ouroboros.governance.candidate_generator as cg
    src = open(cg.__file__).read()
    assert 'getattr(context, "prefer_local", False)' in src   # gate honors the flag
    # dispatch consult — route_label is keyword-only on _try_jprime_primacy
    assert 'self._try_jprime_primacy(context, deadline, route_label="quota_shield")' in src
