"""Continuous local promotion: strict fast-forward, safe branch pruning, and
the event + status token the operator reads before deciding to push.

Driven against REAL throwaway git repositories: what is under test is git's
own behaviour (ancestry, compare-and-swap ref updates, checked-out branches),
which a mocked subprocess cannot demonstrate. Only the gate's VERIFICATION
(which runs pytest) is stubbed, and only where the test is about the
mechanism rather than the checks.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance import accumulation_promotion_gate as gate
from backend.core.ouroboros.governance import main_promoter as mp
from backend.core.ouroboros.governance.worktree_manager import (
    PromotionError, WorktreeManager,
)

SESSION = "bt-2099-01-01-000000"
BRANCH = f"ouroboros/auto/{SESSION}-abc123"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(root: Path, name: str, body: str, *, autonomous: bool = True) -> str:
    (root / name).write_text(body, encoding="utf-8")
    _git(root, "add", name)
    msg = f"test: add {name}"
    if autonomous:
        msg += f"\n\nOp-ID: op-{name}\nSession: {SESSION}\nFiles: {name}\n"
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """main with one commit; the session branch one landing ahead of it;
    the primary checkout parked on a third branch, as a soak leaves it."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "operator@example.test")
    _git(root, "config", "user.name", "Operator")
    _commit(root, "a.py", "A = 1\n", autonomous=False)
    _git(root, "checkout", "-q", "-b", BRANCH)
    landing = _commit(root, "b.py", "B = 2\n")
    # Forked from main, as the harness forks its accumulation branch: the
    # landing must exist ONLY on the session branch for the reaper tests to
    # mean anything.
    _git(root, "checkout", "-q", "-b", "ouroboros/battle-test/acc", "main")
    for var in ("JARVIS_OUROBOROS_SESSION_ID", "JARVIS_AUTO_COMMIT_WORKSPACE",
                "JARVIS_WORKTREE_REAP_PREFIXES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", "true")
    return SimpleNamespace(root=root, landing=landing,
                           mgr=WorktreeManager(repo_root=root))


def _run(coro):
    return asyncio.run(coro)


def _no_merge_state(root: Path) -> None:
    git_dir = root / ".git"
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REBASE_HEAD"):
        assert not (git_dir / marker).exists(), marker


# --------------------------------------------------------------------------
# Phase 1 — strict fast-forward
# --------------------------------------------------------------------------

def test_a_target_nobody_has_checked_out_moves_by_compare_and_swap(repo):
    result = _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    assert result.mode == "ff"
    assert _git(repo.root, "rev-parse", "main") == repo.landing
    # A fast-forward creates no commit: main's tip IS the landing.
    assert _git(repo.root, "rev-list", "--count", "main") == "2"
    _no_merge_state(repo.root)


def test_a_checked_out_target_moves_in_its_worktree_and_its_files_follow(repo):
    _git(repo.root, "checkout", "-q", "main")
    _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    assert _git(repo.root, "rev-parse", "HEAD") == repo.landing
    assert (repo.root / "b.py").read_text() == "B = 2\n"


def test_a_diverged_target_is_refused_and_nothing_moves(repo):
    _git(repo.root, "checkout", "-q", "main")
    operator = _commit(repo.root, "c.py", "C = 3\n", autonomous=False)
    with pytest.raises(PromotionError) as err:
        _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    assert err.value.state == "diverged"
    assert _git(repo.root, "rev-parse", "main") == operator
    assert _git(repo.root, "rev-parse", BRANCH) == repo.landing
    _no_merge_state(repo.root)


def test_promoting_a_commit_behind_the_branch_tip_lands_only_that_commit(repo):
    """The session may have landed again since; only the verified sha moves."""
    _git(repo.root, "checkout", "-q", BRANCH)
    later = _commit(repo.root, "d.py", "D = 4\n")
    _git(repo.root, "checkout", "-q", "ouroboros/battle-test/acc")
    _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    assert _git(repo.root, "rev-parse", "main") == repo.landing
    assert _git(repo.root, "rev-parse", BRANCH) == later


