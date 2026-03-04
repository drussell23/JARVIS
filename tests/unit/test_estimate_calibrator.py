"""Tests for backend.core.estimate_calibrator -- EstimateCalibrator.

Covers recording, p95 factor computation, persistence, corruption
handling, history trimming, and the ``get_stats()`` diagnostics view.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.estimate_calibrator import EstimateCalibrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def calibrator(tmp_path: Path) -> EstimateCalibrator:
    """EstimateCalibrator backed by a temp-directory file."""
    return EstimateCalibrator(history_file=tmp_path / "estimates.json")


# ---------------------------------------------------------------------------
# Default factor (no history)
# ---------------------------------------------------------------------------


class TestDefaultFactor:
    def test_default_factor_with_no_history(self, calibrator: EstimateCalibrator) -> None:
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1_000_000_000)
        assert result == 1_200_000_000  # 1.2x default

    def test_default_factor_zero_raw(self, calibrator: EstimateCalibrator) -> None:
        result = calibrator.get_calibrated_estimate("llm:test@v1", 0)
        assert result == 0


# ---------------------------------------------------------------------------
# Recording and calibrated estimates
# ---------------------------------------------------------------------------


class TestRecordAndCalibrate:
    def test_records_and_uses_history(self, calibrator: EstimateCalibrator) -> None:
        for _ in range(5):
            calibrator.record("llm:test@v1", estimated=1000, actual=1100)
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1000)
        # All ratios are 1.1, so p95 = 1.1 → calibrated = 1100
        assert result >= 1100

    def test_never_shrinks_below_raw(self, calibrator: EstimateCalibrator) -> None:
        for _ in range(5):
            calibrator.record("llm:test@v1", estimated=1000, actual=800)
        result = calibrator.get_calibrated_estimate("llm:test@v1", 1000)
        assert result >= 1000

    def test_p95_picks_high_overrun(self, calibrator: EstimateCalibrator) -> None:
        # 18 entries with ratio=1.0, 2 entries with ratio=2.0
        # Sorted: [1.0]*18 + [2.0]*2  (20 total)
        # p95 index = ceil(0.95*20)-1 = 19-1 = 18 → value at idx 18 = 2.0
        for _ in range(18):
            calibrator.record("llm:big@v1", estimated=1000, actual=1000)
        for _ in range(2):
            calibrator.record("llm:big@v1", estimated=1000, actual=2000)

        result = calibrator.get_calibrated_estimate("llm:big@v1", 1000)
        assert result == 2000

    def test_two_samples_uses_default(self, calibrator: EstimateCalibrator) -> None:
        calibrator.record("llm:tiny@v1", estimated=100, actual=150)
        calibrator.record("llm:tiny@v1", estimated=100, actual=160)
        result = calibrator.get_calibrated_estimate("llm:tiny@v1", 100)
        # Only 2 samples → default 1.2x
        assert result == 120

    def test_three_samples_uses_p95(self, calibrator: EstimateCalibrator) -> None:
        calibrator.record("llm:med@v1", estimated=100, actual=110)
        calibrator.record("llm:med@v1", estimated=100, actual=120)
        calibrator.record("llm:med@v1", estimated=100, actual=130)
        result = calibrator.get_calibrated_estimate("llm:med@v1", 100)
        # Ratios: [1.1, 1.2, 1.3], p95 idx = ceil(0.95*3)-1 = 2 → ratio 1.3
        assert result == 130

    def test_handles_zero_estimated(self, calibrator: EstimateCalibrator) -> None:
        # estimated=0 should not divide by zero
        calibrator.record("edge:zero@v1", estimated=0, actual=500)
        # ratio = 500 / max(0,1) = 500 — extreme but safe
        assert len(calibrator._history["edge:zero@v1"]) == 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_persists_to_file(self, calibrator: EstimateCalibrator, tmp_path: Path) -> None:
        calibrator.record("test:v1", estimated=100, actual=120)
        data = json.loads((tmp_path / "estimates.json").read_text())
        assert "test:v1" in data
        assert len(data["test:v1"]) == 1
        assert data["test:v1"][0]["ratio"] == pytest.approx(1.2)

    def test_reload_preserves_data(self, tmp_path: Path) -> None:
        cal1 = EstimateCalibrator(history_file=tmp_path / "estimates.json")
        cal1.record("persist:v1", estimated=200, actual=260)

        # Create a new instance from the same file
        cal2 = EstimateCalibrator(history_file=tmp_path / "estimates.json")
        assert len(cal2._history.get("persist:v1", [])) == 1
        assert cal2._history["persist:v1"][0]["ratio"] == pytest.approx(1.3)

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        deep_path = tmp_path / "a" / "b" / "c" / "estimates.json"
        cal = EstimateCalibrator(history_file=deep_path)
        cal.record("deep:v1", estimated=100, actual=100)
        assert deep_path.exists()


# ---------------------------------------------------------------------------
# History trimming
# ---------------------------------------------------------------------------


class TestHistoryTrimming:
    def test_max_history_per_component(self, calibrator: EstimateCalibrator) -> None:
        for i in range(60):
            calibrator.record("test:v1", estimated=100, actual=100 + i)
        assert len(calibrator._history["test:v1"]) == 50

    def test_keeps_most_recent_entries(self, calibrator: EstimateCalibrator) -> None:
        for i in range(60):
            calibrator.record("test:v1", estimated=100, actual=100 + i)
        # The first 10 entries (actual=100..109) should be trimmed;
        # the last entry should be actual=159.
        last = calibrator._history["test:v1"][-1]
        assert last["actual"] == 159


# ---------------------------------------------------------------------------
# Stats / diagnostics
# ---------------------------------------------------------------------------


class TestGetStats:
    def test_get_stats(self, calibrator: EstimateCalibrator) -> None:
        for _ in range(5):
            calibrator.record("a:v1", estimated=100, actual=110)
        stats = calibrator.get_stats()
        assert "a:v1" in stats
        assert stats["a:v1"]["samples"] == 5
        assert stats["a:v1"]["p95_factor"] == pytest.approx(1.1)
        assert stats["a:v1"]["mean_ratio"] == pytest.approx(1.1)

    def test_empty_stats(self, calibrator: EstimateCalibrator) -> None:
        stats = calibrator.get_stats()
        assert stats == {}

    def test_multiple_components(self, calibrator: EstimateCalibrator) -> None:
        for _ in range(5):
            calibrator.record("a:v1", estimated=100, actual=110)
            calibrator.record("b:v1", estimated=200, actual=300)
        stats = calibrator.get_stats()
        assert len(stats) == 2
        assert stats["a:v1"]["samples"] == 5
        assert stats["b:v1"]["samples"] == 5


# ---------------------------------------------------------------------------
# Error handling (corrupt / missing files)
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_corrupt_file_handled(self, tmp_path: Path) -> None:
        f = tmp_path / "estimates.json"
        f.write_text("NOT JSON")
        cal = EstimateCalibrator(history_file=f)
        assert cal._history == {}

    def test_missing_file_handled(self, tmp_path: Path) -> None:
        cal = EstimateCalibrator(history_file=tmp_path / "nonexistent.json")
        assert cal._history == {}

    def test_wrong_root_type_handled(self, tmp_path: Path) -> None:
        f = tmp_path / "estimates.json"
        f.write_text(json.dumps([1, 2, 3]))  # list instead of dict
        cal = EstimateCalibrator(history_file=f)
        assert cal._history == {}

    def test_malformed_entries_filtered(self, tmp_path: Path) -> None:
        f = tmp_path / "estimates.json"
        data = {
            "good:v1": [{"estimated": 100, "actual": 120, "ratio": 1.2}],
            "bad:v1": [{"missing_keys": True}],
            "mixed:v1": [
                {"estimated": 100, "actual": 120, "ratio": 1.2},
                {"garbage": "data"},
            ],
        }
        f.write_text(json.dumps(data))
        cal = EstimateCalibrator(history_file=f)
        assert len(cal._history["good:v1"]) == 1
        assert len(cal._history.get("bad:v1", [])) == 0
        assert len(cal._history["mixed:v1"]) == 1


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


class TestSingletonAccess:
    def test_init_and_get(self, tmp_path: Path) -> None:
        from backend.core.estimate_calibrator import (
            get_estimate_calibrator,
            init_estimate_calibrator,
            _calibrator_lock,
        )
        import backend.core.estimate_calibrator as mod

        # Save original to restore after test
        original = mod._calibrator_instance
        try:
            mod._calibrator_instance = None
            cal = init_estimate_calibrator(
                history_file=tmp_path / "singleton.json"
            )
            assert cal is get_estimate_calibrator()
        finally:
            mod._calibrator_instance = original

    def test_lazy_creation(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import backend.core.estimate_calibrator as mod

        original = mod._calibrator_instance
        try:
            mod._calibrator_instance = None
            cal = mod.get_estimate_calibrator()
            assert cal is not None
            assert isinstance(cal, EstimateCalibrator)
        finally:
            mod._calibrator_instance = original
