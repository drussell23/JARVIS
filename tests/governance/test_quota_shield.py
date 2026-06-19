from __future__ import annotations
import dataclasses


@dataclasses.dataclass
class _Adv:
    risk_score: float = 0.0
    blast_radius: int = 0


def test_quota_shield_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_QUOTA_SHIELD_ENABLED", raising=False)
    from backend.core.ouroboros.governance.quota_shield import quota_shield_enabled
    assert quota_shield_enabled() is False
    monkeypatch.setenv("JARVIS_QUOTA_SHIELD_ENABLED", "true")
    assert quota_shield_enabled() is True


def test_cognitive_load_monotonic():
    from backend.core.ouroboros.governance.quota_shield import compute_cognitive_load
    low = compute_cognitive_load(risk_score=0.0, blast_radius=0, token_volume=0)
    hi_risk = compute_cognitive_load(risk_score=1.0, blast_radius=0, token_volume=0)
    hi_blast = compute_cognitive_load(risk_score=0.0, blast_radius=100, token_volume=0)
    hi_tok = compute_cognitive_load(risk_score=0.0, blast_radius=0, token_volume=100000)
    assert 0.0 <= low <= hi_risk <= 1.0
    assert low < hi_risk and low < hi_blast and low < hi_tok
    assert compute_cognitive_load(risk_score=1.0, blast_radius=100, token_volume=100000) <= 1.0


def test_decide_routes_local_on_low_load():
    from backend.core.ouroboros.governance.quota_shield import decide
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    d = decide(advisory=_Adv(risk_score=0.05, blast_radius=1), pressure_level=PressureLevel.OK,
               token_volume=200, local_enabled=True)
    assert d.route_local is True
    assert d.memory_override is False
    assert d.cognitive_load < 0.5


def test_decide_routes_remote_on_high_load():
    from backend.core.ouroboros.governance.quota_shield import decide
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    d = decide(advisory=_Adv(risk_score=0.9, blast_radius=50), pressure_level=PressureLevel.OK,
               token_volume=50000, local_enabled=True)
    assert d.route_local is False
    assert d.memory_override is False


def test_critical_memory_hard_overrides_even_low_load():
    from backend.core.ouroboros.governance.quota_shield import decide
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    d = decide(advisory=_Adv(risk_score=0.0, blast_radius=0), pressure_level=PressureLevel.CRITICAL,
               token_volume=10, local_enabled=True)
    assert d.route_local is False           # trivial, but host stability wins
    assert d.memory_override is True
    assert "memory" in d.reason.lower()


def test_local_disabled_never_routes_local():
    from backend.core.ouroboros.governance.quota_shield import decide
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel
    d = decide(advisory=_Adv(risk_score=0.0, blast_radius=0), pressure_level=PressureLevel.OK,
               token_volume=10, local_enabled=False)
    assert d.route_local is False


import pytest


class _Gov:
    def __init__(self): self.started = 0
    async def start_if_enabled(self): self.started += 1
    def owns_daemon(self): return False


@pytest.mark.asyncio
async def test_apply_shield_disabled_returns_ctx_unchanged(monkeypatch):
    monkeypatch.setenv("JARVIS_QUOTA_SHIELD_ENABLED", "false")
    import dataclasses
    from backend.core.ouroboros.governance.quota_shield import apply_quota_shield

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        target_files: tuple = ("a.py",)
        prefer_local: bool = False

    ctx = _Ctx()
    out = await apply_quota_shield(ctx, advisory=None)
    assert out is ctx                      # untouched when disabled


@pytest.mark.asyncio
async def test_apply_shield_low_load_sets_prefer_local_and_prewarms(monkeypatch):
    monkeypatch.setenv("JARVIS_QUOTA_SHIELD_ENABLED", "true")
    import dataclasses
    from backend.core.ouroboros.governance.quota_shield import apply_quota_shield
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        target_files: tuple = ("a.py",)
        prefer_local: bool = False

    @dataclasses.dataclass
    class _Adv:
        risk_score: float = 0.02
        blast_radius: int = 1

    gov = _Gov()
    out = await apply_quota_shield(
        _Ctx(), advisory=_Adv(),
        gate=type("G", (), {"pressure": staticmethod(lambda: PressureLevel.OK)})(),
        governor=gov, local_enabled=True, token_estimator=lambda ctx: 50)
    assert out.prefer_local is True        # low load -> route local
    await __import__("asyncio").sleep(0)   # let the fire-and-forget pre-warm schedule
    assert gov.started == 1                # JIT pre-warm fired


@pytest.mark.asyncio
async def test_apply_shield_critical_memory_no_local_no_prewarm(monkeypatch):
    monkeypatch.setenv("JARVIS_QUOTA_SHIELD_ENABLED", "true")
    import dataclasses
    from backend.core.ouroboros.governance.quota_shield import apply_quota_shield
    from backend.core.ouroboros.governance.memory_pressure_gate import PressureLevel

    @dataclasses.dataclass(frozen=True)
    class _Ctx:
        op_id: str = "o"
        target_files: tuple = ("a.py",)
        prefer_local: bool = False

    @dataclasses.dataclass
    class _Adv:
        risk_score: float = 0.0
        blast_radius: int = 0

    gov = _Gov()
    out = await apply_quota_shield(
        _Ctx(), advisory=_Adv(),
        gate=type("G", (), {"pressure": staticmethod(lambda: PressureLevel.CRITICAL)})(),
        governor=gov, local_enabled=True, token_estimator=lambda ctx: 10)
    assert out.prefer_local is False       # CRITICAL -> host stability over quota
    await __import__("asyncio").sleep(0)
    assert gov.started == 0                # no pre-warm when not routing local
