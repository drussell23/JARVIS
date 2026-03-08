"""tests/governance/multi_repo/test_e2e_multi_repo.py

End-to-end integration tests for the multi-repo coordinator.

These tests wire together the real IntentSignal, ContextBuilder,
CrossRepoBlastRadius, and RepoPipelineManager against a temporary
filesystem, with only the GovernedLoopService (pipeline) mocked.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.multi_repo import (
    ContextBuilder,
    CrossRepoBlastRadius,
    CrossRepoContext,
    RepoConfig,
    RepoPipelineManager,
    RepoRegistry,
)


class TestE2ESingleRepoFixPipeline:
    """Full flow: IntentSignal -> ContextBuilder -> CrossRepoBlastRadius
    -> RepoPipelineManager -> mocked GovernedLoopService."""

    @pytest.mark.asyncio
    async def test_e2e_single_repo_fix_pipeline(self, tmp_path: Path):
        # -- Set up a jarvis repo with source + test files ----------------
        jarvis = tmp_path / "jarvis"
        (jarvis / "src").mkdir(parents=True)
        (jarvis / "tests").mkdir(parents=True)
        (jarvis / "src" / "utils.py").write_text(
            "def helper():\n    return 42\n"
        )
        (jarvis / "tests" / "test_utils.py").write_text(
            "import utils\n\ndef test_helper():\n    assert utils.helper() == 42\n"
        )

        # -- Registry & components ----------------------------------------
        registry = RepoRegistry(configs=(
            RepoConfig(
                name="jarvis",
                local_path=jarvis,
                canary_slices=("tests/",),
            ),
        ))

        # 1. Build context
        builder = ContextBuilder(registry=registry, token_budget=8000)
        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/utils.py",),
            repo="jarvis",
            description="helper() returns wrong value",
            evidence={"signature": "AssertionError:test_utils:4"},
            confidence=0.92,
            stable=True,
        )
        ctx = await builder.build(signal)

        assert ctx.primary_file == "src/utils.py"
        assert ctx.primary_repo == "jarvis"
        # The test companion (test_utils.py) should be in related files
        related_paths = [rf.path for rf in ctx.related_files]
        assert any("test_utils.py" in p for p in related_paths), (
            f"Expected test_utils.py in related files, got {related_paths}"
        )

        # 2. Blast radius analysis
        analyzer = CrossRepoBlastRadius(registry=registry)
        report = await analyzer.analyze(signal)

        assert report.target_repo == "jarvis"
        assert not report.crosses_repo_boundary
        assert report.risk_escalation is None

        # 3. Submit through RepoPipelineManager with mocked GLS
        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock(
            return_value=MagicMock(op_id="op-e2e"),
        )

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_gls},
            blast_radius_analyzer=analyzer,
        )
        result = await manager.submit(signal)

        assert result.op_id == "op-e2e"
        mock_gls.submit.assert_called_once()

        # Verify the OperationContext passed to the pipeline
        call_args = mock_gls.submit.call_args
        assert call_args[1]["trigger_source"] == "intent:test_failure"


class TestE2ECrossRepoEscalation:
    """Cross-repo signal escalates risk to approval_required."""

    @pytest.mark.asyncio
    async def test_e2e_cross_repo_escalation(self, tmp_path: Path):
        # -- Jarvis repo with a shared API module -------------------------
        jarvis = tmp_path / "jarvis"
        (jarvis / "src").mkdir(parents=True)
        (jarvis / "src" / "shared_api.py").write_text(
            "class SharedAPI:\n    pass\n"
        )

        # -- Prime repo that imports (depends on) shared_api --------------
        prime = tmp_path / "prime"
        (prime / "src").mkdir(parents=True)
        (prime / "src" / "consumer.py").write_text(
            "import shared_api\n\ndef consume():\n    return shared_api.SharedAPI()\n"
        )

        # -- Registry with both repos ------------------------------------
        registry = RepoRegistry(configs=(
            RepoConfig(
                name="jarvis",
                local_path=jarvis,
                canary_slices=("tests/",),
            ),
            RepoConfig(
                name="prime",
                local_path=prime,
                canary_slices=("tests/",),
            ),
        ))

        # -- Analyze blast radius for shared_api.py in jarvis -------------
        analyzer = CrossRepoBlastRadius(registry=registry)
        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/shared_api.py",),
            repo="jarvis",
            description="SharedAPI contract changed",
            evidence={"signature": "TypeError:SharedAPI:init"},
            confidence=0.95,
            stable=True,
        )
        report = await analyzer.analyze(signal)

        assert report.crosses_repo_boundary is True
        assert report.risk_escalation == "approval_required"
        assert "prime" in report.affected_repos
        # The consumer.py in prime should be among affected files
        affected_paths = [(af.repo, af.path) for af in report.affected_files]
        assert any(
            repo == "prime" and "consumer.py" in path
            for repo, path in affected_paths
        ), f"Expected prime/consumer.py in affected files, got {affected_paths}"
