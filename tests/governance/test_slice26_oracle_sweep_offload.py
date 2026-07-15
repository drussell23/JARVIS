"""Slice 26 — Oracle sweep off-loop + reservation-aware backpressure.

Soak bt-2026-07-15-192117 died heartbeat_stale (GIL holds 12.4→16.2→25.6s →
ExternalWatchdog SIGKILL) with the sweep ORCHESTRATION as the culprit — the
Slice 32 AST process pool was fine, but around it:
  * ``scan_dir`` ran the ENTIRE recursive walk in one ``asyncio.to_thread``
    hop — a pure-Python iterdir loop holding the GIL for the whole tree × 3
    repos with zero yield points;
  * ``incremental_update([])`` (falsy → full scan, gls.py oracle poll) had NO
    adaptive throttle — the AIMD + Memory Armor axes guarded only full_index;
  * the falsy branch re-embedded the ENTIRE graph every 300s poll, which also
    lazily fired the chroma init + the ~800MB embedder model load mid-sweep;
  * the EmbeddingService HIGH/LITE model constructors ran ON the asyncio loop
    thread (the ProactiveResourceGuard 800MB grant landed seconds before the
    death window), and no backpressure consumer knew about in-flight grants.

Root-cause fixes under test here:
  1. cooperative chunked tree walk (bounded per-chunk offloads via the shared
     cooperative_fs_io substrate, AIMD-adaptive chunk size, loop yields);
  2. multi-axis throttle on the incremental sweep;
  3. sweep embed scoped to actually-changed files;
  4. model constructors offloaded to the executor (all four sites);
  5. MemoryPressureGate reservation dimension (unsettled grants shrink the
     effective free-% for EVERY gate consumer — Oracle armor, scoper,
     SensorGovernor — strictest-wins, fail-open).
"""
from __future__ import annotations

import inspect
import threading
import time

import backend.core.ouroboros.oracle as O
from backend.core.ouroboros.governance import cooperative_fs_io as CFS
from backend.core.ouroboros.governance import memory_pressure_gate as MPG


# ── helpers ──────────────────────────────────────────────────────────


def _bare_oracle():
    """TheOracle without __init__ — just what _find_python_files needs."""
    inst = O.TheOracle.__new__(O.TheOracle)
    inst._shutting_down = False
    return inst


