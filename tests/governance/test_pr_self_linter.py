from __future__ import annotations
import hashlib
import pytest
from backend.core.ouroboros.governance import pr_self_linter as lint
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain, TokenKind, LintClearedToken,
)

DIFF = "+def f():\n+    return 1\n"

def _prev(chain):
    s = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id="op-1", state_binding="c", payload={})
    return chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id="op-1",
                      state_binding="t", payload={}, prev=s)

@pytest.mark.asyncio
async def test_pass_mints_chained_lint_token():
    chain = DAGProofChain(); prev = _prev(chain)
    async def critique_fn(diff): return {"rating": 5, "concerns": []}
    tok = await lint.acquire_lint_cleared_token(
        op_id="op-1", diff=DIFF, chain=chain, prev_token=prev, critique_fn=critique_fn)
    assert isinstance(tok, LintClearedToken)
    assert tok.prev_hash == prev.digest()
    assert tok.state_binding == hashlib.sha256(DIFF.encode()).hexdigest()

@pytest.mark.asyncio
async def test_low_rating_raises_lint_rejected():
    chain = DAGProofChain(); prev = _prev(chain)
    async def critique_fn(diff): return {"rating": 2, "concerns": ["hardcoded path"]}
    with pytest.raises(lint.LintRejected):
        await lint.acquire_lint_cleared_token(
            op_id="op-1", diff=DIFF, chain=chain, prev_token=prev, critique_fn=critique_fn)

@pytest.mark.asyncio
async def test_malformed_rating_fails_closed():
    chain = DAGProofChain(); prev = _prev(chain)
    async def critique_none(diff): return {"rating": None}
    async def critique_garbage(diff): return {"rating": "five"}
    for cf in (critique_none, critique_garbage):
        with pytest.raises(lint.LintRejected):
            await lint.acquire_lint_cleared_token(
                op_id="op-1", diff=DIFF, chain=chain, prev_token=prev, critique_fn=cf)
