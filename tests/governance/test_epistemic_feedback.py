"""
Tests for epistemic_feedback.py — pure stdlib leaf module.
TDD: written BEFORE implementation. All tests must fail before the module exists.

Spec: docs/superpowers/specs/2026-06-22-epistemic-feedback-and-lane-escalation.md §1.2
"""
from __future__ import annotations

import os
import sys
import importlib
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_module():
    """Import the module under test. Raises ImportError if not yet written."""
    spec_path = (
        "backend.core.ouroboros.governance.epistemic_feedback"
    )
    return importlib.import_module(spec_path)


# ---------------------------------------------------------------------------
# 1. build_failure_context — clean parse (no syntax fatal header)
# ---------------------------------------------------------------------------

class TestBuildFailureContextCleanParse:
    """clean-parse failed_src → labeled unified diff present, NO syntax-fatal header."""

    def test_no_syntax_fatal_header_on_clean_src(self):
        mod = _import_module()
        prior = "def foo():\n    return 1\n"
        failed = "def foo():\n    return 2\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=["test_foo"],
            sub_goal_label="my-goal",
        )
        assert isinstance(result, str)
        assert "[SOVEREIGN SYNTAX FATAL]" not in result

    def test_unified_diff_present_for_clean_src(self):
        mod = _import_module()
        prior = "def foo():\n    return 1\n"
        failed = "def foo():\n    return 2\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=[],
        )
        # unified diff headers
        assert "Previous Stable Sub-Goal" in result
        assert "Current Failing Iteration" in result
        # diff hunk marker
        assert "@@" in result

    def test_sub_goal_label_appears_in_output(self):
        mod = _import_module()
        prior = "x = 1\n"
        failed = "x = 2\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=[],
            sub_goal_label="GOAL-42",
        )
        assert "GOAL-42" in result


# ---------------------------------------------------------------------------
# 2. build_failure_context — SyntaxError path
# ---------------------------------------------------------------------------

class TestBuildFailureContextSyntaxError:
    """failed_src with SyntaxError → [SOVEREIGN SYNTAX FATAL] header AND a diff still present."""

    BAD_SRC = "def (:\n    bad\n"  # guaranteed SyntaxError

    def test_syntax_fatal_header_present(self):
        mod = _import_module()
        prior = "def foo():\n    pass\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=self.BAD_SRC,
            stderr="SyntaxError: invalid syntax",
            failing_tests=["test_x"],
        )
        assert "[SOVEREIGN SYNTAX FATAL]" in result
        assert "line=" in result

    def test_diff_still_present_after_syntax_error(self):
        """fail-soft: even on SyntaxError, the diff block is still emitted."""
        mod = _import_module()
        prior = "def foo():\n    pass\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=self.BAD_SRC,
            stderr="SyntaxError",
            failing_tests=[],
        )
        assert "Previous Stable Sub-Goal" in result
        assert "Current Failing Iteration" in result

    def test_syntax_fatal_header_has_lineno(self):
        mod = _import_module()
        prior = "x = 1\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=self.BAD_SRC,
            stderr="",
            failing_tests=[],
        )
        # header must contain line= with a number
        import re
        match = re.search(r"\[SOVEREIGN SYNTAX FATAL\] line=(\d+)", result)
        assert match is not None, f"header not found in: {result!r}"

    def test_syntax_fatal_header_has_msg(self):
        mod = _import_module()
        prior = "x = 1\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=self.BAD_SRC,
            stderr="",
            failing_tests=[],
        )
        assert "msg=" in result


# ---------------------------------------------------------------------------
# 3. build_failure_context — fail-soft on garbage/None inputs
# ---------------------------------------------------------------------------

