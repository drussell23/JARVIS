"""Slice 206 — Boot-Warmup Lifecycle + honest starvation reclassification.

Diagnosis (Slice 206 prelude): the 59 control-plane-starvation events were
MISLEADING — they conflated one-time boot-warmup blocking (heavy semantic /
posture / oracle init) with genuine steady-state starvation. Steady-state
paths run in microseconds; the offload utilities (build_async /
build_bundle_async / build_offloaded) already exist.

This slice makes the metric HONEST rather than gaming it:
  * A BOOT_WARMUP → STEADY_STATE lifecycle. During warmup, loop lag is
    recorded as a DISTINCT, VISIBLE ``warmup_lag`` counter — not hidden — and
    ``control_plane_starvation_events`` only counts POST-warmup events (the
    "something is wrong" signal then means what it claims).
  * Anti-gaming guard: a HARD warmup deadline force-transitions to
    STEADY_STATE regardless of any signal, so "warmup" can never be claimed
    indefinitely to mask real starvation.
  * Proactive off-loop warmup pre-warms the heavy builds via the existing
    thread-offloaded paths so they never block the loop on first lazy use.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.init_lifecycle import (
    LifecyclePhase,
    current_phase,
    in_warmup,
    init_lifecycle_enabled,
    mark_warmup_complete,
    reset_for_tests,
    start_warmup,
)

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" \
    / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_INIT_LIFECYCLE_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_INIT_WARMUP_MAX_S", raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


# ===========================================================================
# A — gate
# ===========================================================================

def test_disabled_by_default():
    assert init_lifecycle_enabled() is False


def test_disabled_reports_steady_state(monkeypatch):
    # When the lifecycle is off, the system is treated as steady (no warmup
    # window) so behavior is byte-identical to pre-206.
    monkeypatch.setenv("JARVIS_INIT_LIFECYCLE_ENABLED", "false")
    start_warmup(now=1000.0)
    assert in_warmup(now=1001.0) is False
    assert current_phase(now=1001.0) is LifecyclePhase.STEADY_STATE


# ===========================================================================
# B — warmup → steady-state transition
# ===========================================================================

def test_starts_in_warmup(monkeypatch):
    monkeypatch.setenv("JARVIS_INIT_LIFECYCLE_ENABLED", "1")
    start_warmup(now=1000.0)
    assert in_warmup(now=1010.0) is True
    assert current_phase(now=1010.0) is LifecyclePhase.BOOT_WARMUP


def test_explicit_complete_transitions(monkeypatch):
    monkeypatch.setenv("JARVIS_INIT_LIFECYCLE_ENABLED", "1")
    start_warmup(now=1000.0)
    mark_warmup_complete(now=1030.0)
    assert in_warmup(now=1031.0) is False
    assert current_phase(now=1031.0) is LifecyclePhase.STEADY_STATE


# ===========================================================================
# C — the anti-gaming hard deadline (the honesty guard)
# ===========================================================================

def test_hard_deadline_forces_steady_state(monkeypatch):
    """Warmup can NEVER be claimed indefinitely — past the max window the
    system is steady-state regardless of any completion signal."""
    monkeypatch.setenv("JARVIS_INIT_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_INIT_WARMUP_MAX_S", "180")
    start_warmup(now=1000.0)
    assert in_warmup(now=1000.0 + 179) is True
    assert in_warmup(now=1000.0 + 181) is False   # deadline → steady, no signal
    assert current_phase(now=1000.0 + 181) is LifecyclePhase.STEADY_STATE


def test_never_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("JARVIS_INIT_LIFECYCLE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_INIT_WARMUP_MAX_S", "not-a-number")
    start_warmup(now=1000.0)
    assert isinstance(in_warmup(now=1010.0), bool)  # bad env → default window


# ===========================================================================
# D — registry warmup_lag counter + honest watchdog reclassification
# ===========================================================================

def test_registry_has_warmup_lag_counter(monkeypatch, tmp_path):
    # Hermetic registry path so suite ordering can't leave a stale .bin.
    monkeypatch.setenv(
        "JARVIS_OBSERVABILITY_REGISTRY_PATH", str(tmp_path / "reg.bin"),
    )
    monkeypatch.delenv("JARVIS_OBSERVABILITY_REGISTRY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.observability_registry import (
        WARMUP_LAG, _reset_singleton_for_tests, get_observability_registry,
        record_warmup_lag,
    )
    _reset_singleton_for_tests()
    snap = get_observability_registry().snapshot()
    assert WARMUP_LAG in snap and snap[WARMUP_LAG] == 0
    record_warmup_lag()
    assert get_observability_registry().get(WARMUP_LAG) == 1
    _reset_singleton_for_tests()


def test_watchdog_reclassifies_warmup_lag():
    """The watchdog must record warmup-window lag as warmup_lag, NOT as
    steady-state starvation (the honest reclassification, not suppression)."""
    src = (_GOV / "control_plane_watchdog.py").read_text(encoding="utf-8")
    assert "in_warmup" in src
    assert "record_warmup_lag" in src


# ===========================================================================
# E — wiring pins
# ===========================================================================

def test_gls_drives_warmup_lifecycle():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "start_warmup" in src
    assert "mark_warmup_complete" in src
    assert "WARMUP_COMPLETE" in src


def test_gls_proactively_warms_via_existing_offload_paths():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    # uses the pre-existing thread-offloaded builds, not new sync calls
    assert any(
        u in src for u in ("build_async", "build_offloaded", "build_bundle_async")
    )
