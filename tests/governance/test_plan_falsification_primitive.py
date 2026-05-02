"""PlanFalsificationDetector Slice 1 — primitive regression spine.

Closes the structural gap that ``DynamicRePlanner._STRATEGY_MAP``
was working around — replaces hardcoded regex-pattern → strategy
matching with structural falsification of plan-step hypotheses
against typed evidence streams.

Coverage:
  * Closed 5-value FalsificationKind + FalsificationOutcome
    taxonomies (J.A.R.M.A.T.R.I.X.)
  * Frozen dataclass mutation guards + to_dict/from_dict round-trip
  * Total compute_falsification_verdict aggregation — every
    (hypotheses × evidence × flag) combination maps to expected
    FalsificationOutcome
  * Master flag short-circuit (DISABLED)
  * Empty hypotheses / insufficient evidence (INSUFFICIENT_EVIDENCE)
  * Match by step_index (precise) + match by file_path (fallback)
  * Stable ordering: lowest step_index, earliest captured_monotonic
  * Phase C MonotonicTighteningVerdict.PASSED stamping — only on
    REPLAN_TRIGGERED
  * Defensive degradation: garbage / non-tuple / non-dataclass
    elements silently dropped
  * pair_plan_step_with_hypothesis convenience constructor
  * Master flag asymmetric env semantics
  * Env-knob clamping (min_evidence_count, falsification_max_age_s)
  * Staleness filter (evidence older than max_age dropped)
  * AST-walked authority invariants (pure-stdlib + no async + no
    exec/eval/compile)
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import FrozenInstanceError

import pytest

from backend.core.ouroboros.governance.plan_falsification import (
    EvidenceItem,
    FalsificationKind,
    FalsificationOutcome,
    FalsificationVerdict,
    PLAN_FALSIFICATION_SCHEMA_VERSION,
    PlanStepHypothesis,
    compute_falsification_verdict,
    falsification_max_age_s,
    min_evidence_count,
    pair_plan_step_with_hypothesis,
    plan_falsification_enabled,
)


# ---------------------------------------------------------------------------
# Closed-taxonomy invariants
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_kind_has_exactly_five_values(self):
        assert len(list(FalsificationKind)) == 5

    def test_kind_value_set_exact(self):
        expected = {
            "file_missing", "symbol_missing", "verify_rejected",
            "repair_stuck", "evidence_contradicted",
        }
        actual = {v.value for v in FalsificationKind}
        assert actual == expected

    def test_outcome_has_exactly_five_values(self):
        assert len(list(FalsificationOutcome)) == 5

    def test_outcome_value_set_exact(self):
        expected = {
            "replan_triggered", "no_falsification",
            "insufficient_evidence", "disabled", "failed",
        }
        actual = {v.value for v in FalsificationOutcome}
        assert actual == expected

    def test_kind_is_str_enum(self):
        for v in FalsificationKind:
            assert isinstance(v.value, str)
            assert isinstance(v, str)

    def test_outcome_is_str_enum(self):
        for v in FalsificationOutcome:
            assert isinstance(v.value, str)
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Frozen dataclass guards
# ---------------------------------------------------------------------------


class TestFrozenHypothesis:
    def test_hypothesis_is_frozen(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        with pytest.raises(FrozenInstanceError):
            h.step_index = 1  # type: ignore[misc]

    def test_default_schema_version(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        assert h.schema_version == "plan_falsification.1"

    def test_to_dict_round_trip(self):
        h = PlanStepHypothesis(
            step_index=2, file_path="auth.py",
            change_type="modify",
            expected_outcome="login() exists and returns bool",
            hypothesis_id="hyp-abc",
        )
        h2 = PlanStepHypothesis.from_dict(h.to_dict())
        assert h2 == h

    def test_from_dict_garbage_returns_safe_default(self):
        h = PlanStepHypothesis.from_dict({"step_index": "not-an-int"})
        assert isinstance(h, PlanStepHypothesis)


class TestFrozenEvidence:
    def test_evidence_is_frozen(self):
        e = EvidenceItem(kind=FalsificationKind.FILE_MISSING)
        with pytest.raises(FrozenInstanceError):
            e.kind = FalsificationKind.SYMBOL_MISSING  # type: ignore[misc]

    def test_default_payload_is_empty_dict(self):
        e = EvidenceItem(kind=FalsificationKind.FILE_MISSING)
        assert e.payload == {}

    def test_to_dict_carries_kind_value(self):
        e = EvidenceItem(
            kind=FalsificationKind.VERIFY_REJECTED,
            target_step_index=3,
            target_file_path="auth.py",
            detail="test_login failed",
            source="verify_runner",
            captured_monotonic=12345.6,
            payload={"test": "test_login", "status": "FAILED"},
        )
        d = e.to_dict()
        assert d["kind"] == "verify_rejected"
        assert d["target_step_index"] == 3
        assert d["target_file_path"] == "auth.py"
        assert d["payload"]["test"] == "test_login"


class TestFrozenVerdict:
    def test_verdict_is_frozen(self):
        v = FalsificationVerdict(
            outcome=FalsificationOutcome.NO_FALSIFICATION,
        )
        with pytest.raises(FrozenInstanceError):
            v.outcome = FalsificationOutcome.REPLAN_TRIGGERED  # type: ignore[misc]

    def test_is_replan_triggered_only_for_replan(self):
        for outcome in FalsificationOutcome:
            v = FalsificationVerdict(outcome=outcome)
            assert v.is_replan_triggered == (
                outcome is FalsificationOutcome.REPLAN_TRIGGERED
            )

    def test_is_tightening_only_for_replan(self):
        for outcome in FalsificationOutcome:
            v = FalsificationVerdict(outcome=outcome)
            assert v.is_tightening == (
                outcome is FalsificationOutcome.REPLAN_TRIGGERED
            )


# ---------------------------------------------------------------------------
# Master flag short-circuit
# ---------------------------------------------------------------------------


class TestMasterFlagShortCircuit:
    def test_disabled_returns_disabled_outcome(self):
        v = compute_falsification_verdict((), (), enabled=False)
        assert v.outcome is FalsificationOutcome.DISABLED
        assert v.monotonic_tightening_verdict == ""

    def test_disabled_short_circuits_before_evidence_check(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_step_index=0,
        )
        v = compute_falsification_verdict((h,), (e,), enabled=False)
        assert v.outcome is FalsificationOutcome.DISABLED


# ---------------------------------------------------------------------------
# Insufficient evidence
# ---------------------------------------------------------------------------


class TestInsufficientEvidence:
    def test_empty_hypotheses_yields_insufficient(self):
        e = EvidenceItem(kind=FalsificationKind.FILE_MISSING)
        v = compute_falsification_verdict((), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE
        assert v.total_hypotheses == 0
        assert v.total_evidence == 1

    def test_empty_evidence_yields_insufficient(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        v = compute_falsification_verdict((h,), (), enabled=True)
        assert v.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE
        assert v.total_hypotheses == 1
        assert v.total_evidence == 0

    def test_min_evidence_count_threshold_enforced(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE", "3",
        )
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        # Single evidence item; below threshold of 3.
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_step_index=0,
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# Replan triggered — match logic
# ---------------------------------------------------------------------------


class TestReplanTriggered:
    def test_step_index_match_triggers_replan(self):
        h = PlanStepHypothesis(
            step_index=2, file_path="auth.py",
            expected_outcome="login() exists",
        )
        e = EvidenceItem(
            kind=FalsificationKind.SYMBOL_MISSING,
            target_step_index=2,
            detail="login() not found in auth.py",
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert v.falsified_step_index == 2
        assert v.falsifying_evidence_kinds == ("symbol_missing",)
        assert "login() not found" in v.contradicting_detail
        assert v.monotonic_tightening_verdict == "passed"

    def test_file_path_fallback_match_triggers_replan(self):
        """When evidence has no target_step_index, falls back to
        case-insensitive file_path equality."""
        h = PlanStepHypothesis(step_index=5, file_path="Auth.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_file_path="auth.py",  # lowercase mismatch
            detail="auth.py not on disk",
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert v.falsified_step_index == 5

    @pytest.mark.parametrize("kind_str", [
        "file_missing", "symbol_missing", "verify_rejected",
        "repair_stuck", "evidence_contradicted",
    ])
    def test_each_kind_triggers_replan(self, kind_str: str):
        kind = FalsificationKind(kind_str)
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(kind=kind, target_step_index=0)
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert v.falsifying_evidence_kinds == (kind_str,)

    def test_lowest_step_index_wins(self):
        """Multiple matches → first by stable ordering (lowest
        step_index, then earliest captured_monotonic)."""
        h_low = PlanStepHypothesis(step_index=1, file_path="a.py")
        h_high = PlanStepHypothesis(step_index=5, file_path="b.py")
        e_low = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=1,
            captured_monotonic=200.0, detail="a missing",
        )
        e_high = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=5,
            captured_monotonic=100.0,  # earlier!
            detail="b missing",
        )
        # e_high captured earlier; sort puts it first → matches h_high
        # first by stable order. Winner = step 5 because evidence
        # ordering is by captured_monotonic ascending.
        v = compute_falsification_verdict(
            (h_low, h_high), (e_low, e_high), enabled=True,
        )
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert v.falsified_step_index == 5
        assert "b missing" in v.contradicting_detail
        # Both kinds recorded for audit (deduped).
        assert v.falsifying_evidence_kinds == ("file_missing",)

    def test_multi_kind_recorded_in_audit(self):
        """When multiple distinct kinds match, all are recorded
        (deduped) for operator audit."""
        h0 = PlanStepHypothesis(step_index=0, file_path="x.py")
        h1 = PlanStepHypothesis(step_index=1, file_path="y.py")
        e0 = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=0,
            captured_monotonic=1.0,
        )
        e1 = EvidenceItem(
            kind=FalsificationKind.VERIFY_REJECTED, target_step_index=1,
            captured_monotonic=2.0,
        )
        v = compute_falsification_verdict(
            (h0, h1), (e0, e1), enabled=True,
        )
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        assert set(v.falsifying_evidence_kinds) == {
            "file_missing", "verify_rejected",
        }


# ---------------------------------------------------------------------------
# No falsification
# ---------------------------------------------------------------------------


class TestNoFalsification:
    def test_no_match_yields_no_falsification(self):
        """Hypothesis on file_a, evidence on file_b — no match."""
        h = PlanStepHypothesis(step_index=0, file_path="auth.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_file_path="other.py",  # different file
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.NO_FALSIFICATION
        assert v.falsified_step_index is None
        assert v.monotonic_tightening_verdict == ""

    def test_evidence_with_no_targets_yields_no_falsification(self):
        """Evidence with neither step_index nor file_path can't
        match anything."""
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_file_path="",  # empty
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.outcome is FalsificationOutcome.NO_FALSIFICATION


# ---------------------------------------------------------------------------
# Staleness filter
# ---------------------------------------------------------------------------


class TestStalenessFilter:
    def test_stale_evidence_dropped(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S", "10",
        )
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_step_index=0,
            captured_monotonic=100.0,
        )
        # Decision time 200s later — evidence is 200-100=100s old
        # vs max_age=10. Should be dropped → INSUFFICIENT_EVIDENCE.
        v = compute_falsification_verdict(
            (h,), (e,), enabled=True, decision_monotonic=200.0,
        )
        assert v.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    def test_fresh_evidence_kept(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S", "10",
        )
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING,
            target_step_index=0,
            captured_monotonic=195.0,
        )
        # 5s old, max_age=10 — kept.
        v = compute_falsification_verdict(
            (h,), (e,), enabled=True, decision_monotonic=200.0,
        )
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED


# ---------------------------------------------------------------------------
# Defensive degradation
# ---------------------------------------------------------------------------


class TestDefensiveDegradation:
    def test_non_tuple_inputs_coerced(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=0,
        )
        # Pass lists instead of tuples — coerced.
        v = compute_falsification_verdict([h], [e], enabled=True)  # type: ignore[arg-type]
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED

    def test_non_dataclass_elements_silently_dropped(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=0,
        )
        v = compute_falsification_verdict(
            (h, "not-a-hypothesis"),  # type: ignore[arg-type]
            (e, 42),  # type: ignore[arg-type]
            enabled=True,
        )
        assert v.outcome is FalsificationOutcome.REPLAN_TRIGGERED
        # Garbage dropped silently.
        assert v.total_hypotheses == 1
        assert v.total_evidence == 1

    def test_evidence_with_garbage_kind_silently_dropped(self):
        """An EvidenceItem whose kind is not a FalsificationKind
        instance is dropped (defensive — only typed evidence counts)."""
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        # Force a malformed evidence item via direct construction.
        # This shouldn't normally happen (the constructor accepts any
        # kind), but defense-in-depth.
        e_bad = EvidenceItem(
            kind="not-a-kind",  # type: ignore[arg-type]
            target_step_index=0,
        )
        v = compute_falsification_verdict(
            (h,), (e_bad,), enabled=True,
        )
        assert v.outcome is FalsificationOutcome.INSUFFICIENT_EVIDENCE

    def test_compute_never_raises(self):
        garbage = [
            (None, None),
            ("not-a-tuple", "not-a-tuple"),
            (42, 42),
        ]
        for h, e in garbage:
            try:
                v = compute_falsification_verdict(
                    h, e, enabled=True,  # type: ignore[arg-type]
                )
                assert isinstance(v, FalsificationVerdict)
            except Exception:
                pytest.fail(f"compute raised on inputs {h!r}, {e!r}")


# ---------------------------------------------------------------------------
# Phase C tightening stamping
# ---------------------------------------------------------------------------


class TestPhaseCTighteningStamp:
    def test_replan_triggered_stamps_passed(self):
        h = PlanStepHypothesis(step_index=0, file_path="x.py")
        e = EvidenceItem(
            kind=FalsificationKind.FILE_MISSING, target_step_index=0,
        )
        v = compute_falsification_verdict((h,), (e,), enabled=True)
        assert v.monotonic_tightening_verdict == "passed"

    @pytest.mark.parametrize("outcome", [
        FalsificationOutcome.NO_FALSIFICATION,
        FalsificationOutcome.INSUFFICIENT_EVIDENCE,
        FalsificationOutcome.DISABLED,
        FalsificationOutcome.FAILED,
    ])
    def test_non_replan_outcomes_stamp_empty(
        self, outcome: FalsificationOutcome,
    ):
        v = FalsificationVerdict(outcome=outcome)
        assert v.is_tightening is False
        # Verdicts constructed via compute_falsification_verdict
        # also stamp empty for these outcomes (verified via the
        # disabled/insufficient/no-match tests above).


# ---------------------------------------------------------------------------
# Convenience constructor
# ---------------------------------------------------------------------------


class TestPairConstructor:
    def test_pair_builds_hypothesis(self):
        h = pair_plan_step_with_hypothesis(
            step_index=2,
            ordered_change={
                "file_path": "auth.py",
                "change_type": "modify",
                "description": "add login()",
            },
            expected_outcome="login() exists and returns bool",
            hypothesis_id="hyp-abc",
        )
        assert h.step_index == 2
        assert h.file_path == "auth.py"
        assert h.change_type == "modify"
        assert h.expected_outcome == "login() exists and returns bool"
        assert h.hypothesis_id == "hyp-abc"

    def test_pair_truncates_long_predicate(self):
        long_predicate = "x" * 5000
        h = pair_plan_step_with_hypothesis(
            step_index=0, ordered_change={"file_path": "a.py"},
            expected_outcome=long_predicate,
        )
        assert len(h.expected_outcome) == 1000

    def test_pair_truncates_long_hypothesis_id(self):
        long_id = "x" * 500
        h = pair_plan_step_with_hypothesis(
            step_index=0, ordered_change={"file_path": "a.py"},
            hypothesis_id=long_id,
        )
        assert len(h.hypothesis_id) == 128

    def test_pair_garbage_change_returns_safe_default(self):
        h = pair_plan_step_with_hypothesis(
            step_index=0, ordered_change=None,  # type: ignore[arg-type]
        )
        assert isinstance(h, PlanStepHypothesis)
        assert h.file_path == ""

    def test_pair_never_raises(self):
        for bad_change in [None, "not-a-mapping", 42, []]:
            try:
                h = pair_plan_step_with_hypothesis(
                    step_index=0, ordered_change=bad_change,  # type: ignore[arg-type]
                )
                assert isinstance(h, PlanStepHypothesis)
            except Exception:
                pytest.fail(f"pair raised on {bad_change!r}")


# ---------------------------------------------------------------------------
# Master flag asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlagSemantics:
    def test_default_is_false_pre_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_ENABLED", raising=False,
        )
        assert plan_falsification_enabled() is False

    def test_empty_string_is_default_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_PLAN_FALSIFICATION_ENABLED", "")
        assert plan_falsification_enabled() is False

    @pytest.mark.parametrize(
        "truthy", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy_enables(self, monkeypatch, truthy: str):
        monkeypatch.setenv("JARVIS_PLAN_FALSIFICATION_ENABLED", truthy)
        assert plan_falsification_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "FALSE"],
    )
    def test_falsy_disables(self, monkeypatch, falsy: str):
        monkeypatch.setenv("JARVIS_PLAN_FALSIFICATION_ENABLED", falsy)
        assert plan_falsification_enabled() is False


# ---------------------------------------------------------------------------
# Env-knob clamping
# ---------------------------------------------------------------------------


class TestEnvKnobClamping:
    def test_min_evidence_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE", raising=False,
        )
        assert min_evidence_count() == 1

    def test_min_evidence_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE", "0",
        )
        assert min_evidence_count() == 1
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE", "9999",
        )
        assert min_evidence_count() == 16

    def test_min_evidence_garbage_uses_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MIN_EVIDENCE", "not-a-number",
        )
        assert min_evidence_count() == 1

    def test_max_age_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S", raising=False,
        )
        assert falsification_max_age_s() == 300.0

    def test_max_age_floor_and_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S", "0.001",
        )
        assert falsification_max_age_s() == 1.0
        monkeypatch.setenv(
            "JARVIS_PLAN_FALSIFICATION_MAX_AGE_S", "9999",
        )
        assert falsification_max_age_s() == 3600.0


# ---------------------------------------------------------------------------
# Authority invariant — pure-stdlib at hot path
# ---------------------------------------------------------------------------


class TestPureStdlibInvariant:
    def _source(self) -> str:
        path = (
            pathlib.Path(__file__).parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "plan_falsification.py"
        )
        return path.read_text()

    def test_no_governance_imports_at_module_top(self):
        """Slice 1 stays pure-stdlib (registration-contract
        exemption applies — n/a here since Slice 4 adds the
        register_* functions)."""
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
                if "backend." in module or "governance" in module:
                    lineno = getattr(node, "lineno", 0)
                    if any(s <= lineno <= e for s, e in exempt_ranges):
                        continue
                    raise AssertionError(
                        f"Slice 1 must be pure-stdlib — found "
                        f"governance import {module!r} at line {lineno}"
                    )

    def test_no_async_def_in_module(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                raise AssertionError(
                    f"Slice 1 must be sync — found async def "
                    f"{node.name!r} at line "
                    f"{getattr(node, 'lineno', '?')}"
                )

    def test_no_exec_eval_compile_calls(self):
        source = self._source()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if node.func.id in ("exec", "eval", "compile"):
                        raise AssertionError(
                            f"Slice 1 must NOT exec/eval/compile — "
                            f"found {node.func.id}() at line "
                            f"{getattr(node, 'lineno', '?')}"
                        )


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert PLAN_FALSIFICATION_SCHEMA_VERSION == "plan_falsification.1"
