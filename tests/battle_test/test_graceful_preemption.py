"""Graceful Preemption Shield — anti-corruption matrix tests."""
from __future__ import annotations

import os
import subprocess

import pytest

from backend.core.ouroboros.battle_test import graceful_preemption as gp


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("JARVIS_PREEMPTION_SHIELD_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_PREEMPTION_GIT_STASH_ENABLED", raising=False)
    gp._reset_for_tests()
    yield
    gp._reset_for_tests()


def test_shield_enabled_default_true():
    assert gp.shield_enabled() is True


def test_shield_disabled_skips(monkeypatch):
    monkeypatch.setenv("JARVIS_PREEMPTION_SHIELD_ENABLED", "false")
    assert gp.engage(signal_name="sigterm") == {"skipped": "shield_disabled"}


def test_engage_is_idempotent(monkeypatch):
    monkeypatch.setattr(gp, "is_gcp_preemption", lambda: False)
    monkeypatch.setattr(gp, "git_safety_stash", lambda repo_root=None: "tree_clean")
    monkeypatch.setattr(gp, "halt_child_workers", lambda: 0)
    first = gp.engage(signal_name="sigterm")
    second = gp.engage(signal_name="sigterm")
    assert "skipped" not in first
    assert second == {"skipped": "already_engaged"}


def test_is_gcp_preemption_true(monkeypatch):
    class _Resp:
        def read(self): return b"TRUE"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(gp.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert gp.is_gcp_preemption() is True


def test_is_gcp_preemption_false_off_gcp(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no metadata server")
    monkeypatch.setattr(gp.urllib.request, "urlopen", _boom)
    assert gp.is_gcp_preemption() is False


def _init_repo(path):
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "t"], check=True)
    open(os.path.join(path, "seed.txt"), "w").write("seed\n")
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    subprocess.run(["git", "-C", path, "commit", "-qm", "seed"], check=True)
    return path


def test_git_safety_stashes_in_flight_changes(tmp_path):
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("HALF-WRITTEN APPLY\n")
    open(os.path.join(repo, "newfile.py"), "w").write("x = 1\n")
    assert gp.git_safety_stash(repo) == "stashed"
    porcelain = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True,
    ).stdout.strip()
    assert porcelain == ""
    stashes = subprocess.run(
        ["git", "-C", repo, "stash", "list"], capture_output=True, text=True,
    ).stdout
    assert "preemption-shield" in stashes


def test_git_safety_clean_tree_is_noop(tmp_path):
    repo = _init_repo(str(tmp_path / "r"))
    assert gp.git_safety_stash(repo) == "tree_clean"


def test_git_safety_clears_stale_index_lock(tmp_path):
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("dirty\n")
    lock = os.path.join(repo, ".git", "index.lock")
    open(lock, "w").write("")
    assert gp.git_safety_stash(repo) == "stashed"
    assert not os.path.isfile(lock)


def test_git_safety_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PREEMPTION_GIT_STASH_ENABLED", "false")
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("dirty\n")
    assert gp.git_safety_stash(repo) == "stash_disabled"


def test_git_safety_failsoft_on_non_repo(tmp_path):
    out = gp.git_safety_stash(str(tmp_path))
    assert isinstance(out, str) and out != "stashed"


def test_engage_returns_telemetry(monkeypatch, tmp_path):
    monkeypatch.setattr(gp, "is_gcp_preemption", lambda: True)
    monkeypatch.setattr(gp, "halt_child_workers", lambda: 2)
    monkeypatch.setattr(gp, "git_safety_stash", lambda repo_root=None: "tree_clean")
    out = gp.engage(signal_name="sigterm")
    assert out["gcp_preemption"] is True
    assert out["children_halted"] == 2
    assert out["git_safety"] == "tree_clean"
    assert out["signal"] == "sigterm"
    assert "elapsed_s" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
