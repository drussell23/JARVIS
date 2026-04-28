"""Tests for Slice 1.2 — Determinism Substrate.

Pins:
  * Canonical serializer determinism (byte-identical across runs)
  * Hash stability (same input → same hash)
  * Temperature policy enforcement (decisional=0, creative=requested)
  * PromptHasher correctness
  * Unsupported type rejection (no silent str() fallback)
  * Architecture stability (no locale/encoding variance)
"""
from __future__ import annotations

import enum
import os
from typing import Any

import pytest

from backend.core.ouroboros.governance.observability.determinism_substrate import (
    CREATIVE_PHASES,
    CallCategory,
    DecisionHash,
    PromptHasher,
    canonical_hash,
    canonical_serialize,
    get_decisional_temperature,
    is_substrate_enabled,
    resolve_temperature,
)


# ---------------------------------------------------------------------------
# Canonical Serializer
# ---------------------------------------------------------------------------


class TestCanonicalSerialize:
    """Canonical JSON serializer determinism pins."""

    def test_sort_keys(self) -> None:
        """Keys are sorted alphabetically, not insertion-order."""
        a = canonical_serialize({"z": 1, "a": 2, "m": 3})
        b = canonical_serialize({"a": 2, "m": 3, "z": 1})
        assert a == b
        assert '"a":2' in a
        # z must come after m
        assert a.index('"m"') < a.index('"z"')

    def test_no_whitespace(self) -> None:
        """No whitespace jitter — compact separators."""
        s = canonical_serialize({"key": "value", "n": 42})
        assert " " not in s

    def test_ensure_ascii(self) -> None:
        """Non-ASCII characters are escaped — locale-independent."""
        s = canonical_serialize({"emoji": "🎉"})
        assert "\\u" in s  # escaped, not raw bytes

    def test_tuple_to_list(self) -> None:
        """Tuples are converted to lists (JSON has no tuple type)."""
        s = canonical_serialize({"t": (1, 2, 3)})
        assert "[1,2,3]" in s

    def test_frozenset_to_sorted_list(self) -> None:
        """Frozensets are sorted for determinism."""
        s = canonical_serialize({"fs": frozenset({"c", "a", "b"})})
        assert '["a","b","c"]' in s

    def test_set_to_sorted_list(self) -> None:
        """Sets are sorted for determinism."""
        s = canonical_serialize({"s": {"c", "a", "b"}})
        assert '["a","b","c"]' in s

    def test_bytes_to_hex(self) -> None:
        """Bytes are hex-encoded."""
        s = canonical_serialize({"b": b"\xde\xad"})
        assert '"dead"' in s

    def test_enum_to_value(self) -> None:
        """Enums serialize to their .value."""

        class Color(str, enum.Enum):
            RED = "red"

        s = canonical_serialize({"c": Color.RED})
        assert '"red"' in s

    def test_nan_rejected(self) -> None:
        """NaN is rejected (non-deterministic repr)."""
        with pytest.raises(ValueError):
            canonical_serialize({"x": float("nan")})

    def test_inf_rejected(self) -> None:
        """Inf is rejected (non-deterministic repr)."""
        with pytest.raises(ValueError):
            canonical_serialize({"x": float("inf")})

    def test_unsupported_type_rejected(self) -> None:
        """Unsupported types raise TypeError (no silent str())."""
        import datetime

        with pytest.raises(TypeError):
            canonical_serialize({"d": datetime.datetime.now()})

    def test_nested_determinism(self) -> None:
        """Nested dicts/lists are serialized deterministically."""
        obj = {
            "outer": {
                "z": [3, 2, 1],
                "a": {"nested": True},
            },
        }
        s1 = canonical_serialize(obj)
        s2 = canonical_serialize(obj)
        assert s1 == s2

    @pytest.mark.parametrize("value", [
        None, True, False, 0, 42, -1, 3.14, "", "hello",
        [], [1, 2], {}, {"a": 1}, [[1], [2]],
    ])
    def test_primitive_types_accepted(self, value: Any) -> None:
        """All JSON-primitive types serialize without error."""
        result = canonical_serialize({"v": value})
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Canonical Hash
# ---------------------------------------------------------------------------


