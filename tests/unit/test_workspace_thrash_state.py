"""Tests for workspace query thrash state using typed snapshot API."""
import pytest
from unittest.mock import MagicMock, patch


class TestCurrentThrashState:
    def test_returns_string(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        result = _current_thrash_state()
        assert isinstance(result, str)
        assert result == result.lower()

    def test_returns_unknown_when_no_quantizer(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            return_value=None,
        ):
            result = _current_thrash_state()
            assert result == "unknown"

    def test_returns_typed_thrash_state_via_snapshot_sync(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        mock_snap = MagicMock()
        mock_snap.thrash_state = MagicMock(value="emergency")
        mock_mq = MagicMock()
        mock_mq.snapshot_sync.return_value = mock_snap
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            result = _current_thrash_state()
            assert result == "emergency"

    def test_returns_typed_thrash_state_via_property(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        mock_mq = MagicMock()
        mock_mq.thrash_state = MagicMock(value="thrashing")
        # No snapshot_sync available
        del mock_mq.snapshot_sync
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            result = _current_thrash_state()
            assert result == "thrashing"

    def test_returns_unknown_on_import_error(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            side_effect=ImportError("no module"),
        ):
            result = _current_thrash_state()
            assert result == "unknown"

    def test_returns_unknown_on_attribute_error(self):
        from backend.vision.yabai_space_detector import _current_thrash_state
        mock_mq = MagicMock()
        # snapshot_sync returns None, thrash_state also absent
        mock_mq.snapshot_sync.return_value = None
        del mock_mq.thrash_state
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            result = _current_thrash_state()
            assert result == "unknown"

    def test_handles_plain_string_thrash_state(self):
        """If thrash_state is a plain string (no .value), still works."""
        from backend.vision.yabai_space_detector import _current_thrash_state
        mock_mq = MagicMock()
        mock_mq.thrash_state = "healthy"
        del mock_mq.snapshot_sync
        with patch(
            "backend.core.memory_quantizer.get_memory_quantizer_instance",
            return_value=mock_mq,
        ):
            result = _current_thrash_state()
            assert result == "healthy"


class TestResolveWorkspaceQueryTimeout:
    def test_standard_timeout(self):
        from backend.vision.yabai_space_detector import _resolve_workspace_query_timeout
        result = _resolve_workspace_query_timeout(2.0)
        assert "effective_timeout_seconds" in result
        assert result["base_timeout_seconds"] == 2.0

    def test_thrashing_increases_timeout(self):
        from backend.vision.yabai_space_detector import _resolve_workspace_query_timeout
        with patch(
            "backend.vision.yabai_space_detector._current_thrash_state",
            return_value="thrashing",
        ):
            result = _resolve_workspace_query_timeout(2.0)
            assert result["effective_timeout_seconds"] > 2.0
            assert result["thrash_state"] == "thrashing"

    def test_emergency_increases_timeout_more(self):
        from backend.vision.yabai_space_detector import _resolve_workspace_query_timeout
        with patch(
            "backend.vision.yabai_space_detector._current_thrash_state",
            return_value="emergency",
        ):
            result = _resolve_workspace_query_timeout(2.0)
            assert result["effective_timeout_seconds"] > 2.0
            assert result["thrash_state"] == "emergency"

    def test_unknown_thrash_state_no_multiplier(self):
        from backend.vision.yabai_space_detector import _resolve_workspace_query_timeout
        with patch(
            "backend.vision.yabai_space_detector._current_thrash_state",
            return_value="unknown",
        ):
            with patch(
                "backend.vision.yabai_space_detector._is_startup_phase_for_workspace_query",
                return_value=False,
            ):
                result = _resolve_workspace_query_timeout(2.0)
                assert result["effective_timeout_seconds"] == 2.0
                assert result["timeout_reason"] == "standard"
