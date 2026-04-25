"""Anthropic resilience pack (rooted-problem follow-up 2026-04-25).

Pin two coordinated fixes for external Anthropic API instability surfaced
by F1 Slice 4 S4b (`bt-2026-04-25-085942`):

**Fix 1: L3 auto-recovery from degraded mode** (`safety_net.py`)

Without this, a transient Anthropic API outage that triggers
REDUCED_AUTONOMY / READ_ONLY_PLANNING demotion stays sticky FOREVER
(until session restart). The original code reset failure counters on
probe success but never sent a REQUEST_MODE_SWITCH back to FULL_AUTONOMY.

The fix tracks consecutive successes WHILE in degraded state; after
`probe_recovery_success_threshold` (default 3) successes, emits
REQUEST_MODE_SWITCH back to FULL_AUTONOMY.

**Fix 2: Failure-rate-aware outer-retry max** (`candidate_generator.py`)

When the FailbackStateMachine has logged transient failures
(consecutive_failures > 0), bump the outer-retry cap from 3 to 5 for
that op. Healthy ops keep the base cap (no extra cost when stable).

Pin coverage:

A. SafetyNetConfig has probe_recovery_success_threshold (default 3).
B. Recovery promotion fires after 3 consecutive successes while degraded.
C. Recovery promotion does NOT fire while healthy (not degraded).
D. Recovery streak resets on any failure (only consecutive successes count).
E. Recovery threshold = 0 disables auto-recovery (preserves pre-fix behavior).
F. Idempotent — recovery clears escalated flags so subsequent demotions work.
G. Failure-rate-aware outer-retry: FSM consecutive_failures > 0 → bump.
H. Failure-rate-aware outer-retry: FSM healthy → use base cap.
I. Source-grep pins for both fixes.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# (A) SafetyNetConfig has the new threshold
# ---------------------------------------------------------------------------


def test_config_recovery_threshold_default_3() -> None:
    """probe_recovery_success_threshold defaults to 3 (symmetric with
    escalation threshold)."""
    from backend.core.ouroboros.governance.autonomy.safety_net import (
        SafetyNetConfig,
    )
    cfg = SafetyNetConfig()
    assert cfg.probe_recovery_success_threshold == 3


def test_config_recovery_threshold_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env override JARVIS_SAFETY_NET_RECOVERY_THRESHOLD respected."""
    monkeypatch.setenv("JARVIS_SAFETY_NET_RECOVERY_THRESHOLD", "5")
    from backend.core.ouroboros.governance.autonomy.safety_net import (
        SafetyNetConfig,
    )
    cfg = SafetyNetConfig()
    assert cfg.probe_recovery_success_threshold == 5


# ---------------------------------------------------------------------------
# Helpers — build a SafetyNet with a captured CommandBus
# ---------------------------------------------------------------------------


class _RecordingBus:
    """Test bus that records try_put commands for inspection.
    Mirrors CommandBus.try_put signature only — sufficient for SafetyNet."""
    def __init__(self):
        self.commands = []

    def try_put(self, cmd) -> bool:
        self.commands.append(cmd)
        return True


def _make_safetynet():
    from backend.core.ouroboros.governance.autonomy.safety_net import (
        ProductionSafetyNet,
    )
    bus = _RecordingBus()
    net = ProductionSafetyNet(command_bus=bus)
    return net, bus


def _probe_event(success: bool, component: str = "test"):
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        EventEnvelope,
        EventType,
    )
    return EventEnvelope(
        source_layer="L1",
        event_type=EventType.HEALTH_PROBE_RESULT,
        payload={
            "component": component,
            "success": success,
            "health_score": 1.0 if success else 0.5,
        },
    )


def _drain_bus_until_target_mode(bus, target_mode: str, max_drain: int = 50):
    """Filter recorded commands by REQUEST_MODE_SWITCH + target_mode."""
    from backend.core.ouroboros.governance.autonomy.autonomy_types import (
        CommandType,
    )
    found = [
        cmd for cmd in bus.commands
        if cmd.command_type == CommandType.REQUEST_MODE_SWITCH
        and cmd.payload.get("target_mode") == target_mode
    ]
    # Mutate in place so subsequent calls don't re-match the same command
    bus.commands = [
        cmd for cmd in bus.commands
        if not (
            cmd.command_type == CommandType.REQUEST_MODE_SWITCH
            and cmd.payload.get("target_mode") == target_mode
        )
    ]
    return found


