"""Curiosity-scan observability closure — silent zero-emission is dead.

The bt-2026-07-18-231346 graduation soak produced ZERO curiosity log
lines and the outcome was undiagnosable: ``run_curiosity_scan_once``
logged only when ``emitted > 0``, and the sensor's reader path logged
nothing at all on empty rankings. These tests pin the fix:

  1. every armed scan logs ONE verdict line, emission or not;
  2. ``zero_cause`` is a CLASSIFIED value from the closed taxonomy,
     ordered by starvation point (data → budget → construction →
     transport → admission);
  3. the sensor's reader path logs its rankings/decision histogram;
  4. the report schema stays additive (legacy fields untouched).
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from backend.core.ouroboros.governance import domain_entropy_engine as dee


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "true")
    yield


CLUSTERS = [
    {"cluster_id": "a", "kind": "goal", "size": 3},
    {"cluster_id": "b", "kind": "goal", "size": 30},
]


class _Router:
    def __init__(self, verdict="enqueued"):
        self.verdict = verdict
        self.ingested = []

    async def ingest(self, env):
        self.ingested.append(env)
        return self.verdict


class _Load:
    """Duck-typed cognitive-load report."""
    def __init__(self, verdict_value):
        class _V:  # noqa: D401
            value = verdict_value
        self.verdict = _V()


def _scan(**kw):
    return asyncio.run(dee.run_curiosity_scan_once(**kw))


# ---------------------------------------------------------------------------
# (1) Zero-cause taxonomy — each starvation point classified
# ---------------------------------------------------------------------------


def test_disabled_scan_carries_master_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "false")
    rep = _scan(router=_Router())
    assert rep.zero_cause == dee.ZERO_CAUSE_DISABLED
    assert rep.master_enabled is False


def test_no_zones_cause(caplog):
    with caplog.at_level(logging.INFO):
        rep = _scan(router=_Router(), clusters=[])
    assert rep.zero_cause == dee.ZERO_CAUSE_NO_ZONES
    assert "zero_cause=no_zones" in caplog.text


def test_budget_zero_cause_when_overloaded(caplog):
    with caplog.at_level(logging.INFO):
        rep = _scan(
            router=_Router(), clusters=CLUSTERS,
            load_report=_Load("overloaded"),
        )
    assert rep.zero_cause == dee.ZERO_CAUSE_BUDGET
    assert rep.zones_identified == 2
    assert "zero_cause=budget_zero" in caplog.text


def test_router_missing_cause():
    rep = _scan(router=None, clusters=CLUSTERS, load_report=_Load("normal"))
    assert rep.zero_cause == dee.ZERO_CAUSE_ROUTER_MISSING


def test_ingest_rejected_cause_with_histogram(caplog):
    with caplog.at_level(logging.INFO):
        rep = _scan(
            router=_Router(verdict="rejected_governor"),
            clusters=CLUSTERS, load_report=_Load("normal"),
        )
    assert rep.zero_cause == dee.ZERO_CAUSE_INGEST_REJECTED
    assert "rejected_governor" in caplog.text     # histogram surfaces WHY
    assert rep.emitted == 0
    assert len(rep.ingest_results) >= 1


def test_taxonomy_is_closed_and_ordered():
    assert dee.SCAN_ZERO_CAUSES == (
        dee.ZERO_CAUSE_DISABLED, dee.ZERO_CAUSE_NO_ZONES,
        dee.ZERO_CAUSE_BUDGET, dee.ZERO_CAUSE_NO_ENVELOPES,
        dee.ZERO_CAUSE_ROUTER_MISSING, dee.ZERO_CAUSE_INGEST_REJECTED,
    )


# ---------------------------------------------------------------------------
# (2) The verdict line is UNCONDITIONAL when armed
# ---------------------------------------------------------------------------


def test_zero_emission_scan_still_logs_verdict(caplog):
    with caplog.at_level(logging.INFO):
        _scan(router=None, clusters=[], load_report=_Load("normal"))
    assert "[DomainEntropy] proactive scan" in caplog.text


def test_successful_emission_logs_and_has_empty_zero_cause(caplog):
    with caplog.at_level(logging.INFO):
        rep = _scan(
            router=_Router(), clusters=CLUSTERS, load_report=_Load("normal"),
        )
    assert rep.emitted >= 1
    assert rep.zero_cause == ""
    assert "emitted=" in caplog.text
    assert "zero_cause" not in rep.diagnostic


# ---------------------------------------------------------------------------
# (3) Schema stays additive
# ---------------------------------------------------------------------------


def test_report_legacy_fields_unchanged():
    rep = _scan(router=_Router(), clusters=CLUSTERS, load_report=_Load("normal"))
    for field in (
        "master_enabled", "normalized_entropy", "zones_identified",
        "budget", "emitted", "ingest_results", "diagnostic",
        "schema_version",
    ):
        assert hasattr(rep, field)
    assert rep.schema_version == dee.DOMAIN_ENTROPY_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# (4) Sensor reader-path verdict line (source-anchored pin + unit)
# ---------------------------------------------------------------------------


def test_sensor_reader_scan_line_is_unconditional():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (
        root / "backend/core/ouroboros/governance/intake/sensors/"
        "proactive_exploration_sensor.py"
    ).read_text()
    # The verdict line must sit BEFORE the per-ranking emission loop —
    # i.e., it fires even when rankings is empty.
    line = src.index("curiosity-reader scan")
    loop = src.index("for ranking in rankings:", line)
    assert loop > line
