"""
Tests for PRECHECK gate — 5 deterministic guards.

TDD: run before implementation → all tests must FAIL (ImportError or assertion).
Run after implementation → all tests must PASS.
"""
from __future__ import annotations

import time
from typing import List
from unittest.mock import patch

import pytest

from backend.vision.realtime.precheck_gate import PrecheckGate, PrecheckResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate(**env_overrides) -> PrecheckGate:
    """Construct a PrecheckGate with optional env-var overrides."""
    if env_overrides:
        with patch.dict("os.environ", {k: str(v) for k, v in env_overrides.items()}):
            return PrecheckGate()
    return PrecheckGate()


def _base_kwargs(**overrides):
    """Return a complete set of valid check() kwargs, with optional field overrides."""
    defaults = dict(
        frame_age_ms=100,
        fused_confidence=0.85,
        action_id="act-001",
        action_type="click",
        target_task_type="system_command",
        intent_timestamp=time.time() - 0.5,  # 500 ms ago — fresh
        is_degraded=False,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# TestFreshnessGuard
# ---------------------------------------------------------------------------

class TestFreshnessGuard:
    def test_fresh_frame_passes(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(frame_age_ms=100))
        assert "STALE_FRAME" not in result.failed_guards

    def test_stale_frame_fails(self):
        gate = _make_gate(VISION_FRESHNESS_MS=500)
        result = gate.check(**_base_kwargs(frame_age_ms=600))
        assert "STALE_FRAME" in result.failed_guards

    def test_freshness_configurable(self):
        # With a very tight threshold (50 ms), a 100 ms old frame should fail.
        gate = _make_gate(VISION_FRESHNESS_MS=50)
        result = gate.check(**_base_kwargs(frame_age_ms=100))
        assert "STALE_FRAME" in result.failed_guards

        # With a very loose threshold (1000 ms), a 100 ms old frame should pass.
        gate2 = _make_gate(VISION_FRESHNESS_MS=1000)
        result2 = gate2.check(**_base_kwargs(frame_age_ms=100))
        assert "STALE_FRAME" not in result2.failed_guards


# ---------------------------------------------------------------------------
# TestConfidenceGuard
# ---------------------------------------------------------------------------

class TestConfidenceGuard:
    def test_high_confidence_passes(self):
        gate = _make_gate(VISION_CONFIDENCE_THRESHOLD=0.75)
        result = gate.check(**_base_kwargs(fused_confidence=0.85))
        assert "LOW_CONFIDENCE" not in result.failed_guards

    def test_low_confidence_fails(self):
        gate = _make_gate(VISION_CONFIDENCE_THRESHOLD=0.75)
        result = gate.check(**_base_kwargs(fused_confidence=0.60))
        assert "LOW_CONFIDENCE" in result.failed_guards

    def test_threshold_configurable(self):
        # High threshold — 0.85 just barely fails.
        gate_strict = _make_gate(VISION_CONFIDENCE_THRESHOLD=0.90)
        result = gate_strict.check(**_base_kwargs(fused_confidence=0.85))
        assert "LOW_CONFIDENCE" in result.failed_guards

        # Low threshold — 0.60 passes.
        gate_loose = _make_gate(VISION_CONFIDENCE_THRESHOLD=0.50)
        result2 = gate_loose.check(**_base_kwargs(fused_confidence=0.60))
        assert "LOW_CONFIDENCE" not in result2.failed_guards


# ---------------------------------------------------------------------------
# TestRiskGuard
# ---------------------------------------------------------------------------

class TestRiskGuard:
    def test_safe_action_passes(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(action_type="click", target_task_type="system_command"))
        assert "RISK_REQUIRES_APPROVAL" not in result.failed_guards
        assert "DEGRADED_REQUIRES_APPROVAL" not in result.failed_guards

    def test_high_risk_email_requires_approval(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(target_task_type="email_compose"))
        assert "RISK_REQUIRES_APPROVAL" in result.failed_guards

    def test_high_risk_file_delete(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(target_task_type="file_delete"))
        assert "RISK_REQUIRES_APPROVAL" in result.failed_guards

    def test_degraded_mode_all_require_approval(self):
        gate = _make_gate()
        # Even a completely safe action type must fail in degraded mode.
        result = gate.check(**_base_kwargs(target_task_type="system_command", is_degraded=True))
        assert "DEGRADED_REQUIRES_APPROVAL" in result.failed_guards


# ---------------------------------------------------------------------------
# TestIdempotencyGuard
# ---------------------------------------------------------------------------

class TestIdempotencyGuard:
    def test_new_action_passes(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(action_id="act-001"))
        assert "IDEMPOTENCY_HIT" not in result.failed_guards

    def test_duplicate_blocked(self):
        gate = _make_gate()
        # Commit the action first (simulates a previously executed action).
        gate.commit_action("act-001")
        result = gate.check(**_base_kwargs(action_id="act-001"))
        assert "IDEMPOTENCY_HIT" in result.failed_guards

    def test_different_action_id_not_blocked(self):
        gate = _make_gate()
        gate.commit_action("act-001")
        # Different id must still pass idempotency.
        result = gate.check(**_base_kwargs(action_id="act-002"))
        assert "IDEMPOTENCY_HIT" not in result.failed_guards


# ---------------------------------------------------------------------------
# TestIntentExpiryGuard
# ---------------------------------------------------------------------------

class TestIntentExpiryGuard:
    def test_fresh_intent_passes(self):
        gate = _make_gate(VISION_INTENT_EXPIRY_S=2.0)
        result = gate.check(**_base_kwargs(intent_timestamp=time.time() - 1.0))
        assert "INTENT_EXPIRED" not in result.failed_guards

    def test_expired_intent_fails(self):
        gate = _make_gate(VISION_INTENT_EXPIRY_S=2.0)
        result = gate.check(**_base_kwargs(intent_timestamp=time.time() - 3.0))
        assert "INTENT_EXPIRED" in result.failed_guards

    def test_expiry_configurable(self):
        # Very tight expiry (0.1 s): a 0.5 s old intent expires.
        gate_tight = _make_gate(VISION_INTENT_EXPIRY_S=0.1)
        result = gate_tight.check(**_base_kwargs(intent_timestamp=time.time() - 0.5))
        assert "INTENT_EXPIRED" in result.failed_guards

        # Generous expiry (60 s): a 0.5 s old intent is fine.
        gate_loose = _make_gate(VISION_INTENT_EXPIRY_S=60.0)
        result2 = gate_loose.check(**_base_kwargs(intent_timestamp=time.time() - 0.5))
        assert "INTENT_EXPIRED" not in result2.failed_guards


# ---------------------------------------------------------------------------
# TestAllGuards
# ---------------------------------------------------------------------------

class TestAllGuards:
    def test_all_pass_returns_true(self):
        gate = _make_gate()
        kwargs = _base_kwargs(
            frame_age_ms=100,
            fused_confidence=0.90,
            action_id="act-pass-all",
            action_type="click",
            target_task_type="system_command",
            intent_timestamp=time.time() - 0.5,
            is_degraded=False,
        )
        result = gate.check(**kwargs)
        assert result.passed is True
        assert result.failed_guards == []

    def test_multiple_failures_collected(self):
        gate = _make_gate(VISION_FRESHNESS_MS=50, VISION_CONFIDENCE_THRESHOLD=0.95)
        # frame_age=200 ms > 50 ms threshold  →  STALE_FRAME
        # fused_confidence=0.80 < 0.95        →  LOW_CONFIDENCE
        result = gate.check(**_base_kwargs(frame_age_ms=200, fused_confidence=0.80))
        assert "STALE_FRAME" in result.failed_guards
        assert "LOW_CONFIDENCE" in result.failed_guards
        assert result.passed is False

    def test_internal_error_fail_closed(self):
        gate = _make_gate()
        # Patch one of the internal guard methods to raise an unexpected exception.
        with patch.object(gate, "_check_freshness", side_effect=RuntimeError("boom")):
            result = gate.check(**_base_kwargs())
        assert result.passed is False
        assert "PRECHECK_INTERNAL_ERROR" in result.failed_guards


# ---------------------------------------------------------------------------
# TestPrecheckResult
# ---------------------------------------------------------------------------

class TestPrecheckResult:
    def test_result_has_all_fields(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs())

        # Verify every required field exists and has the right type/shape.
        assert isinstance(result, PrecheckResult)
        assert isinstance(result.passed, bool)
        assert isinstance(result.failed_guards, list)
        assert isinstance(result.action_id, str)
        assert isinstance(result.frame_age_ms, (int, float))
        assert isinstance(result.fused_confidence, float)
        assert isinstance(result.risk_class, str)
        assert result.risk_class in ("safe", "elevated", "high_risk")
        assert isinstance(result.approval_required, bool)
        assert result.approval_source is None or isinstance(result.approval_source, str)
        assert isinstance(result.decision_provenance, dict)

    def test_result_approval_required_true_for_high_risk(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(target_task_type="file_delete"))
        assert result.approval_required is True

    def test_result_approval_required_false_for_safe(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs(target_task_type="system_command"))
        assert result.approval_required is False

    def test_decision_provenance_contains_guard_outcomes(self):
        gate = _make_gate()
        result = gate.check(**_base_kwargs())
        prov = result.decision_provenance
        # Every guard should leave a key in provenance.
        assert "freshness" in prov
        assert "confidence" in prov
        assert "risk" in prov
        assert "idempotency" in prov
        assert "intent_expiry" in prov
