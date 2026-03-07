"""Tests for BehavioralHealthMonitor anomaly detection."""

import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "backend")
)

from autonomy.contracts.behavioral_health import (
    BehavioralHealthMonitor,
    BehavioralHealthReport,
    ThrottleRecommendation,
)


def _make_envelope(confidence=0.9):
    from core.contracts.decision_envelope import (
        DecisionEnvelope,
        DecisionSource,
        DecisionType,
        OriginComponent,
    )

    return DecisionEnvelope(
        envelope_id="env-1",
        trace_id="trace-1",
        parent_envelope_id=None,
        decision_type=DecisionType.EXTRACTION,
        source=DecisionSource.HEURISTIC,
        origin_component=OriginComponent.EMAIL_TRIAGE_EXTRACTION,
        payload={},
        confidence=confidence,
        created_at_epoch=time.time(),
        created_at_monotonic=time.monotonic(),
        causal_seq=1,
        config_version="v1",
    )


def _make_report(emails_processed=5, tier_counts=None, errors=None):
    from autonomy.email_triage.schemas import TriageCycleReport

    return TriageCycleReport(
        cycle_id="cycle-1",
        started_at=time.time(),
        completed_at=time.time(),
        emails_fetched=emails_processed,
        emails_processed=emails_processed,
        tier_counts=tier_counts or {1: 1, 2: 1, 3: 2, 4: 1},
        notifications_sent=1,
        notifications_suppressed=0,
        errors=errors or [],
    )


class TestHealthyBaseline:
    def test_initial_health_is_healthy(self):
        """No cycles recorded yet — monitor should report healthy."""
        monitor = BehavioralHealthMonitor()
        report = monitor.check_health()
        assert report.healthy is True
        assert report.recommendation == ThrottleRecommendation.NONE
        assert len(report.anomalies) == 0

    def test_normal_cycles_stay_healthy(self):
        """Five normal cycles should produce a healthy report with NONE recommendation."""
        monitor = BehavioralHealthMonitor()
        for _ in range(5):
            report = _make_report(emails_processed=5)
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        health = monitor.check_health()
        assert health.healthy is True
        assert health.recommendation == ThrottleRecommendation.NONE


class TestRateAnomaly:
    def test_rate_spike_detected(self):
        """A sudden 10x spike in emails_processed should trigger rate anomaly."""
        monitor = BehavioralHealthMonitor()
        # 5 normal cycles
        for _ in range(5):
            report = _make_report(emails_processed=5)
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        # 1 spike cycle
        spike_report = _make_report(emails_processed=50)
        spike_envelopes = [_make_envelope(confidence=0.9) for _ in range(50)]
        monitor.record_cycle(spike_report, spike_envelopes)

        health = monitor.check_health()
        assert health.healthy is False
        assert any("rate" in a.lower() for a in health.anomalies)


class TestErrorRateAnomaly:
    def test_error_spike_detected(self):
        """High error ratio (8/10) after clean cycles should trigger error anomaly."""
        monitor = BehavioralHealthMonitor()
        # 5 clean cycles
        for _ in range(5):
            report = _make_report(emails_processed=5, errors=[])
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        # 1 error-heavy cycle
        error_report = _make_report(
            emails_processed=10,
            errors=["err1", "err2", "err3", "err4", "err5", "err6", "err7", "err8"],
        )
        error_envelopes = [_make_envelope(confidence=0.9) for _ in range(10)]
        monitor.record_cycle(error_report, error_envelopes)

        health = monitor.check_health()
        assert health.healthy is False
        assert any("error" in a.lower() for a in health.anomalies)


class TestConfidenceDegradation:
    def test_declining_confidence_detected(self):
        """Steadily declining confidence should trigger confidence anomaly."""
        monitor = BehavioralHealthMonitor()
        confidences = [0.95, 0.85, 0.75, 0.65, 0.55]
        for conf in confidences:
            report = _make_report(emails_processed=5)
            envelopes = [_make_envelope(confidence=conf) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        health = monitor.check_health()
        assert health.healthy is False
        assert any("confidence" in a.lower() for a in health.anomalies)


class TestThrottleRecommendations:
    def test_reduce_batch_recommendation(self):
        """After error spike, recommendation should not be NONE."""
        monitor = BehavioralHealthMonitor()
        # 5 clean cycles
        for _ in range(5):
            report = _make_report(emails_processed=5, errors=[])
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        # 1 error-heavy cycle
        error_report = _make_report(
            emails_processed=10,
            errors=["err1", "err2", "err3", "err4", "err5", "err6", "err7", "err8"],
        )
        error_envelopes = [_make_envelope(confidence=0.9) for _ in range(10)]
        monitor.record_cycle(error_report, error_envelopes)

        rec, reason = monitor.should_throttle()
        assert rec != ThrottleRecommendation.NONE
        assert reason is not None

    def test_no_throttle_when_healthy(self):
        """Five normal cycles should produce NONE throttle and no reason."""
        monitor = BehavioralHealthMonitor()
        for _ in range(5):
            report = _make_report(emails_processed=5)
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        rec, reason = monitor.should_throttle()
        assert rec == ThrottleRecommendation.NONE
        assert reason is None


class TestSlidingWindow:
    def test_old_cycles_drop_out(self):
        """With window_size=3, bad cycles should age out after 3 good ones."""
        monitor = BehavioralHealthMonitor(window_size=3)

        # 3 bad cycles (high error rate)
        for _ in range(3):
            report = _make_report(
                emails_processed=10,
                errors=["e1", "e2", "e3", "e4", "e5", "e6", "e7", "e8"],
            )
            envelopes = [_make_envelope(confidence=0.5) for _ in range(10)]
            monitor.record_cycle(report, envelopes)

        # 3 good cycles — should push bad ones out of window
        for _ in range(3):
            report = _make_report(emails_processed=5, errors=[])
            envelopes = [_make_envelope(confidence=0.9) for _ in range(5)]
            monitor.record_cycle(report, envelopes)

        health = monitor.check_health()
        assert health.healthy is True
