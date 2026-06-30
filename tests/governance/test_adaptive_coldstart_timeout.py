"""Task HW1 -- adaptive hardware-aware cold-start timeouts.

A heavy 32B/L4 node needs minutes to load ~20GB into VRAM, but the FSM's
cold-start deadlines (awaken self-heal 600s, warmup 180s) were sized for a 7B
CPU survival node -- so the heavy node got reaped before it finished loading.
These tests prove the patience is DERIVED from the awakened tier's heaviness
(GPU OR large model), that the survival/CPU path stays byte-identical (the
multiplier never applies), and that the tier is persisted + reset correctly.
"""
from __future__ import annotations

import pytest

import backend.core.ouroboros.governance.failover_lifecycle as fl
from backend.core.ouroboros.governance import provider_quarantine as pq
from backend.core.ouroboros.governance.failover_lifecycle import (
    FailoverLifecycleController,
    FailoverState,
)
from backend.core.ouroboros.governance.failover_tier import FailoverTier


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirror tests/governance/test_failover_budget_awaken.py)
# ---------------------------------------------------------------------------

class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()
    monkeypatch.setenv("JARVIS_FAILOVER_LIFECYCLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_ROUTE", "dw")
    monkeypatch.setenv("JARVIS_FAILOVER_ANY_ROUTE_OUTAGE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FAILOVER_EARLY_PREWARM_ENABLED", "false")
    yield
    pq._PROVIDER_HEALTH_GRADIENT_SINGLETON = None
    fl._reset_singleton_for_tests()


def _make_ctrl(clock, awakens=None, flares=None, **kw):
    awakens = awakens if awakens is not None else []
    flares = flares if flares is not None else []

    def _awaken(*, startup_script):
        awakens.append(startup_script)
        return True

    defaults = dict(
        vm_awaken_fn=_awaken,
        vm_delete_fn=lambda: True,
        dw_probe_fn=lambda: False,
        node_ready_fn=lambda endpoint: True,
        clock_fn=clock,
        is_degrading_fn=lambda: False,
        flare_fn=lambda payload: flares.append(payload),
    )
    defaults.update(kw)
    return FailoverLifecycleController(**defaults)


def _heavy_tier() -> FailoverTier:
    return FailoverTier(
        name="quality",
        machine_type="g2-standard-4",
        image_family="x",
        model_label="qwen2.5-coder:32b",
        accelerator_type="nvidia-l4",
        accelerator_count=1,
    )


def _survival_tier() -> FailoverTier:
    return FailoverTier(
        name="survival",
        machine_type="g2-standard-4",
        image_family="x",
        model_label="qwen2.5-coder:7b",
    )


# ---------------------------------------------------------------------------
# _tier_is_heavy: derived from the hardware request, not a static flag
# ---------------------------------------------------------------------------

def test_tier_is_heavy():
    assert fl._tier_is_heavy(_heavy_tier()) is True
    assert fl._tier_is_heavy(_survival_tier()) is False
    assert fl._tier_is_heavy(None) is False


def test_tier_is_heavy_large_cpu_model(monkeypatch):
    # No GPU but a large model (>= 14B threshold) -> still heavy.
    big_cpu = FailoverTier(
        name="quality",
        machine_type="e2-highmem-8",
        image_family="x",
        model_label="qwen2.5-coder:32b",
    )
    assert fl._tier_is_heavy(big_cpu) is True
    # A 7B model under the threshold is not heavy.
    assert fl._tier_is_heavy(_survival_tier()) is False


# ---------------------------------------------------------------------------
# _adaptive_timeout scales for heavy, byte-identical for survival/None
# ---------------------------------------------------------------------------

def test_adaptive_timeout_scales_for_heavy(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)

    ctrl._awakened_tier = _heavy_tier()
    assert ctrl._adaptive_timeout(180.0) == 720.0  # 180 * 4.0

    ctrl._awakened_tier = _survival_tier()
    assert ctrl._adaptive_timeout(180.0) == 180.0  # byte-identical

    ctrl._awakened_tier = None
    assert ctrl._adaptive_timeout(180.0) == 180.0  # byte-identical

    # Tunable multiplier: bump to 5.0 -> heavy scales accordingly.
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "5.0")
    ctrl._awakened_tier = _heavy_tier()
    assert ctrl._adaptive_timeout(180.0) == 900.0


def test_mult_one_disables(monkeypatch):
    monkeypatch.setenv("JARVIS_JPRIME_HEAVY_COLDSTART_MULT", "1.0")
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    ctrl._awakened_tier = _heavy_tier()
    assert ctrl._adaptive_timeout(180.0) == 180.0


# ---------------------------------------------------------------------------
# _do_awaken persists the WHOLE tier object, not just the label
# ---------------------------------------------------------------------------

async def test_do_awaken_persists_tier(monkeypatch):
    # Arm the quality tier so resolve_tier returns the heavy 32B/GPU spec.
    monkeypatch.setenv("JARVIS_FAILOVER_QUALITY_TIER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FAILOVER_AWAKEN_URGENCY", "immediate")
    monkeypatch.setenv("JARVIS_FAILOVER_AWAKEN_COMPLEXITY", "complex")
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    assert ctrl._awakened_tier is None

    await ctrl._do_awaken()

    assert isinstance(ctrl._awakened_tier, FailoverTier)
    assert ctrl._awakened_tier.is_gpu is True
    assert ctrl._active_model_label == ctrl._awakened_tier.model_label
    assert fl._tier_is_heavy(ctrl._awakened_tier) is True


async def test_do_awaken_survival_tier_not_heavy(monkeypatch):
    # Quality gate OFF (default) -> survival 7B/CPU tier -> not heavy.
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    await ctrl._do_awaken()
    assert isinstance(ctrl._awakened_tier, FailoverTier)
    assert ctrl._awakened_tier.is_gpu is False
    assert fl._tier_is_heavy(ctrl._awakened_tier) is False
    assert ctrl._adaptive_timeout(600.0) == 600.0  # byte-identical


# ---------------------------------------------------------------------------
# A return to DORMANT nulls the awakened tier (no stale heavy inflation)
# ---------------------------------------------------------------------------

async def test_dormant_resets_tier(monkeypatch):
    clock = FakeClock()
    ctrl = _make_ctrl(clock)
    # Simulate an awakened heavy tier sitting in AWAKENING.
    ctrl._awakened_tier = _heavy_tier()
    ctrl._state = FailoverState.AWAKENING
    ctrl._awakening_started_at = clock.t
    # Push the clock past the (adaptive) self-heal deadline so the reap fires.
    clock.t += ctrl._adaptive_timeout(fl._awaken_timeout_s()) + 10.0

    await ctrl._tick_awakening(now=clock.t)

    assert ctrl.state == FailoverState.DORMANT
    assert ctrl._awakened_tier is None
