"""Regression spine — read-only Advisor bypass + tool-layer enforcement.

Closes the graduation-test blocker found in Session 2 of the subagent
arc (bt-2026-04-18-020622): a cartography op with 50 importers and 0%
coverage was BLOCKED by OperationAdvisor at line 151 of
``operation_advisor.py`` before GENERATE could fire, which meant
``dispatch_subagent`` never had the chance to run.

Derek's directive (2026-04-17):
  * Option A (scope-hack the task) and Option C (env-bypass the
    Advisor) are both forbidden — we do not hack our own governance.
  * Option B is the only acceptable path: give the Advisor semantic
    intelligence about execution intent, backed by a cryptographic
    tool-layer guarantee that no mutation can occur.

This file tests that guarantee from both ends:
  1. Advisor skips blast_radius + test_coverage signals when
     is_read_only=True.
  2. Policy engine Rule 0d denies every mutation tool when
     policy_ctx.is_read_only=True — belt-and-suspenders with
     ScopedToolAccess's _MUTATION_TOOLS frozenset.
  3. Intent inference is deterministic (keyword-scan, no LLM) and
     conservative (positive signal AND no mutation verbs).
  4. OperationContext.with_read_only_intent() produces a proper
     hash-chained successor so the flag is part of the audit trail.
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance.operation_advisor import (
    AdvisoryDecision,
    OperationAdvisor,
    infer_read_only_intent,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.tool_executor import (
    GoverningToolPolicy,
    PolicyContext,
    PolicyDecision,
    ToolCall,
)


# ---------------------------------------------------------------------------
# 1. infer_read_only_intent — deterministic keyword scan
# ---------------------------------------------------------------------------


READ_ONLY_DESCRIPTIONS: Tuple[str, ...] = (
    "Execute a complete architectural mapping of the Trinity substrate. "
    "Do not mutate any code. This is a read-only architectural cartography task.",
    "Read-only audit of the governance layer. Do NOT write any source files.",
    "Build a gap analysis covering every subsystem boundary.",
    "Pure-exploration discovery across three distinct subsystems.",
    "Call graph survey — no code changes, documentation output only.",
    # ---- Regression: word-boundary collision cases -------------------
    # These tripped substring-match in the first Session-3 run
    # (bt-2026-04-18-032138) — "dispatch" contains "patch",
    # "implementation" contains "implement", "fixed" contains "fix".
    # The fix switched mutation-verb matching to \b word boundaries.
    "Read-only analysis. Dispatch parallel exploration subagents for "
    "cartography across three scopes.",
    "Cartography of the implementation surface — documentation only, "
    "do not mutate any code.",
    "Read-only gap analysis. No changes required to the fixed contract.",
)

MUTATING_DESCRIPTIONS: Tuple[str, ...] = (
    "Refactor the SubagentOrchestrator to split dispatch from merge.",
    "Fix the VALIDATE flake where iter=1 returns infra instead of test.",
    "Implement the CodeReviewerAgent parallel to VALIDATE.",
    "Add new BackgroundAgentPool knob for per-route concurrency.",
    "Rewrite the auto_committer to use signed commits.",
    "Remove the dead code path in orchestrator.py:4815.",
    # ---- Word-boundary correctness: these remain blocked even though
    #      the surrounding text contains read-only-looking words.
    "Read-only audit of X. Then refactor the dispatcher.",
    "Cartography first, then fix the flake.",
)

AMBIGUOUS_DESCRIPTIONS: Tuple[str, ...] = (
    "",
    "Trinity consciousness substrate",  # no positive signal
    "Read the memory_engine module",    # "read" alone is too weak
    "Survey and implement the fixes",   # positive + negative → False (conservative)
)


@pytest.mark.parametrize("desc", READ_ONLY_DESCRIPTIONS)
def test_infer_read_only_intent_positive(desc: str) -> None:
    assert infer_read_only_intent(desc) is True


@pytest.mark.parametrize("desc", MUTATING_DESCRIPTIONS)
def test_infer_read_only_intent_negative(desc: str) -> None:
    assert infer_read_only_intent(desc) is False


@pytest.mark.parametrize("desc", AMBIGUOUS_DESCRIPTIONS)
def test_infer_read_only_intent_ambiguous_defaults_false(desc: str) -> None:
    """Conservative: any ambiguity → False so normal risk gating applies."""
    assert infer_read_only_intent(desc) is False


# ---------------------------------------------------------------------------
# 2. Advisor bypass — the exact Session-2 scenario
# ---------------------------------------------------------------------------


class _HighBlastAdvisor(OperationAdvisor):
    """Test double — returns fixed blast_radius and test_coverage signals.

    Avoids the real AST walk (which is slow and depends on live repo
    state) so these tests are deterministic.
    """

    def __init__(self, blast_radius: int, test_coverage: float) -> None:
        super().__init__(Path("/tmp"))
        self._fake_blast = blast_radius
        self._fake_coverage = test_coverage

    def _compute_blast_radius(self, target_files: Tuple[str, ...]) -> int:
        return self._fake_blast

    def _compute_test_coverage(self, target_files: Tuple[str, ...]) -> float:
        return self._fake_coverage

    def _get_chronic_entropy(self, target_files: Tuple[str, ...], description: str) -> float:
        return 0.0

    def _check_staleness(self, target_files: Tuple[str, ...]):
        return []

    def _check_large_files(self, target_files: Tuple[str, ...]):
        return []


def test_advisor_blocks_mutating_op_with_session_2_profile() -> None:
    """Baseline — the BLOCK branch fires on the original Session 2 profile."""
    advisor = _HighBlastAdvisor(blast_radius=50, test_coverage=0.0)
    advisory = advisor.advise(
        target_files=(
            "backend/core/ouroboros/consciousness/memory_engine.py",
            "backend/core/ouroboros/governance/semantic_guardian.py",
            "backend/core/ouroboros/governance/ledger.py",
        ),
        description="Refactor the Trinity substrate to extract a shared base class.",
        op_id="op-test-mutating",
        is_read_only=False,
    )
    assert advisory.decision == AdvisoryDecision.BLOCK
    assert any(
        "Zero test coverage + extreme blast radius" in r for r in advisory.reasons
    )


def test_advisor_bypasses_block_for_read_only_op_with_session_2_profile() -> None:
    """Core regression — identical profile as Session 2, but is_read_only=True.

    The Advisor MUST NOT block. The blast_radius and coverage reasons
    must be suppressed. A positive "read-only bypass" reason must be
    surfaced for observability.
    """
    advisor = _HighBlastAdvisor(blast_radius=50, test_coverage=0.0)
    advisory = advisor.advise(
        target_files=(
            "backend/core/ouroboros/consciousness/memory_engine.py",
            "backend/core/ouroboros/governance/semantic_guardian.py",
            "backend/core/ouroboros/governance/ledger.py",
        ),
        description="Read-only cartography of the Trinity substrate.",
        op_id="op-test-readonly",
        is_read_only=True,
    )
    assert advisory.decision != AdvisoryDecision.BLOCK
    assert advisory.blast_radius == 50
    assert advisory.test_coverage == 0.0
    assert not any("High blast radius" in r for r in advisory.reasons)
    assert not any("Low test coverage" in r for r in advisory.reasons)
    assert any("Read-only op" in r for r in advisory.reasons)


def test_advisor_still_surfaces_orthogonal_signals_for_read_only() -> None:
    """Read-only ops still trigger stale-file / entropy reasons — those
    signals speak to generation quality, not blast radius."""
    advisor = _HighBlastAdvisor(blast_radius=50, test_coverage=0.0)
    # Override entropy to trigger that reason
    advisor._get_chronic_entropy = lambda tf, d: 0.7  # type: ignore[assignment]
    advisory = advisor.advise(
        target_files=("backend/core/ouroboros/consciousness/memory_engine.py",),
        description="Read-only gap analysis.",
        op_id="op-test-entropy",
        is_read_only=True,
    )
    assert any("chronic entropy" in r.lower() for r in advisory.reasons)


# ---------------------------------------------------------------------------
# 3. Tool-layer enforcement — policy engine Rule 0d
# ---------------------------------------------------------------------------


MUTATION_TOOLS_UNDER_TEST: Tuple[str, ...] = (
    "edit_file",
    "write_file",
    "delete_file",
    "bash",
)


def _make_policy_ctx(repo_root: Path, *, is_read_only: bool) -> PolicyContext:
    return PolicyContext(
        repo="jarvis",
        repo_root=repo_root,
        op_id="op-test",
        call_id="op-test:r0:edit_file",
        round_index=0,
        risk_tier=None,
        is_read_only=is_read_only,
    )


@pytest.fixture
def policy(tmp_path: Path) -> GoverningToolPolicy:
    # GoverningToolPolicy takes a repo-root map; single-repo is the
    # common-case wiring used in production.
    return GoverningToolPolicy(repo_roots={"jarvis": tmp_path})


@pytest.mark.parametrize("tool_name", MUTATION_TOOLS_UNDER_TEST)
def test_policy_denies_mutations_under_read_only_contract(
    policy: GoverningToolPolicy, tmp_path: Path, tool_name: str
) -> None:
    ctx = _make_policy_ctx(tmp_path, is_read_only=True)
    # Minimal valid args so we reach the read-only gate rather than
    # tripping a per-tool argument-shape rule first. For edit_file we
    # still hit Rule 0d before any path validation because 0d is listed
    # earlier in the rule ladder.
    args = {"path": "README.md", "content": "x", "command": "echo hi"}
    call = ToolCall(name=tool_name, arguments=args)
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.DENY
    assert result.reason_code == "tool.denied.read_only_operation"


def test_policy_allows_read_tools_under_read_only_contract(
    policy: GoverningToolPolicy, tmp_path: Path
) -> None:
    ctx = _make_policy_ctx(tmp_path, is_read_only=True)
    # Create a real file so read_file's path-resolution check passes
    target = tmp_path / "data.txt"
    target.write_text("hello")
    call = ToolCall(name="read_file", arguments={"path": "data.txt"})
    result = policy.evaluate(call, ctx)
    assert result.decision == PolicyDecision.ALLOW


@pytest.mark.parametrize("tool_name", MUTATION_TOOLS_UNDER_TEST)
def test_policy_permits_mutations_when_read_only_false(
    policy: GoverningToolPolicy, tmp_path: Path, tool_name: str
) -> None:
    """Baseline — is_read_only=False must not trigger Rule 0d."""
    ctx = _make_policy_ctx(tmp_path, is_read_only=False)
    # Build per-tool valid args so we don't get denied by a different rule.
    target = tmp_path / "f.py"
    target.write_text("x = 1\n")
    args_by_tool = {
        "edit_file": {
            "path": "f.py", "old_str": "x = 1", "new_str": "x = 2",
        },
        "write_file": {"path": "f.py", "content": "y = 2\n"},
        "delete_file": {"path": "f.py"},
        "bash": {"command": "echo hi"},
    }
    call = ToolCall(name=tool_name, arguments=args_by_tool[tool_name])
    result = policy.evaluate(call, ctx)
    # We don't assert ALLOW (some tools have stricter downstream rules),
    # but the denial must NOT be read_only_operation.
    if result.decision == PolicyDecision.DENY:
        assert result.reason_code != "tool.denied.read_only_operation"


# ---------------------------------------------------------------------------
# 4. OperationContext.with_read_only_intent — hash-chain successor
# ---------------------------------------------------------------------------


def test_with_read_only_intent_produces_hash_chained_successor() -> None:
    ctx = OperationContext.create(
        target_files=("backend/core/ouroboros/governance/ledger.py",),
        description="Read-only cartography.",
    )
    assert ctx.is_read_only is False
    ctx2 = ctx.with_read_only_intent(True)
    assert ctx2.is_read_only is True
    assert ctx2.previous_hash == ctx.context_hash
    assert ctx2.context_hash != ctx.context_hash
    # Phase should be unchanged — read-only stamping happens pre-CLASSIFY
    assert ctx2.phase == ctx.phase


def test_create_factory_accepts_is_read_only_directly() -> None:
    """Callers can create pre-stamped read-only contexts (useful for tests)."""
    ctx = OperationContext.create(
        target_files=("a.py",),
        description="anything",
        is_read_only=True,
    )
    assert ctx.is_read_only is True
