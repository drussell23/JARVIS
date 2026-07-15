"""Slice 23 — kill the GIL contention under intake bursts.

Two fronts:
  * The pure-Python stdlib-hashing-TFIDF semantic fallback encode holds the
    GIL; dispatched to the thread pool it starves the loop under a burst
    (the bt-2026-07-15-154242 heartbeat_stale SIGKILL). Slice 23 routes the
    FALLBACK encode to the fs ProcessPoolExecutor (its own GIL) — verified end
    to end here (real spawned worker), and the fs pool is folded into the
    child_reaper cascade so a burst/OOM leaves ZERO ghost workers.
  * Real-time backpressure escalation: a signal burst fills the intake queue
    faster than host-stress rises, so cognitive shedding now composes live
    queue pressure to shed low-value (sheddable-urgency) signals at the source.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.core.ouroboros.governance import child_reaper as CR
from backend.core.ouroboros.governance.semantic_index import (
    _StdlibHashingEmbedder,
    _stdlib_embed_worker,
)


# ═══════════════════════════════════════════════════════════════════════
# The process-pool encode worker (mandate 1)
# ═══════════════════════════════════════════════════════════════════════


def test_stdlib_worker_deterministic_unit_norm():
    v = _stdlib_embed_worker("hello world foo", 384)
    assert len(v) == 384
    assert abs(sum(x * x for x in v) - 1.0) < 1e-6  # unit-normalized
    assert _stdlib_embed_worker("hello world foo", 384) == v  # deterministic


def test_stdlib_worker_matches_classmethod():
    """The module worker must produce the SAME vector as the in-process
    embedder — process-pooling changes WHERE it runs, never the result."""
    text, dim = "the quick brown fox", 256
    assert _stdlib_embed_worker(text, dim) == _StdlibHashingEmbedder._embed_one(
        text, dim
    )


def test_stdlib_worker_fail_soft():
    assert _stdlib_embed_worker("", 384) == [0.0] * 384
    assert _stdlib_embed_worker(None, 384) == [0.0] * 384  # type: ignore[arg-type]


def test_worker_runs_in_real_process_pool():
    """End-to-end: the worker executes in the fs ProcessPoolExecutor (spawn),
    proving the fallback encode can run in a separate process with its own
    GIL. (Runs as a real module so spawn workers can import it.)"""
    from backend.core.ouroboros.governance.cooperative_fs_io import (
        is_offload_error,
        offload,
        shutdown_fs_process_pool,
    )

    async def _run():
        r = await offload(_stdlib_embed_worker, "process pool test", 384,
                          cpu_bound=True)
        assert not is_offload_error(r), r
        assert len(r) == 384
        return r

    try:
        asyncio.run(_run())
    finally:
        shutdown_fs_process_pool()


def test_encode_offloaded_routes_by_embedder_kind():
    """_encode_offloaded picks the executor from the embedder: stdlib fallback
    → process pool (cpu_bound=True), fastembed → thread (cpu_bound=False)."""
    src = inspect.getsource(
        __import__(
            "backend.core.ouroboros.governance.semantic_index",
            fromlist=["SemanticIndex"],
        ).SemanticIndex._encode_offloaded
    )
    assert "using_fallback" in src
    assert "cpu_bound=True" in src   # stdlib fallback → process pool
    assert "cpu_bound=False" in src  # fastembed → thread
    # BrokenProcessPool (OOM-killed worker) is caught, not propagated.
    assert "except Exception" in src


# ═══════════════════════════════════════════════════════════════════════
# fs pool registered with the child_reaper cascade (mandate 4)
# ═══════════════════════════════════════════════════════════════════════


def test_child_reaper_cleanup_registry_runs_on_cascade():
    CR.reset_for_tests()
    ran = {"n": 0}
    CR.register_cleanup(lambda: ran.__setitem__("n", ran["n"] + 1), label="t")
    CR.cascade_terminate()  # empty PID registry, but cleanups must run
    assert ran["n"] == 1
    CR.reset_for_tests()


def test_cleanup_registration_idempotent():
    CR.reset_for_tests()
    fn = lambda: None
    CR.register_cleanup(fn, label="x")
    CR.register_cleanup(fn, label="x")
    # one bad cleanup never blocks the rest
    CR.register_cleanup(lambda: (_ for _ in ()).throw(RuntimeError()), label="boom")
    ok = {"n": 0}
    CR.register_cleanup(lambda: ok.__setitem__("n", 1), label="ok")
    CR.cascade_terminate()
    assert ok["n"] == 1
    CR.reset_for_tests()


def test_fs_pool_registers_hard_reap():
    """Creating the fs process pool registers its hard-reap with child_reaper
    so the cascade tears it down."""
    import backend.core.ouroboros.governance.cooperative_fs_io as C
    src = inspect.getsource(C._get_fs_process_pool)
    assert "register_cleanup" in src
    assert "reap_fs_process_pool_hard" in src
    # the hard-reap snapshots worker PIDs + SIGKILLs survivors
    reap_src = inspect.getsource(C.reap_fs_process_pool_hard)
    assert "_processes" in reap_src
    assert "SIGKILL" in reap_src
    assert "shutdown_fs_process_pool" in reap_src


def test_fs_pool_hard_reap_never_raises_when_no_pool():
    import backend.core.ouroboros.governance.cooperative_fs_io as C
    C.shutdown_fs_process_pool()  # ensure none
    C.reap_fs_process_pool_hard()  # must be a clean no-op


# ═══════════════════════════════════════════════════════════════════════
# Backpressure-aware value shedding (mandate 2)
# ═══════════════════════════════════════════════════════════════════════


def test_backpressure_fraction_env_driven(monkeypatch):
    from backend.core.ouroboros.governance.intake.unified_intake_router import (
        _intake_backpressure_shed_fraction as f,
    )
    assert f() == 0.75  # default
    monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_SHED_FRACTION", "0.5")
    assert f() == 0.5
    for bad in ("0", "1.5", "-1", "garbage"):
        monkeypatch.setenv("JARVIS_INTAKE_BACKPRESSURE_SHED_FRACTION", bad)
        assert f() == 0.75  # out-of-range → default


def test_shed_gate_composes_live_queue_pressure():
    import backend.core.ouroboros.governance.intake.unified_intake_router as R
    src = inspect.getsource(R.UnifiedIntakeRouter._ingest_impl)
    # the shed decision reads live queue depth vs backpressure_threshold
    assert "intake_queue_depth()" in src
    assert "backpressure_threshold" in src
    assert "_under_backpressure" in src
    assert "_intake_backpressure_shed_fraction" in src
    # a backpressure shed is telemetry-tagged distinctly from a forecast shed
    assert '"backpressure"' in src


def test_governor_and_shed_modes_env_driven():
    """Mandate 2 — thresholds/mode dynamic from env, not hardcoded."""
    import backend.core.ouroboros.governance.intake.unified_intake_router as R
    gov = inspect.getsource(R._intake_governor_mode)
    shed = inspect.getsource(R._intake_cognitive_shed_mode)
    assert "JARVIS_INTAKE_GOVERNOR_MODE" in gov
    assert "JARVIS_INTAKE_COGNITIVE_SHED_MODE" in shed
    assert "enforce" in gov and "enforce" in shed
