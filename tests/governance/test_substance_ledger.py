"""P0.5 — substance telemetry: the value-ratio verdict metric.

Run #25: "everything O+V initiates is annotation-grade." P0.1-P0.4 change WHAT
O+V works on; this classifies each dispatched signal (from the evidence those
layers stamp) into a substance breakdown + ratio, so the verdict becomes a
measurable KPI.

Proof: every substance marker classifies correctly with the right precedence;
the ratios are computed right; the gate + accessor work; classification is
fail-soft; and both seams (dispatch record + summary emit) are wired.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance import substance_ledger as S


@pytest.fixture(autouse=True)
def _reset():
    S.reset_default_substance_ledger_for_tests()
    yield
    S.reset_default_substance_ledger_for_tests()


# ── classify_signal: markers → buckets ───────────────────────────────

def test_work_order_is_substantive():
    assert S.classify_signal({"work_order": True}) == S.BUCKET_SUBSTANTIVE


def test_latent_defect_is_substantive():
    assert S.classify_signal(
        {"deep_analysis_category": "latent_defect"}
    ) == S.BUCKET_SUBSTANTIVE


def test_reputation_boost_is_substantive():
    assert S.classify_signal({"reputation_boost": 1}) == S.BUCKET_SUBSTANTIVE
    assert S.classify_signal({"reputation_boost": 0}) != S.BUCKET_SUBSTANTIVE


def test_value_bands_map_correctly():
    assert S.classify_signal({"value_band": 3}) == S.BUCKET_SUBSTANTIVE   # ORACLE
    assert S.classify_signal({"value_band": 2}) == S.BUCKET_EXECUTABLE
    assert S.classify_signal({"value_band": 1}) == S.BUCKET_ANNOTATION
    assert S.classify_signal({"value_band": 0}) == S.BUCKET_INDETERMINATE
    assert S.classify_signal({}) == S.BUCKET_INDETERMINATE


def test_substantive_markers_win_over_band():
    """A cosmetic-band op that is ALSO a work order / defect / reputation-
    boosted counts as substantive (the marker is the stronger evidence)."""
    assert S.classify_signal(
        {"work_order": True, "value_band": 1}
    ) == S.BUCKET_SUBSTANTIVE
    assert S.classify_signal(
        {"reputation_boost": 2, "value_band": 1}
    ) == S.BUCKET_SUBSTANTIVE


def test_classify_is_failsoft():
    assert S.classify_signal(None) == S.BUCKET_INDETERMINATE
    assert S.classify_signal({"value_band": "garbage"}) == S.BUCKET_INDETERMINATE
    assert S.classify_signal({"reputation_boost": "x"}) == S.BUCKET_INDETERMINATE


# ── the ratios ───────────────────────────────────────────────────────

def test_snapshot_ratios():
    L = S.get_default_substance_ledger()
    L.record({"work_order": True})               # substantive
    L.record({"value_band": 3})                  # substantive (oracle)
    L.record({"value_band": 2})                  # executable
    L.record({"value_band": 1})                  # annotation
    snap = L.snapshot()
    assert snap["substantive"] == 2
    assert snap["executable"] == 1
    assert snap["annotation"] == 1
    assert snap["total"] == 4
    # (2 substantive + 1 executable) / 4
    assert snap["substance_ratio"] == 0.75
    # 2 substantive / 4
    assert snap["proven_substance_ratio"] == 0.5


def test_empty_snapshot_is_zero():
    snap = S.get_default_substance_ledger().snapshot()
    assert snap["total"] == 0
    assert snap["substance_ratio"] == 0.0
    assert snap["proven_substance_ratio"] == 0.0


def test_all_annotation_reads_near_zero():
    """The Run #25 signature: an all-annotation session reads ratio 0.0 —
    exactly the KPI that P0.1-P0.4 should lift."""
    L = S.get_default_substance_ledger()
    for _ in range(5):
        L.record({"value_band": 1})
    assert L.snapshot()["substance_ratio"] == 0.0


# ── gate + accessors ─────────────────────────────────────────────────

def test_telemetry_default_on(monkeypatch):
    monkeypatch.delenv("JARVIS_SUBSTANCE_TELEMETRY_ENABLED", raising=False)
    assert S.telemetry_enabled() is True


def test_telemetry_explicit_off(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBSTANCE_TELEMETRY_ENABLED", "false")
    assert S.telemetry_enabled() is False


def test_record_dispatch_respects_gate(monkeypatch):
    monkeypatch.setenv("JARVIS_SUBSTANCE_TELEMETRY_ENABLED", "false")
    S.record_dispatch({"work_order": True})
    assert S.substance_snapshot()["total"] == 0  # gated off → not counted
    monkeypatch.setenv("JARVIS_SUBSTANCE_TELEMETRY_ENABLED", "true")
    S.record_dispatch({"work_order": True})
    assert S.substance_snapshot()["total"] == 1


# ── both seams are wired ─────────────────────────────────────────────

def test_dispatch_seam_wired():
    from backend.core.ouroboros.governance.intake import unified_intake_router
    src = inspect.getsource(unified_intake_router.UnifiedIntakeRouter._dispatch_one)
    assert "record_dispatch" in src


def test_summary_emit_seam_wired():
    from backend.core.ouroboros.battle_test import session_recorder
    src = inspect.getsource(session_recorder)
    assert "substance_snapshot" in src
    assert 'summary["substance"]' in src