def test_an_unverified_commit_cannot_ride_along(repo):
    _git(repo.root, "checkout", "-q", BRANCH)
    later = _commit(repo.root, "d.py", "D = 4\n")
    with pytest.raises(PromotionError) as err:
        _run(repo.mgr.fast_forward_branch("main", BRANCH, [later]))
    assert err.value.state == "diverged"
    assert "2 commit(s), 1 were verified" in err.value.detail


def test_promote_commits_ff_only_never_cherry_picks(repo):
    _git(repo.root, "checkout", "-q", "main")
    _commit(repo.root, "c.py", "C = 3\n", autonomous=False)
    before = _git(repo.root, "rev-parse", "main")
    with pytest.raises(PromotionError):
        _run(repo.mgr.promote_commits(repo.root, BRANCH, [repo.landing], ff_only=True))
    assert _git(repo.root, "rev-parse", "main") == before
    _no_merge_state(repo.root)


# --------------------------------------------------------------------------
# Phase 2 — the one safe deletion rule
# --------------------------------------------------------------------------

def test_a_merged_branch_nobody_uses_is_deleted(repo):
    _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    assert _run(repo.mgr.delete_branch_if_merged(BRANCH, into="main")) == "deleted"
    assert BRANCH not in _git(repo.root, "branch", "--list", BRANCH)


def test_an_unmerged_branch_is_kept(repo):
    assert _run(repo.mgr.delete_branch_if_merged(BRANCH, into="main")) == "unmerged"
    assert _git(repo.root, "rev-parse", BRANCH) == repo.landing


def test_a_live_sessions_branch_is_kept_even_when_merged(repo, tmp_path):
    _run(repo.mgr.fast_forward_branch("main", BRANCH, [repo.landing]))
    _git(repo.root, "worktree", "add", "-q", str(tmp_path / "session"), BRANCH)
    assert _run(repo.mgr.delete_branch_if_merged(BRANCH, into="main")) == "checked_out"


def test_the_boot_reaper_no_longer_force_deletes_unpromoted_landings(repo):
    """THE data-loss defect: `branch -D` on every ouroboros/auto/bt-* branch
    at boot discarded landings that were never promoted."""
    orphan = "ouroboros/auto/bt-2098-01-01-000000-dead00"
    _git(repo.root, "branch", orphan, "main")          # merged: safe to drop
    _run(repo.mgr.reap_orphans())
    assert _git(repo.root, "rev-parse", BRANCH) == repo.landing
    assert not _git(repo.root, "branch", "--list", orphan)


def test_promotion_prunes_the_source_branch(repo, monkeypatch):
    monkeypatch.setattr(gate, "verify_commit", _passing_verify)
    verdict = _run(gate.promote_accumulation_commit(
        sha=repo.landing, branch=BRANCH, repo_root=repo.root,
        manager=repo.mgr, target_branch="main", record_lesson=_no_lesson,
    ))
    assert verdict.promoted and verdict.branch_disposition == "deleted"


async def _passing_verify(sha, **_kw):
    return (gate.Finding("stubbed", True, "verification is not under test"),)


async def _no_lesson(**_kw):
    return None


def test_a_diverged_promotion_logs_ERROR_and_pins_no_quarantine_ref(
    repo, monkeypatch, caplog,
):
    monkeypatch.setattr(gate, "verify_commit", _passing_verify)
    _git(repo.root, "checkout", "-q", "main")
    _commit(repo.root, "c.py", "C = 3\n", autonomous=False)
    with caplog.at_level(logging.ERROR, logger="Ouroboros.PromotionGate"):
        verdict = _run(gate.promote_accumulation_commit(
            sha=repo.landing, branch=BRANCH, repo_root=repo.root,
            manager=repo.mgr, target_branch="main", record_lesson=_no_lesson,
        ))
    assert (verdict.promoted, verdict.state) == (False, "diverged")
    assert any("Rebase" in r.getMessage() for r in caplog.records
               if r.levelno == logging.ERROR)
    assert not _git(repo.root, "for-each-ref", "refs/heads/ouroboros/quarantine")
    assert _git(repo.root, "rev-parse", BRANCH) == repo.landing


