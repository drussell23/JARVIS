"""Sovereign Zero-Shot & Decay Matrix — 1-strike timeout quarantine with TTL
forgiveness, persisted across subprocess forks (2026-06-20)."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_ttft_observer import (
    TtftObserver,
    _zeroshot_ttl_s,
    zeroshot_timeout_quarantine_enabled,
)


@pytest.fixture(autouse=True)
def _tracking_on(monkeypatch):
    monkeypatch.setenv("JARVIS_TOPOLOGY_TTFT_TRACKING_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTFT_ZEROSHOT_ENABLED", "true")


def _obs():
    return TtftObserver(path=Path(tempfile.mktemp()))


def test_one_timeout_immediately_cold_no_sigma_window():
    o = _obs()
    # A single timeout — no σ window (n>=3) needed.
    o.record_timeout("slow", op_id="op1")
    assert o.is_cold_storage("slow") is True


def test_ban_persists_to_disk():
    p = Path(tempfile.mktemp())
    o = TtftObserver(path=p)
    o.record_timeout("slow")
    payload = json.loads(p.read_text())
    assert "slow" in payload.get("zero_shot_bans", {})


def test_ban_survives_fork_rehydration():
    p = Path(tempfile.mktemp())
    TtftObserver(path=p).record_timeout("slow")
    # Fresh observer = the forked subprocess rehydrating from disk.
    assert TtftObserver(path=p).is_cold_storage("slow") is True


def test_ttl_decay_reenters_probing():
    # TTL floor is 300s (no-thrash), so age the ban timestamp directly to prove
    # the decay branch rather than sleeping. Default TTL = 28800s.
    o = _obs()
    o.record_timeout("slow")
    assert o.is_cold_storage("slow") is True
    # Backdate the ban to 9 hours ago (> the 8h default TTL).
    o._zero_shot_bans["slow"] = time.time() - (9 * 3600)
    # Read decays the expired ban → model re-enters probing.
    assert o.is_cold_storage("slow") is False
    assert "slow" not in o._zero_shot_bans  # decayed out


def test_clean_model_not_cold():
    assert _obs().is_cold_storage("fast") is False


def test_clear_drops_ban():
    o = _obs()
    o.record_timeout("slow")
    o.clear("slow")
    assert o.is_cold_storage("slow") is False


def test_disabled_no_ban(monkeypatch):
    monkeypatch.setenv("JARVIS_TTFT_ZEROSHOT_ENABLED", "0")
    o = _obs()
    o.record_timeout("slow")
    assert o.is_cold_storage("slow") is False


def test_ttl_clamped(monkeypatch):
    monkeypatch.setenv("JARVIS_TTFT_ZEROSHOT_TTL_S", "5")        # below 300 floor
    assert _zeroshot_ttl_s() == 300.0
    monkeypatch.setenv("JARVIS_TTFT_ZEROSHOT_TTL_S", "999999")    # above 24h
    assert _zeroshot_ttl_s() == 24 * 3600.0
    monkeypatch.delenv("JARVIS_TTFT_ZEROSHOT_TTL_S", raising=False)
    assert _zeroshot_ttl_s() == 28800.0


def test_default_enabled(monkeypatch):
    monkeypatch.delenv("JARVIS_TTFT_ZEROSHOT_ENABLED", raising=False)
    assert zeroshot_timeout_quarantine_enabled() is True


def test_record_timeout_never_raises():
    o = _obs()
    o.record_timeout("")        # empty
    o.record_timeout(None)      # type: ignore
    assert o.is_cold_storage("") is False
