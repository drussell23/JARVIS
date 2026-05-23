"""Regression spine for ``OperationAdvisor`` async-cache fix.

Pins the structural invariants that prevent event-loop starvation
during CLASSIFY phase Advisor evaluation — the failure mode
observed in stage-1 wiring soak 2026-05-12 (session
``bt-2026-05-13-054721``): first CLASSIFY took ~12 minutes
wall-clock between dispatch and Advisor verdict, subsequent ones
~60s each, dominating dispatch latency for every op flowing
through the harness.

Two coupled fixes:

1. **TTL'd memoization in ``OperationAdvisor._compute_blast_radius``**
   keyed on ``(frozenset(target_files), str(scan_root))``.  The
   cold scan reads every Python file in the project root (~29.5k
   files on this repo, ~15s wall-clock).  Without memoization,
   every op paid the full scan; with a 60s TTL, repeat calls
   (signal coalescing on the same target files; WAL replay of
   stuck envelopes) return in microseconds (114,000× speedup
   measured empirically).

2. **``asyncio.to_thread`` wrap at the two call sites**
   (``phase_runners/classify_runner.py`` primary + fallback,
   ``orchestrator.py`` secondary).  Even with the cache, the
   first-time scan still pays its ~15s cost — running that
   synchronously on the asyncio event loop starves every other
   coroutine (16 sensors + router dispatch + governed loop) for
   the duration.  Dispatching through ``to_thread`` decouples the
   wait from event-loop scheduling — same pattern as the
   SWE-Bench-Pro per-problem harness uses for git subprocess
   work.

Together: cold path stays bounded at one OS thread (the event
loop stays responsive throughout), warm path returns
sub-millisecond, and concurrent Advisor evaluations from
parallel BG-pool workers no longer serialize on a shared event
loop.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.operation_advisor import (
    OperationAdvisor,
    _ADVISOR_BLAST_EXECUTOR_MAX_WORKERS,
    _BLAST_RADIUS_CACHE_MAX_ENTRIES,
    _BLAST_RADIUS_CACHE_SHARED,
    _BLAST_RADIUS_CACHE_TTL_S,
    _get_advisor_blast_executor,
)


@pytest.fixture(autouse=True)
def _reset_shared_blast_radius_cache():
    """Clear the module-level shared cache between tests so they
    don't observe each other's writes.  Required because the cache
    is process-wide (a deliberate design choice — see
    ``_BLAST_RADIUS_CACHE_SHARED`` comment in the module — but tests
    are easier to reason about in isolation)."""
    _BLAST_RADIUS_CACHE_SHARED.clear()
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()


# ---------------------------------------------------------------------------
# Cache behavioral pins
# ---------------------------------------------------------------------------


def test_blast_radius_repeats_hit_cache(tmp_path):
    """Repeat calls with the same target_files MUST return cached
    result within the TTL window, not re-scan the filesystem.

    Empirical: cold scan ~15s on main repo, warm ~0.02ms (the
    speedup that lets the 12-min dispatch lag collapse).
    """
    # Tiny scan tree so cold isn't slow under CI
    (tmp_path / "a.py").write_text("import target_mod\n")
    (tmp_path / "b.py").write_text("# unrelated\n")
    advisor = OperationAdvisor(tmp_path)
    target_files = ("target_mod.py",)

    t0 = time.monotonic()
    r1 = advisor._compute_blast_radius(target_files)
    cold = time.monotonic() - t0

    t1 = time.monotonic()
    r2 = advisor._compute_blast_radius(target_files)
    warm = time.monotonic() - t1

    assert r1 == r2, f"Cached result diverged: cold={r1}, warm={r2}"
    assert warm < cold / 10, (
        f"Warm call ({warm*1000:.3f}ms) is not at least 10x faster "
        f"than cold ({cold*1000:.3f}ms) — cache is not memoizing."
    )


def test_blast_radius_cache_is_tuple_order_invariant(tmp_path):
    """The cache key uses ``frozenset(target_files)`` so coalesced
    envelopes that arrive with reordered target_files don't cause
    duplicate scans."""
    (tmp_path / "a.py").write_text("import x\n")
    advisor = OperationAdvisor(tmp_path)

    # Pre-warm with one order
    _ = advisor._compute_blast_radius(("a.py", "b.py", "c.py"))
    cache_size_before = len(advisor._blast_radius_cache)

    # Query with reordered tuple — should hit same cache entry
    _ = advisor._compute_blast_radius(("c.py", "a.py", "b.py"))
    cache_size_after = len(advisor._blast_radius_cache)

    assert cache_size_after == cache_size_before, (
        f"Tuple reordering created a duplicate cache entry: "
        f"before={cache_size_before}, after={cache_size_after}. "
        "The key must be frozenset(target_files), not the raw tuple."
    )


def test_blast_radius_cache_respects_ttl(tmp_path, monkeypatch):
    """After ``_BLAST_RADIUS_CACHE_TTL_S`` seconds an entry is
    stale and a fresh scan runs.

    Tested by monkeypatching the TTL to 0 — every call should
    re-scan, exercising the expiry branch.
    """
    (tmp_path / "a.py").write_text("import x\n")
    monkeypatch.setattr(operation_advisor, "_BLAST_RADIUS_CACHE_TTL_S", 0.0)
    advisor = OperationAdvisor(tmp_path)
    target_files = ("x.py",)

    # First call populates
    advisor._compute_blast_radius(target_files)
    # Wait 1ms so monotonic clock moves past 0
    time.sleep(0.001)
    # Second call must miss (TTL=0) — but result should still be correct
    r = advisor._compute_blast_radius(target_files)
    assert isinstance(r, int)


def test_blast_radius_cache_is_shared_across_advisor_instances(tmp_path):
    """Module-level cache MUST be shared across all OperationAdvisor
    instances pointing at the same scan_root.

    The production code instantiates a fresh ``OperationAdvisor``
    per CLASSIFY phase (classify_runner line ~278, orchestrator
    line ~1855), so a per-instance cache would be wasted across
    ops — that's exactly what stage-1 wiring soak 2026-05-13
    (session bt-2026-05-13-070956) caught: Advisor verdict took
    8m28s for a SWE-Bench-Pro envelope because each fresh
    instance re-scanned 29.5k files.  This test pins the shared
    surface so the regression can't sneak back.
    """
    # Tiny scan tree so cold is also fast
    (tmp_path / "a.py").write_text("import target_mod\n")
    advisor_one = OperationAdvisor(tmp_path)
    advisor_two = OperationAdvisor(tmp_path)
    target_files = ("target_mod.py",)

    # First instance populates the shared cache
    t0 = time.monotonic()
    r1 = advisor_one._compute_blast_radius(target_files)
    cold = time.monotonic() - t0

    # Second instance — DIFFERENT object — should hit the same cache
    t1 = time.monotonic()
    r2 = advisor_two._compute_blast_radius(target_files)
    warm = time.monotonic() - t1

    assert r1 == r2, (
        f"Cross-instance cache returned different result: "
        f"r1={r1} r2={r2}"
    )
    # Warm hit MUST be substantially faster — proves the cache is
    # actually shared (otherwise the second instance would do the
    # full scan).  Allowing 10x margin for noise on slow CI.
    assert warm < cold / 10 or warm < 0.001, (
        f"Second instance ({warm*1000:.3f}ms) was not at least 10x "
        f"faster than first ({cold*1000:.3f}ms) — the cache is per-"
        "instance, not shared.  Fresh OperationAdvisor instances in "
        "the production CLASSIFY path will re-pay the cold scan and "
        "the 12-min event-loop starvation will return."
    )


def test_blast_radius_cache_different_scan_roots_dont_collide(tmp_path):
    """Two advisors pointing at DIFFERENT roots MUST get distinct
    cache entries (worktree-aware ops with per-envelope scan_root
    must not cross-pollute)."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "f.py").write_text("import x\n")
    (root_b / "f.py").write_text("# different content\nimport x\nimport y\n")

    advisor_a = OperationAdvisor(root_a)
    advisor_b = OperationAdvisor(root_b)

    # Both query same target_files — but they live in different roots,
    # so cache_key (which includes str(scan_root)) MUST be distinct.
    advisor_a._compute_blast_radius(("x.py",))
    advisor_b._compute_blast_radius(("x.py",))

    # 2 distinct entries in the shared cache
    assert len(_BLAST_RADIUS_CACHE_SHARED) == 2, (
        f"Expected 2 cache entries (one per scan_root), got "
        f"{len(_BLAST_RADIUS_CACHE_SHARED)}.  Cache key MUST "
        "include str(scan_root) so worktree-aware advisors don't "
        "see each other's results."
    )


