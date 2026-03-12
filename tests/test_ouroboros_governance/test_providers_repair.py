# tests/test_ouroboros_governance/test_providers_repair.py
from __future__ import annotations
import pytest
from unittest.mock import MagicMock
from backend.core.ouroboros.governance.providers import (
    _build_codegen_prompt,
    _check_diff_budget,
)
from backend.core.ouroboros.governance.op_context import RepairContext


def _ctx():
    ctx = MagicMock()
    ctx.op_id = "test-op"
    ctx.description = "fix foo"
    ctx.target_files = ("src/foo.py",)
    ctx.cross_repo = False
    ctx.repo_scope = ("jarvis",)
    ctx.telemetry = None
    ctx.generation = None
    ctx.routing = None
    ctx.dependency_edges = ()
    return ctx


def _repair_ctx():
    return RepairContext(
        iteration=2, max_iterations=5, failure_class="test",
        failure_signature_hash="abc123",
        failing_tests=("tests/test_foo.py::test_bar",),
        failure_summary="AssertionError: expected 1 got 2",
        current_candidate_content="def foo(): return 2\n",
        current_candidate_file_path="src/foo.py",
    )


class TestBuildCodegenPromptRepairContext:
    def test_repair_section_injected(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("def foo(): return 1\n")
        prompt = _build_codegen_prompt(_ctx(), repo_root=tmp_path, repair_context=_repair_ctx())
        assert "REPAIR" in prompt

    def test_no_repair_section_without_context(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("def foo(): return 1\n")
        prompt = _build_codegen_prompt(_ctx(), repo_root=tmp_path, repair_context=None)
        assert "REPAIR ITERATION" not in prompt

    def test_failing_tests_appear(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")
        prompt = _build_codegen_prompt(_ctx(), repo_root=tmp_path, repair_context=_repair_ctx())
        assert "tests/test_foo.py::test_bar" in prompt

    def test_candidate_content_appears(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")
        rc = _repair_ctx()
        prompt = _build_codegen_prompt(_ctx(), repo_root=tmp_path, repair_context=rc)
        assert rc.current_candidate_content in prompt


class TestCheckDiffBudget:
    def test_small_diff_within_budget(self):
        diff = "\n".join(["+new line" for _ in range(10)] + ["-old line" for _ in range(5)])
        assert _check_diff_budget(diff, max_diff_lines=150, max_files_changed=3) is True

    def test_oversized_diff_rejected(self):
        diff = "\n".join([f"+line {i}" for i in range(200)])
        assert _check_diff_budget(diff, max_diff_lines=150, max_files_changed=3) is False

    def test_empty_diff_within_budget(self):
        assert _check_diff_budget("", max_diff_lines=150, max_files_changed=3) is True
