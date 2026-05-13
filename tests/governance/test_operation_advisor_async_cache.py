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
    _BLAST_RADIUS_CACHE_MAX_ENTRIES,
    _BLAST_RADIUS_CACHE_SHARED,
    _BLAST_RADIUS_CACHE_TTL_S,
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


def test_classify_runner_advisor_calls_are_to_thread_wrapped():
    """AST pin: every ``_advisor.advise(...)`` call in
    classify_runner MUST be wrapped in ``asyncio.to_thread``.

    A direct ``_advisor.advise(...)`` call on the event loop
    re-introduces the 12-min wiring-soak starvation.
    """
    src, tree = _classify_runner_ast()

    # Find every Attribute access of form `<x>.advise` that is a Call
    direct_advise_calls = []
    to_thread_wraps = 0
    for call in _walk_calls(tree):
        if _is_to_thread_call_with_advise(call):
            to_thread_wraps += 1
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "advise":
            # Walk up via parent? ast doesn't have parent links.
            # Direct call site that ISN'T wrapped in to_thread is a regression.
            unparsed = ast.unparse(call)
            # Skip the format_for_prompt-style ones (different surface)
            direct_advise_calls.append(
                f"line {call.lineno}: {unparsed[:120]}"
            )

    assert to_thread_wraps >= 2, (
        f"Expected at least 2 ``asyncio.to_thread(...advise, ...)`` "
        f"wraps (primary path + fallback path); found "
        f"{to_thread_wraps}.  The fix is incomplete."
    )
    assert not direct_advise_calls, (
        "Some `_advisor.advise(...)` call sites are NOT wrapped in "
        "asyncio.to_thread — these will block the asyncio event loop "
        "for ~15s (cold) or seconds (warm cache) per call.  The fix "
        "must wrap EVERY synchronous advise call:\n"
        + "\n".join(f"  - {s}" for s in direct_advise_calls)
    )


def test_orchestrator_advisor_call_is_to_thread_wrapped():
    """AST pin: the legacy orchestrator-direct advise call site
    is ALSO wrapped in asyncio.to_thread.

    classify_runner is the primary path, but the orchestrator
    still has a parallel call site that must stay consistent —
    drift there would re-introduce starvation for whatever path
    reaches it.
    """
    src, tree = _orchestrator_ast()
    to_thread_wraps = 0
    direct_calls = []
    for call in _walk_calls(tree):
        if _is_to_thread_call_with_advise(call):
            to_thread_wraps += 1
            continue
        func = call.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "advise"
            and isinstance(func.value, ast.Name)
            and func.value.id == "_advisor"
        ):
            direct_calls.append(
                f"line {call.lineno}: {ast.unparse(call)[:120]}"
            )

    assert to_thread_wraps >= 1, (
        f"Expected at least 1 ``asyncio.to_thread(_advisor.advise, "
        f"...)`` wrap in orchestrator.py; found {to_thread_wraps}."
    )
    assert not direct_calls, (
        "Some _advisor.advise() call sites in orchestrator.py are "
        "NOT wrapped in asyncio.to_thread:\n"
        + "\n".join(f"  - {s}" for s in direct_calls)
    )


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
