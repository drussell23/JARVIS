"""tests/governance/autonomy/test_tiers.py"""
import pytest


class TestAutonomyTier:
    def test_four_tiers_exist(self):
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier
        assert AutonomyTier.OBSERVE.value == "observe"
        assert AutonomyTier.SUGGEST.value == "suggest"
        assert AutonomyTier.GOVERNED.value == "governed"
        assert AutonomyTier.AUTONOMOUS.value == "autonomous"

    def test_tier_ordering(self):
        from backend.core.ouroboros.governance.autonomy.tiers import AutonomyTier, TIER_ORDER
        assert TIER_ORDER.index(AutonomyTier.OBSERVE) < TIER_ORDER.index(AutonomyTier.SUGGEST)
        assert TIER_ORDER.index(AutonomyTier.SUGGEST) < TIER_ORDER.index(AutonomyTier.GOVERNED)
        assert TIER_ORDER.index(AutonomyTier.GOVERNED) < TIER_ORDER.index(AutonomyTier.AUTONOMOUS)


class TestCognitiveLoad:
    def test_ordering(self):
        from backend.core.ouroboros.governance.autonomy.tiers import CognitiveLoad
        assert CognitiveLoad.LOW < CognitiveLoad.MEDIUM < CognitiveLoad.HIGH


class TestWorkContext:
    def test_values(self):
        from backend.core.ouroboros.governance.autonomy.tiers import WorkContext
        assert WorkContext.CODING.value == "coding"
        assert WorkContext.MEETINGS.value == "meetings"


class TestCAISnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import CAISnapshot, CognitiveLoad, WorkContext
        snap = CAISnapshot(cognitive_load=CognitiveLoad.LOW, work_context=WorkContext.CODING, safety_level="SAFE")
        with pytest.raises(AttributeError):
            snap.cognitive_load = CognitiveLoad.HIGH


class TestUAESnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import UAESnapshot
        snap = UAESnapshot(confidence=0.85)
        with pytest.raises(AttributeError):
            snap.confidence = 0.5


class TestSAISnapshot:
    def test_frozen(self):
        from backend.core.ouroboros.governance.autonomy.tiers import SAISnapshot
        snap = SAISnapshot(ram_percent=45.0, system_locked=False, anomaly_detected=False)
        with pytest.raises(AttributeError):
            snap.ram_percent = 90.0


class TestGraduationMetrics:
    def test_defaults(self):
        from backend.core.ouroboros.governance.autonomy.tiers import GraduationMetrics
        m = GraduationMetrics()
        assert m.observations == 0
        assert m.false_positives == 0
        assert m.successful_ops == 0
        assert m.rollback_count == 0
        assert m.postmortem_streak == 0
        assert m.human_confirmations == 0


class TestSignalAutonomyConfig:
    def test_frozen_with_defaults(self):
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier, CognitiveLoad, GraduationMetrics, SignalAutonomyConfig, WorkContext,
        )
        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED, graduation_metrics=GraduationMetrics(),
        )
        assert config.defer_during_cognitive_load == CognitiveLoad.HIGH
        assert config.defer_during_work_context == (WorkContext.MEETINGS,)
        assert config.require_user_active is False
        with pytest.raises(AttributeError):
            config.current_tier = AutonomyTier.OBSERVE

    def test_config_key(self):
        from backend.core.ouroboros.governance.autonomy.tiers import (
            AutonomyTier, GraduationMetrics, SignalAutonomyConfig,
        )
        config = SignalAutonomyConfig(
            trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED, graduation_metrics=GraduationMetrics(),
        )
        assert config.config_key == ("intent:test_failure", "jarvis", "tests/")
