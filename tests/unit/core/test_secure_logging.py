"""Tests for centralized log sanitization (CWE-117 prevention)."""

from backend.core.secure_logging import sanitize_for_log, mask_sensitive


class TestSanitizeForLog:
    """Test CWE-117 log injection prevention."""

    def test_strips_newlines(self):
        assert "\n" not in sanitize_for_log("hello\nworld")

    def test_strips_carriage_return(self):
        assert "\r" not in sanitize_for_log("hello\rworld")

    def test_strips_null_bytes(self):
        assert "\x00" not in sanitize_for_log("hello\x00world")

    def test_strips_ansi_escape(self):
        assert "\x1b" not in sanitize_for_log("hello\x1b[31mred\x1b[0m")

    def test_truncates_to_max_len(self):
        result = sanitize_for_log("a" * 500, max_len=100)
        assert len(result) == 100

    def test_default_max_len(self):
        result = sanitize_for_log("a" * 500)
        assert len(result) == 200

    def test_preserves_safe_content(self):
        assert sanitize_for_log("hello world 123") == "hello world 123"

    def test_handles_non_string(self):
        assert sanitize_for_log(42) == "42"
        assert sanitize_for_log(None) == "None"

    def test_handles_empty_string(self):
        assert sanitize_for_log("") == ""


class TestMaskSensitive:
    """Test CWE-532 sensitive data masking."""

    def test_masks_long_value(self):
        result = mask_sensitive("sk-ant-api03-xxxxxxxxxxxx")
        assert result == "sk-a****"

    def test_masks_short_value(self):
        result = mask_sensitive("abc")
        assert result == "****"

    def test_custom_prefix_length(self):
        result = mask_sensitive("abcdefgh", visible_prefix=6)
        assert result == "abcdef****"

    def test_handles_non_string(self):
        result = mask_sensitive(12345)
        assert result == "1234****"