class TestBuildFailureContextFailSoft:
    """Garbage or None inputs must never raise; must return a str (possibly empty)."""

    def test_none_prior_src_no_raise(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src=None,
            failed_src=None,
            stderr=None,
            failing_tests=None,
        )
        assert isinstance(result, str)

    def test_integer_inputs_no_raise(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src=42,
            failed_src=object(),
            stderr=123,
            failing_tests={"a": "b"},
        )
        assert isinstance(result, str)

    def test_empty_strings_no_raise(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="",
            failed_src="",
            stderr="",
            failing_tests=[],
        )
        assert isinstance(result, str)

    def test_failing_tests_as_single_string_no_raise(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr="",
            failing_tests="test_something",  # str instead of list
        )
        assert isinstance(result, str)

    def test_very_large_failing_tests_list_no_raise(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr="",
            failing_tests=list(range(1000)),
        )
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# 4. build_failure_context — diff truncation (middle-elided)
# ---------------------------------------------------------------------------

class TestBuildFailureContextDiffTruncation:
    """diff over JARVIS_EPISTEMIC_DIFF_MAX_CHARS → middle-elided, both ends present."""

    def test_long_diff_truncated_with_elision_marker(self, monkeypatch):
        mod = _import_module()
        # Force a very small max so we trigger truncation
        monkeypatch.setenv("JARVIS_EPISTEMIC_DIFF_MAX_CHARS", "200")
        importlib.reload(mod)
        mod = _import_module()

        prior_lines = [f"line_{i} = {i}\n" for i in range(200)]
        failed_lines = [f"line_{i} = {i + 1000}\n" for i in range(200)]
        prior = "".join(prior_lines)
        failed = "".join(failed_lines)
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=[],
        )
        assert "elided" in result or "chars elided" in result

    def test_long_diff_both_ends_present(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_DIFF_MAX_CHARS", "300")
        importlib.reload(mod)
        mod = _import_module()

        prior_lines = [f"line_{i} = {i}\n" for i in range(300)]
        failed_lines = [f"line_{i} = {i + 9999}\n" for i in range(300)]
        prior = "".join(prior_lines)
        failed = "".join(failed_lines)
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=[],
        )
        # Both the very start and very end of the diff should be present
        # (beginning = "---" or "+++" header lines; end = last hunk lines)
        assert "---" in result
        assert "+++" in result

    def test_short_diff_not_truncated(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_DIFF_MAX_CHARS", "4000")
        importlib.reload(mod)
        mod = _import_module()

        prior = "x = 1\n"
        failed = "x = 2\n"
        result = mod.build_failure_context(
            prior_src=prior,
            failed_src=failed,
            stderr="",
            failing_tests=[],
        )
        assert "elided" not in result


# ---------------------------------------------------------------------------
# 5. build_failure_context — stderr trace + failing tests labels
# ---------------------------------------------------------------------------

class TestBuildFailureContextStderrTrace:
    """Stderr tail and failing_tests labels appear in output."""

    def test_stderr_tail_label_present(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr="AssertionError: something went wrong\n",
            failing_tests=["test_foo"],
        )
        assert "FAILING TEST STDERR" in result

    def test_stderr_content_present(self):
        mod = _import_module()
        sentinel = "AssertionError: unique_sentinel_value_xyz"
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr=sentinel,
            failing_tests=[],
        )
        assert "unique_sentinel_value_xyz" in result

    def test_failing_tests_label_present(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr="",
            failing_tests=["test_alpha", "test_beta"],
        )
        assert "FAILING TESTS" in result

    def test_failing_test_ids_in_output(self):
        mod = _import_module()
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr="",
            failing_tests=["test_alpha", "test_beta"],
        )
        assert "test_alpha" in result
        assert "test_beta" in result

    def test_stderr_tail_only_last_n_chars(self, monkeypatch):
        """Only the tail (last JARVIS_EPISTEMIC_TRACE_MAX_CHARS) of stderr is included."""
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TRACE_MAX_CHARS", "20")
        importlib.reload(mod)
        mod = _import_module()

        stderr = "A" * 100 + "TAIL_SENTINEL"
        result = mod.build_failure_context(
            prior_src="x=1\n",
            failed_src="x=2\n",
            stderr=stderr,
            failing_tests=[],
        )
        assert "TAIL_SENTINEL" in result
        # The very beginning of the long stderr should NOT be present
        assert "A" * 50 not in result


# ---------------------------------------------------------------------------
# 6. temperature_for_attempt
# ---------------------------------------------------------------------------

