"""Regression tests for the event-loop stalls sampled on 2026-10-15.

loop-stalls.log (11:20-11:24) caught the loop thread inside:
  1. backend/main.py health_check -- synchronous probes in the /health body
  2. situational_awareness/core_engine._get_system_metadata -- subprocess.run
     of the macOS-only ``sw_vers`` every scan
  3. coding_council/trinity/message_queue._get_conn -- sqlite3 on the loop
and the boot log showed process_cleanup_manager:
  4. rmtree of the live interpreter's own __pycache__ (WinError 145 race)
  5. a ``python -c`` learning-engine "shutdown" that could only fail or no-op
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import sqlite3
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# 3. Trinity message queue: every sqlite touch is off the loop
# ---------------------------------------------------------------------------

def _queue_module():
    from backend.core.coding_council.trinity import message_queue
    return message_queue


def test_queue_constructor_does_no_io(tmp_path):
    mq = _queue_module()
    db = tmp_path / "nested" / "queue.db"
    mq.PersistentMessageQueue(db_path=db)
    assert not db.parent.exists(), "constructor must not touch the filesystem"


def test_queue_sqlite_never_runs_on_loop_thread(tmp_path, monkeypatch):
    mq = _queue_module()
    real_connect = sqlite3.connect
    connect_threads = []

    def spy_connect(*args: Any, **kwargs: Any):
        connect_threads.append(threading.get_ident())
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(mq.sqlite3, "connect", spy_connect)

    async def scenario():
        loop_thread = threading.get_ident()
        q = mq.PersistentMessageQueue(db_path=tmp_path / "q.db")
        msg = mq.QueueMessage(channel="c", payload={"k": 1})
        assert await q.enqueue(msg) is True
        assert await q.enqueue(msg) is False  # dedupe still works
        got = await q.dequeue(channel="c")
        assert got is not None and got.id == msg.id
        assert got.status is mq.MessageStatus.PROCESSING
        assert await q.complete(msg.id) is True
        stats = await q.get_stats()
        assert stats["pending"] == 0
        return loop_thread

    loop_thread = asyncio.run(scenario())
    assert connect_threads, "queue never opened the database"
    assert loop_thread not in connect_threads


def test_queue_retry_dead_letter_does_not_self_deadlock(tmp_path):
    mq = _queue_module()

    async def scenario():
        q = mq.PersistentMessageQueue(db_path=tmp_path / "q.db")
        msg = mq.QueueMessage(channel="c", payload={"x": 1}, max_retries=1)
        await q.enqueue(msg)
        await q.dequeue(channel="c")
        assert await q.fail(msg.id, "boom") is True
        dead = await q.get_dead_letters()
        assert len(dead) == 1
        # Used to await enqueue() while holding the non-reentrant lock.
        ok = await asyncio.wait_for(q.retry_dead_letter(dead[0]["id"]), timeout=10)
        assert ok is True
        stats = await q.get_stats()
        assert stats["pending"] == 1 and stats["dead_letters"] == 0

    asyncio.run(scenario())


def test_queue_busy_timeout_is_env_driven(monkeypatch):
    mq = _queue_module()
    monkeypatch.setenv(mq._BUSY_TIMEOUT_ENV, "4.5")
    assert mq._busy_timeout_s() == 4.5
    monkeypatch.setenv(mq._BUSY_TIMEOUT_ENV, "garbage")
    assert mq._busy_timeout_s() == mq._BUSY_TIMEOUT_DEFAULT_S


# ---------------------------------------------------------------------------
# 2. Situational awareness: no subprocess for the OS version
# ---------------------------------------------------------------------------

def test_system_metadata_spawns_no_subprocess(monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    from backend.vision.situational_awareness import core_engine
    import subprocess

    def forbid(*_a: Any, **_k: Any):
        raise AssertionError("subprocess spawned from _get_system_metadata")

    monkeypatch.setattr(subprocess, "run", forbid)
    monkeypatch.setattr(subprocess, "Popen", forbid)
    core_engine._os_version.cache_clear()

    engine = core_engine.SituationalAwarenessEngine.__new__(
        core_engine.SituationalAwarenessEngine
    )
    meta = asyncio.run(engine._get_system_metadata())
    assert isinstance(meta["os_version"], str) and meta["os_version"]

    src = inspect.getsource(core_engine.SituationalAwarenessEngine._get_system_metadata)
    code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "sw_vers" not in code and "subprocess" not in code


# ---------------------------------------------------------------------------
# 1. /health: sync probes off-loop, and the probe never constructs a healer
# ---------------------------------------------------------------------------

def _main_functions(*names: str):
    """Compile selected top-level functions from backend/main.py in isolation
    (importing main builds the whole FastAPI app)."""
    tree = ast.parse((REPO_ROOT / "backend" / "main.py").read_text(encoding="utf-8"))
    nodes = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return nodes, [nodes[n] for n in names]


def test_health_check_offloads_sync_probes():
    nodes, _ = _main_functions()
    health = nodes["health_check"]
    src = ast.unparse(health) if hasattr(ast, "unparse") else ""
    calls = [
        n for n in ast.walk(health)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "call_off_loop"
    ]
    assert calls, "health_check must run its synchronous probes via call_off_loop"
    assert calls[0].args[0].id == "_collect_sync_health_details"
    for forbidden in ("get_self_healer", "current_pressure", "os.path.exists"):
        assert forbidden not in src, f"{forbidden} back on the loop in health_check"


def test_sync_health_details_never_constructs_self_healer(monkeypatch):
    _, fn_nodes = _main_functions("_loaded_rust_self_healer", "_collect_sync_health_details")
    module = ast.Module(body=fn_nodes, type_ignores=[])
    ns: dict = {"os": os, "sys": sys, "Any": Any}
    exec(compile(module, "backend/main.py", "exec"), ns)

    built = []
    fake = SimpleNamespace(
        peek_self_healer=lambda: None,
        get_self_healer=lambda: built.append(1),
    )
    monkeypatch.setitem(sys.modules, "vision.rust_self_healer", fake)
    monkeypatch.delitem(sys.modules, "backend.vision.rust_self_healer", raising=False)

    details = ns["_collect_sync_health_details"](SimpleNamespace())
    assert details["self_healing"] == {"enabled": False}
    assert not built

    report = {"running": True, "total_fix_attempts": 2, "success_rate": 0.5,
              "last_successful_build": None}
    healer = SimpleNamespace(get_health_report=lambda: report)
    fake.peek_self_healer = lambda: healer
    details = ns["_collect_sync_health_details"](SimpleNamespace())
    assert details["self_healing"]["enabled"] is True
    assert details["self_healing"]["fix_attempts"] == 2


def test_rust_self_healer_report_spawns_no_task():
    pytest.importorskip("psutil")
    pytest.importorskip("aiofiles")
    from backend.vision import rust_self_healer as rsh

    assert rsh.peek_self_healer() is rsh._self_healer
    healer = rsh.RustSelfHealer.__new__(rsh.RustSelfHealer)
    healer._running = False
    healer._fix_history = []
    healer._retry_counts = {}
    healer._last_successful_build = None
    healer._last_rust_working = None
    # No running loop: the old create_task() raised here.
    report = healer.get_health_report()
    assert report["is_rust_working"] is None


# ---------------------------------------------------------------------------
# 4 + 5. process_cleanup_manager
# ---------------------------------------------------------------------------

def _cleanup_manager(tmp_path):
    from backend.process_cleanup_manager import ProcessCleanupManager
    manager = ProcessCleanupManager.__new__(ProcessCleanupManager)
    manager.backend_path = tmp_path
    return manager


def test_cache_prune_keeps_live_pycache_and_removes_stale(tmp_path):
    pkg = tmp_path / "pkg"
    cache = pkg / "__pycache__"
    cache.mkdir(parents=True)
    (pkg / "live.py").write_text("x = 1\n")
    live_pyc = cache / "live.cpython-311.pyc"
    live_pyc.write_bytes(b"live")
    live_opt = cache / "live.cpython-39.opt-1.pyc"
    live_opt.write_bytes(b"live-opt")
    orphan_pyc = cache / "gone.cpython-311.pyc"
    orphan_pyc.write_bytes(b"orphan")
    legacy_sourceless = pkg / "deleted_module.pyc"
    legacy_sourceless.write_bytes(b"legacy")

    removed = _cleanup_manager(tmp_path)._clear_python_cache()

    assert live_pyc.exists() and live_opt.exists(), "live bytecode must be kept"
    assert cache.is_dir()
    assert not orphan_pyc.exists()
    assert not legacy_sourceless.exists()
    assert removed == 2


def test_cache_prune_tolerates_unremovable_entry(tmp_path, monkeypatch):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    stale = cache / "gone.cpython-311.pyc"
    stale.write_bytes(b"x")
    real_unlink = Path.unlink

    def locked(self, *a: Any, **k: Any):
        if self == stale:
            raise PermissionError(32, "in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", locked)
    assert _cleanup_manager(tmp_path)._clear_python_cache() == 0


def test_code_change_cleanup_spawns_no_learning_engine_subprocess():
    from backend.process_cleanup_manager import ProcessCleanupManager
    src = inspect.getsource(ProcessCleanupManager.cleanup_old_instances_on_code_change)
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    body = "\n".join(code_lines)
    assert "shutdown_learning_engine" not in body
    assert "sys.executable" not in body
