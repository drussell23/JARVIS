"""Teardown-hygiene spine — the post-SESSION-COMPLETE noise bundle.

Operator paste 2026-07-18 (session bt-2026-07-19-054703), four roots:

  #32 '--- Logging error --- ImportError: sys.meta_path is None' walls —
      the leak-logger handler fired during interpreter finalization.
  #33 'Task was destroyed but it is pending' — InputController reader
      (select-blocked thread outlived the stop shield) + TrinityEventBus
      loops (never stopped by the harness at all).
  #34 fresh '🧬 synthesizing' + EXHAUSTION storm AFTER the session
      report — workers dequeued NEW ops in the signal→GLS.stop window.
  #35 resource_tracker KeyError spam — the preemption shield SIGTERM'd
      multiprocessing's janitor process; the relaunched tracker's empty
      registry KeyError'd every later unregister.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# #32 — finalization guard
# ---------------------------------------------------------------------------


class TestFinalizationGuard:
    def test_leak_handler_guards_finalizing_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        body = src[src.index("def _harness_loop_exception_handler"):][:2000]
        assert "is_finalizing()" in body
        assert "meta_path is None" in body
        # The guard sits BEFORE the first logger call.
        assert body.index("is_finalizing()") < body.index("_async_leak_logger")


# ---------------------------------------------------------------------------
# #33 — self-pipe reader wake + event-bus teardown wiring
# ---------------------------------------------------------------------------


class _FdHolder:
    def __init__(self, fd: int, wake_r: int = -1) -> None:
        self._fd = fd
        self._wake_r = wake_r


class TestReaderSelfPipe:
    def test_wake_pipe_returns_instantly_not_at_timeout(self):
        from backend.core.ouroboros.governance import key_input
        stdin_r, stdin_w = os.pipe()      # stays empty — like a quiet TTY
        wake_r, wake_w = os.pipe()
        try:
            os.write(wake_w, b"\x00")     # stop() poked the pipe
            t0 = time.monotonic()
            out = key_input.InputController._blocking_read(
                _FdHolder(stdin_r, wake_r), 64,
            )
            elapsed = time.monotonic() - t0
            assert out == b""
            # Instant wake — far under the 0.5s select timeout.
            assert elapsed < 0.2
        finally:
            for fd in (stdin_r, stdin_w, wake_r, wake_w):
                os.close(fd)

    def test_stdin_data_still_delivered_with_wake_pipe_armed(self):
        from backend.core.ouroboros.governance import key_input
        stdin_r, stdin_w = os.pipe()
        wake_r, wake_w = os.pipe()
        try:
            os.write(stdin_w, b"q")
            out = key_input.InputController._blocking_read(
                _FdHolder(stdin_r, wake_r), 1,
            )
            assert out == b"q"
        finally:
            for fd in (stdin_r, stdin_w, wake_r, wake_w):
                os.close(fd)

    async def test_stop_writes_wake_byte_and_closes_pipes(self):
        from backend.core.ouroboros.governance.key_input import InputController
        c = InputController()
        c._wake_r, c._wake_w = os.pipe()
        wake_r = c._wake_r
        await c.stop()
        # Pipes closed + fields reset — no fd leak across restarts.
        assert c._wake_r == -1 and c._wake_w == -1
        with pytest.raises(OSError):
            os.fstat(wake_r)

    def test_harness_stops_trinity_event_bus_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        assert "shutdown_trinity_event_bus" in src
        # Bounded — a wedged bus never hangs the exit cinematic.
        idx = src.index("shutdown_trinity_event_bus(), timeout=")
        assert idx > 0


# ---------------------------------------------------------------------------
# #34 — post-signal work quiescence
# ---------------------------------------------------------------------------


class TestQuiesceGate:
    def test_request_quiesce_stops_worker_dequeue_loop(self):
        from backend.core.ouroboros.governance.background_agent_pool import (
            BackgroundAgentPool,
        )
        pool = BackgroundAgentPool.__new__(BackgroundAgentPool)
        pool._quiesced = False
        pool.request_quiesce()
        assert pool._quiesced is True
        pool.request_quiesce()                # idempotent
        assert pool._quiesced is True

    def test_worker_loop_honors_quiesce_pin(self):
        src = (
            _REPO
            / "backend/core/ouroboros/governance/background_agent_pool.py"
        ).read_text()
        body = src[src.index("async def _worker_loop"):][:1200]
        assert "while self._running and not self._quiesced:" in body

    def test_signal_handler_fires_quiesce_pin(self):
        src = (
            _REPO / "backend/core/ouroboros/battle_test/harness.py"
        ).read_text()
        body = src[src.index("def _handle_shutdown_signal"):][:4000]
        assert "request_quiesce()" in body


# ---------------------------------------------------------------------------
# #35 — orderly pool drain + resource_tracker exemption
# ---------------------------------------------------------------------------


class TestExecutorRegistry:
    def test_register_and_shutdown_all(self):
        from backend.core.ouroboros.governance import executor_registry

        class _Pool:
            def __init__(self) -> None:
                self.kw = None

            def shutdown(self, wait=True, cancel_futures=False):
                self.kw = (wait, cancel_futures)

        p1, p2 = _Pool(), _Pool()
        executor_registry.register(p1)
        executor_registry.register(p2)
        assert executor_registry.shutdown_all() >= 2
        assert p1.kw == (False, True)
        assert p2.kw == (False, True)

    def test_shutdown_all_survives_hostile_pool(self):
        from backend.core.ouroboros.governance import executor_registry

        class _Bomb:
            def shutdown(self, *a, **k):
                raise RuntimeError("pool exploded")

        class _Legacy:
            def __init__(self) -> None:
                self.called = False

            def shutdown(self, wait=True):   # no cancel_futures kwarg
                self.called = True

        bomb, legacy = _Bomb(), _Legacy()
        executor_registry.register(bomb)
        executor_registry.register(legacy)
        executor_registry.shutdown_all()     # must not raise
        assert legacy.called is True

    def test_registry_is_weak_never_extends_pool_life(self):
        import gc
        from backend.core.ouroboros.governance import executor_registry

        class _Pool:
            def shutdown(self, *a, **k):
                pass

        p = _Pool()
        executor_registry.register(p)
        del p
        gc.collect()
        # Registry drops the dead ref — shutdown_all never resurrects.
        assert all(
            type(x).__name__ != "_Pool" for x in list(executor_registry._POOLS)
        )


class TestResourceTrackerExemption:
    def test_shield_never_kills_the_janitor_pin(self):
        src = (
            _REPO
            / "backend/core/ouroboros/battle_test/graceful_preemption.py"
        ).read_text()
        body = src[src.index("def halt_child_workers"):]
        body = body[:body.index("def _run_git")]
        assert "resource_tracker" in body
        assert "_is_resource_tracker" in body
        # Drain precedes the sweep.
        assert body.index("shutdown_all") < body.index("terminate()")

    def test_long_lived_pools_register_pin(self):
        for rel in (
            "backend/core/ouroboros/governance/ast_compile_helper.py",
            "backend/core/ouroboros/governance/cooperative_fs_io.py",
            "backend/core/ouroboros/simulator.py",
        ):
            src = (_REPO / rel).read_text()
            assert "executor_registry" in src, f"{rel} missing registration"

    def test_halt_child_workers_smoke_no_children(self):
        """Callable end-to-end in a childless test process: drains the
        registry, sweeps nothing, returns 0, never raises."""
        from backend.core.ouroboros.battle_test.graceful_preemption import (
            halt_child_workers,
        )
        assert isinstance(halt_child_workers(), int)