class TestTemperatureForAttempt:
    """Parametric degeneration: base_temp * decay^count, floored, fail-soft."""

    def test_zero_count_returns_base(self):
        mod = _import_module()
        assert mod.temperature_for_attempt(0.2, 0) == pytest.approx(0.2)

    def test_one_count_half_decay(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "0.5")
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "0.0")
        importlib.reload(mod)
        mod = _import_module()
        result = mod.temperature_for_attempt(0.2, 1)
        assert result == pytest.approx(0.1)

    def test_three_count_floored(self, monkeypatch):
        """0.2 * 0.5^3 = 0.025, floor=0.0 → still 0.025; with floor=0.05 → 0.05."""
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "0.5")
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "0.05")
        importlib.reload(mod)
        mod = _import_module()
        result = mod.temperature_for_attempt(0.2, 3)
        # 0.2 * 0.5^3 = 0.025 < 0.05 → floored to 0.05
        assert result == pytest.approx(0.05)

    def test_floor_is_respected_default(self, monkeypatch):
        """Default floor is 0.0; result never goes below 0."""
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "0.5")
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "0.0")
        importlib.reload(mod)
        mod = _import_module()
        result = mod.temperature_for_attempt(0.2, 100)
        assert result >= 0.0

    def test_negative_count_treated_as_zero(self, monkeypatch):
        """Spec says max(0, repeated_signature_count) — negative treated as 0."""
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "0.5")
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "0.0")
        importlib.reload(mod)
        mod = _import_module()
        result = mod.temperature_for_attempt(0.2, -5)
        assert result == pytest.approx(0.2)

    def test_fail_soft_on_bad_env(self, monkeypatch):
        """If env is garbage, function must not raise — returns base_temp."""
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_DECAY", "not_a_float")
        monkeypatch.setenv("JARVIS_EPISTEMIC_TEMP_FLOOR", "also_bad")
        importlib.reload(mod)
        mod = _import_module()
        result = mod.temperature_for_attempt(0.2, 3)
        assert isinstance(result, float)
        assert result == pytest.approx(0.2)  # fail-soft returns base_temp


# ---------------------------------------------------------------------------
# 7. pivot_verdict
# ---------------------------------------------------------------------------

