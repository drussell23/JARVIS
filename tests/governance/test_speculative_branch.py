"""Priority #4 Slice 1 — Speculative Branch Tree primitive regression suite.

Tests the pure-stdlib primitive layer: 3 closed-taxonomy 5-value enums,
4 frozen dataclasses with to_dict/from_dict round-trip, 5 env-knob
helpers with floor+ceiling clamps, 2 pure decision functions
(compute_tree_verdict + compute_tree_outcome), 1 canonical fingerprint
function.

Test classes:
  * TestMasterFlag — asymmetric env semantics
  * TestEnvKnobs — clamping discipline
  * TestClosedTaxonomies — 5-value enum integrity
  * TestSchemaIntegrity — frozen dataclasses + round-trip + schema mismatch
  * TestCanonicalFingerprint — order-independence + stability
  * TestComputeTreeVerdict — convergence decision tree
  * TestComputeTreeOutcome — top-level outcome resolution
  * TestVerdictHelpers — is_actionable + has_disagreement_signal
  * TestDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification import (
    speculative_branch as sbt_mod,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchOutcome,
    BranchResult,
    BranchTreeTarget,
    EvidenceKind,
    SBT_SCHEMA_VERSION,
    TreeVerdict,
    TreeVerdictResult,
    canonical_evidence_fingerprint,
    compute_tree_outcome,
    compute_tree_verdict,
    sbt_diminishing_returns_threshold,
    sbt_enabled,
    sbt_max_breadth,
    sbt_max_depth,
    sbt_max_wall_seconds,
    sbt_min_confidence_for_winner,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens (Slice 1/2/3 pattern)
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _ev(
    kind: EvidenceKind = EvidenceKind.FILE_READ,
    content_hash: str = "h1",
    confidence: float = 0.9,
    source_tool: str = "read_file",
) -> BranchEvidence:
    return BranchEvidence(
        kind=kind, content_hash=content_hash,
        confidence=confidence, source_tool=source_tool,
    )


def _branch(
    branch_id: str = "b1",
    outcome: BranchOutcome = BranchOutcome.SUCCESS,
    evidence: tuple = (),
    fingerprint: str = "",
    depth: int = 0,
) -> BranchResult:
    return BranchResult(
        branch_id=branch_id, outcome=outcome,
        evidence=evidence, fingerprint=fingerprint, depth=depth,
    )


def _target(
    decision_id: str = "op-1.GENERATE.unclear_dep",
    ambiguity_kind: str = "unclear_dep_graph",
) -> BranchTreeTarget:
    return BranchTreeTarget(
        decision_id=decision_id,
        ambiguity_kind=ambiguity_kind,
    )


# ---------------------------------------------------------------------------
# TestMasterFlag
# ---------------------------------------------------------------------------


class TestMasterFlag:

    def test_default_is_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_ENABLED", raising=False)
        assert sbt_enabled() is False

    def test_empty_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "")
        assert sbt_enabled() is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", v)
        assert sbt_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_falsy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", v)
        assert sbt_enabled() is False

    @pytest.mark.parametrize("v", ["", "   ", "\t\n"])
    def test_whitespace_treated_as_unset(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", v)
        assert sbt_enabled() is False


# ---------------------------------------------------------------------------
# TestEnvKnobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_max_depth_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_MAX_DEPTH", raising=False)
        assert sbt_max_depth() == 3

    def test_max_depth_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_DEPTH", "0")
        assert sbt_max_depth() == 1

    def test_max_depth_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_DEPTH", "999")
        assert sbt_max_depth() == 8

    def test_max_depth_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_DEPTH", "junk")
        assert sbt_max_depth() == 3

    def test_max_breadth_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_MAX_BREADTH", raising=False)
        assert sbt_max_breadth() == 3

    def test_max_breadth_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_BREADTH", "1")
        assert sbt_max_breadth() == 2  # floor=2 (need 2 for convergence)

    def test_max_breadth_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_BREADTH", "100")
        assert sbt_max_breadth() == 8

    def test_max_wall_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_MAX_WALL_SECONDS", raising=False)
        assert sbt_max_wall_seconds() == pytest.approx(60.0)

    def test_max_wall_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_WALL_SECONDS", "0.1")
        assert sbt_max_wall_seconds() == pytest.approx(10.0)

    def test_max_wall_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_MAX_WALL_SECONDS", "9999")
        assert sbt_max_wall_seconds() == pytest.approx(600.0)

    def test_dim_returns_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD", raising=False,
        )
        assert sbt_diminishing_returns_threshold() == pytest.approx(0.95)

    def test_dim_returns_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD", "1.5",
        )
        assert sbt_diminishing_returns_threshold() == pytest.approx(1.0)
        monkeypatch.setenv(
            "JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD", "0.1",
        )
        assert sbt_diminishing_returns_threshold() == pytest.approx(0.5)

    def test_min_confidence_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_SBT_MIN_CONFIDENCE_FOR_WINNER", raising=False,
        )
        assert sbt_min_confidence_for_winner() == pytest.approx(0.5)

    def test_min_confidence_clamps(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SBT_MIN_CONFIDENCE_FOR_WINNER", "-1",
        )
        assert sbt_min_confidence_for_winner() == pytest.approx(0.0)
        monkeypatch.setenv(
            "JARVIS_SBT_MIN_CONFIDENCE_FOR_WINNER", "5",
        )
        assert sbt_min_confidence_for_winner() == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestClosedTaxonomies
# ---------------------------------------------------------------------------


class TestClosedTaxonomies:

    def test_branch_outcome_5_values(self):
        assert {x.value for x in BranchOutcome} == {
            "success", "partial", "timeout", "disabled", "failed",
        }

    def test_evidence_kind_5_values(self):
        assert {x.value for x in EvidenceKind} == {
            "file_read", "symbol_lookup", "pattern_match",
            "caller_graph", "type_inference",
        }

    def test_tree_verdict_5_values(self):
        assert {x.value for x in TreeVerdict} == {
            "converged", "diverged", "inconclusive",
            "truncated", "failed",
        }

    def test_all_string_subclassed(self):
        for enum_cls in (BranchOutcome, EvidenceKind, TreeVerdict):
            for member in enum_cls:
                assert isinstance(member.value, str)


# ---------------------------------------------------------------------------
# TestSchemaIntegrity
# ---------------------------------------------------------------------------


class TestSchemaIntegrity:

    def test_branch_evidence_frozen(self):
        e = _ev()
        with pytest.raises(FrozenInstanceError):
            e.confidence = 0.5  # type: ignore

    def test_branch_evidence_round_trip(self):
        e = _ev(content_hash="abc", confidence=0.7)
        recon = BranchEvidence.from_dict(e.to_dict())
        assert recon is not None
        assert recon.kind is e.kind
        assert recon.content_hash == "abc"
        assert recon.confidence == pytest.approx(0.7)

    def test_branch_evidence_schema_mismatch(self):
        e = _ev()
        d = e.to_dict()
        d["schema_version"] = "wrong.99"
        assert BranchEvidence.from_dict(d) is None

    def test_branch_evidence_unknown_kind(self):
        e = _ev()
        d = e.to_dict()
        d["kind"] = "made_up_kind"
        assert BranchEvidence.from_dict(d) is None

    def test_branch_evidence_garbage(self):
        assert BranchEvidence.from_dict("not a dict") is None  # type: ignore
        assert BranchEvidence.from_dict({}) is None
        assert BranchEvidence.from_dict(None) is None  # type: ignore

    def test_branch_evidence_snippet_truncated(self):
        e = BranchEvidence(
            kind=EvidenceKind.FILE_READ, content_hash="x",
            snippet="A" * 1000,
        )
        d = e.to_dict()
        assert len(d["snippet"]) == 256

    def test_branch_result_frozen(self):
        b = _branch()
        with pytest.raises(FrozenInstanceError):
            b.depth = 5  # type: ignore

    def test_branch_result_round_trip(self):
        ev = _ev(content_hash="x", confidence=0.8)
        b = _branch(branch_id="bz", evidence=(ev,), fingerprint="fp1")
        recon = BranchResult.from_dict(b.to_dict())
        assert recon is not None
        assert recon.branch_id == "bz"
        assert recon.outcome is BranchOutcome.SUCCESS
        assert len(recon.evidence) == 1
        assert recon.fingerprint == "fp1"

    def test_branch_result_schema_mismatch(self):
        b = _branch()
        d = b.to_dict()
        d["schema_version"] = "v0"
        assert BranchResult.from_dict(d) is None

    def test_branch_result_unknown_outcome(self):
        b = _branch()
        d = b.to_dict()
        d["outcome"] = "fake_outcome"
        assert BranchResult.from_dict(d) is None

    def test_branch_result_average_confidence_empty(self):
        b = _branch(evidence=())
        assert b.average_confidence() == 0.0

    def test_branch_result_average_confidence(self):
        b = _branch(
            evidence=(_ev(confidence=0.4), _ev(confidence=0.8)),
        )
        assert b.average_confidence() == pytest.approx(0.6)

    def test_target_frozen(self):
        t = _target()
        with pytest.raises(FrozenInstanceError):
            t.decision_id = "x"  # type: ignore

    def test_target_round_trip(self):
        t = BranchTreeTarget(
            decision_id="dec1", ambiguity_kind="x",
            ambiguity_payload={"key": "val"},
            max_depth=5, max_breadth=4, max_wall_seconds=120.0,
        )
        recon = BranchTreeTarget.from_dict(t.to_dict())
        assert recon is not None
        assert recon.decision_id == "dec1"
        assert recon.max_depth == 5

    def test_target_effective_overrides(self):
        t = BranchTreeTarget(
            decision_id="x", ambiguity_kind="x",
            max_depth=5, max_breadth=4, max_wall_seconds=120.0,
        )
        assert t.effective_max_depth() == 5
        assert t.effective_max_breadth() == 4
        assert t.effective_max_wall_seconds() == pytest.approx(120.0)

    def test_target_effective_clamps_overrides(self):
        t = BranchTreeTarget(
            decision_id="x", ambiguity_kind="x",
            max_depth=999, max_breadth=999, max_wall_seconds=99999,
        )
        assert t.effective_max_depth() == 8
        assert t.effective_max_breadth() == 8
        assert t.effective_max_wall_seconds() == pytest.approx(600.0)

    def test_target_effective_falls_back_to_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_SBT_MAX_DEPTH", raising=False)
        t = _target()  # no overrides
        assert t.effective_max_depth() == 3

    def test_verdict_result_frozen(self):
        r = TreeVerdictResult(outcome=TreeVerdict.FAILED)
        with pytest.raises(FrozenInstanceError):
            r.detail = "x"  # type: ignore

    def test_verdict_result_round_trip(self):
        r = TreeVerdictResult(
            outcome=TreeVerdict.CONVERGED,
            target=_target(),
            branches=(_branch(fingerprint="fp"),),
            winning_branch_idx=0,
            winning_fingerprint="fp",
            aggregate_confidence=0.85,
            detail="x",
        )
        recon = TreeVerdictResult.from_dict(r.to_dict())
        assert recon is not None
        assert recon.outcome is TreeVerdict.CONVERGED
        assert recon.winning_branch_idx == 0
        assert len(recon.branches) == 1


# ---------------------------------------------------------------------------
# TestCanonicalFingerprint
# ---------------------------------------------------------------------------


class TestCanonicalFingerprint:

    def test_empty_returns_empty(self):
        assert canonical_evidence_fingerprint([]) == ""

    def test_stable_across_calls(self):
        ev = _ev(content_hash="abc")
        fp1 = canonical_evidence_fingerprint([ev])
        fp2 = canonical_evidence_fingerprint([ev])
        assert fp1 == fp2
        assert len(fp1) == 16

    def test_order_independent(self):
        a = _ev(kind=EvidenceKind.FILE_READ, content_hash="A")
        b = _ev(kind=EvidenceKind.SYMBOL_LOOKUP, content_hash="B")
        fp_ab = canonical_evidence_fingerprint([a, b])
        fp_ba = canonical_evidence_fingerprint([b, a])
        assert fp_ab == fp_ba

    def test_different_evidence_different_fp(self):
        a = _ev(content_hash="A")
        b = _ev(content_hash="B")
        assert (
            canonical_evidence_fingerprint([a])
            != canonical_evidence_fingerprint([b])
        )

    def test_kind_affects_fp(self):
        a = _ev(kind=EvidenceKind.FILE_READ, content_hash="X")
        b = _ev(kind=EvidenceKind.SYMBOL_LOOKUP, content_hash="X")
        assert (
            canonical_evidence_fingerprint([a])
            != canonical_evidence_fingerprint([b])
        )

    def test_garbage_returns_empty(self):
        assert canonical_evidence_fingerprint(None) == ""  # type: ignore


# ---------------------------------------------------------------------------
# TestComputeTreeVerdict — convergence decision tree
# ---------------------------------------------------------------------------


class TestComputeTreeVerdict:

    def test_empty_returns_failed(self):
        assert compute_tree_verdict([]) is TreeVerdict.FAILED

    def test_non_sequence_returns_failed(self):
        assert compute_tree_verdict(None) is TreeVerdict.FAILED  # type: ignore

    def test_all_failed_returns_failed(self):
        bs = [
            _branch(branch_id="b1", outcome=BranchOutcome.FAILED),
            _branch(branch_id="b2", outcome=BranchOutcome.FAILED),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.FAILED

    def test_all_timeout_returns_truncated(self):
        bs = [
            _branch(branch_id="b1", outcome=BranchOutcome.TIMEOUT),
            _branch(branch_id="b2", outcome=BranchOutcome.TIMEOUT),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.TRUNCATED

    def test_all_partial_returns_inconclusive(self):
        bs = [
            _branch(branch_id="b1", outcome=BranchOutcome.PARTIAL),
            _branch(branch_id="b2", outcome=BranchOutcome.PARTIAL),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.INCONCLUSIVE

    def test_single_fp_high_confidence_converged(self):
        ev = _ev(confidence=0.9)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.CONVERGED

    def test_single_fp_low_confidence_inconclusive(self):
        ev = _ev(confidence=0.1)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.INCONCLUSIVE

    def test_strict_majority_converged(self):
        ev_a = _ev(content_hash="A", confidence=0.9)
        ev_b = _ev(content_hash="B", confidence=0.9)
        fp_a = canonical_evidence_fingerprint([ev_a])
        fp_b = canonical_evidence_fingerprint([ev_b])
        # 3 vs 1 — strict majority on A
        bs = [
            _branch(branch_id="b1", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b2", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b3", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b4", evidence=(ev_b,), fingerprint=fp_b),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.CONVERGED

    def test_no_majority_diverged(self):
        ev_a = _ev(content_hash="A", confidence=0.9)
        ev_b = _ev(content_hash="B", confidence=0.9)
        ev_c = _ev(content_hash="C", confidence=0.9)
        fp_a = canonical_evidence_fingerprint([ev_a])
        fp_b = canonical_evidence_fingerprint([ev_b])
        fp_c = canonical_evidence_fingerprint([ev_c])
        bs = [
            _branch(branch_id="b1", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b2", evidence=(ev_b,), fingerprint=fp_b),
            _branch(branch_id="b3", evidence=(ev_c,), fingerprint=fp_c),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.DIVERGED

    def test_two_way_tie_diverged(self):
        ev_a = _ev(content_hash="A", confidence=0.9)
        ev_b = _ev(content_hash="B", confidence=0.9)
        fp_a = canonical_evidence_fingerprint([ev_a])
        fp_b = canonical_evidence_fingerprint([ev_b])
        bs = [
            _branch(branch_id="b1", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b2", evidence=(ev_b,), fingerprint=fp_b),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.DIVERGED

    def test_strict_majority_low_confidence_inconclusive(self):
        ev_low = _ev(content_hash="L", confidence=0.1)
        ev_high = _ev(content_hash="H", confidence=0.9)
        fp_l = canonical_evidence_fingerprint([ev_low])
        fp_h = canonical_evidence_fingerprint([ev_high])
        bs = [
            _branch(branch_id="b1", evidence=(ev_low,), fingerprint=fp_l),
            _branch(branch_id="b2", evidence=(ev_low,), fingerprint=fp_l),
            _branch(branch_id="b3", evidence=(ev_high,), fingerprint=fp_h),
        ]
        assert compute_tree_verdict(bs) is TreeVerdict.INCONCLUSIVE

    def test_explicit_min_confidence_override(self):
        ev = _ev(confidence=0.4)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        # Default 0.5 would inconclusive; override 0.3 → converged
        assert compute_tree_verdict(
            bs, min_confidence=0.3,
        ) is TreeVerdict.CONVERGED

    def test_mixed_garbage_filtered(self):
        ev = _ev(confidence=0.9)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            "not a branch",
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            42,
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        # Garbage filtered; 2 success same fp → CONVERGED
        assert compute_tree_verdict(bs) is TreeVerdict.CONVERGED  # type: ignore


# ---------------------------------------------------------------------------
# TestComputeTreeOutcome — top-level outcome resolution
# ---------------------------------------------------------------------------


class TestComputeTreeOutcome:

    def test_master_off_returns_failed_with_detail(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        result = compute_tree_outcome(_target(), [_branch()])
        assert result.outcome is TreeVerdict.FAILED
        assert "JARVIS_SBT_ENABLED" in result.detail

    def test_enabled_override_true_engages(self):
        ev = _ev(confidence=0.9)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        result = compute_tree_outcome(
            _target(), bs, enabled_override=True,
        )
        assert result.outcome is TreeVerdict.CONVERGED

    def test_enabled_override_false_returns_failed(self):
        result = compute_tree_outcome(
            _target(), [_branch()], enabled_override=False,
        )
        assert result.outcome is TreeVerdict.FAILED

    def test_garbage_target_returns_failed(self):
        result = compute_tree_outcome(
            "not a target", [_branch()],  # type: ignore
            enabled_override=True,
        )
        assert result.outcome is TreeVerdict.FAILED
        assert "BranchTreeTarget" in result.detail

    def test_empty_branches_returns_failed(self):
        result = compute_tree_outcome(
            _target(), [], enabled_override=True,
        )
        assert result.outcome is TreeVerdict.FAILED

    def test_winning_branch_index_set_on_converged(self):
        ev = _ev(confidence=0.9)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        result = compute_tree_outcome(
            _target(), bs, enabled_override=True,
        )
        assert result.winning_branch_idx is not None
        assert result.winning_fingerprint == fp
        assert result.aggregate_confidence > 0.5

    def test_no_winner_on_diverged(self):
        ev_a = _ev(content_hash="A", confidence=0.9)
        ev_b = _ev(content_hash="B", confidence=0.9)
        fp_a = canonical_evidence_fingerprint([ev_a])
        fp_b = canonical_evidence_fingerprint([ev_b])
        bs = [
            _branch(branch_id="b1", evidence=(ev_a,), fingerprint=fp_a),
            _branch(branch_id="b2", evidence=(ev_b,), fingerprint=fp_b),
        ]
        result = compute_tree_outcome(
            _target(), bs, enabled_override=True,
        )
        assert result.outcome is TreeVerdict.DIVERGED
        assert result.winning_branch_idx is None
        assert result.winning_fingerprint == ""

    def test_winner_picks_highest_confidence_in_group(self):
        ev_a = _ev(content_hash="A", confidence=0.6)
        ev_a_strong = _ev(content_hash="A", confidence=0.95)
        fp = canonical_evidence_fingerprint([ev_a])
        # All same fingerprint, but b2 has stronger evidence
        bs = [
            _branch(branch_id="b1", evidence=(ev_a,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev_a_strong,), fingerprint=fp),
            _branch(branch_id="b3", evidence=(ev_a,), fingerprint=fp),
        ]
        result = compute_tree_outcome(
            _target(), bs, enabled_override=True,
        )
        # Winner is b2 (idx 1) because highest confidence in winning group
        assert result.winning_branch_idx == 1

    def test_detail_token_shape(self):
        ev = _ev(confidence=0.9)
        fp = canonical_evidence_fingerprint([ev])
        bs = [
            _branch(branch_id="b1", evidence=(ev,), fingerprint=fp),
            _branch(branch_id="b2", evidence=(ev,), fingerprint=fp),
        ]
        result = compute_tree_outcome(
            _target(), bs, enabled_override=True,
        )
        for token in (
            "verdict=converged", "branches=2", "success=2",
            "winner_idx=", "winner_fp=", "agg_conf=",
        ):
            assert token in result.detail


# ---------------------------------------------------------------------------
# TestVerdictHelpers
# ---------------------------------------------------------------------------


class TestVerdictHelpers:

    def test_is_actionable_converged(self):
        r = TreeVerdictResult(
            outcome=TreeVerdict.CONVERGED, winning_branch_idx=0,
        )
        assert r.is_actionable() is True

    def test_is_actionable_no_winner_idx(self):
        r = TreeVerdictResult(
            outcome=TreeVerdict.CONVERGED, winning_branch_idx=None,
        )
        assert r.is_actionable() is False

    @pytest.mark.parametrize(
        "outcome",
        [
            TreeVerdict.DIVERGED, TreeVerdict.INCONCLUSIVE,
            TreeVerdict.TRUNCATED, TreeVerdict.FAILED,
        ],
    )
    def test_is_actionable_only_converged(self, outcome):
        r = TreeVerdictResult(outcome=outcome, winning_branch_idx=0)
        assert r.is_actionable() is False

    def test_has_disagreement_signal_diverged_only(self):
        for outcome in TreeVerdict:
            r = TreeVerdictResult(outcome=outcome)
            expected = outcome is TreeVerdict.DIVERGED
            assert r.has_disagreement_signal() is expected


# ---------------------------------------------------------------------------
# TestDefensiveContract
# ---------------------------------------------------------------------------


class TestDefensiveContract:

    def test_compute_tree_outcome_never_raises(self):
        for inp in (None, 42, [], "string", object()):
            r = compute_tree_outcome(
                inp, [_branch()],  # type: ignore
                enabled_override=True,
            )
            assert isinstance(r, TreeVerdictResult)

    def test_compute_tree_verdict_never_raises(self):
        for inp in (None, 42, "string"):
            v = compute_tree_verdict(inp)  # type: ignore
            assert isinstance(v, TreeVerdict)

    def test_canonical_fingerprint_never_raises(self):
        for inp in (None, 42, "string", [object()]):
            fp = canonical_evidence_fingerprint(inp)  # type: ignore
            assert isinstance(fp, str)

    def test_branch_evidence_average_confidence_garbage(self):
        # Build a result with broken evidence — average should
        # still return a float
        b = BranchResult(
            branch_id="x", outcome=BranchOutcome.SUCCESS,
        )
        assert b.average_confidence() == 0.0


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants — AST-level pin
# ---------------------------------------------------------------------------


_SBT_PATH = Path(sbt_mod.__file__)


def _module_source() -> str:
    return _SBT_PATH.read_text()


def _module_ast() -> ast.AST:
    return ast.parse(_module_source())


_BANNED_IMPORT_SUBSTRINGS = (
    ".providers", "doubleword_provider", "urgency_router",
    "candidate_generator", "orchestrator", "tool_executor",
    "phase_runner", "iron_gate", "change_engine",
    "auto_action_router", "subagent_scheduler",
    "semantic_guardian", "semantic_firewall", "risk_engine",
)


class TestCostContractAuthorityInvariants:

    def test_no_banned_imports(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in _BANNED_IMPORT_SUBSTRINGS:
                        assert banned not in alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for banned in _BANNED_IMPORT_SUBSTRINGS:
                    assert banned not in module

    def test_no_governance_imports_pure_stdlib(self):
        """Slice 1 primitive MUST be pure-stdlib — strongest
        authority invariant. Mirrors Priority #1/#2/#3 Slice 1
        discipline."""
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert "backend." not in module, (
                    f"primitive must be pure-stdlib — found {module!r}"
                )
                assert "governance" not in module, (
                    f"primitive must be pure-stdlib — found {module!r}"
                )

    def test_no_eval_family_calls(self):
        src = _module_source()
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in ("exec", "eval", "compile")
        for token in _FORBIDDEN_CALL_TOKENS:
            assert token not in src

    def test_no_subprocess_or_os_system(self):
        src = _module_source()
        assert "subprocess" not in src
        assert "os." + "system" not in src

    def test_no_async_functions(self):
        """Slice 1 is sync; Slice 2 wraps via asyncio.gather +
        to_thread."""
        tree = _module_ast()
        for node in ast.walk(tree):
            assert not isinstance(node, ast.AsyncFunctionDef), (
                f"forbidden async function: "
                f"{getattr(node, 'name', '?')}"
            )

    def test_no_mutation_calls(self):
        """AST walk: no shutil.rmtree / os.remove / os.unlink call
        sites (substring scan would false-positive on docstrings)."""
        tree = _module_ast()
        forbidden = {
            ("shutil", "rmtree"), ("os", "remove"), ("os", "unlink"),
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Attribute,
            ):
                if isinstance(node.func.value, ast.Name):
                    pair = (node.func.value.id, node.func.attr)
                    assert pair not in forbidden

    def test_public_api_exported(self):
        for name in sbt_mod.__all__:
            assert hasattr(sbt_mod, name), (
                f"sbt_mod.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_cost_contract_constant_present(self):
        assert hasattr(
            sbt_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
        )
        assert sbt_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_schema_version_constant(self):
        assert SBT_SCHEMA_VERSION == "speculative_branch.1"
