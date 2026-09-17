"""A landing earns main, or it stays quarantined. It is never promoted on trust.

The gate was built because the first real accumulation landing would have
passed the two checks that were asked for. ``7f8c686ce0`` carries valid
trailers and its module's tests are green — and it also deleted that module's
``__all__``, de-indented a docstring line and churned quote style, while its
own message said "Keep behaviour otherwise identical". That is the whole-file
re-emission signature, and no test in this repository can see it.

Live verdicts, in order, on the real branch:

    [ok]     provenance: session=bt-2026-09-09-024244 op=op-01a0840c-...
    [REFUSE] structure:   __all__ removed (3 exports)
    [ok]     coverage:    green

    ...after the collateral repair commit, over the range:

    [ok] provenance: 1 autonomous, 1 operator
    [ok] structure:  no public surface lost
    [ok] coverage:   green
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import accumulation_promotion_gate as G


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", "true")
    yield


class _Manager:
    """Stands in for WorktreeManager — the only thing allowed to touch git."""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    async def promote_commits(self, *, target_root, branch, commit_shas, **kw):
        self.calls.append((branch, tuple(commit_shas)))
        if self._raises is not None:
            raise self._raises
        return type("R", (), {
            "promoted_shas": tuple(commit_shas),
            "landed_shas": tuple(commit_shas),
            "strategy": "ff",
        })()


def _ok(check):
    return G.Finding(check, True, "stub")


def _bad(check):
    return G.Finding(check, False, "stub refusal")


# --------------------------------------------------------------------------
# Refusal never merges, and never rolls back
# --------------------------------------------------------------------------

def test_a_failing_check_refuses_and_leaves_git_untouched(monkeypatch, tmp_path):
    mgr = _Manager()
    monkeypatch.setattr(
        G, "verify_commit",
        lambda *a, **k: _async([_ok("provenance"), _bad("structure"), _ok("coverage")]),
    )
    lessons = []
    v = asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=mgr, record_lesson=_recorder(lessons),
    ))
    assert v.promoted is False
    assert v.state == "refused"
    assert mgr.calls == [], "a refused promotion still called the merge mechanism"
    assert [f.check for f in v.refusals] == ["structure"]


def test_a_refusal_is_recorded_as_a_lesson(monkeypatch, tmp_path):
    """A refusal is the signal the generating lane needs and never got."""
    monkeypatch.setattr(
        G, "verify_commit", lambda *a, **k: _async([_bad("structure")]),
    )
    lessons = []
    asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Manager(), record_lesson=_recorder(lessons),
    ))
    assert lessons and lessons[0]["failure_class"] == "promotion_refused"
    assert lessons[0]["phase"] == "PROMOTE"


def test_a_success_is_NOT_written_to_lesson_memory(monkeypatch, tmp_path):
    """LessonMemory is failure-mode memory feeding few-shot injection. Writing
    successes into it degrades the corpus it exists to be."""
    monkeypatch.setattr(G, "verify_commit", lambda *a, **k: _async([_ok("all")]))
    lessons = []
    v = asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Manager(), record_lesson=_recorder(lessons),
    ))
    assert v.promoted is True
    assert lessons == []


def test_the_gate_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("JARVIS_ACCUMULATION_PROMOTION_ENABLED", raising=False)
    mgr = _Manager()
    v = asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path, manager=mgr,
    ))
    assert v.state == "gate_disabled" and mgr.calls == []


# --------------------------------------------------------------------------
# Conflict: fail closed, quarantine, keep going
# --------------------------------------------------------------------------

def test_a_conflict_quarantines_and_does_not_raise(monkeypatch, tmp_path):
    """Fired live: a cherry-pick that could not apply left main untouched, the
    state pinned, and the fault recorded — with no exception escaping into the
    worker pool."""
    class _Err(RuntimeError):
        reason = "conflict_aborted"

    monkeypatch.setattr(G, "verify_commit", lambda *a, **k: _async([_ok("all")]))
    pinned = []

    async def _fake_quarantine(sha, branch, repo_root, reason):
        pinned.append((sha, branch, reason))
        return f"ouroboros/quarantine/{branch.rsplit('/', 1)[-1]}-{reason}"

    monkeypatch.setattr(G, "_quarantine", _fake_quarantine)
    lessons = []
    v = asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Manager(raises=_Err("could not apply")),
        record_lesson=_recorder(lessons),
    ))
    assert v.promoted is False
    assert v.state == "conflict_quarantined"
    assert pinned and pinned[0][2] == "conflict_aborted"
    assert lessons[0]["failure_class"] == "promotion_conflict"


def test_a_conflict_does_not_stall_the_next_promotion(monkeypatch, tmp_path):
    """The pool keeps moving: a conflicted promotion is one op's verdict, not
    a latch on the lane."""
    monkeypatch.setattr(G, "verify_commit", lambda *a, **k: _async([_ok("all")]))
    monkeypatch.setattr(
        G, "_quarantine", lambda *a, **k: _async_value("ouroboros/quarantine/x"),
    )
    bad = asyncio.run(G.promote_accumulation_commit(
        sha="a", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Manager(raises=RuntimeError("locked")),
        record_lesson=_recorder([]),
    ))
    good = asyncio.run(G.promote_accumulation_commit(
        sha="b", branch="ouroboros/auto/y", repo_root=tmp_path,
        manager=_Manager(), record_lesson=_recorder([]),
    ))
    assert bad.promoted is False and good.promoted is True


def test_concurrent_promotions_are_independent(monkeypatch, tmp_path):
    """Multi-session cadence: one branch's conflict must not fail another's."""
    monkeypatch.setattr(G, "verify_commit", lambda *a, **k: _async([_ok("all")]))
    monkeypatch.setattr(
        G, "_quarantine", lambda *a, **k: _async_value("ouroboros/quarantine/x"),
    )

    async def _both():
        return await asyncio.gather(
            G.promote_accumulation_commit(
                sha="a", branch="ouroboros/auto/x", repo_root=tmp_path,
                manager=_Manager(raises=RuntimeError("locked")),
                record_lesson=_recorder([]),
            ),
            G.promote_accumulation_commit(
                sha="b", branch="ouroboros/auto/y", repo_root=tmp_path,
                manager=_Manager(), record_lesson=_recorder([]),
            ),
        )

    bad, good = asyncio.run(_both())
    assert bad.state == "conflict_quarantined"
    assert good.promoted is True


def test_a_broken_lesson_seam_never_changes_the_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "verify_commit", lambda *a, **k: _async([_bad("x")]))

    async def _boom(**kw):
        raise RuntimeError("lesson store down")

    v = asyncio.run(G.promote_accumulation_commit(
        sha="deadbeef", branch="ouroboros/auto/x", repo_root=tmp_path,
        manager=_Manager(), record_lesson=_boom,
    ))
    assert v.state == "refused"


def test_the_fault_carries_its_quarantine_ref():
    fault = G.PromotionConflictFault("conflict_aborted", "x", quarantine_ref="refs/q")
    assert fault.quarantine_ref == "refs/q"
    assert fault.reason == "conflict_aborted"


# --------------------------------------------------------------------------
# The structural check, which is the one that caught the real defect
# --------------------------------------------------------------------------

def test_a_dropped_export_is_detected():
    before = 'def a():\n    pass\n\n__all__ = ["a", "b"]\n'
    after = 'def a():\n    pass\n'
    b, a = G._public_surface(before), G._public_surface(after)
    assert b["all"] - a["all"] == {"a", "b"}


def test_a_dropped_function_is_detected():
    b = G._public_surface("def a():\n    pass\ndef b():\n    pass\n")
    a = G._public_surface("def a():\n    pass\n")
    assert b["names"] - a["names"] == {"b"}


def test_adding_public_surface_is_not_a_regression():
    """A shrink check, not an equality check — adding is ordinary work."""
    b = G._public_surface("def a():\n    pass\n")
    a = G._public_surface("def a():\n    pass\ndef c():\n    pass\n")
    assert not (b["names"] - a["names"])


def test_private_names_are_not_public_surface():
    s = G._public_surface("def _hidden():\n    pass\ndef shown():\n    pass\n")
    assert s["names"] == {"shown"}


def test_unparseable_source_is_reported_not_swallowed():
    assert G._public_surface("def (:\n") is None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _async(value):
    async def _run(*a, **k):
        return tuple(value)
    return _run()


def _async_value(value):
    async def _run(*a, **k):
        return value
    return _run()


def _recorder(sink):
    async def _rec(**kw):
        sink.append(kw)
        return "ok"
    return _rec
