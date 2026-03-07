"""Shared fixtures for Ouroboros governance tests."""

import pytest
from pathlib import Path


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project structure for testing."""
    src = tmp_path / "backend" / "core"
    src.mkdir(parents=True)
    (src / "__init__.py").touch()
    test_file = src / "example.py"
    test_file.write_text("def hello():\n    return 'world'\n")
    return tmp_path


@pytest.fixture
def tmp_ledger_dir(tmp_path):
    """Temporary directory for operation ledger."""
    d = tmp_path / "ledger"
    d.mkdir()
    return d
