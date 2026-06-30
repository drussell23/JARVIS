from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.orange_pr_reviewer import OrangePRReviewer
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain,
    TokenKind,
)


def _chain_tokens(chain, op_id="op-1", branch_context=""):
    s = chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION, op_id=op_id, state_binding="c",
        payload={}, branch_context=branch_context,
    )
    b = chain.mint(
        kind=TokenKind.BLAST_RADIUS_CLEARED,
        op_id=op_id,
        state_binding="t",
        payload={},
        prev=s,
        branch_context=branch_context,
    )
    l = chain.mint(
        kind=TokenKind.LINT_CLEARED,
        op_id=op_id,
        state_binding="d",
        payload={},
        prev=b,
        branch_context=branch_context,
    )
    return s, b, l


@pytest.mark.asyncio
async def test_refuses_without_valid_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")  # don't trigger live linter
    rv = OrangePRReviewer(str(tmp_path))
    chain = DAGProofChain()
    s, b, l = _chain_tokens(chain)
    # Foreign-secret sandbox token -> verify_chain fails -> None (refuse), no git touched.
    foreign = DAGProofChain()
    s2, _, _ = _chain_tokens(foreign)
    result = await rv.create_review_pr(
        op_id="op-1",
        description="d",
        files=[("x.py", "y = 1\n")],
        chain=chain,
        sandbox_token=s2,
        blast_token=b,
        lint_token=l,
    )
    assert result is None


@pytest.mark.asyncio
async def test_enforcer_off_is_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false")
    rv = OrangePRReviewer(str(tmp_path))
    assert rv._enforcer_enabled() is False


@pytest.mark.asyncio
async def test_refuses_on_branch_context_mismatch(monkeypatch, tmp_path):
    """Tokens minted with branch_context='wt-A' must be refused when
    expected_branch_context='wt-B'."""
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")
    chain = DAGProofChain()
    s, b, l = _chain_tokens(chain, branch_context="wt-A")
    rv = OrangePRReviewer(str(tmp_path))
    result = await rv.create_review_pr(
        op_id="op-1",
        description="d",
        files=[("x.py", "y = 1\n")],
        chain=chain,
        sandbox_token=s,
        blast_token=b,
        lint_token=l,
        expected_branch_context="wt-B",
    )
    assert result is None


@pytest.mark.asyncio
async def test_matching_branch_context_passes_branch_check(monkeypatch, tmp_path):
    """Tokens minted with branch_context='wt-A' and expected='wt-A' pass the
    branch check (reaches git ops; returns None only due to no real git repo)."""
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")
    chain = DAGProofChain()
    s, b, l = _chain_tokens(chain, branch_context="wt-A")
    rv = OrangePRReviewer(str(tmp_path))
    # Inject a fake _run_git_sync that records calls and fails on rev-parse
    # (simulates no real git repo) so the test stays hermetic.
    calls: list = []

    def fake_git(args):
        calls.append(args)
        return 1, "", "not a git repo"

    rv._run_git_sync = fake_git
    result = await rv.create_review_pr(
        op_id="op-1",
        description="d",
        files=[("x.py", "y = 1\n")],
        chain=chain,
        sandbox_token=s,
        blast_token=b,
        lint_token=l,
        expected_branch_context="wt-A",
    )
    # Result is None (git failed), but git was ATTEMPTED — proving the
    # branch check was passed (not short-circuited by the mismatch guard).
    assert result is None
    assert any("rev-parse" in " ".join(c) for c in calls)