def _make_tree(tmp_path):
    """Small source tree with an excluded dir and non-.py noise."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "a.py").write_text("A = 1\n")
    (tmp_path / "pkg" / "sub" / "b.py").write_text("B = 2\n")
    (tmp_path / "top.py").write_text("T = 0\n")
    (tmp_path / "notes.txt").write_text("not python\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "ghost.py").write_text("EXCLUDED\n")
    return {"pkg/a.py", "pkg/sub/b.py", "top.py"}


# ── 1. chunk worker semantics ────────────────────────────────────────


def test_chunk_worker_one_level_exclusion_and_suffix(tmp_path):
    _make_tree(tmp_path)
    subdirs, files = O._scan_dir_chunk_worker([str(tmp_path)])
    # one level only: sees pkg/ (dir) + top.py (file); __pycache__ excluded,
    # notes.txt suffix-filtered, sub/ NOT yet visited.
    assert [s.rsplit("/", 1)[-1] for s in subdirs] == ["pkg"]
    assert sorted(f.rsplit("/", 1)[-1] for f in files) == ["top.py"]


def test_chunk_worker_never_raises_on_missing_dir(tmp_path):
    subdirs, files = O._scan_dir_chunk_worker([str(tmp_path / "gone")])
    assert subdirs == [] and files == []


# ── 2. cooperative walk ≡ legacy walk ────────────────────────────────


async def test_coop_walk_matches_legacy(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_ORACLE_COOP_SCAN_ENABLED", raising=False)
    expected = _make_tree(tmp_path)
    inst = _bare_oracle()
    coop = await inst._find_python_files(tmp_path)
    legacy = await inst._find_python_files_legacy(tmp_path)
    rel = lambda paths: {str(p.relative_to(tmp_path)) for p in paths}  # noqa: E731
    assert rel(coop) == rel(legacy) == expected


async def test_coop_walk_kill_switch_routes_to_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_COOP_SCAN_ENABLED", "0")
    inst = _bare_oracle()
    called = {}

    async def _sentinel(root):
        called["root"] = root
        return []

    inst._find_python_files_legacy = _sentinel
    assert await inst._find_python_files(tmp_path) == []
    assert called["root"] == tmp_path


async def test_coop_walk_offload_failure_fails_soft(tmp_path, monkeypatch):
    """OffloadError per chunk → bounded inline scan, walk still completes."""
    monkeypatch.delenv("JARVIS_ORACLE_COOP_SCAN_ENABLED", raising=False)
    expected = _make_tree(tmp_path)

    async def _broken_offload(fn, /, *args, **kwargs):
        return CFS.OffloadError(
            fn_name="x", exc_type="RuntimeError", message="boom", cpu_bound=False,
        )

    monkeypatch.setattr(CFS, "offload", _broken_offload)
    inst = _bare_oracle()
    got = {str(p.relative_to(tmp_path)) for p in await inst._find_python_files(tmp_path)}
    assert got == expected


async def test_coop_walk_shutdown_cancels(tmp_path):
    _make_tree(tmp_path)
    inst = _bare_oracle()
    inst._shutting_down = True
    assert await inst._find_python_files(tmp_path) == []


# ── 3. sweep throttle + scoped embed (wiring pins, Slice 25 style) ──


def test_scan_for_changes_returns_changed_and_is_throttled():
    src = inspect.getsource(O.TheOracle._scan_for_changes)
    assert "return changed" in src
    assert "_AdaptiveIndexThrottle" in src
    assert "_ORACLE_MEM_LAG_MULT" in src
    assert "critical_persist" in src           # armor can suspend the sweep
    assert "_oracle_sweep_backpressure_enabled" in src
    assert "self._shutting_down" in src        # cancellation token


def test_incremental_update_scopes_the_embed():
    src = inspect.getsource(O.TheOracle.incremental_update)
    assert "scanned_changed" in src
    assert "_oracle_scoped_sweep_embed_enabled" in src
    # a quiet sweep must embed NOTHING (no lazy chroma/model init on idle poll)
    assert "if changed_nodes:" in src


def test_mem_lag_mult_shared_not_duplicated():
    """The armor level→lag map is module-level; full_index aliases it."""
    assert O._ORACLE_MEM_LAG_MULT["critical"] == 4.0
    src = inspect.getsource(O.TheOracle._run_index_batches)
    assert "_MEM_LAG_MULT = _ORACLE_MEM_LAG_MULT" in src


def test_slice26_env_masters_default_true(monkeypatch):
    for var, fn in [
        ("JARVIS_ORACLE_COOP_SCAN_ENABLED", O._oracle_coop_scan_enabled),
        ("JARVIS_ORACLE_SWEEP_BACKPRESSURE_ENABLED", O._oracle_sweep_backpressure_enabled),
        ("JARVIS_ORACLE_SCOPED_SWEEP_EMBED_ENABLED", O._oracle_scoped_sweep_embed_enabled),
    ]:
        monkeypatch.delenv(var, raising=False)
        assert fn() is True
        monkeypatch.setenv(var, "0")
        assert fn() is False


def test_scan_chunk_dirs_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_ORACLE_SCAN_CHUNK_DIRS", "7")
    assert O._oracle_scan_chunk_dirs() == 7
    monkeypatch.setenv("JARVIS_ORACLE_SCAN_CHUNK_DIRS", "bogus")
    assert O._oracle_scan_chunk_dirs() == 32


# ── 4. embedder model load off-loop ──────────────────────────────────


async def test_construct_model_offloaded_runs_in_executor(monkeypatch):
    from backend.core.embedding_service import EmbeddingService

    monkeypatch.delenv("JARVIS_EMBED_MODEL_LOAD_OFFLOAD_ENABLED", raising=False)
    inst = EmbeddingService.__new__(EmbeddingService)
    name = await inst._construct_model_offloaded(
        lambda: threading.current_thread().name, "TEST",
    )
    assert name != threading.current_thread().name


async def test_construct_model_offloaded_kill_switch_inline(monkeypatch):
    from backend.core.embedding_service import EmbeddingService

    monkeypatch.setenv("JARVIS_EMBED_MODEL_LOAD_OFFLOAD_ENABLED", "0")
    inst = EmbeddingService.__new__(EmbeddingService)
    name = await inst._construct_model_offloaded(
        lambda: threading.current_thread().name, "TEST",
    )
    assert name == threading.current_thread().name


def test_all_four_model_load_sites_offloaded():
    from backend.core import budgeted_loaders
    from backend.core.embedding_service import EmbeddingService

    for meth in (
        EmbeddingService._try_load_pytorch_tier,
        EmbeddingService._try_load_fastembed_tier,
        EmbeddingService.maybe_promote_tier,
    ):
        assert "_construct_model_offloaded" in inspect.getsource(meth), meth
    src = inspect.getsource(budgeted_loaders.EmbeddingBudgetedLoader.load_with_grant)
    assert "run_in_executor" in src


# ── 5. reservation-aware backpressure ────────────────────────────────


def _probe(free_pct=25.0, total_gb=8.0):
    total = int(total_gb * 1024**3)
    return MPG.MemoryProbe(
        free_pct=free_pct,
        total_bytes=total,
        available_bytes=int(total * free_pct / 100.0),
        source="test",
        ok=True,
        error=None,
    )


class _StubGuard:
    def __init__(self, mb):
        self._mb = mb

    def unsettled_reservation_mb(self, settle_s):
        return self._mb


def _patch_guard(monkeypatch, mb):
    import backend.core.proactive_resource_guard as PRG

    monkeypatch.setattr(
        PRG, "get_proactive_resource_guard", lambda: _StubGuard(mb),
    )


def test_reservation_dim_escalates_level(monkeypatch):
    # free 25% of 8GB → WARN alone; 800MB unsettled ≈ 9.8% → adjusted ~15.2% → HIGH
    _patch_guard(monkeypatch, 800.0)
    monkeypatch.delenv("JARVIS_MEMORY_PRESSURE_RESERVATIONS_ENABLED", raising=False)
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    assert gate.pressure() is MPG.PressureLevel.HIGH


def test_reservation_dim_disabled_is_noop(monkeypatch):
    _patch_guard(monkeypatch, 800.0)
    monkeypatch.setenv("JARVIS_MEMORY_PRESSURE_RESERVATIONS_ENABLED", "false")
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    assert gate.pressure() is MPG.PressureLevel.WARN


def test_reservation_dim_fails_open(monkeypatch):
    import backend.core.proactive_resource_guard as PRG

    def _boom():
        raise RuntimeError("guard unavailable")

    monkeypatch.setattr(PRG, "get_proactive_resource_guard", _boom)
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    assert gate.pressure() is MPG.PressureLevel.WARN  # free-% only


def test_reservation_dominant_reason_and_dimension(monkeypatch):
    _patch_guard(monkeypatch, 800.0)
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    decision = gate.can_fanout(16)
    assert decision.level is MPG.PressureLevel.HIGH
    assert decision.reason_code.endswith("_via_reservations")
    assert decision.dominant_dimension == "reservations"
    assert decision.n_allowed <= MPG.high_fanout_cap()


def test_snapshot_carries_reservation_dim(monkeypatch):
    _patch_guard(monkeypatch, 800.0)
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    snap = gate.snapshot()
    assert snap["reservations"]["enabled"] is True
    assert snap["reservations"]["unsettled_mb"] == 800.0


def test_quiet_guard_keeps_legacy_level(monkeypatch):
    _patch_guard(monkeypatch, 0.0)
    gate = MPG.MemoryPressureGate(probe_fn=_probe)
    decision = gate.can_fanout(16)
    assert decision.level is MPG.PressureLevel.WARN
    assert decision.dominant_dimension == "free_pct"


def test_unsettled_reservation_mb_settle_window():
    from backend.core.proactive_resource_guard import (
        MemoryBudget,
        ProactiveResourceGuard,
    )

    guard = ProactiveResourceGuard.__new__(ProactiveResourceGuard)
    guard._budget_lock = threading.RLock()
    now = time.time()
    guard._budgets = {
        "sentence_transformer": MemoryBudget("sentence_transformer", 800, now - 5.0),
        "old_component": MemoryBudget("old_component", 400, now - 3600.0),
    }
    assert guard.unsettled_reservation_mb(settle_s=120.0) == 800.0
    assert guard.unsettled_reservation_mb(settle_s=7200.0) == 1200.0


def test_reservation_settle_s_env_driven(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_RESERVATION_SETTLE_S", "300")
    assert MPG.reservation_settle_s() == 300.0
    monkeypatch.delenv("JARVIS_MEMORY_RESERVATION_SETTLE_S", raising=False)
    assert MPG.reservation_settle_s() == 120.0


# ── 6. watchdog isolation invariant (Slice 47) untouched ─────────────


def test_no_watchdog_threshold_inflation():
    """Slice 26 is root-cause-only: the heartbeat/watchdog knobs must be
    untouched — grep the changed modules for any watchdog coupling."""
    for mod in (O,):
        src = inspect.getsource(mod)
        assert "HEARTBEAT_STALE" not in src
        assert "wall_clock" not in src.lower() or "watchdog" not in src.lower()
