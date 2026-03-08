"""tests/governance/autonomy/test_e2e_autonomy.py

End-to-end: Full trust graduation lifecycle and demotion with gate checks.
"""
import pytest
from dataclasses import replace
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier, CAISnapshot, CognitiveLoad, GraduationMetrics,
    SAISnapshot, SignalAutonomyConfig, UAESnapshot, WorkContext,
)
from backend.core.ouroboros.governance.autonomy.gate import AutonomyGate
from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
from backend.core.ouroboros.governance.autonomy.state import AutonomyState


def _cai(load=CognitiveLoad.LOW, ctx=WorkContext.CODING, safety="SAFE"):
    return CAISnapshot(cognitive_load=load, work_context=ctx, safety_level=safety)

def _uae(confidence=0.85):
    return UAESnapshot(confidence=confidence)

def _sai(ram=45.0, locked=False, anomaly=False):
    return SAISnapshot(ram_percent=ram, system_locked=locked, anomaly_detected=anomaly)


class TestE2EGraduationLifecycle:
    @pytest.mark.asyncio
    async def test_full_graduation_observe_to_autonomous(self, tmp_path):
        """Signal graduates through all 4 tiers with gate checks at each level."""
        gate = AutonomyGate()
        grad = TrustGraduator()
        state = AutonomyState(state_path=tmp_path / "state.json")

        # Start at OBSERVE
        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
            current_tier=AutonomyTier.OBSERVE, graduation_metrics=GraduationMetrics(),
        )
        grad.register(config)

        # Gate should block OBSERVE
        proceed, reason = await gate.should_proceed(config, _cai(), _uae(), _sai())
        assert proceed is False
        assert reason == "tier:observe_only"

        # Accumulate metrics for OBSERVE -> SUGGEST
        promoted_metrics = GraduationMetrics(observations=20, false_positives=0, human_confirmations=5)
        grad.register(replace(config, graduation_metrics=promoted_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.SUGGEST
        suggest_config = grad.promote("intent:test_failure", "jarvis", "tests/", new_tier)

        # Gate should allow SUGGEST
        proceed, reason = await gate.should_proceed(suggest_config, _cai(), _uae(), _sai())
        assert proceed is True

        # Accumulate metrics for SUGGEST -> GOVERNED
        governed_metrics = GraduationMetrics(successful_ops=30, rollback_count=1)
        grad.register(replace(suggest_config, graduation_metrics=governed_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.GOVERNED
        governed_config = grad.promote("intent:test_failure", "jarvis", "tests/", new_tier)

        # Accumulate metrics for GOVERNED -> AUTONOMOUS
        auto_metrics = GraduationMetrics(successful_ops=50, rollback_count=0)
        grad.register(replace(governed_config, graduation_metrics=auto_metrics))
        new_tier = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert new_tier == AutonomyTier.AUTONOMOUS
        auto_config = grad.promote("intent:test_failure", "jarvis", "tests/", new_tier)
        assert auto_config.current_tier == AutonomyTier.AUTONOMOUS

        # Save and reload state
        state.save(grad.all_configs())
        loaded = state.load()
        assert len(loaded) == 1
        assert loaded[0].current_tier == AutonomyTier.AUTONOMOUS


class TestE2EDemotionAndBreakGlass:
    @pytest.mark.asyncio
    async def test_rollback_demotes_then_break_glass_resets(self, tmp_path):
        """Rollback demotes, break-glass resets all to OBSERVE."""
        gate = AutonomyGate()
        grad = TrustGraduator()
        state = AutonomyState(state_path=tmp_path / "state.json")

        # Register two configs at different tiers
        grad.register(SignalAutonomyConfig(
            trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
            current_tier=AutonomyTier.AUTONOMOUS, graduation_metrics=GraduationMetrics(successful_ops=50),
        ))
        grad.register(SignalAutonomyConfig(
            trigger_source="intent:test_failure", repo="prime", canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED, graduation_metrics=GraduationMetrics(successful_ops=30),
        ))

        # Rollback demotes AUTONOMOUS -> GOVERNED
        new_tier = grad.demote("intent:test_failure", "jarvis", "tests/", "rollback")
        assert new_tier == AutonomyTier.GOVERNED

        # Break-glass resets ALL to OBSERVE
        grad.break_glass_reset()
        c1 = grad.get_config("intent:test_failure", "jarvis", "tests/")
        c2 = grad.get_config("intent:test_failure", "prime", "tests/")
        assert c1.current_tier == AutonomyTier.OBSERVE
        assert c2.current_tier == AutonomyTier.OBSERVE

        # Gate blocks everything at OBSERVE
        proceed, reason = await gate.should_proceed(c1, _cai(), _uae(), _sai())
        assert proceed is False

        # Save reset state
        state.save(grad.all_configs())
        loaded = state.load()
        assert all(c.current_tier == AutonomyTier.OBSERVE for c in loaded)

        # State reset clears file
        state.reset()
        assert not (tmp_path / "state.json").exists()
