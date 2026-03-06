"""Tests for UMF Shadow Mode Parity Logger (Task 15)."""
import pytest


class TestShadowParityLogger:

    def test_record_match(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        logger.record("trace-001", "route", legacy_result="delivered", umf_result="delivered")
        assert logger.total_comparisons == 1
        assert logger.mismatches == 0

    def test_record_mismatch(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        logger.record("trace-002", "route", legacy_result="delivered", umf_result="rejected")
        assert logger.total_comparisons == 1
        assert logger.mismatches == 1

    def test_parity_ratio_perfect(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        for i in range(100):
            logger.record(f"trace-{i}", "dedup", legacy_result="ok", umf_result="ok")
        assert logger.parity_ratio == 1.0

    def test_parity_ratio_with_mismatches(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        for i in range(999):
            logger.record(f"trace-{i}", "route", legacy_result="ok", umf_result="ok")
        logger.record("trace-999", "route", legacy_result="ok", umf_result="rejected")
        assert logger.parity_ratio == pytest.approx(0.999, abs=0.001)

    def test_is_promotion_ready_above_threshold(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger(parity_threshold=0.999)
        for i in range(1000):
            logger.record(f"t-{i}", "route", legacy_result="ok", umf_result="ok")
        assert logger.is_promotion_ready() is True

    def test_is_promotion_ready_below_threshold(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger(parity_threshold=0.999)
        for i in range(990):
            logger.record(f"t-{i}", "route", legacy_result="ok", umf_result="ok")
        for i in range(10):
            logger.record(f"m-{i}", "route", legacy_result="ok", umf_result="rejected")
        assert logger.is_promotion_ready() is False

    def test_mismatch_details_logged(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        logger.record("trace-x", "heartbeat", legacy_result="alive", umf_result="stale")
        diffs = logger.get_recent_diffs(limit=10)
        assert len(diffs) == 1
        assert diffs[0]["trace_id"] == "trace-x"
        assert diffs[0]["category"] == "heartbeat"
        assert diffs[0]["legacy_result"] == "alive"
        assert diffs[0]["umf_result"] == "stale"

    def test_parity_ratio_zero_comparisons(self):
        from backend.core.umf.shadow_parity import ShadowParityLogger
        logger = ShadowParityLogger()
        assert logger.parity_ratio == 1.0  # No comparisons = perfect parity
        assert logger.is_promotion_ready() is False  # But not ready (no data)
