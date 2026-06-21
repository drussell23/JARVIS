"""Autonomous Fleet Latency/Entitlement Discovery + Aegis session-lifecycle fixes
(2026-06-20). Layer 1: forked soaks must not inherit a consumed aegis PSK. Layer 2:
the DW selector must skip cold-storage (latency) models, not just entitlement-banned.
"""
from __future__ import annotations

import os

from backend.core.ouroboros.governance.candidate_generator import (
    _latency_quarantine_enabled,
)
from backend.core.ouroboros.governance.graduation.live_fire_soak import (
    get_default_harness,
)


# ── Layer 1: aegis env-strip per fork ───────────────────────────────────────

def test_forked_env_strips_stale_aegis_psk_and_url(monkeypatch):
    monkeypatch.setenv("JARVIS_AEGIS_BOOTSTRAP_PSK", "STALE_CONSUMED")
    monkeypatch.setenv("JARVIS_AEGIS_URL", "http://dead-daemon:9999")
    env = get_default_harness()._build_env_for_flag("JARVIS_CURIOSITY_ENGINE_ENABLED")
    # Each fork bootstraps aegis cleanly via its own preflight.
    assert "JARVIS_AEGIS_BOOTSTRAP_PSK" not in env
    assert "JARVIS_AEGIS_URL" not in env
    # ...but the target flag + harness masters are still set.
    assert env["JARVIS_CURIOSITY_ENGINE_ENABLED"] == "true"
    assert env["JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED"] == "true"


def test_forked_env_clean_when_no_stale_aegis(monkeypatch):
    monkeypatch.delenv("JARVIS_AEGIS_BOOTSTRAP_PSK", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_URL", raising=False)
    env = get_default_harness()._build_env_for_flag("JARVIS_CURIOSITY_ENGINE_ENABLED")
    assert "JARVIS_AEGIS_BOOTSTRAP_PSK" not in env
    assert env["JARVIS_CURIOSITY_ENGINE_ENABLED"] == "true"


# ── Layer 2: latency-quarantine gate ────────────────────────────────────────

def test_latency_quarantine_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_DW_LATENCY_QUARANTINE_ENABLED", raising=False)
    assert _latency_quarantine_enabled() is True


def test_latency_quarantine_off(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_LATENCY_QUARANTINE_ENABLED", "0")
    assert _latency_quarantine_enabled() is False
    monkeypatch.setenv("JARVIS_DW_LATENCY_QUARANTINE_ENABLED", "false")
    assert _latency_quarantine_enabled() is False


def test_latency_quarantine_truthy_variants(monkeypatch):
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JARVIS_DW_LATENCY_QUARANTINE_ENABLED", v)
        assert _latency_quarantine_enabled() is True
