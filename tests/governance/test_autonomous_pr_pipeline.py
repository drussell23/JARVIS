from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import autonomous_pr_pipeline as app
from backend.core.ouroboros.governance.autonomous_pr_pipeline import (
    PRGatePipelineError,
    PRGateResult,
    pipeline_enabled,
    run_pr_gate_pipeline,
)
from backend.core.ouroboros.governance.blast_radius_verify import (
    acquire_blast_radius_token,
)
from backend.core.ouroboros.governance.dag_capability_token import (
    BlastRadiusClearedToken,
    DAGProofChain,
    SandboxExecutionToken,
    TokenKind,
)
from backend.core.ouroboros.governance.pre_apply_exec_lock import (
    RequiresCloudExecution,
    SandboxLockFailed,
)

OP_ID = "op-13b"
BRANCH = f"ouroboros/a1-validate/{OP_ID}"
CANDIDATE = [("backend/x.py", "x = 1\n")]


def _real_sandbox_gate(chain: DAGProofChain):
    """A seam that mints a REAL branch-bound SandboxExecutionToken on the chain
    (so the branch-bound chain genuinely chains + verifies)."""

    async def _gate(*, op_id, candidate_files, repo_root, chain, branch_context):
        return chain.mint(
            kind=TokenKind.SANDBOX_EXECUTION,
            op_id=op_id,
            state_binding="cand",
            payload={"exit_code": "0"},
            prev=None,
            branch_context=branch_context,
        )

    return _gate


@pytest.mark.asyncio
async def test_pipeline_success_assembles_chain(tmp_path):
    chain = DAGProofChain()
    cleanup_calls = []

    async def factory(branch):
        assert branch == BRANCH
        return tmp_path

    async def cleanup(wt):
        cleanup_calls.append(wt)

    async def apply_candidate(wt, files):
        return None

    async def graph_resolver(files, *, repo_root, oracle=None):
        return {"tests/test_x.py"}

    async def test_run_fn(tests, wt):
        return {"failed": [], "total": 1}

    async def tree_sha_fn(wt):
        return "sha"

    result = await run_pr_gate_pipeline(
        op_id=OP_ID,
        candidate_files=CANDIDATE,
        repo_root=str(tmp_path),
        chain=chain,
        worktree_factory=factory,
        worktree_cleanup=cleanup,
        sandbox_gate=_real_sandbox_gate(chain),
        blast_gate=acquire_blast_radius_token,  # the REAL Gate 2
        apply_candidate=apply_candidate,
        graph_resolver=graph_resolver,
        test_run_fn=test_run_fn,
        tree_sha_fn=tree_sha_fn,
    )

    assert isinstance(result, PRGateResult)
    assert isinstance(result.sandbox_token, SandboxExecutionToken)
    assert isinstance(result.blast_token, BlastRadiusClearedToken)
    assert result.branch_context == BRANCH
    # Both tokens minted in the SAME branch context.
    assert result.sandbox_token.branch_context == BRANCH
    assert result.blast_token.branch_context == BRANCH
    # blast chains onto sandbox (prev-link).
    assert result.blast_token.prev_hash == result.sandbox_token.digest()
    # Both verify on the SAME chain instance.
    assert chain.verify(result.sandbox_token)
    assert chain.verify(result.blast_token)
    # Cleanup WAS called (success path).
    assert cleanup_calls == [tmp_path]


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [SandboxLockFailed("boom"), RequiresCloudExecution("no docker")])
async def test_pipeline_gate1_failure_raises_and_cleans_up(tmp_path, exc):
    chain = DAGProofChain()
    cleanup_calls = []

    async def factory(branch):
        return tmp_path

    async def cleanup(wt):
        cleanup_calls.append(wt)

    async def sandbox_gate(**kwargs):
        raise exc

    with pytest.raises(PRGatePipelineError):
        await run_pr_gate_pipeline(
            op_id=OP_ID,
            candidate_files=CANDIDATE,
            repo_root=str(tmp_path),
            chain=chain,
            worktree_factory=factory,
            worktree_cleanup=cleanup,
            sandbox_gate=sandbox_gate,
        )
    # Cleanup ran via finally even though Gate 1 rejected.
    assert cleanup_calls == [tmp_path]


