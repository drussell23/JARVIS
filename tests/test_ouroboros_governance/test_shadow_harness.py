"""Tests for the Ouroboros Shadow Harness.

The shadow harness runs candidate code in a side-effect-free environment
parallel to production.  The SideEffectFirewall provides hard enforcement,
the OutputComparator scores similarity, and ShadowHarness tracks confidence
over time with auto-disqualification.

No LLM calls.  Pure deterministic logic.
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import shutil
import subprocess
import textwrap

import pytest

from backend.core.ouroboros.governance.shadow_harness import (
    CompareMode,
    OutputComparator,
    ShadowHarness,
    ShadowModeViolation,
    ShadowResult,
    SideEffectFirewall,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def firewall() -> SideEffectFirewall:
    """Return a default SideEffectFirewall."""
    return SideEffectFirewall()


@pytest.fixture
def comparator() -> OutputComparator:
    """Return a default OutputComparator."""
    return OutputComparator()


@pytest.fixture
def harness() -> ShadowHarness:
    """Return a default ShadowHarness."""
    return ShadowHarness()


# ---------------------------------------------------------------------------
# TestSideEffectFirewall
# ---------------------------------------------------------------------------


class TestSideEffectFirewall:
    """The SideEffectFirewall blocks dangerous operations inside its context."""

    def test_blocks_file_write(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "secret.txt"
        with firewall:
            with pytest.raises(ShadowModeViolation, match="write"):
                builtins.open(str(target), "w")

    def test_blocks_file_append(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "log.txt"
        with firewall:
            with pytest.raises(ShadowModeViolation, match="write"):
                builtins.open(str(target), "a")

    def test_blocks_file_write_plus(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "data.txt"
        with firewall:
            with pytest.raises(ShadowModeViolation, match="write"):
                builtins.open(str(target), "r+")

    def test_blocks_file_exclusive_create(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "new.txt"
        with firewall:
            with pytest.raises(ShadowModeViolation, match="write"):
                builtins.open(str(target), "x")

    def test_allows_file_read(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "readable.txt"
        target.write_text("hello")
        with firewall:
            fh = builtins.open(str(target), "r")
            content = fh.read()
            fh.close()
        assert content == "hello"

    def test_allows_file_read_default_mode(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "default.txt"
        target.write_text("default mode")
        with firewall:
            fh = builtins.open(str(target))
            content = fh.read()
            fh.close()
        assert content == "default mode"

    def test_blocks_subprocess_run(self, firewall: SideEffectFirewall) -> None:
        with firewall:
            with pytest.raises(ShadowModeViolation, match="subprocess"):
                subprocess.run(["echo", "hello"])

    def test_blocks_subprocess_popen(self, firewall: SideEffectFirewall) -> None:
        with firewall:
            with pytest.raises(ShadowModeViolation, match="subprocess"):
                subprocess.Popen(["echo", "hello"])

    def test_blocks_os_remove(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("data")
        with firewall:
            with pytest.raises(ShadowModeViolation, match="os.remove"):
                os.remove(str(target))
        # File should still exist
        assert target.exists()

    def test_blocks_os_unlink(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target = tmp_path / "victim2.txt"
        target.write_text("data")
        with firewall:
            with pytest.raises(ShadowModeViolation, match="os.unlink"):
                os.unlink(str(target))
        assert target.exists()

    def test_blocks_shutil_rmtree(self, firewall: SideEffectFirewall, tmp_path) -> None:
        target_dir = tmp_path / "to_remove"
        target_dir.mkdir()
        with firewall:
            with pytest.raises(ShadowModeViolation, match="shutil.rmtree"):
                shutil.rmtree(str(target_dir))
        assert target_dir.exists()

    def test_allows_ast_parse(self, firewall: SideEffectFirewall) -> None:
        with firewall:
            tree = ast.parse("x = 1 + 2")
        assert isinstance(tree, ast.Module)

    def test_allows_json_loads(self, firewall: SideEffectFirewall) -> None:
        with firewall:
            data = json.loads('{"key": "value"}')
        assert data == {"key": "value"}

    def test_restores_originals_on_exit(self, firewall: SideEffectFirewall) -> None:
        original_open = builtins.open
        original_run = subprocess.run
        original_popen = subprocess.Popen
        original_remove = os.remove
        original_unlink = os.unlink
        original_rmtree = shutil.rmtree

        with firewall:
            # Inside firewall, things are patched
            assert builtins.open is not original_open

        # After exit, everything is restored
        assert builtins.open is original_open
        assert subprocess.run is original_run
        assert subprocess.Popen is original_popen
        assert os.remove is original_remove
        assert os.unlink is original_unlink
        assert shutil.rmtree is original_rmtree

    def test_restores_on_exception(self, firewall: SideEffectFirewall) -> None:
        original_open = builtins.open
        original_run = subprocess.run

        with pytest.raises(RuntimeError, match="boom"):
            with firewall:
                raise RuntimeError("boom")

        # Even after exception, originals are restored
        assert builtins.open is original_open
        assert subprocess.run is original_run


# ---------------------------------------------------------------------------
# TestOutputComparator
# ---------------------------------------------------------------------------


class TestOutputComparator:
    """OutputComparator scores similarity between expected and actual outputs."""

    def test_exact_match_1_0(self, comparator: OutputComparator) -> None:
        score = comparator.compare("hello world", "hello world", CompareMode.EXACT)
        assert score == 1.0

    def test_exact_mismatch_0_0(self, comparator: OutputComparator) -> None:
        score = comparator.compare("hello", "world", CompareMode.EXACT)
        assert score == 0.0

    def test_ast_identical_1_0(self, comparator: OutputComparator) -> None:
        code = "x = 1 + 2"
        score = comparator.compare(code, code, CompareMode.AST)
        assert score == 1.0

    def test_ast_whitespace_diff_high_score(self, comparator: OutputComparator) -> None:
        code_a = textwrap.dedent("""\
            def foo():
                return 1
        """)
        code_b = textwrap.dedent("""\
            def foo():
                return   1
        """)
        score = comparator.compare(code_a, code_b, CompareMode.AST)
        assert score == 1.0  # ASTs are identical despite whitespace

    def test_ast_different_code_low_score(self, comparator: OutputComparator) -> None:
        code_a = "x = 1"
        code_b = "y = some_function(arg1, arg2, arg3)"
        score = comparator.compare(code_a, code_b, CompareMode.AST)
        assert score < 1.0

    def test_ast_unparseable_0_0(self, comparator: OutputComparator) -> None:
        code_a = "def valid(): pass"
        code_b = "def invalid(: oops"
        score = comparator.compare(code_a, code_b, CompareMode.AST)
        assert score == 0.0

    def test_ast_both_unparseable_0_0(self, comparator: OutputComparator) -> None:
        code_a = "def invalid(:"
        code_b = "def also_invalid(:"
        score = comparator.compare(code_a, code_b, CompareMode.AST)
        assert score == 0.0

    def test_semantic_delegates_to_ast(self, comparator: OutputComparator) -> None:
        code = "x = 1 + 2"
        ast_score = comparator.compare(code, code, CompareMode.AST)
        semantic_score = comparator.compare(code, code, CompareMode.SEMANTIC)
        assert ast_score == semantic_score

    def test_ast_partial_score_common_prefix(self, comparator: OutputComparator) -> None:
        """Different ASTs should produce a partial score based on common prefix."""
        code_a = "x = 1\ny = 2"
        code_b = "x = 1\nz = 999"
        score = comparator.compare(code_a, code_b, CompareMode.AST)
        assert 0.0 < score < 1.0


# ---------------------------------------------------------------------------
# TestShadowResult
# ---------------------------------------------------------------------------


class TestShadowResult:
    """ShadowResult is a frozen dataclass that captures a single run's output."""

    def test_result_creation(self) -> None:
        result = ShadowResult(
            confidence=0.95,
            comparison_mode=CompareMode.EXACT,
            violations=(),
            shadow_duration_s=0.123,
            production_match=True,
            disqualified=False,
        )
        assert result.confidence == 0.95
        assert result.comparison_mode is CompareMode.EXACT
        assert result.violations == ()
        assert result.shadow_duration_s == 0.123
        assert result.production_match is True
        assert result.disqualified is False

    def test_result_is_frozen(self) -> None:
        result = ShadowResult(
            confidence=0.9,
            comparison_mode=CompareMode.AST,
            violations=(),
            shadow_duration_s=0.5,
            production_match=True,
            disqualified=False,
        )
        with pytest.raises(AttributeError):
            result.confidence = 0.0  # type: ignore[misc]

    def test_result_with_violations(self) -> None:
        result = ShadowResult(
            confidence=0.0,
            comparison_mode=CompareMode.EXACT,
            violations=("attempted file write", "attempted subprocess"),
            shadow_duration_s=0.01,
            production_match=False,
            disqualified=True,
        )
        assert len(result.violations) == 2
        assert "file write" in result.violations[0]


