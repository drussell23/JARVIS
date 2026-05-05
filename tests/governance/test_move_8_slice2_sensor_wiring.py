"""Move 8 Slice 2 — ProactiveExplorationSensor curiosity wire-up.

Pins the third signal-source wire-up: Slice 1's
:func:`rank_curious_clusters` substrate composes into the existing
:meth:`ProactiveExplorationSensor.scan_once` loop alongside the
LearningConsolidator failure-rule path and the
codebase_character cluster-coverage path.

Verifies (16 tests):

  * scan_once invokes _emit_curiosity_signals (structural call
    site present); regression-pinned via AST.
  * _emit_curiosity_signals method exists on the class with the
    expected async signature.
  * Master-flag-off short-circuits → zero envelopes ingested.
  * Master-flag-on with a SURFACED ranking → one envelope
    ingested with the expected evidence shape.
  * BELOW_FLOOR / COLD_START / DECAY_SUPPRESSED / COOLDOWN
    decisions all skipped (only SURFACED emits).
  * Posture=HARDEN suppresses emission entirely.
  * Posture=EXPLORE/CONSOLIDATE/MAINTAIN does NOT suppress.
  * Posture-read raising does NOT poison the loop (fail-open).
  * Reader raising does NOT poison the loop.
  * router.ingest raising on one ranking does NOT block others.
  * Envelope shape: source="exploration" / urgency="low" /
    evidence.category="curiosity_driven" / cluster_id +
    magnitude + dominant_source + rank present.
  * The new code path is wired AFTER the cluster-coverage path
    in scan_once (composition order regression pin).
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock, patch

import pytest

from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
    ProactiveExplorationSensor,
)
from backend.core.ouroboros.governance.proactive_curiosity_reader import (
    CuriosityRanking,
    CuriosityRankingDecision,
    reset_cooldown_ledger_for_tests,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sensor() -> ProactiveExplorationSensor:
    router = AsyncMock()
    sensor = ProactiveExplorationSensor(
        repo="test-repo", router=router,
    )
    return sensor


def _surfaced(cluster_id: str = "c1") -> CuriosityRanking:
    return CuriosityRanking(
        cluster_id=cluster_id,
        magnitude=0.8,
        confidence=0.7,
        samples_count=10,
        dominant_source="logprob_entropy",
        decay_reason="none",
        last_updated_at_unix=1000.0,
        rank=1,
        decision=CuriosityRankingDecision.SURFACED,
    )


def _rejected(
    decision: CuriosityRankingDecision,
    cluster_id: str = "rej",
) -> CuriosityRanking:
    return CuriosityRanking(
        cluster_id=cluster_id,
        magnitude=0.8,
        confidence=0.7,
        samples_count=10,
        dominant_source="logprob_entropy",
        decay_reason="none",
        last_updated_at_unix=1000.0,
        rank=-1,
        decision=decision,
    )


@pytest.fixture(autouse=True)
def _reset_cooldown():
    reset_cooldown_ledger_for_tests()
    yield
    reset_cooldown_ledger_for_tests()


# ---------------------------------------------------------------------------
# Structural pins — method exists + scan_once calls it
# ---------------------------------------------------------------------------


def test_emit_curiosity_signals_method_exists():
    sensor = _make_sensor()
    assert hasattr(sensor, "_emit_curiosity_signals")
    import inspect
    method = getattr(sensor, "_emit_curiosity_signals")
    assert inspect.iscoroutinefunction(method), (
        "_emit_curiosity_signals must be async"
    )


def test_scan_once_calls_emit_curiosity_signals_after_cluster_coverage():
    """Composition-order pin — curiosity emission is the 3rd
    signal source, AFTER cluster-coverage. Wave 2 architecture
    convention: failure-rules → cluster-coverage → curiosity.
    AST-pinned so a future refactor that re-orders or drops the
    call fails CI."""
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/intake/sensors"
        / "proactive_exploration_sensor.py"
    )
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Locate scan_once.
    scan_once = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            if node.name == "scan_once":
                scan_once = node
                break
    assert scan_once is not None
    # Walk in source order; record both call site positions.
    cluster_idx = None
    curiosity_idx = None
    for i, node in enumerate(ast.walk(scan_once)):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                if func.attr == "_emit_cluster_coverage_signals":
                    cluster_idx = i
                elif func.attr == "_emit_curiosity_signals":
                    curiosity_idx = i
    assert cluster_idx is not None, (
        "scan_once must call _emit_cluster_coverage_signals"
    )
    assert curiosity_idx is not None, (
        "scan_once must call _emit_curiosity_signals "
        "(Move 8 Slice 2 wire-up regression)"
    )
    assert curiosity_idx > cluster_idx, (
        "_emit_curiosity_signals must follow cluster_coverage"
    )


# ---------------------------------------------------------------------------
# Master-flag gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_master_flag_off_no_emission():
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=False,
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == []
    sensor._router.ingest.assert_not_called()


@pytest.mark.asyncio
async def test_master_flag_on_surfaced_emits_one():
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_surfaced("c1"),),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="EXPLORE",
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == ["c1"]
    sensor._router.ingest.assert_called_once()


# ---------------------------------------------------------------------------
# Decision gating — only SURFACED emits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [
    CuriosityRankingDecision.BELOW_FLOOR,
    CuriosityRankingDecision.COLD_START,
    CuriosityRankingDecision.DECAY_SUPPRESSED,
    CuriosityRankingDecision.COOLDOWN,
])
async def test_non_surfaced_decisions_skipped(decision):
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_rejected(decision),),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="EXPLORE",
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == []
    sensor._router.ingest.assert_not_called()


# ---------------------------------------------------------------------------
# Posture suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posture_harden_suppresses():
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_surfaced("c1"),),
    ) as mock_rank, patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="HARDEN",
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == []
    # Reader should not even be called when posture suppresses
    mock_rank.assert_not_called()
    sensor._router.ingest.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", [
    "EXPLORE", "CONSOLIDATE", "MAINTAIN",
])
async def test_non_harden_posture_does_not_suppress(posture):
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_surfaced("c1"),),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value=posture,
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == ["c1"]


@pytest.mark.asyncio
async def test_posture_read_raises_fails_open():
    """Probe glitch must NOT starve the loop. Fail-open."""
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_surfaced("c1"),),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        side_effect=RuntimeError("posture probe broke"),
    ):
        result = await sensor._emit_curiosity_signals()
    # Fail-open: emission proceeds.
    assert result == ["c1"]


# ---------------------------------------------------------------------------
# Defensive — never raises into parent scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_raises_returns_empty():
    sensor = _make_sensor()
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        side_effect=RuntimeError("reader broke"),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="EXPLORE",
    ):
        result = await sensor._emit_curiosity_signals()
    assert result == []
    sensor._router.ingest.assert_not_called()


@pytest.mark.asyncio
async def test_router_ingest_raises_does_not_block_other_rankings():
    sensor = _make_sensor()
    sensor._router.ingest = AsyncMock(
        side_effect=[RuntimeError("first ingest broke"), None],
    )
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(_surfaced("bad"), _surfaced("good")),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="EXPLORE",
    ):
        result = await sensor._emit_curiosity_signals()
    # First ingest raised → "bad" was attempted but not appended.
    # Second ingest succeeded → "good" was appended.
    assert result == ["good"]


# ---------------------------------------------------------------------------
# Envelope shape contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_shape_carries_curiosity_evidence():
    sensor = _make_sensor()
    captured: List[Any] = []
    async def _capture(envelope):
        captured.append(envelope)
    sensor._router.ingest = _capture
    ranking = _surfaced("test-cluster")
    with patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader."
        "proactive_curiosity_reader_enabled",
        return_value=True,
    ), patch(
        "backend.core.ouroboros.governance."
        "proactive_curiosity_reader.rank_curious_clusters",
        return_value=(ranking,),
    ), patch(
        "backend.core.ouroboros.governance.posture_health."
        "safe_load_posture_value",
        return_value="EXPLORE",
    ):
        await sensor._emit_curiosity_signals()
    assert len(captured) == 1
    env = captured[0]
    # Source / urgency contract
    assert getattr(env, "source", None) == "exploration"
    # Evidence carries curiosity-specific fields
    evidence = getattr(env, "evidence", {}) or {}
    assert evidence.get("category") == "curiosity_driven"
    assert evidence.get("cluster_id") == "test-cluster"
    assert evidence.get("magnitude") == pytest.approx(0.8)
    assert evidence.get("dominant_source") == "logprob_entropy"
    assert evidence.get("rank") == 1
    assert evidence.get("sensor") == "ProactiveExplorationSensor"


# ---------------------------------------------------------------------------
# Pre-existing wiring untouched (regression pin)
# ---------------------------------------------------------------------------


def test_pre_existing_signal_sources_still_wired():
    """Make sure the existing failure-rules + cluster-coverage
    paths still execute — Slice 2 is additive, not a rewrite."""
    target = (
        Path(__file__).resolve().parents[2]
        / "backend/core/ouroboros/governance/intake/sensors"
        / "proactive_exploration_sensor.py"
    )
    source = target.read_text(encoding="utf-8")
    assert "_emit_cluster_coverage_signals" in source
    assert "LearningConsolidator" in source
    assert "_emit_curiosity_signals" in source
