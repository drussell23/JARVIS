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

@pytest.mark.asyncio
async def test_containment_breach_raises_sandbox_lock_failed():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(0, breached=True)
    with pytest.raises(lock.SandboxLockFailed):
        await lock.acquire_sandbox_execution_token(
            op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
            chain=chain, docker_available=lambda: True, runner=runner)

@pytest.mark.asyncio
async def test_token_records_py_file_count():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(0)
    tok = await lock.acquire_sandbox_execution_token(
        op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
        chain=chain, docker_available=lambda: True, runner=runner)
    assert tok.payload["py_files"] == "1"

@pytest.mark.asyncio
async def test_branch_context_forwarded_to_token():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(0)
    tok = await lock.acquire_sandbox_execution_token(
        op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
        chain=chain, docker_available=lambda: True, runner=runner,
        branch_context="wt-1")
    assert tok.branch_context == "wt-1"

@pytest.mark.asyncio
async def test_runner_called_with_real_run_in_container_signature():
    """Pin the EXACT run_in_container signature -- no op_id, no **kwargs.

    If the production call ever re-adds op_id (or any unknown kwarg) this
    test TypeErrors, proving Gate 1 would crash on real Docker.
    """
    chain = DAGProofChain()
    captured = {}
    # Mirror container_sandbox.run_in_container's real signature exactly (no op_id).
    async def runner(code, *, worktree, image=None, policy=None,
                     seccomp_profile=None, docker_run=None, read_only=False):
        captured["worktree"] = worktree
        return _FakeResult(0)
    tok = await lock.acquire_sandbox_execution_token(
        op_id="op-sig", candidate_files=CANDIDATE, repo_root="/repo",
        chain=chain, docker_available=lambda: True, runner=runner)
    assert isinstance(tok, SandboxExecutionToken)
    assert captured["worktree"] == "/repo"  # call used real-signature kwargs only
