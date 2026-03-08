"""tests/governance/autonomy/test_gate.py"""
import pytest
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier, CAISnapshot, CognitiveLoad, GraduationMetrics,
    SAISnapshot, SignalAutonomyConfig, UAESnapshot, WorkContext,
)


def _make_config(tier: AutonomyTier = AutonomyTier.GOVERNED) -> SignalAutonomyConfig:
    return SignalAutonomyConfig(
        trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
        current_tier=tier, graduation_metrics=GraduationMetrics(),
    )


def _cai(load=CognitiveLoad.LOW, ctx=WorkContext.CODING, safety="SAFE"):
    return CAISnapshot(cognitive_load=load, work_context=ctx, safety_level=safety)


def _uae(confidence=0.85):
    return UAESnapshot(confidence=confidence)


def _sai(ram=45.0, locked=False, anomaly=False):
    return SAISnapshot(ram_percent=ram, system_locked=locked, anomaly_detected=anomaly)


class TestGateObserveTierBlocks:
    @pytest.mark.asyncio
    async def test_observe_tier_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(AutonomyTier.OBSERVE), _cai(), _uae(), _sai())
        assert proceed is False
        assert reason == "tier:observe_only"


class TestGateCognitiveLoadBlocks:
    @pytest.mark.asyncio
    async def test_high_cognitive_load_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(load=CognitiveLoad.HIGH), _uae(), _sai())
        assert proceed is False
        assert reason == "cai:cognitive_load_high"


class TestGateWorkContextBlocks:
    @pytest.mark.asyncio
    async def test_meeting_context_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(ctx=WorkContext.MEETINGS), _uae(), _sai())
        assert proceed is False
        assert reason == "cai:in_meeting"


class TestGateMemoryPressureBlocks:
    @pytest.mark.asyncio
    async def test_high_ram_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(), _uae(), _sai(ram=95.0))
        assert proceed is False
        assert reason == "sai:memory_pressure"


class TestGateScreenLockedBlocks:
    @pytest.mark.asyncio
    async def test_screen_locked_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(), _uae(), _sai(locked=True))
        assert proceed is False
        assert reason == "sai:screen_locked"


class TestGateLowConfidenceBlocks:
    @pytest.mark.asyncio
    async def test_low_uae_confidence_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(), _uae(confidence=0.3), _sai())
        assert proceed is False
        assert reason == "uae:low_pattern_confidence"


class TestGateCrossSystemDisagreement:
    @pytest.mark.asyncio
    async def test_cai_safe_but_sai_anomaly_blocks(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(safety="SAFE"), _uae(), _sai(anomaly=True))
        assert proceed is False
        assert reason == "disagreement:cai_safe_sai_anomaly"


class TestGateProceeds:
    @pytest.mark.asyncio
    async def test_all_clear_proceeds(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(), _uae(), _sai())
        assert proceed is True
        assert reason == "proceed"

    @pytest.mark.asyncio
    async def test_suggest_tier_proceeds(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(AutonomyTier.SUGGEST), _cai(), _uae(), _sai())
        assert proceed is True
        assert reason == "proceed"

    @pytest.mark.asyncio
    async def test_medium_cognitive_load_proceeds(self):
        from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
        gate = AutonomyGate()
        proceed, reason = await gate.should_proceed(_make_config(), _cai(load=CognitiveLoad.MEDIUM), _uae(), _sai())
        assert proceed is True
        assert reason == "proceed"
