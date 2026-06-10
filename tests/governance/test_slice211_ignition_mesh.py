"""Slice 211 — Strategic Ignition Mesh: wire the roadmap orchestrator + the
adaptive (recovering) stress-aware cadence.

The GOAL-001 autonomy test found roadmap_orchestrator has ZERO callers in the
live loop. This slice plugs it into the GLS boot as a deferred daemon driving
single-poll bursts on an adaptive cadence. The cadence formula is CORRECTED
from the proposed cumulative version (which never recovers) to a recent-rate
backoff that returns to baseline when the vendor stabilizes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.roadmap_cadence import (
    AdaptiveRoadmapCadence,
    compute_adaptive_interval,
)

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" \
    / "ouroboros" / "governance"


# ===========================================================================
# A — the corrected (recovering) adaptive formula
# ===========================================================================

def test_no_stress_returns_base():
    assert compute_adaptive_interval(120.0, exhaustion_delta=0, max_s=1800) == 120.0


def test_stress_backs_off_on_RECENT_rate():
    # delta of 5 exhaustions since last poll → 120 * (1+5) = 720s
    assert compute_adaptive_interval(120.0, exhaustion_delta=5, max_s=1800) == 720.0


def test_backoff_is_capped():
    assert compute_adaptive_interval(120.0, exhaustion_delta=100, max_s=1800) == 1800.0


def test_recovers_to_base_when_stress_subsides():
    """The whole point vs the cumulative formula: once the recent delta drops
    back to 0, the interval returns to base (a cumulative counter never would)."""
    high = compute_adaptive_interval(120.0, exhaustion_delta=8, max_s=1800)
    recovered = compute_adaptive_interval(120.0, exhaustion_delta=0, max_s=1800)
    assert high > recovered == 120.0


def test_never_raises_on_garbage():
    assert compute_adaptive_interval(-1, exhaustion_delta=-9, max_s=0) >= 1.0


# ===========================================================================
# B — the cadence tracker derives DELTA, not cumulative
# ===========================================================================

def test_cadence_first_call_anchors_to_base(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "r.bin"))
    monkeypatch.delenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.observability_registry import (
        _reset_singleton_for_tests,
    )
    _reset_singleton_for_tests()
    cad = AdaptiveRoadmapCadence()
    # first call: no prior reading → delta 0 → base
    assert cad.next_interval_s() == 120.0
    _reset_singleton_for_tests()


def test_cadence_backs_off_on_new_exhaustions_then_recovers(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "r.bin"))
    monkeypatch.delenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.observability_registry import (
        _reset_singleton_for_tests, get_observability_registry,
        record_provider_exhaustion,
    )
    _reset_singleton_for_tests()
    reg = get_observability_registry()
    cad = AdaptiveRoadmapCadence()
    cad.next_interval_s()                       # anchor at 0
    for _ in range(3):
        record_provider_exhaustion()            # +3 since last poll
    backed_off = cad.next_interval_s()          # delta=3 → 120*4 = 480
    assert backed_off == 480.0
    recovered = cad.next_interval_s()           # delta=0 now → back to 120
    assert recovered == 120.0
    _reset_singleton_for_tests()


# ===========================================================================
# C — wiring pins
# ===========================================================================

def test_gls_wires_roadmap_orchestrator():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "execute_roadmap" in src
    assert "AdaptiveRoadmapCadence" in src or "next_interval_s" in src


def test_gls_drives_single_poll_bursts_not_engine_py():
    """Wire into the LIVE loop (GLS), and drive single-poll bursts so the
    adaptive cadence (not the orchestrator's fixed internal timer) owns the
    timing."""
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "max_iterations_override" in src
