"""Predictable Cost-Aware FSM Gate.

The escalation FSM mathematically evaluates the incoming op's token budget. If
the op's context requirement exceeds the 7B model's cognitive capacity, GPU
escalation is STRICTLY GUARANTEED (the 7B literally cannot fit the context) --
independent of urgency. Provisioning still respects the master cost gate.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_tier as ft


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("JARVIS_FAILOVER_7B_TOKEN_CAPACITY", raising=False)
    monkeypatch.delenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", raising=False)
    yield


def test_under_capacity_does_not_require_gpu():
    assert ft.op_exceeds_small_capacity(estimated_tokens=4_000) is False


def test_over_capacity_requires_gpu():
    # Default 7B working capacity ~24000 (headroom under the 32K window).
    assert ft.op_exceeds_small_capacity(estimated_tokens=30_000) is True


def test_capacity_threshold_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_7B_TOKEN_CAPACITY", "8000")
    assert ft.op_exceeds_small_capacity(estimated_tokens=9_000) is True
    assert ft.op_exceeds_small_capacity(estimated_tokens=7_000) is False


def test_zero_or_unknown_tokens_does_not_force_gpu():
    assert ft.op_exceeds_small_capacity(estimated_tokens=0) is False


# ---------------------------------------------------------------------------
# resolve_tier_for_op: token-budget overflow STRICTLY guarantees GPU (when
# quality is enabled); urgency is the other escalation path.
# ---------------------------------------------------------------------------

def test_overflow_forces_quality_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    # A BACKGROUND op that nonetheless overflows the 7B -> MUST get the GPU.
    t = ft.resolve_tier_for_op(
        urgency="background", complexity="simple", estimated_tokens=40_000,
    )
    assert t.name == "quality" and t.is_gpu is True


def test_overflow_blocked_when_quality_disabled(monkeypatch):
    """Master cost gate OFF -> no GPU even on overflow (degraded best-effort on
    7B). The NEED is real but spend is never silent."""
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "false")
    t = ft.resolve_tier_for_op(
        urgency="background", complexity="simple", estimated_tokens=40_000,
    )
    assert t.name == "survival"


def test_small_op_stays_survival(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    t = ft.resolve_tier_for_op(
        urgency="background", complexity="simple", estimated_tokens=2_000,
    )
    assert t.name == "survival"


def test_immediate_still_escalates_via_urgency(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    t = ft.resolve_tier_for_op(
        urgency="immediate", complexity="simple", estimated_tokens=1_000,
    )
    assert t.name == "quality"  # urgency path, not the token path
