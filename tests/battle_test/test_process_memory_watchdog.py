"""Arc B regression spine — ProcessMemoryWatchdog + Oracle cold-build
checkpoint.

Closes the structural blind spot behind the 52GB soak OOM:

  * MemoryPressureGate probes *system-wide free %* and only clamps
    NEW L3 fan-out — a single 52GB process tree on a 71%-free host
    reads "OK". It has no authority over a running in-process leaker.
  * Nothing in the harness probed *process* RSS at all.

Arc B adds (1) a monotonic per-batch Oracle checkpoint so a cold
build is durable, and (2) a process-tree-RSS watchdog that produces a
graceful summary-bearing stop BEFORE the kernel OOM-kills the tree.

Pins:
  * threshold resolution: absolute env, adaptive fraction, disabled
  * _fire_process_memory_cap stamps stop_reason + sets the race event
  * async monitor fires CAP when probed RSS exceeds the cap
  * Oracle cold-build calls _save_cache per the env-tunable cadence
  * AST pins: watchdog armed beside wall-clock; 5-way race wired;
    PROCESS_MEMORY_CAP termination cause exists
"""
from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.harness import BattleTestHarness
from backend.core.ouroboros.oracle import OracleConfig, TheOracle

_REPO = Path(__file__).resolve().parents[2]
_HARNESS_SRC = _REPO / "backend/core/ouroboros/battle_test/harness.py"
_TERMHOOK_SRC = _REPO / "backend/core/ouroboros/battle_test/termination_hook.py"

_PM_ENV = (
    "JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED",
    "JARVIS_PROCESS_MEMORY_CAP_MB",
    "JARVIS_PROCESS_MEMORY_WARN_MB",
    "JARVIS_PROCESS_MEMORY_CAP_FRACTION",
    "JARVIS_PROCESS_MEMORY_WATCHDOG_INTERVAL_S",
)


@pytest.fixture(autouse=True)
def _clean_pm_env(monkeypatch):
    for name in _PM_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def _bare_harness() -> BattleTestHarness:
    """A method-bearing instance without running the heavy __init__."""
    h = BattleTestHarness.__new__(BattleTestHarness)
    h._stop_reason = "unknown"
    h._session_dir = Path("/tmp/pm-watchdog-test")
    h._started_at = time.time()
    h._process_memory_event = asyncio.Event()
    h._process_memory_hard_deadline_stop = None
    h._oracle = None
    h._shutdown_watchdog = None
    return h


# ---------------------------------------------------------------------------
# Threshold resolution
# ---------------------------------------------------------------------------

def test_thresholds_absolute_env(monkeypatch):
    monkeypatch.setenv("JARVIS_PROCESS_MEMORY_CAP_MB", "4096")
    monkeypatch.setenv("JARVIS_PROCESS_MEMORY_WARN_MB", "3000")
    monkeypatch.setenv("JARVIS_PROCESS_MEMORY_WATCHDOG_INTERVAL_S", "9")
    warn, cap, interval = _bare_harness()._resolve_process_memory_thresholds()
    assert cap == 4096.0
    assert warn == 3000.0
    assert interval == 9.0


def test_thresholds_adaptive_fraction(monkeypatch):
    import psutil

    class _VM:
        total = 32 * 1024 * 1024 * 1024  # 32 GiB

    monkeypatch.setattr(psutil, "virtual_memory", lambda: _VM())
    monkeypatch.setenv("JARVIS_PROCESS_MEMORY_CAP_FRACTION", "0.5")
    warn, cap, _ = _bare_harness()._resolve_process_memory_thresholds()
    assert cap == pytest.approx(32 * 1024 * 0.5)  # 16384 MB
    assert warn == pytest.approx(cap * 0.85)
    assert warn < cap  # WARN must precede CAP


def test_thresholds_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("JARVIS_PROCESS_MEMORY_WATCHDOG_ENABLED", "false")
    _, cap, _ = _bare_harness()._resolve_process_memory_thresholds()
    assert cap is None, "master-switch off must DISABLE (cap=None)"


# ---------------------------------------------------------------------------
# Fire path + async monitor
# ---------------------------------------------------------------------------

async def test_fire_cap_stamps_stop_reason_and_sets_event():
    h = _bare_harness()
    await h._fire_process_memory_cap(rss_mb=9999.0, cap_mb=1024.0)
    assert h._stop_reason == "process_memory_cap"
    assert h._process_memory_event.is_set()


async def test_fire_cap_preserves_prior_stop_reason():
    h = _bare_harness()
    h._stop_reason = "shutdown_signal"  # an earlier classified stop
    await h._fire_process_memory_cap(rss_mb=9999.0, cap_mb=1024.0)
    assert h._stop_reason == "shutdown_signal"  # not clobbered
    assert h._process_memory_event.is_set()  # but still joins the race


