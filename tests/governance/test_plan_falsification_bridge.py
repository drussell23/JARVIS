"""PlanFalsificationDetector Slice 4 — orchestrator bridge regression spine.

Slice 4 ships the orchestrator wire-up: a proactive structural
detector call that runs at the GENERATE retry loop's reactive
replan site and preempts the legacy ``DynamicRePlanner`` regex
table when the structural detector trips REPLAN_TRIGGERED.

Coverage:
  * ``bridge_enabled`` / ``prompt_inject_enabled`` asymmetric env
    semantics
  * ``_feedback_max_chars`` env knob clamp (floor 200 / ceiling
    4000)
  * ``extract_hypotheses_from_plan_json`` — happy path, garbage
    JSON, missing field, non-list ordered_changes, non-dict
    entries, expected_outcome thread-through
  * ``build_evidence_from_validation`` — closed failure_class →
    FalsificationKind mapping; unknown class → empty tuple
    (no fabricated evidence); detail char cap
  * ``render_falsification_feedback`` — REPLAN_TRIGGERED renders
    block; other outcomes return empty; ASCII-only output;
    char cap honored; matched hypothesis info woven in
  * ``bridge_to_replan`` async one-shot — bridge off → DISABLED +
    empty; master off → detector returns DISABLED + empty;
    REPLAN_TRIGGERED + inject on → non-empty feedback;
    REPLAN_TRIGGERED + inject off → DISABLED feedback (shadow
    mode); CancelledError propagates; combined fs probe + upstream
    evidence; defensive degradation on corrupt plan_json
  * Authority allowlist — only Slice 1 + Slice 2 imports allowed;
    no exec/eval/compile; no orchestrator/iron_gate/etc imports
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import unittest.mock as mock

import pytest

from backend.core.ouroboros.governance.plan_falsification import (
    EvidenceItem,
    FalsificationKind,
    FalsificationOutcome,
    FalsificationVerdict,
    PlanStepHypothesis,
)
from backend.core.ouroboros.governance.plan_falsification_orchestrator_bridge import (
    PLAN_FALSIFICATION_BRIDGE_SCHEMA_VERSION,
    _feedback_max_chars,
    bridge_enabled,
    bridge_to_replan,
    build_evidence_from_validation,
    extract_hypotheses_from_plan_json,
    prompt_inject_enabled,
    render_falsification_feedback,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_PLAN_FALSIFICATION_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED",
        "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS",
        "JARVIS_PLAN_FALSIFICATION_FS_PROBE_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def repo(tmp_path) -> pathlib.Path:
    p = tmp_path / "repo"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert (
            PLAN_FALSIFICATION_BRIDGE_SCHEMA_VERSION
            == "plan_falsification_bridge.1"
        )


# ---------------------------------------------------------------------------
# Sub-flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestSubFlags:
    def test_bridge_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", raising=False,
        )
        assert bridge_enabled() is True

    def test_bridge_empty_treated_as_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", "",
        )
        assert bridge_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "On"])
    def test_bridge_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", raw,
        )
        assert bridge_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "garbage"])
    def test_bridge_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", raw,
        )
        assert bridge_enabled() is False

    def test_inject_default_true(self, monkeypatch):
        assert prompt_inject_enabled() is True

    def test_inject_falsy(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED", "false",
        )
        assert prompt_inject_enabled() is False


class TestFeedbackMaxChars:
    def test_default(self, monkeypatch):
        assert _feedback_max_chars() == 1500

    def test_explicit(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "2200",
        )
        assert _feedback_max_chars() == 2200

    def test_floor_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "10",
        )
        assert _feedback_max_chars() == 200

    def test_ceiling_clamp(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "10000",
        )
        assert _feedback_max_chars() == 4000

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "abc",
        )
        assert _feedback_max_chars() == 1500


# ---------------------------------------------------------------------------
# extract_hypotheses_from_plan_json
# ---------------------------------------------------------------------------


class TestExtractHypotheses:
    def test_happy_path(self):
        plan = (
            '{"schema_version":"plan.1","ordered_changes":['
            '{"file_path":"a.py","change_type":"modify",'
            '"expected_outcome":"a defines foo()"},'
            '{"file_path":"b.py","change_type":"create"}'
            ']}'
        )
        out = extract_hypotheses_from_plan_json(plan)
        assert len(out) == 2
        assert all(isinstance(h, PlanStepHypothesis) for h in out)
        assert out[0].file_path == "a.py"
        assert out[0].step_index == 0
        assert out[0].expected_outcome == "a defines foo()"
        assert out[1].file_path == "b.py"
        assert out[1].step_index == 1

    def test_empty_string_returns_empty(self):
        assert extract_hypotheses_from_plan_json("") == ()

    def test_none_returns_empty(self):
        assert extract_hypotheses_from_plan_json(None) == ()  # type: ignore[arg-type]

    def test_garbage_json_returns_empty(self):
        assert extract_hypotheses_from_plan_json("{not json") == ()

    def test_non_dict_top_level_returns_empty(self):
        assert extract_hypotheses_from_plan_json("[1,2,3]") == ()

    def test_missing_ordered_changes_returns_empty(self):
        assert extract_hypotheses_from_plan_json('{"foo":1}') == ()

    def test_non_list_ordered_changes_returns_empty(self):
        assert extract_hypotheses_from_plan_json(
            '{"ordered_changes":"oops"}',
        ) == ()

    def test_non_dict_entries_silently_dropped(self):
        plan = (
            '{"ordered_changes":['
            '{"file_path":"a.py"},'
            '"not a dict",'
            'null,'
            '{"file_path":"b.py"}'
            ']}'
        )
        out = extract_hypotheses_from_plan_json(plan)
        assert len(out) == 2
        assert {h.file_path for h in out} == {"a.py", "b.py"}

    def test_expected_outcome_optional(self):
        plan = '{"ordered_changes":[{"file_path":"a.py"}]}'
        out = extract_hypotheses_from_plan_json(plan)
        assert out[0].expected_outcome == ""


# ---------------------------------------------------------------------------
# build_evidence_from_validation
# ---------------------------------------------------------------------------


class TestBuildEvidence:
    @pytest.mark.parametrize(
        "fc, expected",
        [
            ("test", FalsificationKind.VERIFY_REJECTED),
            ("build", FalsificationKind.VERIFY_REJECTED),
            ("verify", FalsificationKind.VERIFY_REJECTED),
            ("validation", FalsificationKind.VERIFY_REJECTED),
            ("repair", FalsificationKind.REPAIR_STUCK),
            # Case insensitive + whitespace tolerated
            ("  TEST  ", FalsificationKind.VERIFY_REJECTED),
        ],
    )
    def test_known_classes_mapped(self, fc, expected):
        out = build_evidence_from_validation(
            failure_class=fc,
            short_summary="3 tests failed",
        )
        assert len(out) == 1
        assert out[0].kind is expected
        assert out[0].source.startswith(
            "plan_falsification_bridge"
        )

    @pytest.mark.parametrize("fc", ["infra", "budget", "unknown", ""])
    def test_unknown_class_no_evidence(self, fc):
        out = build_evidence_from_validation(
            failure_class=fc,
            short_summary="something",
        )
        assert out == ()

    def test_none_failure_class_no_evidence(self):
        out = build_evidence_from_validation(
            failure_class=None,
            short_summary="x",
        )
        assert out == ()

    def test_target_file_anchor_used(self):
        out = build_evidence_from_validation(
            failure_class="test",
            short_summary="...",
            target_files=("auth.py", "other.py"),
        )
        assert out[0].target_file_path == "auth.py"

    def test_no_target_files_anchor_empty(self):
        out = build_evidence_from_validation(
            failure_class="test",
            short_summary="...",
            target_files=(),
        )
        assert out[0].target_file_path == ""

    def test_summary_truncated_to_500(self):
        out = build_evidence_from_validation(
            failure_class="test",
            short_summary="x" * 1000,
        )
        assert len(out[0].detail) <= 500


# ---------------------------------------------------------------------------
# render_falsification_feedback
# ---------------------------------------------------------------------------


class TestRenderFeedback:
    def _make_replan_verdict(self) -> FalsificationVerdict:
        return FalsificationVerdict(
            outcome=FalsificationOutcome.REPLAN_TRIGGERED,
            falsified_step_index=2,
            falsifying_evidence_kinds=("file_missing", "verify_rejected"),
            contradicting_detail="auth.py not on disk",
            total_hypotheses=3,
            total_evidence=2,
            monotonic_tightening_verdict="passed",
        )

    def test_replan_renders_block(self):
        out = render_falsification_feedback(self._make_replan_verdict())
        assert "Plan Falsification" in out
        assert "Step #2" in out
        assert "file_missing" in out
        assert "verify_rejected" in out
        assert "auth.py not on disk" in out

    def test_replan_includes_hypothesis_context(self):
        verdict = self._make_replan_verdict()
        hyps = (
            PlanStepHypothesis(
                step_index=2,
                file_path="auth.py",
                change_type="modify",
                expected_outcome="auth.py defines login()",
            ),
        )
        out = render_falsification_feedback(
            verdict, plan_hypotheses=hyps,
        )
        assert "auth.py" in out
        assert "modify" in out
        assert "auth.py defines login()" in out

    def test_no_falsification_returns_empty(self):
        verdict = FalsificationVerdict(
            outcome=FalsificationOutcome.NO_FALSIFICATION,
        )
        assert render_falsification_feedback(verdict) == ""

    def test_disabled_returns_empty(self):
        verdict = FalsificationVerdict(
            outcome=FalsificationOutcome.DISABLED,
        )
        assert render_falsification_feedback(verdict) == ""

    def test_failed_returns_empty(self):
        verdict = FalsificationVerdict(
            outcome=FalsificationOutcome.FAILED,
        )
        assert render_falsification_feedback(verdict) == ""

    def test_insufficient_returns_empty(self):
        verdict = FalsificationVerdict(
            outcome=FalsificationOutcome.INSUFFICIENT_EVIDENCE,
        )
        assert render_falsification_feedback(verdict) == ""

    def test_non_verdict_input_returns_empty(self):
        assert render_falsification_feedback("garbage") == ""  # type: ignore[arg-type]
        assert render_falsification_feedback(None) == ""  # type: ignore[arg-type]

    def test_output_ascii_only(self):
        verdict = self._make_replan_verdict()
        out = render_falsification_feedback(verdict)
        # Iron Gate enforces strict ASCII; the bridge must too.
        out.encode("ascii")  # raises if non-ASCII

    def test_char_cap_honored(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_FEEDBACK_MAX_CHARS", "200",
        )
        verdict = FalsificationVerdict(
            outcome=FalsificationOutcome.REPLAN_TRIGGERED,
            falsified_step_index=0,
            falsifying_evidence_kinds=("file_missing",),
            contradicting_detail="x" * 1000,
            total_hypotheses=1,
            total_evidence=1,
            monotonic_tightening_verdict="passed",
        )
        out = render_falsification_feedback(verdict)
        assert len(out) <= 200


# ---------------------------------------------------------------------------
# bridge_to_replan async one-shot
# ---------------------------------------------------------------------------


class TestBridgeToReplan:
    @pytest.mark.asyncio
    async def test_bridge_disabled_returns_disabled_empty(self, repo):
        verdict, text = await bridge_to_replan(
            plan_json='{"ordered_changes":[{"file_path":"missing.py",'
                      '"change_type":"modify"}]}',
            project_root=repo,
            enabled=False,
        )
        assert verdict.outcome is FalsificationOutcome.DISABLED
        assert text == ""

    @pytest.mark.asyncio
    async def test_bridge_disabled_via_env(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_BRIDGE_ENABLED", "false",
        )
        verdict, text = await bridge_to_replan(
            plan_json='{"ordered_changes":[{"file_path":"missing.py",'
                      '"change_type":"modify"}]}',
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.DISABLED
        assert text == ""

    @pytest.mark.asyncio
    async def test_master_off_returns_disabled(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "false",
        )
        verdict, text = await bridge_to_replan(
            plan_json='{"ordered_changes":[{"file_path":"missing.py",'
                      '"change_type":"modify"}]}',
            project_root=repo,
        )
        # Bridge is on, but detector master is off → DISABLED.
        assert verdict.outcome is FalsificationOutcome.DISABLED
        assert text == ""

    @pytest.mark.asyncio
    async def test_replan_triggered_emits_feedback(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        plan = (
            '{"schema_version":"plan.1","ordered_changes":['
            '{"file_path":"missing.py","change_type":"modify",'
            '"expected_outcome":"missing.py exports foo()"}'
            ']}'
        )
        verdict, text = await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert text  # non-empty
        assert "missing.py" in text
        assert "missing.py exports foo()" in text

    @pytest.mark.asyncio
    async def test_inject_off_keeps_verdict_drops_feedback(
        self, repo, monkeypatch,
    ):
        """Shadow mode: detector still RUNS (verdict surfaces in
        observability), but no prompt injection."""
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        plan = (
            '{"ordered_changes":[{"file_path":"missing.py",'
            '"change_type":"modify"}]}'
        )
        verdict, text = await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
            inject_prompt=False,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert text == ""

    @pytest.mark.asyncio
    async def test_inject_off_via_env(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_PROMPT_INJECT_ENABLED", "false",
        )
        plan = (
            '{"ordered_changes":[{"file_path":"missing.py",'
            '"change_type":"modify"}]}'
        )
        verdict, text = await bridge_to_replan(
            plan_json=plan,
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert text == ""

    @pytest.mark.asyncio
    async def test_combined_fs_probe_and_validation_evidence(
        self, repo, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        plan = (
            '{"ordered_changes":[{"file_path":"missing.py",'
            '"change_type":"modify"}]}'
        )
        verdict, text = await bridge_to_replan(
            plan_json=plan,
            validation_failure_class="test",
            validation_short_summary="3 tests failed in module",
            target_files=("missing.py",),
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        kinds = set(verdict.falsifying_evidence_kinds)
        assert "file_missing" in kinds  # from fs probe
        assert "verify_rejected" in kinds  # from validation evidence

    @pytest.mark.asyncio
    async def test_corrupt_plan_json_falls_through(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        # No upstream evidence, no usable hypotheses.
        verdict, text = await bridge_to_replan(
            plan_json="{not json",
            project_root=repo,
        )
        # Empty hypotheses → INSUFFICIENT_EVIDENCE → no prompt block.
        assert verdict.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE
        assert text == ""

    @pytest.mark.asyncio
    async def test_empty_plan_json_falls_through(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        verdict, text = await bridge_to_replan(
            plan_json="",
            project_root=repo,
        )
        assert verdict.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE
        assert text == ""

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )

        async def _cancelling_detect(*_a, **_kw):
            raise asyncio.CancelledError()

        with mock.patch(
            "backend.core.ouroboros.governance."
            "plan_falsification_orchestrator_bridge.detect_falsification",
            _cancelling_detect,
        ):
            with pytest.raises(asyncio.CancelledError):
                await bridge_to_replan(
                    plan_json='{"ordered_changes":[{"file_path":"x.py"}]}',
                    project_root=repo,
                )

    @pytest.mark.asyncio
    async def test_returns_verdict_dataclass_always(self, repo, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", "true",
        )
        verdict, _ = await bridge_to_replan(
            plan_json="", project_root=repo,
        )
        assert isinstance(verdict, FalsificationVerdict)


# ---------------------------------------------------------------------------
# Authority allowlist — Slice 5 will pin formally
# ---------------------------------------------------------------------------


_BRIDGE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "plan_falsification_orchestrator_bridge.py"
)


_FORBIDDEN_GOVERNANCE_MODULES = {
    "orchestrator",
    "phase_runner",
    "iron_gate",
    "change_engine",
    "candidate_generator",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
    "semantic_guardian",
    "semantic_firewall",
    "risk_engine",
}


_ALLOWED_GOVERNANCE_MODULES = {
    "plan_falsification",  # Slice 1 primitive
    "plan_falsification_detector",  # Slice 2 async detector
}


class TestAuthorityInvariants:
    @staticmethod
    def _source() -> str:
        return _BRIDGE_PATH.read_text()

    def test_only_slice_1_and_2_governance_imports_allowed(self):
        source = self._source()
        tree = ast.parse(source)
        registration_funcs = {
            "register_flags", "register_shipped_invariants",
        }
        exempt_ranges = []
        for fnode in ast.walk(tree):
            if isinstance(fnode, ast.FunctionDef):
                if fnode.name in registration_funcs:
                    start = getattr(fnode, "lineno", 0)
                    end = getattr(fnode, "end_lineno", start) or start
                    exempt_ranges.append((start, end))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "backend." not in module and "governance" not in module:
                    continue
                lineno = getattr(node, "lineno", 0)
                if any(s <= lineno <= e for s, e in exempt_ranges):
                    continue
                tail = module.rsplit(".", 1)[-1]
                if tail in _FORBIDDEN_GOVERNANCE_MODULES:
                    raise AssertionError(
                        f"Slice 4 must not import forbidden module "
                        f"{module!r} at line {lineno}"
                    )
                if tail not in _ALLOWED_GOVERNANCE_MODULES:
                    raise AssertionError(
                        f"Slice 4 imports unexpected governance "
                        f"module {module!r} at line {lineno}; only "
                        f"{_ALLOWED_GOVERNANCE_MODULES} permitted"
                    )

    def test_no_exec_eval_compile_calls(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 4 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )

    def test_bridge_to_replan_is_async(self):
        source = self._source()
        tree = ast.parse(source)
        async_names = {
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        }
        assert "bridge_to_replan" in async_names

    def test_public_surface_exported(self):
        from backend.core.ouroboros.governance import (
            plan_falsification_orchestrator_bridge as mod,
        )
        for name in (
            "PLAN_FALSIFICATION_BRIDGE_SCHEMA_VERSION",
            "bridge_enabled",
            "bridge_to_replan",
            "build_evidence_from_validation",
            "extract_hypotheses_from_plan_json",
            "prompt_inject_enabled",
            "render_falsification_feedback",
        ):
            assert name in mod.__all__, f"{name} missing from __all__"


# ---------------------------------------------------------------------------
# Orchestrator wire-up: structural detector preempts reactive path
# ---------------------------------------------------------------------------


_ORCH_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "orchestrator.py"
)


class TestOrchestratorWireUp:
    """The wire-up site is enormous (102K-line file); we verify the
    structural shape via AST grep rather than full integration."""

    @staticmethod
    def _source() -> str:
        return _ORCH_PATH.read_text()

    def test_bridge_imported_at_replan_site(self):
        src = self._source()
        # The bridge must be imported via lazy import (inside
        # function body). Verify the import string appears.
        assert (
            "plan_falsification_orchestrator_bridge"
            in src
        )
        assert "bridge_to_replan" in src

    def test_bridge_runs_before_dynamic_replanner(self):
        """Stage 1 (structural) must precede Stage 2 (regex). The
        order is load-bearing — preemption requires that the
        structural detector's verdict be available when the legacy
        path's `if not _replan_text` check runs."""
        src = self._source()
        bridge_idx = src.find("_falsification_bridge")
        legacy_idx = src.find(
            "DynamicRePlanner.suggest_replan",
        )
        assert bridge_idx > 0
        assert legacy_idx > 0
        assert bridge_idx < legacy_idx, (
            "structural detector must run before regex backstop"
        )

    def test_legacy_path_gated_on_empty_replan_text(self):
        """The legacy DynamicRePlanner call must be inside an
        `if not _replan_text:` guard — otherwise it would run
        unconditionally and overwrite the structural feedback."""
        src = self._source()
        # Slice the orchestrator around the wire-up site.
        bridge_idx = src.find("_falsification_bridge")
        # Look ahead 4000 chars for the guard.
        window = src[bridge_idx:bridge_idx + 4000]
        assert "if not _replan_text" in window
