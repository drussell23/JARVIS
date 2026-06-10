"""Slice 204 — Chronos Continuity Matrix (non-volatile, honest uptime ledger).

Decouples operational history from volatile container memory: a disk-backed,
hash-chained ledger (.jarvis/chronos_coherence.json) that survives container
recreation. On boot it RE-CHAINS within a gap threshold instead of resetting.

The honesty guard (load-bearing): the ledger tracks TWO distinct totals —
  * total_operational_s   — evolutionary history; chains across ANY restart
    within the gap threshold (crash, reboot, OR supervised rebuild).
  * unsupervised_interval_s — the strict §41.6 metric; chains across an
    UNSUPERVISED recovery (same image), but RESETS on a SUPERVISED rebuild
    (image changed = operator intervention). A rebuild must not let us claim
    continuous *unsupervised* time — that would game the metric.

Sleep handling: heartbeat ticks compare wall-clock vs monotonic deltas; a
host freeze (macOS sleep advances wall but not monotonic) is detected,
flagged CHRONOS_SLEEP_EVENT, and the FROZEN time is NOT counted as
operational (only true running time accrues).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.chronos_ledger import (
    ChronosLedger,
    chronos_enabled,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_CHRONOS_LEDGER_ENABLED", raising=False)
    yield


def _ledger(tmp_path):
    return ChronosLedger(state_path=tmp_path / "chronos.json")


# ===========================================================================
# A — gate
# ===========================================================================

def test_disabled_by_default():
    assert chronos_enabled() is False


# ===========================================================================
# B — fresh boot
# ===========================================================================

def test_fresh_boot_starts_at_zero(tmp_path):
    led = _ledger(tmp_path)
    snap = led.rechain_on_boot(now_unix=1000.0, image_id="img-A")
    assert snap["boot_count"] == 1
    assert snap["total_operational_s"] == 0.0
    assert snap["unsupervised_interval_s"] == 0.0


# ===========================================================================
# C — heartbeat accrual
# ===========================================================================

def test_heartbeat_accrues_both_totals(tmp_path):
    led = _ledger(tmp_path)
    led.rechain_on_boot(now_unix=1000.0, image_id="A")
    led.heartbeat(now_unix=1060.0, now_monotonic=60.0)
    led.heartbeat(now_unix=1120.0, now_monotonic=120.0)
    snap = led.snapshot()
    assert snap["total_operational_s"] == pytest.approx(120.0)
    assert snap["unsupervised_interval_s"] == pytest.approx(120.0)


# ===========================================================================
# D — re-chain across restart (the core)
# ===========================================================================

def test_rechain_same_image_within_gap_is_unsupervised_recovery(tmp_path):
    p = tmp_path / "c.json"
    led1 = ChronosLedger(state_path=p)
    led1.rechain_on_boot(now_unix=1000.0, image_id="A")
    led1.heartbeat(now_unix=1300.0, now_monotonic=300.0)  # +300s both
    # restart: same image, 120s gap (crash/reboot recovery)
    led2 = ChronosLedger(state_path=p)
    snap = led2.rechain_on_boot(now_unix=1420.0, image_id="A", gap_threshold_s=1200)
    assert snap["boot_count"] == 2
    # history preserved AND unsupervised interval chained (same image)
    assert snap["total_operational_s"] == pytest.approx(300.0)
    assert snap["unsupervised_interval_s"] == pytest.approx(300.0)
    assert snap["last_event"] in ("recovery_unsupervised", "RECOVERY_UNSUPERVISED")


def test_rechain_changed_image_resets_unsupervised_but_keeps_history(tmp_path):
    p = tmp_path / "c.json"
    led1 = ChronosLedger(state_path=p)
    led1.rechain_on_boot(now_unix=1000.0, image_id="A")
    led1.heartbeat(now_unix=1300.0, now_monotonic=300.0)
    # SUPERVISED rebuild: image changed
    led2 = ChronosLedger(state_path=p)
    snap = led2.rechain_on_boot(now_unix=1360.0, image_id="B", gap_threshold_s=1200)
    # evolutionary history chains; unsupervised interval RESETS (honesty guard)
    assert snap["total_operational_s"] == pytest.approx(300.0)
    assert snap["unsupervised_interval_s"] == 0.0
    assert snap["last_event"] in ("rebuild_supervised", "REBUILD_SUPERVISED")


def test_rechain_large_gap_resets_unsupervised_keeps_history(tmp_path):
    p = tmp_path / "c.json"
    led1 = ChronosLedger(state_path=p)
    led1.rechain_on_boot(now_unix=1000.0, image_id="A")
    led1.heartbeat(now_unix=1300.0, now_monotonic=300.0)
    # huge gap (extended downtime) — same image but beyond threshold
    led2 = ChronosLedger(state_path=p)
    snap = led2.rechain_on_boot(now_unix=99999.0, image_id="A", gap_threshold_s=1200)
    assert snap["total_operational_s"] == pytest.approx(300.0)  # history kept
    assert snap["unsupervised_interval_s"] == 0.0               # continuity broke
    assert snap["last_event"] in ("downtime_reset", "DOWNTIME_RESET")


# ===========================================================================
# E — sleep / drift detection
# ===========================================================================

def test_sleep_freeze_does_not_count_frozen_time(tmp_path):
    led = _ledger(tmp_path)
    led.rechain_on_boot(now_unix=1000.0, image_id="A")
    # normal tick
    led.heartbeat(now_unix=1060.0, now_monotonic=60.0)
    # macOS sleep: wall jumps 3600s but monotonic only advanced 60s
    led.heartbeat(now_unix=4660.0, now_monotonic=120.0)
    snap = led.snapshot()
    # only the TRUE running time (monotonic) accrues, not the frozen hour
    assert snap["total_operational_s"] == pytest.approx(120.0, abs=2.0)
    assert snap["sleep_events"] >= 1


def test_clock_going_backwards_is_safe(tmp_path):
    led = _ledger(tmp_path)
    led.rechain_on_boot(now_unix=1000.0, image_id="A")
    led.heartbeat(now_unix=900.0, now_monotonic=30.0)  # wall went backwards
    snap = led.snapshot()
    assert snap["total_operational_s"] >= 0.0  # never negative


# ===========================================================================
# F — hash chain + durability + fail-soft
# ===========================================================================

def test_state_persists_and_hash_chains(tmp_path):
    p = tmp_path / "c.json"
    led = ChronosLedger(state_path=p)
    led.rechain_on_boot(now_unix=1000.0, image_id="A")
    led.heartbeat(now_unix=1060.0, now_monotonic=60.0)
    import json
    data = json.loads(p.read_text())
    assert data.get("last_hash")  # hash-chained, tamper-evident
    assert data["total_operational_s"] == pytest.approx(60.0)


def test_corrupt_state_fails_soft_to_fresh(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{garbage not json")
    led = ChronosLedger(state_path=p)
    snap = led.rechain_on_boot(now_unix=1000.0, image_id="A")
    assert snap["boot_count"] == 1  # treated as fresh, no raise


def test_heartbeat_never_raises_on_bad_path():
    led = ChronosLedger(state_path=Path("/nonexistent-x9/c.json"))
    led.rechain_on_boot(now_unix=1.0, image_id="A")
    led.heartbeat(now_unix=61.0, now_monotonic=60.0)  # must not raise


# ===========================================================================
# G — wiring pins
# ===========================================================================

def test_gls_wires_chronos_heartbeat():
    gov = Path(__file__).resolve().parents[2] / "backend" / "core" \
        / "ouroboros" / "governance"
    src = (gov / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "chronos_ledger" in src or "rechain_on_boot" in src


def test_registry_endpoint_surfaces_chronos():
    gov = Path(__file__).resolve().parents[2] / "backend" / "core" \
        / "ouroboros" / "governance"
    src = (gov / "observability_registry.py").read_text(encoding="utf-8")
    assert "chronos" in src
