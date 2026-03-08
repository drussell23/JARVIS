"""tests/governance/multi_repo/test_registry.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class TestRepoConfig:
    def test_frozen_dataclass(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig

        cfg = RepoConfig(
            name="jarvis",
            local_path=Path("/tmp/jarvis"),
            canary_slices=("tests/",),
        )
        assert cfg.name == "jarvis"
        assert cfg.default_branch == "main"
        assert cfg.enabled is True
        with pytest.raises(AttributeError):
            cfg.name = "other"

    def test_custom_defaults(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig

        cfg = RepoConfig(
            name="prime",
            local_path=Path("/tmp/prime"),
            canary_slices=("tests/",),
            default_branch="develop",
            enabled=False,
        )
        assert cfg.default_branch == "develop"
        assert cfg.enabled is False


class TestRepoRegistryCreation:
    def test_from_configs(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        configs = (
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        )
        registry = RepoRegistry(configs=configs)
        assert registry.get("jarvis").name == "jarvis"
        assert registry.get("prime").name == "prime"

    def test_get_unknown_raises(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))
        with pytest.raises(KeyError):
            registry.get("unknown")

    def test_list_enabled_filters_disabled(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        configs = (
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",), enabled=True),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",), enabled=False),
        )
        registry = RepoRegistry(configs=configs)
        enabled = registry.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].name == "jarvis"


class TestRepoRegistryFromEnv:
    def test_from_env_creates_jarvis_repo(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        with patch.dict("os.environ", {"JARVIS_REPO_PATH": "/tmp/jarvis-test"}, clear=False):
            registry = RepoRegistry.from_env()
            cfg = registry.get("jarvis")
            assert cfg.local_path == Path("/tmp/jarvis-test")

    def test_from_env_includes_prime_when_set(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        env = {
            "JARVIS_REPO_PATH": "/tmp/j",
            "JARVIS_PRIME_REPO_PATH": "/tmp/p",
        }
        with patch.dict("os.environ", env, clear=False):
            registry = RepoRegistry.from_env()
            assert registry.get("prime").local_path == Path("/tmp/p")

    def test_from_env_omits_prime_when_unset(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        env = {"JARVIS_REPO_PATH": "/tmp/j"}
        with patch.dict("os.environ", env, clear=False):
            registry = RepoRegistry.from_env()
            with pytest.raises(KeyError):
                registry.get("prime")


class TestRepoRegistryFileOps:
    @pytest.mark.asyncio
    async def test_read_file_delegates_to_connector(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        # Create a real file in tmp_path
        (tmp_path / "src" / "foo.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("hello = 42")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        content = await registry.read_file("jarvis", "src/foo.py")
        assert content == "hello = 42"

    @pytest.mark.asyncio
    async def test_read_file_returns_none_for_missing(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        content = await registry.read_file("jarvis", "nonexistent.py")
        assert content is None

    @pytest.mark.asyncio
    async def test_search_files_finds_by_glob(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("pass")
        (tmp_path / "tests" / "test_b.py").write_text("pass")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        matches = await registry.search_files("tests/test_*.py", repo="jarvis")
        assert len(matches) == 2
