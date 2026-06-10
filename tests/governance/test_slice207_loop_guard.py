"""Slice 207 — Class-level loop guard on SemanticIndex.build().

Slice 206 proved (and I reported honestly) that the 25s loop freeze persists:
a non-singleton SemanticIndex somewhere calls the SYNC build() directly on the
event loop. Rather than hunt that caller (and stay vulnerable to the next
module that does the same), the CLASS itself becomes loop-aware: if build() is
invoked synchronously on the running event loop, it does NOT block — it
redirects to the existing thread-offloaded build_async (single-flight) and
returns the current (eventually-consistent) built-state immediately.

Why this is coherent (the plan's run_coroutine_threadsafe().result() is NOT):
a sync method returning bool cannot offload-and-wait without re-blocking the
loop or deadlocking. The viable pivot — valid here because the index is
ADVISORY/eventually-consistent — is "schedule the thread build, return the
last-known-good now." In a worker thread get_running_loop() raises, so the
guard never fires there and never recurses.

Gated JARVIS_LOOP_GUARD_ENABLED default-FALSE (OFF = byte-identical: build()
always runs _build_impl). Off-loop callers are unaffected either way.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.semantic_index import (
    SemanticIndex,
    loop_guard_enabled,
)

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" \
    / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_LOOP_GUARD_ENABLED", raising=False)
    yield


def _idx(tmp_path):
    return SemanticIndex(project_root=tmp_path)


# ===========================================================================
# A — gate
# ===========================================================================

def test_loop_guard_disabled_by_default():
    assert loop_guard_enabled() is False


# ===========================================================================
# B — off-loop: byte-identical (runs the real build)
# ===========================================================================

def test_offloop_runs_real_build(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_GUARD_ENABLED", "1")
    idx = _idx(tmp_path)
    calls = {"impl": 0, "async": 0}
    monkeypatch.setattr(idx, "_build_impl", lambda *, force=False: calls.__setitem__("impl", calls["impl"] + 1) or True)
    monkeypatch.setattr(idx, "build_async", lambda: calls.__setitem__("async", calls["async"] + 1) or "started")
    # No running loop here → guard must NOT fire → real build runs.
    idx.build(force=True)
    assert calls["impl"] == 1 and calls["async"] == 0


# ===========================================================================
# C — on-loop: redirect to thread-offload, do NOT block (the fix)
# ===========================================================================

def test_onloop_redirects_to_build_async(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_GUARD_ENABLED", "1")
    idx = _idx(tmp_path)
    calls = {"impl": 0, "async": 0}
    monkeypatch.setattr(idx, "_build_impl", lambda *, force=False: calls.__setitem__("impl", calls["impl"] + 1) or True)
    monkeypatch.setattr(idx, "build_async", lambda: calls.__setitem__("async", calls["async"] + 1) or "started")

    async def _run():
        # Called synchronously INSIDE the running loop → guard fires.
        return idx.build(force=True)

    result = asyncio.run(_run())
    assert isinstance(result, bool)
    assert calls["async"] == 1          # redirected to thread-offload
    assert calls["impl"] == 0           # the loop was NEVER blocked by _build_impl


def test_onloop_returns_built_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_GUARD_ENABLED", "1")
    idx = _idx(tmp_path)
    monkeypatch.setattr(idx, "build_async", lambda: "started")
    monkeypatch.setattr(idx, "_build_impl", lambda *, force=False: True)
    # cold (never built) → returns False (advisory degrade, non-blocking)
    idx._built_at = 0.0
    assert asyncio.run(_run_build(idx)) is False
    # already built → returns True (last-known-good)
    idx._built_at = 12345.0
    assert asyncio.run(_run_build(idx)) is True


async def _run_build(idx):
    return idx.build(force=True)


# ===========================================================================
# D — guard disabled: byte-identical even on the loop
# ===========================================================================

def test_disabled_on_loop_runs_real_build(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LOOP_GUARD_ENABLED", "false")
    idx = _idx(tmp_path)
    calls = {"impl": 0, "async": 0}
    monkeypatch.setattr(idx, "_build_impl", lambda *, force=False: calls.__setitem__("impl", calls["impl"] + 1) or True)
    monkeypatch.setattr(idx, "build_async", lambda: calls.__setitem__("async", calls["async"] + 1) or "started")
    asyncio.run(_run_build(idx))
    assert calls["impl"] == 1 and calls["async"] == 0  # legacy behavior


# ===========================================================================
# E — recursion safety (worker thread has no loop → real build)
# ===========================================================================

def test_worker_thread_runs_real_build(tmp_path, monkeypatch):
    """The guard must NOT recurse: build_async's daemon thread calls build()
    with no running loop → real build runs there."""
    monkeypatch.setenv("JARVIS_LOOP_GUARD_ENABLED", "1")
    idx = _idx(tmp_path)
    calls = {"impl": 0}
    monkeypatch.setattr(idx, "_build_impl", lambda *, force=False: calls.__setitem__("impl", calls["impl"] + 1) or True)
    import threading
    t = threading.Thread(target=lambda: idx.build(force=True))
    t.start(); t.join(timeout=5)
    assert calls["impl"] == 1  # ran the real build in the thread (no redirect)


# ===========================================================================
# F — wiring pin
# ===========================================================================

def test_build_has_loop_guard():
    src = (_GOV / "semantic_index.py").read_text(encoding="utf-8")
    assert "loop_guard_enabled" in src
    assert "get_running_loop" in src