# ---------------------------------------------------------------------------
# TestShadowHarness
# ---------------------------------------------------------------------------


class TestShadowHarness:
    """ShadowHarness tracks confidence over time and auto-disqualifies."""

    def test_initial_state(self, harness: ShadowHarness) -> None:
        assert harness.is_disqualified is False

    def test_disqualification_after_3_low(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(0.5)
        assert harness.is_disqualified is False
        harness.record_run(0.6)
        assert harness.is_disqualified is False
        harness.record_run(0.4)
        assert harness.is_disqualified is True

    def test_no_disqualification_with_mixed(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(0.5)  # low
        harness.record_run(0.8)  # high -- resets streak
        harness.record_run(0.5)  # low
        harness.record_run(0.5)  # low (only 2 consecutive)
        assert harness.is_disqualified is False

    def test_high_confidence_resets_streak(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(0.5)  # low
        harness.record_run(0.5)  # low (2 consecutive)
        harness.record_run(0.9)  # high -- resets streak to 0
        harness.record_run(0.5)  # low (only 1 consecutive now)
        harness.record_run(0.5)  # low (only 2 consecutive)
        assert harness.is_disqualified is False

    def test_reset_clears_state(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(0.5)
        harness.record_run(0.5)
        harness.record_run(0.5)
        assert harness.is_disqualified is True

        harness.reset()
        assert harness.is_disqualified is False

    def test_custom_threshold_and_disqualify_after(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.9, disqualify_after=2)
        harness.record_run(0.85)  # below 0.9
        harness.record_run(0.80)  # below 0.9, 2 consecutive
        assert harness.is_disqualified is True

    def test_exactly_at_threshold_is_not_low(self) -> None:
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=3)
        harness.record_run(0.7)  # exactly at threshold -- NOT low
        harness.record_run(0.7)
        harness.record_run(0.7)
        assert harness.is_disqualified is False

    def test_record_run_after_disqualified(self) -> None:
        """Once disqualified, further runs do not change state."""
        harness = ShadowHarness(confidence_threshold=0.7, disqualify_after=2)
        harness.record_run(0.5)
        harness.record_run(0.5)
        assert harness.is_disqualified is True
        harness.record_run(0.99)  # even a high score doesn't undo disqualification
        assert harness.is_disqualified is True
