"""GitIndexGuard Slice 1 -- regression spine.

Pins the pure-stdlib primitive that detects a MISSING
``.git/index`` (the background-Cursor-Agent unlink failure mode
that produced the false "7856 staged deletions" in Source
Control, operator soak 2026-05-19) and advisorily rebuilds it
from HEAD with ``git read-tree HEAD`` -- working tree untouched,
present indexes NEVER modified.

Coverage:
  * Master flag asymmetric env semantics (default false until
    graduation)
  * Timeout env knob clamping (floor / ceiling / garbage)
  * Closed-5 GitIndexGuardOutcome taxonomy + exact value set
  * Frozen GitIndexAnomaly dataclass + to_dict round-trip
  * _resolve_index_path: .git dir / .git gitfile worktree /
    absent / unparseable
  * git_index_present: present / absent / non-repo
  * detect_and_rebuild: DISABLED / HEALTHY (and the load-bearing
    "present index is never rebuilt -- no subprocess" safety pin)
    / MISSING_REBUILT (real repo) / MISSING_REBUILD_FAILED /
    FAILED (non-repo)
  * on_anomaly seam: fired for anomalies, NOT for HEALTHY /
    DISABLED, callback exception swallowed
  * NEVER raises across every IO boundary
  * register_flags seeds 2 specs; register_shipped_invariants
    self-validates green against the shipped source
"""
from __future__ import annotations

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.git_index_guard import (
    GIT_INDEX_GUARD_SCHEMA_VERSION,
    GitIndexAnomaly,
    GitIndexGuardOutcome,
    detect_and_rebuild,
    git_index_guard_enabled,
    git_index_guard_timeout_s,
    git_index_present,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance import git_index_guard as gig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in (
        "JARVIS_GIT_INDEX_GUARD_ENABLED",
        "JARVIS_GIT_INDEX_GUARD_TIMEOUT_S",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real, minimal git repo with one commit on HEAD."""
    def _run(*args: str) -> None:
        subprocess.run(
            ["git", *args], cwd=str(tmp_path),
            check=True, capture_output=True, text=True,
        )
    _run("init", "-q")
    _run("config", "user.email", "t@t.t")
    _run("config", "user.name", "t")
    (tmp_path / "keep.txt").write_text("payload\n", encoding="utf-8")
    _run("add", "keep.txt")
    _run("commit", "-q", "-m", "seed")
    return tmp_path


# ---------------------------------------------------------------------------
# Master flag + timeout knob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False), ("", False), ("   ", False),
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("0", False), ("false", False), ("garbage", False),
    ],
)
def test_master_flag_asymmetric(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("JARVIS_GIT_INDEX_GUARD_ENABLED", raising=False)
    else:
        monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", raw)
    assert git_index_guard_enabled() is expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 5.0), ("", 5.0), ("garbage", 5.0),
        ("0.0", 1.0), ("0.5", 1.0), ("-9", 1.0),
        ("10", 10.0), ("999", 30.0), ("30", 30.0),
    ],
)
def test_timeout_clamp(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(
            "JARVIS_GIT_INDEX_GUARD_TIMEOUT_S", raising=False,
        )
    else:
        monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_TIMEOUT_S", raw)
    assert git_index_guard_timeout_s() == expected


# ---------------------------------------------------------------------------
# Taxonomy + dataclass
# ---------------------------------------------------------------------------


def test_closed_5_taxonomy():
    assert {m.value for m in GitIndexGuardOutcome} == {
        "healthy", "missing_rebuilt", "missing_rebuild_failed",
        "disabled", "failed",
    }


def test_anomaly_frozen_and_roundtrip():
    a = GitIndexAnomaly(
        repo_root="/r", index_path="/r/.git/index",
        outcome=GitIndexGuardOutcome.MISSING_REBUILT, detail="d",
    )
    assert a.schema_version == GIT_INDEX_GUARD_SCHEMA_VERSION
    assert a.to_dict() == {
        "repo_root": "/r",
        "index_path": "/r/.git/index",
        "outcome": "missing_rebuilt",
        "detail": "d",
        "schema_version": GIT_INDEX_GUARD_SCHEMA_VERSION,
    }
    with pytest.raises(FrozenInstanceError):
        a.detail = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _resolve_index_path / git_index_present
# ---------------------------------------------------------------------------


def test_resolve_index_dir_layout(git_repo):
    idx = gig._resolve_index_path(git_repo)
    assert idx == git_repo / ".git" / "index"
    assert git_index_present(git_repo) is True


def test_resolve_index_gitfile_worktree(tmp_path):
    # Simulate a linked-worktree .git *file*.
    real_gitdir = tmp_path / "realgit" / "worktrees" / "wt"
    real_gitdir.mkdir(parents=True)
    (real_gitdir / "index").write_bytes(b"x")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(
        f"gitdir: {real_gitdir}\n", encoding="utf-8",
    )
    assert gig._resolve_index_path(wt) == real_gitdir / "index"
    assert git_index_present(wt) is True


def test_resolve_index_non_repo(tmp_path):
    assert gig._resolve_index_path(tmp_path) is None
    assert git_index_present(tmp_path) is False


def test_resolve_index_unparseable_gitfile(tmp_path):
    (tmp_path / ".git").write_text("not a gitdir line\n")
    assert gig._resolve_index_path(tmp_path) is None
    assert git_index_present(tmp_path) is False


# ---------------------------------------------------------------------------
# detect_and_rebuild
# ---------------------------------------------------------------------------


def test_disabled_when_master_off(git_repo):
    out = detect_and_rebuild(git_repo)
    assert out.outcome is GitIndexGuardOutcome.DISABLED


def test_healthy_present_index_never_rebuilds(monkeypatch, git_repo):
    """Load-bearing safety pin: a present index must NEVER trigger
    a rebuild -- no git subprocess may run (read-tree HEAD would
    discard legitimately staged work)."""
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    with mock.patch.object(
        gig, "_run_git", side_effect=AssertionError("must not run git"),
    ):
        out = detect_and_rebuild(git_repo)
    assert out.outcome is GitIndexGuardOutcome.HEALTHY


def test_missing_index_rebuilt_workingtree_untouched(
    monkeypatch, git_repo,
):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    idx = git_repo / ".git" / "index"
    idx.unlink()
    assert not idx.is_file()

    fired: List[GitIndexAnomaly] = []
    out = detect_and_rebuild(git_repo, on_anomaly=fired.append)

    assert out.outcome is GitIndexGuardOutcome.MISSING_REBUILT
    assert idx.is_file()  # index back
    # Working tree content untouched.
    assert (git_repo / "keep.txt").read_text() == "payload\n"
    # git status agrees: clean (no spurious deletions).
    st = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(git_repo),
        capture_output=True, text=True,
    )
    assert st.stdout.strip() == ""
    assert len(fired) == 1
    assert fired[0].outcome is GitIndexGuardOutcome.MISSING_REBUILT


def test_missing_rebuild_failed(monkeypatch, git_repo):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    (git_repo / ".git" / "index").unlink()
    fired: List[GitIndexAnomaly] = []
    fake = subprocess.CompletedProcess(
        args=["git"], returncode=128, stdout="", stderr="boom",
    )
    with mock.patch.object(gig, "_run_git", return_value=fake):
        out = detect_and_rebuild(git_repo, on_anomaly=fired.append)
    assert out.outcome is GitIndexGuardOutcome.MISSING_REBUILD_FAILED
    assert "rc=128" in out.detail and "boom" in out.detail
    assert len(fired) == 1


def test_failed_non_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    fired: List[GitIndexAnomaly] = []
    out = detect_and_rebuild(tmp_path, on_anomaly=fired.append)
    assert out.outcome is GitIndexGuardOutcome.FAILED
    assert len(fired) == 1


def test_on_anomaly_not_fired_for_healthy_or_disabled(
    monkeypatch, git_repo,
):
    calls: List[GitIndexAnomaly] = []
    # DISABLED
    detect_and_rebuild(git_repo, on_anomaly=calls.append)
    # HEALTHY
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    detect_and_rebuild(git_repo, on_anomaly=calls.append)
    assert calls == []


def test_on_anomaly_exception_swallowed(monkeypatch, git_repo):
    monkeypatch.setenv("JARVIS_GIT_INDEX_GUARD_ENABLED", "true")
    (git_repo / ".git" / "index").unlink()

    def _boom(_a):
        raise RuntimeError("emitter exploded")

    # Must not raise despite the misbehaving SSE seam.
    out = detect_and_rebuild(git_repo, on_anomaly=_boom)
    assert out.outcome is GitIndexGuardOutcome.MISSING_REBUILT


def test_never_raises_on_garbage_inputs():
    # Non-path-ish, None-ish, etc. -- defensive across boundary.
    for bad in ("", "/nonexistent/zzz", "."):
        out = detect_and_rebuild(Path(bad))
        assert isinstance(out, GitIndexAnomaly)
    assert git_index_present(Path("/nonexistent/zzz")) is False


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


def test_register_flags_seeds_two():
    seen = []

    class _Reg:
        def register(self, spec):
            seen.append(spec.name)

    assert register_flags(_Reg()) == 2
    assert set(seen) == {
        "JARVIS_GIT_INDEX_GUARD_ENABLED",
        "JARVIS_GIT_INDEX_GUARD_TIMEOUT_S",
    }


def test_shipped_invariant_self_validates_green():
    import ast

    invs = register_shipped_invariants()
    assert len(invs) == 1
    src = Path(gig.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    violations = invs[0].validate(tree, src)
    assert violations == (), f"self-validate not green: {violations}"