@pytest.mark.asyncio
async def test_pipeline_gate2_failure_raises_and_cleans_up(tmp_path):
    chain = DAGProofChain()
    cleanup_calls = []

    async def factory(branch):
        return tmp_path

    async def cleanup(wt):
        cleanup_calls.append(wt)

    async def apply_candidate(wt, files):
        return None

    async def graph_resolver(files, *, repo_root, oracle=None):
        return {"tests/test_x.py"}

    async def test_run_fn(tests, wt):
        # A real Gate 2 breach: a non-empty failed list.
        return {"failed": ["tests/test_x.py::test_a"], "total": 1}

    async def tree_sha_fn(wt):
        return "sha-pre"

    with pytest.raises(PRGatePipelineError):
        await run_pr_gate_pipeline(
            op_id=OP_ID,
            candidate_files=CANDIDATE,
            repo_root=str(tmp_path),
            chain=chain,
            worktree_factory=factory,
            worktree_cleanup=cleanup,
            sandbox_gate=_real_sandbox_gate(chain),
            blast_gate=acquire_blast_radius_token,  # REAL Gate 2
            apply_candidate=apply_candidate,
            graph_resolver=graph_resolver,
            test_run_fn=test_run_fn,
            tree_sha_fn=tree_sha_fn,
        )
    assert cleanup_calls == [tmp_path]


@pytest.mark.asyncio
async def test_cleanup_runs_even_on_unexpected_error(tmp_path):
    chain = DAGProofChain()
    cleanup_calls = []

    async def factory(branch):
        return tmp_path

    async def cleanup(wt):
        cleanup_calls.append(wt)

    async def apply_candidate(wt, files):
        raise ValueError("unexpected seam crash")

    with pytest.raises(ValueError):
        await run_pr_gate_pipeline(
            op_id=OP_ID,
            candidate_files=CANDIDATE,
            repo_root=str(tmp_path),
            chain=chain,
            worktree_factory=factory,
            worktree_cleanup=cleanup,
            sandbox_gate=_real_sandbox_gate(chain),
            apply_candidate=apply_candidate,
        )
    # An UNEXPECTED (non-gate) error still triggers cleanup via finally.
    assert cleanup_calls == [tmp_path]


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_mask_result(tmp_path):
    """A best-effort cleanup failure is swallowed -- the real result wins."""
    chain = DAGProofChain()

    async def factory(branch):
        return tmp_path

    async def cleanup(wt):
        raise OSError("rmtree boom")

    async def apply_candidate(wt, files):
        return None

    async def graph_resolver(files, *, repo_root, oracle=None):
        return set()

    async def test_run_fn(tests, wt):
        return {"failed": [], "total": 0}

    async def tree_sha_fn(wt):
        return "sha"

    result = await run_pr_gate_pipeline(
        op_id=OP_ID,
        candidate_files=CANDIDATE,
        repo_root=str(tmp_path),
        chain=chain,
        worktree_factory=factory,
        worktree_cleanup=cleanup,
        sandbox_gate=_real_sandbox_gate(chain),
        blast_gate=acquire_blast_radius_token,
        apply_candidate=apply_candidate,
        graph_resolver=graph_resolver,
        test_run_fn=test_run_fn,
        tree_sha_fn=tree_sha_fn,
    )
    assert isinstance(result, PRGateResult)


def test_pipeline_disabled_flag(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", raising=False)
    assert pipeline_enabled() is False
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    assert pipeline_enabled() is True
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false")
    assert pipeline_enabled() is False
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "1")
    assert pipeline_enabled() is True
