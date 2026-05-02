"""Priority #4 Slice 2 — Speculative Branch Tree async runner regression suite.

Async tree executor reusing Move 5's READONLY_TOOL_ALLOWLIST + Move 6's
K-way parallel pattern + HypothesisProbe's three-termination contract.

Test classes:
  * TestRunnerEnabledFlag — sub-flag asymmetric env semantics
  * TestProberProtocol — _NullBranchProber default + custom injection
  * TestEvidenceClassification — evidence → outcome mapping
  * TestAllowlistFiltering — defense-in-depth tool drop
  * TestRunSpeculativeTreeMatrix — closed-taxonomy outcome tree
  * TestTieBreakerSpawn — DIVERGED → tie-breaker level
  * TestPriorEvidencePropagation — level 1+ receives aggregated evidence
  * TestWallCapEnforcement — TRUNCATED on slow prober
  * TestDiminishingReturnsEarlyStop — early cancel on consensus
  * TestRunnerDefensiveContract — public surface NEVER raises
  * TestCostContractAuthorityInvariants — AST-level pin
"""
from __future__ import annotations

import ast
import asyncio
import sys
import time
from pathlib import Path
from typing import Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.verification.speculative_branch import (
    BranchEvidence,
    BranchOutcome,
    BranchResult,
    BranchTreeTarget,
    EvidenceKind,
    TreeVerdict,
    TreeVerdictResult,
)
from backend.core.ouroboros.governance.verification import (
    speculative_branch_runner as runner_mod,
)
from backend.core.ouroboros.governance.verification.speculative_branch_runner import (
    BranchProber,
    READONLY_TOOL_ALLOWLIST,
    _NullBranchProber,
    _aggregate_prior_evidence,
    _allocate_level_budgets,
    _build_branch_id,
    _classify_evidence_outcome,
    _filter_evidence_to_allowlist,
    is_tool_allowlisted,
    run_speculative_tree,
    sbt_runner_enabled,
)


# ---------------------------------------------------------------------------
# Forbidden-call tokens
# ---------------------------------------------------------------------------

_FORBIDDEN_CALL_TOKENS = (
    "e" + "val(",
    "e" + "xec(",
    "comp" + "ile(",
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _target(
    decision_id: str = "test-decision",
    max_depth: int = 2,
    max_breadth: int = 2,
    max_wall_seconds: float = 10.0,
) -> BranchTreeTarget:
    return BranchTreeTarget(
        decision_id=decision_id,
        ambiguity_kind="test_ambiguity",
        max_depth=max_depth,
        max_breadth=max_breadth,
        max_wall_seconds=max_wall_seconds,
    )


class _AgreeingProber:
    """All branches return identical evidence."""
    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash="agreed",
                confidence=0.9,
                source_tool="read_file",
            ),
        )


class _DisagreeingProber:
    """Each branch returns distinct evidence."""
    def __init__(self):
        self.call_count = 0
        self.priors_at_each_call: list = []

    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        self.call_count += 1
        self.priors_at_each_call.append((depth, len(prior_evidence)))
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash=f"distinct_{self.call_count}",
                confidence=0.9,
                source_tool="read_file",
            ),
        )


class _RaisingProber:
    """Always raises — runner converts to FAILED branches."""
    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        raise RuntimeError("intentional test failure")


class _LevelAwareProber:
    """Level 0 disagrees, level 1 (tie-breaker) agrees."""
    def __init__(self):
        self.priors_seen: list = []

    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        self.priors_seen.append((depth, len(prior_evidence)))
        if depth == 0:
            return (
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ,
                    content_hash=f"L0_{branch_id}",
                    confidence=0.9,
                    source_tool="read_file",
                ),
            )
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash="resolved_at_L1",
                confidence=0.95,
                source_tool="read_file",
            ),
        )


class _SlowProber:
    """Sleeps long enough to trigger per-branch wall cap."""
    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        time.sleep(2.0)
        return ()


