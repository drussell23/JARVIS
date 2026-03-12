"""tests/governance/autonomy/test_risk_classifier.py

TDD tests for M1: OperationRiskClassifier — lightweight risk scoring
utility for L3 SafetyNet incident escalation decisions.

Covers:
- RiskAssessment.is_actionable property
- OperationRiskClassifier.classify scoring and level thresholds
- OperationRiskClassifier.classify_from_rollback_history convenience
- SafetyNet integration: risk_level and risk_score in REPORT_ROLLBACK_CAUSE payload
- High-risk profiles trigger earlier escalation
"""
from __future__ import annotations

import time

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.risk_classifier import (
    OperationRiskClassifier,
    RiskAssessment,
    RiskLevel,
)
from backend.core.ouroboros.governance.autonomy.safety_net import (
    ProductionSafetyNet,
    SafetyNetConfig,
)


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _rollback_event(
    op_id: str,
    brain_id: str = "qwen_coder",
    reason: str = "validation_failed",
    affected_files: list[str] | None = None,
) -> EventEnvelope:
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.OP_ROLLED_BACK,
        payload={
            "op_id": op_id,
            "brain_id": brain_id,
            "rollback_reason": reason,
            "affected_files": affected_files or ["auth.py"],
            "phase_at_failure": "VALIDATE",
        },
    )


# -----------------------------------------------------------------------
# RiskAssessment property tests
# -----------------------------------------------------------------------


class TestRiskAssessmentActionable:
    def test_low_risk_not_actionable(self):
        assessment = RiskAssessment(
            level=RiskLevel.LOW,
            score=0.1,
            factors={"rollback_frequency": 0.0},
            reason="no risk factors",
        )
        assert assessment.is_actionable is False

    def test_medium_risk_not_actionable(self):
        assessment = RiskAssessment(
            level=RiskLevel.MEDIUM,
            score=0.4,
            factors={"rollback_frequency": 0.2},
            reason="moderate risk",
        )
        assert assessment.is_actionable is False

    def test_high_risk_actionable(self):
        assessment = RiskAssessment(
            level=RiskLevel.HIGH,
            score=0.65,
            factors={"rollback_frequency": 0.6},
            reason="high rollback frequency",
        )
        assert assessment.is_actionable is True

    def test_critical_risk_actionable(self):
        assessment = RiskAssessment(
            level=RiskLevel.CRITICAL,
            score=0.9,
            factors={"rollback_frequency": 1.0},
            reason="critical risk",
        )
        assert assessment.is_actionable is True


# -----------------------------------------------------------------------
# OperationRiskClassifier tests
# -----------------------------------------------------------------------