# --------------------------------------------------------------------------
# Phase 3 — the call site, the event, the status token
# --------------------------------------------------------------------------

def test_promote_landing_resolves_everything_from_git(repo, monkeypatch):
    monkeypatch.setattr(gate, "verify_commit", _passing_verify)
    monkeypatch.setattr(gate, "_record", _no_record)
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_TARGET", "main")
    out = _run(mp.promote_landing(repo.landing, manager=repo.mgr))
    assert out.verdict.promoted, out.verdict
    assert (out.target, out.source_branch) == ("main", BRANCH)
    assert out.unpushed is None                        # no origin to compare


async def _no_record(*_a, **_kw):
    return None


def test_end_to_end_through_the_REAL_gate(tmp_path, monkeypatch):
    """No stubbed verification: provenance, structure, semantic delta and a
    real pytest run of the covering test, then the fast-forward and the
    prune. It also pins the interpreter: the gate's default `python3` is the
    host's system Python, which has no pytest, and would have refused every
    real promotion with "could not run tests"."""
    monkeypatch.setattr(gate, "_record", _no_record)
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_TARGET", "main")
    for var in ("JARVIS_OUROBOROS_SESSION_ID", "JARVIS_AUTO_COMMIT_WORKSPACE"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "e2e"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "operator@example.test")
    _git(root, "config", "user.name", "Operator")
    _commit(root, "README.md", "seed\n", autonomous=False)
    _git(root, "checkout", "-q", "-b", BRANCH)
    (root / "tests").mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    _git(root, "add", "calc.py", "tests/test_calc.py")
    _git(root, "commit", "-q", "-m",
         f"feat: calc\n\nOp-ID: op-calc\nSession: {SESSION}\nFiles: calc.py\n")
    landing = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")

    mgr = WorktreeManager(repo_root=root, worktree_base=tmp_path / "wt")
    out = _run(mp.promote_landing(landing, manager=mgr))
    assert out.verdict.promoted, out.verdict.render()
    # The verification checkout is gone, from disk and from git's records.
    assert not list((tmp_path / "wt").glob("promote-verify-*"))
    assert "promote-verify-" not in _git(root, "worktree", "list")
    assert {f.check for f in out.verdict.findings} >= {
        "provenance", "structure", "semantic_delta", "coverage"}
    assert _git(root, "rev-parse", "main") == landing
    assert (root / "calc.py").exists()                  # main was checked out
    assert out.verdict.branch_disposition == "deleted"
    assert not _git(root, "branch", "--list", BRANCH)


def test_a_tests_only_landing_is_its_own_cover(tmp_path):
    """Found live (bt-2026-09-26-143412): every landing was a new test file
    for an uncovered module, and the gate skipped touched test files when
    collecting cover — so each resolved to zero tests and was refused as
    "no tests/**/test_<stem>.py" for a test file."""
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_queue.py").write_text("def test_x(): pass\n")
    (tmp_path / "tests" / "conftest.py").write_text("")
    got = gate._test_paths_for(
        ["tests/unit/test_queue.py", "tests/conftest.py", "tests/unit/test_gone.py"],
        tmp_path,
    )
    # The new test runs; a helper is not a test; a deleted file is not cover.
    assert got == ("tests/unit/test_queue.py",)


def test_no_target_is_a_refusal_not_a_guess(repo, monkeypatch):
    monkeypatch.delenv("JARVIS_ACCUMULATION_PROMOTION_TARGET", raising=False)
    out = _run(mp.promote_landing(repo.landing, manager=repo.mgr))
    assert out.verdict.state == "no_target"
    assert _git(repo.root, "rev-parse", "main") != repo.landing


