"""Slice 195 — Adaptive Horizon Governor (the end of the 360s magic number).

The background pool's per-op watchdog ceiling was a static table (base 360s,
read_only/complex/swe_bench 900s) with a 4-file cliff. A Sovereign Organism
derives its temporal runway from the operation's actual shape: input context
size, a continuous complexity vector, and the active model's catalog profile
(a 120B+ heavy model gets a longer leash than an 8B).

Watchdog doctrine pins (Slice 47 — load-bearing):
  * The horizon is computed ONCE at worker pickup from STATIC envelope
    signals. It is NOT an activity-gated mid-run extension (the rejected
    "adaptive budget waiver" failure mode) — a wedged op still dies.
  * The governor can only RAISE above the legacy max-aggregated floor, and
    is hard-clamped by JARVIS_HORIZON_MAX_S — the anti-hang purpose survives.
  * OFF (JARVIS_ADAPTIVE_HORIZON_ENABLED=false) → byte-identical legacy
    (floor + reason pass through untouched).
  * Zero hardcoded model names — the catalog factor keys off
    dw_catalog_client.parse_parameter_count thresholds (env-tunable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptive_horizon import (
    adaptive_horizon_enabled,
    compute_horizon,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "JARVIS_ADAPTIVE_HORIZON_ENABLED",
        "JARVIS_HORIZON_MAX_S",
        "JARVIS_HORIZON_SIZE_KNEE_CHARS",
        "JARVIS_HORIZON_SIZE_FACTOR_MAX",
        "JARVIS_HORIZON_PER_FILE_FACTOR",
        "JARVIS_HORIZON_FILE_FACTOR_CAP",
        "JARVIS_HORIZON_HEAVY_PARAMS_B",
        "JARVIS_HORIZON_HEAVY_MODEL_FACTOR",
    ):
        monkeypatch.delenv(var, raising=False)


# ===========================================================================
# A — master gate + legacy passthrough
# ===========================================================================

def test_enabled_default_true():
    assert adaptive_horizon_enabled() is True


def test_disabled_is_byte_identical_legacy(monkeypatch):
    monkeypatch.setenv("JARVIS_ADAPTIVE_HORIZON_ENABLED", "false")
    s, reason = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=500_000, target_file_count=12,
        model_id="some/heavy-200B-model",
    )
    assert s == 360.0
    assert reason == "base"


def test_trivial_op_stays_at_the_legacy_floor():
    """No signals → all factors 1.0 → exactly the legacy ceiling."""
    s, reason = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=0, model_id=None,
    )
    assert s == 360.0


# ===========================================================================
# B — the three factors
# ===========================================================================

def test_context_size_raises_the_horizon():
    small, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=1_000, target_file_count=0, model_id=None,
    )
    big, reason = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=100_000, target_file_count=0, model_id=None,
    )
    assert big > small >= 360.0
    assert "adaptive" in reason


def test_size_factor_is_capped(monkeypatch):
    monkeypatch.setenv("JARVIS_HORIZON_SIZE_FACTOR_MAX", "2.0")
    capped, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=10_000_000, target_file_count=0, model_id=None,
    )
    assert capped == pytest.approx(720.0)


def test_file_count_scales_continuously_no_cliff():
    """The legacy table had a >=4-file cliff; the governor scales per-file."""
    two, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=2, model_id=None,
    )
    three, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=3, model_id=None,
    )
    assert 360.0 < two < three


def test_heavy_model_catalog_profile_extends_runway():
    """A 120B-class model id (param token parsed from the CATALOG heuristic —
    no hardcoded names) earns the heavy-model factor."""
    light, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=0, model_id="acme/tiny-7B-fast",
    )
    heavy, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=0,
        model_id="nvidia/nemotron-3-super-120B",
    )
    assert light == 360.0
    assert heavy == pytest.approx(360.0 * 1.5)


def test_unknown_model_is_factor_one():
    s, _ = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=0, target_file_count=0, model_id="mystery/no-size-token",
    )
    assert s == 360.0


# ===========================================================================
# C — watchdog doctrine: clamps
# ===========================================================================

def test_hard_max_clamp_survives_extreme_inputs(monkeypatch):
    monkeypatch.setenv("JARVIS_HORIZON_MAX_S", "1800")
    s, _ = compute_horizon(
        legacy_floor_s=900.0, legacy_reason="complex",
        context_chars=10_000_000, target_file_count=64,
        model_id="acme/galaxy-400B",
    )
    assert s == 1800.0


def test_never_below_the_legacy_floor():
    s, _ = compute_horizon(
        legacy_floor_s=900.0, legacy_reason="read_only",
        context_chars=0, target_file_count=0, model_id=None,
    )
    assert s >= 900.0


def test_never_raises_on_garbage():
    s, reason = compute_horizon(
        legacy_floor_s=360.0, legacy_reason="base",
        context_chars=-5, target_file_count=-3, model_id=12345,  # type: ignore[arg-type]
    )
    assert s >= 360.0
    assert isinstance(reason, str)


# ===========================================================================
# D — wiring + doctrine pins
# ===========================================================================

def test_pool_wires_the_governor():
    src = (_GOV / "background_agent_pool.py").read_text(encoding="utf-8")
    assert "compute_horizon" in src


def test_governor_is_static_input_only_no_ledger_coupling():
    """Slice 47 watchdog isolation: the governor must read STATIC envelope
    inputs only — no orchestrator/op-ledger/liveness imports, no asyncio."""
    src = (_GOV / "adaptive_horizon.py").read_text(encoding="utf-8")
    for forbidden in (
        "import asyncio",
        "from backend.core.ouroboros.governance.orchestrator",
        "op_ledger", "liveness", "iron_gate", "change_engine",
    ):
        assert forbidden not in src, f"watchdog coupling: {forbidden}"