def test_blast_radius_cache_max_entries_evicts_fifo(tmp_path, monkeypatch):
    """Cache size MUST be bounded by
    ``_BLAST_RADIUS_CACHE_MAX_ENTRIES`` via FIFO eviction.  A
    runaway sensor that emits 10,000 distinct target file sets
    won't leak memory."""
    monkeypatch.setattr(
        operation_advisor, "_BLAST_RADIUS_CACHE_MAX_ENTRIES", 5,
    )
    advisor = OperationAdvisor(tmp_path)

    # Fill cache to 10 distinct entries
    for i in range(10):
        advisor._compute_blast_radius((f"file{i}.py",))

    assert len(advisor._blast_radius_cache) <= 5, (
        f"Cache grew to {len(advisor._blast_radius_cache)} entries "
        f"despite max={5} — eviction is broken."
    )
    # FIFO: oldest entries (file0..file4) should be evicted; newest
    # (file5..file9) should remain.
    cache_keys = list(advisor._blast_radius_cache.keys())
    surviving_files = {next(iter(fs)) for fs, _ in cache_keys}
    assert "file9.py" in surviving_files, "newest entry was evicted"
    assert "file0.py" not in surviving_files, "oldest entry survived"


# ---------------------------------------------------------------------------
# Behavioral pin — advise() survives event-loop contention via to_thread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advise_under_to_thread_survives_event_loop_contention(tmp_path):
    """When called via ``asyncio.to_thread``, ``advise()`` MUST
    complete promptly even with the event loop saturated by
    CPU-spinning coroutines.

    This is the production failure mode the fix closes: the
    classify_runner used to call ``advise()`` directly on the
    event loop, blocking the entire harness for the duration of
    the 29k-file scan.
    """
    # Tiny scan tree so cold scan isn't slow
    (tmp_path / "a.py").write_text("import x\n")
    (tmp_path / "b.py").write_text("pass\n")
    advisor = OperationAdvisor(tmp_path)

    contention_active = {"stop": False}

    async def _spinner():
        while not contention_active["stop"]:
            for _ in range(50_000):
                pass
            await asyncio.sleep(0)

    spinners = [asyncio.create_task(_spinner()) for _ in range(8)]
    try:
        t0 = time.monotonic()
        # 5 concurrent advise() calls dispatched through to_thread
        results = await asyncio.gather(*[
            asyncio.to_thread(
                advisor.advise,
                ("a.py",),
                "test op",
                f"op{i}",
            )
            for i in range(5)
        ])
        elapsed = time.monotonic() - t0
    finally:
        contention_active["stop"] = True
        for s in spinners:
            s.cancel()
        for s in spinners:
            try:
                await s
            except asyncio.CancelledError:
                pass

    assert len(results) == 5
    # Without to_thread, this would block for ~75s (15s × 5) on the
    # event loop with no progress on spinners.  With to_thread + cache,
    # it should be well under 5 seconds even on slow CI.
    assert elapsed < 5.0, (
        f"5 concurrent advise() calls under 8 spinners took "
        f"{elapsed:.2f}s — expected < 5s.  Either to_thread isn't "
        "actually dispatching to a worker pool OR the cache isn't "
        "memoizing the repeated scan."
    )


