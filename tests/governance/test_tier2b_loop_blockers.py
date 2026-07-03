"""Tier-2b — 2nd starvation tier: intake ingest / semantic-index build /
posture git-signals moved OFF the asyncio loop via the unified
``cooperative_fs_io.offload`` substrate.

After the FS-crawl class was fixed (bt-iso-1783093701 LoopSink aggregation),
three NON-FS blockers still starved the loop (max lag 50s, 64
ControlPlaneStarvation events):

  * ``intake.UnifiedIntakeRouter.ingest``   — ~3.5s/signal (two inline
    fastembed encodes on the loop)
  * ``semantic_index.SemanticIndex.build``  — ~6.4s/build (embedding index)
  * ``posture_observer.run_one_cycle`` + ``posture.signal.commit_ratios``
    — git subprocesses (git log) blocking the loop

Per blocker this suite proves:
  (a) the work routes through ``cooperative_fs_io.offload``
  (b) correctness parity vs the old sync path
  (c) fail-soft: OffloadError → degraded (never raised into the loop)
  (d) loop-responsiveness: a heartbeat coroutine keeps ticking while a slow
      offloaded call runs
  (e) race (SemanticIndex): a reader during a concurrent rebuild always sees a
      COMPLETE index (old or new), never a partial one
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import List, Sequence
from unittest import mock

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io as cfs
from backend.core.ouroboros.governance import semantic_index as si
from backend.core.ouroboros.governance import posture_observer as po


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_vec(text: str, dim: int = 16) -> List[float]:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    vals = [((b / 255.0) * 2.0 - 1.0) for b in h[:dim]]
    return vals


class _FakeEmbedder:
    model_name = "fake-embedder"

    def __init__(self) -> None:
        self.embed_calls = 0
        self.disabled = False

    def embed(self, texts: Sequence[str]):
        self.embed_calls += 1
        return [_fake_vec(t) for t in texts]


def _enable_semantic(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_SEMANTIC_INFERENCE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")


def _new_built_index(tmp_path, monkeypatch):
    """A SemanticIndex with a fake embedder and a synchronously-built,
    non-empty centroid so score/boost/build paths are live."""
    idx = si.SemanticIndex(tmp_path)
    monkeypatch.setattr(idx, "_embedder", _FakeEmbedder(), raising=True)
    # Seed a corpus directly so build has content even with no git history.
    monkeypatch.setattr(
        si, "_assemble_corpus",
        lambda root, **kw: [
            si.CorpusItem(text="feat: alpha", source=si.SOURCE_GIT_COMMIT,
                          ts=time.time(), halflife_days=14.0),
            si.CorpusItem(text="fix: beta", source=si.SOURCE_GIT_COMMIT,
                          ts=time.time(), halflife_days=14.0),
        ],
        raising=True,
    )
    return idx


class _OffloadSpy:
    """Wrap ``cooperative_fs_io.offload`` to record dispatches, delegating to
    the real implementation."""

    def __init__(self) -> None:
        self.calls: list = []
        self._real = cfs.offload

    async def __call__(self, fn, *args, cpu_bound=False, **kwargs):
        self.calls.append((getattr(fn, "__name__", repr(fn)), cpu_bound))
        return await self._real(fn, *args, cpu_bound=cpu_bound, **kwargs)


# ===========================================================================
# Blocker #1 — SemanticIndex.build  (THREAD: fastembed/numpy release the GIL)
# ===========================================================================


def test_semantic_build_offloaded_routes_through_offload(tmp_path, monkeypatch):
    """(a) build_offloaded dispatches _build_impl through the substrate,
    THREAD path (cpu_bound=False)."""
    _enable_semantic(monkeypatch)
    idx = _new_built_index(tmp_path, monkeypatch)
    spy = _OffloadSpy()
    monkeypatch.setattr(cfs, "offload", spy, raising=True)

    ok = asyncio.run(idx.build_offloaded(force=True))
    assert ok is True
    assert any(name == "_build_impl" and cpu is False for name, cpu in spy.calls)


def test_semantic_build_offloaded_parity_with_sync(tmp_path, monkeypatch):
    """(b) build_offloaded yields the SAME centroid as the sync build."""
    _enable_semantic(monkeypatch)
    idx_sync = _new_built_index(tmp_path, monkeypatch)
    assert idx_sync.build(force=True) is True
    centroid_sync = idx_sync.snapshot_global_centroid()

    idx_off = _new_built_index(tmp_path, monkeypatch)
    assert asyncio.run(idx_off.build_offloaded(force=True)) is True
    centroid_off = idx_off.snapshot_global_centroid()

    assert len(centroid_off) == len(centroid_sync)
    for a, b in zip(centroid_off, centroid_sync):
        assert a == pytest.approx(b)


def test_semantic_build_offloaded_failsoft_keeps_prior(tmp_path, monkeypatch):
    """(c) an OffloadError leaves the prior index intact and returns False."""
    _enable_semantic(monkeypatch)
    idx = _new_built_index(tmp_path, monkeypatch)
    # Build a good index first.
    assert idx.build(force=True) is True
    prior = idx.snapshot_global_centroid()
    assert prior  # non-empty

    async def _boom(fn, *args, cpu_bound=False, **kwargs):
        return cfs.OffloadError(
            fn_name="_build_impl", exc_type="RuntimeError",
            message="synthetic", cpu_bound=cpu_bound,
        )

    monkeypatch.setattr(cfs, "offload", _boom, raising=True)
    ok = asyncio.run(idx.build_offloaded(force=True))
    assert ok is False
    # Prior centroid preserved — no partial wipe.
    assert idx.snapshot_global_centroid() == prior


def test_semantic_build_offloaded_loop_stays_responsive(tmp_path, monkeypatch):
    """(d) a heartbeat coroutine keeps ticking while a slow build runs."""
    _enable_semantic(monkeypatch)
    idx = _new_built_index(tmp_path, monkeypatch)

    # Make the embed slow (simulates fastembed cold encode) — it runs in the
    # offload thread, so the loop must keep scheduling the heartbeat.
    slow = _FakeEmbedder()
    _orig_embed = slow.embed

    def _slow_embed(texts):
        time.sleep(0.4)
        return _orig_embed(texts)

    slow.embed = _slow_embed  # type: ignore[assignment]
    monkeypatch.setattr(idx, "_embedder", slow, raising=True)

    async def _run() -> int:
        ticks = {"n": 0}

        async def _heartbeat():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(_heartbeat())
        await idx.build_offloaded(force=True)
        hb.cancel()
        return ticks["n"]

    ticks = asyncio.run(_run())
    # If the build had blocked the loop for 0.4s, the 0.02s heartbeat could
    # not have ticked several times.
    assert ticks >= 5, f"heartbeat starved during offloaded build (ticks={ticks})"


def test_semantic_reader_during_rebuild_sees_complete_index(tmp_path, monkeypatch):
    """(e) a reader calling the index during a concurrent rebuild always sees
    a fully-built centroid (old or new), never a half-populated one."""
    _enable_semantic(monkeypatch)
    idx = _new_built_index(tmp_path, monkeypatch)
    assert idx.build(force=True) is True
    expected_dim = len(idx.snapshot_global_centroid())
    assert expected_dim > 0

    async def _run() -> None:
        observations = {"partial": 0, "checks": 0}

        async def _reader():
            for _ in range(200):
                snap = idx.snapshot_global_centroid()
                observations["checks"] += 1
                # Centroid is either empty (never, we pre-built) or full-dim —
                # never a partially-populated vector.
                if snap and len(snap) != expected_dim:
                    observations["partial"] += 1
                await asyncio.sleep(0)

        reader = asyncio.create_task(_reader())
        # Concurrent rebuilds hammering the same index.
        for _ in range(5):
            await idx.build_offloaded(force=True)
        await reader
        assert observations["partial"] == 0, "reader saw a half-built index"
        assert observations["checks"] > 0

    asyncio.run(_run())


def test_boost_and_score_offloaded_routes_and_parity(tmp_path, monkeypatch):
    """boost_and_score_offloaded (intake fast-path) routes through offload
    (THREAD) and returns the same boost/score as the sync boost_for/score."""
    _enable_semantic(monkeypatch)
    monkeypatch.setenv("JARVIS_SEMANTIC_ALIGNMENT_BOOST_MAX", "1")
    idx = _new_built_index(tmp_path, monkeypatch)
    assert idx.build(force=True) is True

    text = "feat: alpha follow-up"
    sync_boost = idx.boost_for(text)
    sync_score = idx.score(text)

    spy = _OffloadSpy()
    monkeypatch.setattr(cfs, "offload", spy, raising=True)
    off_boost, off_score = asyncio.run(idx.boost_and_score_offloaded(text))

    assert any(
        name == "_boost_and_score_sync" and cpu is False
        for name, cpu in spy.calls
    )
    assert off_boost == sync_boost
    assert off_score == pytest.approx(sync_score)


def test_boost_and_score_offloaded_failsoft(tmp_path, monkeypatch):
    """OffloadError → (0, 0.0), never raised."""
    _enable_semantic(monkeypatch)
    idx = _new_built_index(tmp_path, monkeypatch)
    assert idx.build(force=True) is True

    async def _boom(fn, *args, cpu_bound=False, **kwargs):
        return cfs.OffloadError(
            fn_name="_boost_and_score_sync", exc_type="X", message="y",
            cpu_bound=cpu_bound,
        )

    monkeypatch.setattr(cfs, "offload", _boom, raising=True)
    boost, score = asyncio.run(idx.boost_and_score_offloaded("anything"))
    assert boost == 0
    assert score == 0.0


# ===========================================================================
# Blocker #2 — posture git-signals  (THREAD: subprocess.run(git) releases GIL)
# ===========================================================================


def test_posture_commit_ratios_routes_through_offload(monkeypatch):
    """(a) commit_ratios_async dispatches the git calls through offload
    (THREAD)."""
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    sc = po.SignalCollector(Path("."))
    monkeypatch.setattr(sc, "_git_head", lambda: "head1")
    monkeypatch.setattr(
        sc, "_git_subjects",
        lambda n: ["feat: a", "fix: b", "refactor: c", "test: d"],
    )
    spy = _OffloadSpy()
    monkeypatch.setattr(cfs, "offload", spy, raising=True)

    ratios = asyncio.run(sc.commit_ratios_async())
    # Both git calls (HEAD + subjects) routed through offload, THREAD path.
    assert len(spy.calls) == 2
    assert all(cpu is False for _, cpu in spy.calls)
    # (b) parity: 4 subjects → 1/4 each of feat/fix/refactor, test_docs 1/4.
    assert ratios["feat"] == pytest.approx(1 / 4)
    assert ratios["fix"] == pytest.approx(1 / 4)
    assert ratios["refactor"] == pytest.approx(1 / 4)
    assert ratios["test_docs"] == pytest.approx(1 / 4)


def test_posture_commit_ratios_failsoft_offload_error(monkeypatch):
    """(c) OffloadError on the git calls → neutral zero ratios, no raise."""
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    sc = po.SignalCollector(Path("."))

    async def _boom(fn, *args, cpu_bound=False, **kwargs):
        return cfs.OffloadError(
            fn_name=getattr(fn, "__name__", "?"), exc_type="X",
            message="y", cpu_bound=cpu_bound,
        )

    monkeypatch.setattr(cfs, "offload", _boom, raising=True)
    ratios = asyncio.run(sc.commit_ratios_async())
    assert ratios == {"feat": 0.0, "fix": 0.0, "refactor": 0.0, "test_docs": 0.0}


def test_posture_git_runs_on_worker_thread(monkeypatch):
    """The git work runs on a NON-event-loop thread (Slice 257 invariant
    preserved via the substrate)."""
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    seen = {"head": None}
    loop_ident = {"v": None}

    def _head():
        seen["head"] = _thread_ident()
        return "h"

    async def _run():
        loop_ident["v"] = _thread_ident()
        sc = po.SignalCollector(Path("."))
        monkeypatch.setattr(sc, "_git_head", _head)
        monkeypatch.setattr(sc, "_git_subjects", lambda n: ["feat: x"])
        await sc.commit_ratios_async()

    asyncio.run(_run())
    assert seen["head"] is not None
    assert seen["head"] != loop_ident["v"]


def test_posture_cycle_loop_stays_responsive(monkeypatch):
    """(d) heartbeat keeps ticking while a slow git-log runs off-loop."""
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "true")
    sc = po.SignalCollector(Path("."))

    def _slow_subjects(n):
        time.sleep(0.4)
        return ["feat: x"]

    monkeypatch.setattr(sc, "_git_head", lambda: "h")
    monkeypatch.setattr(sc, "_git_subjects", _slow_subjects)

    async def _run() -> int:
        ticks = {"n": 0}

        async def _heartbeat():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.02)

        hb = asyncio.create_task(_heartbeat())
        await sc.commit_ratios_async()
        hb.cancel()
        return ticks["n"]

    ticks = asyncio.run(_run())
    assert ticks >= 5, f"heartbeat starved during git offload (ticks={ticks})"


def _thread_ident() -> int:
    import threading
    return threading.get_ident()


# ===========================================================================
# Blocker #3 — intake ingest  (THREAD: embedding releases the GIL)
# ===========================================================================
#
# What UnifiedIntakeRouter.ingest actually blocked on: NOT SemanticIndex.build
# (that was already fired via build_async, non-blocking) but the two INLINE
# fastembed encodes in _compute_priority (_si.boost_for + _si.score, ~1.7s
# each = ~3.5s). Fixing #1 does NOT resolve it; the embeds are the block.
# The fix routes a single combined encode through offload(cpu_bound=False).


def _prep_default_index(tmp_path, monkeypatch):
    si.reset_default_index()
    idx = si.get_default_index(tmp_path)
    monkeypatch.setattr(idx, "_embedder", _FakeEmbedder(), raising=True)
    monkeypatch.setattr(
        si, "_assemble_corpus",
        lambda root, **kw: [
            si.CorpusItem(text="feat: alpha", source=si.SOURCE_GIT_COMMIT,
                          ts=time.time(), halflife_days=14.0),
        ],
        raising=True,
    )
    idx.build(force=True)
    return idx


def test_ingest_routes_semantic_through_offload(tmp_path, monkeypatch):
    """(a) ingest dispatches the semantic prior through offload (THREAD)."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter, IntakeRouterConfig,
    )
    _enable_semantic(monkeypatch)
    _prep_default_index(tmp_path, monkeypatch)

    spy = _OffloadSpy()
    monkeypatch.setattr(cfs, "offload", spy, raising=True)

    async def _run() -> str:
        gls = mock.MagicMock()
        gls.submit = mock.AsyncMock()
        router = UnifiedIntakeRouter(
            gls=gls, config=IntakeRouterConfig(project_root=tmp_path,
                                               dedup_window_s=60.0),
        )
        await router.start()
        try:
            env = make_envelope(
                source="backlog", description="feat: alpha follow-up",
                target_files=("backend/x.py",), repo="jarvis",
                confidence=0.8, urgency="normal",
                evidence={"signature": "sig-ingest-1"}, requires_human_ack=False,
            )
            return await router.ingest(env)
        finally:
            await router.stop()

    result = asyncio.run(_run())
    assert result == "enqueued"
    assert any(
        name == "_boost_and_score_sync" and cpu is False
        for name, cpu in spy.calls
    ), "ingest did not route the semantic prior through offload (thread)"


