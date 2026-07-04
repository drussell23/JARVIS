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


def _install_index(root: Path) -> None:
    """Build + install the coverage index synchronously (test-only shortcut).

    ``file_has_test_coverage`` is index-backed (fix/coverage-sync-inline-ast):
    COLD degrades neutral (every file treated covered → penalty 0), so any pin
    that needs a real uncovered verdict must warm the index first. Mirrors the
    slice48 helper. (``ts`` is the module import below — resolved at call time.)
    """
    idx = ts._build_coverage_index_sync(
        ts._resolve_scan_root(root),
        ts._strat_test_dir_names(),
    )
    with ts._COVERAGE_IDX_LOCK:
        ts._coverage_index[ts._resolve_scan_root(root)] = idx


# ── §1 ─────────────────────────────────────────────────────────────────
def test_no_files_or_covered_is_zero(tmp_path: Path) -> None:
    assert ingest_priority_penalty([], tmp_path) == 0
    rel = _mk(tmp_path, "backend/big.py", 5000, covered=True)
    _install_index(tmp_path)  # warm so "covered → 0" is a real verdict
    assert ingest_priority_penalty([rel], tmp_path) == 0


# ── §2 ─────────────────────────────────────────────────────────────────
def test_huge_uncovered_gets_penalty(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    _install_index(tmp_path)  # cold degrades neutral (penalty 0) by design
    assert ingest_priority_penalty([rel], tmp_path) > 0


# ── §3 ─────────────────────────────────────────────────────────────────
def test_suppress_escape_hatch(tmp_path: Path) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 5000, covered=False)
    assert ingest_priority_penalty([rel], tmp_path, suppress=True) == 0


# ── §4 ─────────────────────────────────────────────────────────────────
def test_penalty_is_bounded(tmp_path: Path) -> None:
    rels = [_mk(tmp_path, f"backend/h{i}.py", 9000, covered=False) for i in range(5)]
    _install_index(tmp_path)  # cold degrades neutral (penalty 0) by design
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
    _install_index(tmp_path)  # cold degrades neutral → both would rank equal

    common = dict(
        source="ai_miner",
        description="improve",
        repo="jarvis",
        confidence=0.5,
        urgency="normal",
        evidence={},
        requires_human_ack=True,
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

import backend.core.ouroboros.governance.cooperative_fs_io as _cfsio
import backend.core.ouroboros.governance.target_stratification as ts
from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope
from backend.core.ouroboros.governance.intake.unified_intake_router import (
    IntakeRouterConfig,
    UnifiedIntakeRouter,
)


def _patch_inproc_offload(monkeypatch: pytest.MonkeyPatch, seen: dict) -> None:
    """Route the coverage-index offload through an in-process, off-loop double.

    Fix 1 makes the real build dispatch with ``cpu_bound=True`` → a *spawn*
    ``ProcessPoolExecutor`` whose worker re-imports the module fresh, so a
    ``monkeypatch`` of ``_build_coverage_index_sync`` (how these orchestration
    tests inject slowness/failure and count builds) can never reach the worker
    process. This double keeps the orchestration under test deterministic and
    observable in-process while staying faithful on the load-bearing points:

      * it records the dispatch ``cpu_bound`` so these tests still GUARD that
        Fix 1 stayed ``cpu_bound=True`` (a revert to the thread pool trips the
        end-of-test assertion), and
      * it runs the (possibly-monkeypatched) fn via ``asyncio.to_thread`` —
        genuinely OFF the event loop, in this process — mirroring offload's
        fail-soft ``OffloadError`` contract on any raise.

    The REAL process-pool + GIL-freeing behaviour is proven separately by
    ``test_warm_index_restores_penalty_with_evidence`` (real-pool E2E) and
    ``test_offload_cpu_bound_frees_loop_during_gil_build`` (Fix 2).
    """

    async def _fake_offload(fn, *args, cpu_bound=False, **kwargs):
        # Record cpu_bound ONLY for the coverage-index build dispatch (the
        # intake path issues other cpu_bound=False offloads that would
        # otherwise clobber the guard). ``_build_coverage_index_sync`` is the
        # module global the build task looks up at call time, so it resolves
        # to whichever (possibly-monkeypatched) builder this test installed.
        if fn is ts._build_coverage_index_sync:
            seen["cpu_bound"] = cpu_bound
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — mirror offload fail-soft
            return _cfsio.OffloadError(
                fn_name=getattr(fn, "__name__", repr(fn)),
                exc_type=type(exc).__name__,
                message=str(exc),
                cpu_bound=cpu_bound,
            )

    monkeypatch.setattr(_cfsio, "offload", _fake_offload)


async def _require_process_pool() -> None:
    """Skip (not fail) when the real ``cpu_bound=True`` process pool can't spawn.

    The cpu_bound offload path needs a spawn ``ProcessPoolExecutor``; a
    sandboxed / process-creation-restricted test environment makes that
    unavailable, in which case offload returns a fail-soft ``OffloadError``.
    Tests that assert the REAL pool's behaviour probe it here and skip with a
    clear reason rather than reporting a spurious failure (run with the
    sandbox disabled to exercise them).
    """
    probe = await _cfsio.offload(_gil_busy_spin, 0.0, cpu_bound=True)
    if _cfsio.is_offload_error(probe):
        pytest.skip(f"process pool unavailable (sandbox?): {probe}")


def _gil_busy_spin(duration_s: float, *_a, **_k) -> int:
    """Module-level (picklable) pure-Python GIL-holding busy spin.

    Spins holding the GIL for ~``duration_s`` — NOT ``time.sleep`` (which
    releases the GIL). Module-level so it pickles by reference across the
    spawn process-pool boundary. Stand-in for ``_build_coverage_index_sync``'s
    real ``ast.parse``-over-963K-lines CPU cost.
    """
    end = time.monotonic() + duration_s
    total = 0
    while time.monotonic() < end:
        total += 1
    return total


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    assert (
        gaps and max(gaps) < 0.3
    ), f"event loop starved during ingest: max heartbeat gap {max(gaps):.3f}s"


# ── §7: heartbeat keeps ticking while the FIRST-ingest index build runs ──
@pytest.mark.asyncio
async def test_loop_responsive_while_first_ingest_index_builds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 3000, covered=False)
    router = _mk_router(tmp_path)

    builds = {"n": 0}
    real_builder = ts._build_coverage_index_sync

    def slow_builder(*a, **k):
        builds["n"] += 1
        time.sleep(0.5)  # slow cold build, via the (in-process) offload double
        return real_builder(*a, **k)

    monkeypatch.setattr(ts, "_build_coverage_index_sync", slow_builder)
    seen: dict = {}
    _patch_inproc_offload(monkeypatch, seen)

    t0 = time.monotonic()
    result, gaps = await _hb_gaps(router.ingest(_mk_env(rel)))
    ingest_wall = time.monotonic() - t0

    assert result == "enqueued"
    # Degraded-proceed: ingest must NOT wait for the cold build.
    assert ingest_wall < 0.45, f"ingest blocked on cold index build: {ingest_wall:.3f}s"
    assert (
        gaps and max(gaps) < 0.3
    ), f"event loop starved while index built: max gap {max(gaps):.3f}s"
    # Build was actually triggered off-loop (single-flight, fire-and-forget).
    deadline = time.monotonic() + 3.0
    while builds["n"] == 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert builds["n"] == 1
    # Guard Fix 1: the build dispatched with cpu_bound=True (process pool).
    assert seen.get("cpu_bound") is True