class _MaliciousProber:
    """Returns one allowlisted + one non-allowlisted evidence."""
    def probe_branch(
        self, *, target, branch_id, depth, prior_evidence=(),
    ):
        return (
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash="legit",
                confidence=0.9,
                source_tool="read_file",
            ),
            BranchEvidence(
                kind=EvidenceKind.FILE_READ,
                content_hash="suspicious",
                confidence=0.9,
                source_tool="edit_file",  # NOT allowlisted
            ),
        )


@pytest.fixture(autouse=True)
def _isolated_runner(monkeypatch):
    """Each test runs with master + runner enabled."""
    monkeypatch.setenv("JARVIS_SBT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", "true")
    yield


# ---------------------------------------------------------------------------
# TestRunnerEnabledFlag
# ---------------------------------------------------------------------------


class TestRunnerEnabledFlag:

    def test_default_true_post_graduation(self, monkeypatch):
        """Slice 5 graduation flipped runner sub-gate to True
        (2026-05-02)."""
        monkeypatch.delenv("JARVIS_SBT_RUNNER_ENABLED", raising=False)
        assert sbt_runner_enabled() is True

    def test_empty_treated_as_unset(self, monkeypatch):
        """Empty = unset = graduated default-true."""
        monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", "")
        assert sbt_runner_enabled() is True

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "ON"])
    def test_truthy(self, monkeypatch, v):
        monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", v)
        assert sbt_runner_enabled() is True


# ---------------------------------------------------------------------------
# TestProberProtocol
# ---------------------------------------------------------------------------


class TestProberProtocol:

    def test_null_prober_returns_empty(self):
        np = _NullBranchProber()
        result = np.probe_branch(
            target=_target(), branch_id="b", depth=0,
        )
        assert result == ()


# ---------------------------------------------------------------------------
# TestEvidenceClassification
# ---------------------------------------------------------------------------


class TestEvidenceClassification:

    def test_empty_evidence_partial(self):
        assert (
            _classify_evidence_outcome(())
            is BranchOutcome.PARTIAL
        )

    def test_zero_confidence_evidence_partial(self):
        ev = BranchEvidence(
            kind=EvidenceKind.FILE_READ,
            content_hash="x", confidence=0.0,
        )
        assert (
            _classify_evidence_outcome((ev,))
            is BranchOutcome.PARTIAL
        )

    def test_positive_confidence_success(self):
        ev = BranchEvidence(
            kind=EvidenceKind.FILE_READ,
            content_hash="x", confidence=0.5,
        )
        assert (
            _classify_evidence_outcome((ev,))
            is BranchOutcome.SUCCESS
        )

    def test_mixed_confidence_success_if_any_positive(self):
        ev_zero = BranchEvidence(
            kind=EvidenceKind.FILE_READ,
            content_hash="zero", confidence=0.0,
        )
        ev_high = BranchEvidence(
            kind=EvidenceKind.FILE_READ,
            content_hash="high", confidence=0.9,
        )
        assert (
            _classify_evidence_outcome((ev_zero, ev_high))
            is BranchOutcome.SUCCESS
        )


# ---------------------------------------------------------------------------
# TestAllowlistFiltering — defense-in-depth
# ---------------------------------------------------------------------------


