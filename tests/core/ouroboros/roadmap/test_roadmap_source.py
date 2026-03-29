"""Tests for 'roadmap' source support in the governance pipeline.

Covers:
- intent_envelope._VALID_SOURCES includes "roadmap"
- unified_intake_router._PRIORITY_MAP includes "roadmap" at priority 4
- daemon_config.DaemonConfig has all roadmap/synthesis fields with correct defaults
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import _VALID_SOURCES
from backend.core.ouroboros.governance.intake.unified_intake_router import _PRIORITY_MAP
from backend.core.ouroboros.daemon_config import DaemonConfig


# ---------------------------------------------------------------------------
# test_roadmap_is_valid_source
# ---------------------------------------------------------------------------


def test_roadmap_is_valid_source() -> None:
    """'roadmap' must be present in the canonical valid-sources frozenset."""
    assert "roadmap" in _VALID_SOURCES, (
        f"'roadmap' not found in _VALID_SOURCES; current set: {sorted(_VALID_SOURCES)}"
    )


# ---------------------------------------------------------------------------
# test_roadmap_priority_in_map
# ---------------------------------------------------------------------------


def test_roadmap_priority_in_map() -> None:
    """'roadmap' must appear in _PRIORITY_MAP at priority 4 (same as exploration)."""
    assert "roadmap" in _PRIORITY_MAP, (
        f"'roadmap' not found in _PRIORITY_MAP; current map: {_PRIORITY_MAP}"
    )
    assert _PRIORITY_MAP["roadmap"] == 4, (
        f"'roadmap' priority should be 4 (same as exploration), "
        f"got {_PRIORITY_MAP['roadmap']}"
    )
    assert _PRIORITY_MAP["exploration"] == _PRIORITY_MAP["roadmap"], (
        "roadmap and exploration must share the same priority tier for tie-breaking by submitted_at"
    )


# ---------------------------------------------------------------------------
# test_roadmap_config_fields
# ---------------------------------------------------------------------------


def test_roadmap_config_fields() -> None:
    """DaemonConfig must expose all roadmap/synthesis fields with correct defaults."""
    cfg = DaemonConfig()

    # Roadmap sensor (Clock 1)
    assert cfg.roadmap_enabled is True
    assert cfg.roadmap_refresh_s == 3600.0
    assert cfg.roadmap_p1_enabled is True
    assert cfg.roadmap_p1_commit_limit == 50
    assert cfg.roadmap_p1_days == 30
    assert cfg.roadmap_p2_enabled is False
    assert cfg.roadmap_p3_enabled is False

    # Feature synthesis (Clock 2)
    assert cfg.synthesis_enabled is True
    assert cfg.synthesis_min_interval_s == 21600.0
    assert cfg.synthesis_ttl_s == 86400.0
    assert cfg.synthesis_prompt_version == 1


def test_roadmap_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env() must read roadmap/synthesis fields from OUROBOROS_* env vars."""
    monkeypatch.setenv("OUROBOROS_ROADMAP_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_ROADMAP_REFRESH_S", "7200.0")
    monkeypatch.setenv("OUROBOROS_ROADMAP_P1_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_ROADMAP_P1_COMMIT_LIMIT", "100")
    monkeypatch.setenv("OUROBOROS_ROADMAP_P1_DAYS", "60")
    monkeypatch.setenv("OUROBOROS_ROADMAP_P2_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_ROADMAP_P3_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_SYNTHESIS_ENABLED", "false")
    monkeypatch.setenv("OUROBOROS_SYNTHESIS_MIN_INTERVAL_S", "43200.0")
    monkeypatch.setenv("OUROBOROS_SYNTHESIS_TTL_S", "172800.0")
    monkeypatch.setenv("OUROBOROS_SYNTHESIS_PROMPT_VERSION", "2")

    cfg = DaemonConfig.from_env()

    assert cfg.roadmap_enabled is False
    assert cfg.roadmap_refresh_s == 7200.0
    assert cfg.roadmap_p1_enabled is False
    assert cfg.roadmap_p1_commit_limit == 100
    assert cfg.roadmap_p1_days == 60
    assert cfg.roadmap_p2_enabled is True
    assert cfg.roadmap_p3_enabled is True
    assert cfg.synthesis_enabled is False
    assert cfg.synthesis_min_interval_s == 43200.0
    assert cfg.synthesis_ttl_s == 172800.0
    assert cfg.synthesis_prompt_version == 2