# ---------------------------------------------------------------------------
# AST pins — call sites use to_thread, not direct sync calls
# ---------------------------------------------------------------------------


def _classify_runner_ast():
    from backend.core.ouroboros.governance.phase_runners import (
        classify_runner,
    )
    src = Path(inspect.getfile(classify_runner)).read_text(encoding="utf-8")
    return src, ast.parse(src)


def _orchestrator_ast():
    from backend.core.ouroboros.governance import orchestrator
    src = Path(inspect.getfile(orchestrator)).read_text(encoding="utf-8")
    return src, ast.parse(src)


def _walk_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _is_to_thread_call_with_advise(call: ast.Call) -> bool:
    """Match ``asyncio.to_thread(_advisor.advise, ...)`` shape.

    Accepts:
    - asyncio.to_thread(_advisor.advise, ...)
    - asyncio.to_thread(<obj>.advise, ...) for any obj
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "to_thread":
        return False
    inner = func.value
    if not (isinstance(inner, ast.Name) and inner.id == "asyncio"):
        return False
    if not call.args:
        return False
    first = call.args[0]
    if not isinstance(first, ast.Attribute):
        return False
    return first.attr == "advise"


# Note: the prior ``test_classify_runner_advisor_calls_are_to_thread_wrapped``
# and ``test_orchestrator_advisor_call_is_to_thread_wrapped`` AST pins
# asserted the FIRST-iteration shape (``asyncio.to_thread(_advisor.advise, …)``).
# PR-B 2026-05-13 evolved that to ``_advisor.advise_async(…)`` so the
# dedicated bounded executor (not the default asyncio one) handles
# advisor blast scans.  The structural invariant is preserved — advisor
# work must NOT block the event loop — but the call shape changed.
# ``test_classify_runner_uses_advise_async_not_to_thread`` +
# ``test_orchestrator_uses_advise_async_not_to_thread`` below are the
# successor pins.


# ---------------------------------------------------------------------------
# Config knob pins — env-var-driven, sane defaults
# ---------------------------------------------------------------------------


def test_cache_ttl_has_sensible_default():
    """Default TTL is 60s — short enough to stay honest under
    fast-moving file changes, long enough for high hit rate."""
    assert 1.0 <= _BLAST_RADIUS_CACHE_TTL_S <= 600.0, (
        f"_BLAST_RADIUS_CACHE_TTL_S={_BLAST_RADIUS_CACHE_TTL_S} is "
        "outside sensible bounds [1s, 10min].  Default of 60s "
        "balances freshness vs hit rate; if the operator wants "
        "longer/shorter, JARVIS_ADVISOR_BLAST_RADIUS_CACHE_TTL_S "
        "is the env knob."
    )


def test_cache_max_entries_is_bounded():
    """Cache is memory-bounded.  Unbounded growth under a runaway
    sensor would defeat the purpose."""
    assert 16 <= _BLAST_RADIUS_CACHE_MAX_ENTRIES <= 100_000, (
        f"_BLAST_RADIUS_CACHE_MAX_ENTRIES={_BLAST_RADIUS_CACHE_MAX_ENTRIES} "
        "outside sensible bounds.  Default of 256 leaves headroom "
        "for ~50 typical entries; raise if hit rate measurements "
        "say so."
    )


# ---------------------------------------------------------------------------
# PR-B: Dedicated bounded executor for advisor blast work
# ---------------------------------------------------------------------------


def test_dedicated_executor_is_bounded_and_named():
    """The advisor's dedicated ThreadPoolExecutor MUST be a small,
    bounded pool with a distinct thread-name prefix so it's
    distinguishable from the default executor in stack traces /
    py-spy output.

    Operator binding 2026-05-13: advisor isolation is the point;
    blast scans are CPU-light I/O-heavy and ~2 workers is enough.
    Unbounded would defeat the isolation guarantee.
    """
    assert 1 <= _ADVISOR_BLAST_EXECUTOR_MAX_WORKERS <= 16, (
        f"_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS="
        f"{_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS} outside sensible "
        "[1, 16] range.  Default 2 keeps the pool small enough that "
        "advisor work can't congest itself."
    )
    executor = _get_advisor_blast_executor()
    assert executor is not None
    # Same instance returned on subsequent calls (singleton)
    assert _get_advisor_blast_executor() is executor
    # Bounded
    assert executor._max_workers == _ADVISOR_BLAST_EXECUTOR_MAX_WORKERS, (
        f"Executor max_workers ({executor._max_workers}) doesn't match "
        f"the module constant ({_ADVISOR_BLAST_EXECUTOR_MAX_WORKERS})"
    )
    # Distinct thread name (operator visibility)
    assert executor._thread_name_prefix == "advisor-blast", (
        f"Executor thread_name_prefix is "
        f"{executor._thread_name_prefix!r}, expected 'advisor-blast' "
        "(operator binding 2026-05-13 for stack-trace identification)"
    )


@pytest.mark.asyncio
async def test_advise_async_dispatches_to_dedicated_executor(
    tmp_path, monkeypatch,
):
    """``advise_async`` MUST run the underlying ``advise()`` call on
    a thread from the dedicated ``advisor-blast`` pool — NOT on the
    asyncio main thread, NOT on the default ThreadPoolExecutor.

    This is the legacy isolation contract for the **master-OFF
    rollback path** post-Slice 12S (2026-05-23). The default
    production path post-Slice 12S runs the scan ON the asyncio
    loop using ``cooperative_yield_every_n_async`` +
    ``offload_blocking`` (which gives the loop scheduling slots
    throughout the scan, solving the wedge LoopDeadman tripped on
    in bt-2026-05-23-171810). The cooperative-on contract is
    pinned in tests/governance/test_slice12s_advisor_blast_cooperative.py.

    Verified by inspecting ``threading.current_thread().name`` from
    inside the call when the cooperative master flag is FALSE.
    """
    import threading

    # Force the legacy thread-pool path so this test pins the
    # rollback contract specifically. Without this, Slice 12S
    # cooperative dispatch (default) runs the scan on the main
    # loop thread and the assertion below would fire on a
    # MainThread name — a SEMANTIC regression of the original
    # isolation guarantee even though the loop no longer needs it.
    monkeypatch.setenv(
        "JARVIS_ADVISOR_BLAST_COOPERATIVE_ENABLED", "false",
    )

    (tmp_path / "a.py").write_text("import x\n")
    advisor = OperationAdvisor(tmp_path)

    # Monkey-patch _compute_blast_radius to record the thread name
    captured = {}
    original = advisor._compute_blast_radius

    def _spy(*args, **kwargs):
        captured["thread_name"] = threading.current_thread().name
        return original(*args, **kwargs)

    advisor._compute_blast_radius = _spy  # type: ignore[method-assign]
    await advisor.advise_async(("x.py",), "test", "op-test")

    assert "thread_name" in captured
    thread_name = captured["thread_name"]
    assert thread_name.startswith("advisor-blast"), (
        f"advise_async ran on thread {thread_name!r}, expected one "
        "prefixed 'advisor-blast'.  This means the call is leaking "
        "back to asyncio.to_thread (default executor) — the "
        "isolation contract is broken and harness contention will "
        "re-introduce the v7 starvation pattern."
    )


@pytest.mark.asyncio
async def test_advise_async_isolated_from_default_executor_saturation(tmp_path):
    """Saturate the DEFAULT asyncio executor with 20 long-running
    tasks; advise_async MUST still complete promptly because it
    routes through the dedicated pool.

    This is the production failure mode that motivated PR-B:
    stage-1 wiring soak 2026-05-13 (session bt-2026-05-13-072716)
    showed advise() never completed in 360s when 16 sensors + Oracle
    + DreamEngine were also dispatching blocking I/O to the default
    executor.
    """
    (tmp_path / "a.py").write_text("import x\n")
    advisor = OperationAdvisor(tmp_path)

    async def _saturate_default_executor():
        # Each task sleeps 2s on a default-pool worker
        def _block():
            time.sleep(2.0)
        await asyncio.to_thread(_block)

    # Spawn 20 sensor-like tasks to saturate the default pool
    saturators = [
        asyncio.create_task(_saturate_default_executor())
        for _ in range(20)
    ]
    # Brief delay so the saturators actually start hogging
    await asyncio.sleep(0.05)

    # Now run 3 advisor advise_async — they should NOT queue behind
    # the saturators (different executor)
    t0 = time.monotonic()
    results = await asyncio.gather(*[
        advisor.advise_async(("x.py",), "test", f"op{i}")
        for i in range(3)
    ])
    elapsed = time.monotonic() - t0

    # Clean up
    await asyncio.gather(*saturators)

    assert len(results) == 3
    # Without PR-B, advise() under 20 default-pool saturators would
    # queue behind them — total time would be at least
    # ceil(20 / N_default_workers) × 2s ≈ 4-10s of wait + scan time.
    # With dedicated pool, advise should be unblocked from default-
    # pool contention.  10s ceiling is generous for slow CI.
    assert elapsed < 10.0, (
        f"3 advise_async calls under 20-task default-pool saturation "
        f"took {elapsed:.2f}s — expected < 10s.  The dedicated "
        "executor isn't actually isolating: either advise_async is "
        "still routing through the default pool, OR another part of "
        "advise() is on the event loop and blocking.  This is the "
        "v7 starvation pattern returning."
    )


def test_classify_runner_uses_advise_async_not_to_thread():
    """AST pin: classify_runner.py MUST use ``advisor.advise_async``,
    NOT ``asyncio.to_thread(advisor.advise, ...)``.

    The dedicated executor only kicks in via the advise_async path.
    A drift back to asyncio.to_thread would silently route advisor
    work through the default (contested) pool.
    """
    from backend.core.ouroboros.governance.phase_runners import (
        classify_runner,
    )
    src = Path(inspect.getfile(classify_runner)).read_text(encoding="utf-8")
    tree = ast.parse(src)

    advise_async_calls = 0
    to_thread_with_advise = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Count advise_async invocations
        if isinstance(func, ast.Attribute) and func.attr == "advise_async":
            advise_async_calls += 1
        # Flag any asyncio.to_thread(<x>.advise, ...) pattern
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "advise"
        ):
            to_thread_with_advise.append(
                f"line {node.lineno}: {ast.unparse(node)[:120]}"
            )

    assert advise_async_calls >= 2, (
        f"classify_runner.py has {advise_async_calls} advise_async "
        "calls, expected ≥ 2 (primary path + fallback).  PR-B "
        "wiring incomplete."
    )
    assert not to_thread_with_advise, (
        "classify_runner.py still has asyncio.to_thread(...advise, ...) "
        "call(s) — these route through the contested default executor "
        "and re-introduce the v7 advisor starvation:\n"
        + "\n".join(f"  - {s}" for s in to_thread_with_advise)
    )


def test_orchestrator_uses_advise_async_not_to_thread():
    """AST pin: orchestrator.py's parallel CLASSIFY path MUST also
    use ``advise_async``.  Same rationale as classify_runner — drift
    here would silently route to the default executor for any path
    that reaches it."""
    from backend.core.ouroboros.governance import orchestrator
    src = Path(inspect.getfile(orchestrator)).read_text(encoding="utf-8")
    tree = ast.parse(src)

    advise_async_calls = 0
    to_thread_with_advise = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "advise_async":
            advise_async_calls += 1
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
            and node.args
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == "advise"
        ):
            to_thread_with_advise.append(
                f"line {node.lineno}: {ast.unparse(node)[:120]}"
            )

    assert advise_async_calls >= 1, (
        f"orchestrator.py has {advise_async_calls} advise_async "
        "calls, expected ≥ 1 (legacy parallel CLASSIFY path)."
    )
    assert not to_thread_with_advise, (
        "orchestrator.py still has asyncio.to_thread(...advise, ...) "
        "call(s):\n"
        + "\n".join(f"  - {s}" for s in to_thread_with_advise)
    )
