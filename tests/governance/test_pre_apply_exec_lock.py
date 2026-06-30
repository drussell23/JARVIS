from __future__ import annotations
import hashlib
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain, SandboxExecutionToken,
)

CANDIDATE = [("backend/x.py", "def f():\n    return 1\n")]

class _FakeResult:
    def __init__(self, exit_code, breached=False):
        self.exit_code = exit_code
        self.breached = breached
        self.diagnostic = "ok" if exit_code == 0 else "boom"

@pytest.mark.asyncio
async def test_exit_zero_mints_token():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(0)
    tok = await lock.acquire_sandbox_execution_token(
        op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
        chain=chain, docker_available=lambda: True, runner=runner)
    assert isinstance(tok, SandboxExecutionToken)
    assert tok.payload["exit_code"] == "0"
    # state_binding binds the EXACT candidate content
    expect = hashlib.sha256(b"backend/x.py\x00def f():\n    return 1\n").hexdigest()
    assert tok.state_binding == expect

@pytest.mark.asyncio
async def test_nonzero_exit_raises_sandbox_lock_failed():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(1)
    with pytest.raises(lock.SandboxLockFailed):
        await lock.acquire_sandbox_execution_token(
            op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
            chain=chain, docker_available=lambda: True, runner=runner)

@pytest.mark.asyncio
async def test_no_docker_raises_requires_cloud_execution_no_process_fallback():
    chain = DAGProofChain()
    async def runner(**_):
        raise AssertionError("runner must NOT be called without Docker")
    with pytest.raises(lock.RequiresCloudExecution):
        await lock.acquire_sandbox_execution_token(
            op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
            chain=chain, docker_available=lambda: False, runner=runner)
