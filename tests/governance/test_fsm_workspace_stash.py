"""Atomic Workspace Checkpointing — a mid-mutation op survives the FSM boundary.

When a graceful shutdown traps an op mid-mutation with a dirty git index, the
suspend serializer snapshots the working-tree delta (``git stash create -u``,
non-destructive) and binds the raw stash SHA INSIDE the HMAC-signed FSM payload.
On hydration the delta is re-applied by that SHA — so the op resumes against its
own uncommitted changes even if the tree was cleaned across the boundary
(rollback / worktree teardown / a cloud node's fresh checkout).

These tests use a REAL throwaway git repo (no mock VC) to pin: the stash/apply
round-trip restores a cleaned delta, a clean tree produces no ref, capture_inflight
stamps the ref onto the checkpoint and it survives the HMAC envelope, and a
disabled master / bad ref degrade fail-soft.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from backend.core.ouroboros.governance import fsm_checkpoint as CK
from backend.core.ouroboros.governance import in_flight_registry as R
from backend.core.ouroboros.governance import workspace_checkpoint as WC


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path):
    d = str(tmp_path / "wt")
    os.makedirs(d)
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (tmp_path / "wt" / "code.py").write_text("def foo():\n    return 1\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


def _dirty(repo, content="def foo():\n    return 42  # EDIT\n"):
    with open(os.path.join(repo, "code.py"), "w") as fh:
        fh.write(content)


def _clean(repo):
    _git(repo, "checkout", "--", "code.py")


def _read(repo):
    return open(os.path.join(repo, "code.py")).read()


# ---------------------------------------------------------------------------
# Core mechanism — stash create (non-destructive) + apply by raw SHA
# ---------------------------------------------------------------------------


def test_stash_create_apply_round_trip_restores_cleaned_delta(repo):
    _dirty(repo)
    ref = WC.create_stash_ref(repo)
    assert ref and len(ref) == 40
    # create is NON-destructive — the delta is still in the tree.
    assert "EDIT" in _read(repo)
    # Simulate the tree being cleaned across the boundary.
    _clean(repo)
    assert "EDIT" not in _read(repo)
    # Hydrate: re-apply by raw SHA.
    assert WC.apply_stash_ref(repo, ref) is True
    assert "EDIT" in _read(repo)


def test_clean_tree_produces_no_stash(repo):
    assert WC.create_stash_ref(repo) is None


@pytest.mark.parametrize("bad", ["", "not-a-sha", "z" * 40, "abc123"])
def test_apply_bad_ref_is_failsoft(repo, bad):
    assert WC.apply_stash_ref(repo, bad) is False


def test_apply_dangling_but_unknown_sha_returns_false(repo):
    # A well-formed but non-existent SHA → git errors → False, never raises.
    assert WC.apply_stash_ref(repo, "0" * 40) is False


# ---------------------------------------------------------------------------
# Suspend integration — capture_inflight stamps the ref; HMAC round-trip
# ---------------------------------------------------------------------------


@pytest.fixture()
def wired(monkeypatch, repo):
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_STASH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_ROOT", repo)
    R.reset_default_registry()
    yield
    R.reset_default_registry()


class _Ctx:
    def __init__(self, op_id):
        self.op_id = op_id
        self.description = "mutate code.py"
        self.target_files = ["code.py"]
        self.phase = "GENERATE"


def test_capture_inflight_stamps_stash_ref_and_survives_hmac(wired, repo, tmp_path):
    base = str(tmp_path / "ck")
    _dirty(repo)
    R.register_op_safely("op-mut", ctx_ref=_Ctx("op-mut"), last_phase_name="GENERATE")

    n = CK.capture_inflight(base_dir=base, reason="wall_clock_cap")
    assert n == 1
    cp = CK.list_pending(base_dir=base)[0]        # re-read from disk (HMAC-verified)
    assert cp.workspace_stash_ref and len(cp.workspace_stash_ref) == 40

    # END-TO-END: clean the tree, then hydrate-restore by the checkpoint's ref.
    _clean(repo)
    assert "EDIT" not in _read(repo)
    assert CK.restore_workspace_stash(cp.workspace_stash_ref) is True
    assert "EDIT" in _read(repo)


def test_clean_tree_stamps_empty_ref(wired, repo, tmp_path):
    base = str(tmp_path / "ck")
    # tree is clean (no _dirty)
    R.register_op_safely("op-clean", ctx_ref=_Ctx("op-clean"), last_phase_name="GENERATE")
    CK.capture_inflight(base_dir=base, reason="wall_clock_cap")
    cp = CK.list_pending(base_dir=base)[0]
    assert cp.workspace_stash_ref == ""


def test_stash_ref_is_hmac_bound(wired, repo, tmp_path):
    import json
    base = str(tmp_path / "ck")
    _dirty(repo)
    R.register_op_safely("op-h", ctx_ref=_Ctx("op-h"), last_phase_name="GENERATE")
    CK.capture_inflight(base_dir=base, reason="wall_clock_cap")
    # Forge the stash ref in the payload without re-signing → HMAC verify fails.
    path = os.path.join(CK.checkpoint_dir(base), "op-h.json")
    raw = json.loads(open(path).read())
    payload = json.loads(raw["payload"])
    payload["workspace_stash_ref"] = "f" * 40          # forged
    raw["payload"] = json.dumps(payload, sort_keys=True)
    open(path, "w").write(json.dumps(raw))
    assert CK.list_pending(base_dir=base) == []          # rejected


# ---------------------------------------------------------------------------
# Master switch + robustness
# ---------------------------------------------------------------------------


def test_disabled_captures_no_stash(monkeypatch, repo, tmp_path):
    monkeypatch.setenv("JARVIS_FSM_CHECKPOINT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_STASH_ENABLED", "false")
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_ROOT", repo)
    R.reset_default_registry()
    base = str(tmp_path / "ck")
    _dirty(repo)
    R.register_op_safely("op-off", ctx_ref=_Ctx("op-off"), last_phase_name="GENERATE")
    CK.capture_inflight(base_dir=base, reason="wall_clock_cap")
    cp = CK.list_pending(base_dir=base)[0]
    assert cp.workspace_stash_ref == ""
    # restore is a no-op when disabled
    assert CK.restore_workspace_stash("a" * 40) is False
    R.reset_default_registry()


def test_restore_empty_ref_is_noop(monkeypatch):
    monkeypatch.setenv("JARVIS_FSM_WORKSPACE_STASH_ENABLED", "true")
    assert CK.restore_workspace_stash("") is False


def test_helpers_never_raise_on_bad_root():
    assert WC.create_stash_ref("/proc/nonexistent/xyz") is None
    assert WC.apply_stash_ref("/proc/nonexistent/xyz", "a" * 40) is False
