"""Tests for multi-level YAML config inheritance (GAP 7).

Layer order (later overrides earlier):
  1. <global_root>/.jarvis/governance.yaml
  2. <repo_root>/.jarvis/governance.yaml
  3. <repo_root>/.jarvis/governance.local.yaml
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_dirs_returns_empty_dict(tmp_path):
    """When no config files exist, load_layered_config returns {}."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    assert result == {}


def test_global_config_loaded(tmp_path):
    """Values from <global_root>/.jarvis/governance.yaml are returned."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    _write(
        tmp_path / "global" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 300\npipeline_timeout_s: 120\n",
    )

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    assert result["approval_timeout_s"] == 300
    assert result["pipeline_timeout_s"] == 120


def test_repo_overrides_global(tmp_path):
    """Repo-level values override global values for the same key."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    _write(
        tmp_path / "global" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 300\nmax_concurrent_ops: 2\n",
    )
    _write(
        tmp_path / "repo" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 900\n",
    )

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    assert result["approval_timeout_s"] == 900   # repo wins
    assert result["max_concurrent_ops"] == 2     # global value carried through


def test_local_overrides_repo(tmp_path):
    """.local.yaml values override repo-level values."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    _write(
        tmp_path / "global" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 300\n",
    )
    _write(
        tmp_path / "repo" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 900\ngeneration_timeout_s: 60\n",
    )
    _write(
        tmp_path / "repo" / ".jarvis" / "governance.local.yaml",
        "approval_timeout_s: 1200\n",
    )

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    assert result["approval_timeout_s"] == 1200    # .local wins
    assert result["generation_timeout_s"] == 60    # repo value still present


def test_malformed_yaml_skipped(tmp_path):
    """A file with invalid YAML is silently skipped; other layers still load."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    _write(
        tmp_path / "global" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: 300\n",
    )
    # Write intentionally broken YAML at repo level
    _write(
        tmp_path / "repo" / ".jarvis" / "governance.yaml",
        "approval_timeout_s: :\n  broken: yaml: content: [\n",
    )

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    # Global layer loaded; broken repo layer skipped
    assert result.get("approval_timeout_s") == 300


def test_non_dict_yaml_skipped(tmp_path):
    """A YAML file whose top-level content is a list is skipped gracefully."""
    from backend.core.ouroboros.governance.config_loader import load_layered_config

    _write(
        tmp_path / "global" / ".jarvis" / "governance.yaml",
        "max_concurrent_ops: 4\n",
    )
    # Top-level list, not a dict
    _write(
        tmp_path / "repo" / ".jarvis" / "governance.yaml",
        "- item_one\n- item_two\n",
    )

    result = load_layered_config(
        global_root=tmp_path / "global",
        repo_root=tmp_path / "repo",
    )
    assert result.get("max_concurrent_ops") == 4  # global still loaded
    # list file does not bleed list content into result
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Structural test: GovernedLoopConfig.from_env references config_loader
# ---------------------------------------------------------------------------

def test_governed_loop_config_from_env_references_config_loader():
    """GovernedLoopConfig.from_env source must reference load_layered_config or config_loader."""
    from backend.core.ouroboros.governance.governed_loop_service import GovernedLoopConfig

    source = inspect.getsource(GovernedLoopConfig.from_env)
    assert "load_layered_config" in source or "config_loader" in source, (
        "GovernedLoopConfig.from_env must import/call load_layered_config "
        "from config_loader to support multi-level YAML config inheritance."
    )
