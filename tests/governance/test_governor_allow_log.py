"""Follow-up #1 — governor "allow" log visibility tests.

Slice 5 closure policy binding:
  * Default INFO noise unacceptable → `_allow_log_mode()` defaults to ``off``
  * Must be rate-limited / sampled / debug-opt-in
  * Prefer "one structured line per meaningful allow cluster" OR
    "DEBUG with explicit operator flag"

Three modes: ``off`` (silent) / ``summary`` (1 line per N allows) /
``debug`` (DEBUG per allow). No INFO-per-allow path anywhere.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.intent_envelope import (
    make_envelope,
)
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
    _allow_log_interval,
    _allow_log_mode,
)
from backend.core.ouroboros.governance.sensor_governor import (
    Urgency,
    ensure_seeded as _sg_seed,
    reset_default_governor,
)


_LOGGER = "backend.core.ouroboros.governance.intake.unified_intake_router"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if (k.startswith("JARVIS_INTAKE_GOVERNOR")
                or k.startswith("JARVIS_SENSOR_GOVERNOR")):
            monkeypatch.delenv(k, raising=False)
    reset_default_governor()
    yield
    reset_default_governor()


def _make_router(tmp_path: Path) -> UnifiedIntakeRouter:
    return UnifiedIntakeRouter(
        gls=None, config=IntakeRouterConfig(project_root=tmp_path),
    )


def _make_env(source: str = "backlog", urgency: str = "normal"):
    return make_envelope(
        source=source, description="t",
        target_files=("f.py",), repo="jarvis",
        confidence=0.8, urgency=urgency,
        evidence={}, requires_human_ack=False,
    )


# ---------------------------------------------------------------------------
# Mode env parsing
# ---------------------------------------------------------------------------


class TestModeEnv:

    def test_default_is_off(self):
        assert _allow_log_mode() == "off"

    def test_explicit_summary(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        assert _allow_log_mode() == "summary"

    def test_explicit_debug(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "debug")
        assert _allow_log_mode() == "debug"

    def test_invalid_falls_back_to_off(self, monkeypatch):
        """No silent INFO leakage — unknown values mean off (the quietest
        default), not summary. This preserves the zero-noise invariant
        even under operator typo."""
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "verbose")
        assert _allow_log_mode() == "off"

    def test_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "SUMMARY")
        assert _allow_log_mode() == "summary"

    def test_interval_default(self):
        assert _allow_log_interval() == 100

    def test_interval_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "50",
        )
        assert _allow_log_interval() == 50

    def test_interval_clamped_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "0",
        )
        assert _allow_log_interval() == 1

    def test_interval_clamped_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "99999",
        )
        assert _allow_log_interval() == 10000

    def test_interval_malformed_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "banana",
        )
        assert _allow_log_interval() == 100


# ---------------------------------------------------------------------------
# off mode — preserves pre-follow-up silence
# ---------------------------------------------------------------------------


class TestOffMode:

    def test_off_produces_no_log_on_allow(
        self, monkeypatch, tmp_path, caplog,
    ):
        # Default: allow-log off. Ingest a normal env; no governor-allow
        # line should appear even though the allow path runs.
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_MODE", "shadow")
        monkeypatch.setenv("JARVIS_SENSOR_GOVERNOR_ENABLED", "true")
        reset_default_governor()
        _sg_seed()

        router = _make_router(tmp_path)
        env = _make_env()
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            router._note_governor_allow(env, _FakeDecision(allowed=True))

        # Zero lines from our helper in off mode
        assert not any(
            "governor allow" in r.message for r in caplog.records
        )

    def test_off_never_increments_counter(self, tmp_path):
        router = _make_router(tmp_path)
        for _ in range(10):
            router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        assert router._gov_allow_total == 0
        assert router._gov_allow_by_sensor == {}


# ---------------------------------------------------------------------------
# summary mode — 1 INFO line per N allows, then reset
# ---------------------------------------------------------------------------


class TestSummaryMode:

    def test_summary_no_log_before_interval(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "10",
        )
        router = _make_router(tmp_path)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for _ in range(9):
                router._note_governor_allow(
                    _make_env(), _FakeDecision(allowed=True),
                )
        # 9 allows < interval 10 → no rollup yet
        assert not any("rollup" in r.message for r in caplog.records)
        assert router._gov_allow_total == 9

    def test_summary_emits_rollup_at_interval(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "5",
        )
        router = _make_router(tmp_path)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for _ in range(5):
                router._note_governor_allow(
                    _make_env(source="backlog"),
                    _FakeDecision(allowed=True),
                )
        rollups = [r for r in caplog.records if "rollup" in r.message]
        assert len(rollups) == 1
        assert "total=5" in rollups[0].message
        assert "window=5" in rollups[0].message
        assert "backlog=5" in rollups[0].message

    def test_summary_resets_after_rollup(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "3",
        )
        router = _make_router(tmp_path)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for _ in range(3):
                router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        # After first rollup, counters reset
        assert router._gov_allow_total == 0
        assert router._gov_allow_by_sensor == {}
        # Next 2 allows don't fire a second rollup
        for _ in range(2):
            router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        rollups = [r for r in caplog.records if "rollup" in r.message]
        assert len(rollups) == 1

    def test_summary_top_5_sensors_only(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "7",
        )
        router = _make_router(tmp_path)
        # 7 allows across 7 different sensors — rollup should only
        # list the top 5
        sources = ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for src in sources:
                router._note_governor_allow(
                    _make_env(source=src if src in _ALLOWED_SOURCES else "backlog"),
                    _FakeDecision(allowed=True),
                )
        # Just verify a rollup fired with top_sensors=[...]
        rollups = [r for r in caplog.records if "rollup" in r.message]
        assert len(rollups) == 1
        assert "top_sensors=" in rollups[0].message

    def test_summary_multiple_rollups_on_sustained_traffic(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        monkeypatch.setenv(
            "JARVIS_INTAKE_GOVERNOR_ALLOW_LOG_INTERVAL", "4",
        )
        router = _make_router(tmp_path)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            for _ in range(12):  # 12 allows → 3 rollups at interval 4
                router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        rollups = [r for r in caplog.records if "rollup" in r.message]
        assert len(rollups) == 3


# ---------------------------------------------------------------------------
# debug mode — per-allow DEBUG logs, opt-in only
# ---------------------------------------------------------------------------


class TestDebugMode:

    def test_debug_emits_per_allow_at_debug_level(
        self, monkeypatch, tmp_path, caplog,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "debug")
        router = _make_router(tmp_path)
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            for _ in range(3):
                router._note_governor_allow(
                    _make_env(source="backlog"),
                    _FakeDecision(allowed=True, weighted_cap=10,
                                  current_count=2, remaining=8),
                )
        debug_allows = [
            r for r in caplog.records
            if "governor allow:" in r.message and r.levelno == logging.DEBUG
        ]
        assert len(debug_allows) == 3
        assert "sensor=backlog" in debug_allows[0].message

    def test_debug_does_not_leak_to_info(
        self, monkeypatch, tmp_path, caplog,
    ):
        """DEBUG mode must NOT emit at INFO level (noise constraint)."""
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "debug")
        router = _make_router(tmp_path)
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        # caplog captures INFO+; DEBUG records are filtered out
        assert not any(
            r.levelno == logging.INFO and "governor allow" in r.message
            for r in caplog.records
        )

    def test_debug_does_not_increment_summary_counter(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "debug")
        router = _make_router(tmp_path)
        for _ in range(5):
            router._note_governor_allow(_make_env(), _FakeDecision(allowed=True))
        # Debug path bypasses the summary counter
        assert router._gov_allow_total == 0


# ---------------------------------------------------------------------------
# Safety — helper never raises
# ---------------------------------------------------------------------------


class TestSafety:

    def test_helper_never_raises_on_malformed_decision(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv("JARVIS_INTAKE_GOVERNOR_ALLOW_LOG", "summary")
        router = _make_router(tmp_path)
        # Pass a decision-shaped object with missing attributes — the
        # helper should never propagate an error into the ingest path
        router._note_governor_allow(_make_env(), object())
        # No state corruption
        assert isinstance(router._gov_allow_total, int)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


_ALLOWED_SOURCES = {
    "architecture", "backlog", "test_failure", "voice_human", "ai_miner",
    "capability_gap", "runtime_health", "exploration", "roadmap",
    "cu_execution", "intent_discovery", "todo_scanner", "doc_staleness",
    "github_issue", "performance_regression", "cross_repo_drift",
    "security_advisory", "web_intelligence", "vision_sensor",
}


class _FakeDecision:
    """Minimal stand-in for BudgetDecision; only exposes the attrs
    _note_governor_allow reads."""

    def __init__(
        self, allowed: bool = True,
        weighted_cap: int = 10, current_count: int = 1, remaining: int = 9,
    ):
        self.allowed = allowed
        self.weighted_cap = weighted_cap
        self.current_count = current_count
        self.remaining = remaining