def test_ingest_failsoft_when_offload_errors(tmp_path, monkeypatch):
    """(c) an OffloadError on the semantic prior still enqueues (intake
    proceeds without the bias, never raises)."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter, IntakeRouterConfig,
    )
    _enable_semantic(monkeypatch)
    _prep_default_index(tmp_path, monkeypatch)

    async def _boom(fn, *args, cpu_bound=False, **kwargs):
        return cfs.OffloadError(
            fn_name=getattr(fn, "__name__", "?"), exc_type="X",
            message="y", cpu_bound=cpu_bound,
        )

    monkeypatch.setattr(cfs, "offload", _boom, raising=True)

    async def _run() -> str:
        gls = mock.MagicMock()
        gls.submit = mock.AsyncMock()
        router = UnifiedIntakeRouter(
            gls=gls, config=IntakeRouterConfig(project_root=tmp_path,
                                               dedup_window_s=60.0),
        )
        await router.start()
        try:
            env = make_envelope(
                source="backlog", description="feat: whatever",
                target_files=("backend/y.py",), repo="jarvis",
                confidence=0.8, urgency="normal",
                evidence={"signature": "sig-ingest-2"}, requires_human_ack=False,
            )
            return await router.ingest(env)
        finally:
            await router.stop()

    assert asyncio.run(_run()) == "enqueued"


def test_compute_priority_override_matches_inline(tmp_path, monkeypatch):
    """(b) parity — priority computed with semantic_boost_override equals the
    priority the inline path produces for the same boost value."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake import (
        unified_intake_router as uir,
    )
    _enable_semantic(monkeypatch)
    _prep_default_index(tmp_path, monkeypatch)

    env = make_envelope(
        source="backlog", description="feat: alpha follow-up",
        target_files=("backend/z.py",), repo="jarvis",
        confidence=0.8, urgency="normal",
        evidence={"signature": "sig-parity"}, requires_human_ack=False,
    )
    # Inline path (override=None) computes semantic_boost internally.
    p_inline, _ = uir._compute_priority(env, repo_root=tmp_path)
    # Recover the boost the inline path used from evidence.
    boost_used = int(env.evidence.get("semantic_boost", 0))
    p_override, _ = uir._compute_priority(
        env, repo_root=tmp_path, semantic_boost_override=boost_used,
    )
    assert p_inline == p_override


