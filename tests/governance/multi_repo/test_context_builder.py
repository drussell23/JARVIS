"""tests/governance/multi_repo/test_context_builder.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


class TestContextFile:
    def test_frozen_dataclass(self):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextFile

        cf = ContextFile(
            repo="jarvis",
            path="src/foo.py",
            content="x = 1",
            relevance="import_dependency",
        )
        assert cf.repo == "jarvis"
        with pytest.raises(AttributeError):
            cf.repo = "other"


class TestCrossRepoContext:
    def test_creation(self):
        from backend.core.ouroboros.governance.multi_repo.context_builder import (
            CrossRepoContext, ContextFile,
        )

        ctx = CrossRepoContext(
            primary_file="tests/test_a.py",
            primary_repo="jarvis",
            related_files=(
                ContextFile(repo="jarvis", path="src/a.py", content="code", relevance="test"),
            ),
            total_tokens_estimate=500,
        )
        assert ctx.primary_repo == "jarvis"
        assert len(ctx.related_files) == 1
        assert ctx.total_tokens_estimate == 500


class TestContextBuilderFindImports:
    @pytest.mark.asyncio
    async def test_finds_import_in_same_repo(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        # Set up a file with an import
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "from backend.core.utils import helper\nx = 1\n"
        )
        (tmp_path / "backend" / "core").mkdir(parents=True)
        (tmp_path / "backend" / "core" / "utils.py").write_text("def helper(): pass\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)
        related = await builder.find_related_files("jarvis", "src/a.py")
        paths = [f.path for f in related]
        assert any("utils.py" in p for p in paths)


class TestContextBuilderFindTestCompanion:
    @pytest.mark.asyncio
    async def test_finds_test_file_for_source(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils.py").write_text("def foo(): pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_utils.py").write_text("def test_foo(): pass\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)
        related = await builder.find_related_files("jarvis", "src/utils.py")
        paths = [f.path for f in related]
        assert any("test_utils.py" in p for p in paths)


class TestContextBuilderBuild:
    @pytest.mark.asyncio
    async def test_build_returns_context_with_primary_content(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_edge(): assert False\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "assert:test_a"},
            confidence=0.9,
            stable=True,
        )
        ctx = await builder.build(signal)
        assert ctx.primary_file == "tests/test_a.py"
        assert ctx.primary_repo == "jarvis"
        assert ctx.total_tokens_estimate > 0


class TestContextBuilderTokenBudget:
    @pytest.mark.asyncio
    async def test_respects_token_budget(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        # Create a file with a huge body
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "big.py").write_text("x = 1\n" * 50000)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_big.py").write_text(
            "from src.big import x\ndef test_x(): pass\n"
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=100)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_big.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "x:big"},
            confidence=0.9,
            stable=True,
        )
        ctx = await builder.build(signal)
        # Token estimate should be near or below budget
        assert ctx.total_tokens_estimate <= 200  # some slack for primary file
