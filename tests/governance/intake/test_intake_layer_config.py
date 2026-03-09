"""Tests for IntakeLayerConfig multi-repo registry field."""
from pathlib import Path

from backend.core.ouroboros.governance.intake.intake_layer_service import IntakeLayerConfig
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig, RepoRegistry,
)


def _make_registry(tmp_path: Path) -> RepoRegistry:
    return RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=tmp_path / "jarvis", canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=tmp_path / "prime", canary_slices=("tests/",)),
    ))


def test_intake_layer_config_accepts_repo_registry(tmp_path):
    """IntakeLayerConfig can be constructed with repo_registry."""
    registry = _make_registry(tmp_path)
    config = IntakeLayerConfig(project_root=tmp_path, repo_registry=registry)
    assert config.repo_registry is registry


def test_intake_layer_config_defaults_registry_to_none(tmp_path):
    """IntakeLayerConfig.repo_registry defaults to None (backward compat)."""
    config = IntakeLayerConfig(project_root=tmp_path)
    assert config.repo_registry is None
