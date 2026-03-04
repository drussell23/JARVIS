"""Tests for DecisionEnvelope, PressurePolicy, and ActuatorAction types.

Covers:
 1. DecisionEnvelope is frozen (FrozenInstanceError on mutation)
 2. is_stale returns True for old epoch
 3. is_stale returns True for same epoch, old sequence
 4. is_stale returns False when current
 5. PressurePolicy default thresholds cover all actionable tiers
 6. PressurePolicy exit < enter for all tiers (hysteresis)
 7. PressurePolicy.for_ram_gb(16.0) produces consumer thresholds
 8. PressurePolicy.for_ram_gb(64.0) produces server thresholds
 9. ActuatorAction values are correct
10. ActuatorAction.priority ordering: DISPLAY_SHED < CLEANUP
11. PressurePolicy dwell/cooldown positive
12. PressurePolicy version starts with "v"
"""

from __future__ import annotations

import dataclasses
import time

import pytest

from backend.core.memory_types import (
    ActuatorAction,
    DecisionEnvelope,
    PressurePolicy,
    PressureTier,
)


# ===================================================================
# DecisionEnvelope tests
# ===================================================================

class TestDecisionEnvelope:
    """Tests for the DecisionEnvelope frozen dataclass."""

    @staticmethod
    def _make_envelope(
        *,
        epoch: int = 5,
        sequence: int = 10,
        snapshot_id: str = "snap-001",
        policy_version: str = "v1.0",
        pressure_tier: PressureTier = PressureTier.ELEVATED,
        timestamp: float | None = None,
    ) -> DecisionEnvelope:
        return DecisionEnvelope(
            snapshot_id=snapshot_id,
            epoch=epoch,
            sequence=sequence,
            policy_version=policy_version,
            pressure_tier=pressure_tier,
            timestamp=timestamp if timestamp is not None else time.time(),
        )

    def test_frozen_raises_on_mutation(self) -> None:
        """DecisionEnvelope must be frozen -- mutation raises FrozenInstanceError."""
        envelope = self._make_envelope()
        with pytest.raises(dataclasses.FrozenInstanceError):
            envelope.epoch = 99  # type: ignore[misc]

    def test_is_stale_old_epoch(self) -> None:
        """Envelope with an older epoch is stale."""
        envelope = self._make_envelope(epoch=3, sequence=10)
        assert envelope.is_stale(current_epoch=5, current_sequence=1) is True

    def test_is_stale_same_epoch_old_sequence(self) -> None:
        """Same epoch but older sequence number is stale."""
        envelope = self._make_envelope(epoch=5, sequence=7)
        assert envelope.is_stale(current_epoch=5, current_sequence=10) is True

    def test_is_stale_false_when_current(self) -> None:
        """Envelope matching current epoch/sequence is NOT stale."""
        envelope = self._make_envelope(epoch=5, sequence=10)
        assert envelope.is_stale(current_epoch=5, current_sequence=10) is False

    def test_is_stale_false_when_ahead(self) -> None:
        """Envelope ahead of current epoch/sequence is NOT stale."""
        envelope = self._make_envelope(epoch=6, sequence=1)
        assert envelope.is_stale(current_epoch=5, current_sequence=10) is False

    def test_is_stale_same_epoch_same_sequence(self) -> None:
        """Exact match on epoch and sequence is not stale."""
        envelope = self._make_envelope(epoch=5, sequence=5)
        assert envelope.is_stale(current_epoch=5, current_sequence=5) is False


# ===================================================================
# PressurePolicy tests
# ===================================================================

# The four actionable tiers that PressurePolicy must cover.
_ACTIONABLE_TIERS = frozenset({
    PressureTier.ELEVATED,
    PressureTier.CONSTRAINED,
    PressureTier.CRITICAL,
    PressureTier.EMERGENCY,
})


