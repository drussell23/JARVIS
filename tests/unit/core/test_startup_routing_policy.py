"""Tests for StartupRoutingPolicy — deadline-based deterministic fallback during boot.

Disease 10 — Startup Sequencing, Task 5.
"""

from __future__ import annotations

import time

import pytest

from backend.core.startup_routing_policy import (
    BootRoutingDecision,
    DecisionLogEntry,
    FallbackReason,
    StartupRoutingPolicy,
)


# ---------------------------------------------------------------------------
# TestBootRoutingDecision
# ---------------------------------------------------------------------------


class TestBootRoutingDecision:
    """Verify enum members exist and are complete."""

    def test_all_decisions_exist(self):
        expected = {"PENDING", "GCP_PRIME", "LOCAL_MINIMAL", "CLOUD_CLAUDE", "DEGRADED"}
        actual = {member.name for member in BootRoutingDecision}
        assert actual == expected
        assert len(BootRoutingDecision) == 5


# ---------------------------------------------------------------------------
# TestPolicyDuringBoot
# ---------------------------------------------------------------------------


class TestPolicyDuringBoot:
    """Verify decide() logic under various signal combinations."""

    def test_pending_before_any_signal(self):
        """With no signals fired, decide() returns PENDING."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.PENDING
        assert reason == FallbackReason.NONE

    def test_gcp_ready_before_deadline(self):
        """GCP signalled ready before deadline -> GCP_PRIME."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready("10.0.0.1", 8080)

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.GCP_PRIME
        assert reason == FallbackReason.NONE

    def test_deadline_expired_with_local(self):
        """Deadline expired + local model loaded -> LOCAL_MINIMAL."""
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0)  # instant expiry
        policy.signal_local_model_loaded()

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    def test_deadline_expired_no_local_with_cloud(self):
        """Deadline expired, no local model, cloud enabled -> CLOUD_CLAUDE."""
        policy = StartupRoutingPolicy(
            gcp_deadline_s=0.0,
            cloud_fallback_enabled=True,
        )

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_DEADLINE_EXPIRED

    def test_deadline_expired_no_local_no_cloud(self):
        """Deadline expired, no local, no cloud -> DEGRADED."""
        policy = StartupRoutingPolicy(
            gcp_deadline_s=0.0,
            cloud_fallback_enabled=False,
        )

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.DEGRADED
        assert reason == FallbackReason.NO_AVAILABLE_PATH

    def test_gcp_revoked_falls_back_to_local(self):
        """GCP was ready then revoked, local model available -> LOCAL_MINIMAL."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready("10.0.0.1", 8080)
        policy.signal_local_model_loaded()
        policy.signal_gcp_revoked("instance preempted")

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_REVOKED

    def test_gcp_revoked_no_local_falls_to_cloud(self):
        """GCP revoked, no local model, cloud enabled -> CLOUD_CLAUDE."""
        policy = StartupRoutingPolicy(
            gcp_deadline_s=60.0,
            cloud_fallback_enabled=True,
        )
        policy.signal_gcp_ready("10.0.0.1", 8080)
        policy.signal_gcp_revoked("quota exceeded")

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.CLOUD_CLAUDE
        assert reason == FallbackReason.GCP_REVOKED

    def test_gcp_handshake_failed_with_local(self):
        """Handshake failure with local model loaded -> LOCAL_MINIMAL."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready("10.0.0.1", 8080)
        policy.signal_local_model_loaded()
        policy.signal_gcp_handshake_failed("capabilities mismatch")

        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.LOCAL_MINIMAL
        assert reason == FallbackReason.GCP_HANDSHAKE_FAILED

    def test_gcp_deadline_remaining_decreases(self):
        """gcp_deadline_remaining should decrease over time and floor at 0."""
        policy = StartupRoutingPolicy(gcp_deadline_s=0.05)
        assert policy.gcp_deadline_remaining > 0.0

        # After the deadline passes, remaining should be 0.
        time.sleep(0.06)
        assert policy.gcp_deadline_remaining == 0.0


# ---------------------------------------------------------------------------
# TestPolicyFinalization
# ---------------------------------------------------------------------------


class TestPolicyFinalization:
    """Verify finalize() locks the policy and future signals are ignored."""

    def test_finalize_locks_decision(self):
        """After finalize(), decide() returns the same decision repeatedly."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.signal_gcp_ready("10.0.0.1", 8080)

        decision1, reason1 = policy.decide()
        assert decision1 == BootRoutingDecision.GCP_PRIME

        policy.finalize()
        assert policy.is_finalized is True

        # Even after new signals, decision is locked.
        policy.signal_gcp_revoked("should be ignored")
        decision2, reason2 = policy.decide()
        assert decision2 == BootRoutingDecision.GCP_PRIME
        assert reason2 == FallbackReason.NONE

    def test_signals_after_finalize_are_ignored(self):
        """Signals after finalize() must not change internal state."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.finalize()

        # These should all be no-ops.
        policy.signal_gcp_ready("10.0.0.1", 8080)
        policy.signal_gcp_revoked("should be ignored")
        policy.signal_local_model_loaded()
        policy.signal_gcp_handshake_failed("should be ignored")

        decision, reason = policy.decide()
        # Nothing was signalled before finalize, so PENDING.
        assert decision == BootRoutingDecision.PENDING
        assert reason == FallbackReason.NONE


# ---------------------------------------------------------------------------
# TestPolicyObservability
# ---------------------------------------------------------------------------


class TestPolicyObservability:
    """Verify decision_log records transitions correctly."""

    def test_decision_log_records_transitions(self):
        """Each call to decide() appends a DecisionLogEntry."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)

        # First decision — PENDING.
        d1, _ = policy.decide()
        assert d1 == BootRoutingDecision.PENDING

        # Signal GCP ready and decide again — GCP_PRIME.
        policy.signal_gcp_ready("10.0.0.1", 8080)
        d2, _ = policy.decide()
        assert d2 == BootRoutingDecision.GCP_PRIME

        log = policy.decision_log
        assert len(log) == 2

        assert isinstance(log[0], DecisionLogEntry)
        assert log[0].decision == BootRoutingDecision.PENDING
        assert log[0].reason == FallbackReason.NONE
        assert isinstance(log[0].timestamp, float)

        assert log[1].decision == BootRoutingDecision.GCP_PRIME
        assert log[1].reason == FallbackReason.NONE

        # Log should be ordered by timestamp.
        assert log[0].timestamp <= log[1].timestamp

    def test_decision_log_is_a_copy(self):
        """Mutating the returned log must not affect internal state."""
        policy = StartupRoutingPolicy(gcp_deadline_s=60.0)
        policy.decide()

        log = policy.decision_log
        assert len(log) == 1
        log.clear()
        # Internal log unaffected.
        assert len(policy.decision_log) == 1

    def test_decision_log_entry_has_detail(self):
        """DecisionLogEntry includes detail string from fallback reason context."""
        policy = StartupRoutingPolicy(gcp_deadline_s=0.0, cloud_fallback_enabled=False)
        decision, reason = policy.decide()
        assert decision == BootRoutingDecision.DEGRADED

        log = policy.decision_log
        assert len(log) == 1
        # Detail should be a non-empty string explaining the degraded state.
        assert isinstance(log[0].detail, str)
