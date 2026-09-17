"""Quarantine is the forensic record. It fails closed, and it never overwrites.

Live precedent: a mis-specified promotion left main at 0f9ef8808d and pinned
ouroboros/quarantine/bt-2026-09-09-024244-6f47b1-PromotionError. The ref is the
only thing that makes that wreckage findable afterwards, so a write that cannot
happen must be loud rather than silent -- and a name already describing a
DIFFERENT failure must not be repointed, because both records matter.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import accumulation_promotion_gate as G


class _Git:
    """Scripted git: (args-prefix -> (rc, stdout))."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, args, cwd, timeout_s=15.0):
        self.calls.append(tuple(args))
        for prefix, result in self.table:
            if tuple(args[:len(prefix)]) == tuple(prefix):
                return result
        return 1, ""


def test_a_repeat_pin_of_the_same_state_is_not_a_collision(monkeypatch, tmp_path):
    """Idempotent: the same failure pinned twice keeps one ref."""
    git = _Git([
        (["rev-parse", "--verify", "--quiet", "ouroboros/quarantine/x-boom"], (0, "abc123\n")),
        (["rev-parse", "--verify", "--quiet"], (0, "abc123\n")),
    ])
    monkeypatch.setattr(G, "_git", git)
    ref = asyncio.run(G._quarantine("abc123", "ouroboros/auto/x", tmp_path, "boom"))
    assert ref.endswith("x-boom")
    assert ("branch",) not in [c[:1] for c in git.calls], "it repointed an existing ref"


def test_a_name_describing_a_different_failure_fails_closed(monkeypatch, tmp_path):
    """Overwriting would erase one forensic record to write another."""
    git = _Git([
        (["rev-parse", "--verify", "--quiet", "ouroboros/quarantine/x-boom"], (0, "olddeadbeef\n")),
        (["rev-parse", "--verify", "--quiet"], (0, "newc0ffee11\n")),
    ])
    monkeypatch.setattr(G, "_git", git)
    with pytest.raises(G.QuarantineIsolationFault) as exc:
        asyncio.run(G._quarantine("newc0ffee11", "ouroboros/auto/x", tmp_path, "boom"))
    assert exc.value.reason == "ref_collision"
    assert "olddeadbeef"[:12] in str(exc.value)
    assert ("branch",) not in [c[:1] for c in git.calls]


def test_a_refused_ref_write_is_a_fault_not_a_shrug(monkeypatch, tmp_path):
    git = _Git([
        (["rev-parse"], (1, "")),
        (["branch"], (1, "")),
    ])
    monkeypatch.setattr(G, "_git", git)
    with pytest.raises(G.QuarantineIsolationFault) as exc:
        asyncio.run(G._quarantine("abc", "ouroboros/auto/x", tmp_path, "boom"))
    assert exc.value.reason == "ref_write_failed"


def test_an_unwritable_quarantine_still_leaves_a_verdict(monkeypatch, tmp_path):
    """main's cleanliness is the mechanism's guarantee; the verdict must report
    BOTH failures rather than a conflict whose evidence was never pinned."""
    class _Mgr:
        async def promote_commits(self, **kw):
            raise RuntimeError("conflict")

    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", "true")

    async def _ok(*a, **k):
        return (G.Finding("all", True, "stub"),)

    async def _fault(*a, **k):
        raise G.QuarantineIsolationFault("ref_collision", "taken", ref="r")

    monkeypatch.setattr(G, "verify_commit", _ok)
    monkeypatch.setattr(G, "_quarantine", _fault)
    lessons = []

    async def _rec(**kw):
        lessons.append(kw)

    v = asyncio.run(G.promote_accumulation_commit(
        sha="abc", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Mgr(), record_lesson=_rec,
    ))
    assert v.promoted is False
    assert v.state == "quarantine_isolation_fault"
    assert lessons and lessons[0]["failure_class"] == "quarantine_isolation"


def test_gc_prunes_only_what_git_calls_prunable(monkeypatch, tmp_path):
    """A live worktree can hold uncommitted work; prune touches only entries
    whose directory is already gone."""
    listing = (
        "worktree /home/x/jarvis\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /tmp/jarvis_repair_sandbox_a\nHEAD def\nprunable gitdir missing\n\n"
        "worktree /home/x/jarvis/.worktrees/live\nHEAD 111\nbranch refs/heads/live\n"
    )
    git = _Git([
        (["worktree", "list"], (0, listing)),
        (["worktree", "prune"], (0, "")),
    ])
    monkeypatch.setattr(G, "_git", git)
    count, names = G.gc_stale_worktrees(tmp_path)
    assert count == 1
    assert names == ("/tmp/jarvis_repair_sandbox_a",)


def test_gc_is_a_noop_when_nothing_is_stale(monkeypatch, tmp_path):
    git = _Git([(["worktree", "list"], (0, "worktree /home/x\nHEAD abc\n"))])
    monkeypatch.setattr(G, "_git", git)
    assert G.gc_stale_worktrees(tmp_path) == (0, ())
    assert ["worktree", "prune"] not in [list(c) for c in git.calls], (
        "prune ran with nothing prunable"
    )


def test_gc_never_raises(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(G, "_git", _boom)
    assert G.gc_stale_worktrees(tmp_path) == (0, ())


def test_gc_runs_only_after_the_state_is_pinned(monkeypatch, tmp_path):
    """Order matters: the ref is what makes the wreckage survivable, and
    bookkeeping must never run before the record exists."""
    import inspect

    src = inspect.getsource(G.promote_accumulation_commit)
    assert src.index("_quarantine(") < src.index("gc_stale_worktrees(")
