"""Tests for vision intelligence boundary adapters."""
from backend.vision.intelligence.boundary_adapters import safe_state_key, safe_text


class TestSafeStateKey:
    def test_none_returns_sentinel(self):
        assert safe_state_key(None) == "__none__"

    def test_string_passthrough(self):
        assert safe_state_key("error_state") == "error_state"

    def test_int_converts(self):
        assert safe_state_key(42) == "42"

    def test_empty_string(self):
        assert safe_state_key("") == ""


class TestSafeText:
    def test_none_returns_empty(self):
        assert safe_text(None) == ""

    def test_string_passthrough(self):
        assert safe_text("hello") == "hello"

    def test_int_converts(self):
        assert safe_text(123) == "123"

    def test_empty_string(self):
        assert safe_text("") == ""