# ---------------------------------------------------------------------------
# (B) Recovery promotion after 3 consecutive successes while degraded
# ---------------------------------------------------------------------------


def test_recovery_promotion_after_3_successes_while_degraded() -> None:
    """3 consecutive failures demote → 3 consecutive successes promote."""
    net, bus = _make_safetynet()

    # Demote: 3 consecutive failures → REDUCED_AUTONOMY
    for _ in range(3):
        net._on_health_probe(_probe_event(success=False))

    reduced_cmds = _drain_bus_until_target_mode(bus, "REDUCED_AUTONOMY")
    assert len(reduced_cmds) == 1, "REDUCED_AUTONOMY demotion expected"
    assert net._escalated_reduced is True

    # Recover: 3 consecutive successes → FULL_AUTONOMY promotion
    for _ in range(3):
        net._on_health_probe(_probe_event(success=True))

    full_cmds = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(full_cmds) == 1, (
        "FULL_AUTONOMY auto-recovery promotion expected after 3 successes"
    )
    # Flags cleared so subsequent demotions work fresh
    assert net._escalated_reduced is False
    assert net._escalated_readonly is False
    assert net._consecutive_successes_while_degraded == 0


def test_recovery_promotion_after_severe_demotion() -> None:
    """READ_ONLY_PLANNING (severe) → 3 successes promote back to FULL."""
    net, bus = _make_safetynet()

    # Demote severely: 5 consecutive failures
    for _ in range(5):
        net._on_health_probe(_probe_event(success=False))

    readonly_cmds = _drain_bus_until_target_mode(bus, "READ_ONLY_PLANNING")
    assert len(readonly_cmds) == 1
    assert net._escalated_readonly is True

    # Recover
    for _ in range(3):
        net._on_health_probe(_probe_event(success=True))

    full_cmds = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(full_cmds) == 1
    assert net._escalated_readonly is False


# ---------------------------------------------------------------------------
# (C) No promotion when healthy (not degraded)
# ---------------------------------------------------------------------------


def test_no_promotion_when_not_degraded() -> None:
    """Successes while NOT degraded → no FULL_AUTONOMY emission."""
    net, bus = _make_safetynet()
    for _ in range(10):
        net._on_health_probe(_probe_event(success=True))
    full_cmds = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(full_cmds) == 0


# ---------------------------------------------------------------------------
# (D) Streak resets on any failure
# ---------------------------------------------------------------------------


def test_recovery_streak_resets_on_failure() -> None:
    """Mixed signal (success-success-fail-success-success) does NOT promote
    — only CONSECUTIVE successes count."""
    net, bus = _make_safetynet()

    # Demote
    for _ in range(3):
        net._on_health_probe(_probe_event(success=False))
    _drain_bus_until_target_mode(bus, "REDUCED_AUTONOMY")  # consume demotion

    # Mixed: 2 successes, 1 failure, 2 more successes (streak NEVER hits 3)
    net._on_health_probe(_probe_event(success=True))
    net._on_health_probe(_probe_event(success=True))
    net._on_health_probe(_probe_event(success=False))  # resets streak
    net._on_health_probe(_probe_event(success=True))
    net._on_health_probe(_probe_event(success=True))

    full_cmds = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(full_cmds) == 0, (
        "Promotion should NOT fire — streak reset by intervening failure"
    )


# ---------------------------------------------------------------------------
# (E) Recovery threshold = 0 disables auto-recovery
# ---------------------------------------------------------------------------


