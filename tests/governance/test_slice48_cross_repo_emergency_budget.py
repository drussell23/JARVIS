"""Slice 48 — cross_repo_cleanup bounded emergency teardown.

The v43 soak (bt-2026-05-30-221223) exited via the BoundedShutdownWatchdog
(os._exit(75)) because _sync_emergency_cleanup blocked 38.6s in a synchronous
registry open()/stat after a host resume — blowing the 30s teardown budget.

This is an atexit path (no running event loop), so the fix is a daemon-thread
wrapper with a join(timeout=budget): the cleanup gets its budget, then the
main teardown proceeds regardless so an uninterruptible syscall can never
stall past the ShutdownWatchdog deadline.

Pins:
  §1  a blocking cleanup callback cannot stall the call past the budget
  §2  fast cleanup still runs callbacks + marks cleaned-up
  §3  idempotent — second call is a no-op
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from backend.core.cross_repo_cleanup import CrossRepoCleanupCoordinator


def _bare_coordinator() -> CrossRepoCleanupCoordinator:
    """A coordinator instance that skips the singleton/atexit __init__."""
    coord = object.__new__(CrossRepoCleanupCoordinator)
    coord._cleaned_up = False
    coord._cleanup_callbacks = {}
    coord._async_cleanup_callbacks = {}
    coord._repo_name = "test-repo"
    coord.registry = types.SimpleNamespace(
        clear_repo_resources=lambda repo: 0,
    )
    return coord


# ── §1 blocking callback cannot stall past budget ───────────────────────
def test_blocking_cleanup_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_CROSS_REPO_EMERGENCY_CLEANUP_BUDGET_S", "0.3")
    coord = _bare_coordinator()
    release = threading.Event()
    coord._cleanup_callbacks["slow"] = lambda: release.wait(timeout=10.0)

    start = time.monotonic()
    coord._sync_emergency_cleanup()
    elapsed = time.monotonic() - start
    release.set()  # let the leaked daemon thread unwind cleanly

    assert elapsed < 2.0, f"teardown blocked {elapsed:.2f}s — budget not enforced"
    assert coord._cleaned_up is True


# ── §2 fast path still runs callbacks ───────────────────────────────────
def test_fast_cleanup_runs_callbacks() -> None:
    coord = _bare_coordinator()
    called: list[int] = []
    coord._cleanup_callbacks["fast"] = lambda: called.append(1)

    coord._sync_emergency_cleanup()

    assert called == [1]
    assert coord._cleaned_up is True


# ── §3 idempotent ───────────────────────────────────────────────────────
def test_emergency_cleanup_idempotent() -> None:
    coord = _bare_coordinator()
    calls: list[int] = []
    coord._cleanup_callbacks["c"] = lambda: calls.append(1)

    coord._sync_emergency_cleanup()
    coord._sync_emergency_cleanup()

    assert calls == [1]  # second call is a no-op