class _Comm:
    def __init__(self):
        self.beats: List[dict] = []

    async def emit_heartbeat(self, **kw):
        self.beats.append(kw)


def _commit_beat(sha: str, op_id: str = "op-1"):
    return SimpleNamespace(
        msg_type=SimpleNamespace(value="HEARTBEAT"), op_id=op_id,
        payload={"phase": mp.COMMIT_PHASE, "commit_hash": sha},
    )


def test_the_transport_promotes_each_landing_once_and_announces_it(monkeypatch):
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", "true")
    calls: List[str] = []

    async def promote(sha):
        calls.append(sha)
        return mp.LandingPromotion(
            gate.PromotionVerdict(True, "promoted", (sha,), landed_shas=(sha,),
                                  branch_disposition="checked_out"),
            sha, "main", BRANCH, unpushed=3,
        )

    record = mp.PromotionRecord()
    t = mp.MainPromotionTransport(promote=promote, record=record,
                                  manager=_InertManager())
    comm = _Comm()
    t.bind(comm)

    async def scenario():
        await t.send(_commit_beat("f" * 40))
        await t.send(_commit_beat("f" * 40))            # re-emitted: once only
        await t._queue.join()
        await t.aclose()

    asyncio.run(scenario())
    assert calls == ["f" * 40]
    [beat] = comm.beats
    assert beat["phase"] == mp.PROMOTION_SUCCESS
    assert (beat["commit"], beat["target_branch"], beat["unpushed"]) == ("f" * 40, "main", 3)
    snap = record.snapshot()
    assert (snap.state, snap.sha, snap.total, snap.unpushed) == ("promoted", "f" * 40, 1, 3)


class _InertManager:
    async def prune_merged_branches(self, *_a, **_kw):
        return {}


def test_the_transport_is_inert_while_the_gate_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", raising=False)
    t = mp.MainPromotionTransport(promote=_fail_if_called, manager=_InertManager())
    asyncio.run(t.send(_commit_beat("e" * 40)))
    assert t._queue is None


async def _fail_if_called(sha):
    raise AssertionError("promoted while disabled")


def test_the_status_line_carries_the_promotion_across_the_bridge(monkeypatch):
    from backend.core.ouroboros.battle_test import status_line as sl
    mp.reset_for_tests()
    mp.get_promotion_record().record(
        promoted=True, target="main", sha="06b35c990f" + "0" * 30,
        state="promoted", unpushed=3,
    )
    snap = sl.StatusLineBuilder().snapshot()
    back = sl.payload_to_snapshot(sl.snapshot_to_payload(snap))
    assert sl._format_promotion_token(back) == "main ← 06b35c990f · 3 unpushed"
    mp.reset_for_tests()


@pytest.mark.parametrize("state,unpushed,want", [
    ("promoted", 0, "main ← 06b35c990f · pushed"),
    ("promoted", None, "main ← 06b35c990f"),
    ("diverged", None, "main ✗ diverged — rebase"),
    ("refused", None, "main ✗ refused"),
    ("", None, ""),
])
def test_render_promotion(state, unpushed, want):
    snap = mp.PromotionSnapshot(state=state, target="main",
                                sha="06b35c990f" + "0" * 30, unpushed=unpushed)
    assert mp.render_promotion(snap) == want


def test_ov_arms_promotion_with_the_production_profile_and_a_refusal_wins():
    from backend.core.ouroboros.cli import ov
    env: dict = {}
    ov.arm_production_soak(env)
    assert env["JARVIS_ACCUMULATION_PROMOTION_ENABLED"] == "true"
    env = {"JARVIS_ACCUMULATION_PROMOTION_ENABLED": "false"}
    ov.arm_production_soak(env)
    assert env["JARVIS_ACCUMULATION_PROMOTION_ENABLED"] == "false"