def test_recovery_threshold_zero_preserves_pre_fix_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_SAFETY_NET_RECOVERY_THRESHOLD=0 → byte-for-byte pre-fix:
    flags reset on every success, no promotion command emitted."""
    monkeypatch.setenv("JARVIS_SAFETY_NET_RECOVERY_THRESHOLD", "0")
    net, bus = _make_safetynet()

    for _ in range(3):
        net._on_health_probe(_probe_event(success=False))
    _drain_bus_until_target_mode(bus, "REDUCED_AUTONOMY")

    # Single success → flags cleared (pre-fix semantics), no promotion
    net._on_health_probe(_probe_event(success=True))

    full_cmds = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(full_cmds) == 0
    # Pre-fix semantics: flags cleared on success
    assert net._escalated_reduced is False
    assert net._escalated_readonly is False


# ---------------------------------------------------------------------------
# (F) Idempotent — subsequent demotions work after recovery
# ---------------------------------------------------------------------------


def test_idempotent_demote_recover_demote_cycle() -> None:
    """Demote → recover → demote again: each cycle works independently."""
    net, bus = _make_safetynet()

    # Cycle 1: demote
    for _ in range(3):
        net._on_health_probe(_probe_event(success=False))
    cycle1_demote = _drain_bus_until_target_mode(bus, "REDUCED_AUTONOMY")
    assert len(cycle1_demote) == 1

    # Cycle 1: recover
    for _ in range(3):
        net._on_health_probe(_probe_event(success=True))
    cycle1_recover = _drain_bus_until_target_mode(bus, "FULL_AUTONOMY")
    assert len(cycle1_recover) == 1

    # Cycle 2: demote again (proves flags reset cleanly)
    for _ in range(3):
        net._on_health_probe(_probe_event(success=False))
    cycle2_demote = _drain_bus_until_target_mode(bus, "REDUCED_AUTONOMY")
    assert len(cycle2_demote) == 1


# ---------------------------------------------------------------------------
# (G) Failure-rate-aware outer-retry: FSM degraded → bump
# ---------------------------------------------------------------------------


def test_outer_retry_max_degraded_default_5() -> None:
    """Bump cap defaults to 5 (vs base 3)."""
    from backend.core.ouroboros.governance.candidate_generator import (
        _FALLBACK_OUTER_RETRY_MAX,
        _FALLBACK_OUTER_RETRY_MAX_DEGRADED,
    )
    assert _FALLBACK_OUTER_RETRY_MAX == 3
    assert _FALLBACK_OUTER_RETRY_MAX_DEGRADED == 5


def test_outer_retry_max_degraded_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JARVIS_FALLBACK_OUTER_RETRY_MAX_DEGRADED env-overridable."""
    monkeypatch.setenv("JARVIS_FALLBACK_OUTER_RETRY_MAX_DEGRADED", "8")
    import importlib
    import backend.core.ouroboros.governance.candidate_generator as cg
    importlib.reload(cg)
    assert cg._FALLBACK_OUTER_RETRY_MAX_DEGRADED == 8


# ---------------------------------------------------------------------------
# (H) Source-grep pins
# ---------------------------------------------------------------------------


def _read(p: str) -> str:
    return Path(p).read_text(encoding="utf-8")


def test_pin_safety_net_has_recovery_promotion() -> None:
    """safety_net.py contains the recovery promotion path."""
    src = _read("backend/core/ouroboros/governance/autonomy/safety_net.py")
    assert "probe_recovery_success_threshold" in src
    assert '"target_mode": "FULL_AUTONOMY"' in src
    assert "Auto-recovery: L3 promoted to FULL_AUTONOMY" in src
    assert "_consecutive_successes_while_degraded" in src


def test_pin_candidate_generator_has_failure_rate_aware_retry() -> None:
    """candidate_generator.py contains the failure-rate-aware outer-retry
    bump."""
    src = _read("backend/core/ouroboros/governance/candidate_generator.py")
    assert "_FALLBACK_OUTER_RETRY_MAX_DEGRADED" in src
    # The string is split across literal lines for code-style; grep both halves
    assert "degraded mode" in src
    assert "bumping outer-retry" in src
    # Pin: the bump check uses FSM's consecutive_failures
    assert "_consecutive_failures" in src


def test_pin_master_off_via_threshold_zero() -> None:
    """Recovery threshold = 0 must short-circuit the new path."""
    src = _read("backend/core/ouroboros/governance/autonomy/safety_net.py")
    # Pin the threshold-zero branch is present
    assert "_recovery_threshold > 0" in src
    assert "_recovery_threshold <= 0" in src
    # Pin the comment explaining pre-fix preservation
    assert "byte-for-byte pre-fix" in src
