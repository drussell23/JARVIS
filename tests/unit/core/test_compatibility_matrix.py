"""tests/unit/core/test_compatibility_matrix.py — P3-3 upgrade compat matrix."""
from __future__ import annotations

import pytest

from backend.core.compatibility_matrix import (
    Component,
    ComponentVersion,
    CompatibilityRule,
    CompatibilityMatrix,
    DEFAULT_RULES,
    get_compatibility_matrix,
)


def _cv(component: str, version: str) -> ComponentVersion:
    return ComponentVersion.parse(component, version)


class TestComponentVersion:
    def test_parse_major_minor_patch(self):
        cv = ComponentVersion.parse("jarvis", "2.3.1")
        assert cv.major == 2
        assert cv.minor == 3
        assert cv.patch == 1

    def test_parse_major_minor_only_defaults_patch_to_zero(self):
        cv = ComponentVersion.parse("prime", "2.3")
        assert cv.patch == 0

    def test_parse_invalid_raises(self):
        with pytest.raises(ValueError):
            ComponentVersion.parse("jarvis", "2")

    def test_ordering_by_major_minor_patch(self):
        a = _cv("jarvis", "2.3.0")
        b = _cv("jarvis", "2.4.0")
        c = _cv("jarvis", "3.0.0")
        assert a < b < c
        assert c > b > a

    def test_str_includes_component_and_version(self):
        cv = _cv("jarvis", "2.3.1")
        assert "jarvis" in str(cv)
        assert "2.3.1" in str(cv)


class TestCompatibilityRule:
    def test_rule_covers_matching_pair(self):
        rule = CompatibilityRule(
            component_a="jarvis",
            component_b="prime",
            min_a=(2, 0, 0), max_a=(2, 99, 99),
            min_b=(2, 0, 0), max_b=(2, 99, 99),
        )
        a = _cv("jarvis", "2.3.0")
        b = _cv("prime", "2.1.0")
        assert rule.covers(a, b) is True

    def test_rule_rejects_wrong_component_pair(self):
        rule = CompatibilityRule(
            component_a="jarvis",
            component_b="prime",
        )
        a = _cv("jarvis", "2.3.0")
        b = _cv("reactor", "2.1.0")   # wrong component
        assert rule.covers(a, b) is False

    def test_rule_rejects_out_of_range_version(self):
        rule = CompatibilityRule(
            component_a="jarvis",
            component_b="prime",
            min_a=(2, 0, 0), max_a=(2, 99, 99),
            min_b=(2, 0, 0), max_b=(2, 99, 99),
        )
        a = _cv("jarvis", "2.3.0")
        b = _cv("prime", "1.9.0")   # below min_b
        assert rule.covers(a, b) is False

    def test_rule_works_symmetrically(self):
        rule = CompatibilityRule(
            component_a="jarvis",
            component_b="prime",
            min_a=(2, 0, 0), max_a=(2, 99, 99),
            min_b=(2, 0, 0), max_b=(2, 99, 99),
        )
        a = _cv("jarvis", "2.3.0")
        b = _cv("prime", "2.1.0")
        assert rule.covers(a, b) == rule.covers(b, a)

    def test_unbounded_rule_covers_any_version(self):
        rule = CompatibilityRule(
            component_a="jarvis",
            component_b="prime",
        )
        a = _cv("jarvis", "99.99.99")
        b = _cv("prime", "0.0.1")
        assert rule.covers(a, b) is True


class TestCompatibilityMatrix:
    """N/N-1/N+1 contract tests."""

    def _matrix(self) -> CompatibilityMatrix:
        return CompatibilityMatrix(DEFAULT_RULES)

    # --- Same-major (N/N) pairs ---

    def test_jarvis_prime_same_major_compatible(self):
        ok, _ = self._matrix().is_compatible(
            _cv("jarvis", "2.3.0"), _cv("prime", "2.3.0")
        )
        assert ok is True

    def test_jarvis_reactor_same_major_compatible(self):
        ok, _ = self._matrix().is_compatible(
            _cv("jarvis", "2.1.0"), _cv("reactor", "2.0.0")
        )
        assert ok is True

    def test_prime_reactor_same_major_compatible(self):
        ok, _ = self._matrix().is_compatible(
            _cv("prime", "2.2.0"), _cv("reactor", "2.1.0")
        )
        assert ok is True

    # --- N/N-1 backward compatibility ---

    def test_jarvis_2x_prime_1x_allowed(self):
        ok, _ = self._matrix().is_compatible(
            _cv("jarvis", "2.3.0"), _cv("prime", "1.9.0")
        )
        assert ok is True

    def test_jarvis_2x_reactor_1x_allowed(self):
        ok, _ = self._matrix().is_compatible(
            _cv("jarvis", "2.0.0"), _cv("reactor", "1.8.0")
        )
        assert ok is True

    # --- N+1 forward — not in default rules → should fail ---

    def test_jarvis_2x_prime_3x_not_covered(self):
        ok, reason = self._matrix().is_compatible(
            _cv("jarvis", "2.3.0"), _cv("prime", "3.0.0")
        )
        assert ok is False
        assert reason  # non-empty reason string

    # --- Incompatible cross-major (beyond N-1) ---

    def test_prime_1x_reactor_2x_not_covered(self):
        ok, _ = self._matrix().is_compatible(
            _cv("prime", "1.9.0"), _cv("reactor", "2.0.0")
        )
        assert ok is False

    # --- check_all ---

    def test_check_all_empty_when_all_compatible(self):
        issues = self._matrix().check_all({
            Component.JARVIS.value: "2.3.0",
            Component.PRIME.value: "2.2.0",
            Component.REACTOR.value: "2.1.0",
        })
        assert issues == []

    def test_check_all_reports_incompatible_pair(self):
        issues = self._matrix().check_all({
            Component.JARVIS.value: "2.3.0",
            Component.PRIME.value: "3.0.0",   # not in rules
            Component.REACTOR.value: "2.1.0",
        })
        assert len(issues) >= 1
        # The jarvis↔prime pair must be in the report
        assert any("jarvis" in i or "prime" in i for i in issues)

    def test_check_all_single_component_no_issues(self):
        issues = self._matrix().check_all({Component.JARVIS.value: "2.3.0"})
        assert issues == []

    def test_module_singleton_is_reused(self):
        m1 = get_compatibility_matrix()
        m2 = get_compatibility_matrix()
        assert m1 is m2
