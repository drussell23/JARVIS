from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.orange_pr_reviewer import OrangePRReviewer
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain,
    TokenKind,
)


def _mint_override(chain, op_id="op-1"):
    return chain.mint(
        kind=TokenKind.HUMAN_OVERRIDE, op_id=op_id,
        state_binding="non_autonomous",
        payload={"caller": "test", "reason": "manual"},
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


# ---------------------------------------------------------------------------
# Task 16: HumanOverrideToken -- the SECOND cryptographic path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_override_token_authorizes(monkeypatch, tmp_path):
    """Enforcer ON + a valid, signed HumanOverrideToken on the same chain with
    a matching op_id passes the enforcer and REACHES git (proving the override
    path authorized the PR without any autonomous chain)."""
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")
    chain = DAGProofChain()
    override = _mint_override(chain, op_id="op-1")
    rv = OrangePRReviewer(str(tmp_path))
    calls: list = []

    def fake_git(args):
        calls.append(args)
        return 1, "", "not a git repo"  # fail rev-parse -> hermetic, no real git.

    rv._run_git_sync = fake_git
    result = await rv.create_review_pr(
        op_id="op-1",
        description="d",
        files=[("x.py", "y = 1\n")],
        chain=chain,
        override_token=override,
    )
    # None only because fake git fails — but git was ATTEMPTED, i.e. the
    # enforcer authorized via the override and did NOT short-circuit.
    assert result is None
    assert any("rev-parse" in " ".join(c) for c in calls)


@pytest.mark.asyncio
async def test_invalid_override_refused(monkeypatch, tmp_path):
    """Forged override, wrong op_id, and wrong-type token are each refused at
    the enforcer (None) WITHOUT touching git (polymorphism enforced)."""
    import dataclasses

    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")
    rv = OrangePRReviewer(str(tmp_path))
    calls: list = []

    def fake_git(args):
        calls.append(args)
        return 1, "", "not a git repo"

    rv._run_git_sync = fake_git

    chain = DAGProofChain()

    # (a) Forged signature.
    forged = dataclasses.replace(_mint_override(chain, op_id="op-1"), sig="dead" * 16)
    assert await rv.create_review_pr(
        op_id="op-1", description="d", files=[("x.py", "y = 1\n")],
        chain=chain, override_token=forged,
    ) is None

    # (b) Genuine override but op_id mismatch.
    wrong_op = _mint_override(chain, op_id="other-op")
    assert await rv.create_review_pr(
        op_id="op-1", description="d", files=[("x.py", "y = 1\n")],
        chain=chain, override_token=wrong_op,
    ) is None

    # (c) Wrong-type token passed as override (a SandboxExecutionToken) ->
    #     isinstance(HumanOverrideToken) is False -> refuse.
    sandbox = chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION, op_id="op-1",
        state_binding="c", payload={},
    )
    assert await rv.create_review_pr(
        op_id="op-1", description="d", files=[("x.py", "y = 1\n")],
        chain=chain, override_token=sandbox,
    ) is None

    # No git was ever attempted on a refusal -> enforcer refused cheaply.
    assert not any("rev-parse" in " ".join(c) for c in calls)


@pytest.mark.asyncio
async def test_autonomous_path_still_required_without_override(monkeypatch, tmp_path):
    """No override + an incomplete autonomous chain still refuses (the override
    path did NOT weaken the autonomous lock)."""
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_A1_PR_LINTER_ENABLED", "false")
    rv = OrangePRReviewer(str(tmp_path))
    chain = DAGProofChain()
    s, b, l = _chain_tokens(chain)
    # Omit the lint token entirely: no override, incomplete chain -> refuse.
    result = await rv.create_review_pr(
        op_id="op-1",
        description="d",
        files=[("x.py", "y = 1\n")],
        chain=chain,
        sandbox_token=s,
        blast_token=b,
        lint_token=None,
    )
    assert result is None
