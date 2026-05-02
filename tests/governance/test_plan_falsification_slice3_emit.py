"""PlanFalsificationDetector Slice 3 — PlanGenerator hypothesis-emit
extension regression spine.

Slice 3 extends the existing ``plan.1`` schema additively with a
per-change ``expected_outcome`` falsifiable predicate, teaches the
model what that means via the prompt, parses + preserves the field,
and materializes :class:`PlanStepHypothesis` instances via the
new ``PlanResult.to_plan_step_hypotheses()`` method (which delegates
to Slice 1's ``pair_plan_step_with_hypothesis`` — zero duplication).

Coverage:
  * ``_plan_hypothesis_emit_enabled`` asymmetric env semantics
    (default true)
  * Schema + prompt: ``expected_outcome`` field present in the
    ``_plan_schema_instruction`` output + falsifiable-predicate
    rule appears
  * Parser: ``expected_outcome`` preserved when sub-flag on,
    silently dropped when off (legacy shape preserved)
  * Parser: missing ``expected_outcome`` defaults to "" (older
    models that don't populate it)
  * ``PlanResult.to_plan_step_hypotheses`` returns Slice 1
    dataclasses, ordered by step_index, with expected_outcome
    threaded through
  * Skip cases: planning skipped → empty tuple; no changes →
    empty tuple; sub-flag off → empty tuple
  * Garbage entries (non-dict in ordered_changes) silently
    dropped
  * ``to_prompt_section`` renders the ``Expected outcome:`` line
    when present and omits it when absent (no empty leftover line)
  * Detector + emitter end-to-end: PlanResult → hypotheses →
    detect_falsification(missing file) → REPLAN_TRIGGERED
"""
from __future__ import annotations

import json
import pathlib

import pytest

from backend.core.ouroboros.governance.plan_falsification import (
    FalsificationOutcome,
    PlanStepHypothesis,
)
from backend.core.ouroboros.governance.plan_falsification_detector import (
    detect_falsification,
)
from backend.core.ouroboros.governance.plan_generator import (
    PlanGenerator,
    PlanResult,
    _plan_hypothesis_emit_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Sub-flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestEmitFlag:
    def test_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", raising=False,
        )
        assert _plan_hypothesis_emit_enabled() is True

    def test_empty_treated_as_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "")
        assert _plan_hypothesis_emit_enabled() is True

    def test_whitespace_treated_as_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "   ",
        )
        assert _plan_hypothesis_emit_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["1", "true", "TRUE", "yes", "On"],
    )
    def test_explicit_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", raw)
        assert _plan_hypothesis_emit_enabled() is True

    @pytest.mark.parametrize(
        "raw", ["0", "false", "FALSE", "no", "off", "garbage"],
    )
    def test_explicit_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", raw)
        assert _plan_hypothesis_emit_enabled() is False


# ---------------------------------------------------------------------------
# Prompt: schema instruction teaches the model
# ---------------------------------------------------------------------------


class TestPromptSchema:
    def test_schema_instruction_includes_expected_outcome(self):
        prompt = PlanGenerator._plan_schema_instruction()
        assert "expected_outcome" in prompt

    def test_schema_instruction_explains_falsifiable_predicate(self):
        prompt = PlanGenerator._plan_schema_instruction()
        # The rules section must call out falsifiability so the
        # model knows what to write — no hand-wave.
        assert "falsifiable predicate" in prompt.lower()

    def test_schema_instruction_includes_examples(self):
        prompt = PlanGenerator._plan_schema_instruction()
        # At least one positive + one negative example in the rules.
        assert "Good predicates" in prompt
        assert "Bad predicates" in prompt

    def test_schema_instruction_unchanged_legacy_fields_present(self):
        # Additive — none of the existing fields removed.
        prompt = PlanGenerator._plan_schema_instruction()
        for legacy in (
            "file_path", "change_type", "description",
            "dependencies", "estimated_scope",
            "risk_factors", "test_strategy", "architectural_notes",
        ):
            assert legacy in prompt, f"legacy field {legacy} missing"


# ---------------------------------------------------------------------------
# Parser: preserves / drops / defaults
# ---------------------------------------------------------------------------


def _make_generator() -> PlanGenerator:
    """Construct a PlanGenerator. We only exercise its parser, so
    the generator dependency can be a stub."""
    class _StubGen:
        async def plan(self, prompt, deadline):  # pragma: no cover
            return ""
    return PlanGenerator(generator=_StubGen(), repo_root=pathlib.Path("/tmp"))