class TestCanonicalHash:
    """Content-addressed hashing pins."""

    def test_deterministic(self) -> None:
        """Same input → same hash across multiple calls."""
        h1 = canonical_hash({"a": 1, "b": "hello"})
        h2 = canonical_hash({"a": 1, "b": "hello"})
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_key_order_independent(self) -> None:
        """Hash is key-order independent."""
        h1 = canonical_hash({"z": 1, "a": 2})
        h2 = canonical_hash({"a": 2, "z": 1})
        assert h1 == h2

    def test_different_input_different_hash(self) -> None:
        """Different inputs → different hashes."""
        h1 = canonical_hash({"a": 1})
        h2 = canonical_hash({"a": 2})
        assert h1 != h2

    def test_error_sentinel_on_unsupported_type(self) -> None:
        """Unsupported types produce error sentinel, not crash."""
        import datetime

        h = canonical_hash({"d": datetime.datetime.now()})
        assert h.startswith("error:")


# ---------------------------------------------------------------------------
# Temperature Policy
# ---------------------------------------------------------------------------


class TestTemperaturePolicy:
    """§24.10.1 temperature pinning enforcement."""

    @pytest.mark.parametrize("phase", [
        "CLASSIFY", "ROUTE", "VALIDATE", "VALIDATE_RETRY",
        "GATE", "APPLY", "VERIFY", "PLAN", "CONTEXT_EXPANSION",
    ])
    def test_decisional_phases_pinned_to_zero(
        self, phase: str, monkeypatch: Any,
    ) -> None:
        """ALL decisional phases are pinned to temperature=0."""
        monkeypatch.delenv("JARVIS_DECISIONAL_TEMPERATURE", raising=False)
        temp, category = resolve_temperature(
            phase=phase, requested_temperature=0.7,
        )
        assert temp == 0.0
        assert category == CallCategory.DECISIONAL

    @pytest.mark.parametrize("phase", ["GENERATE", "GENERATE_RETRY"])
    def test_creative_phases_use_requested_temperature(
        self, phase: str,
    ) -> None:
        """GENERATE phases use the requested (creative) temperature."""
        temp, category = resolve_temperature(
            phase=phase, requested_temperature=0.5,
        )
        assert temp == 0.5
        assert category == CallCategory.CREATIVE

    def test_creative_phases_set(self) -> None:
        """Only GENERATE and GENERATE_RETRY are creative."""
        assert CREATIVE_PHASES == frozenset({"GENERATE", "GENERATE_RETRY"})

    def test_decisional_temp_env_override(self, monkeypatch: Any) -> None:
        """Env override for decisional temperature is clamped."""
        monkeypatch.setenv("JARVIS_DECISIONAL_TEMPERATURE", "0.2")
        assert get_decisional_temperature() == 0.2

    def test_decisional_temp_clamped_high(self, monkeypatch: Any) -> None:
        """Values above 0.3 are clamped."""
        monkeypatch.setenv("JARVIS_DECISIONAL_TEMPERATURE", "1.0")
        assert get_decisional_temperature() == 0.3

    def test_decisional_temp_clamped_negative(
        self, monkeypatch: Any,
    ) -> None:
        """Negative values are clamped to 0."""
        monkeypatch.setenv("JARVIS_DECISIONAL_TEMPERATURE", "-0.5")
        assert get_decisional_temperature() == 0.0

    def test_decisional_temp_invalid(self, monkeypatch: Any) -> None:
        """Invalid values fall back to 0."""
        monkeypatch.setenv("JARVIS_DECISIONAL_TEMPERATURE", "abc")
        assert get_decisional_temperature() == 0.0

    def test_case_insensitive_phase(self) -> None:
        """Phase matching is case-insensitive."""
        temp, cat = resolve_temperature(
            phase="generate", requested_temperature=0.5,
        )
        assert cat == CallCategory.CREATIVE

    def test_empty_phase_is_decisional(self) -> None:
        """Empty/unknown phase defaults to DECISIONAL."""
        temp, cat = resolve_temperature(
            phase="", requested_temperature=0.5,
        )
        assert cat == CallCategory.DECISIONAL
        assert temp == 0.0


