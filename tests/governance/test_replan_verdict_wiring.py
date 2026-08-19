"""The re-planner reasons from the verdict, on BOTH paths.

`generate_runner.run()` — the SHIPPING path
(`JARVIS_PHASE_RUNNER_GATE_EXTRACTED`) — read a bare `validation` that has no
binding anywhere in its scope, guarded by `if 'validation' in dir()`. The
guard therefore always evaluated False, and dynamic re-planning ran on empty
failure context on every real op, while the inline orchestrator twin read a
real verdict. Runner parity, again.

NO NEW VERDICT TYPE WAS DEFINED. `ValidationResult` is already frozen, already
carries `failure_class` + `short_summary`, and is already on
`OperationContext`. `advance()` uses `dataclasses.replace`, so a verdict set
at VALIDATE carries forward into the next GENERATE attempt — which is exactly
the "previous pass's verdict" the re-planner wants. A parallel dataclass would
have been a second spelling of a value that already existed.

`replan_inputs` is total: every degenerate input maps to a defined state,
because a re-planner must degrade rather than fly blind or raise.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.op_context import (
    REPLAN_SUMMARY_MAX_CHARS,
    ValidationResult,
    replan_inputs,
)


def _verdict(**kw) -> ValidationResult:
    base = dict(passed=False, best_candidate=None,
                validation_duration_s=0.0, error="e")
    base.update(kw)
    return ValidationResult(**base)


class TestTotality:
    def test_a_real_verdict_reaches_the_replanner(self):
        assert replan_inputs(
            _verdict(failure_class="test", short_summary="2 tests failed")
        ) == ("test", "2 tests failed")

    def test_no_verdict_is_the_honest_empty_state(self):
        """First GENERATE attempt: nothing has validated yet."""
        assert replan_inputs(None) == ("", "")

    def test_a_crashed_evaluator_is_indistinguishable_and_deliberately_so(self):
        """Both mean 'no verdict exists'. Inventing a third state would claim
        knowledge about WHY that this reader does not have."""
        assert replan_inputs(None) == ("", "")

    def test_falsy_fields_never_become_the_string_None(self):
        """A `None` failure_class must not put "None" into a prompt."""
        assert replan_inputs(_verdict()) == ("", "")

    def test_a_hostile_object_degrades_instead_of_raising(self):
        class _Hostile:
            @property
            def failure_class(self):
                raise RuntimeError("boom")

        assert replan_inputs(_Hostile()) == ("", "")

    def test_a_duck_typed_verdict_is_accepted(self):
        """Tests and any future verdict type work without inheritance."""
        class _Duck:
            failure_class = "build"
            short_summary = "compile error"
        assert replan_inputs(_Duck()) == ("build", "compile error")

    def test_an_oversized_summary_is_truncated(self):
        """The field's own comment promises <=300 chars; nothing enforced it,
        and this string reaches a prompt."""
        _, em = replan_inputs(_verdict(short_summary="x" * 5000))
        assert len(em) == REPLAN_SUMMARY_MAX_CHARS

    def test_non_string_fields_are_coerced(self):
        class _Enum:
            def __str__(self): return "infra"
        class _Odd:
            failure_class = _Enum()
            short_summary = 12345
        assert replan_inputs(_Odd()) == ("infra", "12345")

    def test_whitespace_only_summary_normalises_to_empty(self):
        assert replan_inputs(_verdict(short_summary="   \n ")) == ("", "")


class TestTheVerdictCanActuallyReachGenerate:
    def test_advance_carries_validation_forward(self):
        """The load-bearing assumption: a verdict set at VALIDATE survives
        into the next GENERATE attempt. If `advance()` ever stopped carrying
        fields forward, the wiring below would silently go empty again."""
        import dataclasses
        from backend.core.ouroboros.governance.op_context import OperationContext
        src = inspect.getsource(OperationContext.advance)
        assert "dataclasses.replace" in src or "replace(" in src, (
            "advance() must carry unspecified fields forward, or a verdict "
            "set at VALIDATE cannot reach a GENERATE retry"
        )
        assert dataclasses is not None

    def test_validate_runner_publishes_the_verdict_onto_the_context(self):
        path = (Path(__file__).resolve().parents[2]
                / "backend/core/ouroboros/governance/phase_runners"
                / "validate_runner.py")
        src = path.read_text(encoding="utf-8")
        assert "validation=best_validation" in src, (
            "VALIDATE must publish the verdict onto the context, or GENERATE "
            "has nothing to read"
        )


class TestBothPathsUseTheOneReader:
    """The drift that caused this: two call sites, one of them wrong."""

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
        "backend/core/ouroboros/governance/orchestrator.py",
    ])
    def test_call_site_uses_replan_inputs(self, rel):
        src = (Path(__file__).resolve().parents[2] / rel).read_text(
            encoding="utf-8")
        assert "_replan_inputs(" in src, f"{rel} must use the shared reader"

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/governance/phase_runners/generate_runner.py",
        "backend/core/ouroboros/governance/orchestrator.py",
    ])
    def test_the_dir_guard_is_gone(self, rel):
        """AST, not substring.

        A substring check flagged the COMMENT that documents the removed
        guard — a detector that cannot tell code from prose about code. The
        same correction the lint gate needed when it mistook any line with a
        string literal for a quoted annotation."""
        tree = ast.parse(
            (Path(__file__).resolve().parents[2] / rel).read_text(
                encoding="utf-8")
        )
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            if not any(isinstance(op, ast.In) for op in node.ops):
                continue
            for cmp_to in node.comparators:
                if (isinstance(cmp_to, ast.Call)
                        and isinstance(cmp_to.func, ast.Name)
                        and cmp_to.func.id == "dir"):
                    offenders.append(getattr(node, "lineno", "?"))
        assert not offenders, (
            f"{rel} still interrogates the scope with `in dir()` at lines "
            f"{offenders} instead of reading the context — the idiom no "
            "static tool can verify"
        )

    def test_generate_runner_has_no_undefined_names(self):
        pytest.importorskip("pyflakes")
        import sys
        root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(root))
        from ci.lint_gate import undefined_names
        path = (root / "backend/core/ouroboros/governance/phase_runners"
                / "generate_runner.py")
        assert not [f for f in undefined_names(path) if not f.inert]