class TestParserPreservesField:
    def _parse(self, payload: dict) -> PlanResult:
        return _make_generator()._parse_plan_response(json.dumps(payload))

    def test_preserved_when_emit_on(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "true",
        )
        result = self._parse({
            "schema_version": "plan.1",
            "approach": "x",
            "complexity": "moderate",
            "ordered_changes": [{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "wire login",
                "expected_outcome": "auth.py defines login(req) -> bool",
            }],
        })
        assert result.ordered_changes[0]["expected_outcome"] == (
            "auth.py defines login(req) -> bool"
        )

    def test_dropped_when_emit_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "false",
        )
        result = self._parse({
            "schema_version": "plan.1",
            "approach": "x",
            "complexity": "moderate",
            "ordered_changes": [{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "wire login",
                "expected_outcome": "should not be in parsed result",
            }],
        })
        # Legacy shape — field gone.
        assert "expected_outcome" not in result.ordered_changes[0]

    def test_safe_default_when_model_omits(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "true",
        )
        # Older model that doesn't yet emit the field.
        result = self._parse({
            "schema_version": "plan.1",
            "approach": "x",
            "complexity": "moderate",
            "ordered_changes": [{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "wire login",
            }],
        })
        assert result.ordered_changes[0]["expected_outcome"] == ""

    def test_non_string_coerced_to_string(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "true",
        )
        result = self._parse({
            "schema_version": "plan.1",
            "approach": "x",
            "complexity": "moderate",
            "ordered_changes": [{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "x",
                "expected_outcome": 42,  # type: ignore[dict-item]
            }],
        })
        # Defensive coercion — the field is always a string.
        assert isinstance(
            result.ordered_changes[0]["expected_outcome"], str,
        )

    def test_legacy_fields_unchanged_when_emit_on(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "true",
        )
        result = self._parse({
            "schema_version": "plan.1",
            "approach": "x",
            "complexity": "moderate",
            "ordered_changes": [{
                "file_path": "auth.py",
                "change_type": "create",
                "description": "wire login",
                "dependencies": ["base.py"],
                "estimated_scope": "small",
                "expected_outcome": "auth.py exists",
            }],
        })
        change = result.ordered_changes[0]
        assert change["file_path"] == "auth.py"
        assert change["change_type"] == "create"
        assert change["description"] == "wire login"
        assert change["dependencies"] == ["base.py"]
        assert change["estimated_scope"] == "small"


# ---------------------------------------------------------------------------
# PlanResult.to_plan_step_hypotheses materializes Slice 1 dataclasses
# ---------------------------------------------------------------------------


