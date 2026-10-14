"""Sibling repos are found by what they are, not by what their folder is called.

Boot warned "J-Prime repo not found, Reactor-Core repo not found" on a host
whose siblings live in folders named ``prime`` and ``reactor`` and whose .env
declares JARVIS_PRIME_PATH / REACTOR_CORE_PATH.
"""
from pathlib import Path

from backend.core.coding_council.config import ConfigDiscovery


def _checkout(root: Path, folder: str, package: str, marker: str) -> Path:
    repo = root / folder
    (repo / package / marker).mkdir(parents=True)
    (repo / package / "__init__.py").write_text("")
    return repo


def _discover(jarvis: Path, monkeypatch):
    monkeypatch.setattr(ConfigDiscovery, "_find_repo_root", staticmethod(lambda _p: jarvis))
    return ConfigDiscovery().discover_trinity_repos()


def test_siblings_in_folders_named_anything_are_recognised(tmp_path, monkeypatch):
    for name in ("J_PRIME_REPO", "JARVIS_PRIME_PATH", "JARVIS_PRIME_REPO_PATH", "REACTOR_CORE_REPO",
                 "REACTOR_CORE_PATH", "REACTOR_CORE_REPO_PATH", "JARVIS_REACTOR_REPO_PATH"):
        monkeypatch.delenv(name, raising=False)
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    prime = _checkout(tmp_path, "prime", "jarvis_prime", "core")
    reactor = _checkout(tmp_path, "reactor", "reactor_core", "orchestration")
    repos = _discover(jarvis, monkeypatch)
    assert repos["j_prime"] == prime and repos["reactor_core"] == reactor


def test_the_declared_path_names_the_kernel_uses_are_honoured(tmp_path, monkeypatch):
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    elsewhere = tmp_path / "somewhere" / "mind"
    elsewhere.mkdir(parents=True)
    monkeypatch.delenv("J_PRIME_REPO", raising=False)
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(elsewhere))
    assert _discover(jarvis, monkeypatch)["j_prime"] == elsewhere.resolve()
