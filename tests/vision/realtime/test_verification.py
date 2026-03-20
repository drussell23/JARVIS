"""Tests for post-action verification."""
import pytest
import numpy as np
from backend.vision.realtime.verification import (
    ActionVerifier, VerificationResult, VerificationStatus,
)


class TestVerifier:
    @pytest.fixture
    def verifier(self):
        return ActionVerifier()

    def test_click_success_detects_change(self, verifier):
        """Frame diff at click coords shows change → success."""
        before = np.zeros((100, 100, 3), dtype=np.uint8)
        after = np.zeros((100, 100, 3), dtype=np.uint8)
        after[45:55, 45:55] = 255  # region around click changed
        result = verifier.verify_click(before, after, coords=(50, 50), region_size=20)
        assert result.status == VerificationStatus.SUCCESS

    def test_click_fail_no_change(self, verifier):
        """Identical frames → click didn't work."""
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        result = verifier.verify_click(frame, frame.copy(), coords=(50, 50), region_size=20)
        assert result.status == VerificationStatus.FAIL

    def test_type_success_text_appeared(self, verifier):
        """Different frame content → type worked."""
        before = np.zeros((100, 100, 3), dtype=np.uint8)
        after = np.ones((100, 100, 3), dtype=np.uint8) * 128  # text pixels appeared
        result = verifier.verify_type(before, after, target_region=(40, 40, 60, 60))
        assert result.status == VerificationStatus.SUCCESS

    def test_scroll_success_content_shifted(self, verifier):
        """Content shifted in scroll direction → success."""
        before = np.zeros((100, 100, 3), dtype=np.uint8)
        before[0:50, :] = 128  # top half gray
        after = np.zeros((100, 100, 3), dtype=np.uint8)
        after[10:60, :] = 128  # shifted down 10px
        result = verifier.verify_scroll(before, after, direction="down")
        assert result.status == VerificationStatus.SUCCESS

    def test_verification_result_fields(self, verifier):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        result = verifier.verify_click(frame, frame.copy(), coords=(50, 50))
        assert hasattr(result, 'status')
        assert hasattr(result, 'confidence')
        assert hasattr(result, 'diff_magnitude')