class TestPressurePolicy:
    """Tests for the PressurePolicy frozen dataclass."""

    def test_default_thresholds_cover_actionable_tiers(self) -> None:
        """Default enter/exit thresholds must cover all four actionable tiers."""
        policy = PressurePolicy()
        for tier in _ACTIONABLE_TIERS:
            assert tier in policy.enter_thresholds, f"Missing enter threshold for {tier}"
            assert tier in policy.exit_thresholds, f"Missing exit threshold for {tier}"

    def test_exit_less_than_enter_hysteresis(self) -> None:
        """Exit threshold must be strictly less than enter threshold (hysteresis deadband)."""
        policy = PressurePolicy()
        for tier in _ACTIONABLE_TIERS:
            assert policy.exit_thresholds[tier] < policy.enter_thresholds[tier], (
                f"Hysteresis violated for {tier}: "
                f"exit={policy.exit_thresholds[tier]} >= enter={policy.enter_thresholds[tier]}"
            )

    def test_for_ram_gb_consumer_16(self) -> None:
        """16 GB Mac should get consumer thresholds."""
        policy = PressurePolicy.for_ram_gb(16.0)
        assert policy.version == "v1.0-consumer"
        # Consumer enter thresholds: 80/88/93/96
        assert policy.enter_thresholds[PressureTier.ELEVATED] == 80.0
        assert policy.enter_thresholds[PressureTier.CONSTRAINED] == 88.0
        assert policy.enter_thresholds[PressureTier.CRITICAL] == 93.0
        assert policy.enter_thresholds[PressureTier.EMERGENCY] == 96.0
        # Consumer exit thresholds: 75/84/90/93
        assert policy.exit_thresholds[PressureTier.ELEVATED] == 75.0
        assert policy.exit_thresholds[PressureTier.CONSTRAINED] == 84.0
        assert policy.exit_thresholds[PressureTier.CRITICAL] == 90.0
        assert policy.exit_thresholds[PressureTier.EMERGENCY] == 93.0

    def test_for_ram_gb_server_64(self) -> None:
        """64 GB+ should get server thresholds."""
        policy = PressurePolicy.for_ram_gb(64.0)
        assert policy.version == "v1.0-server"
        # Server enter thresholds: 55/65/80/90
        assert policy.enter_thresholds[PressureTier.ELEVATED] == 55.0
        assert policy.enter_thresholds[PressureTier.CONSTRAINED] == 65.0
        assert policy.enter_thresholds[PressureTier.CRITICAL] == 80.0
        assert policy.enter_thresholds[PressureTier.EMERGENCY] == 90.0
        # Server exit thresholds: 50/60/75/85
        assert policy.exit_thresholds[PressureTier.ELEVATED] == 50.0
        assert policy.exit_thresholds[PressureTier.CONSTRAINED] == 60.0
        assert policy.exit_thresholds[PressureTier.CRITICAL] == 75.0
        assert policy.exit_thresholds[PressureTier.EMERGENCY] == 85.0

    def test_for_ram_gb_constrained_8(self) -> None:
        """8 GB (< 12 GB) should get constrained thresholds."""
        policy = PressurePolicy.for_ram_gb(8.0)
        assert policy.version == "v1.0-constrained"
        assert policy.enter_thresholds[PressureTier.ELEVATED] == 85.0
        assert policy.enter_thresholds[PressureTier.EMERGENCY] == 97.0

    def test_for_ram_gb_prosumer_32(self) -> None:
        """32 GB (< 48 GB) should get prosumer thresholds."""
        policy = PressurePolicy.for_ram_gb(32.0)
        assert policy.version == "v1.0-prosumer"
        assert policy.enter_thresholds[PressureTier.ELEVATED] == 65.0
        assert policy.enter_thresholds[PressureTier.EMERGENCY] == 93.0

    def test_for_ram_gb_hysteresis_all_profiles(self) -> None:
        """Every profile from for_ram_gb must maintain exit < enter for all tiers."""
        for gb in (8.0, 16.0, 32.0, 64.0, 128.0):
            policy = PressurePolicy.for_ram_gb(gb)
            for tier in _ACTIONABLE_TIERS:
                assert policy.exit_thresholds[tier] < policy.enter_thresholds[tier], (
                    f"Hysteresis violated for {gb}GB profile, tier {tier}"
                )

    def test_dwell_and_cooldown_positive(self) -> None:
        """min_dwell_seconds and cooldown_seconds must be positive."""
        policy = PressurePolicy()
        assert policy.min_dwell_seconds > 0.0
        assert policy.cooldown_seconds > 0.0

    def test_version_starts_with_v(self) -> None:
        """Policy version string must start with 'v'."""
        policy = PressurePolicy()
        assert policy.version.startswith("v")

    def test_max_actions_per_hour_positive(self) -> None:
        """max_actions_per_hour must be a positive integer."""
        policy = PressurePolicy()
        assert policy.max_actions_per_hour > 0

    def test_frozen(self) -> None:
        """PressurePolicy should be frozen."""
        policy = PressurePolicy()
        with pytest.raises(dataclasses.FrozenInstanceError):
            policy.version = "v2.0"  # type: ignore[misc]


# ===================================================================
# ActuatorAction tests
# ===================================================================

class TestActuatorAction:
    """Tests for the ActuatorAction enum."""

    def test_values_correct(self) -> None:
        """All six expected values must exist with correct string values."""
        assert ActuatorAction.DISPLAY_SHED.value == "display_shed"
        assert ActuatorAction.DEFCON_ESCALATE.value == "defcon_escalate"
        assert ActuatorAction.MODEL_EVICT.value == "model_evict"
        assert ActuatorAction.CLOUD_OFFLOAD.value == "cloud_offload"
        assert ActuatorAction.CLOUD_SCALE.value == "cloud_scale"
        assert ActuatorAction.CLEANUP.value == "cleanup"

    def test_priority_ordering_display_shed_less_than_cleanup(self) -> None:
        """DISPLAY_SHED (priority 0) must be lower priority number than CLEANUP (priority 5)."""
        assert ActuatorAction.DISPLAY_SHED.priority < ActuatorAction.CLEANUP.priority

    def test_priority_monotonic_with_enum_order(self) -> None:
        """Priorities must be monotonically increasing in the declared order."""
        ordered = [
            ActuatorAction.DISPLAY_SHED,
            ActuatorAction.DEFCON_ESCALATE,
            ActuatorAction.MODEL_EVICT,
            ActuatorAction.CLOUD_OFFLOAD,
            ActuatorAction.CLOUD_SCALE,
            ActuatorAction.CLEANUP,
        ]
        for i in range(len(ordered) - 1):
            assert ordered[i].priority < ordered[i + 1].priority, (
                f"{ordered[i].name}.priority ({ordered[i].priority}) "
                f">= {ordered[i + 1].name}.priority ({ordered[i + 1].priority})"
            )

    def test_all_members_have_priority(self) -> None:
        """Every ActuatorAction member must have a valid integer priority."""
        for action in ActuatorAction:
            p = action.priority
            assert isinstance(p, int), f"{action.name}.priority is not int"
            assert p >= 0, f"{action.name}.priority is negative"

    def test_six_members(self) -> None:
        """There must be exactly 6 ActuatorAction members."""
        assert len(ActuatorAction) == 6