# ── §8: N concurrent first-ingests → exactly ONE build, no deadlock ─────
@pytest.mark.asyncio
async def test_concurrent_first_ingests_single_flight_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    seen: dict = {}
    _patch_inproc_offload(monkeypatch, seen)

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
    # Guard Fix 1: the build dispatched with cpu_bound=True (process pool).
    assert seen.get("cpu_bound") is True


# ── §9: warm index → penalty parity with the legacy inline path ─────────
@pytest.mark.asyncio
async def test_warm_index_restores_penalty_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _require_process_pool()  # real cpu_bound=True pool (sandbox → skip)
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rel = _mk(tmp_path, "backend/huge.py", 3000, covered=False)
    router = _mk_router(tmp_path)

    builds = {"n": 0}

    def broken_builder(*a, **k):
        builds["n"] += 1
        raise RuntimeError("synthetic index build failure")

    monkeypatch.setattr(ts, "_build_coverage_index_sync", broken_builder)
    seen: dict = {}
    _patch_inproc_offload(monkeypatch, seen)

    assert await router.ingest(_mk_env(rel, n=1)) == "enqueued"
    await asyncio.sleep(0.3)  # let the failed build task settle
    # Still degraded forever (never inline-scans on the loop), still enqueues.
    env2 = _mk_env(rel, n=2)
    assert await router.ingest(env2) == "enqueued"
    assert "stratification_penalty" not in env2.evidence
    await asyncio.sleep(0.2)
    # Failure cooldown: no rebuild storm (one attempt within the window).
    assert builds["n"] == 1
    # Guard Fix 1: even the failing build dispatched with cpu_bound=True.
    assert seen.get("cpu_bound") is True


# ── Fix 2: cpu_bound=True frees the loop during a GIL-HOLDING cold build ──
@pytest.mark.asyncio
async def test_offload_cpu_bound_frees_loop_during_gil_build(
    tmp_path: Path,
) -> None:
    """A GIL-holding pure-Python build (the real ``ast.parse`` cost class),
    dispatched through the SAME ``offload(..., cpu_bound=True)`` path the
    coverage-index build uses, must NOT starve the event loop.

    Unlike the ``time.sleep``-based responsiveness tests (which release the
    GIL and therefore prove only that intake doesn't *await* the build), this
    drives a genuine GIL-holding busy-spin. With cpu_bound=True the spin runs
    in a separate PROCESS, so a concurrent heartbeat coroutine keeps ticking;
    a thread offload (the pre-Fix-1 behaviour) would hold the GIL for the whole
    spin and the heartbeat would stall. Requires the real process pool → skips
    (not fails) where spawn is unavailable (sandboxed env).
    """
    await _require_process_pool()

    spin_s = 0.5
    ticks = {"n": 0}
    stop = asyncio.Event()

    async def _hb() -> None:
        while not stop.is_set():
            await asyncio.sleep(0.02)
            ticks["n"] += 1

    hb = asyncio.create_task(_hb())
    try:
        t0 = time.monotonic()
        result = await _cfsio.offload(_gil_busy_spin, spin_s, cpu_bound=True)
        elapsed = time.monotonic() - t0
    finally:
        stop.set()
        await hb

    assert not _cfsio.is_offload_error(result), result
    assert (
        elapsed >= spin_s * 0.8
    ), f"busy-spin returned too fast ({elapsed:.3f}s) — did it actually run?"
    # ~spin_s / 0.02s ≈ 25 ideal ticks; a thread offload would yield ~0.
    # Assert a healthy fraction to stay robust to scheduler jitter.
    assert ticks["n"] >= 10, (
        f"event loop starved during GIL-holding build: only {ticks['n']} "
        "heartbeat ticks (process pool did not free the GIL)"
    )