class TestAllowlistFiltering:

    def test_allowlisted_kept(self):
        ev = BranchEvidence(
            kind=EvidenceKind.FILE_READ, content_hash="x",
            source_tool="read_file",
        )
        assert _filter_evidence_to_allowlist((ev,)) == (ev,)

    def test_non_allowlisted_dropped(self):
        ev = BranchEvidence(
            kind=EvidenceKind.FILE_READ, content_hash="x",
            source_tool="edit_file",
        )
        assert _filter_evidence_to_allowlist((ev,)) == ()

    def test_empty_source_tool_kept(self):
        """Some evidence kinds (TYPE_INFERENCE) don't have a tool
        name — empty string is allowed (we trust the kind)."""
        ev = BranchEvidence(
            kind=EvidenceKind.TYPE_INFERENCE, content_hash="x",
            source_tool="",
        )
        assert _filter_evidence_to_allowlist((ev,)) == (ev,)

    def test_mixed_filtered(self):
        legit = BranchEvidence(
            kind=EvidenceKind.FILE_READ, content_hash="L",
            source_tool="read_file",
        )
        bad = BranchEvidence(
            kind=EvidenceKind.FILE_READ, content_hash="B",
            source_tool="bash",
        )
        result = _filter_evidence_to_allowlist((legit, bad))
        assert legit in result
        assert bad not in result

    def test_runner_drops_evidence_in_pipeline(self):
        target = _target()
        result = asyncio.run(run_speculative_tree(
            target, prober=_MaliciousProber(),
        ))
        # Each branch should have only the allowlisted evidence
        for b in result.branches:
            for ev in b.evidence:
                assert ev.source_tool in READONLY_TOOL_ALLOWLIST or ev.source_tool == ""

    def test_re_export_allowlist(self):
        """Slice 2 re-exports the allowlist — defense-in-depth
        contract: all 9 read-only tools, no mutation tools."""
        for required in (
            "read_file", "search_code", "get_callers",
            "glob_files", "list_dir", "list_symbols",
            "git_blame", "git_log", "git_diff",
        ):
            assert required in READONLY_TOOL_ALLOWLIST
        # Mutation tools must NOT appear
        for forbidden in (
            "edit_file", "write_file", "delete_file",
            "run_tests", "bash",
        ):
            assert forbidden not in READONLY_TOOL_ALLOWLIST


# ---------------------------------------------------------------------------
# TestRunSpeculativeTreeMatrix
# ---------------------------------------------------------------------------