def test_ingest_loop_stays_responsive(tmp_path, monkeypatch):
    """(d) heartbeat keeps ticking while a slow embed runs during ingest."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        UnifiedIntakeRouter, IntakeRouterConfig,
    )
    _enable_semantic(monkeypatch)
    idx = _prep_default_index(tmp_path, monkeypatch)

    slow = _FakeEmbedder()
    _orig = slow.embed

    def _slow_embed(texts):
        time.sleep(0.4)
        return _orig(texts)

    slow.embed = _slow_embed  # type: ignore[assignment]
    monkeypatch.setattr(idx, "_embedder", slow, raising=True)

    async def _run() -> int:
        ticks = {"n": 0}

        async def _heartbeat():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.02)

        gls = mock.MagicMock()
        gls.submit = mock.AsyncMock()
        router = UnifiedIntakeRouter(
            gls=gls, config=IntakeRouterConfig(project_root=tmp_path,
                                               dedup_window_s=60.0),
        )
        await router.start()
        hb = asyncio.create_task(_heartbeat())
        try:
            env = make_envelope(
                source="backlog", description="feat: slow embed path",
                target_files=("backend/s.py",), repo="jarvis",
                confidence=0.8, urgency="normal",
                evidence={"signature": "sig-slow"}, requires_human_ack=False,
            )
            await router.ingest(env)
        finally:
            hb.cancel()
            await router.stop()
        return ticks["n"]

    ticks = asyncio.run(_run())
    assert ticks >= 5, f"heartbeat starved during ingest embed (ticks={ticks})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
