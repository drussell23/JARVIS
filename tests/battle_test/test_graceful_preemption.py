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


def _porcelain(repo):
    return subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"], capture_output=True, text=True,
    ).stdout.strip()


def _stash_list(repo):
    return subprocess.run(
        ["git", "-C", repo, "stash", "list"], capture_output=True, text=True,
    ).stdout


def test_git_safety_snapshots_non_destructively(tmp_path):
    """NON-DESTRUCTIVE contract: the dirty + untracked delta is snapshotted into
    a recoverable [preemption-shield] stash entry AND left in place on disk
    (the working tree is NOT cleared — the pre-2026-07-18 data-loss vector)."""
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("HALF-WRITTEN APPLY\n")
    open(os.path.join(repo, "newfile.py"), "w").write("x = 1\n")

    out = gp.git_safety_stash(repo)
    assert out.startswith("snapshot:"), out

    # The working tree is PRESERVED — both the tracked edit and the untracked file.
    assert _porcelain(repo) != "", "tree must NOT be cleared (was the data-loss bug)"
    assert open(os.path.join(repo, "seed.txt")).read() == "HALF-WRITTEN APPLY\n"
    assert os.path.isfile(os.path.join(repo, "newfile.py"))

    # ...and the snapshot is recoverable via `git stash list`.
    assert "preemption-shield" in _stash_list(repo)


def test_git_safety_snapshot_recoverable_after_tree_cleaned(tmp_path):
    """Belt-and-suspenders: even if the tree is later cleaned by something else
    (a subsequent reset / worktree teardown), the tracked delta is recoverable
    from the [preemption-shield] stash entry via apply. (Untracked files don't
    need this path — the shield leaves them on disk in the first place.)"""
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("HALF-WRITTEN APPLY\n")
    assert gp.git_safety_stash(repo).startswith("snapshot:")

    # Simulate the tree being cleaned across the boundary by a LATER op.
    subprocess.run(["git", "-C", repo, "checkout", "--", "seed.txt"], check=True)
    assert "HALF-WRITTEN" not in open(os.path.join(repo, "seed.txt")).read()

    # Recover the tracked delta from the shield's stash entry.
    subprocess.run(["git", "-C", repo, "stash", "apply"], check=True)
    assert open(os.path.join(repo, "seed.txt")).read() == "HALF-WRITTEN APPLY\n"


def test_git_safety_clean_tree_is_noop(tmp_path):
    repo = _init_repo(str(tmp_path / "r"))
    assert gp.git_safety_stash(repo) == "tree_clean"


def test_git_safety_clears_stale_index_lock(tmp_path):
    repo = _init_repo(str(tmp_path / "r"))
    open(os.path.join(repo, "seed.txt"), "w").write("dirty\n")
    lock = os.path.join(repo, ".git", "index.lock")
    open(lock, "w").write("")
    assert gp.git_safety_stash(repo).startswith("snapshot:")
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
