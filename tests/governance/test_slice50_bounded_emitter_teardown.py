"""Slice 50 Phase 1 — Bounded watchdog observer teardown.

Backstory (v45 probe, session bt-2026-06-01-034745, 2026-05-31):
    The wall-clock cap fired cleanly at 300s, but teardown then wedged
    ~57s inside the third-party ``watchdog`` library:

        watchdog/observers/api.py:372  unschedule_all
        watchdog/observers/api.py:246  _clear_emitters  ->  emitter.join()
        threading.py                   _wait_for_tstate_lock  ->  lock.acquire()

    ``BaseObserver.stop()`` synchronously calls ``on_thread_stop()`` ->
    ``unschedule_all()`` -> ``_clear_emitters()`` -> ``emitter.join()`` with
    NO timeout. With the runaway-watching guard scheduling 42 PollingObserver
    roots (one polling thread each, mid ``os.scandir`` walk of nested venvs),
    that unbounded join blocks for as long as the slowest walk takes.

    ``FileWatchGuard._stop_watchdog`` called ``self._observer.stop()``
    DIRECTLY (line 886), so the hang happened on the event loop / shutdown
    path before the bounded ``join(timeout=5.0)`` on line 887 was ever
    reached. The in-process ShutdownWatchdog logged ``os._exit(75)`` but the
    process survived ~30s past its deadline; only the Slice 49 external
    watchdog guaranteed death.

    watchdog observer + emitter threads are ``daemon=True`` (verified), so
    the correct fix is to run the blocking ``stop()`` on a daemon helper
    thread, bound-join it against a policy deadline
    (``JARVIS_EMITTER_TEARDOWN_DEADLINE_S``, default 10s), and on timeout
    ABANDON the daemon handle (flag the leak, clear the exit path) instead
    of letting it loop-lock component shutdown.

These tests lock that behavior down.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from backend.core.resilience.file_watch_guard import FileWatchGuard


class _HangingObserver:
    """Fake watchdog observer whose ``stop()`` blocks like the wedged
    PollingObserver emitter join — finite so a regressed (unbounded) code
    path fails in bounded time rather than hanging the suite forever."""

    def __init__(self, block_s: float = 6.0) -> None:
        self.stop_called = False
        self.join_called = False
        self._gate = threading.Event()
        self._block_s = block_s

    def stop(self) -> None:
        self.stop_called = True
        # Mimics the unbounded emitter.join() inside BaseObserver.stop().
        self._gate.wait(timeout=self._block_s)

    def join(self, timeout=None) -> None:  # noqa: ANN001
        self.join_called = True

    def release(self) -> None:
        self._gate.set()


class _FastObserver:
    """Well-behaved observer — stop()/join() return immediately."""

    def __init__(self) -> None:
        self.stop_called = False
        self.join_called = False

    def stop(self) -> None:
        self.stop_called = True

    def join(self, timeout=None) -> None:  # noqa: ANN001
        self.join_called = True


def _make_guard(tmp_path: Path) -> FileWatchGuard:
    return FileWatchGuard(watch_dir=tmp_path, on_event=lambda _e: None)


@pytest.mark.asyncio
async def test_stop_watchdog_is_bounded_when_observer_stop_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wedge regression: a hanging observer.stop() must NOT block
    the teardown coroutine past the policy deadline."""
    monkeypatch.setenv("JARVIS_EMITTER_TEARDOWN_DEADLINE_S", "0.3")
    guard = _make_guard(tmp_path)
    obs = _HangingObserver(block_s=6.0)
    guard._observer = obs

    t0 = time.monotonic()
    await guard._stop_watchdog()
    elapsed = time.monotonic() - t0

    # Must return within ~deadline, NOT after the 6s block.
    assert elapsed < 2.0, f"teardown hung {elapsed:.2f}s — deadline not enforced"
    assert obs.stop_called, "observer.stop() should still be attempted"
    # Handle abandoned so the exit path is cleared.
    assert guard._observer is None
    obs.release()  # let the daemon helper unwind for clean test teardown


@pytest.mark.asyncio
async def test_stop_watchdog_normal_path_completes(tmp_path: Path) -> None:
    """Zero regression: a well-behaved observer is stopped AND joined."""
    guard = _make_guard(tmp_path)
    obs = _FastObserver()
    guard._observer = obs

    await guard._stop_watchdog()

    assert obs.stop_called
    assert obs.join_called
    assert guard._observer is None


@pytest.mark.asyncio
async def test_stop_watchdog_no_observer_is_noop(tmp_path: Path) -> None:
    """No observer attached -> clean no-op (no exception)."""
    guard = _make_guard(tmp_path)
    guard._observer = None
    await guard._stop_watchdog()  # must not raise
    assert guard._observer is None


@pytest.mark.asyncio
async def test_emitter_teardown_deadline_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline is policy-driven via JARVIS_EMITTER_TEARDOWN_DEADLINE_S."""
    monkeypatch.setenv("JARVIS_EMITTER_TEARDOWN_DEADLINE_S", "0.5")
    guard = _make_guard(tmp_path)
    obs = _HangingObserver(block_s=6.0)
    guard._observer = obs

    t0 = time.monotonic()
    await guard._stop_watchdog()
    elapsed = time.monotonic() - t0

    # Honors the 0.5s override: returns well after 0.5 but well before 6.
    assert 0.4 <= elapsed < 2.5, f"deadline override not honored (elapsed={elapsed:.2f}s)"
    obs.release()
