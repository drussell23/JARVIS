from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import blast_radius_verify as brv
from backend.core.ouroboros.governance.dag_capability_token import (
    BlastRadiusClearedToken,
    DAGProofChain,
    TokenKind,
)

SCOPE = ["backend/x.py"]
PRE = "tree-sha-pre"


def _prev(chain: DAGProofChain):
    return chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION,
        op_id="op-1",
        state_binding="cand",
        payload={"exit_code": "0"},
    )


@pytest.mark.asyncio
async def test_all_pass_mints_chained_token():
    chain = DAGProofChain()
    prev = _prev(chain)

    async def graph_fn(files):
        return {"tests/test_x.py"}

    async def test_fn(tests):
        return {"failed": [], "total": 3}

    async def sha_fn():
        return PRE

    tok = await brv.acquire_blast_radius_token(
        op_id="op-1",
        scope_files=SCOPE,
        pre_op_tree_sha=PRE,
        chain=chain,
        prev_token=prev,
        graph_fn=graph_fn,
        test_fn=test_fn,
        current_tree_sha_fn=sha_fn,
        rollback_fn=None,
        dlq_fn=None,
    )
    assert isinstance(tok, BlastRadiusClearedToken)
    assert tok.prev_hash == prev.digest()  # chained to Gate 1
    assert tok.state_binding == PRE


@pytest.mark.asyncio
async def test_any_failure_rolls_back_and_asserts_sha_and_dlqs():
    chain = DAGProofChain()
    prev = _prev(chain)
    calls: dict = {"rollback": 0, "dlq": []}

    async def graph_fn(files):
        return {"tests/test_x.py"}

    async def test_fn(tests):
        return {"failed": ["tests/test_x.py::t"], "total": 3}

    async def rollback_fn(sha):
        calls["rollback"] += 1

    async def sha_fn():
        return PRE

    def dlq_fn(reason):
        calls["dlq"].append(reason)

    with pytest.raises(brv.BlastRadiusBreach):
        await brv.acquire_blast_radius_token(
            op_id="op-1",
            scope_files=SCOPE,
            pre_op_tree_sha=PRE,
            chain=chain,
            prev_token=prev,
            graph_fn=graph_fn,
            test_fn=test_fn,
            current_tree_sha_fn=sha_fn,
            rollback_fn=rollback_fn,
            dlq_fn=dlq_fn,
        )
    assert calls["rollback"] == 1
    assert calls["dlq"] == ["blast_radius_breach"]


@pytest.mark.asyncio
async def test_graph_failure_is_fail_closed_and_dlqs():
    chain = DAGProofChain()
    prev = _prev(chain)
    calls: dict = {"rollback": 0, "dlq": []}

    async def graph_fn(files):
        raise ValueError("cyclic")

    async def test_fn(tests):
        raise AssertionError("must not run tests on graph failure")

    async def rollback_fn(sha):
        calls["rollback"] += 1

    async def sha_fn():
        return PRE

    def dlq_fn(reason):
        calls["dlq"].append(reason)

    with pytest.raises(brv.BlastRadiusGraphFailure):
        await brv.acquire_blast_radius_token(
            op_id="op-1",
            scope_files=SCOPE,
            pre_op_tree_sha=PRE,
            chain=chain,
            prev_token=prev,
            graph_fn=graph_fn,
            test_fn=test_fn,
            current_tree_sha_fn=sha_fn,
            rollback_fn=rollback_fn,
            dlq_fn=dlq_fn,
        )
    assert calls["rollback"] == 1
    assert calls["dlq"] == ["blast_radius_graph_failure"]


@pytest.mark.asyncio
async def test_rollback_sha_mismatch_raises():
    chain = DAGProofChain()
    prev = _prev(chain)

    async def graph_fn(files):
        return {"tests/test_x.py"}

    async def test_fn(tests):
        return {"failed": ["t"], "total": 1}

    async def sha_fn():
        return "DIFFERENT"

    async def rollback_fn(sha):
        pass

    with pytest.raises(brv.BlastRadiusBreach):
        await brv.acquire_blast_radius_token(
            op_id="op-1",
            scope_files=SCOPE,
            pre_op_tree_sha=PRE,
            chain=chain,
            prev_token=prev,
            graph_fn=graph_fn,
            test_fn=test_fn,
            current_tree_sha_fn=sha_fn,
            rollback_fn=rollback_fn,
            dlq_fn=lambda r: None,
        )
