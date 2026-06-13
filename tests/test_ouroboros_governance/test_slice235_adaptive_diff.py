"""Slice 235 — adaptive diff: capability+size-gated force_full_content.

Layer-5: DW emits a 60K-char full_content blob → JSONDecodeError. Root cause is a
DEAD-CODED flag: providers.py computes `_force_full` from the brain's
`schema_capability` (the full 2b.1-diff machinery — applier + validator — already
exists) but then passes hardcoded `force_full_content=True`, and DW forces True
unconditionally. So the native unified-diff path is off for everyone.

This is the pure keystone that re-enables it CONDITIONALLY: full_content unless the
model can do verbatim diffs AND the file is large enough to warrant one (small
files keep full_content — diffs add no value and the blob problem only hits large
files). Both providers route through this single function instead of hardcoding.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.providers import (
    _diff_schema_threshold_lines,
    should_force_full_content,
)

_CAPABLE = "full_content_and_diff"
_WEAK = "full_content_only"


class TestShouldForceFullContent:
    def test_weak_model_always_full_even_large(self):
        assert should_force_full_content(
            schema_capability=_WEAK, target_line_count=5000, threshold_lines=800,
        ) is True

    def test_capable_model_large_file_uses_diff(self):
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=5000, threshold_lines=800,
        ) is False

    def test_capable_model_small_file_stays_full(self):
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=120, threshold_lines=800,
        ) is True

    def test_exactly_threshold_stays_full(self):
        # strictly greater-than → at the threshold we stay full_content
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=800, threshold_lines=800,
        ) is True

    def test_unknown_line_count_is_conservative_full(self):
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=None, threshold_lines=800,
        ) is True

    def test_unknown_capability_is_conservative_full(self):
        assert should_force_full_content(
            schema_capability="", target_line_count=5000, threshold_lines=800,
        ) is True

    def test_threshold_env_default_and_override(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", raising=False)
        d = _diff_schema_threshold_lines()
        assert isinstance(d, int) and d > 0
        monkeypatch.setenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", "1500")
        assert _diff_schema_threshold_lines() == 1500
        monkeypatch.setenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", "-5")
        assert _diff_schema_threshold_lines() == d  # invalid → default
