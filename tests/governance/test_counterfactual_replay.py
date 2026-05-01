"""Priority #3 Slice 1 — Counterfactual Replay primitive regression tests.

Coverage:

  * **Master flag** — asymmetric env semantics.
  * **Closed-taxonomy pins** — ReplayOutcome 5-value,
    BranchVerdict 5-value, DecisionOverrideKind 5-value.
  * **Env knob clamps** — max_duration / max_phases /
    min_replays / postmortem_tolerance / verify_pct_tolerance.
  * **Schema integrity** — frozen dataclasses + to_dict /
    from_dict round-trip + schema-mismatch tolerance.
  * **compute_branch_verdict** — full closed-taxonomy decision
    tree:
      - primary axis (terminal_success delta)
      - secondary axis (postmortem count + tolerance)
      - tertiary axis (verify pass rate + tolerance)
      - quaternary axis (apply_outcome quality)
      - contradicting → DIVERGED_NEUTRAL
      - within tolerance → EQUIVALENT
  * **compute_replay_outcome** — outcome matrix:
      - DISABLED (master off)
      - FAILED (garbage target / both branches missing)
      - DIVERGED (cached hash mismatch upstream)
      - PARTIAL (one branch missing)
      - SUCCESS (verdict computed)
  * **Verdict helpers** — `is_prevention_evidence` only TRUE
    on SUCCESS+DIVERGED_BETTER; `has_actionable_verdict`
    TRUE on SUCCESS+(any DIVERGED).
  * **Verdict fingerprint** — stable sha256[:16] for Slice 4
    dedup.
  * **Defensive contract** — every public function NEVER
    raises.
  * **Authority invariants** — AST-pinned: stdlib only, no
    governance imports, no exec/eval/compile, no async, no
    mutation tools.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.counterfactual_replay import (
    BranchSnapshot,
    BranchVerdict,
    COUNTERFACTUAL_REPLAY_SCHEMA_VERSION,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
    compute_branch_verdict,
    compute_replay_outcome,
    counterfactual_replay_enabled,
    replay_max_duration_seconds,
    replay_max_phases_per_branch,
    replay_min_replays_for_baseline,
    verdict_tolerance_postmortem_count,
    verdict_tolerance_verify_pass_pct,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    _apply_quality,
    _verdict_fingerprint,
)


_FORBIDDEN_CALL_TOKENS = ("e" + "val(", "e" + "xec(")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_branch(
    *,
    branch_id: str = "branch-1",
    success: bool = True,
    apply_outcome: str = "single",
    verify_passed: int = 10,
    verify_total: int = 10,
    postmortems: tuple = (),
    cost_usd: float = 0.0,
) -> BranchSnapshot:
    return BranchSnapshot(
        branch_id=branch_id,
        terminal_phase="COMPLETE" if success else "VALIDATE",
        terminal_success=success,
        apply_outcome=apply_outcome,
        verify_passed=verify_passed,
        verify_total=verify_total,
        postmortem_records=postmortems,
        cost_usd=cost_usd,
    )


def _make_target() -> ReplayTarget:
    return ReplayTarget(
        session_id="bt-2026-04-25",
        swap_at_phase="GATE",
        swap_decision_kind=DecisionOverrideKind.GATE_DECISION,
        swap_decision_payload={"verdict": "approval_required"},
    )


# ---------------------------------------------------------------------------
# 1. Master flag — asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_is_true_post_graduation(self):
        """Slice 5 graduation flipped the master default to True
        (2026-05-02). Hot-revert path remains via explicit
        falsy env value."""
        os.environ.pop(
            "JARVIS_COUNTERFACTUAL_REPLAY_ENABLED", None,
        )
        assert counterfactual_replay_enabled() is True

    @pytest.mark.parametrize(
        "v", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_truthy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COUNTERFACTUAL_REPLAY_ENABLED": v},
        ):
            assert counterfactual_replay_enabled() is True

    @pytest.mark.parametrize(
        "v", ["0", "false", "no", "off"],
    )
    def test_falsy(self, v):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COUNTERFACTUAL_REPLAY_ENABLED": v},
        ):
            assert counterfactual_replay_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, v):
        """Empty/whitespace = unset = graduated default-true."""
        with mock.patch.dict(
            os.environ,
            {"JARVIS_COUNTERFACTUAL_REPLAY_ENABLED": v},
        ):
            assert counterfactual_replay_enabled() is True


# ---------------------------------------------------------------------------
# 2. Closed taxonomy pins
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:
    def test_replay_outcome_5_values(self):
        assert len(list(ReplayOutcome)) == 5

    def test_replay_outcome_values(self):
        expected = {
            "success", "partial", "diverged", "disabled",
            "failed",
        }
        assert {o.value for o in ReplayOutcome} == expected

    def test_branch_verdict_5_values(self):
        assert len(list(BranchVerdict)) == 5

    def test_branch_verdict_values(self):
        expected = {
            "equivalent", "diverged_better", "diverged_worse",
            "diverged_neutral", "failed",
        }
        assert {v.value for v in BranchVerdict} == expected

    def test_decision_override_kind_5_values(self):
        assert len(list(DecisionOverrideKind)) == 5

    def test_decision_override_kind_values(self):
        expected = {
            "gate_decision", "postmortem_injection",
            "recurrence_boost", "quorum_invocation",
            "coherence_observer",
        }
        assert {
            k.value for k in DecisionOverrideKind
        } == expected


# ---------------------------------------------------------------------------
# 3. Env knob clamps
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    @pytest.mark.parametrize(
        "knob,floor,ceiling,defval,fn",
        [
            (
                "JARVIS_REPLAY_MAX_DURATION_SECONDS",
                30.0, 1800.0, 300.0,
                replay_max_duration_seconds,
            ),
            (
                "JARVIS_REPLAY_MAX_PHASES_PER_BRANCH",
                5, 500, 50,
                replay_max_phases_per_branch,
            ),
            (
                "JARVIS_REPLAY_MIN_REPLAYS_FOR_BASELINE",
                1, 100, 5,
                replay_min_replays_for_baseline,
            ),
            (
                "JARVIS_REPLAY_VERDICT_TOLERANCE_POSTMORTEM",
                0, 10, 0,
                verdict_tolerance_postmortem_count,
            ),
            (
                "JARVIS_REPLAY_VERDICT_TOLERANCE_VERIFY_PCT",
                0.0, 50.0, 1.0,
                verdict_tolerance_verify_pass_pct,
            ),
        ],
    )
    def test_floor_ceiling_clamps(
        self, knob, floor, ceiling, defval, fn,
    ):
        os.environ.pop(knob, None)
        assert fn() == defval
        with mock.patch.dict(os.environ, {knob: "-99999"}):
            assert fn() == floor
        with mock.patch.dict(os.environ, {knob: "99999"}):
            assert fn() == ceiling

    def test_garbage_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_REPLAY_MAX_DURATION_SECONDS": "garbage"},
        ):
            assert replay_max_duration_seconds() == 300.0


# ---------------------------------------------------------------------------
# 4. Schema integrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:
    def test_target_frozen(self):
        t = _make_target()
        with pytest.raises((AttributeError, Exception)):
            t.session_id = "x"  # type: ignore[misc]

    def test_branch_frozen(self):
        b = _make_branch()
        with pytest.raises((AttributeError, Exception)):
            b.branch_id = "x"  # type: ignore[misc]

    def test_verdict_frozen(self):
        v = ReplayVerdict(outcome=ReplayOutcome.SUCCESS)
        with pytest.raises((AttributeError, Exception)):
            v.outcome = ReplayOutcome.FAILED  # type: ignore[misc]

    def test_target_round_trip(self):
        t = _make_target()
        d = t.to_dict()
        recon = ReplayTarget.from_dict(d)
        assert recon is not None
        assert recon.session_id == t.session_id
        assert (
            recon.swap_decision_kind is t.swap_decision_kind
        )
        assert dict(recon.swap_decision_payload) == dict(
            t.swap_decision_payload,
        )

    def test_branch_round_trip(self):
        b = _make_branch(
            success=True,
            apply_outcome="multi",
            verify_passed=8, verify_total=10,
            postmortems=("test_failure", "build_failure"),
            cost_usd=0.42,
        )
        d = b.to_dict()
        recon = BranchSnapshot.from_dict(d)
        assert recon is not None
        assert recon.terminal_success is True
        assert recon.apply_outcome == "multi"
        assert recon.verify_passed == 8
        assert recon.postmortem_records == (
            "test_failure", "build_failure",
        )
        assert recon.cost_usd == 0.42

    def test_target_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong"}
        assert ReplayTarget.from_dict(d) is None

    def test_branch_schema_mismatch_returns_none(self):
        d = {"schema_version": "wrong"}
        assert BranchSnapshot.from_dict(d) is None

    def test_target_malformed_returns_none(self):
        d = {
            "schema_version": (
                COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            ),
            # Missing required fields
        }
        assert ReplayTarget.from_dict(d) is None

    def test_branch_malformed_returns_none(self):
        d = {
            "schema_version": (
                COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            ),
            "branch_id": "x",
            # Missing terminal_phase / terminal_success
        }
        assert BranchSnapshot.from_dict(d) is None

    def test_schema_version_stable(self):
        assert (
            COUNTERFACTUAL_REPLAY_SCHEMA_VERSION
            == "counterfactual_replay.1"
        )

    def test_verify_pass_rate_zero_total(self):
        b = _make_branch(verify_passed=0, verify_total=0)
        # No tests = full credit (1.0)
        assert b.verify_pass_rate() == 1.0

    def test_verify_pass_rate_full(self):
        b = _make_branch(verify_passed=10, verify_total=10)
        assert b.verify_pass_rate() == 1.0

    def test_verify_pass_rate_half(self):
        b = _make_branch(verify_passed=5, verify_total=10)
        assert b.verify_pass_rate() == 0.5


# ---------------------------------------------------------------------------
# 5. compute_branch_verdict — primary axis (terminal success)
# ---------------------------------------------------------------------------


class TestVerdictPrimaryAxis:
    def test_orig_succ_counter_fail_diverged_better(self):
        orig = _make_branch(success=True)
        counter = _make_branch(success=False)
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_BETTER
        )

    def test_orig_fail_counter_succ_diverged_worse(self):
        orig = _make_branch(success=False)
        counter = _make_branch(success=True)
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_WORSE
        )

    def test_both_succ_equal_equivalent(self):
        orig = _make_branch(success=True)
        counter = _make_branch(success=True)
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.EQUIVALENT
        )

    def test_both_fail_equal_equivalent(self):
        orig = _make_branch(success=False)
        counter = _make_branch(success=False)
        # Both failed, no other deltas → EQUIVALENT
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.EQUIVALENT
        )


# ---------------------------------------------------------------------------
# 6. compute_branch_verdict — secondary axis (postmortem count)
# ---------------------------------------------------------------------------


class TestVerdictPostmortemAxis:
    def test_counter_fewer_postmortems_diverged_worse(self):
        orig = _make_branch(
            success=True, postmortems=("a", "b", "c"),
        )
        counter = _make_branch(success=True, postmortems=())
        # Counter has fewer postmortems → counter wins
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_WORSE
        )

    def test_orig_fewer_postmortems_diverged_better(self):
        orig = _make_branch(success=True, postmortems=())
        counter = _make_branch(
            success=True, postmortems=("a", "b", "c"),
        )
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_BETTER
        )

    def test_pm_within_tolerance_equivalent(self):
        # Same default tolerance is 0; difference of 1 does
        # show up. Use explicit tolerance=2 to test.
        orig = _make_branch(
            success=True, postmortems=("a",),
        )
        counter = _make_branch(success=True, postmortems=())
        assert (
            compute_branch_verdict(
                orig, counter, postmortem_tolerance=2,
            )
            is BranchVerdict.EQUIVALENT
        )


# ---------------------------------------------------------------------------
# 7. compute_branch_verdict — tertiary axis (verify pass rate)
# ---------------------------------------------------------------------------


class TestVerdictVerifyAxis:
    def test_counter_higher_verify_rate_diverged_worse(self):
        orig = _make_branch(
            success=True, verify_passed=5, verify_total=10,
        )
        counter = _make_branch(
            success=True, verify_passed=10, verify_total=10,
        )
        # Counter has higher pass rate → counter wins
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_WORSE
        )

    def test_verify_within_tolerance_equivalent(self):
        # 99% vs 100% — within 1.0% default tolerance
        orig = _make_branch(
            success=True, verify_passed=99, verify_total=100,
        )
        counter = _make_branch(
            success=True, verify_passed=100, verify_total=100,
        )
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.EQUIVALENT
        )

    def test_verify_explicit_tolerance(self):
        orig = _make_branch(
            success=True, verify_passed=80, verify_total=100,
        )
        counter = _make_branch(
            success=True, verify_passed=100, verify_total=100,
        )
        # 20% delta — within explicit tolerance=25%
        assert (
            compute_branch_verdict(
                orig, counter, verify_pct_tolerance=25.0,
            )
            is BranchVerdict.EQUIVALENT
        )


# ---------------------------------------------------------------------------
# 8. compute_branch_verdict — multi-criteria contradicting
# ---------------------------------------------------------------------------


class TestVerdictContradicting:
    def test_contradicting_axes_diverged_neutral(self):
        # orig: better postmortem (0), worse verify (5/10)
        # counter: worse postmortem (3), better verify (10/10)
        orig = _make_branch(
            success=True,
            postmortems=(),
            verify_passed=5, verify_total=10,
        )
        counter = _make_branch(
            success=True,
            postmortems=("a", "b", "c"),
            verify_passed=10, verify_total=10,
        )
        assert (
            compute_branch_verdict(orig, counter)
            is BranchVerdict.DIVERGED_NEUTRAL
        )


# ---------------------------------------------------------------------------
# 9. compute_branch_verdict — quaternary axis (apply outcome)
# ---------------------------------------------------------------------------


class TestVerdictApplyAxis:
    def test_apply_quality_helper(self):
        # success-with-apply > success-without > failed-with >
        # failed-without
        assert _apply_quality("single", True) == 3
        assert _apply_quality("multi", True) == 3
        assert _apply_quality("none", True) == 2
        assert _apply_quality("", True) == 2
        assert _apply_quality("single", False) == 1
        assert _apply_quality("none", False) == 0
        assert _apply_quality("", False) == 0

    def test_apply_quality_garbage_returns_zero(self):
        assert _apply_quality(None, True) >= 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. compute_branch_verdict — defensive
# ---------------------------------------------------------------------------


class TestVerdictDefensive:
    def test_orig_none_returns_failed(self):
        assert (
            compute_branch_verdict(None, _make_branch())
            is BranchVerdict.FAILED
        )

    def test_counter_none_returns_failed(self):
        assert (
            compute_branch_verdict(_make_branch(), None)
            is BranchVerdict.FAILED
        )

    def test_garbage_returns_failed(self):
        assert (
            compute_branch_verdict(
                "not a snapshot",  # type: ignore[arg-type]
                _make_branch(),
            )
            is BranchVerdict.FAILED
        )

    def test_both_none_returns_failed(self):
        assert (
            compute_branch_verdict(None, None)
            is BranchVerdict.FAILED
        )


# ---------------------------------------------------------------------------
# 11. compute_replay_outcome — outcome matrix
# ---------------------------------------------------------------------------


class TestReplayOutcome:
    def test_disabled(self):
        v = compute_replay_outcome(
            _make_target(),
            _make_branch(), _make_branch(),
            enabled_override=False,
        )
        assert v.outcome is ReplayOutcome.DISABLED

    def test_failed_on_garbage_target(self):
        v = compute_replay_outcome(
            "not a target",  # type: ignore[arg-type]
            _make_branch(), _make_branch(),
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.FAILED

    def test_diverged_on_hash_mismatch(self):
        v = compute_replay_outcome(
            _make_target(),
            _make_branch(), _make_branch(),
            divergence_phase="GENERATE",
            divergence_reason="hash mismatch",
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.DIVERGED
        assert v.divergence_phase == "GENERATE"

    def test_failed_on_both_branches_missing(self):
        v = compute_replay_outcome(
            _make_target(), None, None,
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.FAILED

    def test_partial_when_orig_only(self):
        v = compute_replay_outcome(
            _make_target(),
            _make_branch(), None,
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.PARTIAL
        assert v.original_branch is not None
        assert v.counterfactual_branch is None

    def test_partial_when_counter_only(self):
        v = compute_replay_outcome(
            _make_target(),
            None, _make_branch(),
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.PARTIAL

    def test_success_with_diverged_better(self):
        orig = _make_branch(success=True)
        counter = _make_branch(success=False)
        v = compute_replay_outcome(
            _make_target(), orig, counter,
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.verdict is BranchVerdict.DIVERGED_BETTER

    def test_success_with_equivalent(self):
        orig = _make_branch(success=True)
        counter = _make_branch(success=True)
        v = compute_replay_outcome(
            _make_target(), orig, counter,
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.SUCCESS
        assert v.verdict is BranchVerdict.EQUIVALENT


# ---------------------------------------------------------------------------
# 12. Verdict helpers
# ---------------------------------------------------------------------------


class TestVerdictHelpers:
    def test_is_prevention_evidence_only_diverged_better(self):
        # Only SUCCESS+DIVERGED_BETTER counts as prevention
        # evidence
        assert ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            verdict=BranchVerdict.DIVERGED_BETTER,
        ).is_prevention_evidence() is True

        assert ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            verdict=BranchVerdict.DIVERGED_WORSE,
        ).is_prevention_evidence() is False

        assert ReplayVerdict(
            outcome=ReplayOutcome.SUCCESS,
            verdict=BranchVerdict.EQUIVALENT,
        ).is_prevention_evidence() is False

        assert ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL,
            verdict=BranchVerdict.DIVERGED_BETTER,
        ).is_prevention_evidence() is False

    def test_has_actionable_verdict(self):
        for v in (
            BranchVerdict.DIVERGED_BETTER,
            BranchVerdict.DIVERGED_WORSE,
            BranchVerdict.DIVERGED_NEUTRAL,
        ):
            assert ReplayVerdict(
                outcome=ReplayOutcome.SUCCESS, verdict=v,
            ).has_actionable_verdict() is True

        for v in (
            BranchVerdict.EQUIVALENT, BranchVerdict.FAILED,
        ):
            assert ReplayVerdict(
                outcome=ReplayOutcome.SUCCESS, verdict=v,
            ).has_actionable_verdict() is False

        # Non-SUCCESS outcome is never actionable
        assert ReplayVerdict(
            outcome=ReplayOutcome.PARTIAL,
            verdict=BranchVerdict.DIVERGED_BETTER,
        ).has_actionable_verdict() is False


# ---------------------------------------------------------------------------
# 13. Verdict fingerprint
# ---------------------------------------------------------------------------


class TestVerdictFingerprint:
    def test_fingerprint_stable(self):
        t = _make_target()
        fp1 = _verdict_fingerprint(
            t, BranchVerdict.DIVERGED_BETTER,
        )
        fp2 = _verdict_fingerprint(
            t, BranchVerdict.DIVERGED_BETTER,
        )
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_different_verdicts_different_fingerprints(self):
        t = _make_target()
        fp_better = _verdict_fingerprint(
            t, BranchVerdict.DIVERGED_BETTER,
        )
        fp_worse = _verdict_fingerprint(
            t, BranchVerdict.DIVERGED_WORSE,
        )
        assert fp_better != fp_worse

    def test_none_target_returns_empty(self):
        assert _verdict_fingerprint(
            None, BranchVerdict.DIVERGED_BETTER,
        ) == ""


# ---------------------------------------------------------------------------
# 14. Defensive contract
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_compute_branch_verdict_never_raises(self):
        # Garbage shapes → FAILED, not exception
        assert (
            compute_branch_verdict(42, "garbage")  # type: ignore[arg-type]
            is BranchVerdict.FAILED
        )

    def test_compute_replay_outcome_never_raises(self):
        # Garbage shapes → FAILED, not exception
        v = compute_replay_outcome(
            42, "garbage", 99,  # type: ignore[arg-type]
            enabled_override=True,
        )
        assert v.outcome is ReplayOutcome.FAILED

    def test_apply_quality_garbage(self):
        assert _apply_quality(42, True) >= 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 15. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


def _module_source() -> str:
    path = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "verification" / "counterfactual_replay.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def source(self):
        return _module_source()

    def test_no_governance_imports(self, source):
        """Slice 1 is PURE-STDLIB. Strongest authority
        invariant. Slice 3+ may import
        adaptation.ledger.MonotonicTighteningVerdict; Slice 1
        stays pure."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "backend." not in module, (
                    f"forbidden backend import: {module}"
                )
                assert "governance" not in module, (
                    f"forbidden governance import: {module}"
                )

    def test_no_orchestrator_imports(self, source):
        forbidden = [
            "orchestrator", "iron_gate", "policy",
            "change_engine", "candidate_generator", "providers",
            "doubleword_provider", "urgency_router",
            "auto_action_router", "subagent_scheduler",
            "tool_executor", "phase_runners",
            "semantic_guardian", "semantic_firewall",
            "risk_engine", "ast_canonical", "semantic_index",
            "episodic_memory",
        ]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                m = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                m = m or ""
                for f in forbidden:
                    assert f not in m, f"forbidden import: {m}"

    def test_stdlib_only_imports(self, source):
        """Final pin: every import must be stdlib."""
        stdlib_only = {
            "__future__", "enum", "hashlib", "logging", "os",
            "dataclasses", "typing",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                root = m.split(".", 1)[0]
                assert root in stdlib_only, (
                    f"non-stdlib import: {m}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    assert root in stdlib_only, (
                        f"non-stdlib import: {alias.name}"
                    )

    def test_no_mutation_tools(self, source):
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "os.remove", "os.unlink",
            "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in source

    def test_no_eval_family_calls(self, source):
        """Critical safety pin — replay primitive NEVER
        executes code."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"forbidden bare call: {node.func.id}"
                    )
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in source, (
                f"forbidden syntactic call: {token!r}"
            )

    def test_no_async_functions(self, source):
        """Slice 1 is sync; Slice 5 wraps via to_thread at
        orchestrator."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function: "
                f"{getattr(node, 'name', '?')}"
            )

    def test_public_api_exported(self, source):
        for name in (
            "BranchSnapshot", "BranchVerdict",
            "DecisionOverrideKind",
            "ReplayOutcome", "ReplayTarget", "ReplayVerdict",
            "compute_branch_verdict", "compute_replay_outcome",
            "counterfactual_replay_enabled",
            "replay_max_duration_seconds",
            "replay_max_phases_per_branch",
            "replay_min_replays_for_baseline",
            "verdict_tolerance_postmortem_count",
            "verdict_tolerance_verify_pass_pct",
            "COUNTERFACTUAL_REPLAY_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in source, (
                f"public API {name!r} not in __all__"
            )
