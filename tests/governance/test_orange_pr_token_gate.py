from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.orange_pr_reviewer import OrangePRReviewer
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain,
    TokenKind,
)


def _chain_tokens(chain, op_id="op-1"):
    s = chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION, op_id=op_id, state_binding="c", payload={}
    )
    b = chain.mint(
        kind=TokenKind.BLAST_RADIUS_CLEARED,
        op_id=op_id,
        state_binding="t",
        payload={},
        prev=s,
    )
    l = chain.mint(
        kind=TokenKind.LINT_CLEARED,
        op_id=op_id,
        state_binding="d",
        payload={},
        prev=b,
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
