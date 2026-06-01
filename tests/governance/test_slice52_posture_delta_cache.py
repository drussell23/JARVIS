"""Slice 52 Phase 2 — reactive posture commit-ratio caching.

Forensic basis (v46, bt-2026-06-01-053522): the dominant recurring on-loop
cost during the 20s control-plane starvation was the PostureObserver git
cycle — ``posture.signal.commit_ratios`` (up to 9.8s) re-running ``git log``
over the last N (=100) commits on EVERY 300s cycle, even when HEAD had not
moved. LoopSink ledger ranked it ~10x above every other callsite.

Fix: cache the computed ratios keyed by (HEAD hash, window). A cheap
``git rev-parse HEAD`` gate (sub-tens-of-ms) short-circuits the 100-commit
``git log`` whenever HEAD is unchanged — the common case, since auto-commits
are rare relative to the 300s cadence. Recompute only when HEAD advances.
When HEAD is unresolvable (no git / detached), never cache — recompute so a
stale value can't pin the posture.

This zeroes the *repeated* traversal cost (not the single cold scan), keeping
the steady-state posture git cost well under 50ms.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.posture_observer import SignalCollector


def _make(tmp_path):
    return SignalCollector(tmp_path)


@pytest.mark.asyncio
async def test_caches_commit_ratios_when_head_unchanged(tmp_path, monkeypatch):
    sc = _make(tmp_path)
    calls = {"subjects": 0}

    async def fake_subjects(n):  # noqa: ANN001
        calls["subjects"] += 1
        return ["feat: a", "fix: b", "feat: c", "refactor: d"]

    async def fake_head():
        return "deadbeef1234"

    monkeypatch.setattr(sc, "_git_subjects_async", fake_subjects)
    monkeypatch.setattr(sc, "_git_head_async", fake_head)

    r1 = await sc.commit_ratios_async()
    r2 = await sc.commit_ratios_async()

    assert r1 == r2
    # Second call served from cache — the expensive git-log must NOT re-run.
    assert calls["subjects"] == 1, f"git log re-ran on unchanged HEAD ({calls['subjects']}x)"
    # Sanity on the ratio math (4 subjects: 2 feat, 1 fix, 1 refactor).
    assert r1["feat"] == pytest.approx(0.5)
    assert r1["fix"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_recomputes_when_head_advances(tmp_path, monkeypatch):
    sc = _make(tmp_path)
    calls = {"subjects": 0}
    heads = iter(["h1", "h2"])  # HEAD moves between the two calls

    async def fake_subjects(n):  # noqa: ANN001
        calls["subjects"] += 1
        return ["feat: a"]

    async def fake_head():
        return next(heads)

    monkeypatch.setattr(sc, "_git_subjects_async", fake_subjects)
    monkeypatch.setattr(sc, "_git_head_async", fake_head)

    await sc.commit_ratios_async()
    await sc.commit_ratios_async()

    assert calls["subjects"] == 2, "HEAD advanced — ratios must be recomputed"


@pytest.mark.asyncio
async def test_never_caches_when_head_unresolvable(tmp_path, monkeypatch):
    sc = _make(tmp_path)
    calls = {"subjects": 0}

    async def fake_subjects(n):  # noqa: ANN001
        calls["subjects"] += 1
        return ["feat: a"]

    async def fake_head():
        return ""  # no git / detached — unresolvable

    monkeypatch.setattr(sc, "_git_subjects_async", fake_subjects)
    monkeypatch.setattr(sc, "_git_head_async", fake_head)

    await sc.commit_ratios_async()
    await sc.commit_ratios_async()

    # No HEAD anchor -> must not serve a (possibly stale) cached value.
    assert calls["subjects"] == 2, "must recompute when HEAD is unresolvable"


@pytest.mark.asyncio
async def test_empty_history_baseline_is_cached_safely(tmp_path, monkeypatch):
    sc = _make(tmp_path)
    calls = {"subjects": 0}

    async def fake_subjects(n):  # noqa: ANN001
        calls["subjects"] += 1
        return []  # empty history

    async def fake_head():
        return "abc"

    monkeypatch.setattr(sc, "_git_subjects_async", fake_subjects)
    monkeypatch.setattr(sc, "_git_head_async", fake_head)

    r1 = await sc.commit_ratios_async()
    r2 = await sc.commit_ratios_async()
    assert r1 == r2 == {"feat": 0.0, "fix": 0.0, "refactor": 0.0, "test_docs": 0.0}
    # Empty-history baseline still anchors to HEAD -> cached, no re-run.
    assert calls["subjects"] == 1