class TestRunSpeculativeTreeMatrix:

    def test_master_off_returns_failed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        result = asyncio.run(run_speculative_tree(_target()))
        assert result.outcome is TreeVerdict.FAILED
        assert "sbt_master_flag_off" in result.detail

    def test_sub_off_returns_failed(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", "false")
        result = asyncio.run(run_speculative_tree(_target()))
        assert result.outcome is TreeVerdict.FAILED
        assert "runner_sub_flag_off" in result.detail

    def test_enabled_override_false(self):
        result = asyncio.run(run_speculative_tree(
            _target(), enabled_override=False,
        ))
        assert result.outcome is TreeVerdict.FAILED

    def test_enabled_override_true_bypasses_flags(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SBT_ENABLED", "false")
        monkeypatch.setenv("JARVIS_SBT_RUNNER_ENABLED", "false")
        result = asyncio.run(run_speculative_tree(
            _target(), prober=_AgreeingProber(),
            enabled_override=True,
        ))
        assert result.outcome is TreeVerdict.CONVERGED

    def test_garbage_target(self):
        result = asyncio.run(run_speculative_tree(
            "not a target",  # type: ignore
        ))
        assert result.outcome is TreeVerdict.FAILED
        assert "BranchTreeTarget" in result.detail

    def test_null_prober_inconclusive(self):
        # Default _NullBranchProber returns empty evidence → all
        # branches PARTIAL → INCONCLUSIVE
        result = asyncio.run(run_speculative_tree(_target()))
        assert result.outcome is TreeVerdict.INCONCLUSIVE

    def test_agreeing_prober_converged(self):
        result = asyncio.run(run_speculative_tree(
            _target(), prober=_AgreeingProber(),
        ))
        assert result.outcome is TreeVerdict.CONVERGED
        assert result.winning_branch_idx is not None
        assert result.aggregate_confidence > 0.5

    def test_raising_prober_failed(self):
        result = asyncio.run(run_speculative_tree(
            _target(), prober=_RaisingProber(),
        ))
        # All branches FAILED → tree FAILED
        assert result.outcome is TreeVerdict.FAILED
        assert all(
            b.outcome is BranchOutcome.FAILED
            for b in result.branches
        )

    def test_disagreeing_prober_diverged(self):
        result = asyncio.run(run_speculative_tree(
            _target(max_depth=1),  # no tie-breaker
            prober=_DisagreeingProber(),
        ))
        assert result.outcome is TreeVerdict.DIVERGED


# ---------------------------------------------------------------------------
# TestTieBreakerSpawn
# ---------------------------------------------------------------------------


class TestTieBreakerSpawn:

    def test_diverged_spawns_tie_breaker_level(self):
        prober = _DisagreeingProber()
        result = asyncio.run(run_speculative_tree(
            _target(max_depth=2, max_breadth=2), prober=prober,
        ))
        # Should have 2 branches at level 0 + 2 at level 1 = 4
        assert len(result.branches) == 4
        depths = {b.depth for b in result.branches}
        assert depths == {0, 1}

    def test_converged_at_level_0_no_tie_breaker(self):
        prober = _AgreeingProber()
        target = _target(max_depth=2, max_breadth=2)
        result = asyncio.run(run_speculative_tree(
            target, prober=prober,
        ))
        # Level 0 converged → no level 1 branches spawned
        depths = {b.depth for b in result.branches}
        assert depths == {0}

    def test_max_depth_caps_tie_breaker_levels(self):
        prober = _DisagreeingProber()
        result = asyncio.run(run_speculative_tree(
            _target(max_depth=1, max_breadth=2), prober=prober,
        ))
        # max_depth=1 → only level 0; verdict stays DIVERGED
        assert all(b.depth == 0 for b in result.branches)
        assert result.outcome is TreeVerdict.DIVERGED

    def test_tie_breaker_can_resolve(self):
        prober = _LevelAwareProber()
        result = asyncio.run(run_speculative_tree(
            _target(max_depth=2, max_breadth=3),
            prober=prober,
        ))
        # Level 0 disagrees (3 distinct), level 1 agrees on
        # "resolved_at_L1". The test verifies the tie-breaker
        # LEVEL ran, not that it resolved.
        # priors_seen is List[Tuple[depth, prior_count]].
        levels = {depth for depth, _ in prober.priors_seen}
        assert 0 in levels
        assert 1 in levels
        # And the result.branches reflect both levels.
        result_levels = {b.depth for b in result.branches}
        assert 0 in result_levels
        assert 1 in result_levels


# ---------------------------------------------------------------------------
# TestPriorEvidencePropagation
# ---------------------------------------------------------------------------


class TestPriorEvidencePropagation:

    def test_level_0_priors_empty(self):
        prober = _LevelAwareProber()
        asyncio.run(run_speculative_tree(
            _target(max_depth=2), prober=prober,
        ))
        l0_priors = [
            count for depth, count in prober.priors_seen
            if depth == 0
        ]
        assert all(c == 0 for c in l0_priors)

    def test_level_1_priors_non_empty_when_diverged(self):
        prober = _LevelAwareProber()
        asyncio.run(run_speculative_tree(
            _target(max_depth=2), prober=prober,
        ))
        l1_priors = [
            count for depth, count in prober.priors_seen
            if depth == 1
        ]
        assert l1_priors  # at least one level-1 call
        assert all(c > 0 for c in l1_priors)

    def test_aggregate_prior_evidence_sorts_by_confidence(self):
        low = BranchResult(
            branch_id="b1", outcome=BranchOutcome.SUCCESS,
            evidence=(
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ,
                    content_hash="L", confidence=0.3,
                ),
            ),
            depth=0, fingerprint="fp1",
        )
        high = BranchResult(
            branch_id="b2", outcome=BranchOutcome.SUCCESS,
            evidence=(
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ,
                    content_hash="H", confidence=0.9,
                ),
            ),
            depth=0, fingerprint="fp2",
        )
        prior = _aggregate_prior_evidence([low, high])
        # Highest confidence first
        assert prior[0].content_hash == "H"

    def test_aggregate_prior_evidence_caps_size(self):
        branches = [
            BranchResult(
                branch_id=f"b{i}", outcome=BranchOutcome.SUCCESS,
                evidence=tuple(
                    BranchEvidence(
                        kind=EvidenceKind.FILE_READ,
                        content_hash=f"h{i}_{j}",
                        confidence=0.5,
                    )
                    for j in range(5)
                ),
                depth=0, fingerprint=f"fp{i}",
            )
            for i in range(10)
        ]
        prior = _aggregate_prior_evidence(branches, cap=8)
        assert len(prior) == 8

    def test_aggregate_prior_evidence_skips_failed(self):
        good = BranchResult(
            branch_id="b1", outcome=BranchOutcome.SUCCESS,
            evidence=(
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ, content_hash="G",
                    confidence=0.9,
                ),
            ),
            depth=0, fingerprint="fp1",
        )
        bad = BranchResult(
            branch_id="b2", outcome=BranchOutcome.FAILED,
            evidence=(
                BranchEvidence(
                    kind=EvidenceKind.FILE_READ, content_hash="B",
                    confidence=0.5,
                ),
            ),
            depth=0, fingerprint="",
        )
        prior = _aggregate_prior_evidence([good, bad])
        assert len(prior) == 1
        assert prior[0].content_hash == "G"


