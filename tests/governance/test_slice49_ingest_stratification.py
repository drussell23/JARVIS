"""Slice 49 Phase 3 — universal ingestion stratification (soft, at the funnel).

OperationAdvisor.advise() already HARD-gates blast-radius on every op
(classify_runner.py:396). This adds a SOFT priority penalty at the single
funnel UnifiedIntakeRouter.ingest -> _compute_priority, so large uncovered
targets are deprioritized (processed last) fleet-wide across ALL sensor
tracks — while staying fully reachable (no drop), with a test-gen escape.

Pins:
  §1  no files / covered file → zero penalty
  §2  huge uncovered file → positive penalty (deprioritized)
  §3  suppress (test-gen escape) → zero penalty
  §4  penalty is bounded (cannot dominate the whole priority scale)
  §5  _compute_priority: huge-uncovered envelope ranks WORSE than small-covered
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.target_stratification import (
    ingest_priority_penalty,
)


def _mk(root: Path, rel: str, lines: int, *, covered: bool) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x = 1\n" * lines)
    if covered:
        (root / "tests").mkdir(exist_ok=True)
        (root / "tests" / f"test_{Path(rel).stem}.py").write_text("def test(): pass\n")
    return rel


# ── §1 ─────────────────────────────────────────────────────────────────
def test_no_files_or_covered_is_zero(tmp_path: Path) -> None:
    assert ingest_priority_penalty([], tmp_path) == 0
    rel = _mk(tmp_path, "backend/big.py", 5000, covered=True)
    assert ingest_priority_penalty([rel], tmp_path) == 0


# ── §2 ─────────────────────────────────────────────────────────────────
def test_huge_uncovered_gets_penalty(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    assert ingest_priority_penalty([rel], tmp_path) > 0


# ── §3 ─────────────────────────────────────────────────────────────────
def test_suppress_escape_hatch(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    assert ingest_priority_penalty([rel], tmp_path, suppress=True) == 0


# ── §4 ─────────────────────────────────────────────────────────────────
def test_penalty_is_bounded(tmp_path: Path) -> None:
    rels = [_mk(tmp_path, f"backend/h{i}.py", 9000, covered=False) for i in range(5)]
    pen = ingest_priority_penalty(rels, tmp_path)
    assert 0 < pen <= 5  # bounded — cannot swamp base priorities (1..99)


# ── §5 integration with _compute_priority ───────────────────────────────
def test_compute_priority_deprioritizes_huge_uncovered(tmp_path: Path) -> None:
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _compute_priority,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

    huge = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    small = _mk(tmp_path, "backend/small.py", 40, covered=True)

    common = dict(
        source="ai_miner", description="improve", repo="jarvis",
        confidence=0.5, urgency="normal", evidence={}, requires_human_ack=True,
    )
    env_huge = make_envelope(target_files=(huge,), **common)
    env_small = make_envelope(target_files=(small,), **common)

    p_huge, _ = _compute_priority(env_huge, repo_root=tmp_path)
    p_small, _ = _compute_priority(env_small, repo_root=tmp_path)

    # lower int = higher priority; the huge uncovered op must rank WORSE
    assert p_huge > p_small


# ═══════════════════════════════════════════════════════════════════════
# Tier-4 loop-starvation fix — the stratification coverage scan must NEVER
# run synchronously on the event loop inside ingest.
#
# Traced mechanism (bt-iso-1783102490): _ingest_impl → _compute_priority →
# ingest_priority_penalty → file_has_test_coverage, whose Strategy 1 does
# sorted(top.rglob("test_*.py")) over the FULL tests/ tree (~2,839 files)
# on EVERY call, and whose Strategy 2 cold-builds an AST import map by
# reading + ast.parse-ing ALL test files (~963K lines) on the FIRST miss —
# the observed 41,713 ms single on-loop block.
#
# Fix contract:
#   * ingest proceeds DEGRADED (penalty override 0, no evidence stash)
#     until the coverage index is built off-loop (single-flight);
#   * once warm, the penalty is computed via the offload substrate
#     (pure in-memory index lookups in a worker thread);
#   * file_has_test_coverage is never invoked on the loop thread by ingest;
#   * builder failure is fail-soft: ingest still returns "enqueued".
# ═══════════════════════════════════════════════════════════════════════
import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import backend.core.ouroboros.governance.target_stratification as ts
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)


def _mk_router(tmp_path: Path):
    gls = MagicMock()
    gls.submit = AsyncMock()
    config = IntakeRouterConfig(project_root=tmp_path, dedup_window_s=60.0)
    return UnifiedIntakeRouter(gls=gls, config=config)


def _mk_env(rel: str, n: int = 0):
    return make_envelope(
        source="backlog",
        description=f"improve module {n}",
        target_files=(rel,),
        repo="jarvis",
        confidence=0.5,
        urgency="normal",
        evidence={"signature": f"sig-{n}-{time.monotonic()}"},
        requires_human_ack=False,
    )


@pytest.fixture(autouse=True)
def _reset_coverage_index_state():
    """Isolate per-test module-level coverage-index state (fail-soft pre-fix)."""
    reset = getattr(ts, "reset_coverage_index", None)
    if reset is not None:
        reset()
    ts._strat_ast_cache.clear()
    yield
    if reset is not None:
        reset()
    ts._strat_ast_cache.clear()


async def _hb_gaps(run_coro, cadence: float = 0.02):
    """Run ``run_coro`` while measuring event-loop heartbeat gaps."""
    gaps: list = []
    stop = asyncio.Event()

    async def _hb() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await asyncio.sleep(cadence)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    hb = asyncio.create_task(_hb())
    try:
        result = await run_coro
    finally:
        stop.set()
        await hb
    return result, gaps


# ── RED §6: the coverage scan must not execute on the loop thread ───────
@pytest.mark.asyncio
async def test_ingest_never_runs_coverage_scan_on_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 3000, covered=False)
    router = _mk_router(tmp_path)

    loop_thread = threading.get_ident()
    on_loop_calls = {"n": 0}
    real = ts.file_has_test_coverage

    def slow_spy(*a, **k):
        if threading.get_ident() == loop_thread:
            on_loop_calls["n"] += 1
        time.sleep(0.4)  # stand-in for the 2,839-file rglob / AST cold build
        return real(*a, **k)

    monkeypatch.setattr(ts, "file_has_test_coverage", slow_spy)

    result, gaps = await _hb_gaps(router.ingest(_mk_env(rel)))
    assert result == "enqueued"
    # The traced bug: file_has_test_coverage ran synchronously on the loop.
    assert on_loop_calls["n"] == 0, (
        f"coverage scan executed {on_loop_calls['n']}x on the event-loop "
        "thread inside ingest (Tier-4 starvation mechanism)"
    )
    assert gaps and max(gaps) < 0.3, (
        f"event loop starved during ingest: max heartbeat gap {max(gaps):.3f}s"
    )


# ── §7: heartbeat keeps ticking while the FIRST-ingest index build runs ──
@pytest.mark.asyncio
async def test_loop_responsive_while_first_ingest_index_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 3000, covered=False)
    router = _mk_router(tmp_path)

    builds = {"n": 0}
    real_builder = ts._build_coverage_index_sync

    def slow_builder(*a, **k):
        builds["n"] += 1
        time.sleep(0.5)  # slow cold build, via the offload substrate
        return real_builder(*a, **k)

    monkeypatch.setattr(ts, "_build_coverage_index_sync", slow_builder)

    t0 = time.monotonic()
    result, gaps = await _hb_gaps(router.ingest(_mk_env(rel)))
    ingest_wall = time.monotonic() - t0

    assert result == "enqueued"
    # Degraded-proceed: ingest must NOT wait for the cold build.
    assert ingest_wall < 0.45, f"ingest blocked on cold index build: {ingest_wall:.3f}s"
    assert gaps and max(gaps) < 0.3, (
        f"event loop starved while index built: max gap {max(gaps):.3f}s"
    )
    # Build was actually triggered off-loop (single-flight, fire-and-forget).
    deadline = time.monotonic() + 3.0
    while builds["n"] == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert builds["n"] == 1


# ── §8: N concurrent first-ingests → exactly ONE build, no deadlock ─────
@pytest.mark.asyncio
async def test_concurrent_first_ingests_single_flight_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rels = [_mk(tmp_path, f"backend/mod{i}.py", 3000, covered=False) for i in range(6)]
    router = _mk_router(tmp_path)

    builds = {"n": 0}
    real_builder = ts._build_coverage_index_sync

    def slow_builder(*a, **k):
        builds["n"] += 1
        time.sleep(0.3)
        return real_builder(*a, **k)

    monkeypatch.setattr(ts, "_build_coverage_index_sync", slow_builder)

    t0 = time.monotonic()
    results = await asyncio.wait_for(
        asyncio.gather(*(router.ingest(_mk_env(r, n=i)) for i, r in enumerate(rels))),
        timeout=5.0,
    )
    wall = time.monotonic() - t0

    assert all(r == "enqueued" for r in results)
    # Degraded-proceed: none of the 6 waited for the 0.3s build (nor serialized
    # 6 × inline scans — the pre-fix behavior).
    assert wall < 1.0, f"concurrent ingests blocked behind index build: {wall:.3f}s"
    # Ops that ran during the build got the DEGRADED path: no penalty stash.
    # (Design choice: semantic/stratification priors are advisory — intake
    # never waits on them; parity returns once the index is warm.)
    await asyncio.sleep(0.6)  # let the single-flight build finish
    assert builds["n"] == 1, f"expected exactly ONE build, got {builds['n']}"


# ── §9: warm index → penalty parity with the legacy inline path ─────────
@pytest.mark.asyncio
async def test_warm_index_restores_penalty_with_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    router = _mk_router(tmp_path)

    # First ingest: cold → degraded, triggers the off-loop build.
    env1 = _mk_env(rel, n=1)
    assert await router.ingest(env1) == "enqueued"
    assert "stratification_penalty" not in env1.evidence  # degraded

    deadline = time.monotonic() + 5.0
    while not ts.coverage_index_ready(tmp_path) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert ts.coverage_index_ready(tmp_path)

    # Second ingest: warm → penalty computed (off-loop) + evidence stashed,
    # numerically identical to the legacy inline computation.
    env2 = _mk_env(rel, n=2)
    assert await router.ingest(env2) == "enqueued"
    expected = ts.ingest_priority_penalty([rel], tmp_path)
    assert expected > 0
    assert env2.evidence.get("stratification_penalty") == expected


# ── §10: builder failure is fail-soft — ingest still enqueues ────────────
@pytest.mark.asyncio
async def test_index_build_failure_is_fail_soft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 3000, covered=False)
    router = _mk_router(tmp_path)

    builds = {"n": 0}

    def broken_builder(*a, **k):
        builds["n"] += 1
        raise RuntimeError("synthetic index build failure")

    monkeypatch.setattr(ts, "_build_coverage_index_sync", broken_builder)

    assert await router.ingest(_mk_env(rel, n=1)) == "enqueued"
    await asyncio.sleep(0.3)  # let the failed build task settle
    # Still degraded forever (never inline-scans on the loop), still enqueues.
    env2 = _mk_env(rel, n=2)
    assert await router.ingest(env2) == "enqueued"
    assert "stratification_penalty" not in env2.evidence
    await asyncio.sleep(0.2)
    # Failure cooldown: no rebuild storm (one attempt within the window).
    assert builds["n"] == 1
