"""tests/governance/multi_repo/test_repo_pipeline.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


class TestRepoPipelineManagerSubmit:
    @pytest.mark.asyncio
    async def test_routes_signal_to_correct_repo_pipeline(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        ))

        mock_jarvis_gls = AsyncMock()
        mock_jarvis_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-j01"))
        mock_prime_gls = AsyncMock()
        mock_prime_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-p01"))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_jarvis_gls, "prime": mock_prime_gls},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "err:test_a"},
            confidence=0.9,
            stable=True,
        )
        result = await manager.submit(signal)
        mock_jarvis_gls.submit.assert_called_once()
        mock_prime_gls.submit.assert_not_called()
        assert result.op_id == "op-j01"

    @pytest.mark.asyncio
    async def test_routes_prime_signal_to_prime_pipeline(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        ))

        mock_jarvis_gls = AsyncMock()
        mock_prime_gls = AsyncMock()
        mock_prime_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-p01"))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_jarvis_gls, "prime": mock_prime_gls},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_prime.py",),
            repo="prime",
            description="prime test failure",
            evidence={"signature": "err:prime"},
            confidence=0.9,
            stable=True,
        )
        await manager.submit(signal)
        mock_prime_gls.submit.assert_called_once()
        mock_jarvis_gls.submit.assert_not_called()


class TestRepoPipelineManagerUnknownRepo:
    @pytest.mark.asyncio
    async def test_raises_for_unknown_repo(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))
        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": AsyncMock()},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_x.py",),
            repo="unknown_repo",
            description="test failure",
            evidence={"signature": "err:x"},
            confidence=0.9,
            stable=True,
        )
        with pytest.raises(KeyError, match="unknown_repo"):
            await manager.submit(signal)


class TestRepoPipelineManagerBlastRadius:
    @pytest.mark.asyncio
    async def test_attaches_blast_radius_to_context(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            BlastRadiusReport, CrossRepoBlastRadius,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))

        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-001"))

        mock_blast = AsyncMock(spec=CrossRepoBlastRadius)
        mock_blast.analyze = AsyncMock(return_value=BlastRadiusReport(
            target_repo="jarvis",
            target_files=("src/a.py",),
            affected_repos=("jarvis",),
            affected_files=(),
            crosses_repo_boundary=False,
            risk_escalation=None,
            contract_impact=None,
        ))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_gls},
            blast_radius_analyzer=mock_blast,
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "err:a"},
            confidence=0.9,
            stable=True,
        )
        await manager.submit(signal)

        # Blast radius should have been called
        mock_blast.analyze.assert_called_once_with(signal)

        # GLS.submit should have been called with the context
        mock_gls.submit.assert_called_once()
        call_args = mock_gls.submit.call_args
        assert call_args[1]["trigger_source"] == "intent:test_failure"


class TestRepoPipelineManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_all_calls_start_on_each(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))

        mock_gls = AsyncMock()
        mock_gls.start = AsyncMock()
        mock_gls.stop = AsyncMock()

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_gls},
        )
        await manager.start_all()
        mock_gls.start.assert_called_once()

        await manager.stop_all()
        mock_gls.stop.assert_called_once()


async def test_submit_sets_primary_repo_on_context():
    """RepoPipelineManager.submit() passes signal.repo as primary_repo."""
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
        RepoPipelineManager,
    )
    from backend.core.ouroboros.governance.multi_repo.registry import (
        RepoConfig, RepoRegistry,
    )
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    registry = RepoRegistry(configs=(
        RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
    ))

    captured_ctx = None

    async def fake_submit(ctx, *, trigger_source=None):
        del trigger_source  # accepted by interface, not inspected
        nonlocal captured_ctx
        captured_ctx = ctx
        return MagicMock(op_id="op-capture-01")

    mock_gls = AsyncMock()
    mock_gls.submit = fake_submit

    manager = RepoPipelineManager(
        registry=registry,
        pipelines={"prime": mock_gls},
    )

    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_prime.py",),
        repo="prime",
        description="prime test failure",
        evidence={"signature": "err:prime"},
        confidence=0.9,
        stable=True,
    )
    await manager.submit(signal)

    assert captured_ctx is not None, "pipeline.submit() was never called"
    assert captured_ctx.primary_repo == "prime"
    assert captured_ctx.repo_scope == ("prime",)