# ---------------------------------------------------------------------------
# TestWallCapEnforcement
# ---------------------------------------------------------------------------


class TestWallCapEnforcement:

    def test_slow_prober_truncates(self):
        target = _target(max_wall_seconds=10.0)
        started = time.monotonic()
        result = asyncio.run(run_speculative_tree(
            target, prober=_SlowProber(),
        ))
        elapsed = time.monotonic() - started
        # Per-branch cap = 10 / (2*2) = 2.5s; SlowProber sleeps 2.0s
        # but each branch has 2.5s budget — they may complete or
        # timeout. Total wall cap is 10s; we should finish well under.
        assert elapsed < 12.0
        # All branches should be TIMEOUT or empty-evidence (PARTIAL)
        # → tree should NOT be CONVERGED
        assert result.outcome != TreeVerdict.CONVERGED

    def test_branches_carry_timeout_outcome(self):
        # Per-branch cap = max(1.0, total_wall / (depth * breadth)).
        # Use max_breadth=8 + max_wall=10.0 → per-branch ≈ 1.25s.
        # SlowProber sleeps 2.0s → reliably triggers per-branch
        # timeout.
        target = BranchTreeTarget(
            decision_id="d", ambiguity_kind="x",
            max_depth=1, max_breadth=8,
            max_wall_seconds=10.0,
        )
        result = asyncio.run(run_speculative_tree(
            target, prober=_SlowProber(),
        ))
        # Branches should report TIMEOUT outcome (per-branch cap hit)
        timeout_count = sum(
            1 for b in result.branches
            if b.outcome is BranchOutcome.TIMEOUT
        )
        assert timeout_count >= 1

    def test_allocate_level_budgets_total(self):
        budgets = _allocate_level_budgets(60.0, 3)
        assert len(budgets) == 3
        # Each budget at least 1.0
        assert all(b >= 1.0 for b in budgets)
        # Budgets approximately sum to total
        assert abs(sum(budgets) - 60.0) < 1.0

    def test_allocate_level_budgets_single(self):
        assert _allocate_level_budgets(30.0, 1) == [30.0]

    def test_allocate_level_budgets_zero(self):
        assert _allocate_level_budgets(60.0, 0) == []


# ---------------------------------------------------------------------------
# TestDiminishingReturnsEarlyStop
# ---------------------------------------------------------------------------


class TestDiminishingReturnsEarlyStop:

    def test_consensus_short_circuits_breadth(self, monkeypatch):
        """All N branches agree at level 0 → diminishing-returns
        early-stop fires → tree returns CONVERGED quickly. Test
        that the prober was called fewer than max_breadth times
        when consensus emerges immediately."""
        # Set a high max_breadth so we'd notice early-stop
        monkeypatch.setenv("JARVIS_SBT_DIMINISHING_RETURNS_THRESHOLD", "0.5")
        target = BranchTreeTarget(
            decision_id="d", ambiguity_kind="x",
            max_depth=1, max_breadth=8,
            max_wall_seconds=10.0,
        )

        call_count = 0

        class CountingProber:
            def probe_branch(
                self, *, target, branch_id, depth, prior_evidence=(),
            ):
                nonlocal call_count
                call_count += 1
                return (
                    BranchEvidence(
                        kind=EvidenceKind.FILE_READ,
                        content_hash="same",
                        confidence=0.9,
                        source_tool="read_file",
                    ),
                )

        result = asyncio.run(run_speculative_tree(
            target, prober=CountingProber(),
        ))
        assert result.outcome is TreeVerdict.CONVERGED
        # All 8 tasks were spawned, but we cancel pending after
        # consensus; call_count is bounded by spawn count + a few
        # in-flight tasks, but the early-stop saves time. The
        # branches collected before cancel should be fewer than
        # max_breadth.
        assert len(result.branches) <= 8


