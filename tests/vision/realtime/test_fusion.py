"""Tests for burst result confidence fusion."""
import pytest
from backend.vision.realtime.fusion import fuse_burst_results, FusedTarget, VisionResult


@pytest.fixture
def agreeing_results():
    """3 results that agree on coordinates."""
    return [
        VisionResult(status="found", coords=(500, 200), confidence=0.90),
        VisionResult(status="found", coords=(502, 198), confidence=0.88),
        VisionResult(status="found", coords=(498, 201), confidence=0.85),
    ]

@pytest.fixture
def outlier_results():
    """2 agree, 1 outlier."""
    return [
        VisionResult(status="found", coords=(500, 200), confidence=0.90),
        VisionResult(status="found", coords=(502, 198), confidence=0.88),
        VisionResult(status="found", coords=(100, 100), confidence=0.70),
    ]


class TestFusion:
    def test_single_result(self):
        results = [VisionResult(status="found", coords=(500, 200), confidence=0.90)]
        fused = fuse_burst_results(results)
        assert fused.coords == (500, 200)
        assert fused.confidence == pytest.approx(0.90, abs=0.05)

    def test_three_agreeing(self, agreeing_results):
        fused = fuse_burst_results(agreeing_results)
        assert fused.coords is not None
        assert 495 <= fused.coords[0] <= 505
        assert 195 <= fused.coords[1] <= 205
        assert fused.confidence > 0.8
        assert fused.bbox_jitter < 10

    def test_outlier_rejection(self, outlier_results):
        fused = fuse_burst_results(outlier_results)
        # Outlier at (100,100) should be rejected
        assert fused.coords is not None
        assert 495 <= fused.coords[0] <= 505
        assert fused.frames_rejected >= 1

    def test_all_outliers(self):
        """All disagree by >50px — uses highest confidence, penalized."""
        results = [
            VisionResult(status="found", coords=(100, 100), confidence=0.90),
            VisionResult(status="found", coords=(500, 500), confidence=0.80),
            VisionResult(status="found", coords=(300, 700), confidence=0.70),
        ]
        fused = fuse_burst_results(results)
        assert fused.coords == (100, 100)  # highest confidence
        assert fused.confidence < 0.90  # penalized

    def test_high_jitter_penalizes(self):
        """Spread > 20px → 20% penalty."""
        results = [
            VisionResult(status="found", coords=(500, 200), confidence=0.90),
            VisionResult(status="found", coords=(530, 200), confidence=0.90),
            VisionResult(status="found", coords=(510, 200), confidence=0.90),
        ]
        fused = fuse_burst_results(results)
        assert fused.confidence < 0.90  # penalized for jitter
        assert fused.bbox_jitter >= 20

    def test_extreme_jitter_double_penalty(self):
        """Spread > 50px → double penalty."""
        results = [
            VisionResult(status="found", coords=(500, 200), confidence=0.90),
            VisionResult(status="found", coords=(560, 200), confidence=0.90),
        ]
        fused = fuse_burst_results(results)
        assert fused.confidence < 0.72  # 0.9 * 0.8 * ~something

    def test_no_hits(self):
        results = [
            VisionResult(status="not_found", coords=None, confidence=0.1),
            VisionResult(status="not_found", coords=None, confidence=0.2),
        ]
        fused = fuse_burst_results(results)
        assert fused.confidence == 0.0
        assert fused.coords is None

    def test_empty_input(self):
        fused = fuse_burst_results([])
        assert fused.confidence == 0.0
        assert fused.coords is None

    def test_deterministic(self, agreeing_results):
        f1 = fuse_burst_results(agreeing_results)
        f2 = fuse_burst_results(agreeing_results)
        assert f1.coords == f2.coords
        assert f1.confidence == f2.confidence

    def test_mixed_found_not_found(self):
        results = [
            VisionResult(status="found", coords=(500, 200), confidence=0.90),
            VisionResult(status="not_found", coords=None, confidence=0.1),
            VisionResult(status="found", coords=(502, 198), confidence=0.85),
        ]
        fused = fuse_burst_results(results)
        assert fused.coords is not None  # should use the 2 found results
        assert fused.confidence > 0.5
