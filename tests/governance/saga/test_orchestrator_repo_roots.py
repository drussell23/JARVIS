"""Tests for OrchestratorConfig repo_registry wiring and saga root resolution."""
import logging
from pathlib import Path

from backend.core.ouroboros.governance.orchestrator import OrchestratorConfig
from backend.core.ouroboros.governance.multi_repo.registry import (
    RepoConfig, RepoRegistry,
)


def _make_registry(tmp_path: Path) -> RepoRegistry:
    jarvis = tmp_path / "jarvis"
    prime = tmp_path / "prime"
    reactor = tmp_path / "reactor-core"
    for p in (jarvis, prime, reactor):
        p.mkdir()
    return RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=prime, canary_slices=("tests/",)),
        RepoConfig(name="reactor-core", local_path=reactor, canary_slices=("tests/",)),
    ))


def test_orchestrator_config_accepts_repo_registry(tmp_path):
    """OrchestratorConfig can be constructed with repo_registry."""
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(
        project_root=tmp_path / "jarvis",
        repo_registry=registry,
    )
    assert cfg.repo_registry is registry


def test_orchestrator_config_defaults_registry_to_none(tmp_path):
    """OrchestratorConfig.repo_registry defaults to None (backward compat)."""
    cfg = OrchestratorConfig(project_root=tmp_path)
    assert cfg.repo_registry is None


def test_resolve_repo_roots_uses_registry(tmp_path):
    """When repo_registry is set, resolve_repo_roots returns per-repo paths."""
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "prime", "reactor-core"),
        op_id="op-test-001",
    )
    assert roots["jarvis"] == tmp_path / "jarvis"
    assert roots["prime"] == tmp_path / "prime"
    assert roots["reactor-core"] == tmp_path / "reactor-core"


def test_resolve_repo_roots_fallback_for_unknown_repo(tmp_path, caplog):
    """Missing repo key falls back to project_root with a warning (no KeyError)."""
    caplog.set_level(logging.WARNING)
    registry = _make_registry(tmp_path)
    cfg = OrchestratorConfig(project_root=tmp_path / "jarvis", repo_registry=registry)
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "unknown_repo"),
        op_id="op-test-002",
    )
    assert roots["jarvis"] == tmp_path / "jarvis"
    assert roots["unknown_repo"] == tmp_path / "jarvis"  # fallback to project_root
    warning_messages = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("unknown_repo" in str(m) for m in warning_messages)
    assert any("op-test-002" in str(m) for m in warning_messages)


def test_resolve_repo_roots_no_registry_uses_project_root(tmp_path):
    """When repo_registry is None, all repos resolve to project_root."""
    cfg = OrchestratorConfig(project_root=tmp_path)
    roots = cfg.resolve_repo_roots(
        repo_scope=("jarvis", "prime"),
        op_id="op-test-003",
    )
    assert roots["jarvis"] == tmp_path
    assert roots["prime"] == tmp_path


def test_resolve_repo_roots_empty_scope_returns_empty_dict(tmp_path):
    """Empty repo_scope returns empty dict — no repos to resolve."""
    cfg = OrchestratorConfig(project_root=tmp_path)
    roots = cfg.resolve_repo_roots(repo_scope=(), op_id="op-test-empty")
    assert roots == {}