# ---------------------------------------------------------------------------
# Prompt Hasher
# ---------------------------------------------------------------------------


class TestPromptHasher:
    """PromptHasher stability pins."""

    def test_hash_prompt_deterministic(self) -> None:
        hasher = PromptHasher()
        h1 = hasher.hash_prompt("Generate code for foo")
        h2 = hasher.hash_prompt("Generate code for foo")
        assert h1 == h2
        assert len(h1) == 64

    def test_hash_prompt_different_input(self) -> None:
        hasher = PromptHasher()
        h1 = hasher.hash_prompt("prompt A")
        h2 = hasher.hash_prompt("prompt B")
        assert h1 != h2

    def test_hash_tool_order_sorted(self) -> None:
        """Tool order is sorted — same set produces same hash."""
        hasher = PromptHasher()
        h1 = hasher.hash_tool_order(("read_file", "search_code"))
        h2 = hasher.hash_tool_order(("search_code", "read_file"))
        assert h1 == h2

    def test_hash_decision_deterministic(self) -> None:
        hasher = PromptHasher()
        dh1 = hasher.hash_decision(
            prompt="test prompt",
            model_id="claude-sonnet-4-20250514",
            temperature=0.0,
            tool_names=("read_file", "search_code"),
        )
        dh2 = hasher.hash_decision(
            prompt="test prompt",
            model_id="claude-sonnet-4-20250514",
            temperature=0.0,
            tool_names=("read_file", "search_code"),
        )
        assert dh1.digest == dh2.digest
        assert dh1.prompt_hash == dh2.prompt_hash
        assert dh1.tool_order_hash == dh2.tool_order_hash

    def test_hash_decision_different_model(self) -> None:
        hasher = PromptHasher()
        dh1 = hasher.hash_decision(
            prompt="test", model_id="model-a", temperature=0.0,
        )
        dh2 = hasher.hash_decision(
            prompt="test", model_id="model-b", temperature=0.0,
        )
        assert dh1.digest != dh2.digest

    def test_hash_decision_different_temperature(self) -> None:
        hasher = PromptHasher()
        dh1 = hasher.hash_decision(
            prompt="test", model_id="model", temperature=0.0,
        )
        dh2 = hasher.hash_decision(
            prompt="test", model_id="model", temperature=0.5,
        )
        assert dh1.digest != dh2.digest

    def test_hash_decision_extra_context(self) -> None:
        hasher = PromptHasher()
        dh1 = hasher.hash_decision(
            prompt="test", model_id="model", temperature=0.0,
            extra_context={"op_id": "op-123"},
        )
        dh2 = hasher.hash_decision(
            prompt="test", model_id="model", temperature=0.0,
            extra_context={"op_id": "op-456"},
        )
        assert dh1.digest != dh2.digest

    def test_decision_hash_to_dict(self) -> None:
        dh = DecisionHash(
            digest="abc123",
            prompt_hash="def456",
            model_id="model",
            temperature=0.0,
            tool_order_hash="ghi789",
        )
        d = dh.to_dict()
        assert d["digest"] == "abc123"
        assert d["model_id"] == "model"
        assert d["temperature"] == 0.0


# ---------------------------------------------------------------------------
# Master Flag
# ---------------------------------------------------------------------------


class TestMasterFlag:

    def test_default_off(self, monkeypatch: Any) -> None:
        monkeypatch.delenv(
            "JARVIS_DETERMINISM_SUBSTRATE_ENABLED", raising=False,
        )
        assert is_substrate_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_truthy(self, val: str, monkeypatch: Any) -> None:
        monkeypatch.setenv("JARVIS_DETERMINISM_SUBSTRATE_ENABLED", val)
        assert is_substrate_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", ""])
    def test_falsy(self, val: str, monkeypatch: Any) -> None:
        monkeypatch.setenv("JARVIS_DETERMINISM_SUBSTRATE_ENABLED", val)
        assert is_substrate_enabled() is False
