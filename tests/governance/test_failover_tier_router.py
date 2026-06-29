"""Adaptive Workload Provisioning -- the Intelligent Tier Router.

J-Prime is a TEMPORARY, cost-bounded survival tier. By default O+V provisions the
cheap e2-highmem-2 + 7B node. For a high-priority IMMEDIATE/COMPLEX op the router
MAY escalate to a g2-standard GPU + 32B node -- but ONLY when the quality tier is
explicitly enabled (gated OFF by default so a GPU node can NEVER spend by
accident). Pure, deterministic, config-driven -- no hardcoded machine/model.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.failover_tier import (
    FailoverTier,
    quality_tier,
    resolve_tier,
)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Clean slate -- tier specs resolve from defaults unless a test overrides.
    for k in list(__import__("os").environ):
        if k.startswith("JARVIS_FAILOVER_SURVIVAL_") or k.startswith("JARVIS_FAILOVER_QUALITY_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", raising=False)
    yield


def test_survival_tier_for_background():
    t = resolve_tier(urgency="background", complexity="simple")
    assert t.name == "survival"
    assert "e2" in t.machine_type and t.is_gpu is False
    assert "7b" in t.model_label.lower()


def test_survival_tier_for_standard():
    assert resolve_tier(urgency="standard", complexity="moderate").name == "survival"


def test_quality_disabled_means_survival_even_for_immediate(monkeypatch):
    """Master OFF (default) -> a GPU node can NEVER be provisioned by accident."""
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "false")
    t = resolve_tier(urgency="immediate", complexity="complex")
    assert t.name == "survival"
    assert t.is_gpu is False


def test_immediate_escalates_to_quality_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    t = resolve_tier(urgency="immediate", complexity="simple")
    assert t.name == "quality"
    assert t.is_gpu is True
    assert t.accelerator_count >= 1 and "l4" in t.accelerator_type.lower()
    assert "32b" in t.model_label.lower()


def test_complex_escalates_to_quality_when_enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    assert resolve_tier(urgency="standard", complexity="complex").name == "quality"


def test_background_stays_survival_even_when_quality_enabled(monkeypatch):
    """Quality enabled, but a BACKGROUND op never warrants the GPU OPEX."""
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    assert resolve_tier(urgency="background", complexity="simple").name == "survival"


def test_tier_specs_are_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MACHINE", "g2-standard-16")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MODEL", "qwen2.5-coder:32b-instruct")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_ACCEL_TYPE", "nvidia-l4")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_ACCEL_COUNT", "2")
    t = resolve_tier(urgency="immediate", complexity="complex")
    assert t.machine_type == "g2-standard-16"
    assert t.accelerator_count == 2
    assert t.model_label == "qwen2.5-coder:32b-instruct"


def test_failover_tier_is_frozen_value():
    t = resolve_tier(urgency="background", complexity="simple")
    assert isinstance(t, FailoverTier)
    with pytest.raises(Exception):
        t.machine_type = "x"  # type: ignore[misc]  -- frozen


# -- quality_tier(): the bake-time source of truth ---------------------------
def test_quality_tier_accessor_returns_quality_spec():
    t = quality_tier()
    assert t.name == "quality"
    assert t.image_family == "jarvis-prime-coder-32b"
    assert t.model_label == "qwen2.5-coder:32b"
    assert t.is_gpu is True


def test_quality_tier_bypasses_the_cost_gate(monkeypatch):
    """The cost gate governs PROVISIONING, not BAKING. With the gate OFF (default)
    resolve_tier returns survival -- but the bake-time accessor MUST still dictate
    the quality spec, else the baker would manufacture the 7B survival image."""
    monkeypatch.delenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", raising=False)
    assert resolve_tier(urgency="immediate", complexity="complex").name == "survival"
    assert quality_tier().name == "quality"
    assert "32b" in quality_tier().model_label.lower()


def test_quality_tier_honors_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_IMAGE", "fam-x")
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_MODEL", "model-x:7b")
    t = quality_tier()
    assert t.image_family == "fam-x"
    assert t.model_label == "model-x:7b"