async def test_async_monitor_fires_when_rss_exceeds_cap(monkeypatch):
    h = _bare_harness()
    monkeypatch.setattr(
        BattleTestHarness, "_probe_process_tree_rss_mb",
        staticmethod(lambda: 5000.0),
    )
    await asyncio.wait_for(
        h._monitor_process_memory(warn_mb=3000.0, cap_mb=4096.0,
                                  interval_s=0.01),
        timeout=5.0,
    )
    assert h._process_memory_event.is_set()
    assert h._stop_reason == "process_memory_cap"


async def test_async_monitor_quiet_below_warn(monkeypatch):
    h = _bare_harness()
    monkeypatch.setattr(
        BattleTestHarness, "_probe_process_tree_rss_mb",
        staticmethod(lambda: 100.0),
    )
    # Below WARN: the monitor must loop indefinitely without firing.
    # (It swallows CancelledError and returns cleanly, exactly like
    # WallClockWatchdog — so we cancel explicitly and assert no fire.)
    task = asyncio.ensure_future(
        h._monitor_process_memory(warn_mb=3000.0, cap_mb=4096.0,
                                  interval_s=0.01)
    )
    await asyncio.sleep(0.2)
    assert not task.done(), "monitor must keep running below WARN"
    assert not h._process_memory_event.is_set()
    assert h._stop_reason == "unknown"
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Oracle cold-build monotonic checkpoint (Arc B item 4)
# ---------------------------------------------------------------------------

async def _count_checkpoints(monkeypatch, every_n: str) -> int:
    monkeypatch.setenv("JARVIS_ORACLE_CHECKPOINT_EVERY_N_BATCHES", every_n)
    monkeypatch.setenv("JARVIS_QUIESCENCE_PROTOCOL_ENABLED", "false")
    oracle = TheOracle()
    bs = OracleConfig.MAX_PARALLEL_FILES
    n_files = bs * 3 - 5  # 3 batches (last partial)
    files = [Path(f"f{i}.py") for i in range(n_files)]

    async def _fake_find(_p):
        return files

    async def _fake_index(_rn, _rp, _fp):
        return None

    saves = {"n": 0}

    async def _fake_save():
        saves["n"] += 1

    monkeypatch.setattr(oracle, "_find_python_files", _fake_find)
    monkeypatch.setattr(oracle, "_index_file", _fake_index)
    monkeypatch.setattr(oracle, "_save_cache", _fake_save)
    await oracle._index_repository("jarvis", Path("/tmp/repo"))
    return saves["n"]


async def test_cold_build_checkpoints_every_batch(monkeypatch):
    # 3 batches: idx0 (skip: idx not >0), idx1 (1%1==0 -> save),
    # idx2 (last -> save) == 2 checkpoints.
    assert await _count_checkpoints(monkeypatch, "1") == 2


async def test_cold_build_checkpoint_cadence_env_tunable(monkeypatch):
    # every_n=2: idx1 (1%2!=0 skip), idx2 (last -> save) == 1.
    assert await _count_checkpoints(monkeypatch, "2") == 1


async def test_cold_build_checkpoint_disabled_when_zero(monkeypatch):
    # every_n=0 disables interim checkpoints entirely.
    assert await _count_checkpoints(monkeypatch, "0") == 0


# ---------------------------------------------------------------------------
# AST / structural pins
# ---------------------------------------------------------------------------

def test_ast_pin_watchdog_armed_beside_wall_clock():
    src = _HARNESS_SRC.read_text()
    assert "self._monitor_process_memory(" in src
    assert "self._process_memory_monitor_task = asyncio.ensure_future(" in src
    assert "self._start_process_memory_hard_deadline_thread(" in src
    # Armed in the same region as the wall-clock watchdog.
    wall_idx = src.index("[WallClockWatchdog] armed:")
    pm_idx = src.index("[ProcessMemoryWatchdog] armed:")
    assert 0 < pm_idx - wall_idx < 2000, (
        "ProcessMemoryWatchdog must be armed alongside WallClockWatchdog"
    )


def test_ast_pin_five_way_race_includes_process_memory():
    src = _HARNESS_SRC.read_text()
    assert "process_memory_waiter = asyncio.ensure_future(" in src
    assert 'self._stop_reason = "process_memory_cap"' in src
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "wait"
            and node.args
            and isinstance(node.args[0], ast.List)
        ):
            names = ast.unparse(node.args[0])
            if "process_memory_waiter" in names and "wall_clock_waiter" in names:
                found = True
                break
    assert found, "process_memory_waiter must join the FIRST_COMPLETED race"


def test_ast_pin_termination_cause_has_process_memory():
    assert 'PROCESS_MEMORY_CAP = "process_memory_cap"' in _TERMHOOK_SRC.read_text()