class TestToPlanStepHypotheses:
    def test_materializes_one_per_change(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[
                {
                    "file_path": "a.py",
                    "change_type": "modify",
                    "description": "...",
                    "expected_outcome": "a defines foo()",
                },
                {
                    "file_path": "b.py",
                    "change_type": "create",
                    "description": "...",
                    "expected_outcome": "b is created",
                },
            ],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert len(hyps) == 2
        assert all(isinstance(h, PlanStepHypothesis) for h in hyps)

    def test_step_index_from_position(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[
                {"file_path": "a.py", "change_type": "modify"},
                {"file_path": "b.py", "change_type": "modify"},
                {"file_path": "c.py", "change_type": "modify"},
            ],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert [h.step_index for h in hyps] == [0, 1, 2]
        assert [h.file_path for h in hyps] == ["a.py", "b.py", "c.py"]

    def test_threads_expected_outcome(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[{
                "file_path": "a.py",
                "change_type": "modify",
                "expected_outcome": "a defines foo() returning bool",
            }],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert hyps[0].expected_outcome == (
            "a defines foo() returning bool"
        )

    def test_threads_change_type(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[{
                "file_path": "new.py",
                "change_type": "create",
            }],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert hyps[0].change_type == "create"

    def test_skipped_plan_returns_empty(self):
        result = PlanResult.skipped_result("trivial_op")
        assert result.to_plan_step_hypotheses(emit_enabled=True) == ()

    def test_no_changes_returns_empty(self):
        result = PlanResult(approach="x", ordered_changes=[])
        assert result.to_plan_step_hypotheses(emit_enabled=True) == ()

    def test_emit_off_returns_empty(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[{"file_path": "a.py"}],
        )
        assert result.to_plan_step_hypotheses(emit_enabled=False) == ()

    def test_emit_off_via_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "false",
        )
        result = PlanResult(
            approach="x",
            ordered_changes=[{"file_path": "a.py"}],
        )
        # No emit_enabled kwarg → reads env.
        assert result.to_plan_step_hypotheses() == ()

    def test_emit_on_via_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_HYPOTHESIS_EMIT_ENABLED", "true",
        )
        result = PlanResult(
            approach="x",
            ordered_changes=[{"file_path": "a.py"}],
        )
        out = result.to_plan_step_hypotheses()
        assert len(out) == 1

    def test_garbage_entries_silently_dropped(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[
                {"file_path": "a.py"},
                "not a dict",  # type: ignore[list-item]
                None,  # type: ignore[list-item]
                {"file_path": "b.py"},
            ],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert len(hyps) == 2
        assert {h.file_path for h in hyps} == {"a.py", "b.py"}

    def test_returns_tuple_not_list(self):
        result = PlanResult(
            approach="x",
            ordered_changes=[{"file_path": "a.py"}],
        )
        assert isinstance(
            result.to_plan_step_hypotheses(emit_enabled=True), tuple,
        )

    def test_uses_slice1_convenience_constructor(self):
        """to_plan_step_hypotheses must delegate to Slice 1's
        pair_plan_step_with_hypothesis — guard against duplication."""
        # Simplest proof: the result type IS PlanStepHypothesis, and
        # the field it would construct (hypothesis_id) defaults to
        # "" via Slice 1's constructor (we don't pass it).
        result = PlanResult(
            approach="x",
            ordered_changes=[{"file_path": "a.py"}],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        assert hyps[0].hypothesis_id == ""


# ---------------------------------------------------------------------------
# to_prompt_section renders the Expected outcome line conditionally
# ---------------------------------------------------------------------------


class TestPromptSectionRender:
    def test_renders_expected_outcome_when_present(self):
        result = PlanResult(
            approach="wire login",
            ordered_changes=[{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "add login",
                "expected_outcome": "auth.py exports login()",
            }],
        )
        section = result.to_prompt_section()
        assert "Expected outcome: auth.py exports login()" in section

    def test_omits_line_when_absent(self):
        result = PlanResult(
            approach="wire login",
            ordered_changes=[{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "add login",
            }],
        )
        section = result.to_prompt_section()
        assert "Expected outcome:" not in section

    def test_omits_line_when_blank(self):
        result = PlanResult(
            approach="wire login",
            ordered_changes=[{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "add login",
                "expected_outcome": "   ",
            }],
        )
        section = result.to_prompt_section()
        assert "Expected outcome:" not in section


# ---------------------------------------------------------------------------
# End-to-end: PlanResult → hypotheses → detector
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_plan_with_missing_file_drives_replan(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        # File "missing.py" does NOT exist.
        result = PlanResult(
            approach="rewrite auth",
            ordered_changes=[{
                "file_path": "missing.py",
                "change_type": "modify",
                "description": "fix login",
                "expected_outcome": "missing.py contains login()",
            }],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        verdict = await detect_falsification(
            hyps, project_root=repo, enabled=True,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert verdict.falsified_step_index == 0

    @pytest.mark.asyncio
    async def test_plan_with_existing_file_no_falsification(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "auth.py").write_text("def login(): return True\n")
        result = PlanResult(
            approach="rewrite auth",
            ordered_changes=[{
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "fix login",
                "expected_outcome": "auth.py contains login()",
            }],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        verdict = await detect_falsification(
            hyps, project_root=repo, enabled=True,
        )
        # File exists → no fs probe miss → no upstream evidence
        # → INSUFFICIENT_EVIDENCE (not REPLAN).
        assert verdict.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    @pytest.mark.asyncio
    async def test_create_change_type_not_falsified_when_missing(
        self, tmp_path,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        # Plan creates new.py — file should NOT exist beforehand.
        result = PlanResult(
            approach="create new module",
            ordered_changes=[{
                "file_path": "new.py",
                "change_type": "create",
                "description": "scaffold",
                "expected_outcome": "new.py is created",
            }],
        )
        hyps = result.to_plan_step_hypotheses(emit_enabled=True)
        verdict = await detect_falsification(
            hyps, project_root=repo, enabled=True,
        )
        # Probe correctly skips create change_types — no false-positive.
        assert verdict.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE
