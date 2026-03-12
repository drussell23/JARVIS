"""Tests for RepairContext dataclass."""
from __future__ import annotations

import dataclasses
import pytest

from backend.core.ouroboros.governance.op_context import RepairContext


class TestRepairContext:
    def test_instantiate_all_fields(self):
        ctx = RepairContext(
            iteration=2,
            max_iterations=5,
            failure_class="test",
            failure_signature_hash="deadbeef" * 8,
            failing_tests=("tests/test_foo.py::test_bar", "tests/test_foo.py::test_baz"),
            failure_summary="AssertionError: expected 1 got 2",
            current_candidate_content="def foo(): return 2",
            current_candidate_file_path="src/foo.py",
        )
        assert ctx.iteration == 2
        assert ctx.max_iterations == 5
        assert ctx.failure_class == "test"
        assert ctx.failing_tests == ("tests/test_foo.py::test_bar", "tests/test_foo.py::test_baz")
        assert ctx.current_candidate_file_path == "src/foo.py"

    def test_is_frozen(self):
        ctx = RepairContext(
            iteration=1,
            max_iterations=5,
            failure_class="syntax",
            failure_signature_hash="abc",
            failing_tests=(),
            failure_summary="SyntaxError",
            current_candidate_content="x",
            current_candidate_file_path="f.py",
        )
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            ctx.iteration = 99  # type: ignore[misc]

    def test_empty_failing_tests(self):
        ctx = RepairContext(
            iteration=0,
            max_iterations=3,
            failure_class="env",
            failure_signature_hash="",
            failing_tests=(),
            failure_summary="ModuleNotFoundError: no module named foo",
            current_candidate_content="",
            current_candidate_file_path="",
        )
        assert ctx.failing_tests == ()
