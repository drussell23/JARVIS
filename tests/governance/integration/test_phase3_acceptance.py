"""Phase 3 acceptance tests — multi-repo saga autonomy."""
import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    ArchitecturalCycleError,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp, PatchedFile, RepoPatch, SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.cross_repo_verifier import CrossRepoVerifier


# AC1: OperationContext with cross_repo=True validates DAG in __post_init__

def test_ac1_dag_cycle_raises_at_create_time():
    """ArchitecturalCycleError raised synchronously at context creation."""
    with pytest.raises(ArchitecturalCycleError):
        OperationContext.create(
            target_files=("x.py",),
            description="cycle test",
            repo_scope=("jarvis", "prime"),
            primary_repo="jarvis",
            dependency_edges=(("jarvis", "prime"), ("prime", "jarvis")),
        )


# AC2: Single-repo path unchanged

def test_ac2_single_repo_context_cross_repo_false():
    ctx = OperationContext.create(
        target_files=("backend/x.py",),
        description="single",
        primary_repo="jarvis",
    )
    assert ctx.cross_repo is False


# AC3: SagaApplyStrategy applies in topological order

def test_ac3_topological_order():
    strategy = SagaApplyStrategy(repo_roots={}, ledger=MagicMock())
    # _topological_sort takes (repo_scope, edges) only — no apply_plan param
    order = strategy._topological_sort(
        repo_scope=("jarvis", "prime", "reactor-core"),
        edges=(("prime", "jarvis"), ("reactor-core", "prime")),
    )
    # jarvis before prime before reactor-core
    assert order.index("jarvis") < order.index("prime") < order.index("reactor-core")


# AC4: Drift abort before any writes

async def test_ac4_drift_aborts_before_writes(tmp_path):
    f = tmp_path / "x.py"
    f.write_bytes(b"original")
    patch_map = {
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="x.py", op=FileOp.MODIFY, preimage=b"original"),),
            new_content=(("x.py", b"modified"),),
        )
    }
    ctx = OperationContext.create(
        target_files=("x.py",),
        description="drift test",
        repo_scope=("jarvis",),
        primary_repo="jarvis",
        apply_plan=("jarvis",),
        repo_snapshots=(("jarvis", "expected_hash"),),
    )
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots={"jarvis": tmp_path}, ledger=ledger)
    with patch.object(strategy, "_get_head_hash", return_value="different_hash"):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ABORTED
    assert f.read_bytes() == b"original"  # untouched


# AC5: RepoPipelineManager passes primary_repo

async def test_ac5_repo_pipeline_manager_passes_repo(tmp_path):
    from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig, RepoRegistry
    from backend.core.ouroboros.governance.multi_repo.repo_pipeline import RepoPipelineManager
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    captured = []

    class FakePipeline:
        async def submit(self, ctx, *, trigger_source=""):
            del trigger_source  # accepted by interface, not inspected
            captured.append(ctx)
            return MagicMock(op_id="op-fake-001")

    registry = RepoRegistry(configs=(
        RepoConfig(name="reactor-core", local_path=tmp_path, canary_slices=()),
    ))
    manager = RepoPipelineManager(
        registry=registry,
        pipelines={"reactor-core": FakePipeline()},
    )
    signal = IntentSignal(
        source="backlog",
        target_files=("backend/x.py",),
        repo="reactor-core",
        description="fix",
        evidence={"signature": "s"},
        confidence=0.9,
        stable=True,
    )
    await manager.submit(signal)
    assert captured[0].primary_repo == "reactor-core"


# AC6: CrossRepoVerifier passes on clean repos

async def test_ac6_cross_repo_verifier_passes_clean(tmp_path):
    verifier = CrossRepoVerifier(repo_roots={"jarvis": tmp_path}, dependency_edges=())
    patch_map = {
        "jarvis": RepoPatch(repo="jarvis", files=(), new_content=())
    }
    result = await verifier.verify(
        repo_scope=("jarvis",),
        patch_map=patch_map,
        dependency_edges=(),
    )
    assert result.passed is True


# AC7: schema_version is 3.0

def test_ac7_schema_version():
    ctx = OperationContext.create(target_files=("x.py",), description="v")
    assert ctx.schema_version == "3.0"