class TestOperationRiskClassifier:
    def test_zero_rollbacks_low_risk(self):
        classifier = OperationRiskClassifier()
        assessment = classifier.classify(
            rollback_count=0,
            window_s=3600.0,
            affected_files=[],
            root_cause_class="unknown",
            pattern_match=False,
        )
        assert assessment.level == RiskLevel.LOW
        assert assessment.score < 0.3

    def test_single_rollback_no_pattern_low_or_medium(self):
        classifier = OperationRiskClassifier()
        assessment = classifier.classify(
            rollback_count=1,
            window_s=3600.0,
            affected_files=["file.py"],
            root_cause_class="test_failure",
            pattern_match=False,
        )
        assert assessment.level in (RiskLevel.LOW, RiskLevel.MEDIUM)
        assert assessment.score < 0.5

    def test_multiple_rollbacks_with_pattern_high_or_critical(self):
        """3 rollbacks, pattern=True, permission_error -> HIGH or CRITICAL."""
        classifier = OperationRiskClassifier()
        assessment = classifier.classify(
            rollback_count=3,
            window_s=3600.0,
            affected_files=["a.py", "b.py", "c.py", "d.py"],
            root_cause_class="permission_error",
            pattern_match=True,
        )
        assert assessment.level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        assert assessment.score >= 0.5

    def test_many_files_increases_risk(self):
        """10+ affected files should produce a higher score than 1 file."""
        classifier = OperationRiskClassifier()
        few_files = classifier.classify(
            rollback_count=1,
            window_s=3600.0,
            affected_files=["a.py"],
            root_cause_class="test_failure",
            pattern_match=False,
        )
        many_files = classifier.classify(
            rollback_count=1,
            window_s=3600.0,
            affected_files=[f"file_{i}.py" for i in range(12)],
            root_cause_class="test_failure",
            pattern_match=False,
        )
        assert many_files.score > few_files.score

    def test_security_violation_highest_severity(self):
        """security_violation root cause should yield the highest factor score."""
        classifier = OperationRiskClassifier()
        assessment = classifier.classify(
            rollback_count=1,
            window_s=3600.0,
            affected_files=["auth.py"],
            root_cause_class="security_violation",
            pattern_match=False,
        )
        assert assessment.factors["root_cause_severity"] == 1.0

    def test_custom_weights(self):
        """Passing custom weights should change the scores accordingly."""
        # Weight only rollback_frequency
        custom_weights = {
            "rollback_frequency": 1.0,
            "affected_file_count": 0.0,
            "pattern_repetition": 0.0,
            "root_cause_severity": 0.0,
        }
        classifier = OperationRiskClassifier(weights=custom_weights)

        assessment_zero = classifier.classify(
            rollback_count=0,
            window_s=3600.0,
            affected_files=[f"f{i}.py" for i in range(20)],
            root_cause_class="security_violation",
            pattern_match=True,
        )
        # Despite many files, security violation, and pattern — the score
        # should be 0.0 because only rollback_frequency is weighted
        assert assessment_zero.score == 0.0

        assessment_many = classifier.classify(
            rollback_count=5,
            window_s=3600.0,
            affected_files=[],
            root_cause_class="unknown",
            pattern_match=False,
        )
        # 5+ rollbacks -> frequency factor 1.0 * weight 1.0 = 1.0
        assert assessment_many.score == 1.0

    def test_classify_from_rollback_history(self):
        """classify_from_rollback_history should extract context from history entries."""
        classifier = OperationRiskClassifier()
        now = time.monotonic()
        history = [
            {
                "op_id": "op_1",
                "brain_id": "qwen_coder",
                "reason": "validation_failed",
                "ts": now - 100,
                "affected_files": ["a.py", "b.py"],
            },
            {
                "op_id": "op_2",
                "brain_id": "qwen_coder",
                "reason": "validation_failed",
                "ts": now - 50,
                "affected_files": ["a.py", "c.py"],
            },
            {
                "op_id": "op_3",
                "brain_id": "deepseek_r1",
                "reason": "timeout",
                "ts": now,
                "affected_files": ["d.py"],
            },
        ]
        assessment = classifier.classify_from_rollback_history(
            history=history,
            window_s=3600.0,
        )
        assert isinstance(assessment, RiskAssessment)
        assert assessment.level is not None
        assert 0.0 <= assessment.score <= 1.0
        # 3 rollbacks in the history -> rollback_count=3
        assert assessment.factors["rollback_frequency"] > 0.0

    def test_classify_from_empty_history(self):
        """Empty history should produce LOW risk."""
        classifier = OperationRiskClassifier()
        assessment = classifier.classify_from_rollback_history(
            history=[],
            window_s=3600.0,
        )
        assert assessment.level == RiskLevel.LOW
        assert assessment.score < 0.3

    def test_level_thresholds(self):
        """Verify level threshold boundaries: <0.3=LOW, <0.5=MEDIUM, <0.7=HIGH, else CRITICAL."""
        classifier = OperationRiskClassifier()

        # We can't easily force exact scores, but we can verify the
        # static method that maps score -> level
        assert classifier._score_to_level(0.0) == RiskLevel.LOW
        assert classifier._score_to_level(0.29) == RiskLevel.LOW
        assert classifier._score_to_level(0.3) == RiskLevel.MEDIUM
        assert classifier._score_to_level(0.49) == RiskLevel.MEDIUM
        assert classifier._score_to_level(0.5) == RiskLevel.HIGH
        assert classifier._score_to_level(0.69) == RiskLevel.HIGH
        assert classifier._score_to_level(0.7) == RiskLevel.CRITICAL
        assert classifier._score_to_level(1.0) == RiskLevel.CRITICAL

    def test_reason_string_populated(self):
        """The assessment reason string should be non-empty and descriptive."""
        classifier = OperationRiskClassifier()
        assessment = classifier.classify(
            rollback_count=3,
            window_s=3600.0,
            affected_files=["a.py", "b.py"],
            root_cause_class="timeout",
            pattern_match=True,
        )
        assert isinstance(assessment.reason, str)
        assert len(assessment.reason) > 0


# -----------------------------------------------------------------------
# SafetyNet integration tests
# -----------------------------------------------------------------------


class TestSafetyNetRiskIntegration:
    def test_rollback_includes_risk_in_payload(self):
        """REPORT_ROLLBACK_CAUSE command should include risk_level and risk_score."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(rollback_pattern_threshold=2)
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        net._on_rollback(_rollback_event("op1"))

        # Find the REPORT_ROLLBACK_CAUSE command
        cmd = bus._heap[0][2]
        assert cmd.command_type == CommandType.REPORT_ROLLBACK_CAUSE
        assert "risk_level" in cmd.payload
        assert "risk_score" in cmd.payload
        assert isinstance(cmd.payload["risk_level"], str)
        assert isinstance(cmd.payload["risk_score"], float)

    def test_high_risk_triggers_earlier_escalation(self):
        """High-risk rollbacks should trigger incident even below the normal
        incident_rollback_threshold when risk is actionable."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        # High incident threshold so normal path wouldn't trigger
        config = SafetyNetConfig(
            rollback_pattern_threshold=2,
            incident_rollback_threshold=10,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        # Emit several rollbacks with high-risk characteristics:
        # pattern match (same reason), security-related, many files
        for i in range(3):
            net._on_rollback(_rollback_event(
                f"op_{i}",
                reason="permission_denied",
                affected_files=[f"core/{j}.py" for j in range(8)],
            ))

        # Collect all commands
        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)

        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        # Despite incident_rollback_threshold=10, the high risk assessment
        # should have triggered escalation
        assert len(mode_cmds) >= 1
        assert any("risk" in c.payload.get("reason", "").lower() for c in mode_cmds)

    def test_low_risk_no_premature_escalation(self):
        """Low-risk rollbacks should NOT trigger escalation before the
        normal incident threshold."""
        bus = CommandBus(maxsize=100)
        emitter = EventEmitter()
        config = SafetyNetConfig(
            rollback_pattern_threshold=5,
            incident_rollback_threshold=10,
        )
        net = ProductionSafetyNet(command_bus=bus, config=config)
        net.register_event_handlers(emitter)

        # Emit a single low-risk rollback
        net._on_rollback(_rollback_event(
            "op_1",
            reason="test_failure",
            affected_files=["a.py"],
        ))

        cmds = []
        while bus._heap:
            _, _, cmd = bus._heap.pop(0)
            cmds.append(cmd)

        mode_cmds = [c for c in cmds if c.command_type == CommandType.REQUEST_MODE_SWITCH]
        assert len(mode_cmds) == 0
