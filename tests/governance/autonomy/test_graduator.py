"""tests/governance/autonomy/test_graduator.py"""
import pytest
from backend.core.ouroboros.governance.autonomy.tiers import (
    AutonomyTier, GraduationMetrics, SignalAutonomyConfig,
)


def _make_config(tier=AutonomyTier.OBSERVE, metrics=None):
    return SignalAutonomyConfig(
        trigger_source="intent:test_failure", repo="jarvis", canary_slice="tests/",
        current_tier=tier, graduation_metrics=metrics or GraduationMetrics(),
    )


class TestGraduatorRegistration:
    def test_register_and_get(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        grad.register(_make_config())
        retrieved = grad.get_config("intent:test_failure", "jarvis", "tests/")
        assert retrieved.current_tier == AutonomyTier.OBSERVE

    def test_get_unknown_returns_none(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        assert grad.get_config("unknown", "unknown", "unknown") is None


class TestGraduatorObserveToSuggest:
    def test_promotes_after_20_observations_5_confirmations(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(observations=20, false_positives=0, human_confirmations=5)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        result = grad.check_graduation("intent:test_failure", "jarvis", "tests/")
        assert result == AutonomyTier.SUGGEST

    def test_no_promote_with_false_positives(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(observations=20, false_positives=1, human_confirmations=5)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") is None

    def test_no_promote_insufficient_observations(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(observations=15, false_positives=0, human_confirmations=5)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.OBSERVE, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") is None


class TestGraduatorSuggestToGoverned:
    def test_promotes_after_30_successful_ops_low_rollback(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(successful_ops=30, rollback_count=1)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.SUGGEST, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") == AutonomyTier.GOVERNED

    def test_no_promote_high_rollback_rate(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(successful_ops=30, rollback_count=2)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.SUGGEST, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") is None


class TestGraduatorGovernedToAutonomous:
    def test_promotes_after_50_ops_zero_rollbacks(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(successful_ops=50, rollback_count=0)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") == AutonomyTier.AUTONOMOUS

    def test_no_promote_with_rollbacks(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(successful_ops=50, rollback_count=1)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") is None


class TestGraduatorAlreadyAutonomous:
    def test_no_promote_beyond_autonomous(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        metrics = GraduationMetrics(successful_ops=100, rollback_count=0)
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.AUTONOMOUS, metrics))
        assert grad.check_graduation("intent:test_failure", "jarvis", "tests/") is None


class TestGraduatorDemotion:
    def test_rollback_demotes_autonomous_to_governed(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.AUTONOMOUS))
        assert grad.demote("intent:test_failure", "jarvis", "tests/", "rollback") == AutonomyTier.GOVERNED

    def test_postmortem_streak_demotes_to_suggest(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED))
        assert grad.demote("intent:test_failure", "jarvis", "tests/", "postmortem_streak") == AutonomyTier.SUGGEST

    def test_anomaly_demotes_to_observe(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.GOVERNED))
        assert grad.demote("intent:test_failure", "jarvis", "tests/", "anomaly") == AutonomyTier.OBSERVE

    def test_break_glass_demotes_all_to_observe(self):
        from backend.core.ouroboros.governance.autonomy.graduator import TrustGraduator
        grad = TrustGraduator()
        grad.register(_make_config(AutonomyTier.AUTONOMOUS))
        grad.register(SignalAutonomyConfig(
            trigger_source="intent:stack_trace", repo="prime", canary_slice="tests/",
            current_tier=AutonomyTier.GOVERNED, graduation_metrics=GraduationMetrics(),
        ))
        grad.break_glass_reset()
        assert grad.get_config("intent:test_failure", "jarvis", "tests/").current_tier == AutonomyTier.OBSERVE
        assert grad.get_config("intent:stack_trace", "prime", "tests/").current_tier == AutonomyTier.OBSERVE