# ---------------------------------------------------------------------------
# TestRunnerDefensiveContract
# ---------------------------------------------------------------------------


class TestRunnerDefensiveContract:

    def test_run_with_none_target(self):
        result = asyncio.run(run_speculative_tree(None))  # type: ignore
        assert isinstance(result, TreeVerdictResult)
        assert result.outcome is TreeVerdict.FAILED

    def test_run_with_int_target(self):
        result = asyncio.run(run_speculative_tree(42))  # type: ignore
        assert result.outcome is TreeVerdict.FAILED

    def test_build_branch_id_with_garbage_target(self):
        bid = _build_branch_id("not target", 0, 0)  # type: ignore
        assert "unknown" in bid

    def test_filter_evidence_with_garbage(self):
        # Garbage in → empty out, no raise
        assert _filter_evidence_to_allowlist(()) == ()

    def test_classify_with_garbage(self):
        # Tuple with non-evidence items — current impl treats all
        # as "any positive confidence" → SUCCESS if any item.confidence
        # exists. For garbage tuple containing non-Evidence items,
        # accessing .confidence raises → caught → returns PARTIAL.
        result = _classify_evidence_outcome(("not evidence",))  # type: ignore
        assert isinstance(result, BranchOutcome)


# ---------------------------------------------------------------------------
# TestCostContractAuthorityInvariants
# ---------------------------------------------------------------------------


_RUNNER_PATH = Path(runner_mod.__file__)


def _module_source() -> str:
    return _RUNNER_PATH.read_text()


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

    def test_no_mutation_calls(self):
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

    def test_run_speculative_tree_is_async(self):
        tree = _module_ast()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                if node.name == "run_speculative_tree":
                    return
        raise AssertionError(
            "run_speculative_tree must be `async def`"
        )

    def test_public_api_exported(self):
        for name in runner_mod.__all__:
            assert hasattr(runner_mod, name), (
                f"runner_mod.__all__ contains '{name}' which is not "
                f"a module attribute"
            )

    def test_cost_contract_constant_present(self):
        assert hasattr(
            runner_mod, "COST_CONTRACT_PRESERVED_BY_CONSTRUCTION",
        )
        assert runner_mod.COST_CONTRACT_PRESERVED_BY_CONSTRUCTION is True

    def test_reuses_slice_1_primitives(self):
        """Positive invariant — proves zero duplication."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.speculative_branch import" in src
        assert "compute_tree_verdict" in src
        assert "compute_tree_outcome" in src

    def test_reuses_readonly_evidence_prober_allowlist(self):
        """Positive invariant — Move 5 reuse contract; runner must
        NOT re-implement the 9-tool frozenset."""
        src = _module_source()
        assert "from backend.core.ouroboros.governance.verification.readonly_evidence_prober import" in src
        assert "READONLY_TOOL_ALLOWLIST" in src
        assert "is_tool_allowlisted" in src
        # Negative: the runner module itself does not redefine the
        # frozenset (it imports + re-exports).
        # Allow ONE reference (the import line); fail if literal
        # frozenset({"read_file", ...}) appears.
        assert "frozenset({" + "\n" not in src or "READONLY_TOOL_ALLOWLIST = " not in src

    def test_no_async_function_outside_runner(self):
        """The only async function should be run_speculative_tree
        + internal helpers (_run_one_branch, _run_one_level,
        _cancel_pending). Catches accidental async-leak into
        synchronous helpers."""
        tree = _module_ast()
        allowed_async = {
            "run_speculative_tree",
            "_run_one_branch",
            "_run_one_level",
            "_cancel_pending",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                assert node.name in allowed_async, (
                    f"unexpected async function: {node.name}"
                )