class TestPivotVerdict:
    """Unresolvable-path detection: True iff temp_at_floor AND count >= stall_passes."""

    def test_true_when_at_floor_and_sufficient_count(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.pivot_verdict(2, True) is True

    def test_false_when_not_at_floor(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.pivot_verdict(2, False) is False

    def test_false_when_count_below_stall_passes(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.pivot_verdict(1, True) is False

    def test_true_when_count_exceeds_stall_passes(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.pivot_verdict(5, True) is True

    def test_fail_soft_on_type_error(self):
        """Non-numeric count → fail-soft returns False."""
        mod = _import_module()
        result = mod.pivot_verdict("bad", True)
        assert result is False

    def test_false_when_both_false(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.pivot_verdict(0, False) is False


# ---------------------------------------------------------------------------
# 7b. should_pivot — composite stuck-signature OR thrash backstop
# ---------------------------------------------------------------------------

class TestShouldPivot:
    """should_pivot composes the legacy stuck-wall trigger with a
    budget-exhaustion (thrash / non-convergence) backstop."""

    def _reload(self, monkeypatch):
        mod = _import_module()
        importlib.reload(mod)
        return _import_module()

    def test_stuck_signature_reason(self, monkeypatch):
        """(a) Legacy condition (temp_at_floor AND count>=stall_passes) ->
        (True, 'stuck_signature')."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
        mod = self._reload(monkeypatch)
        # mid-budget so the thrash backstop does NOT also fire
        pivot, reason = mod.should_pivot(
            repeated_signature_count=2,
            temp_at_floor=True,
            total_attempts=1,
            max_attempts=5,
        )
        assert pivot is True
        assert reason == "stuck_signature"

    def test_budget_exhausted_reason_thrash(self, monkeypatch):
        """(b) THE FIX: total_attempts>=max_attempts pivots even with
        repeated_signature_count=0 (the thrash / never-repeating case)."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
        mod = self._reload(monkeypatch)
        pivot, reason = mod.should_pivot(
            repeated_signature_count=0,
            temp_at_floor=False,
            total_attempts=5,
            max_attempts=5,
        )
        assert pivot is True
        assert reason == "budget_exhausted"

    def test_false_mid_budget_not_stuck(self, monkeypatch):
        """(c) mid-budget, not stuck -> (False, '')."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
        mod = self._reload(monkeypatch)
        pivot, reason = mod.should_pivot(
            repeated_signature_count=0,
            temp_at_floor=False,
            total_attempts=2,
            max_attempts=5,
        )
        assert pivot is False
        assert reason == ""

    def test_flag_off_only_legacy_trigger(self, monkeypatch):
        """(d) JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED=false -> ONLY the legacy
        trigger applies; budget-exhaustion does NOT pivot (OFF byte-identical
        to today's pivot_verdict behavior)."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "false")
        mod = self._reload(monkeypatch)
        # Thrash case that WOULD pivot when on:
        pivot, reason = mod.should_pivot(
            repeated_signature_count=0,
            temp_at_floor=False,
            total_attempts=5,
            max_attempts=5,
        )
        assert pivot is False
        assert reason == ""
        # ...but the legacy stuck-wall trigger STILL fires with the flag off:
        pivot2, reason2 = mod.should_pivot(
            repeated_signature_count=2,
            temp_at_floor=True,
            total_attempts=1,
            max_attempts=5,
        )
        assert pivot2 is True
        assert reason2 == "stuck_signature"

    def test_flag_off_equals_pivot_verdict(self, monkeypatch):
        """OFF byte-identical: with the flag off, should_pivot's verdict
        matches pivot_verdict exactly across the grid (ignoring budget)."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "false")
        mod = self._reload(monkeypatch)
        for count in (0, 1, 2, 5):
            for floor in (True, False):
                legacy = mod.pivot_verdict(count, floor)
                pivot, _ = mod.should_pivot(
                    repeated_signature_count=count,
                    temp_at_floor=floor,
                    total_attempts=99,  # would trip budget if on
                    max_attempts=5,
                )
                assert pivot is legacy

    def test_fail_soft_on_bad_input(self, monkeypatch):
        """(e) fail-soft -> (False, '') on non-numeric input."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
        mod = self._reload(monkeypatch)
        pivot, reason = mod.should_pivot(
            repeated_signature_count="bad",
            temp_at_floor=False,
            total_attempts="bad",
            max_attempts=5,
        )
        assert pivot is False
        assert reason == ""

    def test_stuck_signature_takes_precedence(self, monkeypatch):
        """When BOTH triggers would fire, stuck_signature wins (legacy first)."""
        monkeypatch.setenv("JARVIS_EPISTEMIC_PIVOT_PASSES", "2")
        monkeypatch.setenv("JARVIS_EPISTEMIC_THRASH_PIVOT_ENABLED", "true")
        mod = self._reload(monkeypatch)
        pivot, reason = mod.should_pivot(
            repeated_signature_count=3,
            temp_at_floor=True,
            total_attempts=5,
            max_attempts=5,
        )
        assert pivot is True
        assert reason == "stuck_signature"


# ---------------------------------------------------------------------------
# 8. epistemic_feedback_enabled
# ---------------------------------------------------------------------------

class TestEpistemicFeedbackEnabled:
    """epistemic_feedback_enabled() defaults to True."""

    def test_default_true(self, monkeypatch):
        mod = _import_module()
        monkeypatch.delenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", raising=False)
        importlib.reload(mod)
        mod = _import_module()
        assert mod.epistemic_feedback_enabled() is True

    def test_false_when_env_set_false(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "false")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.epistemic_feedback_enabled() is False

    def test_false_when_env_set_zero(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "0")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.epistemic_feedback_enabled() is False

    def test_true_when_env_set_true(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "true")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.epistemic_feedback_enabled() is True

    def test_true_when_env_set_one(self, monkeypatch):
        mod = _import_module()
        monkeypatch.setenv("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", "1")
        importlib.reload(mod)
        mod = _import_module()
        assert mod.epistemic_feedback_enabled() is True
