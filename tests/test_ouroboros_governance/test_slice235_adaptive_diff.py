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
    _max_target_line_count,
    resolve_diff_capability_for_model,
    resolve_force_full_content,
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

    def test_capable_model_small_file_ALSO_uses_diff(self):
        """Re-emission drift does not scale with file size; if anything the
        trade is worst on small files, where retyping 141 lines to change 3
        buys nothing."""
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=120, threshold_lines=800,
        ) is False

    def test_the_threshold_no_longer_decides_anything(self):
        """A cost heuristic must never again decide a correctness question: at,
        below and above the old threshold the answer is identical."""
        answers = {
            should_force_full_content(
                schema_capability=_CAPABLE, target_line_count=n,
                threshold_lines=800,
            )
            for n in (1, 799, 800, 801, 5000)
        }
        assert answers == {False}

    def test_unknown_line_count_is_irrelevant(self):
        """There is nothing to be conservative ABOUT — the decision reads a
        negotiated capability, not a file."""
        assert should_force_full_content(
            schema_capability=_CAPABLE, target_line_count=None, threshold_lines=800,
        ) is False

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


class TestDiffCapabilityByFamily:
    def test_elite_families_are_diff_capable(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", raising=False)
        assert resolve_diff_capability_for_model("moonshotai/Kimi-K2.6") == "full_content_and_diff"
        assert resolve_diff_capability_for_model("deepseek-ai/DeepSeek-V4-Pro") == "full_content_and_diff"
        assert resolve_diff_capability_for_model("zai-org/GLM-5.1-FP8") == "full_content_and_diff"

    def test_qwen_397b_workhorse_is_not_diff_capable(self, monkeypatch):
        # The 397B is exactly the model that couldn't do verbatim diffs (the
        # reason 2b.1-diff was disabled) — must stay full_content_only.
        monkeypatch.delenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", raising=False)
        assert resolve_diff_capability_for_model("Qwen/Qwen-397B") == "full_content_only"

    def test_unknown_or_empty_model_full_only(self, monkeypatch):
        monkeypatch.delenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", raising=False)
        assert resolve_diff_capability_for_model("") == "full_content_only"
        assert resolve_diff_capability_for_model("randovendor/foo") == "full_content_only"

    def test_env_override_families(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DW_DIFF_CAPABLE_FAMILIES", "acme,Qwen")
        assert resolve_diff_capability_for_model("Qwen/Qwen-397B") == "full_content_and_diff"
        assert resolve_diff_capability_for_model("moonshotai/Kimi-K2.6") == "full_content_only"


class TestMaxTargetLineCount:
    def test_max_across_targets(self, tmp_path):
        (tmp_path / "small.py").write_text("a\nb\n")
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(500)))
        n = _max_target_line_count(["small.py", "big.py"], tmp_path)
        assert n == 500

    def test_missing_files_skipped(self, tmp_path):
        (tmp_path / "real.py").write_text("x\ny\nz\n")
        n = _max_target_line_count(["real.py", "ghost.py"], tmp_path)
        assert n == 3

    def test_all_missing_is_none(self, tmp_path):
        assert _max_target_line_count(["ghost.py"], tmp_path) is None

    def test_empty_targets_is_none(self, tmp_path):
        assert _max_target_line_count([], tmp_path) is None


class TestResolveForceFullContentSeam:
    def test_capable_large_uses_diff(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_DIFF_SCHEMA_THRESHOLD_LINES", raising=False)
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        out = resolve_force_full_content(
            schema_capability="full_content_and_diff",
            target_files=["big.py"], repo_root=tmp_path,
        )
        assert out is False

    def test_weak_model_forces_full(self, tmp_path):
        (tmp_path / "big.py").write_text("\n".join(str(i) for i in range(2000)))
        out = resolve_force_full_content(
            schema_capability="full_content_only",
            target_files=["big.py"], repo_root=tmp_path,
        )
        assert out is True

    def test_capable_small_ALSO_uses_diff(self, tmp_path):
        """Inverted deliberately. The size gate WAS the defect: a 144-line file
        took the whole-file path and three autonomous commits drifted on the
        141 lines they had no reason to retype."""
        (tmp_path / "small.py").write_text("a\nb\nc\n")
        out = resolve_force_full_content(
            schema_capability="full_content_and_diff",
            target_files=["small.py"], repo_root=tmp_path,
        )
        assert out is False

    def test_an_unreadable_target_can_no_longer_change_the_schema(self):
        """It used to fail soft to full_content on a bad read. There is no read
        left to fail — the decision consults a capability that was already
        negotiated — so an unreadable path can no longer push an op into a
        whole-file rewrite. Capability still decides in both directions."""
        assert resolve_force_full_content(
            schema_capability="full_content_and_diff",
            target_files=["x.py"], repo_root=None,
        ) is False
        assert resolve_force_full_content(
            schema_capability="full_content_only",
            target_files=["x.py"], repo_root=None,
        ) is True
