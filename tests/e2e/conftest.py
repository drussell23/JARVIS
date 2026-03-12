"""Shared fixtures for E2E saga tests."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest


def _init_test_repo(path: Path, name: str) -> str:
    """Initialize a git repo with sentinel file. Returns HEAD SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@jarvis.local"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "JARVIS Test"], cwd=str(path), check=True)
    sentinel_src = Path(__file__).parent / "fixtures" / "sentinel_jarvis.py"
    if sentinel_src.exists():
        (path / "sentinel.py").write_text(sentinel_src.read_text())
    else:
        (path / "sentinel.py").write_text("def foo():\n    return 1\n")
    (path / ".jarvis").mkdir(exist_ok=True)
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"init {name}", "--no-verify"],
        cwd=str(path), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def e2e_repo_roots(tmp_path: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Create jarvis + prime + reactor-core git repos."""
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime", "reactor-core"):
        root = tmp_path / name
        sha = _init_test_repo(root, name)
        roots[name] = root
        shas[name] = sha
    return roots, shas


JPRIME_AVAILABLE = os.getenv("JARVIS_PRIME_ENDPOINT", "") != ""

jprime = pytest.mark.skipif(
    not JPRIME_AVAILABLE,
    reason="JARVIS_PRIME_ENDPOINT not set -- skipping J-Prime tests",
)
